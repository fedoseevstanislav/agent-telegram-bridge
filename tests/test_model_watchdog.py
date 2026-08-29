import importlib
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from bridge.common import PossiblyDelivered


def _watchdog():
    try:
        return importlib.import_module("bridge.model_watchdog")
    except ModuleNotFoundError:
        pytest.fail("bridge.model_watchdog is not implemented")


def _write_jsonl(path, records):
    path.write_text(
        "".join(json.dumps(record) + "\n" for record in records),
        encoding="utf-8",
    )


def test_fallback_event_detected_once_and_watermark_suppresses_repeat(tmp_path, monkeypatch):
    watchdog = _watchdog()
    transcript = tmp_path / "session.jsonl"
    _write_jsonl(transcript, [
        {
            "type": "system",
            "subtype": "model_refusal_fallback",
            "timestamp": "2026-07-15T10:44:30.346Z",
            "content": "Safeguards flagged this message. Switched to Opus 4.8.",
            "fallbackModel": "claude-opus-4-8",
        },
    ])
    state = {
        "session-1": {
            "last_fallback_ts": "2026-07-15T10:00:00.000Z",
            "last_model": "claude-opus-4-8",
        }
    }
    alerts = []
    monkeypatch.setattr(watchdog, "send_topic", lambda cfg, topic, text: alerts.append((topic, text)))
    monkeypatch.setattr(watchdog, "save_state", lambda current: None)

    watchdog.check_transcript({}, 3089, "session-1", transcript, state)
    watchdog.check_transcript({}, 3089, "session-1", transcript, state)

    assert len(alerts) == 1
    assert alerts[0][0] == 3089
    assert "Safeguards flagged this message" in alerts[0][1]
    assert "session now runs claude-opus-4-8" in alerts[0][1]
    assert "2026-07-15 13:44 UTC+3" in alerts[0][1]
    assert state["session-1"]["last_fallback_ts"] == "2026-07-15T10:44:30.346Z"


def test_model_transition_detected_once(tmp_path, monkeypatch):
    watchdog = _watchdog()
    transcript = tmp_path / "session.jsonl"
    _write_jsonl(transcript, [
        {
            "type": "assistant",
            "timestamp": "2026-07-17T09:15:00.000Z",
            "message": {"model": "claude-opus-4-8"},
        },
    ])
    state = {"session-1": {"last_fallback_ts": None, "last_model": "claude-fable-5"}}
    alerts = []
    monkeypatch.setattr(watchdog, "send_topic", lambda cfg, topic, text: alerts.append(text))
    monkeypatch.setattr(watchdog, "save_state", lambda current: None)

    watchdog.check_transcript({}, 3089, "session-1", transcript, state)
    watchdog.check_transcript({}, 3089, "session-1", transcript, state)

    assert len(alerts) == 1
    assert "claude-fable-5 → claude-opus-4-8" in alerts[0]
    assert state["session-1"]["last_model"] == "claude-opus-4-8"


def test_synthetic_assistant_model_is_ignored(tmp_path, monkeypatch):
    watchdog = _watchdog()
    transcript = tmp_path / "session.jsonl"
    _write_jsonl(transcript, [
        {"type": "assistant", "message": {"model": "claude-fable-5"}},
        {"type": "assistant", "message": {"model": "<synthetic>"}},
    ])
    state = {"session-1": {"last_fallback_ts": None, "last_model": "claude-fable-5"}}
    alerts = []
    monkeypatch.setattr(watchdog, "send_topic", lambda cfg, topic, text: alerts.append(text))
    monkeypatch.setattr(watchdog, "save_state", lambda current: None)

    watchdog.check_transcript({}, 3089, "session-1", transcript, state)

    assert alerts == []
    assert state["session-1"]["last_model"] == "claude-fable-5"


def test_tail_read_is_bounded_on_large_transcript(tmp_path):
    watchdog = _watchdog()
    transcript = tmp_path / "large.jsonl"
    first = json.dumps({"marker": "outside-tail"}) + "\n"
    filler = json.dumps({"padding": "x" * 1000}) + "\n"
    last = json.dumps({"marker": "inside-tail"}) + "\n"
    transcript.write_text(first + filler * 2200 + last, encoding="utf-8")

    records = watchdog.read_tail_records(transcript)

    assert transcript.stat().st_size > watchdog.TAIL_BYTES
    assert not any(record.get("marker") == "outside-tail" for record in records)
    assert records[-1]["marker"] == "inside-tail"


def test_malformed_lines_are_skipped(tmp_path):
    watchdog = _watchdog()
    transcript = tmp_path / "session.jsonl"
    transcript.write_text(
        "not-json\n"
        + json.dumps({"type": "assistant", "message": {"model": "claude-opus-4-8"}})
        + "\n{unfinished\n",
        encoding="utf-8",
    )

    scan = watchdog.scan_transcript(transcript)

    assert scan["last_model"] == "claude-opus-4-8"


def test_first_run_baselines_without_alerting(tmp_path, monkeypatch):
    watchdog = _watchdog()
    transcript = tmp_path / "session.jsonl"
    _write_jsonl(transcript, [
        {
            "type": "system",
            "subtype": "model_refusal_fallback",
            "timestamp": "2026-07-15T10:44:30.346Z",
            "content": "Historical fallback",
            "fallbackModel": "claude-opus-4-8",
        },
        {
            "type": "assistant",
            "timestamp": "2026-07-15T10:45:00.000Z",
            "message": {"model": "claude-opus-4-8"},
        },
    ])
    state = {}
    alerts = []
    monkeypatch.setattr(watchdog, "send_topic", lambda cfg, topic, text: alerts.append(text))
    monkeypatch.setattr(watchdog, "save_state", lambda current: None)

    watchdog.check_transcript({}, 3089, "session-1", transcript, state)

    assert alerts == []
    assert state == {
        "session-1": {
            "last_fallback_ts": "2026-07-15T10:44:30.346Z",
            "last_model": "claude-opus-4-8",
        }
    }


def test_possibly_delivered_alert_is_watermarked_and_not_retried(tmp_path, monkeypatch):
    watchdog = _watchdog()
    transcript = tmp_path / "session.jsonl"
    _write_jsonl(transcript, [
        {
            "type": "system",
            "subtype": "model_refusal_fallback",
            "timestamp": "2026-07-15T10:44:30.346Z",
            "content": "Fallback",
            "fallbackModel": "claude-opus-4-8",
        },
    ])
    state = {"session-1": {"last_fallback_ts": None, "last_model": None}}
    attempts = []

    def ambiguous_send(cfg, topic, text):
        attempts.append(text)
        raise PossiblyDelivered("Telegram may already have it")

    monkeypatch.setattr(watchdog, "send_topic", ambiguous_send)
    monkeypatch.setattr(watchdog, "save_state", lambda current: None)

    watchdog.check_transcript({}, 3089, "session-1", transcript, state)
    watchdog.check_transcript({}, 3089, "session-1", transcript, state)

    assert len(attempts) == 1
    assert state["session-1"]["last_fallback_ts"] == "2026-07-15T10:44:30.346Z"


def test_sweep_routes_codex_and_skips_feed_and_dead_panes(monkeypatch, tmp_path):
    watchdog = _watchdog()
    registry = {
        "1": {"pane": "%1", "cwd": str(tmp_path)},
        "2": {"pane": "%2", "cwd": str(tmp_path), "feed": True},
        "3": {"pane": "%3", "cwd": str(tmp_path), "session_id": "thread-3"},
        "4": {"pane": "%4", "cwd": str(tmp_path)},
        "5": {"pane": "%5", "cwd": str(tmp_path)},
    }
    checked = []
    codex_checked = []
    monkeypatch.setattr(watchdog, "read_registry", lambda: registry)
    monkeypatch.setattr(watchdog, "load_state", lambda: {})
    monkeypatch.setattr(watchdog, "save_state", lambda state: None)
    monkeypatch.setattr(watchdog, "pane_alive", lambda pane: pane != "%4")
    monkeypatch.setattr(
        watchdog,
        "engine_of_pane",
        lambda pane: {"%3": "codex", "%5": None}.get(pane, "claude"),
    )
    monkeypatch.setattr(watchdog, "read_context", lambda pane: {"session_id": "session-1"})
    monkeypatch.setattr(watchdog, "transcript_path", lambda cwd, sid: Path("/transcript"))
    monkeypatch.setattr(watchdog.os.path, "isfile", lambda path: True)
    monkeypatch.setattr(
        watchdog,
        "check_transcript",
        lambda cfg, topic, sid, path, state: checked.append((topic, sid, path)),
    )
    monkeypatch.setattr(
        watchdog,
        "check_codex",
        lambda cfg, topic, info, pane, state: codex_checked.append((topic, info["session_id"], pane)),
    )

    watchdog.sweep({"bot_token": "token", "chat_id": -100})

    assert checked == [(1, "session-1", Path("/transcript"))]
    assert codex_checked == [(3, "thread-3", "%3")]


def test_bad_transcript_does_not_stop_other_sessions(monkeypatch, tmp_path):
    watchdog = _watchdog()
    registry = {
        "1": {"pane": "%1", "cwd": str(tmp_path)},
        "2": {"pane": "%2", "cwd": str(tmp_path)},
    }
    checked = []
    monkeypatch.setattr(watchdog, "read_registry", lambda: registry)
    monkeypatch.setattr(watchdog, "load_state", lambda: {})
    monkeypatch.setattr(watchdog, "save_state", lambda state: None)
    monkeypatch.setattr(watchdog, "pane_alive", lambda pane: True)
    monkeypatch.setattr(watchdog, "engine_of_pane", lambda pane: "claude")
    monkeypatch.setattr(watchdog, "read_context", lambda pane: {"session_id": pane})
    monkeypatch.setattr(watchdog, "transcript_path", lambda cwd, sid: Path(f"/{sid}.jsonl"))
    monkeypatch.setattr(watchdog.os.path, "isfile", lambda path: True)

    def inspect(cfg, topic, sid, path, state):
        if topic == 1:
            raise ValueError("malformed transcript")
        checked.append((topic, sid))

    monkeypatch.setattr(watchdog, "check_transcript", inspect)

    watchdog.sweep({"bot_token": "token", "chat_id": -100})

    assert checked == [(2, "%2")]


def test_sweep_uses_latest_registered_topic_for_a_pane(monkeypatch, tmp_path):
    watchdog = _watchdog()
    registry = {
        "100": {"pane": "%1", "cwd": str(tmp_path)},
        "200": {"pane": "%1", "cwd": str(tmp_path)},
    }
    checked = []
    monkeypatch.setattr(watchdog, "read_registry", lambda: registry)
    monkeypatch.setattr(watchdog, "load_state", lambda: {})
    monkeypatch.setattr(watchdog, "save_state", lambda state: None)
    monkeypatch.setattr(watchdog, "pane_alive", lambda pane: True)
    monkeypatch.setattr(watchdog, "engine_of_pane", lambda pane: "claude")
    monkeypatch.setattr(watchdog, "read_context", lambda pane: {"session_id": "session-1"})
    monkeypatch.setattr(watchdog, "transcript_path", lambda cwd, sid: Path("/transcript"))
    monkeypatch.setattr(watchdog.os.path, "isfile", lambda path: True)
    monkeypatch.setattr(
        watchdog,
        "check_transcript",
        lambda cfg, topic, sid, path, state: checked.append(topic),
    )

    watchdog.sweep({"bot_token": "token", "chat_id": -100})

    assert checked == [200]


def test_transcript_path_flattens_every_non_alphanumeric_char():
    # Real on-disk convention (verified in ~/.claude/projects/): dots flatten to '-'
    # too, so /home/user/.worktrees/... yields a double dash, not a literal dot.
    watchdog = _watchdog()
    path = watchdog.transcript_path("/home/user/.worktrees/12-feature-branch", "abc123")
    assert path.endswith("/-home-user--worktrees-12-feature-branch/abc123.jsonl")
    plain = watchdog.transcript_path("/home/user", "abc123")
    assert plain.endswith("/-home-user/abc123.jsonl")



# ---- codex model/effort drift (#121) -----------------------------------------

def _settings_event(model, effort, timestamp="2026-07-31T21:08:32.023Z"):
    return {
        "timestamp": timestamp,
        "type": "event_msg",
        "payload": {
            "type": "thread_settings_applied",
            "thread_settings": {"model": model, "reasoning_effort": effort, "cwd": "/home/user"},
        },
    }


def _old(seconds_ago=3600):
    stamp = datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)
    return stamp.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _codex_rollout(path, pairs):
    _write_jsonl(path, [
        {"type": "session_meta", "payload": {"cwd": "/home/user"}},
    ] + [_settings_event(model, effort, ts) for model, effort, ts in pairs])
    return path


def _appended(path, records):
    with open(path, "a", encoding="utf-8") as f:
        for record in records:
            f.write(json.dumps(record) + "\n")


def test_read_settings_events_requires_the_full_record_shape(tmp_path):
    watchdog = _watchdog()
    rollout = tmp_path / "rollout.jsonl"
    decoy = {
        # Same payload shape, wrong outer type: a nested/quoted copy of a settings event
        # (they appear inside compaction replays) must not move state.
        "type": "response_item",
        "timestamp": _old(),
        "payload": {
            "type": "thread_settings_applied",
            "thread_settings": {"model": "gpt-5.6-luna", "reasoning_effort": "low"},
        },
    }
    _write_jsonl(rollout, [decoy, _settings_event("gpt-5.6-sol", "xhigh", _old())])

    events, _consumed = watchdog.read_settings_events(rollout, 0)

    assert [event["label"] for event in events] == ["gpt-5.6-sol xhigh"]


def test_read_settings_events_starts_at_the_stored_offset(tmp_path):
    watchdog = _watchdog()
    rollout = _codex_rollout(tmp_path / "rollout.jsonl", [("gpt-5.6-sol", "xhigh", _old())])
    _first, consumed = watchdog.read_settings_events(rollout, 0)
    _appended(rollout, [_settings_event("gpt-5.6-luna", "low", _old())])

    events, _consumed = watchdog.read_settings_events(rollout, consumed)

    assert [event["label"] for event in events] == ["gpt-5.6-luna low"]


def test_read_settings_events_ignores_a_half_written_trailing_line(tmp_path):
    watchdog = _watchdog()
    rollout = _codex_rollout(tmp_path / "rollout.jsonl", [("gpt-5.6-sol", "xhigh", _old())])
    partial = json.dumps(_settings_event("gpt-5.6-luna", "low", _old()))[:-20]
    with open(rollout, "a", encoding="utf-8") as f:
        f.write(partial)

    events, consumed = watchdog.read_settings_events(rollout, 0)

    assert [event["label"] for event in events] == ["gpt-5.6-sol xhigh"]
    assert consumed == rollout.stat().st_size - len(partial)


def test_read_settings_events_reports_truncation(tmp_path):
    watchdog = _watchdog()
    rollout = _codex_rollout(tmp_path / "rollout.jsonl", [("gpt-5.6-sol", "xhigh", _old())])

    events, consumed = watchdog.read_settings_events(rollout, rollout.stat().st_size + 1)

    assert events is None and consumed is None


def test_baseline_read_is_bounded_and_escalates_to_the_whole_file(tmp_path, monkeypatch):
    watchdog = _watchdog()
    monkeypatch.setattr(watchdog, "CODEX_BASELINE_TAIL_BYTES", 2048)
    rollout = tmp_path / "rollout.jsonl"
    filler = json.dumps({"padding": "x" * 400}) + "\n"
    rollout.write_text(
        json.dumps(_settings_event("gpt-5.6-sol", "xhigh", _old())) + "\n" + filler * 40,
        encoding="utf-8",
    )

    # The only settings event sits outside the short tail, so an unbounded-read bug and a
    # never-escalate bug are both visible here.
    assert watchdog.read_settings_events(rollout, 0, 2048)[0] == []
    assert watchdog.baseline_codex(rollout)[0] == "gpt-5.6-sol xhigh"


def test_clamp_landing_on_a_line_boundary_keeps_the_first_record(tmp_path):
    watchdog = _watchdog()
    rollout = tmp_path / "rollout.jsonl"
    head = json.dumps({"type": "session_meta", "payload": {"cwd": "/home/user"}}) + "\n"
    wanted = json.dumps(_settings_event("gpt-5.6-luna", "low", _old())) + "\n"
    rollout.write_text(head + wanted, encoding="utf-8")

    # max_bytes chosen so the clamp lands exactly at the start of the settings record.
    events, _consumed = watchdog.read_settings_events(rollout, 0, len(wanted))

    assert [event["label"] for event in events] == ["gpt-5.6-luna low"]


def test_codex_first_run_baselines_without_alerting(tmp_path, monkeypatch):
    watchdog = _watchdog()
    rollout = _codex_rollout(tmp_path / "rollout.jsonl", [("gpt-5.6-sol", "xhigh", _old())])
    state = {}
    alerts = []
    monkeypatch.setattr(watchdog, "send_topic", lambda cfg, topic, text: alerts.append(text))
    monkeypatch.setattr(watchdog, "save_state", lambda current: None)

    watchdog.check_codex_rollout({}, 8048, "thread-1", rollout, state)

    assert alerts == []
    assert state["thread-1"]["last_model"] == "gpt-5.6-sol xhigh"
    assert state["thread-1"]["offset"] == rollout.stat().st_size


def test_codex_model_drift_alerts_once(tmp_path, monkeypatch):
    watchdog = _watchdog()
    rollout = _codex_rollout(tmp_path / "rollout.jsonl", [("gpt-5.6-sol", "xhigh", _old())])
    alerts = []
    monkeypatch.setattr(watchdog, "send_topic", lambda cfg, topic, text: alerts.append((topic, text)))
    monkeypatch.setattr(watchdog, "save_state", lambda current: None)
    state = {}
    watchdog.check_codex_rollout({}, 8048, "thread-1", rollout, state)

    _appended(rollout, [
        _settings_event("gpt-5.6-luna", "xhigh", "2026-07-31T21:08:32.023Z"),
        _settings_event("gpt-5.6-luna", "low", "2026-07-31T21:08:32.059Z"),
    ])
    watchdog.check_codex_rollout({}, 8048, "thread-1", rollout, state)
    watchdog.check_codex_rollout({}, 8048, "thread-1", rollout, state)

    assert len(alerts) == 1
    assert alerts[0][0] == 8048
    assert "gpt-5.6-sol xhigh → gpt-5.6-luna low" in alerts[0][1]
    assert "2026-08-01 00:08 UTC+3" in alerts[0][1]
    assert state["thread-1"]["last_model"] == "gpt-5.6-luna low"


def test_codex_effort_only_drift_alerts(tmp_path, monkeypatch):
    watchdog = _watchdog()
    rollout = _codex_rollout(tmp_path / "rollout.jsonl", [("gpt-5.6-sol", "xhigh", _old())])
    alerts = []
    monkeypatch.setattr(watchdog, "send_topic", lambda cfg, topic, text: alerts.append(text))
    monkeypatch.setattr(watchdog, "save_state", lambda current: None)
    state = {}
    watchdog.check_codex_rollout({}, 8048, "thread-1", rollout, state)

    _appended(rollout, [_settings_event("gpt-5.6-sol", "low", _old())])
    watchdog.check_codex_rollout({}, 8048, "thread-1", rollout, state)

    assert len(alerts) == 1
    assert "gpt-5.6-sol xhigh → gpt-5.6-sol low" in alerts[0]


def test_codex_flap_between_sweeps_is_reported(tmp_path, monkeypatch):
    watchdog = _watchdog()
    rollout = _codex_rollout(tmp_path / "rollout.jsonl", [("gpt-5.6-sol", "xhigh", _old())])
    alerts = []
    monkeypatch.setattr(watchdog, "send_topic", lambda cfg, topic, text: alerts.append(text))
    monkeypatch.setattr(watchdog, "save_state", lambda current: None)
    state = {}
    watchdog.check_codex_rollout({}, 8048, "thread-1", rollout, state)

    _appended(rollout, [
        _settings_event("gpt-5.6-luna", "low", _old(120)),
        _settings_event("gpt-5.6-sol", "xhigh", _old(60)),
    ])
    watchdog.check_codex_rollout({}, 8048, "thread-1", rollout, state)
    watchdog.check_codex_rollout({}, 8048, "thread-1", rollout, state)

    assert len(alerts) == 1
    assert "gpt-5.6-luna low" in alerts[0]
    assert "flipped and came back" in alerts[0]
    assert state["thread-1"]["last_model"] == "gpt-5.6-sol xhigh"


def test_codex_unchanged_settings_never_alert(tmp_path, monkeypatch):
    watchdog = _watchdog()
    rollout = _codex_rollout(tmp_path / "rollout.jsonl", [("gpt-5.6-sol", "xhigh", _old())])
    alerts = []
    monkeypatch.setattr(watchdog, "send_topic", lambda cfg, topic, text: alerts.append(text))
    monkeypatch.setattr(watchdog, "save_state", lambda current: None)
    state = {}
    watchdog.check_codex_rollout({}, 8048, "thread-1", rollout, state)

    # Codex re-applies identical settings at every turn start.
    _appended(rollout, [_settings_event("gpt-5.6-sol", "xhigh", _old(30)) for _ in range(3)])
    watchdog.check_codex_rollout({}, 8048, "thread-1", rollout, state)

    assert alerts == []


def test_codex_half_applied_switch_is_left_for_the_next_sweep(tmp_path, monkeypatch):
    watchdog = _watchdog()
    rollout = _codex_rollout(tmp_path / "rollout.jsonl", [("gpt-5.6-sol", "xhigh", _old())])
    alerts = []
    monkeypatch.setattr(watchdog, "send_topic", lambda cfg, topic, text: alerts.append(text))
    monkeypatch.setattr(watchdog, "save_state", lambda current: None)
    state = {}
    watchdog.check_codex_rollout({}, 8048, "thread-1", rollout, state)
    offset_before = state["thread-1"]["offset"]

    # Model already written, effort not yet — the ages are driven explicitly so the test
    # does not race the real settle window.
    ages = {"FRESH": 0.0}
    monkeypatch.setattr(watchdog, "_event_age", lambda ts: ages.get(ts, 3600.0))
    _appended(rollout, [_settings_event("gpt-5.6-luna", "xhigh", "FRESH")])
    watchdog.check_codex_rollout({}, 8048, "thread-1", rollout, state)

    assert alerts == []
    assert state["thread-1"]["offset"] == offset_before  # the burst is not consumed

    # Next sweep: the effort event has landed and both are old enough to act on.
    ages["FRESH"] = 3600.0
    _appended(rollout, [_settings_event("gpt-5.6-luna", "low", _old(60))])
    watchdog.check_codex_rollout({}, 8048, "thread-1", rollout, state)

    assert len(alerts) == 1
    assert "gpt-5.6-sol xhigh → gpt-5.6-luna low" in alerts[0]


def test_codex_rollout_replacement_rebaselines_instead_of_alerting(tmp_path, monkeypatch):
    watchdog = _watchdog()
    rollout = _codex_rollout(tmp_path / "rollout.jsonl",
                             [("gpt-5.6-sol", "xhigh", _old()) for _ in range(4)])
    alerts = []
    monkeypatch.setattr(watchdog, "send_topic", lambda cfg, topic, text: alerts.append(text))
    monkeypatch.setattr(watchdog, "save_state", lambda current: None)
    state = {}
    watchdog.check_codex_rollout({}, 8048, "thread-1", rollout, state)

    _codex_rollout(rollout, [("gpt-5.6-luna", "low", _old())])  # shorter file, same name
    watchdog.check_codex_rollout({}, 8048, "thread-1", rollout, state)

    assert alerts == []
    assert state["thread-1"]["last_model"] == "gpt-5.6-luna low"
    assert state["thread-1"]["offset"] == rollout.stat().st_size


def test_codex_possibly_delivered_alert_is_watermarked(tmp_path, monkeypatch):
    watchdog = _watchdog()
    rollout = _codex_rollout(tmp_path / "rollout.jsonl", [("gpt-5.6-sol", "xhigh", _old())])
    monkeypatch.setattr(watchdog, "save_state", lambda current: None)
    state = {}
    watchdog.check_codex_rollout({}, 8048, "thread-1", rollout, state)
    attempts = []

    def ambiguous_send(cfg, topic, text):
        attempts.append(text)
        raise PossiblyDelivered("Telegram may already have it")

    monkeypatch.setattr(watchdog, "send_topic", ambiguous_send)
    _appended(rollout, [_settings_event("gpt-5.6-luna", "low", _old())])
    watchdog.check_codex_rollout({}, 8048, "thread-1", rollout, state)
    watchdog.check_codex_rollout({}, 8048, "thread-1", rollout, state)

    assert len(attempts) == 1
    assert state["thread-1"]["last_model"] == "gpt-5.6-luna low"


def test_check_codex_keys_state_by_rollout_thread_uuid(tmp_path, monkeypatch):
    watchdog = _watchdog()
    uuid = "01900000-0000-7000-8000-000000000001"
    rollout = _codex_rollout(tmp_path / f"rollout-2026-07-31T12-32-35-{uuid}.jsonl",
                             [("gpt-5.6-sol", "xhigh", _old())])
    monkeypatch.setattr(watchdog, "codex_rollout_path", lambda pane, sid: str(rollout))
    monkeypatch.setattr(watchdog, "save_state", lambda current: None)
    state = {}

    watchdog.check_codex({}, 8048, {"session_id": uuid}, "%15", state)

    assert state[uuid]["last_model"] == "gpt-5.6-sol xhigh"


def test_check_codex_without_a_resolvable_rollout_does_nothing(monkeypatch):
    watchdog = _watchdog()
    monkeypatch.setattr(watchdog, "codex_rollout_path", lambda pane, sid: None)
    state = {}

    watchdog.check_codex({}, 8048, {"session_id": None}, "%15", state)

    assert state == {}


def test_codex_rollout_path_prefers_open_fd_over_registry_id(monkeypatch):
    watchdog = _watchdog()
    monkeypatch.setattr(watchdog, "pane_pid", lambda pane: 4242)
    monkeypatch.setattr(watchdog.codex_ctx, "rollout_for_session", lambda sid: "/by-id")
    monkeypatch.setattr(watchdog.codex_ctx, "open_rollout_for_pid_tree",
                        lambda pid: "/by-fd" if pid == 4242 else None)

    assert watchdog.codex_rollout_path("%15", "thread-1") == "/by-fd"

    monkeypatch.setattr(watchdog.codex_ctx, "open_rollout_for_pid_tree", lambda pid: None)
    assert watchdog.codex_rollout_path("%15", "thread-1") == "/by-id"


def test_future_timestamp_does_not_pin_the_offset(tmp_path, monkeypatch):
    watchdog = _watchdog()
    rollout = _codex_rollout(tmp_path / "rollout.jsonl", [("gpt-5.6-sol", "xhigh", _old())])
    alerts = []
    monkeypatch.setattr(watchdog, "send_topic", lambda cfg, topic, text: alerts.append(text))
    monkeypatch.setattr(watchdog, "save_state", lambda current: None)
    state = {}
    watchdog.check_codex_rollout({}, 8048, "thread-1", rollout, state)

    # A clock-skewed writer stamps the future; deferring it would silence this session
    # for good, so a negative age must count as settled.
    ahead = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    _appended(rollout, [_settings_event("gpt-5.6-luna", "low", ahead)])
    watchdog.check_codex_rollout({}, 8048, "thread-1", rollout, state)

    assert len(alerts) == 1
    assert state["thread-1"]["offset"] == rollout.stat().st_size


def test_settle_window_defers_the_whole_burst(monkeypatch):
    watchdog = _watchdog()
    # Model event just old enough, effort event just too fresh: processing them apart
    # would alert twice on one switch, so the pair must be deferred together.
    events = [
        {"start": 0, "ts": "old", "label": "gpt-5.6-sol xhigh"},
        {"start": 100, "ts": "settled-half", "label": "gpt-5.6-luna xhigh"},
        {"start": 200, "ts": "fresh-half", "label": "gpt-5.6-luna low"},
    ]
    ages = {"old": 900.0, "settled-half": 5.04, "fresh-half": 5.0 - 0.04}
    monkeypatch.setattr(watchdog, "_event_age", lambda ts: ages[ts])

    settled, cut = watchdog.settled_events(events)

    assert [event["label"] for event in settled] == ["gpt-5.6-sol xhigh"]
    assert cut == 100


def test_rollout_swapped_under_the_same_name_rebaselines(tmp_path, monkeypatch):
    watchdog = _watchdog()
    rollout = tmp_path / "rollout.jsonl"
    _codex_rollout(rollout, [("gpt-5.6-sol", "xhigh", _old())])
    alerts = []
    monkeypatch.setattr(watchdog, "send_topic", lambda cfg, topic, text: alerts.append(text))
    monkeypatch.setattr(watchdog, "save_state", lambda current: None)
    state = {}
    watchdog.check_codex_rollout({}, 8048, "thread-1", rollout, state)

    # A DIFFERENT inode at the same path, at least as long as the old offset: size alone
    # cannot see this, so the stored offset would point into unrelated bytes.
    replacement = tmp_path / "other.jsonl"
    _codex_rollout(replacement, [("gpt-5.6-luna", "low", _old()) for _ in range(6)])
    os.replace(replacement, rollout)
    watchdog.check_codex_rollout({}, 8048, "thread-1", rollout, state)

    assert alerts == []
    assert state["thread-1"]["last_model"] == "gpt-5.6-luna low"
    assert state["thread-1"]["file"] == watchdog.file_identity(rollout)
