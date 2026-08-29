"""Unit tests for drain-before-send (#135).

A reply is composed before it is sent; anything that lands in between makes it stale and
produces the "one answer per voice message" behaviour the owner asked to stop. `tg-bridge send`
therefore refuses while the topic has unread messages, unless `--force`.

The cursor must NOT move: `recv` stays the single place that advances it, so nothing is
lost if the agent dies between the refusal and the read.
"""

import json
import types

import pytest

from bridge import cli


def _inbox(tmp_path, monkeypatch, records, cursor=0):
    topic_dir = tmp_path / "topics" / "4367"
    topic_dir.mkdir(parents=True)
    (topic_dir / "inbox.jsonl").write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records), encoding="utf-8")
    (topic_dir / "cursor").write_text(str(cursor), encoding="utf-8")
    monkeypatch.setattr(cli, "state_path",
                        lambda *parts: str(tmp_path.joinpath(*parts)))
    return topic_dir


def _record(text, ts="2026-08-07T16:09:54+0000", kind="voice"):
    return {"ts": ts, "message_id": 1, "thread_id": 4367,
            "from": "Владелец", "kind": kind, "text": text}


def _args(text="reply", force=False):
    return types.SimpleNamespace(text=text, topic="4367", force=force, json=False)


def _no_send(monkeypatch):
    sent = []
    monkeypatch.setattr(cli, "send_text", lambda cfg, topic, text: sent.append((topic, text)))
    monkeypatch.setattr(cli, "resolve_topic", lambda args: 4367)
    return sent


def test_send_refuses_while_a_message_is_unread(tmp_path, monkeypatch, capsys):
    _inbox(tmp_path, monkeypatch, [_record("and one more thing")])
    sent = _no_send(monkeypatch)

    with pytest.raises(SystemExit) as exit_info:
        cli.cmd_send({}, _args())

    assert exit_info.value.code == cli.UNREAD_EXIT
    assert sent == []                       # nothing reached Telegram
    out = capsys.readouterr().out
    assert "NOT SENT" in out
    assert "and one more thing" in out      # the agent is shown what it missed


def test_refusal_does_not_advance_the_cursor(tmp_path, monkeypatch):
    topic_dir = _inbox(tmp_path, monkeypatch, [_record("first"), _record("second")])
    _no_send(monkeypatch)

    with pytest.raises(SystemExit):
        cli.cmd_send({}, _args())

    # recv is the only cursor writer: a refusal that consumed the messages would make them
    # unreadable, which is the silent-loss class #105 fixed.
    assert (topic_dir / "cursor").read_text() == "0"
    assert len(cli.unread_before_send(4367)) == 2


def test_send_proceeds_when_everything_is_read(tmp_path, monkeypatch):
    _inbox(tmp_path, monkeypatch, [_record("already handled")], cursor=1)
    sent = _no_send(monkeypatch)

    cli.cmd_send({}, _args(text="my reply"))

    assert sent == [(4367, "my reply")]


def test_force_sends_despite_unread(tmp_path, monkeypatch):
    _inbox(tmp_path, monkeypatch, [_record("wait, also…")])
    sent = _no_send(monkeypatch)

    cli.cmd_send({}, _args(text="still working on it", force=True))

    assert sent == [(4367, "still working on it")]


def test_send_works_with_no_inbox_at_all(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "state_path", lambda *parts: str(tmp_path.joinpath(*parts)))
    sent = _no_send(monkeypatch)

    cli.cmd_send({}, _args(text="first contact"))

    assert sent == [(4367, "first contact")]


def test_refusal_names_the_command_that_clears_it(tmp_path, monkeypatch, capsys):
    _inbox(tmp_path, monkeypatch, [_record("one more")])
    _no_send(monkeypatch)

    with pytest.raises(SystemExit):
        cli.cmd_send({}, _args())

    out = capsys.readouterr().out
    # Without this an agent retries send, hits exit 3 again, and livelocks: the printed
    # records are a preview, so only an explicit recv clears the block.
    assert "PREVIEW" in out
    assert "tg-bridge recv --topic 4367" in out


def test_recv_then_send_recovers(tmp_path, monkeypatch, capsys):
    _inbox(tmp_path, monkeypatch, [_record("and also this")])
    sent = _no_send(monkeypatch)
    monkeypatch.setattr(cli, "send_typing", lambda cfg, topic: None)

    with pytest.raises(SystemExit) as blocked:
        cli.cmd_send({}, _args())
    assert blocked.value.code == cli.UNREAD_EXIT

    # The documented recovery path must actually unblock the agent.
    cli.cmd_recv({}, types.SimpleNamespace(topic="4367", wait=None, peek=False, json=False))
    cli.cmd_send({}, _args(text="one reply covering both"))

    assert sent == [(4367, "one reply covering both")]
    capsys.readouterr()


def test_feed_topics_are_never_blocked(tmp_path, monkeypatch):
    _inbox(tmp_path, monkeypatch, [_record("stray inbound on a feed")])
    sent = _no_send(monkeypatch)
    # An outbound-only feed has no reader, so its unread would never clear and every
    # later post would be blocked forever.
    monkeypatch.setattr(cli, "read_registry", lambda: {"4367": {"feed": True}})

    cli.cmd_send({}, _args(text="feed event"))

    assert sent == [(4367, "feed event")]


def test_dialog_topics_are_still_blocked_when_a_registry_entry_exists(tmp_path, monkeypatch):
    _inbox(tmp_path, monkeypatch, [_record("real message")])
    sent = _no_send(monkeypatch)
    monkeypatch.setattr(cli, "read_registry", lambda: {"4367": {"name": "dialog"}})

    with pytest.raises(SystemExit):
        cli.cmd_send({}, _args())

    assert sent == []


def test_unread_exit_code_is_distinct_from_the_recv_timeout(tmp_path, monkeypatch):
    """`recv --wait` exits 2 on timeout; the refusal must be tellable apart from it."""
    _inbox(tmp_path, monkeypatch, [_record("x")])
    _no_send(monkeypatch)

    with pytest.raises(SystemExit) as blocked:
        cli.cmd_send({}, _args())

    monkeypatch.setattr(cli, "send_typing", lambda cfg, topic: None)
    cli.cmd_recv({}, types.SimpleNamespace(topic="4367", wait=None, peek=False, json=False))
    with pytest.raises(SystemExit) as timed_out:   # now nothing is unread: a real timeout
        # `wait=0` would be falsy and take the non-blocking branch — it must be a real wait.
        cli.cmd_recv({}, types.SimpleNamespace(topic="4367", wait=1, peek=False, json=False))

    assert blocked.value.code != timed_out.value.code


def test_send_parser_accepts_force():
    parser = cli.build_parser()
    args = parser.parse_args(["send", "--topic", "4367", "--force", "hello"])
    assert args.force is True
    assert parser.parse_args(["send", "--topic", "4367", "hello"]).force is False
