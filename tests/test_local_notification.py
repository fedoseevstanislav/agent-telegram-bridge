import io
import json
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from bridge import cli, common, daemon


def _dialog(name="Codex task", pane="%8", **extra):
    return {
        "name": name,
        "created": "2026-08-02T12:00:00+0000",
        "pane": pane,
        "engine": "codex",
        **extra,
    }


# Every notify case below is the server-automation caller (no dialog topic of its own), which
# is the path that must keep the `notification` contract and the required `--sender`. The
# session-pane path is covered in test_peer_identity.py. `conftest` already unbinds
# TMUX_PANE, so these callers do not resolve.
_CFG = {"bot_token": "token", "chat_id": -100}


def _echoing_tmux(literals=None, calls=None):
    """tmux stand-in for a pane that ACCEPTS typed text.

    Since #133 the daemon captures the pane and only presses Enter once it can see the text
    it typed, so a stub that answers `capture-pane` with nothing models a pane whose input
    was swallowed — and no nudge is sent, correctly. These tests are about the delivery
    contract, so they need a pane that echoes."""
    screen = []

    def tmux(argv, **kwargs):
        if calls is not None:
            calls.append((argv, kwargs))
        if argv[:2] == ["tmux", "display-message"]:
            # Geometry fingerprint (#165): type_line refuses to compare two captures taken
            # across a resize, so a pane that echoes must also report a stable geometry.
            return SimpleNamespace(returncode=0, stdout="100,40,80,0")
        if argv[:2] == ["tmux", "capture-pane"]:
            return SimpleNamespace(returncode=0, stdout="\n".join(screen))
        if "-l" in argv:
            if literals is not None:
                literals.append(argv)
            screen.append(argv[-1])
        return SimpleNamespace(returncode=0)

    return tmux


def test_current_topic_returns_stable_json_metadata(monkeypatch, capsys):
    monkeypatch.setenv("TMUX_PANE", "%8")
    monkeypatch.setattr(cli, "read_registry", lambda: {"4109": _dialog(cwd="/srv/task")})
    monkeypatch.setattr(cli, "pane_alive", lambda pane: pane == "%8", raising=False)

    cli.cmd_current_topic(None, None)

    assert json.loads(capsys.readouterr().out) == {
        "cwd": "/srv/task",
        "engine": "codex",
        "name": "Codex task",
        "pane": "%8",
        "topic_id": 4109,
    }


@pytest.mark.parametrize(
    ("registry", "message"),
    [
        ({}, "no topic"),
        ({"10": _dialog(feed=True)}, "feed"),
        ({"10": _dialog(ended="2026-08-02T12:30:00+0000")}, "ended"),
        ({"10": _dialog(), "11": _dialog(name="other")}, "ambiguous"),
    ],
)
def test_current_topic_rejects_missing_feed_ended_and_ambiguous_bindings(
    monkeypatch, registry, message
):
    monkeypatch.setenv("TMUX_PANE", "%8")
    monkeypatch.setattr(cli, "read_registry", lambda: registry)
    monkeypatch.setattr(cli, "pane_alive", lambda pane: True, raising=False)

    with pytest.raises(SystemExit, match=message):
        cli.cmd_current_topic(None, None)


def test_current_topic_rejects_missing_tmux_pane(monkeypatch):
    monkeypatch.delenv("TMUX_PANE", raising=False)
    with pytest.raises(SystemExit, match="TMUX_PANE"):
        cli.cmd_current_topic(None, None)


def test_current_topic_rejects_dead_pane(monkeypatch):
    monkeypatch.setenv("TMUX_PANE", "%8")
    monkeypatch.setattr(cli, "read_registry", lambda: {"4109": _dialog()})
    monkeypatch.setattr(cli, "pane_alive", lambda pane: False, raising=False)

    with pytest.raises(SystemExit, match="not live"):
        cli.cmd_current_topic(None, None)


def test_append_jsonl_once_is_durable_and_idempotent(tmp_path):
    path = tmp_path / "inbox.jsonl"
    record = {
        "ts": "2026-08-02T12:00:00+0000",
        "from": "automation",
        "kind": "notification",
        "text": "review finished",
        "provenance": "local-notify",
        "idempotency_key": "review-125-r1",
    }

    assert common.append_jsonl_once(str(path), record) == (True, common.WakeClaim(0))
    assert common.append_jsonl_once(str(path), record) == (False, None)
    assert [json.loads(line) for line in path.read_text().splitlines()] == [record]


def test_append_jsonl_once_same_key_is_atomic_under_concurrency(tmp_path):
    path = tmp_path / "inbox.jsonl"
    record = {
        "ts": "2026-08-02T12:00:00+0000",
        "from": "automation",
        "kind": "notification",
        "text": "review finished",
        "provenance": "local-notify",
        "idempotency_key": "review-125-r1",
    }
    workers = 8
    start = threading.Barrier(workers)

    def append():
        start.wait(timeout=5)
        return common.append_jsonl_once(str(path), record)

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(append) for _ in range(workers)]
        results = [future.result() for future in futures]

    assert results.count((True, common.WakeClaim(0))) == 1
    assert results.count((False, None)) == workers - 1
    assert len(path.read_text().splitlines()) == 1


def test_telegram_and_local_writers_choose_one_first_unread_under_concurrency(tmp_path):
    path = tmp_path / "inbox.jsonl"
    telegram_record = {
        "ts": "2026-08-02T12:00:00+0000",
        "from": "the owner",
        "kind": "text",
        "text": "Telegram event",
    }
    local_record = {
        "ts": "2026-08-02T12:00:01+0000",
        "from": "automation",
        "kind": "notification",
        "text": "Local event",
        "provenance": "local-notify",
        "idempotency_key": "review-125-r2",
    }
    start = threading.Barrier(2)

    def append_telegram():
        start.wait(timeout=5)
        return common.append_jsonl(str(path), telegram_record)

    def append_local():
        start.wait(timeout=5)
        return common.append_jsonl_once(str(path), local_record)

    with ThreadPoolExecutor(max_workers=2) as executor:
        telegram_future = executor.submit(append_telegram)
        local_future = executor.submit(append_local)
        telegram_claim = telegram_future.result()
        local_appended, local_claim = local_future.result()

    assert local_appended is True
    assert sum(claim is not None for claim in (telegram_claim, local_claim)) == 1
    assert len(path.read_text().splitlines()) == 2


@pytest.mark.parametrize("wake_claim", [common.WakeClaim(0), None])
def test_telegram_ingress_passes_atomic_wake_claim_to_scheduler(
    monkeypatch, wake_claim
):
    scheduled = []
    monkeypatch.setattr(daemon, "carry_forward_active", lambda _topic: False)
    monkeypatch.setattr(daemon, "append_jsonl", lambda _path, _record: wake_claim)
    monkeypatch.setattr(daemon, "maybe_auto_revive", lambda _cfg, _topic: None)
    monkeypatch.setattr(
        daemon, "schedule_nudge",
        lambda topic, is_first: scheduled.append((topic, is_first)),
    )

    daemon.handle_message(
        {"chat_id": -1001, "owner_id": 42},
        {
            "chat": {"id": -1001},
            "from": {"id": 42, "first_name": "the owner"},
            "message_thread_id": 4109,
            "message_id": 101,
            "text": "Telegram event",
        },
    )

    assert scheduled == [(4109, wake_claim)]


def test_daemon_schedules_delayed_nudge_only_for_first_unread(monkeypatch):
    timers = []

    class Timer:
        def __init__(self, delay, target, args):
            timers.append((delay, target, args))

        def start(self):
            timers.append("started")

    monkeypatch.setattr(daemon, "read_registry", lambda: {"4109": _dialog()})
    monkeypatch.setattr(daemon.threading, "Timer", Timer)

    daemon.schedule_nudge(4109, None)
    assert timers == []

    claim = common.WakeClaim(0)
    daemon.schedule_nudge(4109, claim)
    assert timers == [
        (daemon.NUDGE_DELAY, daemon.maybe_nudge, (4109, "%8", claim)),
        "started",
    ]


def test_notify_persists_before_wake_then_posts_to_telegram(monkeypatch, tmp_path):
    events = []
    monkeypatch.setattr(common, "STATE_DIR", str(tmp_path))
    monkeypatch.setattr(cli, "read_registry", lambda: {"4109": _dialog()})
    monkeypatch.setattr(cli, "pane_alive", lambda pane: True, raising=False)

    real_append = common.append_jsonl_once

    def append(path, record):
        result = real_append(path, record)
        events.append("persist")
        return result

    monkeypatch.setattr(cli, "append_jsonl_once", append, raising=False)
    monkeypatch.setattr(cli, "maybe_nudge", lambda *args: events.append("wake") or True,
                        raising=False)
    monkeypatch.setattr(cli, "send_message",
                        lambda _token, _chat, text, _thread: events.append(("telegram", text)))

    result = cli.notify_topic(
        _CFG,
        4109,
        sender="automation",
        idempotency_key="review-125-r1",
        text="Review finished",
    )

    assert events == ["persist", "wake", ("telegram", "automation: Review finished")]
    assert result == {
        "status": "enqueued",
        "topic_id": 4109,
        "wake": "nudged",
        "telegram": "posted",
        "echo": "skipped",   # automation: no topic of its own to echo into (#140)
    }
    record = json.loads((tmp_path / "topics/4109/inbox.jsonl").read_text())
    assert record.pop("ts")
    assert record == {
        "from": "automation",
        "kind": "notification",
        "text": "Review finished",
        "provenance": "local-notify",
        "idempotency_key": "review-125-r1",
    }


def test_notify_command_reads_event_text_from_stdin(monkeypatch, capsys):
    seen = {}
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO("event from automation\n"))

    def notify(cfg, topic, sender, key, text):
        seen.update(cfg=cfg, topic=topic, sender=sender, key=key, text=text)
        return {"status": "duplicate", "topic_id": topic}

    monkeypatch.setattr(cli, "notify_topic", notify)
    args = SimpleNamespace(topic=4109, sender="automation", idempotency_key="key-1")

    cli.cmd_notify({"chat_id": -100}, args)

    assert seen == {
        "cfg": {"chat_id": -100},
        "topic": 4109,
        "sender": "automation",
        "key": "key-1",
        "text": "event from automation\n",
    }
    assert json.loads(capsys.readouterr().out) == {
        "status": "duplicate",
        "topic_id": 4109,
    }


def test_notify_command_keeps_wake_log_off_json_stdout(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(common, "STATE_DIR", str(tmp_path))
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO("first event\n"))
    monkeypatch.setattr(cli, "read_registry", lambda: {"4109": _dialog()})
    monkeypatch.setattr(cli, "pane_alive", lambda _pane: True, raising=False)
    monkeypatch.setattr(daemon, "pane_alive", lambda _pane: True)
    monkeypatch.setattr(daemon.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(daemon, "_tmux", _echoing_tmux())
    monkeypatch.setattr(cli, "send_message", lambda *_args: None)
    args = SimpleNamespace(topic=4109, sender="automation", idempotency_key="key-1")

    cli.cmd_notify(_CFG, args)

    captured = capsys.readouterr()
    assert json.loads(captured.out) == {
        "echo": "skipped",
        "status": "enqueued",
        "telegram": "posted",
        "topic_id": 4109,
        "wake": "nudged",
    }
    assert "nudged pane %8 for topic 4109" in captured.err


def test_notify_duplicate_has_no_second_inbox_nudge_or_telegram_side_effect(
    monkeypatch, tmp_path
):
    nudges = []
    telegram = []
    monkeypatch.setattr(common, "STATE_DIR", str(tmp_path))
    monkeypatch.setattr(cli, "read_registry", lambda: {"4109": _dialog()})
    monkeypatch.setattr(cli, "pane_alive", lambda pane: True, raising=False)
    monkeypatch.setattr(cli, "maybe_nudge", lambda *args: nudges.append(args) or True,
                        raising=False)
    monkeypatch.setattr(cli, "send_message", lambda *args: telegram.append(args))

    first = cli.notify_topic(_CFG, 4109, "automation", "same-key", "Finished")
    duplicate = cli.notify_topic(_CFG, 4109, "automation", "same-key", "Finished")

    assert first["status"] == "enqueued"
    assert duplicate == {"status": "duplicate", "topic_id": 4109}
    assert len((tmp_path / "topics/4109/inbox.jsonl").read_text().splitlines()) == 1
    assert len(nudges) == 1
    assert len(telegram) == 1


def test_notify_different_key_is_deliverable_without_redundant_unread_nudge(
    monkeypatch, tmp_path
):
    nudges = []
    telegram = []
    monkeypatch.setattr(common, "STATE_DIR", str(tmp_path))
    monkeypatch.setattr(cli, "read_registry", lambda: {"4109": _dialog()})
    monkeypatch.setattr(cli, "pane_alive", lambda pane: True, raising=False)
    monkeypatch.setattr(cli, "maybe_nudge", lambda *args: nudges.append(args) or True,
                        raising=False)
    monkeypatch.setattr(cli, "send_message", lambda *args: telegram.append(args))

    cli.notify_topic(_CFG, 4109, "automation", "key-1", "First")
    result = cli.notify_topic(_CFG, 4109, "automation", "key-2", "Second")

    assert result["status"] == "enqueued"
    assert result["wake"] == "already-unread"
    assert len((tmp_path / "topics/4109/inbox.jsonl").read_text().splitlines()) == 2
    assert len(nudges) == 1
    assert len(telegram) == 2


def test_notify_different_keys_atomically_create_one_first_unread_nudge(
    monkeypatch, tmp_path
):
    nudges = []
    telegram = []
    validated = threading.Barrier(2)
    monkeypatch.setattr(common, "STATE_DIR", str(tmp_path))
    monkeypatch.setattr(cli, "read_registry", lambda: {"4109": _dialog()})

    def pane_alive(_pane):
        validated.wait(timeout=5)
        return True

    monkeypatch.setattr(cli, "pane_alive", pane_alive, raising=False)
    monkeypatch.setattr(cli, "maybe_nudge", lambda *args: nudges.append(args) or True,
                        raising=False)
    monkeypatch.setattr(cli, "send_message", lambda *args: telegram.append(args))

    def notify(key):
        return cli.notify_topic(_CFG, 4109, "automation", key, f"event {key}")

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(notify, "key-1"),
            executor.submit(notify, "key-2"),
        ]
        results = [future.result() for future in futures]

    assert sorted(result["wake"] for result in results) == ["already-unread", "nudged"]
    assert len((tmp_path / "topics/4109/inbox.jsonl").read_text().splitlines()) == 2
    assert len(nudges) == 1
    assert len(telegram) == 2


def test_stale_first_unread_owner_cannot_nudge_a_later_batch(monkeypatch, tmp_path):
    literal_nudges = []
    first_appended = threading.Event()
    allow_first_wake = threading.Event()
    real_append = common.append_jsonl_once
    monkeypatch.setattr(common, "STATE_DIR", str(tmp_path))
    monkeypatch.setattr(cli, "read_registry", lambda: {"4109": _dialog()})
    monkeypatch.setattr(cli, "pane_alive", lambda _pane: True, raising=False)
    monkeypatch.setattr(daemon, "pane_alive", lambda _pane: True)
    monkeypatch.setattr(daemon.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(cli, "send_message", lambda *_args: None)

    tmux = _echoing_tmux(literals=literal_nudges)

    def append_once(path, record):
        result = real_append(path, record)
        if record["idempotency_key"] == "batch-a":
            first_appended.set()
            assert allow_first_wake.wait(timeout=5)
        return result

    monkeypatch.setattr(daemon, "_tmux", tmux)
    monkeypatch.setattr(cli, "append_jsonl_once", append_once)

    def notify(key):
        return cli.notify_topic(_CFG, 4109, "automation", key, key)

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(notify, "batch-a")
        assert first_appended.wait(timeout=5)

        records, cursor = cli.read_new(4109, cli.load_cursor(4109))
        cli.drain_and_commit(4109, records, cursor, as_json=False)

        second_result = notify("batch-b")
        allow_first_wake.set()
        first_result = first.result(timeout=5)

    assert second_result["wake"] == "nudged"
    assert first_result["wake"] == "already-unread"
    assert len(literal_nudges) == 1


def test_notify_reports_failed_wake_without_losing_local_or_telegram_delivery(
    monkeypatch, tmp_path
):
    telegram = []
    monkeypatch.setattr(common, "STATE_DIR", str(tmp_path))
    monkeypatch.setattr(cli, "read_registry", lambda: {"4109": _dialog()})
    monkeypatch.setattr(cli, "pane_alive", lambda pane: True, raising=False)
    monkeypatch.setattr(cli, "maybe_nudge", lambda *args: False, raising=False)
    monkeypatch.setattr(cli, "send_message", lambda *args: telegram.append(args))

    result = cli.notify_topic(_CFG, 4109, "automation", "key", "event")

    assert result["wake"] == "failed"
    assert result["telegram"] == "posted"
    assert (tmp_path / "topics/4109/inbox.jsonl").exists()
    assert len(telegram) == 1


@pytest.mark.parametrize(
    "registry",
    [
        {},
        {"4109": _dialog(feed=True)},
        {"4109": _dialog(ended="2026-08-02T12:30:00+0000")},
        {"4109": {"name": "unbound"}},
    ],
)
def test_notify_invalid_topic_has_no_side_effects(monkeypatch, registry):
    side_effects = []
    monkeypatch.setattr(cli, "read_registry", lambda: registry)
    monkeypatch.setattr(cli, "pane_alive", lambda pane: True, raising=False)
    monkeypatch.setattr(cli, "append_jsonl_once", lambda *args: side_effects.append("persist"),
                        raising=False)
    monkeypatch.setattr(cli, "maybe_nudge", lambda *args: side_effects.append("wake"),
                        raising=False)
    monkeypatch.setattr(cli, "send_message", lambda *args: side_effects.append("telegram"))

    with pytest.raises(SystemExit):
        cli.notify_topic(_CFG, 4109, "automation", "key", "event")
    assert side_effects == []


def test_notify_dead_pane_has_no_side_effects(monkeypatch):
    side_effects = []
    monkeypatch.setattr(cli, "read_registry", lambda: {"4109": _dialog()})
    monkeypatch.setattr(cli, "pane_alive", lambda pane: False, raising=False)
    monkeypatch.setattr(cli, "append_jsonl_once", lambda *args: side_effects.append("persist"),
                        raising=False)

    with pytest.raises(SystemExit, match="not live"):
        cli.notify_topic(_CFG, 4109, "automation", "key", "event")
    assert side_effects == []


def test_notify_telegram_ambiguity_keeps_local_delivery_successful(monkeypatch, tmp_path):
    monkeypatch.setattr(common, "STATE_DIR", str(tmp_path))
    monkeypatch.setattr(cli, "read_registry", lambda: {"4109": _dialog()})
    monkeypatch.setattr(cli, "pane_alive", lambda pane: True, raising=False)
    monkeypatch.setattr(cli, "maybe_nudge", lambda *args: True, raising=False)

    def ambiguous(*args):
        raise common.PossiblyDelivered("sendMessage may already have it")

    monkeypatch.setattr(cli, "send_message", ambiguous)

    result = cli.notify_topic(_CFG, 4109, "automation", "key", "event")

    assert result["status"] == "enqueued"
    assert result["wake"] == "nudged"
    assert result["telegram"] == "ambiguous"
    assert (tmp_path / "topics/4109/inbox.jsonl").exists()


def test_notify_tmux_timeout_is_bounded_and_has_no_side_effects(monkeypatch):
    side_effects = []
    monkeypatch.setattr(cli, "read_registry", lambda: {"4109": _dialog()})

    def timeout(_pane):
        raise subprocess.TimeoutExpired(cmd="tmux", timeout=10)

    monkeypatch.setattr(cli, "pane_alive", timeout, raising=False)
    monkeypatch.setattr(cli, "append_jsonl_once", lambda *args: side_effects.append("persist"),
                        raising=False)

    with pytest.raises(SystemExit, match="timed out"):
        cli.notify_topic(_CFG, 4109, "automation", "key", "event")
    assert side_effects == []


def test_existing_nudge_primitive_returns_success_and_injects_only_recv_cue(monkeypatch):
    calls = []
    monkeypatch.setattr(daemon, "unread_count", lambda topic: 1)
    monkeypatch.setattr(daemon, "pane_alive", lambda pane: True)
    monkeypatch.setattr(daemon.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(daemon, "_tmux", _echoing_tmux(calls=calls))

    assert daemon.maybe_nudge(4109, "%8") is True
    keys = [argv for argv, _kwargs in calls if argv[:2] == ["tmux", "send-keys"]]
    assert len(keys) == 2                       # the recv cue and its Enter — nothing else
    assert keys[0] == [
        "tmux", "send-keys", "-t", "%8", "-l",
        "[tg-bridge] New Telegram message in your topic — "
        "run `tg-bridge recv --topic 4109` and act on it.",
    ]
    assert keys[1] == ["tmux", "send-keys", "-t", "%8", "Enter"]
    # #133: the Enter is only reached after the pane was read back and the text was seen
    order = [argv[1] for argv, _kwargs in calls]
    assert order.index("capture-pane") < order.index("send-keys")
    assert "capture-pane" in order[order.index("send-keys") + 1:]
    assert all("timeout" not in kwargs for _argv, kwargs in calls)


def test_existing_nudge_primitive_reports_bounded_tmux_failure(monkeypatch):
    monkeypatch.setattr(daemon, "unread_count", lambda topic: 1)
    monkeypatch.setattr(daemon, "pane_alive", lambda pane: True)

    def timeout(_argv, **_kwargs):
        raise subprocess.TimeoutExpired(cmd="tmux", timeout=daemon.TMUX_TIMEOUT)

    monkeypatch.setattr(daemon, "_tmux", timeout)
    assert daemon.maybe_nudge(4109, "%8") is False


def test_existing_nudge_primitive_reports_no_unread_without_touching_pane(monkeypatch):
    monkeypatch.setattr(daemon, "unread_count", lambda topic: 0)
    monkeypatch.setattr(
        daemon, "pane_alive",
        lambda pane: pytest.fail("pane liveness must not be checked without unread messages"),
    )

    assert daemon.maybe_nudge(4109, "%8") is False
