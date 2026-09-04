import json

import pytest

from bridge import daemon


def test_claude_resume_uses_last_model(monkeypatch):
    monkeypatch.setattr(daemon, "SPAWN_MODEL", "claude-opus-test[1m]")

    launch = daemon._resume_launch("claude", "SID", model="claude-fable-5")

    assert "--model claude-fable-5" in launch
    assert "claude-opus-test[1m]" not in launch


def test_claude_resume_defaults_to_spawn_model(monkeypatch):
    monkeypatch.setattr(daemon, "SPAWN_MODEL", "claude-opus-test[1m]")

    assert "claude-opus-test[1m]" in daemon._resume_launch("claude", "SID")
    assert "claude-opus-test[1m]" in daemon._resume_launch("claude", "SID", model=None)


def test_codex_resume_carries_no_flags_by_default():
    """A fresh install revives a codex session with its approval prompts intact (#204 D9)."""
    launch = daemon._resume_launch("codex", "SID")

    assert launch == "codex resume SID"
    assert "--model" not in launch          # codex carries model+effort in the rollout


def test_configured_codex_flags_precede_the_resume_subcommand(monkeypatch):
    """Ordering is not cosmetic: the bypass flag is a ROOT flag.

    `codex --dangerously-… resume <sid>` is accepted by clap; `codex resume <sid>
    --dangerously-…` is not. Composing the command from parts made that ordering implicit,
    so it is asserted here rather than left to the order of a format string.
    """
    monkeypatch.setattr(daemon, "load_config", lambda: {
        "spawn_flags": {"codex": "--dangerously-bypass-approvals-and-sandbox"}})

    assert (daemon._resume_launch("codex", "SID")
            == "codex --dangerously-bypass-approvals-and-sandbox resume SID")


def test_last_model_for_session_returns_persisted_model(tmp_path, monkeypatch):
    watchdog = tmp_path / "model-watchdog.json"
    watchdog.write_text(json.dumps({"SID": {"last_model": "claude-fable-5"}}))
    monkeypatch.setattr(daemon, "state_path", lambda *parts: str(watchdog))

    assert daemon.last_model_for_session("SID") == "claude-fable-5"


def test_last_model_for_session_ignores_a_synthetic_watchdog_sample(tmp_path, monkeypatch):
    watchdog = tmp_path / "model-watchdog.json"
    watchdog.write_text(json.dumps({"SID": {"last_model": "<synthetic>"}}))
    monkeypatch.setattr(daemon, "state_path", lambda *parts: str(watchdog))

    assert daemon.last_model_for_session("SID") is None


def test_last_model_for_session_returns_none_for_missing_file(tmp_path, monkeypatch):
    watchdog = tmp_path / "missing.json"
    monkeypatch.setattr(daemon, "state_path", lambda *parts: str(watchdog))

    assert daemon.last_model_for_session("SID") is None


@pytest.mark.parametrize(
    "data",
    [
        {},
        {"OTHER": {"last_model": "claude-fable-5"}},
        {"SID": {"last_model": None}},
        {"SID": None},
        [],
    ],
)
def test_last_model_for_session_returns_none_when_model_is_unavailable(
        tmp_path, monkeypatch, data):
    watchdog = tmp_path / "model-watchdog.json"
    watchdog.write_text(json.dumps(data))
    monkeypatch.setattr(daemon, "state_path", lambda *parts: str(watchdog))

    assert daemon.last_model_for_session("SID") is None


def test_last_model_for_session_returns_none_for_malformed_json(tmp_path, monkeypatch):
    watchdog = tmp_path / "model-watchdog.json"
    watchdog.write_text("{not-json")
    monkeypatch.setattr(daemon, "state_path", lambda *parts: str(watchdog))

    assert daemon.last_model_for_session("SID") is None


@pytest.mark.parametrize(
    ("info", "expected"),
    [
        ({"ended": "2026-07-23T00:00:00Z", "session_id": "SID"}, True),
        ({"ended": "2026-07-23T00:00:00Z", "session_id": None}, False),
        ({"session_id": "SID"}, False),
        ({"ended": "2026-07-23T00:00:00Z", "session_id": "SID", "feed": True}, False),
        ({}, False),
        (None, False),
    ],
)
def test_should_auto_revive_only_closed_resumable_non_feed_topics(info, expected):
    assert daemon.should_auto_revive(info) is expected
