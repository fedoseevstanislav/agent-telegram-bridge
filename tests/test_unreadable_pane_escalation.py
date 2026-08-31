"""#188 — a pane the sweep cannot read must not stay silent forever.

`type_line` returns "sent" / "swallowed" / "failed". The sweep reported the modal case
("swallowed") and logged the unreadable case ("failed"), then retried it on every cycle with
no exit. But "failed" is not always transient: it is what comes back when `_echo_capture`
cannot read the pane or when the pane lock is held. For a wedged pane that meant the session
never got nudged, the retry never stopped, and the topic was never told — quieter than the
modal case that IS reported, which is the exact silence #133 exists to close.

`deliver_briefing` already solved this shape on its final attempt: bound the attempts, then
force one report past the cooldown so giving up is never silent. The sweep now does the same
after `UNREADABLE_ESCALATE_AFTER` consecutive failures.

These drive the REAL `idle_sweep_loop` body rather than a re-implementation of its decision.
That distinction has bitten this repo before: tests that exercised a factored-out helper
left three mutations inside the loop alive. `time.sleep` at the bottom of the loop is the
seam — it sits outside the per-iteration try/except, so raising there ends the loop after
exactly one sweep.
"""

import time

import pytest

from bridge import daemon


class _OneSweep(Exception):
    """Raised from the loop's trailing sleep to end it after a single iteration."""


TID = "4367"
PANE = "%7"


@pytest.fixture
def sweep(monkeypatch):
    """Drive one real `idle_sweep_loop` iteration over a single dark claude topic.

    Everything the loop reads is stubbed except the branch under test. `status` selects what
    `type_line` returns; `reports` collects the `report_blocked_pane` calls.
    """
    reports = []
    state = {"status": "failed"}

    monkeypatch.setattr(daemon, "read_registry",
                        lambda: {TID: {"pane": PANE, "session_id": "s"}})
    monkeypatch.setattr(daemon, "fleet_panes", lambda: [(PANE, "sess", "node", "claude")])
    monkeypatch.setattr(daemon, "pane_alive", lambda p: True)
    monkeypatch.setattr(daemon, "unread_count", lambda t: 2)
    monkeypatch.setattr(daemon, "sweep_nudge_text", lambda t, f, now: "nudge")
    monkeypatch.setattr(daemon, "log", lambda *a, **k: None)
    monkeypatch.setattr(daemon, "_prune_pane_tables", lambda: None)
    monkeypatch.setattr(daemon, "type_line", lambda pane, text, **kw: state["status"])
    monkeypatch.setattr(
        daemon, "report_blocked_pane",
        lambda tid, pane, what, lead=daemon.MODAL_LEAD: reports.append(
            {"tid": str(tid), "what": what, "lead": lead}) or True,
    )

    def _sleep(_secs):
        raise _OneSweep

    monkeypatch.setattr(daemon.time, "sleep", _sleep)

    # Fresh module-level tables per test; monkeypatch restores the originals afterwards.
    for name in ("_dark_since", "_last_sweep_nudge", "_unreadable_streak", "_blocked_reported",
                 "_swallowed_streak"):
        monkeypatch.setattr(daemon, name, {})

    def _run(status="failed", sweeps=1):
        state["status"] = status
        for _ in range(sweeps):
            # Past the grace window and the re-nudge cooldown, so the loop reaches the nudge.
            daemon._dark_since[TID] = time.time() - daemon.SWEEP_GRACE - 1
            daemon._last_sweep_nudge.pop(TID, None)
            with pytest.raises(_OneSweep):
                daemon.idle_sweep_loop({})
        return reports

    return _run


# ---- the escalation ----------------------------------------------------------

def test_the_first_unreadable_sweep_says_nothing(sweep):
    # A single unreadable capture during a repaint is ordinary; the retry is the right
    # answer. Reporting on sight would turn every transient into a message.
    assert sweep("failed", sweeps=1) == []


def test_it_stays_quiet_until_the_threshold(sweep):
    assert sweep("failed", sweeps=daemon.UNREADABLE_ESCALATE_AFTER - 1) == []


def test_a_sustained_unreadable_pane_is_finally_surfaced(sweep):
    reports = sweep("failed", sweeps=daemon.UNREADABLE_ESCALATE_AFTER)
    assert len(reports) == 1, "the pane stayed unreadable and the topic was never told (#188)"
    assert reports[0]["tid"] == TID


def test_it_reports_once_per_streak_not_once_per_sweep(sweep):
    # The condition persists; the loop keeps retrying. One message is the point — a report
    # every 60s would be the message loop report_blocked_pane's cooldown exists to prevent.
    reports = sweep("failed", sweeps=daemon.UNREADABLE_ESCALATE_AFTER + 5)
    assert len(reports) == 1


def test_the_report_does_not_tell_the_owner_to_answer_a_prompt(sweep):
    # There is no prompt in this case. The modal copy would send them looking for something
    # that isn't there, and it names a risk ("I won't press Enter") that does not apply.
    lead = sweep("failed", sweeps=daemon.UNREADABLE_ESCALATE_AFTER)[0]["lead"]
    assert lead == daemon.UNREADABLE_LEAD
    assert lead != daemon.MODAL_LEAD
    assert "waiting on a prompt" not in lead
    assert "can't be read" in lead


def test_the_report_still_names_what_went_undelivered(sweep):
    assert sweep("failed", sweeps=daemon.UNREADABLE_ESCALATE_AFTER)[0]["what"] == (
        "2 undrained message(s)")


# ---- the streak resets -------------------------------------------------------

def test_a_delivered_nudge_clears_the_streak(sweep):
    sweep("failed", sweeps=daemon.UNREADABLE_ESCALATE_AFTER - 1)
    sweep("sent", sweeps=1)
    assert daemon._unreadable_streak.get(TID) is None
    # The counter really restarted: one more failure must not trip the threshold.
    assert sweep("failed", sweeps=daemon.UNREADABLE_ESCALATE_AFTER - 1) == []


def test_a_swallowed_nudge_clears_the_streak_and_reports_as_a_modal(sweep):
    # type_line is stubbed here, so the swallow streak it normally keeps has to be stated:
    # this is the pane that has already swallowed enough to be worth reporting (#254).
    sweep("failed", sweeps=daemon.UNREADABLE_ESCALATE_AFTER - 1)
    daemon._swallowed_streak[PANE] = (daemon.SWALLOW_MAX_ATTEMPTS, 0.0)
    reports = sweep("swallowed", sweeps=1)
    assert daemon._unreadable_streak.get(TID) is None
    assert len(reports) == 1
    assert reports[0]["lead"] == daemon.MODAL_LEAD, (
        "a modal is a different condition and keeps its own copy")


def test_a_single_swallow_reports_nothing(sweep):
    """#254. The unreadable streak is still cleared — a swallow is not an unreadable pane —
    but the owner hears nothing until the pane proves it is actually stuck. Most single
    swallows are a late render that the next tick delivers."""
    sweep("failed", sweeps=daemon.UNREADABLE_ESCALATE_AFTER - 1)
    assert not daemon.pane_is_persistently_swallowing(PANE)
    reports = sweep("swallowed", sweeps=1)
    assert daemon._unreadable_streak.get(TID) is None
    assert reports == []


# ---- the surrounding contract the fix must not break -------------------------

def test_a_failed_nudge_still_does_not_buy_the_cooldown(sweep):
    # #133: nothing was delivered, so the next sweep must retry rather than sit out
    # SWEEP_COOLDOWN. Escalating must not start stamping _last_sweep_nudge as a side effect.
    sweep("failed", sweeps=daemon.UNREADABLE_ESCALATE_AFTER)
    assert TID not in daemon._last_sweep_nudge


def test_a_sent_nudge_still_takes_the_cooldown(sweep):
    sweep("sent", sweeps=1)
    assert TID in daemon._last_sweep_nudge


def test_the_escalation_forces_past_the_blocked_report_cooldown(sweep):
    # A modal report minutes earlier must not swallow the unreadable-pane report: they are
    # different conditions, and this is the message that says the sweep has given up.
    daemon._blocked_reported[TID] = time.time()
    assert len(sweep("failed", sweeps=daemon.UNREADABLE_ESCALATE_AFTER)) == 1
    assert TID not in daemon._blocked_reported


# ---- the report itself carries the lead it was given -------------------------

def test_the_lead_the_caller_passes_is_what_reaches_the_topic(monkeypatch):
    # The tests above stub report_blocked_pane, so they pin the ARGUMENT and not its use.
    # Without this, a body that ignored `lead` and always printed the modal copy would pass
    # every one of them.
    monkeypatch.setattr(daemon, "peek_pane", lambda _p, lines=12: "some terminal")
    monkeypatch.setattr(daemon, "load_config", lambda: {"bot_token": "t", "chat_id": "c"})
    monkeypatch.setattr(daemon, "_blocked_reported", {})
    sent = []
    monkeypatch.setattr(daemon, "reply",
                        lambda cfg, tid, text: bool(sent.append(text)) or True)

    assert daemon.report_blocked_pane(33, "%1", "a message", lead=daemon.UNREADABLE_LEAD)
    assert daemon.UNREADABLE_LEAD in sent[0]
    assert "waiting on a prompt" not in sent[0]
    assert "a message" in sent[0] and "some terminal" in sent[0]


def test_the_default_lead_is_still_the_modal_copy(monkeypatch):
    # Every existing caller relies on the default; changing it would silently reword the
    # #133 escalation that has been in production since.
    monkeypatch.setattr(daemon, "peek_pane", lambda _p, lines=12: "picker")
    monkeypatch.setattr(daemon, "load_config", lambda: {"bot_token": "t", "chat_id": "c"})
    monkeypatch.setattr(daemon, "_blocked_reported", {})
    sent = []
    monkeypatch.setattr(daemon, "reply",
                        lambda cfg, tid, text: bool(sent.append(text)) or True)

    assert daemon.report_blocked_pane(33, "%1", "a message")
    assert daemon.MODAL_LEAD in sent[0]
    assert "waiting on a prompt" in sent[0]
