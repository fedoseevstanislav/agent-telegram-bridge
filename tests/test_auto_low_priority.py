"""Automatic `/low-priority` when the account's 5-hour window is spent (#279).

The limit is account-wide, so ONE reading of `/tmp/claude-usage-cache.json` decides it for
every live Claude pane. The daemon then types the same slash command the owner was typing by
hand, through the same idle-gated, pane-locked path `/model` uses, once per pane per window.
"""

import json

import pytest

from bridge import daemon


# --------------------------------------------------------------------------- fakes


class _Result:
    def __init__(self, stdout="", returncode=0):
        self.stdout = stdout
        self.returncode = returncode


class Fleet:
    """A fake tmux + registry the sweep can run against, recording every keystroke."""

    def __init__(self, monkeypatch, panes, idle=True, pane_text="",
                 echoes=True, capture_fails=False):
        # panes: {topic_id: (pane, engine)}
        self.panes = panes
        self.idle = idle
        self.pane_text = pane_text
        self.echoes = echoes            # False = a modal swallows what we type
        self.capture_fails = capture_fails
        self.keys = []          # (pane, args...) for every send-keys
        self.replies = []       # (topic_id, text)
        self.timers = []        # (delay, func, args) instead of real threads

        registry = {str(tid): {"pane": pane} for tid, (pane, _e) in panes.items()}
        engines = {pane: engine for pane, engine in panes.values()}

        monkeypatch.setattr(daemon, "read_registry", lambda: dict(registry))
        monkeypatch.setattr(daemon, "pane_alive", lambda pane: pane in engines)
        monkeypatch.setattr(daemon, "engine_of_pane", lambda pane: engines.get(pane))
        monkeypatch.setattr(daemon, "pane_is_idle", lambda pane: self.idle)
        monkeypatch.setattr(daemon, "reply",
                            lambda cfg, tid, text: self.replies.append((str(tid), text)))
        monkeypatch.setattr(daemon.time, "sleep", lambda _s: None)
        monkeypatch.setattr(daemon, "_tmux", self._tmux)
        monkeypatch.setattr(daemon.threading, "Timer", self._timer)
        # _pane_lock takes a real flock under STATE_DIR; the conftest already isolates it.

    def _tmux(self, argv, **kwargs):
        if "capture-pane" in argv:
            if self.capture_fails:
                return _Result("", returncode=1)
            pane = argv[argv.index("-t") + 1]
            return _Result(self.pane_text + "".join(
                f"{text}\n" for p, text in self.keys if p == pane and text != "Enter"
                and self.echoes))
        if "send-keys" in argv:
            pane = argv[argv.index("-t") + 1]
            self.keys.append((pane, argv[-1]))
            return _Result()
        return _Result()

    def _timer(self, delay, func, args=()):
        self.timers.append((delay, func, tuple(args)))
        return _FakeTimer()

    # -- assertions helpers
    def typed(self, pane):
        return [text for p, text in self.keys if p == pane]

    def run_pending_timer(self):
        delay, func, args = self.timers.pop(0)
        func(*args)
        return delay


class _FakeTimer:
    def start(self):
        pass


def write_cache(tmp_path, monkeypatch, percent, resets_at="2026-09-02T12:09:59+00:00",
                locked_reason=None, name="usage.json"):
    cache = {
        "five_hour": {"utilization": percent, "resets_at": resets_at,
                      "locked_reason": locked_reason},
        "seven_day": {"utilization": 22.0, "resets_at": "2026-09-07T23:59:59+00:00"},
        "limits": [
            {"kind": "session", "group": "session", "percent": percent, "severity": "normal",
             "resets_at": resets_at, "scope": None, "is_active": True},
            {"kind": "weekly_all", "group": "weekly", "percent": 22, "severity": "normal",
             "resets_at": "2026-09-07T23:59:59+00:00", "scope": None, "is_active": False},
        ],
    }
    path = tmp_path / name
    path.write_text(json.dumps(cache))
    monkeypatch.setattr(daemon, "USAGE_CACHE", str(path))
    return path


@pytest.fixture(autouse=True)
def _clean_window_guard():
    daemon._low_priority_done.clear()
    yield
    daemon._low_priority_done.clear()


# --------------------------------------------------------------------------- C1


def test_limit_reached_types_low_priority_into_every_claude_pane_and_announces(
        tmp_path, monkeypatch):
    write_cache(tmp_path, monkeypatch, 100)
    fleet = Fleet(monkeypatch, {101: ("%1", "claude"), 102: ("%2", "claude")})

    daemon.low_priority_sweep({})

    assert fleet.typed("%1") == ["/low-priority", "Enter"]
    assert fleet.typed("%2") == ["/low-priority", "Enter"]
    assert sorted(fleet.replies) == [
        ("101", "5h limit hit — switched to low-priority until 15:09 UTC+3."),
        ("102", "5h limit hit — switched to low-priority until 15:09 UTC+3."),
    ]


def test_below_the_limit_types_nothing(tmp_path, monkeypatch):
    write_cache(tmp_path, monkeypatch, 30)
    fleet = Fleet(monkeypatch, {101: ("%1", "claude")})

    daemon.low_priority_sweep({})

    assert fleet.keys == [] and fleet.replies == []


def test_locked_window_trips_even_below_100(tmp_path, monkeypatch):
    # `percent` can lag; a non-null locked_reason is its own, independent signal.
    write_cache(tmp_path, monkeypatch, 97, locked_reason="usage_limit_reached")
    fleet = Fleet(monkeypatch, {101: ("%1", "claude")})

    daemon.low_priority_sweep({})

    assert fleet.typed("%1") == ["/low-priority", "Enter"]


def test_codex_panes_are_never_typed_into(tmp_path, monkeypatch):
    write_cache(tmp_path, monkeypatch, 100)
    fleet = Fleet(monkeypatch, {101: ("%1", "claude"), 102: ("%2", "codex")})

    daemon.low_priority_sweep({})

    assert fleet.typed("%1") == ["/low-priority", "Enter"]
    assert fleet.typed("%2") == []
    assert [tid for tid, _t in fleet.replies] == ["101"]


# --------------------------------------------------------------------------- C2


def test_second_sweep_in_the_same_window_does_not_retype(tmp_path, monkeypatch):
    write_cache(tmp_path, monkeypatch, 100)
    fleet = Fleet(monkeypatch, {101: ("%1", "claude")})

    daemon.low_priority_sweep({})
    daemon.low_priority_sweep({})
    daemon.low_priority_sweep({})

    assert fleet.typed("%1") == ["/low-priority", "Enter"]
    assert len(fleet.replies) == 1


def test_a_new_window_types_again(tmp_path, monkeypatch):
    write_cache(tmp_path, monkeypatch, 100)
    fleet = Fleet(monkeypatch, {101: ("%1", "claude")})
    daemon.low_priority_sweep({})

    # the 5h window rolled over and the account is spent again
    write_cache(tmp_path, monkeypatch, 100, resets_at="2026-09-02T17:09:59+00:00")
    daemon.low_priority_sweep({})

    assert fleet.typed("%1") == ["/low-priority", "Enter", "/low-priority", "Enter"]
    assert len(fleet.replies) == 2


def test_pane_already_in_low_priority_mode_is_not_typed(tmp_path, monkeypatch):
    write_cache(tmp_path, monkeypatch, 100)
    fleet = Fleet(monkeypatch, {101: ("%1", "claude")},
                  pane_text="✳ thinking…\nLower priority until 3:09pm · 78% allowance left\n")

    daemon.low_priority_sweep({})

    assert fleet.typed("%1") == []
    assert fleet.replies == []


# --------------------------------------------------------------------------- C3


def test_busy_pane_is_retried_on_the_idle_timer_not_typed_mid_turn(tmp_path, monkeypatch):
    write_cache(tmp_path, monkeypatch, 100)
    fleet = Fleet(monkeypatch, {101: ("%1", "claude")}, idle=False)

    daemon.low_priority_sweep({})

    assert fleet.typed("%1") == [], "a mid-turn pane must not be typed into"
    assert fleet.replies == []
    assert len(fleet.timers) == 1
    delay, func, _args = fleet.timers[0]
    assert delay == daemon.LOW_PRIORITY_RETRY_DELAY
    assert func is daemon._try_low_priority

    fleet.idle = True          # the turn settles
    fleet.run_pending_timer()
    assert fleet.typed("%1") == ["/low-priority", "Enter"]
    assert len(fleet.replies) == 1


def test_a_pane_that_never_settles_gives_up_after_the_attempt_cap(tmp_path, monkeypatch):
    write_cache(tmp_path, monkeypatch, 100)
    fleet = Fleet(monkeypatch, {101: ("%1", "claude")}, idle=False)

    daemon.low_priority_sweep({})
    while fleet.timers:
        fleet.run_pending_timer()

    assert fleet.typed("%1") == []
    assert fleet.replies == []


def test_enter_is_withheld_when_the_pane_did_not_take_the_command(tmp_path, monkeypatch):
    # A modal swallows printable text and reads Enter as "accept the highlighted option" —
    # the #133 hazard. No receipt, no Enter; retried on the same timer instead.
    write_cache(tmp_path, monkeypatch, 100)
    fleet = Fleet(monkeypatch, {101: ("%1", "claude")}, echoes=False)

    daemon.low_priority_sweep({})

    assert fleet.typed("%1") == ["/low-priority"], "typed, but Enter must be withheld"
    assert fleet.replies == []
    assert len(fleet.timers) == 1 and fleet.timers[0][1] is daemon._try_low_priority

    fleet.echoes = True            # the modal was answered; the command lands
    fleet.run_pending_timer()
    assert fleet.typed("%1") == ["/low-priority", "/low-priority", "Enter"]
    assert len(fleet.replies) == 1


def test_an_unreadable_pane_is_never_typed_into_at_all(tmp_path, monkeypatch):
    # Not merely "no Enter": a pane whose pre-type capture fails cannot be verified, so
    # nothing is typed into it either — otherwise the text strands in a box we can't read.
    write_cache(tmp_path, monkeypatch, 100)
    fleet = Fleet(monkeypatch, {101: ("%1", "claude")}, capture_fails=True)

    daemon.low_priority_sweep({})

    assert fleet.typed("%1") == []
    assert fleet.replies == []
    assert len(fleet.timers) == 1, "an unreadable pane is retried, not abandoned"


def test_a_pane_that_cannot_be_locked_is_retried_not_wedged_for_the_window(
        tmp_path, monkeypatch):
    # The window is claimed BEFORE the send, so a bare return on a lock/tmux error would
    # leave the pane claimed and untried until the 5h window reset (review r2, C1).
    write_cache(tmp_path, monkeypatch, 100)
    fleet = Fleet(monkeypatch, {101: ("%1", "claude")})
    boom = {"n": 0}

    def _flaky(pane, timeout=None):
        boom["n"] += 1
        if boom["n"] == 1:
            raise daemon.PaneLockUnavailable(f"pane {pane} is locked")
        return _real_lock(pane, timeout)

    _real_lock = daemon._pane_lock
    monkeypatch.setattr(daemon, "_pane_lock", _flaky)

    daemon.low_priority_sweep({})
    assert fleet.typed("%1") == [] and fleet.replies == []
    assert len(fleet.timers) == 1, "a lock failure must schedule a retry"

    fleet.run_pending_timer()
    assert fleet.typed("%1") == ["/low-priority", "Enter"]
    assert len(fleet.replies) == 1


def _lock_that_runs(monkeypatch, hook):
    """Make `_pane_lock` run `hook()` at the moment the lock is taken — i.e. simulate the
    world changing while this call queued behind another injector."""
    real_lock = daemon._pane_lock

    def _hooked(pane, timeout=None):
        hook()
        return real_lock(pane, timeout)

    monkeypatch.setattr(daemon, "_pane_lock", _hooked)


# Every precondition, enumerated as limbs rather than as the one case a review happened to
# find. Each must hold when re-read INSIDE the lock, not merely when checked before it.
# `after_first_write` means the flip happens between the command text and the Enter.
PRECONDITIONS = [
    ("pane died",              lambda w: w.update(alive=False)),
    ("became codex",           lambda w: w.update(engine="codex")),
    ("already low-priority",   lambda w: w.update(low_priority=True)),
    ("went mid-turn",          lambda w: w.update(idle=False)),
]


class _World:
    def __init__(self, fleet):
        self.fleet = fleet
        self.alive, self.engine, self.low_priority, self.idle = True, "claude", False, True

    def update(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)

    def bind(self, monkeypatch):
        monkeypatch.setattr(daemon, "pane_alive", lambda pane: self.alive)
        monkeypatch.setattr(daemon, "engine_of_pane", lambda pane: self.engine)
        monkeypatch.setattr(daemon, "pane_is_idle", lambda pane: self.idle)
        monkeypatch.setattr(daemon, "_pane_in_low_priority", lambda pane: self.low_priority)


@pytest.mark.parametrize("name,flip", PRECONDITIONS, ids=[n for n, _f in PRECONDITIONS])
def test_no_precondition_can_go_stale_across_the_lock_wait(tmp_path, monkeypatch, name, flip):
    # r2/r3 found these one at a time (engine, then idle, then already-switched). Enumerate
    # them instead: any precondition that flips while we queue for the lock must stop the
    # FIRST keystroke.
    write_cache(tmp_path, monkeypatch, 100)
    fleet = Fleet(monkeypatch, {101: ("%1", "claude")})
    world = _World(fleet)
    world.bind(monkeypatch)
    _lock_that_runs(monkeypatch, lambda: flip(world))

    daemon.low_priority_sweep({})

    assert fleet.typed("%1") == [], f"typed into a pane that {name} while we waited for the lock"
    assert fleet.replies == []


@pytest.mark.parametrize("name,flip", PRECONDITIONS, ids=[n for n, _f in PRECONDITIONS])
def test_no_precondition_can_go_stale_between_the_text_and_the_enter(
        tmp_path, monkeypatch, name, flip):
    # The 0.5 s settle between the command text and the Enter is a real window (r3, A4). The
    # text may strand in the box — type_line's bargain — but the Enter must be withheld.
    write_cache(tmp_path, monkeypatch, 100)
    fleet = Fleet(monkeypatch, {101: ("%1", "claude")})
    world = _World(fleet)
    world.bind(monkeypatch)
    monkeypatch.setattr(daemon.time, "sleep", lambda _s: flip(world))

    daemon.low_priority_sweep({})

    assert "Enter" not in fleet.typed("%1"), f"pressed Enter on a pane that {name} mid-send"
    assert fleet.replies == []


@pytest.mark.parametrize("raiser", ["pane_alive", "engine_of_pane", "_pane_in_low_priority",
                                    "pane_is_idle"])
def test_a_raising_precondition_retries_rather_than_wedging_the_window(
        tmp_path, monkeypatch, raiser):
    # r3 finding 1: a capture error in a precondition escaped before the retry was scheduled,
    # and the window was already claimed — so the pane was wedged until the 5h reset.
    write_cache(tmp_path, monkeypatch, 100)
    fleet = Fleet(monkeypatch, {101: ("%1", "claude")})
    boom = {"n": 0}

    real = getattr(daemon, raiser)

    def _flaky(pane, *a, **kw):
        boom["n"] += 1
        if boom["n"] == 1:
            raise RuntimeError("tmux went away")
        return real(pane, *a, **kw)

    monkeypatch.setattr(daemon, raiser, _flaky)

    daemon.low_priority_sweep({})
    assert fleet.replies == []
    assert len(fleet.timers) == 1, f"a raising {raiser} must schedule a retry, not wedge"

    fleet.run_pending_timer()
    assert fleet.typed("%1")[-2:] == ["/low-priority", "Enter"]
    assert len(fleet.replies) == 1


def test_one_failing_pane_does_not_cost_the_rest_of_the_fleet_its_switch(
        tmp_path, monkeypatch):
    # The 5h limit is the moment the WHOLE fleet is stalled, so a fleet-wide abort on one
    # unreadable pane is the expensive failure.
    # The notice is the one call OUTSIDE _try_low_priority's own handler — `reply` is network
    # I/O and raises on, say, a topic that was closed in Telegram. Without the sweep's own
    # per-pane guard that would abort the loop and every pane after it keeps burning the cap.
    write_cache(tmp_path, monkeypatch, 100)
    fleet = Fleet(monkeypatch, {101: ("%1", "claude"), 102: ("%2", "claude")})

    def _reply(cfg, tid, text):
        if str(tid) == "101":
            raise RuntimeError("Bad Request: topic was closed")
        fleet.replies.append((str(tid), text))

    monkeypatch.setattr(daemon, "reply", _reply)

    daemon.low_priority_sweep({})

    assert fleet.typed("%1") == ["/low-priority", "Enter"], "the first pane was still switched"
    assert fleet.typed("%2") == ["/low-priority", "Enter"], "the second pane must still be done"
    assert [tid for tid, _t in fleet.replies] == ["102"]


def test_a_pane_that_becomes_codex_while_waiting_for_the_lock_is_not_typed_into(
        tmp_path, monkeypatch):
    # A4. The engine read before the lock is stale by the width of the lock wait, and a
    # revive really does replace the process behind a pane.
    write_cache(tmp_path, monkeypatch, 100)
    fleet = Fleet(monkeypatch, {101: ("%1", "claude")})
    engines = {"%1": "claude"}
    monkeypatch.setattr(daemon, "engine_of_pane", lambda pane: engines.get(pane))

    real_lock = daemon._pane_lock

    def _swap_engine_then_lock(pane, timeout=None):
        engines[pane] = "codex"      # the pane changed hands while we queued
        return real_lock(pane, timeout)

    monkeypatch.setattr(daemon, "_pane_lock", _swap_engine_then_lock)

    daemon.low_priority_sweep({})

    assert fleet.typed("%1") == []
    assert fleet.replies == []


def test_an_earlier_switch_in_scrollback_does_not_count_as_a_receipt(tmp_path, monkeypatch):
    # Presence is not enough: the command text can already be on screen from an earlier
    # switch. Only a strict rise over the pre-type capture authorises the Enter.
    write_cache(tmp_path, monkeypatch, 100)
    fleet = Fleet(monkeypatch, {101: ("%1", "claude")},
                  pane_text="> /low-priority\n(switched an hour ago)\n", echoes=False)

    daemon.low_priority_sweep({})

    assert "Enter" not in fleet.typed("%1")
    assert fleet.replies == []


# --------------------------------------------------------------------------- C4


def test_missing_cache_is_a_no_op(tmp_path, monkeypatch):
    monkeypatch.setattr(daemon, "USAGE_CACHE", str(tmp_path / "absent.json"))
    fleet = Fleet(monkeypatch, {101: ("%1", "claude")})

    daemon.low_priority_sweep({})  # must not raise

    assert fleet.keys == [] and fleet.replies == []


@pytest.mark.parametrize("body", [
    "", "not json at all", "[]", "null", "3",
    '{"limits": "nope"}',
    '{"limits": 1}',                                   # not iterable
    '{"limits": [1, "x", null]}',                      # entries that are not dicts
    '{"five_hour": [], "limits": []}',                 # five_hour of the wrong type
    '{"five_hour": {"utilization": "100"}}',           # percent as a string
    '{"five_hour": {"utilization": true, "resets_at": "2026-09-02T12:00:00+00:00"}}',
    '{"limits": [{"kind": "session", "percent": 100, "resets_at": 17}]}',   # reset not a str
    '{"limits": [{"kind": "session", "percent": 100, "resets_at": "yesterday"}]}',
    # a truthy but non-string locked_reason is an unrecognised shape, not a signal
    '{"five_hour": {"locked_reason": true, "resets_at": "2026-09-02T12:00:00+00:00"}}',
    '{"five_hour": {"locked_reason": {}, "resets_at": "2026-09-02T12:00:00+00:00"}}',
    '{"five_hour": {"locked_reason": [1], "resets_at": "2026-09-02T12:00:00+00:00"}}',
    '{"five_hour": {"locked_reason": "  ", "resets_at": "2026-09-02T12:00:00+00:00"}}',
])
def test_malformed_cache_is_a_no_op(tmp_path, monkeypatch, body):
    path = tmp_path / "usage.json"
    path.write_text(body)
    monkeypatch.setattr(daemon, "USAGE_CACHE", str(path))
    fleet = Fleet(monkeypatch, {101: ("%1", "claude")})

    daemon.low_priority_sweep({})

    assert fleet.keys == [] and fleet.replies == []


def test_limit_reached_without_a_reset_timestamp_is_no_signal(tmp_path, monkeypatch):
    # No window key means no way to say "once per window", so the sweep stays silent rather
    # than typing on every poll for the rest of the day.
    path = tmp_path / "usage.json"
    path.write_text(json.dumps({
        "five_hour": {"utilization": 100.0},
        "limits": [{"kind": "session", "group": "session", "percent": 100}],
    }))
    monkeypatch.setattr(daemon, "USAGE_CACHE", str(path))
    fleet = Fleet(monkeypatch, {101: ("%1", "claude")})

    daemon.low_priority_sweep({})

    assert fleet.keys == [] and fleet.replies == []


def test_detection_runs_from_the_existing_dashboard_loop_and_adds_no_thread():
    import inspect

    source = inspect.getsource(daemon.dashboard_loop)
    assert "low_priority_sweep(cfg)" in source, \
        "the 5h check must ride the loop that already polls the usage cache"
    # No new thread/service anywhere in the feature.
    started = inspect.getsource(daemon.main)
    assert "low_priority" not in started, "no new daemon thread for #279"


def test_five_hour_exhausted_prefers_the_session_limit_entry():
    spent = {"five_hour": {"utilization": 12.0, "resets_at": "2026-09-02T12:00:00+00:00"},
             "limits": [{"kind": "session", "percent": 100,
                         "resets_at": "2026-09-02T12:00:00+00:00"}]}
    assert daemon.five_hour_exhausted(spent) is not None
    # …and falls back to five_hour.utilization when no session entry is present
    assert daemon.five_hour_exhausted(
        {"five_hour": {"utilization": 100, "resets_at": "2026-09-02T12:00:00+00:00"},
         "limits": []}) is not None
    assert daemon.five_hour_exhausted(
        {"five_hour": {"utilization": 99, "resets_at": "2026-09-02T12:00:00+00:00"},
         "limits": []}) is None
    assert daemon.five_hour_exhausted(None) is None
