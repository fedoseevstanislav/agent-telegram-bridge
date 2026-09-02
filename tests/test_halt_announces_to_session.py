"""A halted carry-forward must be announced to the SESSION, not only the owner (#134).

Topic 4367, 2026-08-06: the halt fired 43 seconds into the flow, but only the daemon knew.
The session had already been handed the /carryforward instruction, so it executed the
protocol for three more hours and then stopped to wait for a /compact that could never
come — with its listener armed, its inbox drained, and every health signal green. The
owner's read was "it's ignoring me". Recovery took one line typed into the pane by hand;
this makes the daemon type that line itself.
"""

import pytest

from bridge import daemon


@pytest.fixture
def halted_flow(monkeypatch):
    """An active flow for topic 1 on pane %1, with typing and Telegram recorded."""
    typed, replies = [], []
    monkeypatch.setattr(daemon, "_pending_cf", {"1": {"token": "t", "pane": "%1",
                                                      "phase": "settle"}})
    monkeypatch.setattr(daemon, "pane_alive", lambda p: True)
    monkeypatch.setattr(daemon, "_tmux", lambda *a, **k: None)
    monkeypatch.setattr(daemon, "type_line",
                        lambda pane, text, settle=0.4: (typed.append((pane, text)), "sent")[1])
    monkeypatch.setattr(daemon, "reply", lambda cfg, tid, text: replies.append(text) or True)
    return typed, replies


def test_the_halt_tells_the_pane_the_flow_is_dead(halted_flow):
    typed, replies = halted_flow

    assert daemon.halt_carry_forward({}, "1", "user message") is True
    assert len(typed) == 1 and typed[0][0] == "%1"
    assert "cancelled" in typed[0][1], "the session was never told the flow is gone (#134)"
    assert "wait" in typed[0][1], "the notice must break the wait-for-compaction park"
    assert len(replies) == 1, "the owner notice must still go out"


def test_a_dead_pane_is_not_typed_into(halted_flow, monkeypatch):
    typed, replies = halted_flow
    monkeypatch.setattr(daemon, "pane_alive", lambda p: False)

    assert daemon.halt_carry_forward({}, "1", "user message") is True
    assert typed == []
    assert len(replies) == 1


def test_a_failing_notice_does_not_lose_the_halt(halted_flow, monkeypatch):
    """The flow is already popped; a typing failure must neither undo the halt nor eat
    the owner's confirmation — same contract as the Escape above it."""
    typed, replies = halted_flow
    monkeypatch.setattr(daemon, "type_line",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("tmux gone")))

    assert daemon.halt_carry_forward({}, "1", "user message") is True
    assert len(replies) == 1
    assert "1" not in daemon._pending_cf


def test_a_swallowed_notice_is_logged_not_retried(halted_flow, monkeypatch):
    """The owner's next action is resending the halted message, whose own nudge is a fresh
    delivery attempt — a retry chain here would double-type into a modal instead."""
    typed, replies = halted_flow
    monkeypatch.setattr(daemon, "type_line", lambda *a, **k: "swallowed")
    logs = []
    monkeypatch.setattr(daemon, "log", lambda s: logs.append(s))

    assert daemon.halt_carry_forward({}, "1", "user message") is True
    # The EVENT must be logged; the wording may move. Pin the status word and the pane, not
    # the prose (#271 review, finding 2).
    assert any("swallowed" in s and "%1" in s for s in logs)
    assert len(replies) == 1


def test_no_flow_no_notice(monkeypatch):
    typed = []
    monkeypatch.setattr(daemon, "_pending_cf", {})
    monkeypatch.setattr(daemon, "type_line",
                        lambda *a, **k: typed.append(a) or "sent")

    assert daemon.halt_carry_forward({}, "1", "user message") is False
    assert typed == []


def test_two_flows_in_the_same_second_get_distinct_markers(monkeypatch):
    """#271 review, finding 1: with per-second paths, a halt plus a fresh /cf inside one
    second share a marker — the halted session's late write can satisfy the new flow's
    done-gate (an early /compact), and the old worker's cleanup can eat the new marker.
    The path must be per-FLOW."""
    fixed = daemon.datetime.now()

    class _FrozenDatetime:
        @staticmethod
        def now(tz=None):
            return fixed

    monkeypatch.setattr(daemon, "datetime", _FrozenDatetime)
    monkeypatch.setattr(daemon, "pane_alive", lambda p: True)
    monkeypatch.setattr(daemon, "engine_of_pane", lambda p: "claude")
    monkeypatch.setattr(daemon, "reply", lambda cfg, tid, text: True)
    monkeypatch.setattr(daemon, "_cf_mark_unfinished", lambda tid, token: True)
    monkeypatch.setattr(daemon, "_tmux", lambda *a, **k: None)
    monkeypatch.setattr(daemon, "type_line", lambda *a, **k: "sent")
    started = []

    class _Thread:
        def __init__(self, target=None, args=(), daemon=None):
            started.append(args)

        def start(self):
            pass

    monkeypatch.setattr(daemon.threading, "Thread", _Thread)
    monkeypatch.setattr(daemon, "_pending_cf", {})

    info = {"name": "x"}
    assert daemon.handle_carry_forward({}, 7, "/carryforward", info, "%1") is True
    daemon.halt_carry_forward({}, "7", "halted in the same second")
    assert daemon.handle_carry_forward({}, 7, "/carryforward", info, "%1") is True

    markers = [args[4] for args in started]  # _carry_forward_worker(..., marker, ...)
    assert len(markers) == 2
    assert markers[0] != markers[1], (
        "two flows born in the same second share one marker path (#271 review, finding 1)"
    )
