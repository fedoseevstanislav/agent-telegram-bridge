"""#212 — rejecting an untrusted close/reopen event also discards the only observation the
Bot API gives of a topic's open/closed state, so the registry's `closed` flag can go stale
in both directions. Two narrow repairs, neither able to invoke lifecycle effects:

1. A delivery Telegram ACCEPTED proves the topic is open — sends to a closed topic are
   rejected — so `reply` retires a stale `closed` on success. Passive: it records the
   observation and invokes nothing (the hole #206 closed stays closed).
2. The rejection log line is coalesced per identical (event, topic, sender, sender_chat)
   inside a window, so an actor with topic-management authority cannot grow the journal
   and bury real alarms.
"""

import pytest

from bridge import common, daemon


@pytest.fixture
def registry():
    def _seed(entries):
        common.update_registry(lambda reg: (reg.clear(), reg.update(entries)))
    yield _seed
    common.update_registry(lambda reg: reg.clear())


# ---------------------------------------------------------------------------
# 1. reply() reconciliation
# ---------------------------------------------------------------------------

def test_a_successful_send_clears_a_stale_closed_flag(registry, monkeypatch):
    registry({"5935": {"pane": "%1", "closed": True}})
    monkeypatch.setattr(daemon, "send_message", lambda *a, **k: None)

    assert daemon.reply({"bot_token": "t", "chat_id": 1}, 5935, "hi") is True
    assert "closed" not in common.read_registry()["5935"], (
        "Telegram accepted the send, so the topic is open — the stale flag must go (#212)"
    )


def test_a_successful_send_to_an_open_topic_writes_nothing(registry, monkeypatch):
    """No flag, no write: reply is the hot path and must not touch the registry lock on
    every ordinary delivery."""
    registry({"5935": {"pane": "%1"}})
    monkeypatch.setattr(daemon, "send_message", lambda *a, **k: None)
    writes = []
    monkeypatch.setattr(daemon, "set_topic_closed",
                        lambda tid, closed: writes.append((tid, closed)))

    assert daemon.reply({"bot_token": "t", "chat_id": 1}, 5935, "hi") is True
    assert writes == []


def test_a_rejected_send_still_marks_closed(registry, monkeypatch):
    """The existing negative observation is unchanged."""
    registry({"5935": {"pane": "%1"}})

    def _boom(*a, **k):
        raise RuntimeError("Bad Request: TOPIC_CLOSED")

    monkeypatch.setattr(daemon, "send_message", _boom)

    assert daemon.reply({"bot_token": "t", "chat_id": 1}, 5935, "hi") is False
    assert common.read_registry()["5935"].get("closed") is True


def test_general_never_reconciles(registry, monkeypatch):
    """Thread id 0 / None is General — not a closable topic, and set_topic_closed refuses
    it anyway; reply must not even read the registry for it."""
    monkeypatch.setattr(daemon, "send_message", lambda *a, **k: None)
    reads = []
    monkeypatch.setattr(daemon, "read_registry", lambda: reads.append(1) or {})

    assert daemon.reply({"bot_token": "t", "chat_id": 1}, 0, "hi") is True
    assert daemon.reply({"bot_token": "t", "chat_id": 1}, None, "hi") is True
    assert reads == []


def test_reconciliation_invokes_no_lifecycle_effects(registry, monkeypatch):
    """The #206 boundary: clearing the flag may not start a revive or a pending question."""
    registry({"5935": {"pane": "%1", "closed": True, "ended": "2026-01-01T00:00:00+0000"}})
    monkeypatch.setattr(daemon, "send_message", lambda *a, **k: None)
    revives = []
    monkeypatch.setattr(daemon, "revive_one",
                        lambda *a, **k: revives.append(1) or ("failed", {}))

    daemon.reply({"bot_token": "t", "chat_id": 1}, 5935, "hi")
    entry = common.read_registry()["5935"]
    assert "closed" not in entry
    assert entry.get("ended"), "the flag clear must not resurrect an ended entry"
    assert revives == []


# ---------------------------------------------------------------------------
# 2. rejection-log coalescing
# ---------------------------------------------------------------------------

@pytest.fixture
def reject_log(monkeypatch):
    lines = []
    monkeypatch.setattr(daemon, "log", lambda s: lines.append(s))
    daemon._svc_rejects.clear()
    yield lines
    daemon._svc_rejects.clear()


def test_identical_rejections_inside_the_window_emit_one_line(reject_log, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(daemon.time, "monotonic", lambda: now[0])

    for _ in range(50):
        daemon._log_rejected_service_event("close", 5935, 42, None)
        now[0] += 0.1

    assert len(reject_log) == 1, "an event flood must not become a journal flood (#212)"
    assert "ignored forum close of topic 5935" in reject_log[0]


def test_the_suppressed_count_is_carried_on_the_next_line(reject_log, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(daemon.time, "monotonic", lambda: now[0])

    for _ in range(10):
        daemon._log_rejected_service_event("close", 5935, 42, None)
    now[0] += daemon._SVC_REJECT_WINDOW + 1
    daemon._log_rejected_service_event("close", 5935, 42, None)

    assert len(reject_log) == 2
    assert "9 identical rejections suppressed" in reject_log[1]


def test_distinct_rejections_are_not_coalesced(reject_log, monkeypatch):
    monkeypatch.setattr(daemon.time, "monotonic", lambda: 1000.0)

    daemon._log_rejected_service_event("close", 5935, 42, None)
    daemon._log_rejected_service_event("reopen", 5935, 42, None)
    daemon._log_rejected_service_event("close", 5935, 43, None)

    assert len(reject_log) == 3, "different event/sender must each land immediately"


def test_the_first_line_of_a_burst_lands_immediately(reject_log, monkeypatch):
    monkeypatch.setattr(daemon.time, "monotonic", lambda: 1000.0)

    daemon._log_rejected_service_event("close", 5935, 42, None)

    assert len(reject_log) == 1, "coalescing must delay repeats, never the first sighting"


def test_the_window_survives_a_wall_clock_rollback(reject_log, monkeypatch):
    """#272 review r1, finding 2: the window is monotonic, so setting the WALL clock back
    must neither extend suppression nor delay the next window's line. Pinned by keeping
    time.time() far in the past while the monotonic clock advances normally."""
    mono = [1000.0]
    monkeypatch.setattr(daemon.time, "monotonic", lambda: mono[0])
    monkeypatch.setattr(daemon.time, "time", lambda: 5.0)  # wall clock 'rolled back'

    daemon._log_rejected_service_event("close", 5935, 42, None)
    mono[0] += daemon._SVC_REJECT_WINDOW + 1
    daemon._log_rejected_service_event("close", 5935, 42, None)

    assert len(reject_log) == 2, (
        "a wall-clock correction silently extended suppression past the real window"
    )


def test_the_key_table_is_bounded_at_the_cap(reject_log, monkeypatch):
    """#272 review r1, finding 1: `> 256` before insert let the table reach 257, and the
    blanket clear it guarded threw away live windows. The table may never exceed the cap."""
    monkeypatch.setattr(daemon.time, "monotonic", lambda: 1000.0)

    for sender in range(daemon._SVC_REJECT_MAX_KEYS + 10):
        daemon._log_rejected_service_event("close", 5935, sender, None)

    assert len(daemon._svc_rejects) <= daemon._SVC_REJECT_MAX_KEYS


def test_the_cap_does_not_lose_a_live_burst_count(reject_log, monkeypatch):
    """The reviewer's continuing-burst trace: key A has suppressed repeats, the table hits
    the cap on OTHER keys whose windows already turned, and A's next line after its window
    must still carry its count — eviction takes expired windows first."""
    now = [0.0]
    monkeypatch.setattr(daemon.time, "monotonic", lambda: now[0])

    # Fill the table one short of the cap with keys whose windows will have TURNED by the
    # time the cap eviction runs.
    for sender in range(2, daemon._SVC_REJECT_MAX_KEYS + 1):
        daemon._log_rejected_service_event("reopen", 5935, sender, None)

    now[0] = daemon._SVC_REJECT_WINDOW + 1
    daemon._log_rejected_service_event("close", 5935, 1, None)   # key A, live window
    for _ in range(5):
        daemon._log_rejected_service_event("close", 5935, 1, None)

    # A new key at the cap: eviction must take the expired fillers, never live A.
    now[0] += 10
    daemon._log_rejected_service_event("reopen", 5935, 9999, None)

    now[0] += daemon._SVC_REJECT_WINDOW                           # A's window turns
    daemon._log_rejected_service_event("close", 5935, 1, None)

    assert any("5 identical rejections suppressed" in line for line in reject_log), (
        "the cap eviction discarded the window state of a burst still running"
    )


def test_an_all_live_eviction_flushes_the_count_instead_of_dropping_it(
        reject_log, monkeypatch):
    """#272 review r2: when all 256 windows are live, the evicted key may hold a suppressed
    count whose burst is still running — it must be flushed as a line, never dropped."""
    now = [0.0]
    monkeypatch.setattr(daemon.time, "monotonic", lambda: now[0])

    daemon._log_rejected_service_event("close", 5935, 1, None)   # key A, the oldest
    for _ in range(5):
        daemon._log_rejected_service_event("close", 5935, 1, None)
    now[0] = 1.0
    for sender in range(2, daemon._SVC_REJECT_MAX_KEYS + 1):     # fill to cap, all live
        daemon._log_rejected_service_event("reopen", 5935, sender, None)

    now[0] = 2.0
    daemon._log_rejected_service_event("reopen", 5935, 9999, None)  # forces the eviction

    flushed = [line for line in reject_log
               if "5 identical rejections suppressed" in line and "evicted" in line]
    assert flushed, (
        "evicting a live window silently dropped its suppressed count (#272 r2)"
    )
    assert "close" in flushed[0] and "5935" in flushed[0]


def test_a_stale_cleanup_at_capacity_flushes_counts_too(reject_log, monkeypatch):
    """#272 review r3: the expired-entry cleanup at capacity deleted counts unprinted. A
    removal for ANY reason flushes — a count leaves the accounting only by being printed."""
    now = [0.0]
    monkeypatch.setattr(daemon.time, "monotonic", lambda: now[0])

    daemon._log_rejected_service_event("close", 5935, 1, None)   # key A
    for _ in range(5):
        daemon._log_rejected_service_event("close", 5935, 1, None)
    for sender in range(2, daemon._SVC_REJECT_MAX_KEYS + 1):     # fill to cap
        daemon._log_rejected_service_event("reopen", 5935, sender, None)

    now[0] = daemon._SVC_REJECT_WINDOW + 1                       # everything expires
    daemon._log_rejected_service_event("reopen", 5935, 9999, None)  # triggers cleanup

    flushed = [line for line in reject_log
               if "5 identical rejections suppressed" in line and "expired" in line]
    assert flushed, (
        "the capacity cleanup deleted an expired window's count unprinted (#272 r3)"
    )
