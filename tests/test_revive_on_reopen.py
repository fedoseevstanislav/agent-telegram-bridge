"""#192 — reopening a topic in Telegram must bring its session back.

#115 asked for exactly this ("...when reopened in Telegram"); #116 shipped only the
on-message trigger and closed the issue, so reopening was inert. That is worse than a plain
gap: the working path was one keystroke away from the dead one, so reopening a topic and
waiting looked like a broken revive rather than a missing feature. Observed 2026-08-24 on
topic 14886 — reopened at 21:26:23, nothing happened, session stayed dead.

The tests drive the real `handle_message` so the CALL SITE is covered, not a re-implementation
of the decision — the same distinction that left three in-loop mutations alive earlier in this
repo's history.

The subtle one is `test_a_reopen_the_bridge_itself_caused_does_not_loop`. `revive_one` calls
`reopen_topic` BEFORE it clears `ended` (daemon.py ~2802 vs ~2817), so every revive provokes
a `forum_topic_reopened` that arrives back at this very handler with `ended` still set. Two
guards stop the echo — `_auto_reviving` for the duration of the revive, and the cleared
`ended` afterwards — and the manual path never sets the first one, so it leans on the second
alone. That is a timing argument until it is a test.
"""

import types

import pytest

from bridge import daemon


TID = 14886
# `owner_id` matches the sender `_msg` builds: since #206 a forum service message only drives
# state and revival when the owner (or this bot) sent it.
CFG = {"chat_id": -100123, "bot_token": "t", "owner_id": 1}


def _msg(kind, thread_id=TID):
    """A forum service message of `kind` ("closed" or "reopened")."""
    m = {
        "chat": {"id": CFG["chat_id"]},
        "message_id": 1,
        "message_thread_id": thread_id,
        "from": {"id": 1},
    }
    m["forum_topic_closed" if kind == "closed" else "forum_topic_reopened"] = {}
    return m


@pytest.fixture
def harness(monkeypatch):
    """Run the real `handle_message` over a registry we control; record what it did."""
    state = {"registry": {}, "revived": [], "closed_calls": [], "offered": []}

    monkeypatch.setattr(daemon, "read_registry", lambda: state["registry"])
    monkeypatch.setattr(daemon, "set_topic_closed",
                        lambda tid, closed: state["closed_calls"].append((tid, closed)))
    monkeypatch.setattr(daemon, "log", lambda *a, **k: None)
    # Stop at revive_one: what matters here is WHETHER the revive is dispatched, and the
    # real one launches tmux. maybe_auto_revive's own gating still runs for real.
    monkeypatch.setattr(daemon, "revive_one",
                        lambda cfg, tid, entry, **kw: (state["revived"].append(str(tid)) or ("resumed", None)))
    monkeypatch.setattr(daemon, "_auto_reviving", set())
    # #195 turned the immediate revive into a question. What this file guards is the
    # DISPATCH — that a reopen (and only a reopen, and only for a revivable topic) reaches
    # the revive machinery at all. Record the offer as the observable for that.
    monkeypatch.setattr(daemon, "pending_reopens", {})
    # Round 2, finding 9: without this the offer path calls the REAL writer, which targets
    # ~/.local/share/agent-telegram-bridge — the LIVE bridge's state directory. On this host
    # that write succeeds and silently pollutes production state; in a sandbox it fails, and
    # the reduced-durability notice arrives as a second "offer" that breaks the assertions.
    # Either way the test was reporting on the environment, not on the code.
    monkeypatch.setattr(daemon, "_save_pending_reopens", lambda: True)
    monkeypatch.setattr(daemon, "reply", lambda cfg, tid, text: bool(state["offered"].append(str(tid))) or True)
    monkeypatch.setattr(daemon, "session_context_tokens", lambda sid, cwd: 352_556)
    # #195: the question is only asked for a large, old session. Default to "yes" here; the
    # branch has its own test below.
    #
    # Round 3, finding 8 collapsed the two reads into ONE sample, so stubbing
    # resume_picker_expected no longer intercepts anything — the gate fell through to the
    # real session_age_minutes, which stats the REAL transcript of a REAL session id and
    # reported it as minutes old. The test then silently exercised the small-session branch
    # while claiming to test the large one. Stub the sample the gate actually reads.
    monkeypatch.setattr(daemon, "session_cost_sample", lambda sid, cwd: (352_556, 4320.0))

    # maybe_auto_revive dispatches `_revive` on a real daemon thread. Joining it after the
    # fact would make every assertion here a race; run it synchronously instead so "did it
    # revive?" is a fact about the code path rather than about scheduling.
    class _InlineThread:
        def __init__(self, target=None, daemon=None, **kw):
            self._target = target

        def start(self):
            if self._target:
                self._target()

    monkeypatch.setattr(daemon.threading, "Thread", _InlineThread)

    def _run(kind, entry=None, thread_id=TID):
        if entry is not None:
            state["registry"] = {str(thread_id): entry}
        daemon.handle_message(CFG, _msg(kind, thread_id))
        return state

    return _run, state


ENDED = {"pane": "%61", "engine": "claude", "ended": "2026-08-23T20:17:40+0000",
         "session_id": "00000000-0000-4000-8000-000000000002"}


# ---- the fix ------------------------------------------------------------------

def test_reopening_an_ended_topic_reaches_the_revive_path(harness):
    # Pre-#195 this revived outright; now a large old session is offered the choice first.
    # Either way the reopen must REACH the machinery — inertness is the #115/#116 gap this
    # file exists for.
    run, state = harness
    run("reopened", dict(ENDED))
    assert state["offered"] == [str(TID)], (
        "reopening the topic did nothing — this is the #115/#116 gap (#192)"
    )


def test_a_session_claude_will_not_ask_about_is_revived_directly(harness, monkeypatch):
    # Under Claude's own thresholds no picker renders, so there is nothing to relay and
    # nothing to ask: the reopen must still revive rather than stall on a question.
    monkeypatch.setattr(daemon, "session_cost_sample", lambda sid, cwd: (20_000, 4320.0))
    run, state = harness
    run("reopened", dict(ENDED))
    assert state["offered"] == [] and state["revived"] == [str(TID)]


def test_closing_a_topic_never_revives(harness):
    # Both events share this branch, so an edit here breaks in the loudest possible way:
    # ending a session would immediately resurrect it.
    run, state = harness
    run("closed", dict(ENDED))
    assert state["revived"] == [] and state["offered"] == []


# ---- what must stay a no-op ---------------------------------------------------

def test_reopening_a_live_topic_does_nothing(harness):
    run, state = harness
    run("reopened", {"pane": "%61", "engine": "claude", "session_id": "abc"})  # no `ended`
    assert state["revived"] == [] and state["offered"] == []


def test_reopening_a_feed_topic_does_nothing(harness):
    run, state = harness
    run("reopened", dict(ENDED, feed=True))
    assert state["revived"] == [] and state["offered"] == []


def test_reopening_a_topic_with_no_session_id_offers_a_fresh_start(harness):
    # Nothing to resume, and a fresh session is an operator decision — so it is still never
    # revived automatically. But #198: doing nothing AND saying nothing is what the owner hit on
    # 2026-08-26, reopening a killed codex topic to demo the revive and getting silence.
    # Not-reviving is right; not-speaking is the bug.
    entry = dict(ENDED)
    entry.pop("session_id")
    run, state = harness
    run("reopened", entry)
    assert state["revived"] == [], "started a session they never asked for"
    assert state["offered"] == [str(TID)], "reopen fell silent on an unrevivable topic"


def test_reopening_an_unregistered_topic_does_nothing(harness):
    run, state = harness
    run("reopened", None, thread_id=99999)
    assert state["revived"] == [] and state["offered"] == []


# ---- the fix must not displace what the branch already did --------------------

def test_state_recording_still_happens_on_both_events(harness):
    # #161: these service messages are the only source of truth for open/closed, and the
    # digest reads it. Recording must not become conditional on the revive.
    run, state = harness
    run("reopened", dict(ENDED))
    run("closed", dict(ENDED))
    assert state["closed_calls"] == [(TID, False), (TID, True)]


def test_state_is_recorded_even_when_no_revive_is_possible(harness):
    run, state = harness
    run("reopened", {"pane": "%61"})          # not revivable
    assert state["closed_calls"] == [(TID, False)]
    assert state["revived"] == [] and state["offered"] == []


# ---- the echo ------------------------------------------------------------------

def test_a_reopen_the_bridge_itself_caused_does_not_loop(harness, monkeypatch):
    # revive_one reopens the topic BEFORE clearing `ended`, so its own service message comes
    # back here with `ended` still set. While the revive is in flight `_auto_reviving` holds
    # the tid; without that guard this reopen would start a second revive on top of the first.
    run, state = harness
    monkeypatch.setattr(daemon, "_auto_reviving", {str(TID)})
    run("reopened", dict(ENDED))
    assert state["revived"] == [], (
        "a reopen arriving mid-revive started another revive — that is the loop"
    )


def test_after_the_revive_clears_ended_a_further_reopen_is_inert(harness):
    # The other half of the echo guard: once `ended` is gone, replays are no-ops.
    run, state = harness
    entry = dict(ENDED)
    entry.pop("ended")
    run("reopened", entry)
    assert state["revived"] == [] and state["offered"] == []


# ---- codex is excluded from the choice (#195) ---------------------------------

def test_a_codex_topic_reopens_without_being_asked(harness, monkeypatch):
    """Codex has no compaction flow of ours to offer and takes no --autocompact, so there is
    nothing to choose — a reopen just revives it, as #192 shipped."""
    run, state = harness
    asked = []
    monkeypatch.setattr(daemon, "offer_reopen_choice",
                        lambda cfg, tid, entry: asked.append(str(tid)))
    run("reopened", dict(ENDED, engine="codex"))
    assert asked == [], "a codex topic was offered a compaction choice it cannot honour"
    assert state["revived"] == [str(TID)], "the codex topic was not revived at all"
