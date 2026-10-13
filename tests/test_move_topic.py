"""#380: move real isolated state between two groups; fake only Telegram and panes."""

import copy
import fcntl
import io
import json
import os
import threading
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs

import pytest

from bridge import cli, common, daemon, restore_cli


@pytest.fixture
def cfg():
    return {"bot_token": "101:fake-work", "chat_id": -100100, "owner_id": 42,
            "groups": [{"name": "personal", "chat_id": -100200,
                        "bot_token": "202:fake-personal"}]}


@pytest.fixture
def transport(monkeypatch):
    calls = []
    icons = {"🩺": "555", "📚": "556"}
    next_thread = [17]
    cli._ICON_SET_CACHE.clear()

    def urlopen(req, **kwargs):
        token, method = req.full_url.split("/bot", 1)[1].split("/", 1)
        params = {k: v[0] for k, v in parse_qs(req.data.decode()).items()}
        calls.append((token, method, params))
        if method == "getForumTopicIconStickers":
            result = [{"emoji": emoji, "custom_emoji_id": value} for emoji, value in icons.items()]
        elif method == "createForumTopic":
            result = {"message_thread_id": next_thread[0]}
            next_thread[0] += 1
        else:
            result = {"message_id": 100 + len(calls)}
        return io.BytesIO(json.dumps({"ok": True, "result": result}).encode())
    monkeypatch.setattr(common.urllib.request, "urlopen", urlopen)
    return SimpleNamespace(calls=calls, icons=icons, next_thread=next_thread)


@pytest.fixture
def seat(monkeypatch, tmp_path, transport):
    cwd = tmp_path / "shared home"
    cwd.mkdir()
    info = {"name": "Gym training", "created": "2026-08-08T10:00:00+0000",
            "session_id": "kept-session", "pane": "%1", "cwd": str(cwd),
            "engine": "claude", "icon": "🏋️", "topic_icon": "🩺", "boot_id": "boot",
            "briefed_boot": "boot", "reopen": "compact", "custom_flag": {"keep": True}}
    common.write_registry({"7": info, "8": {"name": "Other", "pane": "%2",
                                           "created": "2026-08-08T10:05:00+0000"}})
    for name, value in (("pending_reopens", {}), ("_pending_cf", {}), ("_auto_reviving", set()),
                        ("_pending_model", {}), ("pending_kills", {}), ("_blocked_reported", {}),
                        ("_unreadable_streak", {})):
        monkeypatch.setattr(daemon, name, value)
    monkeypatch.setattr(cli, "pane_alive", lambda pane: True)
    monkeypatch.setattr(daemon, "pane_alive", lambda pane: True)

    def forbidden(*args, **kwargs):
        pytest.fail("moving a topic must not touch a pane or launch a process")
    monkeypatch.setattr(daemon, "type_line", forbidden)
    monkeypatch.setattr(daemon, "launch_pane", forbidden)
    monkeypatch.setattr(daemon, "_tmux", forbidden)
    return info


def command_move(monkeypatch, cfg, key=7, target="personal"):
    # The real command publishes a request; the real lifecycle consumer handles it.
    monkeypatch.setattr(cli.time, "sleep", lambda seconds: daemon.process_topic_moves(cfg))
    args = cli.build_parser().parse_args(["move", "--topic", str(key), "--to", target])
    cli.cmd_move(cfg, args)


def owner_message(cfg, key, text="hello", **fields):
    token, chat, thread = common.topic_credentials(cfg, key)
    return {"chat": {"id": chat}, "message_thread_id": thread, "message_id": 200,
            "from": {"id": cfg["owner_id"], "first_name": "Owner"}, "text": text, **fields}


@pytest.mark.parametrize("reverse", [False, True])
def test_move_command_uses_target_bot_and_preserves_name_and_topic_icon(
    cfg, seat, transport, monkeypatch, capsys, reverse
):
    old, target, new = ("personal:7", "work", 17) if reverse else (7, "personal", "personal:17")
    if reverse:
        common.update_registry(lambda reg: reg.update({"personal:7": reg.pop("7")}))
    command_move(monkeypatch, cfg, old, target)
    creation = [(t, p) for t, method, p in transport.calls if method == "createForumTopic"]
    target_cfg = common.group_config(cfg, target)
    assert creation == [(target_cfg["bot_token"], {"chat_id": str(target_cfg["chat_id"]),
                                                 "name": seat["name"], "icon_custom_emoji_id": "555"})]
    assert capsys.readouterr().out == f"topic_id={new}\n"
    assert common.read_registry()[str(old)] == {"name": seat["name"], "moved_to": new}
    before = len(transport.calls)
    daemon.process_topic_moves(cfg)
    assert len(transport.calls) == before  # the original mailbox cannot execute twice


def test_cli_main_dispatches_move(cfg, seat, monkeypatch, capsys):
    monkeypatch.setattr(cli, "load_config", lambda: cfg)
    monkeypatch.setattr(cli.sys, "argv", ["tg-bridge", "move", "--topic", "7", "--to", "personal"])
    monkeypatch.setattr(cli.time, "sleep", lambda seconds: daemon.process_topic_moves(cfg))
    cli._main()
    assert capsys.readouterr().out == "topic_id=personal:17\n"


def test_move_command_is_consumed_by_existing_lifecycle_loop(cfg, seat, monkeypatch, capsys):
    polls = [0]

    def sleep(seconds):
        if seconds == 0.1:
            with pytest.raises(KeyboardInterrupt):
                daemon.lifecycle_loop(cfg)
        else:
            assert seconds == daemon.LIFECYCLE_POLL
            polls[0] += 1
            if polls[0] > 1:
                raise KeyboardInterrupt
    monkeypatch.setattr(cli.time, "sleep", sleep)
    cli.cmd_move(cfg, cli.build_parser().parse_args(["move", "--topic", "7", "--to", "personal"]))
    assert capsys.readouterr().out == "topic_id=personal:17\n" and polls[0] == 2


@pytest.mark.parametrize("key,target", [(7, "work"), ("personal:7", "personal")])
def test_move_to_own_group_is_refused_before_any_request_or_api(cfg, seat, transport, monkeypatch, key, target):
    with pytest.raises(SystemExit, match="own group"):
        command_move(monkeypatch, cfg, key, target)
    assert not transport.calls and not Path(common.move_request_path(key)).exists()


def test_move_uses_default_icon_when_target_bot_cannot_set_old_icon(cfg, seat, transport, monkeypatch):
    transport.icons.clear()
    command_move(monkeypatch, cfg)
    creation = [p for t, method, p in transport.calls if method == "createForumTopic"]
    assert creation == [{"chat_id": "-100200", "name": seat["name"]}]
    assert common.read_registry()["personal:17"]["icon"] == seat["icon"]


@pytest.mark.parametrize("state", ["live", "parked", "ended"])
def test_registry_transfers_every_field_and_leaves_only_a_pointer(cfg, seat, monkeypatch, state):
    if state != "live":
        seat.update(ended="2026-08-08T11:00:00+0000", closed=True, pane=None)
    if state == "parked":
        seat.update(parked=True, parked_pct=47, parked_ctx_ts="2026-08-08T11:00:00+0000")
    common.update_registry(lambda reg: reg.update({"7": copy.deepcopy(seat)}))
    before = common.read_registry()
    command_move(monkeypatch, cfg)
    registry = common.read_registry()
    # Every field travels except `closed`: the destination topic was just created open (R2).
    expected = {k: v for k, v in before["7"].items() if k != "closed"}
    assert registry["personal:17"] == expected
    assert registry["7"] == {"name": seat["name"], "moved_to": "personal:17"}
    assert registry["8"] == before["8"]
    assert "7" not in common.latest_topics_by_pane(registry).values()
    daemon.set_topic_closed(7, True)
    daemon.set_topic_closed(7, False)
    assert common.read_registry()["7"] == registry["7"]


def test_folder_cursor_outbox_media_and_carry_forward_move_without_replay(cfg, seat, monkeypatch, capsys):
    old = Path(common.state_path("topics", "7", "inbox.jsonl")).parent
    (old / "media").mkdir()
    attachment = old / "media" / "photo.png"
    attachment.write_bytes(b"picture")
    records = [{"ts": "t", "from": "Owner", "kind": "text", "text": text}
               for text in ("already read", "unread one", f"unread image at {attachment}")]
    for record in records:
        common.append_jsonl(str(old / "inbox.jsonl"), record)
    (old / "cursor").write_text("1")
    (old / "outbox.jsonl").write_text('{"content_sha256":"hash-only"}\n')
    (old / "carry-forward.md").write_text("retained carry-forward")
    (old / "carry-forward.md.done").write_text("done")
    before = {path.relative_to(old): path.read_bytes() for path in old.rglob("*") if path.is_file()}
    inode = (old / "inbox.jsonl").stat().st_ino
    command_move(monkeypatch, cfg)
    capsys.readouterr()
    new = old.with_name("personal:17")
    assert new.is_dir() and old.is_symlink()
    assert (new / "inbox.jsonl").stat().st_ino == inode
    assert {path.relative_to(new): path.read_bytes() for path in new.rglob("*") if path.is_file()} == before
    assert attachment.read_bytes() == b"picture"  # absolute reference inside an unread record
    monkeypatch.setenv("TMUX_PANE", "%1")
    args = cli.build_parser().parse_args(["recv", "--topic", "7", "--json"])
    cli.cmd_recv(cfg, args)
    assert [json.loads(line)["text"] for line in capsys.readouterr().out.splitlines()] == [
        records[1]["text"], records[2]["text"]]
    assert cli.load_cursor("personal:17") == 3
    cli.cmd_recv(cfg, args)
    assert capsys.readouterr().out == ""


def test_destination_ingress_waits_for_cutover_without_blocking_existing_topics(
    cfg, seat, transport, monkeypatch, capsys
):
    common.update_registry(lambda reg: reg.update({"personal:7": {"name": "Other personal"}}))
    common.append_jsonl(common.state_path("topics", "7", "inbox.jsonl"),
                        {"ts": "t", "from": "Owner", "kind": "text", "text": "source unread"})
    observed, finished = threading.Event(), threading.Event()
    errors, workers = [], []
    original_api, original_read = daemon.api, daemon.read_registry
    monkeypatch.setattr(daemon, "schedule_nudge", lambda *a: None)

    def read():
        registry = original_read()
        if threading.current_thread().name == "destination-ingress":
            observed.set()
        return registry

    def inbound():
        try:
            daemon.handle_message(cfg, owner_message(cfg, "personal:17", "destination unread"))
        except Exception as error:
            errors.append(error)
        finally:
            finished.set()

    def api(token, method, params, **kwargs):
        if method == "createForumTopic":
            # The target bot's poller is independent of the API call creating the topic.
            worker = threading.Thread(target=inbound, name="destination-ingress")
            workers.append(worker)
            worker.start()
            assert observed.wait(5)
            assert not finished.wait(0.1)
            assert not (Path(common.STATE_DIR) / "topics" / "personal:17" / "inbox.jsonl").exists()
            daemon.handle_message(cfg, owner_message(cfg, "personal:7", "other continues"))
            assert "other continues" in Path(common.state_path("topics", "personal:7", "inbox.jsonl")).read_text()
        return original_api(token, method, params, **kwargs)

    monkeypatch.setattr(daemon, "read_registry", read)
    monkeypatch.setattr(daemon, "api", api)
    try:
        assert daemon.move_topic(cfg, 7, "personal")[0] == "personal:17"
    finally:
        for worker in workers:
            worker.join(5)
    assert finished.is_set() and not errors and all(not w.is_alive() for w in workers)
    assert common.read_registry()["personal:7"] == {"name": "Other personal"}
    assert not any(method == "deleteForumTopic" for _, method, _ in transport.calls)
    cli.cmd_recv(cfg, cli.build_parser().parse_args(["recv", "--topic", "personal:17", "--json"]))
    assert [json.loads(line)["text"] for line in capsys.readouterr().out.splitlines()] == [
        "source unread", "destination unread"]
    assert cli.load_cursor("personal:17") == 2
    cli.cmd_recv(cfg, cli.build_parser().parse_args(["recv", "--topic", "7", "--json"]))
    assert capsys.readouterr().out == ""


def test_sessions_overview_excludes_moved_pointer(cfg, seat, monkeypatch):
    daemon.move_topic(cfg, 7, "personal")
    monkeypatch.setattr(daemon, "fleet_panes", lambda: [("%1", "session", "title", "claude")])
    monkeypatch.setattr(daemon, "read_context", lambda *a: None)
    overview = daemon.sessions_overview()
    assert "topic personal:17" in overview and overview.count(seat["name"]) == 2
    assert "topic 7," not in overview and "registered without tmux" not in overview


@pytest.mark.parametrize("binding", ["7", "8", None])
def test_cwd_binding_changes_only_for_old_key_and_moved_pane_resolves_commands(
    cfg, seat, monkeypatch, transport, capsys, binding
):
    cwd = Path(seat["cwd"])
    path = cwd / cli.CWD_BINDING_FILE
    if binding is not None:
        path.write_text(binding)
    command_move(monkeypatch, cfg)
    if binding:
        assert path.read_text() == ("personal:17" if binding == "7" else "8")
    else:
        assert not path.exists()
    capsys.readouterr()
    monkeypatch.chdir(cwd)
    monkeypatch.setenv("TMUX_PANE", "%1")
    assert cli.current_topic_metadata()["topic_id"] == "personal:17"
    common.append_jsonl(common.state_path("topics", "personal:17", "inbox.jsonl"),
                        {"ts": "t", "from": "Owner", "kind": "text", "text": "new reply"})
    cli.cmd_recv(cfg, cli.build_parser().parse_args(["recv"]))
    assert "new reply" in capsys.readouterr().out
    cli.cmd_send(cfg, cli.build_parser().parse_args(["send", "reply"]))
    message = [(token, p) for token, method, p in transport.calls if method == "sendMessage"][-1]
    assert message[0] == "202:fake-personal"
    assert message[1]["chat_id"] == "-100200" and message[1]["message_thread_id"] == "17"


@pytest.mark.parametrize("busy", ["question-prepared", "question-delivered", "question-memory-only",
                                 "question-reviving", "carry-forward", "revive"])
def test_move_is_refused_for_pending_question_or_worker(cfg, seat, transport, monkeypatch, busy):
    if busy.startswith("question"):
        daemon.pending_reopens["7"] = {"entry": dict(seat), "state": busy.split("-", 1)[1]}
        if busy != "question-memory-only":
            assert daemon._save_pending_reopens()
    elif busy == "carry-forward":
        daemon._pending_cf["7"] = {"token": "active", "pane": "%1"}
    else:
        daemon._auto_reviving.add("7")
    before = common.read_registry()
    with pytest.raises(SystemExit, match="pending reopen|carry-forward in flight|revive in flight"):
        command_move(monkeypatch, cfg)
    assert common.read_registry() == before and not transport.calls


def test_reader_and_worker_locks_fence_the_move(cfg, seat, transport):
    before = common.read_registry()
    with common.topic_lock(7):
        with pytest.raises(RuntimeError, match="operation in flight"):
            daemon.move_topic(cfg, 7, "personal")
    assert common.read_registry() == before and not transport.calls


def test_recv_fences_move_until_printed_messages_are_committed(cfg, seat, monkeypatch, capsys):
    common.append_jsonl(common.state_path("topics", "7", "inbox.jsonl"),
                        {"ts": "t", "from": "Owner", "kind": "text", "text": "unread"})
    original = cli.print_records

    def emit(records, as_json):
        with pytest.raises(RuntimeError, match="operation in flight"):
            daemon.move_topic(cfg, 7, "personal")
        original(records, as_json)
    monkeypatch.setattr(cli, "print_records", emit)
    cli.cmd_recv(cfg, cli.build_parser().parse_args(["recv", "--topic", "7"]))
    assert capsys.readouterr().out.count("unread") == 1
    assert cli.load_cursor(7) == 1
    daemon.move_topic(cfg, 7, "personal")
    assert cli.load_cursor("personal:17") == 1 and cli.read_new("personal:17", 1)[0] == []


def test_carry_forward_worker_still_fences_move_after_its_pending_flag_is_cleared(
    cfg, seat, monkeypatch, transport
):
    entered, finish = threading.Event(), threading.Event()
    daemon._pending_cf["7"] = {"token": "active", "pane": "%1"}

    def wait(*args):
        entered.set()
        assert finish.wait(5)
        return "cancelled"
    monkeypatch.setattr(daemon, "_cf_wait_idle", wait)
    cf_file = common.state_path("topics", "7", "cf.md")
    worker = threading.Thread(target=daemon._carry_forward_worker,
                              args=(cfg, 7, "%1", cf_file, cf_file + ".done", "active", "Gym"))
    worker.start()
    try:
        assert entered.wait(5)
        daemon._cf_release(7, "active")
        with pytest.raises(RuntimeError, match="operation in flight"):
            daemon.move_topic(cfg, 7, "personal")
        assert not transport.calls
    finally:
        finish.set()
        worker.join(5)
    assert not worker.is_alive()
    assert daemon.move_topic(cfg, 7, "personal")[0] == "personal:17"


def test_manual_revive_is_fenced_even_without_auto_reviving_flag(cfg, seat, monkeypatch):
    entered, finish = threading.Event(), threading.Event()
    results = []
    monkeypatch.setattr(daemon, "_tmux", lambda *a, **kw: SimpleNamespace(returncode=1))
    monkeypatch.setattr(daemon, "_name_taken_by_other", lambda *a: False)
    monkeypatch.setattr(daemon, "last_model_and_effort_for_session", lambda *a: (None, None))
    monkeypatch.setattr(daemon, "last_model_for_session", lambda *a: None)

    def launch(*args, **kwargs):
        entered.set()
        assert finish.wait(5)
        return None, "test launcher stopped"
    monkeypatch.setattr(daemon, "launch_pane", launch)
    worker = threading.Thread(target=lambda: results.append(daemon.revive_one(cfg, 7, seat, brief=False)))
    worker.start()
    try:
        assert entered.wait(5) and daemon._auto_reviving == set()
        with pytest.raises(RuntimeError, match="operation in flight"):
            daemon.move_topic(cfg, 7, "personal")
    finally:
        finish.set()
        worker.join(5)
    assert not worker.is_alive() and results[0][0] == "failed"


def test_deferred_revive_briefing_fences_move_while_settling(cfg, seat, monkeypatch):
    entered, finish = threading.Event(), threading.Event()
    monkeypatch.setattr(daemon, "has_live_recv", lambda *a: False)

    def idle(pane):
        entered.set()
        assert finish.wait(5)
        return True
    monkeypatch.setattr(daemon, "pane_is_idle", idle)
    # No briefing will be typed: the test stops at its binding validation after the wait.
    monkeypatch.setattr(daemon, "_briefing_still_ours", lambda *a: False)
    worker = threading.Thread(target=daemon.deliver_briefing, args=("%1", 7, "claude", "brief"))
    worker.start()
    try:
        assert entered.wait(5) and daemon._auto_reviving == set()
        with pytest.raises(RuntimeError, match="operation in flight"):
            daemon.move_topic(cfg, 7, "personal")
    finally:
        finish.set()
        worker.join(5)
    assert not worker.is_alive()


def test_other_group_same_thread_busy_state_does_not_block_or_change(cfg, seat, monkeypatch):
    other = {"name": "Other group seat", "session_id": "other-session", "ended": "t", "parked": True}
    common.update_registry(lambda reg: reg.update({"personal:7": other}))
    daemon.pending_reopens["personal:7"] = {"entry": dict(other), "state": "delivered"}
    daemon._auto_reviving.add("personal:7")
    daemon._pending_cf["personal:7"] = {"token": "other-worker"}
    command_move(monkeypatch, cfg)
    assert common.read_registry()["personal:7"] == other
    assert daemon.pending_reopens["personal:7"]["state"] == "delivered"
    assert daemon._auto_reviving == {"personal:7"}
    assert daemon._pending_cf["personal:7"] == {"token": "other-worker"}


def test_callback_fence_precedes_pending_question_lock(cfg, seat, monkeypatch):
    original = daemon._handle_callback_query

    def check(cfg, query):
        with daemon._pending_reopen_lock:
            with pytest.raises(RuntimeError, match="operation in flight"):
                daemon.move_topic(cfg, 7, "personal")
        return original(cfg, query)
    monkeypatch.setattr(daemon, "_handle_callback_query", check)
    query = {"id": "tap", "from": {"id": 42}, "data": "reopen:compact",
             "message": {"chat": {"id": cfg["chat_id"]}, "message_thread_id": 7, "message_id": 100}}
    daemon.handle_callback_query(cfg, query)
    assert common.read_registry()["7"] == seat


def test_warning_loop_memory_and_autocf_retry_follow_move(cfg, seat, monkeypatch, transport):
    for filename, value in (("warnings.json", {"7": 60, "8": 20}),
                            ("autocf.json", {"7": True, "8": False}),
                            ("park_exempt.json", [7]), ("autocf_exempt.json", [])):
        Path(common.state_path(filename)).write_text(json.dumps(value))
    assert daemon._cf_mark_unfinished("7", "previous-failed-run")
    ticks = [0]

    def sleep(seconds):
        ticks[0] += 1
        if ticks[0] == 1:
            # warning_loop has ALREADY loaded its private dictionaries at this point.
            daemon.move_topic(cfg, 7, "personal")
        else:
            raise KeyboardInterrupt
    monkeypatch.setattr(daemon.time, "sleep", sleep)
    monkeypatch.setattr(daemon, "engine_of_pane", lambda pane: "claude")
    monkeypatch.setattr(daemon, "context_for", lambda pane, *a: {"pct": 65 if pane == "%1" else 20})
    with pytest.raises(KeyboardInterrupt):
        daemon.warning_loop(cfg)
    assert json.loads(Path(common.state_path("warnings.json")).read_text()) == {"personal:17": 60, "8": 20}
    assert json.loads(Path(common.state_path("autocf.json")).read_text()) == {"personal:17": True, "8": False}
    assert Path(daemon._cf_unfinished_path("personal:17")).read_text().endswith("previous-failed-run\n")
    assert not Path(daemon._cf_unfinished_path(7)).exists()
    assert daemon.load_park_exempt() == {"personal:17"}
    Path(common.state_path("autocf_exempt.json")).write_text("[7]")
    assert daemon.load_autocf_exempt() == {"personal:17"}
    assert len([p for t, method, p in transport.calls if method == "sendMessage"]) == 2


def test_move_notices_close_old_topic_and_late_owner_messages_only_get_pointer(
    cfg, seat, monkeypatch, transport
):
    command_move(monkeypatch, cfg)
    messages = [(t, p) for t, method, p in transport.calls if method == "sendMessage"]
    assert len(messages) == 2
    assert "personal (topic personal:17)" in messages[0][1]["text"]
    assert "moved here" in messages[1][1]["text"]
    assert transport.calls[-1] == ("101:fake-work", "closeForumTopic",
                                  {"chat_id": "-100100", "message_thread_id": "7"})
    for text in ("late owner message", "/kill", "!interrupt"):
        before = len(transport.calls)
        daemon.handle_message(cfg, owner_message(cfg, 7, text))
        assert len(transport.calls) == before + 1
        assert transport.calls[-1][1] == "sendMessage"
        assert "personal:17" in transport.calls[-1][2]["text"]
        assert "\n" not in transport.calls[-1][2]["text"]
    assert cli.read_new("personal:17", 0)[0] == []
    assert cli.read_new(7, 0)[0] == []
    before = len(transport.calls)
    daemon.handle_message(cfg, owner_message(cfg, 7, "", forum_topic_reopened={}))
    daemon.handle_message(cfg, owner_message(cfg, 7, "", forum_topic_closed={}))
    assert len(transport.calls) == before
    assert common.read_registry()["7"] == {"name": seat["name"], "moved_to": "personal:17"}


def test_notify_old_key_follows_move_and_keeps_idempotency(cfg, seat, monkeypatch, transport):
    # owner_only avoids a terminal cue: A1's tripwire remains live during the move itself.
    result = cli.notify_topic(cfg, 7, "local seat", "one-event", "before move", owner_only=True)
    assert result["topic_id"] == 7
    command_move(monkeypatch, cfg)
    before = len(transport.calls)
    result = cli.notify_topic(cfg, 7, "local seat", "one-event", "before move", owner_only=True)
    assert result == {"status": "duplicate", "topic_id": "personal:17"}
    assert len(transport.calls) == before
    monkeypatch.setattr(daemon, "type_line", lambda *a, **kw: "sent")
    result = cli.notify_topic(cfg, 7, "local seat", "second-event", "after move")
    assert result["topic_id"] == "personal:17" and result["wake"] == "nudged"
    records = cli.read_new("personal:17", 0)[0]
    assert len(records) == 1 and records[0]["text"] == "after move"
    assert transport.calls[-1][0] == "202:fake-personal"


def test_repeated_move_has_distinct_mailboxes_and_resolves_oldest_key(cfg, seat, monkeypatch):
    command_move(monkeypatch, cfg)
    first = Path(common.move_request_path(7)).read_bytes()
    command_move(monkeypatch, cfg, "personal:17", "work")
    assert Path(common.move_request_path(7)).read_bytes() == first
    assert common.resolve_moved_topic(7) == 18
    assert common.resolve_moved_topic("personal:17") == 18
    assert common.read_registry()["18"] == seat
    assert Path(common.state_path("topics", "7")).resolve().name == "18"


def prepare_revival(monkeypatch):
    launches = []
    monkeypatch.setattr(daemon.threading, "Thread", lambda target, args=(), **kw:
                        SimpleNamespace(start=lambda: target(*args)))
    monkeypatch.setattr(daemon.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(daemon, "_tmux", lambda *a, **kw: SimpleNamespace(returncode=1))
    monkeypatch.setattr(daemon, "_name_taken_by_other", lambda *a: False)
    monkeypatch.setattr(daemon, "current_boot_id", lambda: "new-boot")
    monkeypatch.setattr(daemon, "launch_pane", lambda *a, **kw: launches.append(a) or ("%77", None))
    monkeypatch.setattr(daemon, "last_model_and_effort_for_session", lambda *a: ("model", "max"))
    monkeypatch.setattr(daemon, "_await_live_picker", lambda *a: False)
    monkeypatch.setattr(daemon, "compaction_plan", lambda *a: None)
    monkeypatch.setattr(daemon, "answer_resume_picker", lambda *a, **kw: "answered")
    monkeypatch.setattr(daemon, "_compaction_settled", lambda *a: True)
    monkeypatch.setattr(daemon, "_safe_peek", lambda *a: "")
    monkeypatch.setattr(daemon, "has_live_recv", lambda *a: False)
    monkeypatch.setattr(daemon, "_pane_hosts_session", lambda *a: True)
    monkeypatch.setattr(daemon, "engine_of_pane", lambda *a: "claude")
    monkeypatch.setattr(daemon, "pane_is_idle", lambda *a: True)
    monkeypatch.setattr(daemon, "type_line", lambda *a, **kw: "sent")
    monkeypatch.setattr(daemon, "schedule_nudge", lambda *a: None)
    return launches


@pytest.mark.parametrize("large", [False, True])
def test_moved_parked_session_revives_in_new_topic_with_usual_reopen_question(
    cfg, seat, transport, monkeypatch, large
):
    seat.pop("reopen")  # test the ordinary owner-choice path, without a compact preset
    seat.update(ended="2026-08-08T11:00:00+0000", parked=True, pane=None, closed=True)
    common.update_registry(lambda reg: reg.update({"7": seat}))
    command_move(monkeypatch, cfg)
    launches = prepare_revival(monkeypatch)
    monkeypatch.setattr(daemon, "session_context_tokens", lambda *a: 350000 if large else 10000)
    monkeypatch.setattr(daemon, "session_age_minutes", lambda *a: 120)
    transport.calls.clear()
    daemon.handle_message(cfg, owner_message(cfg, "personal:17", "owner wakes seat"))
    if large:
        assert not launches
        assert daemon.pending_reopens["personal:17"]["state"] == "delivered"
        assert "personal:17" in daemon._load_pending_reopens()
        assert any("reply_markup" in p for t, method, p in transport.calls if method == "sendMessage")
        daemon.handle_message(cfg, owner_message(cfg, "personal:17", "compact"))
    current = common.read_registry()["personal:17"]
    assert current["session_id"] == "kept-session" and current["pane"] == "%77"
    assert "parked" not in current and "ended" not in current and "closed" not in current
    assert len(launches) == 1 and "kept-session" in launches[0][2]
    assert daemon.pending_reopens == {}
    assert [r["text"] for r in cli.read_new("personal:17", 0)[0]] == ["owner wakes seat"]
    assert all(t == "202:fake-personal" for t, method, p in transport.calls)


def test_snapshot_observed_before_move_cannot_repopulate_tombstone(cfg, seat, monkeypatch):
    common.update_registry(lambda reg: reg["7"].pop("session_id"))
    monkeypatch.setattr(daemon, "current_boot_id", lambda: "new-boot")
    monkeypatch.setattr(daemon, "engine_of_pane", lambda pane: "codex")

    def observe(pane, engine):
        if pane == "%1":
            daemon.move_topic(cfg, 7, "personal")
        return "new-session"
    monkeypatch.setattr(daemon, "session_id_for_pane", observe)
    daemon.snapshot_once()
    assert common.read_registry()["7"] == {"name": seat["name"], "moved_to": "personal:17"}
    restore_cli.list_revivable()


def test_move_error_never_prints_either_bot_token(cfg, seat, monkeypatch, capsys):
    def fail(token, method, params, **kw):
        raise RuntimeError("failed request with " + cfg["bot_token"] + " and " + cfg["groups"][0]["bot_token"])
    monkeypatch.setattr(daemon, "api", fail)
    with pytest.raises(SystemExit) as error:
        command_move(monkeypatch, cfg)
    output = str(error.value) + str(capsys.readouterr()) + Path(common.move_request_path(7)).read_text()
    assert "101:fake-work" not in output and "202:fake-personal" not in output
    assert common.read_registry()["7"] == seat


def test_move_rolls_folder_back_if_registry_publish_fails(cfg, seat, monkeypatch, transport):
    old = Path(common.state_path("topics", "7", "inbox.jsonl")).parent
    (old / "inbox.jsonl").write_text("retained\n")

    def fail_publish(mutate):
        registry = common.read_registry()
        mutate(registry)
        raise OSError("failed registry publish")
    monkeypatch.setattr(daemon, "update_registry", fail_publish)
    with pytest.raises(OSError, match="failed registry publish"):
        daemon.move_topic(cfg, 7, "personal")
    assert old.is_dir() and not old.is_symlink() and (old / "inbox.jsonl").read_text() == "retained\n"
    assert not old.with_name("personal:17").exists()
    assert common.read_registry()["7"] == seat
    assert any(method == "deleteForumTopic" for t, method, p in transport.calls)


def test_pointer_can_be_posted_when_old_topic_is_still_closed(cfg, seat, monkeypatch, transport):
    command_move(monkeypatch, cfg)
    original = daemon.send_message
    posted = []
    closed = [True]
    real_api = daemon.api

    def api(token, method, params, **kwargs):
        if method == "reopenForumTopic":
            closed[0] = False
        elif method == "closeForumTopic":
            closed[0] = True
        return real_api(token, method, params, **kwargs)

    def send(token, chat, text, thread, **kwargs):
        if closed[0]:
            raise RuntimeError("TOPIC_CLOSED")
        posted.append(text)
        return original(token, chat, text, thread, **kwargs)
    monkeypatch.setattr(daemon, "api", api)
    monkeypatch.setattr(daemon, "send_message", send)
    daemon.handle_message(cfg, owner_message(cfg, 7, "late message"))
    assert len(posted) == 1 and "personal:17" in posted[0] and closed[0]
    assert cli.read_new("personal:17", 0)[0] == []
    assert common.read_registry()["7"] == {"name": seat["name"], "moved_to": "personal:17"}


def test_notice_failure_reports_new_key_without_repeating_move(cfg, seat, monkeypatch, transport, capsys):
    real_api = daemon.api

    def api(token, method, params, **kwargs):
        if method == "closeForumTopic":
            raise RuntimeError("fake close error " + token)
        return real_api(token, method, params, **kwargs)
    monkeypatch.setattr(daemon, "api", api)
    command_move(monkeypatch, cfg)
    output = capsys.readouterr()
    assert output.out == "topic_id=personal:17\n" and "could not close the old topic" in output.err
    assert cfg["bot_token"] not in output.err
    daemon.process_topic_moves(cfg)
    assert len([method for t, method, p in transport.calls if method == "createForumTopic"]) == 1


def test_unauthorized_ingress_does_not_create_topic_lock(cfg, seat):
    message = owner_message(cfg, 7, "untrusted", **{"from": {"id": 99}})
    daemon.handle_message(cfg, message)
    assert not Path(common.state_path("topic-locks", "7.lock")).exists()


def test_folder_alias_keeps_open_inbox_and_attachment_paths(tmp_path):
    """Shape experiment: rename preserves open files; an alias preserves stored paths."""
    old = tmp_path / "topics" / "7"
    new = old.with_name("personal:7")
    old.mkdir(parents=True)
    (old / "media").mkdir()
    attachment = old / "media" / "photo.png"
    attachment.write_bytes(b"photo")
    inbox = old / "inbox.jsonl"
    inbox.write_text("already read\n")
    with inbox.open("a") as writer, (old / "inbox.jsonl.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        old.rename(new)
        old.symlink_to(new.name, target_is_directory=True)
        writer.write("unread\n")
        writer.flush()
        assert (old / "inbox.jsonl.lock").stat().st_ino == os.fstat(lock.fileno()).st_ino
    assert (new / "inbox.jsonl").read_text() == "already read\nunread\n"
    assert attachment.read_bytes() == b"photo"
    assert old.is_symlink() and new.is_dir()



# ---- #380 review round 1: A3 regression, R1 restart mid-move, R2 copied `closed` ----------

class _StopLoop(Exception):
    """Raised from the loop's sleep, which sits outside its per-poll try."""


def _one_poll(monkeypatch, loop):
    sleeps = {"n": 0}

    def fake_sleep(_seconds):
        sleeps["n"] += 1
        if sleeps["n"] > 1:
            raise _StopLoop
    monkeypatch.setattr(daemon.time, "sleep", fake_sleep)
    try:
        loop({})
    except _StopLoop:
        pass


@pytest.mark.parametrize("loop_name", ["lifecycle_loop", "warning_loop"])
def test_a_malformed_registry_key_does_not_stop_either_sweep(monkeypatch, loop_name):
    """A3: topic_lock parses the key, so a bad key used to raise inside the per-topic loop and
    the rest of the registry was skipped on every poll (fixed for the prepass in #377)."""
    registry = {"bad key!": {"name": "x", "pane": "%9"}, "7": {"name": "y", "pane": "%1"}}
    monkeypatch.setattr(daemon, "read_registry", lambda: registry)
    monkeypatch.setattr(daemon, "process_topic_moves", lambda cfg: None)
    monkeypatch.setattr(daemon, "load_autocf_exempt", lambda: set())
    monkeypatch.setattr(daemon, "log", lambda *a, **k: None)
    reached, real_lock = [], daemon.topic_lock

    def recording_lock(key):
        reached.append(str(key))
        return real_lock(key)  # still raises on a malformed key, as in production
    monkeypatch.setattr(daemon, "topic_lock", recording_lock)
    _one_poll(monkeypatch, getattr(daemon, loop_name))
    assert "7" in reached and "bad key!" not in reached


def test_a_move_interrupted_before_the_registry_changed_is_reported_not_retried(cfg, seat, monkeypatch):
    path = common.move_request_path(7)
    Path(path).write_text(json.dumps({"from": 7, "to": "personal", "state": "running"}))
    monkeypatch.setattr(daemon, "log", lambda *a, **k: None)
    daemon.recover_interrupted_moves(cfg)
    request = json.loads(Path(path).read_text())
    assert request["state"] == "done" and "restarted" in request["error"]
    assert "Gym training" in request["error"] and "personal" in request["error"]
    daemon.process_topic_moves(cfg)  # a finished request is never executed again
    assert common.read_registry()["7"].get("moved_to") is None


def test_a_move_interrupted_after_the_registry_moved_reports_the_new_key(cfg, seat, monkeypatch):
    common.update_registry(lambda reg: reg.update({"7": {"name": "Gym training",
                                                         "moved_to": "personal:17"}}))
    path = common.move_request_path(7)
    Path(path).write_text(json.dumps({"from": 7, "to": "personal", "state": "running"}))
    monkeypatch.setattr(daemon, "log", lambda *a, **k: None)
    daemon.recover_interrupted_moves(cfg)
    request = json.loads(Path(path).read_text())
    assert request["state"] == "done" and request["topic_id"] == "personal:17"
    assert "error" not in request and request["warnings"]


def test_a_closed_topic_arrives_open_in_its_new_group(cfg, seat, monkeypatch):
    common.update_registry(lambda reg: reg["7"].update(closed=True))
    command_move(monkeypatch, cfg)
    assert "closed" not in common.read_registry()["personal:17"]



def test_a_malformed_moved_pointer_does_not_stop_the_warning_sweep(monkeypatch):
    registry = {"bad key!": {"name": "x", "moved_to": "personal:17"},
                "7": {"name": "y", "pane": "%1"}}
    monkeypatch.setattr(daemon, "read_registry", lambda: registry)
    monkeypatch.setattr(daemon, "load_autocf_exempt", lambda: set())
    monkeypatch.setattr(daemon, "log", lambda *a, **k: None)
    reached, real_lock = [], daemon.topic_lock
    monkeypatch.setattr(daemon, "topic_lock", lambda key: reached.append(str(key)) or real_lock(key))
    _one_poll(monkeypatch, daemon.warning_loop)
    assert "7" in reached


@pytest.mark.parametrize("content", ["null", "[]", "3", '"running"'])
def test_recovery_skips_a_request_file_that_is_not_an_object(cfg, seat, monkeypatch, content):
    Path(common.move_request_path(7)).write_text(content)
    daemon.recover_interrupted_moves(cfg)  # must not raise out of main()
    assert Path(common.move_request_path(7)).read_text() == content


def test_recovery_error_never_carries_a_bot_token(cfg, seat, monkeypatch):
    common.update_registry(lambda reg: reg["7"].update(name="leak 202:fake-personal here"))
    path = common.move_request_path(7)
    Path(path).write_text(json.dumps({"from": 7, "to": "personal", "state": "running"}))
    monkeypatch.setattr(daemon, "log", lambda *a, **k: None)
    daemon.recover_interrupted_moves(cfg)
    text = Path(path).read_text()
    assert "202:fake-personal" not in text and "redacted" in text


@pytest.mark.parametrize("moved", [True, False])
@pytest.mark.parametrize("bad", [{"error": 1}, {"error": ["x"]}, {"to": {"nested": 1}}])
def test_no_damaged_request_file_can_stop_the_daemon_starting(cfg, seat, monkeypatch, moved, bad):
    if moved:
        common.update_registry(lambda reg: reg.update({"7": {"name": "Gym training",
                                                             "moved_to": "personal:17"}}))
    Path(common.move_request_path(7)).write_text(json.dumps({"from": 7, "state": "running", **bad}))
    monkeypatch.setattr(daemon, "log", lambda *a, **k: None)
    daemon.recover_interrupted_moves(cfg)  # never raises
