"""A pane busy for the whole settle window must still end up briefed or reported (#172).

`deliver_briefing` waits up to RESTORE_SETTLE for a claude pane to go idle. When it never
does, the function used to `return` having stamped nothing, scheduled nothing and escalated
nothing, under a log line saying it was "leaving for idle_sweep". `idle_sweep_loop` never
calls `deliver_briefing`: it injects `sweep_nudge_text`, which carries no topic id, no re-arm
instruction and no restore cause, and for codex it only fires when unread > 0. So a session
revived into a long turn was never told it had been revived, never re-armed its listener, and
went silent to the owner until something else happened to nudge it.

The fix routes that path — and the two sibling give-up points — through one exhaustion
function, so the answer to "this attempt could not brief the pane, now what?" is written
once. The tests below pin each give-up point to that shared answer rather than to a copy of
it, because the bug was precisely that the three copies disagreed.
"""

import re

import pytest

from bridge import daemon


class _Timer:
    """A faithful stand-in for threading.Timer: it accepts `kwargs`, which the real one does
    and which a retry that must keep waiting for compaction depends on."""

    def __init__(self, delay, fn, args=(), kwargs=None):
        self.delay, self.fn, self.args, self.kwargs = delay, fn, args, kwargs or {}

    def start(self):
        pass


@pytest.fixture
def briefing(monkeypatch):
    """A claude pane on topic 4242 that is alive, bound, and NEVER idle."""
    state = {"timers": [], "blocked": [], "typed": [], "marked": [], "alive": True}
    # A real settle window with no real waiting: the loop still runs its idle probe (which a
    # window of 0 would skip entirely), but the busy case reaches its deadline at once.
    monkeypatch.setattr(daemon, "RESTORE_SETTLE", 0.05)
    monkeypatch.setattr(daemon.time, "sleep", lambda _s: None)
    monkeypatch.setattr(daemon, "pane_alive", lambda _p: state["alive"])
    monkeypatch.setattr(daemon, "pane_is_idle", lambda _p: False)
    monkeypatch.setattr(daemon, "has_live_recv", lambda _t: False)
    monkeypatch.setattr(daemon, "read_registry", lambda: {"4242": {"pane": "%7"}})
    monkeypatch.setattr(daemon, "current_boot_id", lambda: "boot-1")
    monkeypatch.setattr(daemon, "update_registry", lambda fn: state["marked"].append(fn))
    monkeypatch.setattr(daemon, "type_line",
                        lambda *a, **k: state["typed"].append(a) or "sent")
    monkeypatch.setattr(daemon, "report_blocked_pane",
                        lambda tid, pane, what: state["blocked"].append((tid, pane, what)))

    def _timer(delay, fn, args=(), kwargs=None):
        t = _Timer(delay, fn, args, kwargs)
        state["timers"].append(t)
        return t

    monkeypatch.setattr(daemon.threading, "Timer", _timer)
    return state


# ---- C1: a busy pane retries instead of being dropped -----------------------------------

def test_settle_timeout_schedules_the_bounded_retry(briefing):
    daemon.deliver_briefing("%7", "4242", "claude", "brief {tid}")

    assert briefing["typed"] == []            # never type into a mid-turn pane
    assert briefing["marked"] == []           # and never claim it was briefed
    assert briefing["blocked"] == []          # not the last attempt, so no escalation yet
    assert len(briefing["timers"]) == 1
    timer = briefing["timers"][0]
    assert timer.delay == daemon.BRIEFING_RETRY_DELAY
    assert timer.fn is daemon.deliver_briefing
    assert timer.args == ("%7", "4242", "claude", "brief {tid}", 2)


def test_the_settle_retry_is_bounded_and_re_enters_deliver_briefing(briefing):
    """Not just "a timer exists": drive the whole chain and check where it stops.

    Asserting the timer's arguments does not prove the callback is `deliver_briefing`, that
    invoking it works, or that the chain terminates — and a retry that never gave up against
    a pane busy forever would be its own bug."""
    seen = []
    daemon.deliver_briefing("%7", "4242", "claude", "brief {tid}")

    while briefing["timers"]:            # one hop at a time, and never re-run a timer
        timer = briefing["timers"].pop(0)
        seen.append(timer.args[4])       # the attempt number it was handed
        timer.fn(*timer.args, **timer.kwargs)

    assert seen == list(range(2, daemon.BRIEFING_MAX_ATTEMPTS + 1))
    assert briefing["typed"] == []       # still never typed into the busy pane
    assert len(briefing["blocked"]) == 1  # and the owner was told exactly once, at the cap


# ---- C2: the last attempt escalates, past the cooldown ----------------------------------

def test_settle_timeout_on_the_last_attempt_reports_the_blocked_pane(briefing):
    daemon._blocked_reported["4242"] = 10 ** 12   # a cooldown that would swallow the report
    try:
        daemon.deliver_briefing("%7", "4242", "claude", "brief {tid}",
                                attempt=daemon.BRIEFING_MAX_ATTEMPTS)
    finally:
        daemon._blocked_reported.pop("4242", None)

    assert briefing["timers"] == []               # the chain ends here
    assert len(briefing["blocked"]) == 1
    tid, pane, what = briefing["blocked"][0]
    assert (tid, pane) == ("4242", "%7")
    assert "briefing" in what
    assert briefing["marked"] == []


# ---- C3: a dead pane gets neither a retry nor a prompt the owner cannot answer ----------

@pytest.mark.parametrize("attempt", [1, daemon.BRIEFING_MAX_ATTEMPTS])
def test_a_dead_pane_is_neither_retried_nor_escalated(briefing, attempt):
    """The escalation asks the owner to answer a prompt in the pane. If the pane is gone
    there is no prompt and nothing to brief, so both actions are wrong.

    Driven at `_briefing_exhausted` directly: `deliver_briefing`'s own loops bail on a dead
    pane before they ever reach here, so going through the front door would leave this guard
    untested — the first version of this test did exactly that and survived deleting it."""
    briefing["alive"] = False
    daemon._briefing_exhausted("%7", "4242", "claude", "brief {tid}", attempt, "test")

    assert briefing["timers"] == []
    assert briefing["blocked"] == []
    assert briefing["marked"] == []


def test_a_pane_that_dies_while_typing_gets_no_prompt_it_cannot_show(briefing, monkeypatch):
    """The reachable route to that guard: alive through the idle probe, gone by the time the
    injection has failed. Escalating here would tell the owner to answer a prompt in a pane
    that no longer exists (#188's copy problem, reached from the other side)."""
    monkeypatch.setattr(daemon, "pane_is_idle", lambda _p: True)

    def _dies_after_typing(*_a, **_k):
        briefing["alive"] = False
        return "failed"

    monkeypatch.setattr(daemon, "type_line", _dies_after_typing)
    daemon.deliver_briefing("%7", "4242", "claude", "brief {tid}",
                            attempt=daemon.BRIEFING_MAX_ATTEMPTS)

    assert briefing["timers"] == []
    assert briefing["blocked"] == []
    assert briefing["marked"] == []


@pytest.mark.parametrize("attempt", [1, daemon.BRIEFING_MAX_ATTEMPTS])
def test_a_pane_that_dies_swallowing_the_briefing_gets_no_prompt_either(briefing, monkeypatch,
                                                                       attempt):
    """The route the cross-family review found. `swallowed` means "a modal ate the keys", and
    it reports to the owner on the attempt itself rather than only at the cap. With that
    report at the CALL SITE it ran ahead of the liveness gate, so a pane that died during
    `type_line` still produced a "go answer the prompt" message about a pane with no prompt —
    reproduced as reports=1, timers=0, alive=False. The report moved inside the shared path,
    behind the same gate as the final escalation."""
    monkeypatch.setattr(daemon, "pane_is_idle", lambda _p: True)

    def _dies_swallowing(*_a, **_k):
        briefing["alive"] = False
        return "swallowed"

    monkeypatch.setattr(daemon, "type_line", _dies_swallowing)
    daemon.deliver_briefing("%7", "4242", "claude", "brief {tid}", attempt=attempt)

    assert briefing["blocked"] == []
    assert briefing["timers"] == []
    assert briefing["marked"] == []


# ---- C4: one rule, one site -------------------------------------------------------------

def test_every_give_up_point_routes_through_one_exhaustion_function():
    """The bug was three copies of one decision disagreeing. Enumerating the call sites is
    what stops a fourth copy being added; a behavioural test on today's three cannot."""
    import inspect
    body = inspect.getsource(daemon.deliver_briefing)

    assert "threading.Timer" not in body, (
        "deliver_briefing schedules its own retry again — the retry decision belongs to "
        "_briefing_exhausted alone")
    assert body.count("_briefing_exhausted(") == 3, (
        "expected exactly the three give-up points (compaction, settle timeout, non-sent "
        "type_line) to route through the shared path")
    assert "report_blocked_pane(" not in body, (
        "a report outside the shared path is a report outside its liveness gate — that is "
        "exactly how the swallowed case escaped it")

    shared = inspect.getsource(daemon._briefing_exhausted)
    assert shared.count("threading.Timer") == 1
    assert shared.count("_blocked_reported.pop") == 1


# ---- C5: the behaviours that were already right stay right ------------------------------

def test_the_compaction_retry_still_carries_settle_and_await_busy(briefing, monkeypatch):
    monkeypatch.setattr(daemon, "_compaction_settled", lambda *a: False)
    daemon.deliver_briefing("%7", "4242", "claude", "brief {tid}",
                            settle=7, await_busy=True)

    assert len(briefing["timers"]) == 1
    assert briefing["timers"][0].kwargs == {"settle": 7, "await_busy": True}


def test_the_settle_retry_does_not_inherit_settle(briefing):
    """A retry fires BRIEFING_RETRY_DELAY later; re-waiting the original window would pin a
    Timer thread for no reason."""
    daemon.deliver_briefing("%7", "4242", "claude", "brief {tid}")
    assert briefing["timers"][0].kwargs == {}


def test_a_swallowed_injection_still_reports_before_the_last_attempt(briefing, monkeypatch):
    monkeypatch.setattr(daemon, "pane_is_idle", lambda _p: True)
    monkeypatch.setattr(daemon, "type_line", lambda *a, **k: "swallowed")

    daemon.deliver_briefing("%7", "4242", "claude", "brief {tid}")

    assert len(briefing["blocked"]) == 1          # told the owner now, not only at the cap
    assert len(briefing["timers"]) == 1
    assert briefing["marked"] == []               # a swallowed briefing is not a briefing


def test_an_abandoned_injection_neither_retries_nor_escalates(briefing, monkeypatch):
    """The topic left this pane mid-typing: the chain that now owns it briefs it."""
    monkeypatch.setattr(daemon, "pane_is_idle", lambda _p: True)
    monkeypatch.setattr(daemon, "type_line", lambda *a, **k: "abandoned")

    daemon.deliver_briefing("%7", "4242", "claude", "brief {tid}")

    assert briefing["timers"] == []
    assert briefing["blocked"] == []
    assert briefing["marked"] == []


def test_an_idle_pane_is_still_briefed_and_marked(briefing, monkeypatch):
    monkeypatch.setattr(daemon, "pane_is_idle", lambda _p: True)

    daemon.deliver_briefing("%7", "4242", "claude", "brief {tid}")

    assert len(briefing["typed"]) == 1
    assert briefing["typed"][0][:2] == ("%7", "brief 4242")
    assert len(briefing["marked"]) == 1
    assert briefing["timers"] == [] and briefing["blocked"] == []


# ---- C6: the docstring no longer claims a fallback that cannot run ----------------------

def test_the_docstring_no_longer_names_idle_sweep_as_the_settle_fallback():
    """#233's class: a comment that overstates what the code does is load-bearing for the
    next reader, who will not re-derive it."""
    doc = daemon.deliver_briefing.__doc__ or ""
    assert not re.search(r"leave it for idle_sweep", doc)
    assert "idle_sweep_loop to nudge" not in doc
    assert "_briefing_exhausted" in doc, (
        "the docstring should name the path a busy pane actually takes")
