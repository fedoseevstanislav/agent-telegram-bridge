import json

import pytest

from bridge import common, daemon


def _message(text, *, sender_id=42, **extra):
    return {
        "chat": {"id": -1001},
        "from": {"id": sender_id, "first_name": "Sender"},
        "message_thread_id": 4109,
        "message_id": 101,
        "text": text,
        **extra,
    }


@pytest.mark.parametrize("value", [None, "42", 0, -1, True, False, 42.0, [], {}])
def test_load_config_rejects_missing_or_malformed_owner_id(tmp_path, monkeypatch, value):
    path = tmp_path / "config.json"
    payload = {"bot_token": "token", "chat_id": -1001}
    if value is not None:
        payload["owner_id"] = value
    path.write_text(json.dumps(payload))
    path.chmod(0o600)   # the mode the installer requires (#245)
    monkeypatch.setattr(common, "CONFIG_PATH", str(path))

    with pytest.raises(SystemExit, match="owner_id"):
        common.load_config()


def test_load_config_accepts_positive_integer_owner_id(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"bot_token": "token", "chat_id": -1001, "owner_id": 42}))
    path.chmod(0o600)   # the mode the installer requires (#245)
    monkeypatch.setattr(common, "CONFIG_PATH", str(path))

    assert common.load_config()["owner_id"] == 42


def test_config_reload_revalidates_owner_id(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"bot_token": "token", "chat_id": -1001, "owner_id": 42}))
    path.chmod(0o600)   # the mode the installer requires (#245)
    monkeypatch.setattr(common, "CONFIG_PATH", str(path))
    assert common.load_config()["owner_id"] == 42

    path.write_text(json.dumps({"bot_token": "token", "chat_id": -1001}))
    path.chmod(0o600)   # the mode the installer requires (#245)
    with pytest.raises(SystemExit, match="owner_id"):
        common.load_config()


def test_edited_messages_are_not_requested_from_telegram():
    assert json.loads(daemon.ALLOWED_UPDATES) == ["message"]


@pytest.mark.parametrize("text", ["/claude task", "/codex task", "/kill", "/stop", "!redirect"])
def test_non_owner_cannot_reach_spawn_or_control_handlers(monkeypatch, text):
    reached = []
    monkeypatch.setattr(daemon, "handle_command", lambda *_args: reached.append("command"))
    monkeypatch.setattr(daemon, "interrupt_session", lambda *_args: reached.append("interrupt"))

    daemon.handle_message({"chat_id": -1001, "owner_id": 42}, _message(text, sender_id=7))

    assert reached == []


@pytest.mark.parametrize("owner_id", [None, "42", 0, -1, True])
def test_runtime_misconfiguration_also_fails_closed(monkeypatch, owner_id):
    reached = []
    monkeypatch.setattr(daemon, "handle_command", lambda *_args: reached.append("command"))

    daemon.handle_message({"chat_id": -1001, "owner_id": owner_id}, _message("/codex task"))

    assert reached == []


def test_forwarded_content_uses_outer_sender_for_authorization(monkeypatch):
    appended = []
    scheduled = []
    monkeypatch.setattr(daemon, "carry_forward_active", lambda _topic: False)
    monkeypatch.setattr(
        daemon,
        "append_jsonl",
        lambda _path, record: appended.append(record) or common.WakeClaim(0),
    )
    monkeypatch.setattr(daemon, "maybe_auto_revive", lambda _cfg, _topic: None)
    monkeypatch.setattr(
        daemon,
        "schedule_nudge",
        lambda topic, claim: scheduled.append((topic, claim)),
    )

    forwarded = {"forward_origin": {"type": "user", "sender_user": {"id": 999}}}
    daemon.handle_message(
        {"chat_id": -1001, "owner_id": 42},
        _message("forwarded material", sender_id=42, **forwarded),
    )

    assert appended[0]["text"] == "forwarded material"
    assert scheduled == [(4109, common.WakeClaim(0))]


def test_forwarded_content_from_non_owner_is_rejected(monkeypatch):
    appended = []
    monkeypatch.setattr(daemon, "append_jsonl", lambda *_args: appended.append(True))

    daemon.handle_message(
        {"chat_id": -1001, "owner_id": 42},
        _message(
            "forwarded material",
            sender_id=7,
            forward_origin={"type": "user", "sender_user": {"id": 42}},
        ),
    )

    assert appended == []
