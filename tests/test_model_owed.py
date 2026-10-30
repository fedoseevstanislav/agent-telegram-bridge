"""A failed switch back after a compaction is owed, and the next revive pays it.

2026-10-23: a halt's Escape put the flow's queued /compact back in the composer, the
switch back to Fable was typed after it and went out as `/compact /model …`, and the restore
was reported failed. Nothing retried it, and every revive since took the transcript's newest
model, Sonnet, so the session ran on Sonnet for five days.
"""

import json
import os

import pytest

from bridge import common, daemon, transcript


@pytest.fixture
def registry():
    common.update_registry(lambda reg: (reg.clear(), reg.update(
        {"7005": {"engine": "claude", "session_id": "SID", "pane": "%1"}})))
    yield
    common.update_registry(lambda reg: reg.clear())


def _entry():
    return common.read_registry()["7005"]


def _plan():
    return {"model": "claude-fable-5-1[1m]", "effort": "xhigh", "tpath": "/nonexistent",
            "now": {"model": "claude-sonnet-5-5[1m]", "effort": "xhigh"}, "sent": None}


def _guard():
    import contextlib

    @contextlib.contextmanager
    def guard():
        yield True
    return guard


@pytest.fixture
def quiet(monkeypatch):
    monkeypatch.setattr(daemon, "log", lambda *a, **k: None)
    monkeypatch.setattr(daemon, "_reply_quietly", lambda tid, text: None)


def test_a_failed_restore_is_recorded_as_owed(registry, quiet, monkeypatch):
    monkeypatch.setattr(daemon, "_run_model_steps", lambda *a: ("failed", True))
    daemon.restore_after_compaction("%1", "7005", _plan(), _guard(), halted=True)
    assert _entry()["model_owed"] == {"model": "claude-fable-5-1[1m]", "effort": "xhigh"}


def test_a_confirmed_restore_clears_what_was_owed(registry, quiet, monkeypatch):
    common.update_registry(lambda reg: reg["7005"].update(
        model_owed={"model": "claude-fable-5-1[1m]", "effort": "xhigh"}))
    monkeypatch.setattr(daemon, "_run_model_steps", lambda *a: ("ok", True))
    daemon.restore_after_compaction("%1", "7005", _plan(), _guard())
    assert "model_owed" not in _entry()


def test_an_aborted_restore_leaves_the_record_alone(registry, quiet, monkeypatch):
    common.update_registry(lambda reg: reg["7005"].update(
        model_owed={"model": "claude-fable-5-1[1m]", "effort": "xhigh"}))
    monkeypatch.setattr(daemon, "_run_model_steps", lambda *a: ("aborted", True))
    daemon.restore_after_compaction("%1", "7005", _plan(), _guard())
    assert _entry()["model_owed"]["model"] == "claude-fable-5-1[1m]"


def test_a_topic_not_in_the_registry_is_not_created(registry, quiet):
    daemon._set_model_owed("9999", {"model": "m", "effort": "e"})
    assert "9999" not in common.read_registry()


def test_a_model_command_asks_type_line_to_clear_the_composer_first(quiet, monkeypatch):
    seen = {}
    monkeypatch.setattr(daemon, "type_line",
                        lambda pane, text, **kw: (seen.update(kw), "failed")[1])
    monkeypatch.setattr(daemon, "_clear_composer", lambda pane: None)
    monkeypatch.setattr(daemon.transcript, "cursor", lambda tpath: 0)
    daemon._model_cmd("%1", "/model claude-fable-5-1[1m]", "Set model to ", "/x", _guard(),
                      _plan())
    assert seen.get("clear_first") is True


def test_type_line_clears_inside_the_pane_lock_before_typing(monkeypatch):
    import contextlib
    events = []

    @contextlib.contextmanager
    def _lock(pane):
        events.append("lock")
        yield
        events.append("unlock")

    def _tmux(argv, **kw):
        if "send-keys" in argv:
            events.append("C-u" if "C-u" in argv else "keys")
            raise RuntimeError("stop after the first write")
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(daemon, "_pane_lock", _lock)
    monkeypatch.setattr(daemon, "_tmux", _tmux)
    monkeypatch.setattr(daemon, "log", lambda *a, **k: None)
    assert daemon.type_line("%1", "/model x", clear_first=True) == "failed"
    assert events == ["lock", "C-u", "unlock"]


# ---- revive pays the debt -------------------------------------------------------

def _write_transcript(tmp_path, monkeypatch, model, effort, sid="SID"):
    monkeypatch.setattr(transcript, "PROJECTS_DIR", str(tmp_path))
    path = transcript.transcript_path(str(tmp_path), sid)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"type": "assistant", "timestamp": "2026-10-23T23:45:00.000Z",
                             "effort": effort, "message": {"model": model, "content": []}})
                 + "\n")


class _Stop(Exception):
    pass


@pytest.fixture
def capture_launch(monkeypatch):
    seen = {}

    def _launch_pane(tmux_name, cwd, launch, engine, reason, prompt=None):
        seen["launch"] = launch
        return seen.get("pane"), (None if seen.get("pane") else "stubbed")

    def _sleep(secs):
        if seen.get("pane"):
            raise _Stop()  # everything after the launch is out of scope

    monkeypatch.setattr(daemon, "launch_pane", _launch_pane)
    monkeypatch.setattr(daemon.time, "sleep", _sleep)
    monkeypatch.setattr(daemon, "_tmux",
                        lambda *a, **kw: type("R", (), {"returncode": 1, "stdout": "",
                                                        "stderr": ""})())
    monkeypatch.setattr(daemon, "log", lambda *a, **k: None)
    return seen


def test_revive_launches_on_the_owed_pair_not_the_transcript(tmp_path, monkeypatch,
                                                             capture_launch):
    _write_transcript(tmp_path, monkeypatch, "claude-sonnet-5-5", "xhigh")
    entry = {"engine": "claude", "session_id": "SID", "cwd": str(tmp_path), "pane": "%1",
             "model_owed": {"model": "claude-fable-5-1[1m]", "effort": "xhigh"}}
    daemon.revive_one({}, "7005", entry)
    assert "claude-fable-5-1" in capture_launch["launch"]
    assert "sonnet" not in capture_launch["launch"]
    assert "--effort xhigh" in capture_launch["launch"]


def test_without_a_debt_revive_still_follows_the_transcript(tmp_path, monkeypatch,
                                                            capture_launch):
    _write_transcript(tmp_path, monkeypatch, "claude-sonnet-5-5", "high")
    entry = {"engine": "claude", "session_id": "SID", "cwd": str(tmp_path), "pane": "%1"}
    daemon.revive_one({}, "7005", entry)
    assert "claude-sonnet-5-5" in capture_launch["launch"]
    assert "--effort high" in capture_launch["launch"]


@pytest.mark.parametrize("owed", [{"model": "", "effort": "xhigh"}, {"model": "m"},
                                  "claude-fable-5-1", {"model": 3, "effort": "xhigh"}])
def test_a_malformed_debt_is_ignored(tmp_path, monkeypatch, capture_launch, owed):
    _write_transcript(tmp_path, monkeypatch, "claude-sonnet-5-5", "high")
    entry = {"engine": "claude", "session_id": "SID", "cwd": str(tmp_path), "pane": "%1",
             "model_owed": owed}
    daemon.revive_one({}, "7005", entry)
    assert "claude-sonnet-5-5" in capture_launch["launch"]


def test_a_launch_alone_does_not_clear_the_debt(tmp_path, monkeypatch, registry,
                                                capture_launch):
    _write_transcript(tmp_path, monkeypatch, "claude-sonnet-5-5", "xhigh")
    common.update_registry(lambda reg: reg["7005"].update(
        cwd=str(tmp_path), model_owed={"model": "claude-fable-5-1[1m]", "effort": "xhigh"}))
    capture_launch["pane"] = "%9"
    try:
        daemon.revive_one({}, "7005", dict(_entry()))
    except _Stop:
        pass
    assert "claude-fable-5-1" in capture_launch["launch"]
    assert _entry()["model_owed"]["model"] == "claude-fable-5-1[1m]"


def test_a_reply_on_the_owed_model_pays_the_debt(tmp_path, monkeypatch, registry,
                                                capture_launch):
    _write_transcript(tmp_path, monkeypatch, "claude-fable-5-1", "medium")
    common.update_registry(lambda reg: reg["7005"].update(
        cwd=str(tmp_path), model_owed={"model": "claude-fable-5-1[1m]", "effort": "xhigh"}))
    daemon.revive_one({}, "7005", dict(_entry()))
    assert "--effort medium" in capture_launch["launch"]  # the session's own newer record
    assert "model_owed" not in _entry()


def test_the_next_carry_forward_switches_back_to_the_owed_pair(registry, quiet, monkeypatch):
    common.update_registry(lambda reg: reg["7005"].update(
        cwd="/srv/seat", model_owed={"model": "claude-fable-5-1[1m]", "effort": "xhigh"}))
    seen = {}
    monkeypatch.setattr(daemon, "compaction_plan",
                        lambda sid, cwd, pane, model=None, effort=None:
                        seen.update(model=model, effort=effort))
    daemon._cf_compaction_plan("7005", "tok", "%1")
    assert seen == {"model": "claude-fable-5-1[1m]", "effort": "xhigh"}


def test_a_halt_overtaken_by_a_new_flow_still_records_the_debt(registry, quiet, monkeypatch):
    import contextlib

    @contextlib.contextmanager
    def _stopped():
        yield False

    monkeypatch.setattr(daemon, "_halt_guard", lambda tid, pane: _stopped)
    monkeypatch.setattr(daemon, "pane_alive", lambda pane: True)
    monkeypatch.setitem(daemon._pending_cf, "7005", {"token": "new"})
    daemon._after_halt("7005", "%1", _plan())
    assert _entry()["model_owed"] == {"model": "claude-fable-5-1[1m]", "effort": "xhigh"}


def test_a_withheld_model_line_is_cleared_while_the_guard_is_held(quiet, monkeypatch):
    import contextlib
    events = []

    @contextlib.contextmanager
    def guard():
        events.append("enter")
        yield True
        events.append("exit")

    monkeypatch.setattr(daemon, "_clear_composer", lambda pane: events.append("clear"))
    monkeypatch.setattr(daemon, "type_line", lambda pane, text, **kw: "swallowed")
    monkeypatch.setattr(daemon.transcript, "cursor", lambda tpath: 0)
    daemon._model_cmd("%1", "/model x", "Set model to ", "/x", lambda: guard(), _plan())
    assert events == ["enter", "clear", "exit"]


def test_a_fresh_revive_drops_the_old_sessions_debt(tmp_path, monkeypatch, registry,
                                                   capture_launch):
    common.update_registry(lambda reg: reg["7005"].update(
        cwd=str(tmp_path), model_owed={"model": "claude-fable-5-1[1m]", "effort": "xhigh"}))
    daemon.revive_one({}, "7005", dict(_entry()), fresh=True)
    assert "model_owed" not in _entry()
