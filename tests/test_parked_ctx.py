"""A parked session's context size is measured when it is parked and answered by /ctx (#346).

A parked topic has no pane, so the live readout has nothing to read. The number is taken
from the still-bound pane at park time, stamped on the entry with the served claim, shown in
the park notice, and cleared by the revive that makes the session live again.
"""

import pytest

from bridge import common, daemon
from tests.test_idle_park import _one_sweep, park_env, registry  # noqa: F401 (fixtures)


def _park(monkeypatch, ctx_for):
    monkeypatch.setattr(daemon, "context_for", ctx_for)
    _one_sweep(monkeypatch)
    with pytest.raises(KeyboardInterrupt):
        daemon.idle_park_loop({"bot_token": "t", "chat_id": 1})


def test_the_reading_is_stamped_and_named_in_the_notice(registry, park_env, monkeypatch):
    """C1, C2: the pane's reading lands on the entry and in the notice."""
    tmux_calls, replies = park_env
    registry({"7001": {"pane": "%1", "name": "x", "session_id": "sid"}})
    _park(monkeypatch, lambda pane, engine=None: {"pct": 42.7} if pane == "%1" else None)

    entry = common.read_registry()["7001"]
    assert entry.get("parked") is True
    assert entry["parked_ctx"]["pct"] == 42
    assert isinstance(entry["parked_ctx"]["ts"], int)
    assert len(replies) == 1 and "Context: 42% used." in replies[0][1]


@pytest.mark.parametrize("ctx_for", [
    lambda pane, engine=None: None,
    lambda pane, engine=None: {"pct": "garbage"},
    lambda pane, engine=None: {"pct": 140},
    lambda pane, engine=None: (_ for _ in ()).throw(RuntimeError("tmux gone")),
], ids=["none", "malformed", "out-of-range", "raises"])
def test_no_reading_never_stops_the_park(registry, park_env, monkeypatch, ctx_for):
    """C2, C5: without a usable reading the park, claim release and notice are unchanged."""
    tmux_calls, replies = park_env
    registry({"7001": {"pane": "%1", "name": "x", "session_id": "sid"}})
    _park(monkeypatch, ctx_for)

    assert [c for c in tmux_calls if "kill-pane" in c] == [["tmux", "kill-pane", "-t", "%1"]]
    entry = common.read_registry()["7001"]
    assert entry.get("parked") is True and "park_claim" not in entry
    assert "parked_ctx" not in entry
    assert len(replies) == 1 and "Context:" not in replies[0][1]


@pytest.fixture
def ctx_env(registry, monkeypatch):
    replies = []
    monkeypatch.setattr(daemon, "reply", lambda cfg, tid, t: replies.append(t) or True)
    return replies


def test_ctx_on_a_parked_topic_answers_the_stamped_number(registry, ctx_env, monkeypatch):
    """C3: the parked reading, with its time; the dead pane is never read."""
    registry({"7001": {"pane": "%1", "name": "x", "ended": "t", "parked": True,
                       "parked_ctx": {"pct": 37, "ts": 1790000000}}})
    monkeypatch.setattr(daemon, "context_for",
                        lambda *a, **k: pytest.fail("a parked topic's pane must not be read"))
    daemon.handle_command({}, 7001, "/ctx")
    assert len(ctx_env) == 1
    assert "37% used when it was parked at" in ctx_env[0]
    assert daemon.local_datetime(1790000000) in ctx_env[0]


@pytest.mark.parametrize("snap", [None, {"pct": 37}, {"pct": "x", "ts": 1790000000},
                                  {"pct": 10 ** 400, "ts": 1790000000},
                                  {"pct": 37, "ts": 10 ** 400}],
                         ids=["absent", "no-ts", "bad-pct", "huge-pct", "huge-ts"])
def test_ctx_on_a_parked_topic_without_a_reading_says_so(registry, ctx_env, monkeypatch, snap):
    """C3: no usable stamp → an explicit no-reading answer, never a number."""
    entry = {"pane": "%1", "name": "x", "ended": "t", "parked": True}
    if snap is not None:
        entry["parked_ctx"] = snap
    registry({"7001": entry})
    monkeypatch.setattr(daemon, "context_for", lambda *a, **k: {"pct": 99})
    daemon.handle_command({}, 7001, "/ctx")
    assert len(ctx_env) == 1 and "no context reading was taken" in ctx_env[0]
    assert "99" not in ctx_env[0]


def test_ctx_on_a_live_topic_ignores_a_leftover_stamp(registry, ctx_env, monkeypatch):
    """C4: a live entry reads its pane, whatever parked_ctx says."""
    registry({"7001": {"pane": "%1", "name": "x", "parked_ctx": {"pct": 5, "ts": 1790000000}}})
    monkeypatch.setattr(daemon, "context_for", lambda pane, engine=None: {"pct": 61})
    daemon.handle_command({}, 7001, "/ctx")
    assert ctx_env == ["Context window: 61% used (x)"]
