"""#190 — a revived claude session must come back at the effort it was running at.

The model was already carried deliberately (`last_model_for_session`), the effort was not, so
every revive silently reset it to the machine default. Observed twice in two days: topic 1902
had run 352 consecutive Fable turns at `medium` and came back `xhigh`; topic 14886 did the
same. That is not cosmetic on a rate-limited model, and correcting it afterwards costs a full
context re-read (the `/effort` confirm dialog says so outright).

`last_model_for_session` cannot be extended to answer this. It reads `model-watchdog.json`,
where claude entries store the bare model (`"claude-fable-5"`) — only codex entries embed the
effort (`"gpt-5.6-sol medium"`), because `codex_label` joins the pair. The transcript is the
only place a claude session's effort is recorded.

The sharpest failure here is in the OTHER direction: inventing an effort for a model that
takes none. `claude-opus-4-8` records no `effort` at all, and passing `--effort` to it turns a
recoverable "wrong effort" into a revive that will not launch. So "unknown" must mean "omit
the flag", never "use the default".
"""

import json

import pytest

from bridge import daemon, transcript


def _write_transcript(tmp_path, monkeypatch, records, sid="SID", cwd="/home/user"):
    """Lay down a real transcript where transcript_path() will look for it."""
    monkeypatch.setattr(transcript, "PROJECTS_DIR", str(tmp_path))
    path = transcript.transcript_path(cwd, sid)
    import os
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r) + "\n")
    return path


def _turn(model, effort=None, ts="2026-08-22T09:54:20.000Z"):
    r = {"type": "assistant", "timestamp": ts, "message": {"model": model, "content": []}}
    if effort is not None:
        r["effort"] = effort
    return r


# ---- reading the effort back --------------------------------------------------

def test_the_recorded_effort_is_recovered(tmp_path, monkeypatch):
    _write_transcript(tmp_path, monkeypatch, [_turn("claude-fable-5", "medium")])
    assert daemon.last_effort_for_session("SID", "/home/user") == "medium"


def test_the_LAST_effort_wins_not_the_first(tmp_path, monkeypatch):
    # A session that was switched mid-life must resume where it ended up, not where it began.
    _write_transcript(tmp_path, monkeypatch, [
        _turn("claude-fable-5", "xhigh", "2026-08-22T09:00:00.000Z"),
        _turn("claude-fable-5", "high", "2026-08-22T09:30:00.000Z"),
        _turn("claude-fable-5", "medium", "2026-08-22T09:54:00.000Z"),
    ])
    assert daemon.last_effort_for_session("SID", "/home/user") == "medium"


def test_a_model_that_records_no_effort_yields_None(tmp_path, monkeypatch):
    # claude-opus-4-8 records no `effort`. Returning a default here would put `--effort` on a
    # model that rejects it, and the revive would not launch at all.
    _write_transcript(tmp_path, monkeypatch, [_turn("claude-opus-4-8")])
    assert daemon.last_effort_for_session("SID", "/home/user") is None


def test_a_missing_transcript_yields_None_and_does_not_raise(tmp_path, monkeypatch):
    monkeypatch.setattr(transcript, "PROJECTS_DIR", str(tmp_path))
    assert daemon.last_effort_for_session("no-such-session", "/home/user") is None


def test_an_unreadable_transcript_yields_None(tmp_path, monkeypatch):
    # A revive must survive a broken transcript — losing the effort is recoverable, refusing
    # to relaunch the session is not.
    _write_transcript(tmp_path, monkeypatch, [_turn("claude-fable-5", "medium")])

    def _boom(*a, **kw):
        raise OSError("disk gone")

    monkeypatch.setattr(transcript, "read_tail_records", _boom)
    assert daemon.last_effort_for_session("SID", "/home/user") is None


def test_an_unusable_effort_on_the_newest_turn_yields_None_not_a_stale_one(tmp_path, monkeypatch):
    """The flag is interpolated into a shell command, so only a real string counts. And when
    the newest turn's effort is unusable the answer is None — NOT the previous turn's value.
    Falling back to an older record is the same mistake as finding 7: it attaches an effort
    the current model may not accept. Omitting the flag is always recoverable."""
    _write_transcript(tmp_path, monkeypatch, [
        _turn("claude-fable-5", "medium", "2026-08-22T09:00:00.000Z"),
        {"type": "assistant", "timestamp": "2026-08-22T09:30:00.000Z",
         "effort": True, "message": {"model": "claude-fable-5", "content": []}},
    ])
    assert daemon.last_effort_for_session("SID", "/home/user") is None


# ---- the launch command -------------------------------------------------------

def test_the_flag_is_added_when_the_effort_is_known():
    cmd = daemon._resume_launch("claude", "SID", "claude-fable-5", "medium")
    assert "--effort medium" in cmd
    assert "--model claude-fable-5" in cmd and "--resume SID" in cmd


def test_the_flag_is_OMITTED_when_the_effort_is_unknown():
    cmd = daemon._resume_launch("claude", "SID", "claude-fable-5", None)
    assert "--effort" not in cmd, (
        "an unknown effort became a flag — on a model that takes none this is a dead revive"
    )


def test_codex_never_gets_an_effort_flag():
    # Codex carries model+effort in its own rollout and takes neither as a launch flag.
    cmd = daemon._resume_launch("codex", "SID", "gpt-5.6-sol", "medium")
    assert "--effort" not in cmd and cmd.startswith("codex ")


# ---- the call site: revive_one must actually use it ---------------------------

@pytest.fixture
def capture_launch(monkeypatch):
    """Drive the real revive_one and capture the command it hands to launch_pane."""
    seen = {}

    def _launch_pane(tmux_name, cwd, launch, engine, reason, prompt=None):
        seen["launch"] = launch
        seen["reason"] = reason
        return None, "stubbed"          # short-circuit: everything after is out of scope

    monkeypatch.setattr(daemon, "launch_pane", _launch_pane)
    monkeypatch.setattr(daemon, "_tmux",
                        lambda *a, **kw: type("R", (), {"returncode": 1, "stdout": "", "stderr": ""})())
    monkeypatch.setattr(daemon, "log", lambda *a, **k: None)
    return seen


def test_revive_passes_the_recorded_effort_through(tmp_path, monkeypatch, capture_launch):
    # The cwd must be a directory that EXISTS: revive_one silently falls back to ~ when it
    # is not (`if not os.path.isdir(cwd)`), and the transcript would then be looked up under
    # the wrong project dir. Hard-coding /home/user passed here and failed on CI, where that
    # path does not exist — the fallback made it look like the fix was unwired.
    _write_transcript(tmp_path, monkeypatch, [_turn("claude-fable-5", "medium")],
                      sid="SID", cwd=str(tmp_path))
    monkeypatch.setattr(daemon, "last_model_for_session", lambda sid: "claude-fable-5")
    entry = {"engine": "claude", "session_id": "SID", "cwd": str(tmp_path), "pane": "%1"}

    daemon.revive_one({}, "11722", entry)

    assert "--effort medium" in capture_launch["launch"], (
        "revive_one still drops the effort — the fix is not wired to the call site (#190)"
    )


def test_revive_of_a_no_effort_model_launches_without_the_flag(tmp_path, monkeypatch, capture_launch):
    _write_transcript(tmp_path, monkeypatch, [_turn("claude-opus-4-8")],
                      sid="SID", cwd=str(tmp_path))
    monkeypatch.setattr(daemon, "last_model_for_session", lambda sid: "claude-opus-4-8")
    entry = {"engine": "claude", "session_id": "SID", "cwd": str(tmp_path), "pane": "%1"}

    daemon.revive_one({}, "11722", entry)

    assert "--effort" not in capture_launch["launch"]


def test_a_codex_revive_does_not_consult_the_claude_transcript(tmp_path, monkeypatch, capture_launch):
    # Guards the `engine == "claude"` condition: codex has no claude transcript, and reading
    # one for it would be both wrong and a needless failure mode.
    def _must_not_run(*a, **kw):
        raise AssertionError("last_effort_for_session called for a codex revive")

    monkeypatch.setattr(daemon, "last_effort_for_session", _must_not_run)
    monkeypatch.setattr(daemon, "ensure_codex_trust", lambda cwd: None)
    entry = {"engine": "codex", "session_id": "SID", "cwd": str(tmp_path), "pane": "%1"}

    daemon.revive_one({}, "8713", entry)

    assert "--effort" not in capture_launch["launch"]


def test_an_older_models_effort_is_not_carried_to_a_no_effort_model(tmp_path, monkeypatch):
    """Codex review, finding 7. The session ran Fable at medium, then switched to Opus 4.8,
    which records no effort. Scanning for "any effort at all" returns medium and launches
    Opus with a flag it rejects — the revive dies, and the answer has already been consumed,
    so it lands in the dead-session/open-topic state this feature exists to remove."""
    _write_transcript(tmp_path, monkeypatch, [
        _turn("claude-fable-5", "medium", "2026-08-22T09:00:00.000Z"),
        _turn("claude-opus-4-8", None, "2026-08-22T10:00:00.000Z"),
    ])
    assert daemon.last_effort_for_session("SID", "/home/user") is None


def test_the_newest_models_effort_wins_over_an_older_ones(tmp_path, monkeypatch):
    _write_transcript(tmp_path, monkeypatch, [
        _turn("claude-opus-4-8", None, "2026-08-22T09:00:00.000Z"),
        _turn("claude-fable-5", "medium", "2026-08-22T10:00:00.000Z"),
    ])
    assert daemon.last_effort_for_session("SID", "/home/user") == "medium"


# ---- round 3, finding 4: model and effort must come from ONE record -------------

def test_a_stale_watchdog_model_never_beats_the_transcript(tmp_path, monkeypatch,
                                                           capture_launch):
    """The watchdog ticks every five minutes. A session that switched Fable -> Opus 4.8 and
    died inside that window left `model-watchdog.json` still saying Fable, while the
    transcript already said Opus. Reading the model from one source and the effort from the
    other resumed the model it no longer ran — and the two call-site tests above could not
    see it, because both stub last_model_for_session to agree with the transcript."""
    _write_transcript(tmp_path, monkeypatch, [
        _turn("claude-fable-5", "medium", "2026-08-22T09:00:00.000Z"),
        _turn("claude-opus-4-8", None, "2026-08-22T10:00:00.000Z"),
    ], sid="SID", cwd=str(tmp_path))
    monkeypatch.setattr(daemon, "last_model_for_session", lambda sid: "claude-fable-5")
    entry = {"engine": "claude", "session_id": "SID", "cwd": str(tmp_path), "pane": "%1"}

    daemon.revive_one({}, "11722", entry)

    assert "claude-opus-4-8" in capture_launch["launch"], (
        "resumed the stale watchdog model instead of the one the transcript last recorded"
    )
    assert "claude-fable-5" not in capture_launch["launch"]
    assert "--effort" not in capture_launch["launch"]


def test_the_watchdog_is_still_the_fallback_when_the_transcript_is_silent(tmp_path,
                                                                         monkeypatch,
                                                                         capture_launch):
    """The transcript is preferred, not required — but a watchdog model carries NO effort
    with it, because pairing an effort from one source with a model from another is the
    mismatch this stopped reading two sources to avoid."""
    monkeypatch.setattr(transcript, "PROJECTS_DIR", str(tmp_path / "empty"))
    monkeypatch.setattr(daemon, "last_model_for_session", lambda sid: "claude-fable-5")
    entry = {"engine": "claude", "session_id": "SID", "cwd": str(tmp_path), "pane": "%1"}

    daemon.revive_one({}, "11722", entry)

    assert "claude-fable-5" in capture_launch["launch"]
    assert "--effort" not in capture_launch["launch"]
