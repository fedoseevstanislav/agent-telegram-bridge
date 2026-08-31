"""The reopen notice must report what the bridge DID, not what the owner asked for (#235).

Two ways to get this wrong, and the second is the one a resolver-level test cannot see.

The first is tone. The owner closed the topic himself, reopened it himself, answered `compact`,
and was told "♻️ This session's terminal had died — relaunched and resumed." He read it as a
fault report and asked for an investigation into an incident that had not happened:

    "I know the topic was closed. What's the meaning of the message? Does it add anything?
     Only makes an impression that something went wrong. This is why I actually asked you."

The second is truth. When Claude's resume picker does not appear, the bridge already says so —
"the choice was not applied", "resuming with its FULL context" — and the notice underneath it
went on claiming "Resumed from a summary" anyway, because it was keyed on the REQUESTED choice.
Two contradictory messages, the second false, inside the feature that exists because a
misleading notice cost a real investigation. Found by the review, not by the tests that shipped
with the first attempt: those called `_restore_wording` directly and never crossed this seam.
These drive the real `revive_one`.
"""

import pytest

from bridge import daemon


@pytest.fixture
def capture_notices(monkeypatch, tmp_path):
    """Drive the real revive_one far enough to post, and collect what the owner would see."""
    posted = []

    monkeypatch.setattr(daemon, "launch_pane", lambda *a, **kw: ("%NEW", None))
    monkeypatch.setattr(daemon, "_tmux",
                        lambda *a, **kw: type("R", (), {"returncode": 1, "stdout": "", "stderr": ""})())
    monkeypatch.setattr(daemon, "log", lambda *a, **k: None)
    monkeypatch.setattr(daemon, "reply", lambda cfg, tid, text, *a, **kw: posted.append(text) or True)
    monkeypatch.setattr(daemon, "engine_of_pane", lambda pane: "claude")
    monkeypatch.setattr(daemon, "deliver_briefing", lambda *a, **kw: None)
    monkeypatch.setattr(daemon, "rebind_topic", lambda *a, **kw: None, raising=False)
    monkeypatch.setattr(daemon, "read_registry", lambda: {})
    monkeypatch.setattr(daemon, "update_registry", lambda *a, **kw: None)
    monkeypatch.setattr(daemon, "last_model_and_effort_for_session", lambda sid, cwd: ("m", "high"))
    monkeypatch.setattr(daemon, "_safe_peek", lambda pane: "")
    monkeypatch.setattr(daemon, "_resume_picker_present", lambda text: False)
    return posted


def _revive(entry_cwd, choice, picker_result, monkeypatch, posted):
    monkeypatch.setattr(daemon, "answer_resume_picker", lambda pane, c: picker_result)
    entry = {"engine": "claude", "session_id": "SID", "cwd": entry_cwd, "pane": "%OLD"}
    daemon.revive_one({}, "5935", entry, brief=False, cause="reopen",
                      resume_choice=choice, respect_close=True)
    return "\n".join(posted)


def test_an_applied_compact_is_reported_as_a_summary(tmp_path, monkeypatch, capture_notices):
    text = _revive(str(tmp_path), "compact", "answered", monkeypatch, capture_notices)

    assert "Resumed from a summary." in text
    assert "had died" not in text and "found dead" not in text


def test_an_applied_full_is_reported_as_full(tmp_path, monkeypatch, capture_notices):
    text = _revive(str(tmp_path), "full", "answered", monkeypatch, capture_notices)

    assert "Resumed in full." in text


@pytest.mark.parametrize("picker_result", ["absent", "failed"])
def test_an_unapplied_choice_is_never_reported_as_applied(tmp_path, monkeypatch,
                                                          capture_notices, picker_result):
    """The bridge has just told them the choice was NOT applied. Claiming it was, one message
    later, is the same false-report defect this whole change exists to remove."""
    text = _revive(str(tmp_path), "compact", picker_result, monkeypatch, capture_notices)

    assert "the choice was not applied" in text, "the loud warning must still fire"
    assert "from a summary" not in text, (
        "the notice claimed the requested choice was applied, immediately after the bridge "
        "reported that it was not:\n" + text
    )


def test_the_owner_is_still_told_something_happened(tmp_path, monkeypatch, capture_notices):
    """Degrading must not mean going silent — they waited through compaction for an answer."""
    text = _revive(str(tmp_path), "compact", "absent", monkeypatch, capture_notices)

    assert "Resumed on request." in text


# ---- the other door into the same revive -------------------------------------------------


@pytest.mark.parametrize("cause,expected", [(None, "auto"), ("reopen", "reopen")])
def test_maybe_auto_revive_forwards_its_cause(monkeypatch, cause, expected):
    """A reopened topic whose session needs no choice — codex always, claude below Claude's own
    thresholds — reaches `revive_one` through here instead. It used to hardcode `auto`, so those
    sessions were told their terminal "was found dead when a message arrived" when nothing had
    arrived: the same false trigger, through a different door (#236 review r1, Q2)."""
    seen = {}
    monkeypatch.setattr(daemon, "read_registry",
                        lambda: {"5935": {"engine": "codex", "session_id": "SID", "ended": "x"}})
    monkeypatch.setattr(daemon, "should_auto_revive", lambda entry: True)
    monkeypatch.setattr(daemon, "_reopen_needs_asking", lambda entry: False)
    monkeypatch.setattr(daemon, "log", lambda *a, **k: None)
    monkeypatch.setattr(daemon, "revive_one",
                        lambda cfg, tid, entry, **kw: (seen.update(kw) or ("resumed", None)))

    if cause is None:
        daemon.maybe_auto_revive({}, "5935")
    else:
        daemon.maybe_auto_revive({}, "5935", cause=cause)

    assert seen.get("cause") == expected
