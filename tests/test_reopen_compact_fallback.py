"""A missing Claude resume picker falls back to one verified bridge `/compact`."""

import pytest

from bridge import daemon


ENTRY = {"name": "seat", "session_id": "SID", "cwd": "/srv/seat", "engine": "claude"}


def _revive_harness(monkeypatch, picker="absent"):
    """Launch a Claude pane without a real tmux server and collect owner-visible effects."""
    posted, briefings = [], []
    monkeypatch.setattr(daemon, "_tmux",
                        lambda *a, **k: type("R", (), {"returncode": 1, "stdout": ""})())
    monkeypatch.setattr(daemon, "launch_pane", lambda *a, **k: ("%77", None))
    monkeypatch.setattr(daemon, "_revive_tmux_name", lambda *a: "revive")
    monkeypatch.setattr(daemon, "last_model_and_effort_for_session", lambda *a: ("model", None))
    monkeypatch.setattr(daemon, "read_registry", lambda: {})
    monkeypatch.setattr(daemon, "update_registry", lambda fn: None)
    monkeypatch.setattr(daemon, "reopen_topic", lambda *a: True)
    monkeypatch.setattr(daemon, "current_boot_id", lambda: "boot")
    monkeypatch.setattr(daemon, "answer_resume_picker", lambda *a, **k: picker)
    monkeypatch.setattr(daemon, "reply", lambda cfg, tid, text: posted.append(text) or True)
    monkeypatch.setattr(daemon, "log", lambda *a, **k: None)
    monkeypatch.setattr(daemon, "deliver_briefing",
                        lambda pane, tid, engine, tpl, **kw: briefings.append((pane, kw)))
    return posted, briefings


def test_absent_picker_injects_only_after_sustained_idle_then_settles_before_briefing(monkeypatch):
    """C1: the fallback waits for the carry-forward idle streak before it injects."""
    posted, briefings = _revive_harness(monkeypatch)
    order = []

    monkeypatch.setattr(daemon, "pane_alive", lambda pane: True)
    monkeypatch.setattr(daemon, "_cf_busy",
                        lambda pane: order.append("capture: idle") and False)
    monkeypatch.setattr(daemon, "_cf_capture_tail",
                        lambda pane: order.append("capture: before") or "")
    monkeypatch.setattr(daemon, "type_line",
                        lambda pane, text, **kw: order.append(f"send: {text}") or "sent")
    monkeypatch.setattr(daemon, "_cf_compacting",
                        lambda pane: order.append("capture: compacting") or True)
    monkeypatch.setattr(daemon.time, "sleep", lambda seconds: None)

    daemon.revive_one({}, "302", dict(ENTRY), cause="reopen", resume_choice="compact")

    assert order == (["capture: idle"] * daemon.CF_IDLE_SAMPLES
                     + ["capture: before", "send: /compact", "capture: compacting"])
    assert briefings == [("%77", {"await_busy": True, "settle": daemon.COMPACT_SETTLE})]
    assert "bridge resumed the session and ran `/compact`" in "\n".join(posted)


def test_absent_picker_briefing_waits_for_compaction_to_settle(monkeypatch):
    """C1: idle → compacting → sustained idle precedes the actual briefing injection."""
    order = []
    registry = {"302": dict(ENTRY)}
    clock = [0]
    compacting = iter([True, True, False, False, False])

    monkeypatch.setattr(daemon, "_tmux",
                        lambda *a, **k: type("R", (), {"returncode": 1, "stdout": ""})())
    monkeypatch.setattr(daemon, "launch_pane", lambda *a, **k: ("%77", None))
    monkeypatch.setattr(daemon, "_revive_tmux_name", lambda *a: "revive")
    monkeypatch.setattr(daemon, "last_model_and_effort_for_session", lambda *a: ("model", None))
    monkeypatch.setattr(daemon, "read_registry", lambda: registry)
    monkeypatch.setattr(daemon, "update_registry", lambda fn: fn(registry))
    monkeypatch.setattr(daemon, "reopen_topic", lambda *a: True)
    monkeypatch.setattr(daemon, "current_boot_id", lambda: "boot")
    monkeypatch.setattr(daemon, "answer_resume_picker", lambda *a, **k: "absent")
    monkeypatch.setattr(daemon, "reply", lambda *a: True)
    monkeypatch.setattr(daemon, "log", lambda *a, **k: None)
    monkeypatch.setattr(daemon, "pane_alive", lambda pane: True)
    monkeypatch.setattr(daemon, "has_live_recv", lambda tid: False)
    monkeypatch.setattr(daemon, "_cf_capture_tail", lambda pane: "")
    monkeypatch.setattr(daemon, "_cf_hook_block_reason", lambda pane, before: None)
    monkeypatch.setattr(daemon, "_cf_compacting",
                        lambda pane: order.append("capture: compacting") or next(compacting))
    monkeypatch.setattr(daemon, "_cf_busy",
                        lambda pane: order.append("capture: idle") and False)
    monkeypatch.setattr(daemon.time, "time", lambda: clock[0])
    monkeypatch.setattr(daemon.time, "sleep",
                        lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    monkeypatch.setattr(daemon, "type_line",
                        lambda pane, text, **kw: order.append(f"send: {text}") or "sent")

    daemon.revive_one({}, "302", dict(ENTRY), cause="reopen", resume_choice="compact")

    assert order[0:daemon.CF_IDLE_SAMPLES] == ["capture: idle"] * daemon.CF_IDLE_SAMPLES
    assert order[daemon.CF_IDLE_SAMPLES:daemon.CF_IDLE_SAMPLES + 2] == [
        "send: /compact", "capture: compacting"]
    assert order.count("capture: compacting") == 5
    assert order[-1].startswith("send: ") and order[-1] != "send: /compact"
    assert order.index("send: /compact") < order.index("capture: compacting") < len(order) - 1


def test_present_picker_is_answered_without_bridge_injection(monkeypatch):
    """C2: the original picker route is untouched."""
    _posted, briefings = _revive_harness(monkeypatch, picker="answered")
    monkeypatch.setattr(daemon, "_compact_after_absent_picker",
                        lambda *a: (_ for _ in ()).throw(AssertionError("injected despite picker")))

    daemon.revive_one({}, "302", dict(ENTRY), cause="reopen", resume_choice="compact")

    assert briefings == [("%77", {"await_busy": True, "settle": daemon.COMPACT_SETTLE})]


def test_hook_refusal_reports_the_hook_words_and_does_not_inject_again(monkeypatch):
    """C3: a fresh PreCompact refusal ends the single fallback attempt."""
    posted, _briefings = _revive_harness(monkeypatch)
    sends = []
    monkeypatch.setattr(daemon, "pane_alive", lambda pane: True)
    monkeypatch.setattr(daemon, "pane_is_idle", lambda pane: True)
    monkeypatch.setattr(daemon, "_cf_capture_tail", lambda pane: "")
    monkeypatch.setattr(daemon, "type_line",
                        lambda pane, text, **kw: sends.append(text) or "sent")
    monkeypatch.setattr(daemon, "_cf_compacting", lambda pane: False)
    monkeypatch.setattr(daemon, "_cf_hook_block_reason",
                        lambda pane, before: "Compaction blocked by PreCompact hook: worktree dirty")

    daemon.revive_one({}, "302", dict(ENTRY), cause="reopen", resume_choice="compact")

    text = "\n".join(posted)
    assert sends == ["/compact"]
    assert "Compaction blocked by PreCompact hook: worktree dirty" in text
    assert "resuming in full" in text.lower()


def test_unreadable_pre_injection_tail_withholds_compaction(monkeypatch):
    """C3: no freshness proof means no `/compact` whose refusal could not be reported."""
    sends = []
    monkeypatch.setattr(daemon, "pane_alive", lambda pane: True)
    monkeypatch.setattr(daemon, "pane_is_idle", lambda pane: True)
    monkeypatch.setattr(daemon, "_cf_capture_tail", lambda pane: None)
    monkeypatch.setattr(daemon, "type_line",
                        lambda pane, text, **kw: sends.append(text) or "sent")

    assert daemon._compact_after_absent_picker("%77") == ("failed", None)
    assert sends == []


def test_absent_picker_requires_consecutive_idle_samples(monkeypatch):
    """C1: a busy sample RESETS the streak — pinned by how many samples are consumed.

    Asserting only the return value passes with the reset deleted and passes again with the
    old break-on-first-idle gate, because both still end in ("injected", None). The count is
    what distinguishes them: one busy sample in the middle means the streak must start over,
    so a run of CF_IDLE_SAMPLES idle samples has to follow it (review r2: the first version
    of this test exercised the branch without pinning anything that depended on it).
    """
    taken = []

    def sample(pane):
        taken.append(len(taken))
        # idle, then busy, then a full fresh streak.
        return False if len(taken) == 2 else True

    sent = []
    monkeypatch.setattr(daemon, "pane_alive", lambda pane: True)
    monkeypatch.setattr(daemon, "pane_is_idle", sample)
    monkeypatch.setattr(daemon, "_cf_capture_tail", lambda pane: "")
    monkeypatch.setattr(daemon, "type_line", lambda pane, text, **kw: sent.append(text) or "sent")
    monkeypatch.setattr(daemon, "_cf_compacting", lambda pane: True)
    monkeypatch.setattr(daemon.time, "sleep", lambda seconds: None)

    assert daemon._compact_after_absent_picker("%77") == ("injected", None)
    assert sent == ["/compact"]
    # 1 idle + 1 busy + CF_IDLE_SAMPLES idle. Without the reset the first idle would still
    # count and the injection would fire one sample earlier.
    assert len(taken) == 2 + daemon.CF_IDLE_SAMPLES


def test_batch_summary_separates_a_real_summary_resume_from_a_bridge_compaction(monkeypatch):
    """The boot/restore line must not report an injected fallback as a summary resume.

    The fallback re-reads the whole context and compacts afterwards; the summary line exists
    to say a large context was deliberately NOT re-read, so counting the two together states
    the opposite of what happened (review r2, remaining finding)."""
    sent = []
    monkeypatch.setattr(daemon, "api",
                        lambda token, method, payload: sent.append(payload.get("text", "")))
    monkeypatch.setattr(daemon, "save_boot_id", lambda boot: None)
    monkeypatch.setattr(daemon, "read_registry", lambda: {})
    monkeypatch.setattr(daemon, "deliver_briefing", lambda *a, **k: None)

    outcomes = {
        "301": {"from_summary": True, "bridge_compacted": False},
        "302": {"from_summary": False, "bridge_compacted": True},
    }

    def revive(cfg, tid, entry, **kwargs):
        task = {"pane": "%1", "tid": tid, "engine": "claude", "tpl": "brief",
                "needs_brief": False, "reopened": True, "resume_choice": "compact",
                "compacting": True}
        task.update(outcomes[tid])
        return "resumed", task

    monkeypatch.setattr(daemon, "revive_one", revive)
    daemon._restore_targets_now({"bot_token": "T", "chat_id": 1}, "boot",
                                [("301", {"name": "picker-seat"}), ("302", {"name": "fallback-seat"})])

    text = "\n".join(sent)
    assert "1 resumed from summary." in text
    assert "1 resumed in full, then compacted by the bridge." in text
    assert "from summary: picker-seat" in text
    assert "compacted after a full resume: fallback-seat" in text


def test_failed_compact_injection_clears_the_composer(monkeypatch):
    """C2: a withheld Enter cannot leave `/compact` for the briefing to submit later."""
    tmux_calls = []

    monkeypatch.setattr(daemon, "pane_alive", lambda pane: True)
    monkeypatch.setattr(daemon, "pane_is_idle", lambda pane: True)
    monkeypatch.setattr(daemon, "_cf_capture_tail", lambda pane: "")
    monkeypatch.setattr(daemon, "type_line", lambda pane, text, **kw: "failed")
    monkeypatch.setattr(daemon, "_tmux", lambda argv, **kw: tmux_calls.append(argv) or type(
        "R", (), {"returncode": 0})())

    assert daemon._compact_after_absent_picker("%77") == ("failed", None)
    assert tmux_calls == [["tmux", "send-keys", "-t", "%77", "C-u"]]


def test_auto_chosen_absent_picker_uses_the_same_injection_route(monkeypatch):
    """C5: automatic restore gets the same fallback when its picker is missing."""
    posted, briefings = _revive_harness(monkeypatch)
    injected = []
    monkeypatch.setattr(daemon, "session_cost_sample",
                        lambda *a: (daemon.RESUME_MODAL_TOKENS + 1,
                                    daemon.RESUME_MODAL_AGE_MINUTES + 1))
    monkeypatch.setattr(daemon, "_compact_after_absent_picker",
                        lambda pane: injected.append(pane) or ("injected", None))

    daemon.revive_one({}, "302", dict(ENTRY), cause="boot", auto_summary=True)

    assert injected == ["%77"]
    assert briefings == [("%77", {"await_busy": True, "settle": daemon.COMPACT_SETTLE})]
    assert "bridge resumed the session and ran `/compact`" in "\n".join(posted)


def test_mass_restore_keeps_the_compaction_settle_wait(monkeypatch):
    """C5: deferred auto restoration must not lose the compacting briefing gate."""
    delivered = []
    monkeypatch.setattr(daemon, "save_boot_id", lambda boot: None)
    monkeypatch.setattr(daemon, "api", lambda *a, **k: {})
    monkeypatch.setattr(daemon, "revive_one", lambda *a, **k: (
        "resumed", {"pane": "%77", "tid": "302", "engine": "claude", "tpl": "brief",
                    "needs_brief": True, "reopened": True, "resume_choice": "compact",
                    "compacting": True}))
    monkeypatch.setattr(daemon, "deliver_briefing",
                        lambda pane, tid, engine, tpl, **kw: delivered.append((pane, kw)))

    class Thread:
        def __init__(self, target=None, args=(), kwargs=None, daemon=None):
            self.target, self.args, self.kwargs = target, args, kwargs or {}

        def start(self):
            self.target(*self.args, **self.kwargs)

    monkeypatch.setattr(daemon.threading, "Thread", Thread)
    daemon._restore_targets_now({"bot_token": "T", "chat_id": 1}, "boot",
                                [("302", {"name": "seat"})])

    assert delivered == [("%77", {"await_busy": True, "settle": daemon.COMPACT_SETTLE})]


def test_direct_picker_failure_keeps_the_full_context_notice(monkeypatch):
    """C4: only an actual picker failure uses the existing FULL-context report."""
    posted, _briefings = _revive_harness(monkeypatch, picker="failed")

    daemon.revive_one({}, "302", dict(ENTRY), cause="reopen", resume_choice="compact")

    assert "The session is resuming with its FULL context" in "\n".join(posted)


@pytest.mark.parametrize("picker_left_up", [False, True])
def test_injection_failure_reports_the_actual_state_or_an_unanswered_picker(
        monkeypatch, picker_left_up):
    """C4: a failed fallback reports its own outcome, never a summary resume."""
    posted, _briefings = _revive_harness(monkeypatch)
    monkeypatch.setattr(daemon, "_compact_after_absent_picker", lambda pane: ("failed", None))
    monkeypatch.setattr(daemon, "_resume_picker_present", lambda screen: picker_left_up)

    daemon.revive_one({}, "302", dict(ENTRY), cause="reopen", resume_choice="compact")

    if picker_left_up:
        assert daemon.UNANSWERED_PICKER_NOTICE in posted
    else:
        assert any("could not inject /compact" in message for message in posted)
        assert all("from a summary" not in message for message in posted)


def test_injected_fallback_gets_its_own_final_notice(monkeypatch):
    """C6: `/compact` after resume is not reported as Claude's summary picker path."""
    posted, _briefings = _revive_harness(monkeypatch)
    monkeypatch.setattr(daemon, "_compact_after_absent_picker", lambda pane: ("injected", None))

    daemon.revive_one({}, "302", dict(ENTRY), cause="reopen", resume_choice="compact")

    assert "♻️ Resumed, then compacted by the bridge." in posted
