"""Unit tests for the outbound echo into the sender's own topic (#140).

`notify` mirrored only into the recipient's topic, so each thread held just the half of the
conversation it received. The first real peer exchange (topics 6258 ↔ 8713, 2026-08-10) put
four questions in one topic and their four answers in the other: reading either one, the owner
saw replies with nothing they answered. Every hop had reached Telegram; neither thread read
as a conversation.

The echo is a mirror, not a delivery. Two properties carry that distinction and are the
reason most of these tests exist: it writes no inbox record (a record would hand the sender
its own message and wake it for it), and it can never turn a delivered message into a
failed one.
"""

import json

import pytest

from bridge import cli, common

CALLER_PANE = "%A"
A, B = 111, 222
A_ICON, B_ICON = "🦊", "🐙"
CFG = {"bot_token": "token", "chat_id": -100}


def _registry():
    return {
        "111": {"name": "Alpha", "created": "2026-08-10T10:00:00+0000",
                "pane": CALLER_PANE, "icon": A_ICON},
        "222": {"name": "Beta", "created": "2026-08-10T10:05:00+0000",
                "pane": "%B", "icon": B_ICON},
    }


def _env(monkeypatch, tmp_path, pane=CALLER_PANE, send=None):
    if pane is None:
        monkeypatch.delenv("TMUX_PANE", raising=False)
    else:
        monkeypatch.setenv("TMUX_PANE", pane)
    monkeypatch.setattr(common, "STATE_DIR", str(tmp_path))
    monkeypatch.setattr(cli, "read_registry", _registry)
    monkeypatch.setattr(cli, "pane_alive", lambda _pane: True, raising=False)
    monkeypatch.setattr(cli, "maybe_nudge", lambda *args: True, raising=False)
    sent = []
    monkeypatch.setattr(
        cli, "send_message",
        send or (lambda token, chat, text, thread: sent.append((thread, text))))
    return sent


def _notify(key="k1", text="the question", target=B):
    return cli.notify_topic(CFG, target, None, key, text)


def test_sender_topic_shows_what_it_sent(monkeypatch, tmp_path):
    sent = _env(monkeypatch, tmp_path)

    result = _notify()

    assert result["echo"] == "posted"
    threads = [thread for thread, _text in sent]
    assert threads == [B, A]                       # target mirror first, then the echo home
    echo = dict((thread, text) for thread, text in sent)[A]
    # Marked as outbound and naming the target: without both, A's thread reads as if the
    # message arrived rather than left, which is the confusion this fixes.
    assert echo.startswith(f"{A_ICON} → Beta (topic {B}): ")
    assert "the question" in echo


def test_echo_writes_no_inbox_record(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)

    _notify()

    # A record in A's own inbox would be handed back to A as unread and wake it for its own
    # message — the loop the design refuses to create. Only B is written to.
    assert (tmp_path / "topics" / str(B) / "inbox.jsonl").exists()
    assert not (tmp_path / "topics" / str(A) / "inbox.jsonl").exists()


def test_echo_carries_the_senders_icon_not_the_targets(monkeypatch, tmp_path):
    sent = _env(monkeypatch, tmp_path)

    _notify()

    echo = dict((thread, text) for thread, text in sent)[A]
    # The icon is the sender's signature. B's icon here would repeat the impersonation
    # #138 removed from the target mirror, in the other direction.
    assert A_ICON in echo
    assert B_ICON not in echo


def test_a_failing_echo_never_fails_the_delivery(monkeypatch, tmp_path):
    def send(token, chat, text, thread):
        if thread == A:
            raise RuntimeError("telegram down for the echo")

    _env(monkeypatch, tmp_path, send=send)

    result = _notify()

    # The message IS enqueued and mirrored; a cosmetic echo must not decide that.
    assert result["status"] == "enqueued"
    assert result["telegram"] == "posted"
    assert result["echo"] == "failed"


def test_echo_is_reported_ambiguous_like_the_other_side_effects(monkeypatch, tmp_path):
    def send(token, chat, text, thread):
        if thread == A:
            raise cli.PossiblyDelivered("read timeout after connect")

    _env(monkeypatch, tmp_path, send=send)

    assert _notify()["echo"] == "ambiguous"


def test_automation_without_a_topic_has_nothing_to_echo(monkeypatch, tmp_path):
    sent = _env(monkeypatch, tmp_path, pane=None)

    result = cli.notify_topic(CFG, B, "orchestra", "k1", "build finished")

    assert result["echo"] == "skipped"
    assert [thread for thread, _text in sent] == [B]


def test_a_session_notifying_itself_does_not_double_post(monkeypatch, tmp_path):
    sent = _env(monkeypatch, tmp_path)

    result = cli.notify_topic(CFG, A, None, "k1", "note to self")

    # Target mirror and echo would land in the same thread, printing the text twice.
    assert result["echo"] == "skipped"
    assert [thread for thread, _text in sent] == [A]


def test_a_duplicate_key_echoes_nothing(monkeypatch, tmp_path):
    sent = _env(monkeypatch, tmp_path)
    _notify(key="same")
    sent.clear()

    result = _notify(key="same", text="the question")

    # Nothing was delivered on a duplicate, so nothing may be shown as sent.
    assert result == {"status": "duplicate", "topic_id": B}
    assert sent == []


def test_echo_falls_back_to_the_topic_id_when_the_target_is_unnamed(monkeypatch, tmp_path):
    registry = _registry()
    del registry["222"]["name"]
    monkeypatch.setattr(cli, "read_registry", lambda: registry)
    sent = _env(monkeypatch, tmp_path)
    monkeypatch.setattr(cli, "read_registry", lambda: registry)

    _notify()

    echo = dict((thread, text) for thread, text in sent)[A]
    assert f"→ topic {B} (topic {B}): " not in echo   # no doubled id
    assert echo.startswith(f"{A_ICON} → topic {B}: ")
