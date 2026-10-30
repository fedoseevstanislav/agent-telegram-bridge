"""Wake a seat idle at its prompt while its footer lists running background work (#413)."""

import time
import types

import pytest

from bridge import common, daemon

RULE = "─" * 80

IDLE_WITH_SHELL = "\n".join([
    "● Bundle A is building in Codex. Waiting for its completion notification.",
    RULE,
    "❯ ",
    RULE,
    "  project-a (branch-a) • Fable 5.1 • 1h1m 93% W66% • $1.00 • 25%",
    "  ⏵⏵ bypass permissions on · 1 shell, 1 monitor",
])

IDLE_WITH_AGENT = "\n".join([
    RULE,
    "❯ ",
    RULE,
    "  project-b • Opus 5.5 • 49m 93% W65% • $2.00 • 17%",
    "  ⏵⏵ bypass permissions on · 2 shells",
    "",
    "  ● main",
    "  ◯ general-purpose  Footer probe sleeper                 11s · ↓ 55.2k tokens",
])

IDLE_NOTHING = "\n".join([
    "  ⏵⏵ bypass permissions on · 1 shell   (an old footer quoted in scrollback)",
    "  ● main",
    "  ◯ general-purpose  quoted row                    11s · ↓ 1k tokens",
    RULE,
    "❯ ",
    RULE,
    "  project-c • Opus 5.5 • 1h5m 95% W66% • $3.00 • 15%",
    "  ⏵⏵ bypass permissions on (shift+tab to cycle) · /tasks to see subagents",
])


def test_footer_shell_count_ignores_monitors():
    assert daemon.background_work(IDLE_WITH_SHELL) == (1, 0)


def test_footer_counts_shells_and_live_subagent_rows():
    assert daemon.background_work(IDLE_WITH_AGENT) == (2, 1)


def test_scrollback_mention_above_the_input_box_does_not_count():
    assert daemon.background_work(IDLE_NOTHING) == (0, 0)


def test_capture_without_a_rule_line_counts_nothing():
    assert daemon.background_work("1 shell\n● main\n◯ agent") == (0, 0)


def test_check_text_is_addressed_and_names_the_work():
    text = daemon.stall_check_text("123", 21, 1, 2)
    assert text.startswith("[tg-bridge] For the session registered on topic 123")
    assert "idle for 21 min" in text
    assert "1 background shell, 2 subagents" in text
    assert "recv" not in text


@pytest.fixture
def world(monkeypatch):
    """One registered claude seat on pane %1; every input to the loop is a knob."""
    common.update_registry(lambda reg: (reg.clear(), reg.update(
        {"123": {"pane": "%1", "engine": "claude", "session_id": "s"}})))
    state = types.SimpleNamespace(idle_s=25 * 60, busy=False, capture=IDLE_WITH_SHELL,
                                  typed=[], attempts=0, status="sent", now=1_000_000.0, engine="claude",
                                  on_lock=None)
    monkeypatch.setattr(daemon, "fleet_panes", lambda: [("%1", "s", "123", "claude")])
    monkeypatch.setattr(daemon, "pane_alive", lambda pane: True)
    monkeypatch.setattr(daemon, "engine_of_pane", lambda pane: state.engine)
    monkeypatch.setattr(daemon, "_park_activity", lambda tid, info: state.now - state.idle_s)
    monkeypatch.setattr(daemon, "pane_is_idle", lambda pane: not state.busy)
    monkeypatch.setattr(daemon, "_tmux", lambda argv, **kw: types.SimpleNamespace(
        returncode=0, stdout=state.capture, stderr=""))
    def _type_line(pane, text, still_ok=None, **kw):
        # Like the real one: still_ok is checked inside the lock before any keystroke.
        state.attempts += 1
        if state.on_lock:
            state.on_lock()
        if still_ok is not None and not still_ok():
            return "abandoned"
        if state.status in ("sent", "swallowed"):
            state.typed.append((pane, text))
        return state.status

    monkeypatch.setattr(daemon, "type_line", _type_line)
    monkeypatch.setattr(daemon, "log", lambda msg: None)
    monkeypatch.setattr(daemon.time, "time", lambda: state.now)
    daemon._last_stall_check.clear()
    yield state
    daemon._last_stall_check.clear()
    common.update_registry(lambda reg: reg.clear())


def sweep(monkeypatch, n=1):
    """Run n iterations of stall_watch_loop; the (n+1)th poll sleep ends it."""
    calls = {"n": 0}

    def _sleep(secs):
        calls["n"] += 1
        if calls["n"] > n:
            raise KeyboardInterrupt

    monkeypatch.setattr(daemon.time, "sleep", _sleep)
    with pytest.raises(KeyboardInterrupt):
        daemon.stall_watch_loop({})


def test_idle_seat_with_running_shell_is_checked(world, monkeypatch):
    sweep(monkeypatch)
    assert len(world.typed) == 1
    pane, text = world.typed[0]
    assert pane == "%1" and "idle for 25 min" in text and "1 background shell" in text


def test_seat_idle_under_the_threshold_is_left_alone(world, monkeypatch):
    world.idle_s = 19 * 60
    sweep(monkeypatch)
    assert world.typed == []


def test_seat_without_background_work_is_left_alone(world, monkeypatch):
    world.capture = IDLE_NOTHING
    sweep(monkeypatch)
    assert world.typed == []


def test_busy_pane_is_left_alone(world, monkeypatch):
    world.busy = True
    sweep(monkeypatch)
    assert world.typed == []


def test_checks_of_one_topic_are_at_least_the_repeat_apart(world, monkeypatch):
    sweep(monkeypatch)
    world.now += daemon.STALL_REPEAT - 60
    sweep(monkeypatch)
    assert len(world.typed) == 1
    world.now += 60
    sweep(monkeypatch)
    assert len(world.typed) == 2


def test_codex_and_ended_seats_are_skipped(world, monkeypatch):
    monkeypatch.setattr(daemon, "fleet_panes", lambda: [("%1", "s", "123", "codex")])
    sweep(monkeypatch)
    common.update_registry(lambda reg: reg["123"].update(ended=time.strftime("%Y")))
    monkeypatch.setattr(daemon, "fleet_panes", lambda: [("%1", "s", "123", "claude")])
    sweep(monkeypatch)
    assert world.typed == []


def test_one_bad_topic_does_not_stop_the_sweep(world, monkeypatch):
    common.update_registry(lambda reg: reg.update({"9": {"pane": "%9", "engine": "claude"}}))
    real = world.now

    def _activity(tid, info):
        if tid == "9":
            raise RuntimeError("unreadable transcript")
        return real - world.idle_s

    monkeypatch.setattr(daemon, "_park_activity", _activity)
    monkeypatch.setattr(daemon, "fleet_panes",
                        lambda: [("%9", "x", "9", "claude"), ("%1", "s", "123", "claude")])
    sweep(monkeypatch)
    assert [p for p, _ in world.typed] == ["%1"]


def test_a_failed_attempt_is_retried_at_the_next_poll(world, monkeypatch):
    world.status = "failed"
    sweep(monkeypatch, n=2)
    assert world.attempts == 2 and world.typed == []
    world.status = "sent"
    sweep(monkeypatch)
    assert len(world.typed) == 1


def test_a_swallowed_line_waits_out_the_repeat(world, monkeypatch):
    world.status = "swallowed"
    sweep(monkeypatch, n=2)
    assert world.attempts == 1


def test_a_registered_codex_seat_on_a_claude_pane_is_skipped(world, monkeypatch):
    common.update_registry(lambda reg: reg["123"].update(engine="codex"))
    sweep(monkeypatch)
    assert world.attempts == 0


def test_a_turn_that_starts_before_the_keystroke_gets_nothing(world, monkeypatch):
    world.on_lock = lambda: setattr(world, "busy", True)
    sweep(monkeypatch)
    assert world.attempts == 1 and world.typed == []
    assert "123" not in daemon._last_stall_check


def test_a_log_that_raises_does_not_end_the_watch(world, monkeypatch):
    def _boom(msg):
        raise OSError("closed stderr")

    monkeypatch.setattr(daemon, "log", _boom)
    world.status = "failed"
    sweep(monkeypatch, n=3)
    assert world.attempts == 3


@pytest.mark.parametrize("change", [{"engine": "codex"}, {"ended": "now"}, {"pane": "%2"}])
def test_a_registry_change_before_the_keystroke_gets_nothing(world, monkeypatch, change):
    world.on_lock = lambda: common.update_registry(lambda reg: reg["123"].update(change))
    sweep(monkeypatch)
    assert world.attempts == 1 and world.typed == []


def test_a_pane_that_turns_codex_before_the_keystroke_gets_nothing(world, monkeypatch):
    world.on_lock = lambda: setattr(world, "engine", "codex")
    sweep(monkeypatch)
    assert world.attempts == 1 and world.typed == []


def test_an_unreadable_registry_in_the_lock_withholds(world, monkeypatch):
    def _boom():
        raise OSError("registry unreadable")

    world.on_lock = lambda: monkeypatch.setattr(daemon, "read_registry", _boom)
    sweep(monkeypatch)
    assert world.attempts == 1 and world.typed == []
