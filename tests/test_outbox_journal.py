"""Outbound Telegram delivery journal tests for issue #160."""

import hashlib
import json
from types import SimpleNamespace

import pytest

from bridge import cli, common


CFG = {"bot_token": "token", "chat_id": -100}


def _records(tmp_path, topic_id):
    path = tmp_path / "topics" / str(topic_id) / "outbox.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()]


def _api_recorder(monkeypatch):
    calls = []

    def api(_token, method, params):
        assert method == "sendMessage"
        calls.append(params)
        return {"message_id": 9000 + len(calls)}

    monkeypatch.setattr(common, "api", api)
    return calls


def _send_env(tmp_path, monkeypatch):
    monkeypatch.setattr(common, "STATE_DIR", str(tmp_path))
    monkeypatch.setattr(cli, "read_registry", lambda: {})


class _BrokenStderr:
    def __init__(self):
        self.write_calls = 0

    def write(self, _text):
        self.write_calls += 1
        raise BrokenPipeError("stderr is closed")


def test_successful_send_appends_outbox_record_for_exact_api_text(tmp_path, monkeypatch):
    _send_env(tmp_path, monkeypatch)
    calls = _api_recorder(monkeypatch)
    monkeypatch.setattr(cli, "now_iso", lambda: "2026-08-20T12:34:56+0300")

    cli.send_text(CFG, 55, "hello **bold**")

    record, = _records(tmp_path, 55)
    assert record == {
        "ts": "2026-08-20T12:34:56+0300",
        "message_id": 9001,
        "content_sha256": hashlib.sha256(calls[0]["text"].encode("utf-8")).hexdigest(),
        "content_chars": len(calls[0]["text"]),
        "kind": "send",
        "chunk_index": 0,
        "chunk_count": 1,
    }
    assert calls[0]["text"] == "hello <b>bold</b>"


def test_every_send_chunk_is_journaled_with_its_own_message_id(tmp_path, monkeypatch):
    _send_env(tmp_path, monkeypatch)
    calls = _api_recorder(monkeypatch)
    text = "x" * 4000

    cli.send_text(CFG, 55, text)

    records = _records(tmp_path, 55)
    assert len(calls) == len(records) == 2
    assert [record["message_id"] for record in records] == [9001, 9002]
    assert [record["chunk_index"] for record in records] == [0, 1]
    assert {record["chunk_count"] for record in records} == {2}
    assert [record["content_sha256"] for record in records] == [
        hashlib.sha256(call["text"].encode("utf-8")).hexdigest() for call in calls
    ]


def test_outbox_failure_is_fail_open_for_send(tmp_path, monkeypatch, capsys):
    _send_env(tmp_path, monkeypatch)
    calls = _api_recorder(monkeypatch)
    outbox_dir = tmp_path / "topics" / "55"
    outbox_dir.mkdir(parents=True)
    outbox_dir.chmod(0o500)

    try:
        cli.send_text(CFG, 55, "still delivered")
    finally:
        outbox_dir.chmod(0o700)

    assert len(calls) == 1
    assert not (outbox_dir / "outbox.jsonl").exists()
    warning_lines = capsys.readouterr().err.splitlines()
    assert len(warning_lines) == 1
    assert "warning:" in warning_lines[0].lower()
    assert "outbox" in warning_lines[0].lower()


def test_outbox_failure_and_broken_stderr_are_fail_open_for_send(tmp_path, monkeypatch):
    _send_env(tmp_path, monkeypatch)
    calls = _api_recorder(monkeypatch)
    outbox_dir = tmp_path / "topics" / "55"
    outbox_dir.mkdir(parents=True)
    outbox = outbox_dir / "outbox.jsonl"
    outbox.write_text("")
    outbox.chmod(0o400)
    broken_stderr = _BrokenStderr()
    monkeypatch.setattr(cli, "sys", SimpleNamespace(stderr=broken_stderr))

    try:
        cli.send_text(CFG, 55, "still delivered")
    finally:
        outbox.chmod(0o600)

    assert len(calls) == 1
    assert broken_stderr.write_calls == 1


def test_possibly_delivered_send_journals_null_message_id(tmp_path, monkeypatch):
    _send_env(tmp_path, monkeypatch)

    def ambiguous(_token, _method, _params):
        raise common.PossiblyDelivered("lost Telegram acknowledgement")

    monkeypatch.setattr(common, "api", ambiguous)

    with pytest.raises(common.PossiblyDelivered):
        cli.send_text(CFG, 55, "hello **bold**")

    record, = _records(tmp_path, 55)
    assert record["message_id"] is None
    assert record["delivery"] == "possibly_delivered"
    assert record["content_sha256"] == hashlib.sha256(
        b"hello <b>bold</b>"
    ).hexdigest()
    assert record["content_chars"] == len("hello <b>bold</b>")
    assert record["kind"] == "send"
    assert record["chunk_index"] == 0
    assert record["chunk_count"] == 1


def test_multichunk_possibly_delivered_journals_completed_then_ambiguous(
    tmp_path, monkeypatch
):
    _send_env(tmp_path, monkeypatch)
    calls = []

    def ambiguous_third(_token, method, params):
        assert method == "sendMessage"
        calls.append(params)
        if len(calls) == 3:
            raise common.PossiblyDelivered("lost Telegram acknowledgement")
        return {"message_id": 9100 + len(calls)}

    monkeypatch.setattr(common, "api", ambiguous_third)

    with pytest.raises(common.PossiblyDelivered):
        cli.send_text(CFG, 55, "x" * (common._TG_HTML_LIMIT * 2 + 1))

    records = _records(tmp_path, 55)
    assert len(calls) == len(records) == 3
    assert [record["message_id"] for record in records] == [9101, 9102, None]
    assert [record["chunk_index"] for record in records] == [0, 1, 2]
    assert {record["chunk_count"] for record in records} == {3}
    assert [record["content_sha256"] for record in records] == [
        hashlib.sha256(call["text"].encode("utf-8")).hexdigest() for call in calls
    ]
    assert "delivery" not in records[0]
    assert "delivery" not in records[1]
    assert records[2]["delivery"] == "possibly_delivered"


def test_every_chunk_records_its_own_length(tmp_path, monkeypatch):
    """Both write sites journal content_chars — the length of the exact per-chunk text
    handed to the API, never the body itself (length only; the journal stays as
    content-free as the hash next to it)."""
    _send_env(tmp_path, monkeypatch)
    calls = []

    def ambiguous_second(_token, method, params):
        assert method == "sendMessage"
        calls.append(params)
        if len(calls) == 2:
            raise common.PossiblyDelivered("lost Telegram acknowledgement")
        return {"message_id": 9300 + len(calls)}

    monkeypatch.setattr(common, "api", ambiguous_second)

    with pytest.raises(common.PossiblyDelivered):
        cli.send_text(CFG, 55, "y" * (common._TG_HTML_LIMIT + 1))

    records = _records(tmp_path, 55)
    assert len(calls) == len(records) == 2
    assert "delivery" not in records[0]                       # delivered site
    assert records[1]["delivery"] == "possibly_delivered"     # ambiguous site
    assert [record["content_chars"] for record in records] == [
        len(call["text"]) for call in calls
    ]
    assert all(record["content_chars"] > 0 for record in records)


def test_possibly_delivered_without_api_text_omits_untruthful_fields(tmp_path, monkeypatch):
    _send_env(tmp_path, monkeypatch)
    error = common.PossiblyDelivered("missing delivery metadata")

    cli.journal_possibly_delivered(55, "send", error, "Telegram never received this")

    record, = _records(tmp_path, 55)
    assert record == {
        "ts": record["ts"],
        "message_id": None,
        "kind": "send",
        "delivery": "possibly_delivered",
    }


def test_plain_text_fallback_hashes_text_telegram_accepted(tmp_path, monkeypatch):
    _send_env(tmp_path, monkeypatch)
    calls = []

    def reject_html(_token, method, params):
        assert method == "sendMessage"
        calls.append(params)
        if "parse_mode" in params:
            raise RuntimeError("can't parse entities")
        return {"message_id": 9201}

    monkeypatch.setattr(common, "api", reject_html)

    cli.send_text(CFG, 55, "List<int> and **bold**")

    assert len(calls) == 2
    assert calls[0]["text"] == "List&lt;int&gt; and <b>bold</b>"
    assert calls[1]["text"] == "List<int> and **bold**"
    record, = _records(tmp_path, 55)
    assert record["content_sha256"] == hashlib.sha256(
        calls[1]["text"].encode("utf-8")
    ).hexdigest()
    assert record["content_sha256"] != hashlib.sha256(
        calls[0]["text"].encode("utf-8")
    ).hexdigest()


def test_notify_mirror_appends_notify_outbox_record(tmp_path, monkeypatch):
    monkeypatch.setattr(common, "STATE_DIR", str(tmp_path))
    monkeypatch.setattr(
        cli,
        "read_registry",
        lambda: {"77": {"name": "target", "pane": "%77", "engine": "codex"}},
    )
    monkeypatch.setattr(cli, "pane_alive", lambda _pane: True, raising=False)
    monkeypatch.setattr(cli, "maybe_nudge", lambda *_args: True, raising=False)
    calls = _api_recorder(monkeypatch)

    result = cli.notify_topic(CFG, 77, "orchestra", "job-1", "finished")

    assert result["telegram"] == "posted"
    record, = _records(tmp_path, 77)
    assert record["kind"] == "notify"
    assert record["message_id"] == 9001
    assert record["content_sha256"] == hashlib.sha256(
        calls[0]["text"].encode("utf-8")
    ).hexdigest()
    assert calls[0]["text"] == "orchestra: finished"


def test_notify_journal_and_stderr_failures_still_report_posted(tmp_path, monkeypatch):
    monkeypatch.setattr(common, "STATE_DIR", str(tmp_path))
    monkeypatch.setattr(
        cli,
        "read_registry",
        lambda: {"77": {"name": "target", "pane": "%77", "engine": "codex"}},
    )
    monkeypatch.setattr(cli, "pane_alive", lambda _pane: True, raising=False)
    monkeypatch.setattr(cli, "maybe_nudge", lambda *_args: True, raising=False)
    calls = _api_recorder(monkeypatch)
    outbox_dir = tmp_path / "topics" / "77"
    outbox_dir.mkdir(parents=True)
    outbox = outbox_dir / "outbox.jsonl"
    outbox.write_text("")
    outbox.chmod(0o400)
    broken_stderr = _BrokenStderr()
    monkeypatch.setattr(cli, "sys", SimpleNamespace(stderr=broken_stderr))

    try:
        result = cli.notify_topic(CFG, 77, "orchestra", "job-1", "finished")
    finally:
        outbox.chmod(0o600)

    assert len(calls) == 1
    assert result["telegram"] == "posted"
    assert broken_stderr.write_calls == 1
