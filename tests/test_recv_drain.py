"""Unit tests for the recv-drain fix (#105).

Layers:
  * cli: the read cursor must never advance before a message is durably on stdout —
    `print_records` then `sys.stdout.flush()` MUST both precede `save_cursor`, in `cmd_recv`
    AND in `cmd_ask` (which also flushes any pre-existing unread before advancing past it).
  * daemon: `last_inbox_message` (last record) + `recent_inbox_drop` (freshness gate) +
    `dead_listener_nudge` (re-delivery text) — the self-heal re-delivers the last message's
    text when a listener has died, but only when it's fresh enough to be a plausible drop.
  * launcher: `bin/tg-bridge` runs the CLI unbuffered (`python3 -u`).
"""

import json
import os
import sys
import threading
import types
from concurrent.futures import ThreadPoolExecutor

from bridge import cli, common, daemon

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ---- cli: durable-before-cursor ordering -------------------------------------

class _RecordingStdout:
    """Stand-in for sys.stdout that logs write()/flush() into a shared event list, so a
    test can assert the message was written AND flushed before the cursor advanced."""

    def __init__(self, events):
        self.events = events

    def write(self, s):
        if s.strip():                      # ignore the bare "\n" print() emits separately
            self.events.append(("write", s))
        return len(s)

    def flush(self):
        self.events.append(("flush",))


def _patch_state(tmp_path, monkeypatch, records):
    """Point cli.state_path at tmp_path and seed topic 42's inbox with `records`."""
    def _state_path(*parts):
        p = tmp_path.joinpath(*parts)
        p.parent.mkdir(parents=True, exist_ok=True)
        return str(p)
    monkeypatch.setattr(cli, "state_path", _state_path)
    inbox = tmp_path / "topics" / "42" / "inbox.jsonl"
    inbox.parent.mkdir(parents=True, exist_ok=True)
    inbox.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records),
                     encoding="utf-8")


def _order(events, kind, needle=None):
    for i, e in enumerate(events):
        if e[0] == kind and (needle is None or (len(e) > 1 and needle in e[1])):
            return i
    raise AssertionError(f"event {kind!r} ({needle!r}) not found in {events}")


def test_recv_flushes_message_before_advancing_cursor(tmp_path, monkeypatch):
    events = []
    _patch_state(tmp_path, monkeypatch,
                 [{"ts": "t", "from": "Владелец", "kind": "text", "text": "hello-105"}])
    monkeypatch.setattr(
        cli, "save_cursor", lambda tid, c: events.append(("save_cursor", c)) or True,
    )
    monkeypatch.setattr(cli, "send_typing", lambda cfg, tid: events.append(("send_typing",)))
    monkeypatch.setattr(sys, "stdout", _RecordingStdout(events))

    args = types.SimpleNamespace(topic="42", wait=None, peek=False, json=False)
    cli.cmd_recv({}, args)

    # message written -> flushed -> THEN cursor advanced. This is the whole fix: a listener
    # killed after save_cursor still leaves the message durably on disk.
    assert _order(events, "write", "hello-105") < _order(events, "flush") < _order(events, "save_cursor")


def test_recv_drains_writer_that_appends_between_flush_and_cursor_commit(
    tmp_path, monkeypatch
):
    old_record = {"ts": "t1", "from": "the owner", "kind": "text", "text": "old"}
    new_record = {"ts": "t2", "from": "automation", "kind": "notification", "text": "new"}
    _patch_state(tmp_path, monkeypatch, [old_record])
    inbox = tmp_path / "topics" / "42" / "inbox.jsonl"
    (inbox.parent / "cursor").write_text("0", encoding="utf-8")

    writer_sampled = threading.Event()
    allow_append = threading.Event()
    old_flushed = threading.Event()
    output = []
    real_append = common._append_jsonl_record

    def delayed_append(path, record, durable=False):
        writer_sampled.set()
        assert allow_append.wait(timeout=5)
        return real_append(path, record, durable)

    class SignalingStdout(_RecordingStdout):
        def write(self, text):
            result = super().write(text)
            if "old" in text:
                output.append(text)
            if "new" in text:
                output.append(text)
            return result

        def flush(self):
            super().flush()
            if any("old" in text for text in output):
                old_flushed.set()

    monkeypatch.setattr(common, "_append_jsonl_record", delayed_append)
    monkeypatch.setattr(cli, "send_typing", lambda _cfg, _topic: None)
    monkeypatch.setattr(sys, "stdout", SignalingStdout([]))
    args = types.SimpleNamespace(topic="42", wait=None, peek=False, json=False)

    with ThreadPoolExecutor(max_workers=2) as executor:
        writer = executor.submit(common.append_jsonl_once, str(inbox), {
            **new_record,
            "provenance": "local-notify",
            "idempotency_key": "reader-race",
        })
        assert writer_sampled.wait(timeout=5)
        reader = executor.submit(cli.cmd_recv, {}, args)
        assert old_flushed.wait(timeout=5)
        allow_append.set()
        reader.result(timeout=5)
        append_result = writer.result(timeout=5)

    assert append_result == (True, None)
    assert any("old" in text for text in output)
    assert any("new" in text for text in output)
    assert (inbox.parent / "cursor").read_text(encoding="utf-8") == "2"


def test_recv_peek_still_flushes_but_does_not_advance_cursor(tmp_path, monkeypatch):
    # --peek must not touch the cursor, but the durable flush is unconditional (it happens
    # before the peek check), so even a peek writes the message out immediately.
    events = []
    _patch_state(tmp_path, monkeypatch,
                 [{"ts": "t", "from": "S", "kind": "text", "text": "peeked"}])
    monkeypatch.setattr(cli, "save_cursor",
                        lambda tid, c: events.append(("save_cursor", c)))
    monkeypatch.setattr(cli, "send_typing", lambda cfg, tid: events.append(("send_typing",)))
    monkeypatch.setattr(sys, "stdout", _RecordingStdout(events))

    args = types.SimpleNamespace(topic="42", wait=None, peek=True, json=False)
    cli.cmd_recv({}, args)

    assert _order(events, "write", "peeked") < _order(events, "flush")
    assert not any(e[0] == "save_cursor" for e in events)


def test_ask_flushes_reply_before_advancing_cursor(tmp_path, monkeypatch):
    events = []
    _patch_state(tmp_path, monkeypatch, [])
    monkeypatch.setattr(
        cli, "save_cursor", lambda tid, c: events.append(("save_cursor", c)) or True,
    )
    monkeypatch.setattr(cli, "send_typing", lambda cfg, tid: events.append(("send_typing",)))
    monkeypatch.setattr(cli, "send_text", lambda cfg, tid, text: None)
    # the reply "arrives" — isolate the durability ordering from the polling loop
    monkeypatch.setattr(cli, "wait_for_messages",
                        lambda tid, timeout: ([{"ts": "t", "from": "S", "kind": "text",
                                                "text": "the-reply"}], 1, 1000.0, 3.0))
    monkeypatch.setattr(sys, "stdout", _RecordingStdout(events))

    args = types.SimpleNamespace(topic="42", text="q", timeout=1, json=False)
    cli.cmd_ask({}, args)

    # the final cursor advance is the one after the reply prints; it must follow the flush
    last_cursor = max(i for i, e in enumerate(events) if e[0] == "save_cursor")
    assert _order(events, "write", "the-reply") < _order(events, "flush") < last_cursor


def test_ask_emits_preexisting_unread_before_advancing_cursor(tmp_path, monkeypatch):
    # BLOCKER (Codex #105 review): `ask` used to advance the cursor over anything already
    # unread WITHOUT printing it -> a message that arrived before the ask was silently lost.
    # It must now print + flush the pending records before advancing the cursor past them.
    events = []
    _patch_state(tmp_path, monkeypatch,
                 [{"ts": "t", "from": "Владелец", "kind": "text", "text": "unread-before-ask"}])
    monkeypatch.setattr(
        cli, "save_cursor", lambda tid, c: events.append(("save_cursor", c)) or True,
    )
    monkeypatch.setattr(cli, "send_typing", lambda cfg, tid: events.append(("send_typing",)))
    monkeypatch.setattr(cli, "send_text", lambda cfg, tid, text: events.append(("send_text",)))
    monkeypatch.setattr(cli, "wait_for_messages",
                        lambda tid, timeout: ([], 1, 1000.0, 1.0))  # no reply
    monkeypatch.setattr(sys, "stdout", _RecordingStdout(events))

    args = types.SimpleNamespace(topic="42", text="q", timeout=1, json=False)
    try:
        cli.cmd_ask({}, args)
    except SystemExit:
        pass  # no reply within timeout -> exit(2); irrelevant to this assertion

    # the pre-existing unread was written + flushed BEFORE the first cursor advance and the send
    assert _order(events, "write", "unread-before-ask") < _order(events, "flush") \
        < _order(events, "save_cursor") <= _order(events, "send_text")


# ---- daemon state helper -----------------------------------------------------

def _patch_daemon_state(tmp_path, monkeypatch):
    def _state_path(*parts):
        p = tmp_path.joinpath(*parts)
        p.parent.mkdir(parents=True, exist_ok=True)
        return str(p)
    monkeypatch.setattr(daemon, "state_path", _state_path)


def _write_inbox(tmp_path, tid, lines):
    inbox = tmp_path / "topics" / str(tid) / "inbox.jsonl"
    inbox.parent.mkdir(parents=True, exist_ok=True)
    inbox.write_text("".join(lines), encoding="utf-8")
    return inbox


# ---- daemon: last_inbox_message ----------------------------------------------

def test_last_inbox_message_returns_last_nonblank(tmp_path, monkeypatch):
    _patch_daemon_state(tmp_path, monkeypatch)
    _write_inbox(tmp_path, 7, [
        json.dumps({"from": "A", "text": "first"}) + "\n",
        "\n",                                                    # blank line ignored
        json.dumps({"from": "Владелец", "text": "latest"}) + "\n",
    ])
    assert daemon.last_inbox_message(7) == {"from": "Владелец", "text": "latest"}


def test_last_inbox_message_none_when_absent(tmp_path, monkeypatch):
    _patch_daemon_state(tmp_path, monkeypatch)
    assert daemon.last_inbox_message(999) is None


def test_last_inbox_message_none_on_bad_last_line(tmp_path, monkeypatch):
    _patch_daemon_state(tmp_path, monkeypatch)
    _write_inbox(tmp_path, 7, [
        json.dumps({"from": "A", "text": "good"}) + "\n",
        "{ this is not json\n",
    ])
    assert daemon.last_inbox_message(7) is None


def test_last_inbox_message_none_on_non_object_json(tmp_path, monkeypatch):
    # NIT (Codex #105 review): a last line that is valid JSON but not an object (e.g. `[]`)
    # must not raise AttributeError out of the sweep — best-effort returns None.
    _patch_daemon_state(tmp_path, monkeypatch)
    _write_inbox(tmp_path, 7, [json.dumps({"from": "A", "text": "x"}) + "\n", "[]\n"])
    assert daemon.last_inbox_message(7) is None


def test_last_inbox_message_missing_text_key(tmp_path, monkeypatch):
    _patch_daemon_state(tmp_path, monkeypatch)
    _write_inbox(tmp_path, 7, [json.dumps({"from": "A"}) + "\n"])   # no "text" key
    assert daemon.last_inbox_message(7) == {"from": "A"}


# ---- daemon: recent_inbox_drop (freshness gate) ------------------------------

def test_recent_inbox_drop_returns_record_when_fresh(tmp_path, monkeypatch):
    _patch_daemon_state(tmp_path, monkeypatch)
    inbox = _write_inbox(tmp_path, 7, [json.dumps({"from": "S", "text": "fresh"}) + "\n"])
    os.utime(inbox, (1000, 1000))
    # now is only 10s after the write -> well within the window
    assert daemon.recent_inbox_drop(7, now=1010, window=1200) == {"from": "S", "text": "fresh"}


def test_recent_inbox_drop_none_when_stale(tmp_path, monkeypatch):
    # SHOULD-FIX (Codex #105 review): an old, already-handled last message must NOT be
    # re-injected on a routine dead-listener sweep.
    _patch_daemon_state(tmp_path, monkeypatch)
    inbox = _write_inbox(tmp_path, 7, [json.dumps({"from": "S", "text": "old"}) + "\n"])
    os.utime(inbox, (1000, 1000))
    # now is 5000s after the write -> far outside a 1200s window
    assert daemon.recent_inbox_drop(7, now=6000, window=1200) is None


def test_recent_inbox_drop_none_when_absent(tmp_path, monkeypatch):
    _patch_daemon_state(tmp_path, monkeypatch)
    assert daemon.recent_inbox_drop(999, now=1000) is None


# ---- daemon: dead_listener_nudge ---------------------------------------------

def test_dead_listener_nudge_redelivers_last_message():
    text = daemon.dead_listener_nudge(55, {"from": "Владелец", "text": "please pay the R1 invoice"})
    assert "please pay the R1 invoice" in text          # the actual message is re-delivered
    assert "Владелец" in text                          # attributed to the sender
    assert "topic 55" in text and "recv --topic 55" in text
    assert "act on it now" in text


def test_dead_listener_nudge_no_recap_without_record():
    text = daemon.dead_listener_nudge(55, None)
    assert "dropped a message" not in text              # no phantom recap
    assert "Drain with" in text                         # but still the re-arm instruction


def test_dead_listener_nudge_no_recap_on_empty_text():
    text = daemon.dead_listener_nudge(55, {"from": "A", "text": ""})
    assert "dropped a message" not in text


def test_dead_listener_nudge_flags_truncation_of_long_message():
    # SHOULD-FIX (Codex #105 review): a >400-char message must be flagged as truncated, never
    # presented as if complete (its tail may be unrecoverable once the cursor has advanced).
    text = daemon.dead_listener_nudge(55, {"from": "A", "text": "x" * 1000})
    assert "x" * 400 in text
    assert "x" * 401 not in text                         # snippet capped at 400 chars
    assert "truncated" in text                           # and the cap is disclosed


def test_dead_listener_nudge_flattens_newlines():
    # the nudge is sent as a single tmux send-keys line; embedded newlines would break it
    text = daemon.dead_listener_nudge(55, {"from": "A", "text": "line1\nline2"})
    assert "line1 line2" in text
    assert "line1\nline2" not in text


# ---- daemon: sweep_nudge_text — the exact dispatch idle_sweep_loop calls -----

def test_sweep_nudge_text_dead_includes_fresh_message(tmp_path, monkeypatch):
    # Pins the real call site: idle_sweep_loop uses sweep_nudge_text(tid, flavor, now). A
    # fresh drop on the dead-listener flavor -> the message text is re-delivered.
    _patch_daemon_state(tmp_path, monkeypatch)
    inbox = _write_inbox(tmp_path, 55, [json.dumps({"from": "S", "text": "just-dropped"}) + "\n"])
    os.utime(inbox, (1000, 1000))
    text = daemon.sweep_nudge_text(55, "dead", now=1005)
    assert "just-dropped" in text
    assert "isn't running" in text                         # the dead-listener nudge, not "unread"


def test_sweep_nudge_text_dead_skips_stale_message(tmp_path, monkeypatch):
    # Same flavor, stale last message -> plain re-arm nudge, no recap.
    _patch_daemon_state(tmp_path, monkeypatch)
    inbox = _write_inbox(tmp_path, 55, [json.dumps({"from": "S", "text": "handled-hours-ago"}) + "\n"])
    os.utime(inbox, (1000, 1000))
    text = daemon.sweep_nudge_text(55, "dead", now=9999)
    assert "handled-hours-ago" not in text
    assert "Drain with" in text


def test_sweep_nudge_text_unread_flavor(tmp_path, monkeypatch):
    # The "unread" flavor is the other dispatch branch: undrained inbox, not a dead listener.
    _patch_daemon_state(tmp_path, monkeypatch)
    text = daemon.sweep_nudge_text(7, "unread", now=1000)
    assert "undelivered messages in topic 7" in text
    assert "isn't running" not in text                     # not the dead-listener wording


# ---- launcher: unbuffered stdout ---------------------------------------------

def test_launcher_runs_python_unbuffered():
    # the -u flag is the primary drain fix; a revert must fail a test, not just the live check
    with open(os.path.join(REPO, "bin", "tg-bridge"), encoding="utf-8") as f:
        launcher = f.read()
    assert "python3 -u " in launcher
