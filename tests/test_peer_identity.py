"""Unit tests for derived peer identity and the honest result contract (#138, §4.1 M5/M6).

Two failures are covered here.

Attribution: `--sender` was required free text, so any caller could label itself "the owner" or
ride `kind: "notification"`, which the spec reserves for trusted local automation. Worse,
the Telegram mirror went through `send_text`, whose icon prefix is the TARGET topic's — the
emoji the owner reads as "this session is speaking" — so a message from A appeared in B's thread
signed by B. A session's identity is now derived from its own pane, in the record and in
the mirror alike.

Contract: the success JSON said `delivered`, which the substrate cannot promise — the wake
is best effort and nothing acknowledges receipt. It says `enqueued`, and the append fsyncs
the parent directory when it creates the inbox file: partial hardening, no OS-crash claim.
"""

import io
import json
import os
import subprocess
import types

import pytest

from bridge import cli, common

CALLER_PANE = "%A"
A, B = 111, 222
A_ICON, B_ICON = "🦊", "🐙"
CFG = {"bot_token": "token", "chat_id": -100}


def _registry():
    return {
        "111": {"name": "Alpha", "created": "2026-08-08T10:00:00+0000",
                "pane": CALLER_PANE, "icon": A_ICON},
        "222": {"name": "Beta", "created": "2026-08-08T10:05:00+0000",
                "pane": "%B", "icon": B_ICON},
    }


def _notify_env(monkeypatch, tmp_path, pane=CALLER_PANE, pane_alive=None):
    """Target-side preflight and wake stubbed out; returns the captured Telegram sends."""
    if pane is None:
        monkeypatch.delenv("TMUX_PANE", raising=False)
    else:
        monkeypatch.setenv("TMUX_PANE", pane)
    monkeypatch.setattr(common, "STATE_DIR", str(tmp_path))
    monkeypatch.setattr(cli, "read_registry", _registry)
    monkeypatch.setattr(cli, "pane_alive", pane_alive or (lambda _pane: True), raising=False)
    monkeypatch.setattr(cli, "maybe_nudge", lambda *args: True, raising=False)
    mirrored = []
    monkeypatch.setattr(cli, "send_message",
                        lambda token, chat, text, thread: mirrored.append((thread, text)))
    return mirrored


def _record(tmp_path, topic_id=B):
    lines = (tmp_path / "topics" / str(topic_id) / "inbox.jsonl").read_text().splitlines()
    return json.loads(lines[-1])


# --- T4: derived peer identity ----------------------------------------------------

def test_peer_notify_derives_identity_and_ignores_the_sender_flag(tmp_path, monkeypatch):
    _notify_env(monkeypatch, tmp_path)

    cli.notify_topic(CFG, B, "the owner", "111->222:review:1", "please look at #138")

    record = _record(tmp_path)
    # A session cannot borrow another voice: the label and topic id come from the registry.
    assert record["from"] == "Alpha (topic 111)"
    assert record["sender_topic_id"] == A
    assert "the owner" not in record["from"]
    # `notification` is spec'd as trusted local automation; peer traffic must not collapse
    # that trust boundary, so it gets its own kind.
    assert record["kind"] == "peer"


def test_peer_mirror_is_signed_by_the_source_not_the_target(tmp_path, monkeypatch):
    mirrored = _notify_env(monkeypatch, tmp_path)

    cli.notify_topic(CFG, B, None, "111->222:review:1", "please look at #138")

    # The peer path also echoes into A's own topic (#140); this test is about attribution,
    # so take the target's mirror specifically instead of assuming it is the only send.
    text, = [t for thread, t in mirrored if thread == B]
    assert text.startswith(f"{A_ICON} Alpha (topic {A}): ")
    # The reported failure, restated: B's own signature must appear nowhere in a message B
    # did not write.
    assert B_ICON not in text


def test_automation_notify_keeps_the_notification_contract(tmp_path, monkeypatch):
    mirrored = _notify_env(monkeypatch, tmp_path, pane=None)

    cli.notify_topic(CFG, B, "orchestra", "review-125-r1", "review seats finished")

    record = _record(tmp_path)
    assert record["from"] == "orchestra"
    assert record["kind"] == "notification"
    assert "sender_topic_id" not in record              # no pane, no derived identity
    # Un-iconed: an icon means "a session is speaking", and this is machinery. It also drops
    # the pre-existing defect of signing automation with the target's icon.
    assert mirrored == [(B, "orchestra: review seats finished")]


def test_automation_notify_still_requires_a_sender(tmp_path, monkeypatch):
    _notify_env(monkeypatch, tmp_path, pane=None)

    with pytest.raises(SystemExit, match="--sender"):
        cli.notify_topic(CFG, B, None, "review-125-r1", "review seats finished")


@pytest.mark.parametrize("caller_pane_state", ["dead", "tmux hangs"])
def test_a_caller_whose_pane_does_not_answer_cannot_sign_as_that_session(
    tmp_path, monkeypatch, caller_pane_state
):
    """A set `TMUX_PANE` does not prove the process is still in that pane, and a dead pane
    keeps its registry entry until the daemon's next lifecycle poll. Signing on that basis
    would write a durable, the owner-visible attribution naming a session that no longer exists,
    so an unanswerable caller pane falls back to the automation contract instead."""
    def pane_alive(pane):
        if pane != CALLER_PANE:
            return True                                 # the target is fine
        if caller_pane_state == "dead":
            return False
        raise subprocess.TimeoutExpired(cmd="tmux", timeout=10)

    mirrored = _notify_env(monkeypatch, tmp_path, pane_alive=pane_alive)

    with pytest.raises(SystemExit, match="--sender"):
        cli.notify_topic(CFG, B, None, "111->222:review:1", "please look at #138")
    cli.notify_topic(CFG, B, "orchestra", "111->222:review:1", "please look at #138")

    record = _record(tmp_path)
    assert record["kind"] == "notification"
    assert "Alpha" not in record["from"] and "sender_topic_id" not in record
    assert mirrored == [(B, "orchestra: please look at #138")]


def test_notify_parser_makes_sender_optional():
    parser = cli.build_parser()
    args = parser.parse_args(["notify", "--topic", "222", "--idempotency-key", "k"])
    assert args.sender is None
    assert parser.parse_args(
        ["notify", "--topic", "222", "--idempotency-key", "k", "--sender", "orchestra"]
    ).sender == "orchestra"


def test_peer_records_render_their_kind_to_the_reading_agent(capsys):
    """Provenance has to be visible in default text output, not only in `--json`."""
    cli.print_records([{"ts": "2026-08-08T11:00:00+0000", "from": "Alpha (topic 111)",
                        "kind": "peer", "text": "please look at #138"}], as_json=False)

    assert capsys.readouterr().out.strip() == (
        "[2026-08-08T11:00:00+0000] Alpha (topic 111) (peer): please look at #138"
    )


def test_send_and_ask_still_sign_with_the_topic_icon(tmp_path, monkeypatch, capsys):
    """`send_text` is untouched, so every the owner-facing message keeps its existing signature."""
    monkeypatch.setenv("TMUX_PANE", CALLER_PANE)
    monkeypatch.setattr(cli, "read_registry", _registry)
    monkeypatch.setattr(cli, "pane_alive", lambda _pane: True, raising=False)
    monkeypatch.setattr(cli, "state_path", lambda *parts: str(tmp_path.joinpath(*parts)))
    monkeypatch.setattr(cli, "send_typing", lambda cfg, topic: None)
    posted = []
    monkeypatch.setattr(cli, "send_message",
                        lambda token, chat, text, thread: posted.append((thread, text)))

    cli.cmd_send(CFG, types.SimpleNamespace(text="an update", topic=str(A), force=False,
                                            json=False))
    with pytest.raises(SystemExit):                     # ask exits 2 when no reply arrives
        cli.cmd_ask(CFG, types.SimpleNamespace(text="a question", topic=str(A), timeout=0,
                                               json=False))

    assert posted == [(A, f"{A_ICON} an update"), (A, f"{A_ICON} a question")]
    capsys.readouterr()


def test_notify_command_reads_stdin_on_the_peer_path(tmp_path, monkeypatch, capsys):
    _notify_env(monkeypatch, tmp_path)
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO("please look at #138\n"))
    args = types.SimpleNamespace(topic=B, sender=None, idempotency_key="111->222:review:1")

    cli.cmd_notify(CFG, args)

    assert json.loads(capsys.readouterr().out)["status"] == "enqueued"
    assert _record(tmp_path)["text"] == "please look at #138\n"


# --- T5: honest result contract + durability hardening ----------------------------

@pytest.mark.parametrize("pane", [CALLER_PANE, None])
def test_success_status_is_enqueued_not_delivered(tmp_path, monkeypatch, pane):
    """Nothing on this path proves the target agent read anything: the wake is best effort
    and there is no acknowledgement, so the wire value must not claim delivery."""
    _notify_env(monkeypatch, tmp_path, pane=pane)

    result = cli.notify_topic(CFG, B, "orchestra", "key-1", "an event")

    # echo: "posted" from a resolvable caller, "skipped" when there is no sender topic (#140).
    assert result == {"status": "enqueued", "topic_id": B, "wake": "nudged",
                      "telegram": "posted",
                      "echo": "posted" if pane else "skipped"}


def test_duplicate_status_is_unchanged(tmp_path, monkeypatch):
    _notify_env(monkeypatch, tmp_path)

    cli.notify_topic(CFG, B, None, "same-key", "an event")
    duplicate = cli.notify_topic(CFG, B, None, "same-key", "an event")

    assert duplicate == {"status": "duplicate", "topic_id": B}


def _fsync_spy(monkeypatch):
    """Record the real path behind every fsynced fd, then fsync for real."""
    real_fsync = os.fsync
    synced = []

    def spy(fd):
        synced.append(os.readlink(f"/proc/self/fd/{fd}"))
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", spy)
    return synced


def _local_record(key):
    return {"ts": "2026-08-08T11:00:00+0000", "from": "orchestra", "kind": "notification",
            "text": "an event", "provenance": "local-notify", "idempotency_key": key}


def test_parent_directory_is_fsynced_only_when_the_append_creates_the_inbox(
    tmp_path, monkeypatch
):
    """Fsyncing the file persists its content, not the directory entry naming it — so a
    brand-new inbox needs one level up synced too. Repeating it on every append would buy
    nothing, so this asserts the mechanism fires exactly once, on creation.

    This asserts the mechanism and nothing more. It is NOT evidence of OS-crash durability:
    see the sibling test for why the rest of the path chain has no such guarantee.
    """
    synced = _fsync_spy(monkeypatch)
    inbox = tmp_path / "topics" / "222" / "inbox.jsonl"
    inbox.parent.mkdir(parents=True)

    common.append_jsonl_once(str(inbox), _local_record("key-1"))

    assert os.path.realpath(inbox) in synced
    assert os.path.realpath(inbox.parent) in synced

    synced.clear()
    common.append_jsonl_once(str(inbox), _local_record("key-2"))

    assert os.path.realpath(inbox) in synced            # content is still durable
    assert os.path.realpath(inbox.parent) not in synced


def test_telegram_ingress_append_still_fsyncs_nothing(tmp_path, monkeypatch):
    """Why no OS-crash claim follows from the fix above: the ingress writer that may have
    created `topics/<id>/` and `inbox.jsonl` first syncs neither, and nothing at append time
    can tell a persisted dirent chain from an unpersisted one."""
    synced = _fsync_spy(monkeypatch)
    inbox = tmp_path / "topics" / "222" / "inbox.jsonl"
    inbox.parent.mkdir(parents=True)

    common.append_jsonl(str(inbox), {"ts": "2026-08-08T11:00:00+0000", "from": "the owner",
                                     "kind": "text", "text": "hello"})

    assert synced == []
