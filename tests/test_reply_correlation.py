"""Inbound Telegram reply correlation tests for issue #160."""

import pytest

from bridge import daemon


CFG = {"chat_id": -100, "owner_id": 42}


def _capture_record(monkeypatch, message):
    records = []
    monkeypatch.setattr(daemon, "carry_forward_active", lambda _topic: False)
    monkeypatch.setattr(daemon, "state_path", lambda *_parts: "/unused/inbox.jsonl")
    monkeypatch.setattr(
        daemon,
        "append_jsonl",
        lambda _path, record: records.append(record),
    )
    monkeypatch.setattr(daemon, "maybe_auto_revive", lambda *_args: None)
    monkeypatch.setattr(daemon, "schedule_nudge", lambda *_args: None)
    monkeypatch.setattr(daemon, "log", lambda *_args: None)

    daemon.handle_message(CFG, message)

    record, = records
    return record


def _message(**extra):
    return {
        "chat": {"id": -100},
        "from": {"id": 42, "first_name": "the owner"},
        "message_thread_id": 77,
        "message_id": 501,
        "text": "the reply",
        **extra,
    }


def test_inbox_record_carries_replied_to_message_id(monkeypatch):
    record = _capture_record(
        monkeypatch,
        _message(reply_to_message={"message_id": 1234, "text": "original"}),
    )

    assert record == {
        "ts": record["ts"],
        "message_id": 501,
        "reply_to_message_id": 1234,
        "thread_id": 77,
        "from": "the owner",
        "kind": "text",
        "text": "the reply",
    }


def test_inbox_record_omits_reply_key_for_non_reply(monkeypatch):
    record = _capture_record(monkeypatch, _message())

    assert record == {
        "ts": record["ts"],
        "message_id": 501,
        "thread_id": 77,
        "from": "the owner",
        "kind": "text",
        "text": "the reply",
    }


@pytest.mark.parametrize(
    "reply_to_message",
    [
        {"message_id": 77, "text": "topic root"},
        {"message_id": 1234, "forum_topic_created": {}},
        None,
        {},
        "malformed",
        {"message_id": "not-an-integer"},
    ],
)
def test_inbox_record_omits_reply_key_for_topic_root_or_malformed_shape(
    monkeypatch, reply_to_message
):
    record = _capture_record(
        monkeypatch,
        _message(reply_to_message=reply_to_message),
    )

    assert "reply_to_message_id" not in record
