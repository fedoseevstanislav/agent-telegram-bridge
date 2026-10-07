"""#361: both engines end their turn and read foreground recv on bridge nudges."""

import ast
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from bridge import cli, common, daemon

REPO = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("wait", [86400, 1, 0, -1])
@pytest.mark.parametrize("peek", [False, True])
@pytest.mark.parametrize("unread", [False, True])
def test_retired_wait_exits_promptly_without_reading_or_committing(
    tmp_path, wait, peek, unread
):
    # Exercise the actual CLI parser and main in an isolated subprocess. A tripwire
    # forbids even loading the cursor/reading messages; the on-disk check pins writes.
    topic = tmp_path / "topics" / "4242"
    topic.mkdir(parents=True)
    cursor = topic / "cursor"
    cursor.write_text("0" if unread else "1")
    inbox = topic / "inbox.jsonl"
    inbox.write_text(json.dumps({"ts": "t", "from": "owner", "kind": "text",
                                 "text": "do not consume"}) + "\n")
    before = cursor.read_bytes(), cursor.stat().st_mtime_ns, inbox.read_bytes()
    script = """
import sys
from bridge import cli, common
common.STATE_DIR = sys.argv.pop(1)
cli.load_config = lambda: {}
def forbidden(*args):
    raise AssertionError('retired wait read inbox state')
cli.read_new = cli.load_cursor = forbidden
cli.main()
"""
    env = dict(os.environ)
    env.pop("TMUX_PANE", None)
    started = time.monotonic()
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path), "recv", "--topic", "4242",
         "--wait", str(wait), *(["--peek"] if peek else [])],
        cwd=REPO, env=env, capture_output=True, text=True, timeout=1,
    )
    assert time.monotonic() - started < 1
    assert result.returncode == cli.WAIT_RETIRED_EXIT == 5
    assert result.stdout == ""
    assert "Background waits are retired" in result.stderr
    assert "nudge-driven" in result.stderr and "end your turn" in result.stderr
    assert "[tg-bridge]" in result.stderr and "tg-bridge recv --topic 4242" in result.stderr
    assert len(result.stderr.splitlines()) == 1
    assert (cursor.read_bytes(), cursor.stat().st_mtime_ns, inbox.read_bytes()) == before


def test_foreground_recv_still_drains_and_advances_cursor(monkeypatch, capsys):
    topic = Path(common.state_path("topics", "4242", "inbox.jsonl"))
    topic.write_text(json.dumps({"ts": "t", "from": "owner", "kind": "text",
                                "text": "handle this"}) + "\n")
    topic.with_name("cursor").write_text("0")
    monkeypatch.setattr(cli, "require_own_topic", lambda *a: None)
    monkeypatch.setattr(cli, "send_typing", lambda *a: None)
    cli.cmd_recv({}, cli.build_parser().parse_args(["recv", "--topic", "4242"]))
    assert "handle this" in capsys.readouterr().out
    assert topic.with_name("cursor").read_text() == "1"
    cli.cmd_recv({}, cli.build_parser().parse_args(["recv", "--topic", "4242"]))
    assert capsys.readouterr().out == ""


class _SweepFinished(Exception):
    pass


@pytest.mark.parametrize("engine", ["claude", "codex"])
@pytest.mark.parametrize("live_recv", [False, True])
@pytest.mark.parametrize("unread", [0, 2])
@pytest.mark.parametrize("past_grace", [False, True])
def test_sweep_only_nudges_sustained_unread_backlog(
    monkeypatch, engine, live_recv, unread, past_grace
):
    typed = []
    monkeypatch.setattr(daemon, "read_registry", lambda: {"4242": {"pane": "%42"}})
    monkeypatch.setattr(daemon, "fleet_panes", lambda: [("%42", "s", "node", engine)])
    monkeypatch.setattr(daemon, "pane_alive", lambda _: True)
    monkeypatch.setattr(daemon, "pane_is_idle", lambda _: True)
    monkeypatch.setattr(daemon, "has_live_recv", lambda _: live_recv)
    monkeypatch.setattr(daemon, "unread_count", lambda _: unread)
    monkeypatch.setattr(daemon, "type_line", lambda pane, text: typed.append((pane, text)) or "sent")
    monkeypatch.setattr(daemon, "_prune_pane_tables", lambda: None)
    monkeypatch.setattr(daemon, "_dark_since", {"4242": 0 if past_grace else 999})
    monkeypatch.setattr(daemon, "_last_sweep_nudge", {})
    monkeypatch.setattr(daemon, "_unreadable_streak", {})
    monkeypatch.setattr(daemon.time, "time", lambda: 1000)
    def stop(_):
        raise _SweepFinished
    monkeypatch.setattr(daemon.time, "sleep", stop)
    with pytest.raises(_SweepFinished):
        daemon.idle_sweep_loop({})
    if unread and past_grace:
        assert typed == [("%42", daemon.sweep_nudge_text("4242"))]
        assert "undelivered messages" in typed[0][1]
        assert "tg-bridge recv --topic 4242" in typed[0][1]
        assert "background" not in typed[0][1]
    else:
        assert typed == []
    if not unread:
        assert "4242" not in daemon._dark_since


def test_daemon_strings_and_skill_do_not_instruct_background_waits():
    # AST enumerates constants AND f-string fragments, including nested functions.
    # Named exceptions: none in daemon/skill/AGENTS. The refusal and CLI help live in cli.py
    # and may name the retired flag; legacy classifiers/process matching remain allowed.
    tree = ast.parse((REPO / "bridge/daemon.py").read_text())
    strings = [(node.lineno, node.value) for node in ast.walk(tree)
               if isinstance(node, ast.Constant) and isinstance(node.value, str)]
    strings.extend((name, (REPO / name).read_text())
                   for name in ("skill/SKILL.md", "AGENTS.md"))
    for location, text in strings:
        assert "--wait" not in text, location
        assert "run_in_background" not in text, location


def test_claude_arrival_without_recv_types_the_foreground_nudge(monkeypatch):
    typed, timers = [], []
    monkeypatch.setattr(daemon, "read_registry", lambda: {
        "4242": {"pane": "%42", "engine": "claude", "session_id": "seat"}})
    monkeypatch.setattr(daemon, "has_live_recv", lambda _: False)
    monkeypatch.setattr(daemon, "carry_forward_active", lambda _: False)
    monkeypatch.setattr(daemon, "maybe_auto_revive", lambda *a: None)
    monkeypatch.setattr(daemon, "pane_alive", lambda _: True)
    monkeypatch.setattr(daemon, "type_line", lambda pane, text: typed.append((pane, text)) or "sent")
    class Timer:
        def __init__(self, delay, target, args):
            timers.append((delay, target, args))
        def start(self):
            pass
    monkeypatch.setattr(daemon.threading, "Timer", Timer)
    daemon.handle_message({"chat_id": -1001, "owner_id": 42}, {
        "chat": {"id": -1001}, "from": {"id": 42, "first_name": "owner"},
        "message_thread_id": 4242, "message_id": 101, "text": "work on this",
    })
    assert len(timers) == 1
    delay, target, args = timers[0]
    assert delay == daemon.NUDGE_DELAY
    assert target(*args) is True
    assert typed[0][0] == "%42"
    assert "[tg-bridge]" in typed[0][1]
    assert "New Telegram message" in typed[0][1]
    assert "tg-bridge recv --topic 4242" in typed[0][1]
