"""A revived session is woken at once when its message is already waiting (#395).

Observed 2026-10-12 on the gym topic: the owner's message revived the parked session at
13:15:09Z and the briefing landed at 13:15:22Z. The session first answered at 13:19:20Z,
after `idle_sweep_loop`. The message that revives a topic gets its wake while the topic has
no live pane, so the wake is typed nowhere. Every later message skips its wake because the
batch counts as nudged. The briefing itself says "end your turn and wait".
"""

import json
import os

import pytest

from bridge import common, daemon


BOOT = "00000000-0000-4000-8000-0000000b0395"


@pytest.fixture
def registry():
    def _seed(entries):
        common.update_registry(lambda reg: (reg.clear(), reg.update(entries)))
        return entries
    yield _seed
    common.update_registry(lambda reg: reg.clear())


def _inbox(tid, lines, cursor):
    path = common.state_path("topics", str(tid), "inbox.jsonl")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        for i in range(lines):
            f.write(json.dumps({"message_id": i, "text": f"m{i}"}) + "\n")
    with open(os.path.join(os.path.dirname(path), "cursor"), "w") as f:
        f.write(str(cursor))


@pytest.fixture
def env(monkeypatch):
    """deliver_briefing with typing and timers recorded instead of performed."""
    typed, timers = [], []

    class _Timer:
        def __init__(self, delay, fn, args=(), kwargs=None):
            self.call = (delay, fn, tuple(args), dict(kwargs or {}))

        def start(self):
            timers.append(self.call)

    monkeypatch.setattr(daemon, "pane_alive", lambda p: True)
    monkeypatch.setattr(daemon, "pane_is_idle", lambda p: True)
    monkeypatch.setattr(daemon, "has_live_recv", lambda tid: False)
    monkeypatch.setattr(daemon, "current_boot_id", lambda: BOOT)
    monkeypatch.setattr(daemon.threading, "Timer", _Timer)
    state = {"status": "sent"}

    def _type_line(pane, text, settle=0.4, still_ok=None):
        if still_ok is not None and not still_ok():
            return "abandoned"
        typed.append((pane, text))
        return state["status"]
    monkeypatch.setattr(daemon, "type_line", _type_line)
    return typed, timers, state


@pytest.mark.parametrize("engine", ["claude", "codex"])
def test_a_briefed_pane_with_unread_messages_gets_the_ordinary_wake(registry, env, engine):
    """C1: briefing typed, three unread lines -> maybe_nudge on the revived pane after
    NUDGE_DELAY, with no wake claim (the claim lapsed with the dead pane)."""
    typed, timers, _ = env
    registry({"7395": {"pane": "%P", "engine": engine}})
    _inbox("7395", lines=5, cursor=2)

    daemon.deliver_briefing("%P", "7395", engine, "briefing {tid}", attempt=1)

    assert [p for p, _ in typed] == ["%P"]
    assert [t[:3] for t in timers] == [(daemon.NUDGE_DELAY, daemon.maybe_nudge,
                                        ("7395", "%P", None))], (
        "the revived session was not woken for the message waiting in its inbox — it "
        "sits until the self-heal sweep, 2-4 minutes (#395)"
    )


@pytest.mark.parametrize("engine", ["claude", "codex"])
def test_a_briefed_pane_with_an_empty_inbox_gets_no_wake(registry, env, engine):
    """C2."""
    typed, timers, _ = env
    registry({"7395": {"pane": "%P", "engine": engine}})
    _inbox("7395", lines=4, cursor=4)

    daemon.deliver_briefing("%P", "7395", engine, "briefing {tid}", attempt=1)

    assert [p for p, _ in typed] == ["%P"]
    assert timers == []


@pytest.mark.parametrize("status", ["swallowed", "failed"])
def test_an_untyped_briefing_schedules_no_extra_wake(registry, env, monkeypatch, status):
    """C3: the briefing did not land -> the exhaustion path owns the retry; no wake here."""
    typed, timers, state = env
    state["status"] = status
    monkeypatch.setattr(daemon, "_briefing_exhausted", lambda *a, **k: None)
    registry({"7395": {"pane": "%P", "engine": "claude"}})
    _inbox("7395", lines=3, cursor=0)

    daemon.deliver_briefing("%P", "7395", "claude", "briefing {tid}", attempt=1)

    assert timers == []


def test_an_abandoned_briefing_schedules_no_extra_wake(registry, env):
    """C3: the topic left the pane while the briefing was being typed."""
    typed, timers, state = env
    state["status"] = "abandoned"
    registry({"7395": {"pane": "%P", "engine": "claude"}})
    _inbox("7395", lines=3, cursor=0)

    daemon.deliver_briefing("%P", "7395", "claude", "briefing {tid}", attempt=1)

    assert timers == []


def test_a_topic_rebound_before_briefing_schedules_no_extra_wake(registry, env):
    """C3: the chain holds %OLD, the registry is on %NEW -> nothing typed, nothing woken."""
    typed, timers, _ = env
    registry({"7395": {"pane": "%NEW", "engine": "claude"}})
    _inbox("7395", lines=3, cursor=0)

    daemon.deliver_briefing("%OLD", "7395", "claude", "briefing {tid}", attempt=1)

    assert typed == [] and timers == []


def test_a_live_recv_skips_the_briefing_and_the_wake(registry, env, monkeypatch):
    """C3: a listening recv drains the inbox itself."""
    typed, timers, _ = env
    monkeypatch.setattr(daemon, "has_live_recv", lambda tid: True)
    registry({"7395": {"pane": "%P", "engine": "claude"}})
    _inbox("7395", lines=3, cursor=0)

    daemon.deliver_briefing("%P", "7395", "claude", "briefing {tid}", attempt=1)

    assert typed == [] and timers == []


def test_a_pane_that_died_before_briefing_schedules_no_extra_wake(registry, env, monkeypatch):
    """C3."""
    typed, timers, _ = env
    monkeypatch.setattr(daemon, "pane_alive", lambda p: False)
    registry({"7395": {"pane": "%P", "engine": "claude"}})
    _inbox("7395", lines=3, cursor=0)

    daemon.deliver_briefing("%P", "7395", "claude", "briefing {tid}", attempt=1)

    assert typed == [] and timers == []


def test_the_scheduled_wake_types_the_ordinary_nudge_line(registry, env):
    """C5: the timer's target is the existing wake. Run it as the timer would, and it types the
    usual "New Telegram message" line into the revived pane."""
    typed, timers, _ = env
    registry({"7395": {"pane": "%P", "engine": "claude"}})
    _inbox("7395", lines=2, cursor=0)
    daemon.deliver_briefing("%P", "7395", "claude", "briefing {tid}", attempt=1)
    (_delay, fn, args, kwargs), = timers
    typed.clear()

    assert fn(*args, **kwargs) is True
    assert len(typed) == 1 and typed[0][0] == "%P"
    assert "New Telegram message in your topic" in typed[0][1]
    assert "tg-bridge recv --topic 7395" in typed[0][1]


def test_the_scheduled_wake_does_nothing_once_the_inbox_is_drained(registry, env):
    """C5: by the time the timer fires the session may already have read everything."""
    typed, timers, _ = env
    registry({"7395": {"pane": "%P", "engine": "claude"}})
    _inbox("7395", lines=2, cursor=0)
    daemon.deliver_briefing("%P", "7395", "claude", "briefing {tid}", attempt=1)
    (_delay, fn, args, kwargs), = timers
    typed.clear()
    _inbox("7395", lines=2, cursor=2)

    assert fn(*args, **kwargs) is False
    assert typed == []


def test_the_scheduled_wake_does_not_type_into_a_pane_the_topic_has_left(registry, env):
    """C5 (review r1): briefed on %OLD, then the topic is rebound to %NEW before the timer
    fires. %OLD is still alive, and the wake must not land there."""
    typed, timers, _ = env
    registry({"7395": {"pane": "%OLD", "engine": "claude"}})
    _inbox("7395", lines=2, cursor=0)
    daemon.deliver_briefing("%OLD", "7395", "claude", "briefing {tid}", attempt=1)
    (_delay, fn, args, kwargs), = timers
    typed.clear()
    registry({"7395": {"pane": "%NEW", "engine": "claude"}})

    assert fn(*args, **kwargs) is False
    assert typed == [], "the revive wake typed into a pane the topic has left"


def test_a_wake_that_cannot_be_scheduled_still_reports_the_reopen(registry, env, monkeypatch):
    """A1 (review r1): the briefing landed, so the owner's reopen status (on_ready) is still
    owed even if reading the inbox for the wake fails."""
    typed, timers, _ = env
    registry({"7395": {"pane": "%P", "engine": "claude"}})

    def _broken(_tid):
        raise OSError("inbox unreadable")
    monkeypatch.setattr(daemon, "unread_count", _broken)
    ready = []

    daemon.deliver_briefing("%P", "7395", "claude", "briefing {tid}", attempt=1,
                            on_ready=lambda: ready.append(True))

    assert [p for p, _ in typed] == ["%P"]
    assert timers == []
    assert ready == [True], "a failed wake setup swallowed the owner's reopen status"
