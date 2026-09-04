"""A live Claude picker gets answered before the revive briefing can type into it."""

from bridge import daemon


ENTRY = {"name": "seat", "session_id": "SID", "cwd": "/", "engine": "claude"}


def _revive_harness(monkeypatch, screens):
    """Drive the new-pane path and record picker handling before the briefing."""
    answered = []
    briefings = []
    logs = []
    clock = [0]

    monkeypatch.setattr(daemon, "_tmux",
                        lambda *a, **k: type("R", (), {"returncode": 1, "stdout": ""})())
    monkeypatch.setattr(daemon, "launch_pane", lambda *a, **k: ("%77", None))
    monkeypatch.setattr(daemon, "_revive_tmux_name", lambda *a: "revive")
    monkeypatch.setattr(daemon, "last_model_and_effort_for_session",
                        lambda sid, cwd: ("claude-opus-5", None))
    monkeypatch.setattr(daemon, "read_registry", lambda: {})
    monkeypatch.setattr(daemon, "update_registry", lambda fn: None)
    monkeypatch.setattr(daemon, "reopen_topic", lambda *a: True)
    monkeypatch.setattr(daemon, "current_boot_id", lambda: "boot")
    monkeypatch.setattr(daemon, "reply", lambda *a: None)
    monkeypatch.setattr(daemon, "log", lambda message: logs.append(message))
    monkeypatch.setattr(daemon, "deliver_briefing",
                        lambda pane, *a, **k: briefings.append(pane))
    monkeypatch.setattr(daemon, "_safe_peek", lambda pane: next(screens))
    monkeypatch.setattr(daemon, "answer_resume_picker",
                        lambda pane, choice, **k: answered.append((pane, choice)) or "answered")
    monkeypatch.setattr(daemon.time, "time", lambda: clock[0])
    monkeypatch.setattr(daemon.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(daemon.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    return answered, briefings, logs


def test_live_picker_wait_uses_the_daemon_wall_clock_seam(monkeypatch):
    """A monotonic deadline ignored the virtual clock and made unrelated revive tests sleep."""
    clock = [0]
    monkeypatch.setattr(daemon, "_safe_peek", lambda pane: "")
    monkeypatch.setattr(daemon.time, "time", lambda: clock[0])
    monkeypatch.setattr(daemon.time, "sleep",
                        lambda seconds: clock.__setitem__(0, clock[0] + seconds))

    assert daemon._await_live_picker("%77", grace=3, poll=1) is False
    assert clock == [3]


def test_unpredicted_live_picker_on_second_poll_is_compacted_before_briefing(monkeypatch):
    answered, briefings, logs = _revive_harness(
        monkeypatch, iter(["", "Resume from summary\nResume full session as-is\nDon't ask me again"]))

    _status, task = daemon.revive_one({}, "302", dict(ENTRY))

    assert answered == [("%77", "compact")]
    assert briefings == ["%77"]
    assert task["resume_choice"] == "compact"
    assert logs == ["picker rendered on pane %77, no prediction — daemon chose compact"]


def test_unpredicted_absent_picker_times_out_before_briefing(monkeypatch):
    answered, briefings, _logs = _revive_harness(monkeypatch, iter([""] * 15))

    daemon.revive_one({}, "302", dict(ENTRY))

    assert answered == []
    assert briefings == ["%77"]


def test_predicted_picker_keeps_the_existing_answer_path(monkeypatch):
    """The predicted path remains covered by test_a_large_old_session_is_resumed_from_summary_on_a_mass_restore."""
    answered, briefings, _logs = _revive_harness(monkeypatch, iter(()))
    monkeypatch.setattr(daemon, "session_cost_sample",
                        lambda sid, cwd: (daemon.RESUME_MODAL_TOKENS + 1,
                                          daemon.RESUME_MODAL_AGE_MINUTES + 1))

    daemon.revive_one({}, "302", dict(ENTRY), auto_summary=True)

    assert answered == [("%77", "compact")]
    assert briefings == ["%77"]
