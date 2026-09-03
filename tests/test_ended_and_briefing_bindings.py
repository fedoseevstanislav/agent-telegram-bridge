"""Two stamps that trusted a binding nobody re-checked (#237, #238).

#237: `mark_ended` stamped `ended` keyed on the topic id alone. A concurrent revive can
rebind the topic to a live pane between `lifecycle_loop` observing pane A dead and the
stamp landing — the topic then carries `ended` for a pane nobody checked, which makes
`should_auto_revive` true and can drive a revive against an already-live session.
Reproduced as a state transition by the #236 round-1 review.

#238: `deliver_briefing` consulted `_briefing_still_ours` only when it actually waited, so
a plain first attempt typed into whatever pane it was handed, even when the registry had
already moved the topic elsewhere. Reproduced by the same review for both engines. The
exemption's stated reason — that the re-read races the binding write revive_one just made —
does not hold: `update_registry` commits under an exclusive file lock before
`deliver_briefing` is called, so this chain's own bind is always visible.
"""

import types

import pytest

from bridge import common, daemon


BOOT = "00000000-0000-4000-8000-0000000b0071"


@pytest.fixture
def registry(monkeypatch):
    """A real registry in the isolated HOME, seeded per test. mark_ended goes through the
    real update_registry so the compare-and-stamp runs where it will run in production —
    inside the lock."""
    def _seed(entries):
        common.update_registry(lambda reg: (reg.clear(), reg.update(entries)))
        return entries
    yield _seed
    common.update_registry(lambda reg: reg.clear())


# ---------------------------------------------------------------------------
# #237 — mark_ended
# ---------------------------------------------------------------------------

def test_a_dead_pane_still_bound_is_marked_ended(registry):
    """C3 — the ordinary case is unchanged: observed pane == bound pane, stamp lands."""
    registry({"235": {"pane": "%A", "name": "x"}})

    assert daemon.mark_ended("235", "%A") is True
    assert "ended" in common.read_registry()["235"]


def test_a_topic_rebound_mid_sweep_is_not_marked_ended(registry):
    """C1/C2 — the reviewer's reproduction: the sweep observed %A dead, a revive rebound
    the topic to live %B before the stamp. The stamp must not land."""
    registry({"235": {"pane": "%B", "name": "x"}})

    assert daemon.mark_ended("235", "%A") is False
    entry = common.read_registry()["235"]
    assert "ended" not in entry, (
        "ended was stamped for a pane the topic is no longer bound to — "
        "should_auto_revive would now revive an already-live session (#237)"
    )
    assert entry["pane"] == "%B", "the live binding was disturbed"


def test_a_vanished_entry_is_not_stamped(registry):
    """A topic retired between the observation and the stamp must not be resurrected."""
    registry({})

    assert daemon.mark_ended("235", "%A") is False
    assert "235" not in common.read_registry()


def test_the_sweep_does_not_announce_an_end_that_was_not_stamped(registry, monkeypatch):
    """When mark_ended refuses, lifecycle_loop must not tell the topic its session ended,
    and must not close it — the session there is alive."""
    registry({"235": {"pane": "%B", "name": "x"}})
    # The sweep's own snapshot still shows the dead pane: the rebind happened after it read.
    monkeypatch.setattr(daemon, "read_registry",
                        lambda: {"235": {"pane": "%A", "name": "x"}})
    monkeypatch.setattr(daemon, "pane_alive", lambda p: False)
    replies, closes = [], []
    monkeypatch.setattr(daemon, "reply", lambda cfg, tid, text: replies.append((tid, text)))
    monkeypatch.setattr(daemon, "api", lambda tok, method, payload: closes.append(method))

    calls = {"n": 0}

    def _sleep_once(_secs):
        calls["n"] += 1
        if calls["n"] > 1:
            raise KeyboardInterrupt  # one sweep is enough

    monkeypatch.setattr(daemon.time, "sleep", _sleep_once)
    with pytest.raises(KeyboardInterrupt):
        daemon.lifecycle_loop({"bot_token": "t", "chat_id": 1})

    assert replies == [], "announced an end that was refused"
    assert closes == [], "closed a topic whose session is alive"
    assert "ended" not in common.read_registry()["235"]


def test_the_sweep_still_announces_a_real_end(registry, monkeypatch):
    """C3 at the loop level: bound pane observed dead -> stamp, notice, close."""
    registry({"235": {"pane": "%A", "name": "x"}})
    monkeypatch.setattr(daemon, "pane_alive", lambda p: False)
    replies, closes = [], []
    monkeypatch.setattr(daemon, "reply", lambda cfg, tid, text: replies.append((tid, text)))
    monkeypatch.setattr(daemon, "api", lambda tok, method, payload: closes.append(method))

    calls = {"n": 0}

    def _sleep_once(_secs):
        calls["n"] += 1
        if calls["n"] > 1:
            raise KeyboardInterrupt

    monkeypatch.setattr(daemon.time, "sleep", _sleep_once)
    with pytest.raises(KeyboardInterrupt):
        daemon.lifecycle_loop({"bot_token": "t", "chat_id": 1})

    assert [tid for tid, _ in replies] == [235]
    assert closes == ["closeForumTopic"]
    assert "ended" in common.read_registry()["235"]


# ---------------------------------------------------------------------------
# #238 — deliver_briefing, plain first attempt
# ---------------------------------------------------------------------------

@pytest.fixture
def briefing_env(registry, monkeypatch):
    """Everything deliver_briefing touches, with type_line recording instead of typing."""
    typed = []
    monkeypatch.setattr(daemon, "pane_alive", lambda p: True)
    monkeypatch.setattr(daemon, "pane_is_idle", lambda p: True)
    monkeypatch.setattr(daemon, "has_live_recv", lambda tid: False)
    monkeypatch.setattr(daemon, "current_boot_id", lambda: BOOT)
    monkeypatch.setattr(daemon, "type_line",
                        lambda pane, text, settle=0.4, still_ok=None:
                        (typed.append(pane), "sent")[1])
    return typed


@pytest.mark.parametrize("engine", ["claude", "codex"])
def test_a_first_attempt_does_not_type_into_a_pane_the_topic_has_left(
        registry, briefing_env, engine):
    """C1 — the reviewer's reproduction, both engines: registry bound to %NEW, the chain
    holds %OLD, attempt=1. Nothing may be typed."""
    registry({"5935": {"pane": "%NEW", "engine": engine}})

    daemon.deliver_briefing("%OLD", "5935", engine, "briefing {tid}", attempt=1)

    assert briefing_env == [], (
        "a first-attempt briefing was typed into a pane the topic has left — one topic's "
        "operating instructions land in another topic's session (#238)"
    )
    assert common.read_registry()["5935"].get("briefed_boot") is None, (
        "briefed at a pane that received nothing"
    )


@pytest.mark.parametrize("engine", ["claude", "codex"])
def test_a_first_attempt_into_the_bound_pane_still_types(registry, briefing_env, engine):
    """C2/C3 — the guard must not eat the legitimate inline path: this chain's own bind is
    committed before deliver_briefing runs, so the re-read sees it and the briefing goes
    through, stamping the pane that was actually typed into."""
    registry({"5935": {"pane": "%P", "engine": engine}})

    daemon.deliver_briefing("%P", "5935", engine, "briefing {tid}", attempt=1)

    assert briefing_env == ["%P"]
    assert common.read_registry()["5935"]["briefed_boot"] == BOOT


def test_a_close_that_raced_a_revive_is_undone(registry, monkeypatch):
    """#270 r1 finding 1: the stamp was consistent, but the notice and close run outside
    the lock — a revive claiming the topic in between left a live session announced ended
    with its topic closed. Reproduces the reviewer's interleaving: the rebind lands
    immediately after mark_ended returns."""
    registry({"235": {"pane": "%A", "name": "x"}})
    monkeypatch.setattr(daemon, "pane_alive", lambda p: False)
    events = []
    real_mark_ended = daemon.mark_ended

    def _mark_then_revive(tid, pane):
        stamped = real_mark_ended(tid, pane)
        if stamped:
            def _revive(reg):  # what revive_one's _bind does, at the worst moment
                reg["235"]["pane"] = "%B"
                reg["235"].pop("ended", None)
            common.update_registry(_revive)
            events.append("rebound")
        return stamped

    monkeypatch.setattr(daemon, "mark_ended", _mark_then_revive)
    monkeypatch.setattr(daemon, "reply",
                        lambda cfg, tid, text: events.append(("reply", text)) or True)
    monkeypatch.setattr(daemon, "api",
                        lambda tok, method, payload: events.append(method))
    monkeypatch.setattr(daemon, "reopen_topic",
                        lambda cfg, tid: events.append("reopen") or True)

    calls = {"n": 0}

    def _sleep_once(_secs):
        calls["n"] += 1
        if calls["n"] > 1:
            raise KeyboardInterrupt

    monkeypatch.setattr(daemon.time, "sleep", _sleep_once)
    with pytest.raises(KeyboardInterrupt):
        daemon.lifecycle_loop({"bot_token": "t", "chat_id": 1})

    assert "closeForumTopic" in events, "precondition: the racy close did happen"
    assert events.index("reopen") > events.index("closeForumTopic"), (
        "the close of a revived topic must be undone (#270 r1, finding 1)"
    )
    corrections = [e for e in events if isinstance(e, tuple) and "Disregard" in e[1]]
    assert corrections, "the owner must be told the end notice was wrong"
    entry = common.read_registry()["235"]
    assert entry["pane"] == "%B" and "ended" not in entry, "the revive's claim must stand"


@pytest.fixture(autouse=True)
def _no_pane_state_leak():
    def _reset():
        for t in (daemon._swallowed_streak, daemon._pane_locks, daemon._blocked_reported):
            t.clear()
    _reset()
    yield
    _reset()


def test_type_line_abandons_before_typing_when_authority_lapsed(monkeypatch):
    """The pre-call ownership check is stale by the time the pane lock is held; a False
    from the handed-in predicate must land NOTHING."""
    from test_modal_safe_nudge import CLAUDE_IDLE, FakePane
    pane = FakePane(CLAUDE_IDLE)
    monkeypatch.setattr(daemon, "_tmux", pane)
    monkeypatch.setattr(daemon.time, "sleep", lambda *_a: None)

    assert daemon.type_line("%1", "briefing text", still_ok=lambda: False) == "abandoned"
    assert pane.typed == [], "keystrokes reached a pane the caller no longer owns"
    assert pane.enters == 0


def test_type_line_withholds_enter_when_authority_lapses_mid_call(monkeypatch):
    """The receipt ladder is the LONG wait; authority lost while it ran must withhold the
    Enter — stranded unsent beats delivered to the wrong session. (The stranded line's
    later fate is #269's class, stated in the code.)"""
    from test_modal_safe_nudge import CLAUDE_IDLE, FakePane
    pane = FakePane(CLAUDE_IDLE)
    monkeypatch.setattr(daemon, "_tmux", pane)
    monkeypatch.setattr(daemon.time, "sleep", lambda *_a: None)
    answers = iter([True, False])

    assert daemon.type_line("%1", "briefing text",
                            still_ok=lambda: next(answers)) == "abandoned"
    assert pane.enters == 0, "the Enter delivered a briefing whose topic had moved on"
    assert pane.typed, "precondition: the payload was already typed when authority lapsed"


def test_deliver_briefing_hands_its_ownership_check_into_type_line(registry, monkeypatch):
    """The rebind lands while type_line runs. The fake rebinds the topic and then consults
    the handed-in predicate — the delivery is refused, nothing retried, nothing stamped."""
    registry({"12999": {"pane": "%178", "engine": "claude"}})
    monkeypatch.setattr(daemon, "pane_alive", lambda p: True)
    monkeypatch.setattr(daemon, "pane_is_idle", lambda p: True)
    monkeypatch.setattr(daemon, "has_live_recv", lambda tid: False)
    monkeypatch.setattr(daemon, "current_boot_id", lambda: BOOT)
    timers = []
    monkeypatch.setattr(daemon.threading, "Timer",
                        lambda *a, **k: timers.append(a) or
                        type("T", (), {"start": lambda self: None})())

    seen = {"still_ok": "never called", "verdict": None}

    def _type_line(pane, text, settle=0.4, still_ok=None):
        seen["still_ok"] = still_ok
        if still_ok is None:
            return "sent"
        common.update_registry(lambda reg: reg["12999"].__setitem__("pane", "%999"))
        seen["verdict"] = still_ok()
        return "abandoned"

    monkeypatch.setattr(daemon, "type_line", _type_line)

    daemon.deliver_briefing("%178", "12999", "claude", "brief {tid}")

    assert seen["still_ok"] is not None, (
        "deliver_briefing no longer hands its ownership check into type_line"
    )
    assert seen["verdict"] is False, "the handed-in check missed the mid-type rebind"
    assert timers == [], "an abandoned briefing belongs to the new owner, not a retry"
    assert common.read_registry()["12999"].get("briefed_boot") is None


def test_a_new_codex_pane_on_the_same_boot_is_briefed(registry, monkeypatch):
    """A same-boot codex replacement kept the OLD pane's briefed_boot, so the crash-retry
    guard refused the new pane's first briefing and the session came up dark. The #270 r1
    reviewer's reproduction, through revive_one."""
    entry = {"pane": "%OLD", "engine": "codex", "session_id": "sid",
             "briefed_boot": BOOT, "name": "x"}
    registry({"5935": dict(entry)})
    monkeypatch.setattr(daemon, "current_boot_id", lambda: BOOT)
    monkeypatch.setattr(daemon, "_tmux",
                        lambda *a, **k: types.SimpleNamespace(returncode=1, stdout=""))
    monkeypatch.setattr(daemon, "ensure_codex_trust", lambda cwd: None)
    monkeypatch.setattr(daemon, "launch_pane", lambda name, cwd, launch, engine, reason: ("%NEW", None))
    monkeypatch.setattr(daemon, "reply", lambda cfg, tid, text: True)
    monkeypatch.setattr(daemon, "reopen_topic", lambda cfg, tid: True)
    monkeypatch.setattr(daemon, "pane_alive", lambda p: True)
    monkeypatch.setattr(daemon, "pane_is_idle", lambda p: True)
    monkeypatch.setattr(daemon, "has_live_recv", lambda tid: False)
    typed = []
    monkeypatch.setattr(daemon, "type_line",
                        lambda pane, text, settle=0.4, still_ok=None:
                        (typed.append(pane), "sent")[1])

    status, task = daemon.revive_one({"bot_token": "t", "chat_id": 1}, "5935",
                                     entry, cause="manual")

    assert status == "resumed"
    assert typed == ["%NEW"], (
        "the same-boot replacement pane was refused its first briefing (#270 r1, finding 3)"
    )
    assert common.read_registry()["5935"]["briefed_boot"] == BOOT
