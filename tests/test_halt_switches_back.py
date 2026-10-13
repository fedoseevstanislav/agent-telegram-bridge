"""An owner message that halts a compaction switches the model back first (#397).

A live topic, 2026-10-12: the auto-compaction moved the session to Sonnet 5.5 xhigh at
15:33:34Z, and the owner's message halted it at 15:34:43Z. #353 C7 had made a halt stop all
typing, including the switch back. So the session answered the owner on Sonnet until it was
switched back by hand at 15:41Z. The owner's rule (15:44Z): "we go back to the previous model
first".
"""

import pytest

from bridge import daemon
from tests.test_compact_on_sonnet import (FakeClaude, PREV_EFFORT, PREV_MODEL, SETTINGS_BYTES,
                                          _install)

NOTICE = "[tg-bridge] Your carry-forward was cancelled"


class _Threads:
    """Stands in for threading.Thread: records each start so the test runs it when it says."""
    def __init__(self):
        self.started = []

    def __call__(self, target, args=(), daemon=None):
        threads = self

        class _T:
            def start(self):
                threads.started.append((target, args))
        return _T()

    def run_all(self):
        for target, args in self.started:
            target(*args)


@pytest.fixture
def env(tmp_path, monkeypatch):
    fake = FakeClaude(tmp_path)
    _install(monkeypatch, fake)
    replies, threads = [], _Threads()
    monkeypatch.setattr(daemon.threading, "Thread", threads)
    monkeypatch.setattr(daemon, "reply", lambda cfg, tid, text: replies.append(text) or True)
    monkeypatch.setattr(daemon, "load_config", lambda: {})

    def flow(now=(daemon.COMPACT_MODEL, daemon.COMPACT_EFFORT), sent=None, plan=True):
        st = {"token": "t", "pane": "%0", "phase": "compact"}
        if plan:
            st["model_plan"] = {"model": PREV_MODEL, "effort": PREV_EFFORT,
                                "tpath": fake.transcript, "sent": sent,
                                "now": {"model": now[0], "effort": now[1]}}
        monkeypatch.setattr(daemon, "_pending_cf", {"7033": st})
        return st
    return fake, replies, threads, flow


def _models(fake):
    return [t for t in fake.typed if t.startswith(("/model", "/effort"))]


def test_a_halt_after_the_switch_switches_back_then_tells_the_session(env):
    """C1, C6: /model and /effort back to the session's own pair, then the notice. The
    settings file is put back byte for byte."""
    fake, replies, threads, flow = env
    flow()

    assert daemon.halt_carry_forward({}, 7033, "owner message") is True
    threads.run_all()

    assert _models(fake) == [f"/model {PREV_MODEL}", f"/effort {PREV_EFFORT}"]
    assert fake.typed[-1].startswith(NOTICE), "the notice must come after the switch back"
    assert fake.settings.read_bytes() == SETTINGS_BYTES
    halt = [r for r in replies if r.startswith("🛑 Carry-forward halted")]
    assert len(halt) == 1
    assert f"Switching the session back to `{PREV_MODEL}` at `{PREV_EFFORT}`" in halt[0]
    assert not any("could not confirm" in r for r in replies)


def test_a_sent_but_unconfirmed_switch_is_also_undone(env):
    """C1: `now` still shows the old pair, but a /model was sent; its effect is unknown."""
    fake, replies, threads, flow = env
    flow(now=(PREV_MODEL, PREV_EFFORT), sent=f"/model {daemon.COMPACT_MODEL}")

    daemon.halt_carry_forward({}, 7033, "owner message")
    threads.run_all()

    assert _models(fake) == [f"/model {PREV_MODEL}", f"/effort {PREV_EFFORT}"]


@pytest.mark.parametrize("plan", [True, False])
def test_a_halt_before_any_switch_types_no_model_command(env, plan):
    """C2: nothing to undo, so the notice is typed at once, as before #397."""
    fake, replies, threads, flow = env
    flow(now=(PREV_MODEL, PREV_EFFORT), plan=plan)

    daemon.halt_carry_forward({}, 7033, "owner message")

    assert threads.started == []
    assert _models(fake) == []
    assert len(fake.typed) == 1 and fake.typed[0].startswith(NOTICE)
    assert not any("Switching the session back" in r for r in replies)


def test_the_halt_returns_before_the_switch_back_runs(env):
    """C3, A3: the halt is called from the getUpdates loop; the switch back waits for idle and
    for each confirmation, so it must not run there."""
    fake, replies, threads, flow = env
    flow()

    daemon.halt_carry_forward({}, 7033, "owner message")

    assert len(threads.started) == 1
    assert fake.typed == [], "the halt typed on the caller's thread"
    assert len(replies) == 1


def test_a_new_flow_on_the_topic_pre_empts_the_switch_back_and_the_notice(env, monkeypatch):
    """C4: the new flow switches the model and instructs the session itself."""
    fake, replies, threads, flow = env
    flow()
    daemon.halt_carry_forward({}, 7033, "owner message")
    daemon._pending_cf["7033"] = {"token": "t2", "pane": "%0", "phase": "settle"}

    threads.run_all()

    assert fake.typed == []
    assert len(replies) == 1, "a pre-empted switch back sends no extra notice"


def test_a_dead_pane_gets_nothing_typed_and_the_owner_is_told(env, monkeypatch):
    """C4, C5 (review r1): a dead pane is not a new flow; the owner still learns where the
    session was left."""
    fake, replies, threads, flow = env
    flow()
    daemon.halt_carry_forward({}, 7033, "owner message")
    monkeypatch.setattr(daemon, "pane_alive", lambda p: False)

    threads.run_all()

    assert fake.typed == []
    warned = [r for r in replies if r.startswith("⚠️ The session's terminal closed")]
    assert len(warned) == 1
    assert f"/model {PREV_MODEL}" in warned[0]


def test_a_new_flow_right_after_the_switch_back_gets_no_cancellation_notice(env, monkeypatch):
    """C4 (review r1): the check and the keystrokes sit under one _cf_lock hold, so a flow that
    starts after the switch back either is seen, or starts after the notice was typed."""
    fake, replies, threads, flow = env
    flow()
    real = daemon.restore_after_compaction

    def restore_then_new_flow(*a, **k):
        out = real(*a, **k)
        daemon._pending_cf["7033"] = {"token": "t2", "pane": "%0", "phase": "settle"}
        return out
    monkeypatch.setattr(daemon, "restore_after_compaction", restore_then_new_flow)
    daemon.halt_carry_forward({}, 7033, "owner message")

    threads.run_all()

    assert _models(fake) == [f"/model {PREV_MODEL}", f"/effort {PREV_EFFORT}"]
    assert not any(t.startswith(NOTICE) for t in fake.typed)
    assert len(replies) == 1


def test_a_new_flow_after_the_settle_timeout_gets_no_warning_and_no_notice(env, monkeypatch):
    """C5 (review r1): the pane never settles, and a new flow starts once the wait is over.
    The decision is made under _cf_lock, so the new flow is seen."""
    fake, replies, threads, flow = env
    flow()
    monkeypatch.setattr(daemon, "_cf_busy", lambda pane: True)
    now = {"t": 0}

    def clock():
        now["t"] += 5
        if now["t"] > daemon.CF_SETTLE_WAIT and "7033" not in daemon._pending_cf:
            daemon._pending_cf["7033"] = {"token": "t2", "pane": "%0", "phase": "settle"}
        return now["t"]
    monkeypatch.setattr(daemon.time, "time", clock)
    daemon.halt_carry_forward({}, 7033, "owner message")

    threads.run_all()

    assert fake.typed == []
    assert len(replies) == 1


def test_a_pane_that_never_settles_is_not_switched_and_the_owner_is_told(env, monkeypatch):
    """C4, C5: /model into a running turn is not a command; say where the session is left."""
    fake, replies, threads, flow = env
    flow()
    monkeypatch.setattr(daemon, "_cf_busy", lambda pane: True)
    clock = iter(range(0, 10_000, 5))
    monkeypatch.setattr(daemon.time, "time", lambda: next(clock))

    daemon.halt_carry_forward({}, 7033, "owner message")
    threads.run_all()

    assert _models(fake) == []
    warned = [r for r in replies if r.startswith("⚠️ The session did not go idle")]
    assert len(warned) == 1
    assert f"`{daemon.COMPACT_MODEL}` at `{daemon.COMPACT_EFFORT}` effort" in warned[0]
    assert f"/model {PREV_MODEL}" in warned[0]
    assert fake.typed[-1].startswith(NOTICE)


def test_an_unconfirmed_switch_back_names_the_model_left_on_once(tmp_path, monkeypatch):
    """C5: the client never confirms; one notice, worded for a halt."""
    fake = FakeClaude(tmp_path, confirm=False)
    _install(monkeypatch, fake)
    replies, threads = [], _Threads()
    monkeypatch.setattr(daemon.threading, "Thread", threads)
    monkeypatch.setattr(daemon, "reply", lambda cfg, tid, text: replies.append(text) or True)
    monkeypatch.setattr(daemon, "load_config", lambda: {})
    monkeypatch.setattr(daemon, "_pending_cf", {"7033": {
        "token": "t", "pane": "%0", "phase": "compact",
        "model_plan": {"model": PREV_MODEL, "effort": PREV_EFFORT, "tpath": fake.transcript,
                       "sent": None, "now": {"model": daemon.COMPACT_MODEL,
                                             "effort": daemon.COMPACT_EFFORT}}}})

    clock = iter(range(0, 100_000, 1))
    monkeypatch.setattr(daemon.time, "time", lambda: next(clock))

    daemon.halt_carry_forward({}, 7033, "owner message")
    threads.run_all()

    failed = [r for r in replies if "could not confirm" in r]
    assert len(failed) == 1
    assert failed[0].startswith("⚠️ Your message cancelled the compaction")
    assert f"/model {PREV_MODEL}" in failed[0]


@pytest.mark.parametrize("dies", [False, True])
def test_an_error_in_the_switch_back_still_tells_the_owner(env, monkeypatch, dies):
    """C5, C8 (review r2): a raising switch back is not "done". The owner gets one notice
    naming the model left on, whether or not the pane then dies."""
    fake, replies, threads, flow = env
    flow()

    def boom(*a, **k):
        if dies:
            monkeypatch.setattr(daemon, "pane_alive", lambda p: False)
        raise PermissionError("settings.json")
    monkeypatch.setattr(daemon, "restore_after_compaction", boom)
    daemon.halt_carry_forward({}, 7033, "owner message")

    threads.run_all()

    warned = [r for r in replies if r.startswith("⚠️ ")]
    assert len(warned) == 1
    assert f"/model {PREV_MODEL}" in warned[0]
    assert any(t.startswith(NOTICE) for t in fake.typed) is (not dies)


def test_a_final_check_that_raises_still_tells_the_owner_once(env, monkeypatch):
    """C5 (review r3): restore raises, then the final pane check raises too. Nothing escapes,
    and the owner gets one notice."""
    import subprocess
    fake, replies, threads, flow = env
    flow()

    def boom(*a, **k):
        def tmux_timeout(p):
            raise subprocess.TimeoutExpired("tmux", 5)
        monkeypatch.setattr(daemon, "pane_alive", tmux_timeout)
        raise PermissionError("settings.json")
    monkeypatch.setattr(daemon, "restore_after_compaction", boom)
    daemon.halt_carry_forward({}, 7033, "owner message")

    threads.run_all()  # must not raise

    warned = [r for r in replies if r.startswith("⚠️ ")]
    assert len(warned) == 1 and "failed with an error" in warned[0]


def test_a_restore_that_warned_and_then_raised_is_not_warned_twice(env, monkeypatch):
    """C5 (review r3): the restore sent its own "could not confirm" notice, then raised."""
    fake, replies, threads, flow = env
    flow()
    real = daemon.restore_after_compaction

    def warn_then_raise(pane, tid, plan, guard, **k):
        monkeypatch.setattr(daemon, "_run_model_steps", lambda *a, **kw: ("failed", True))
        real(pane, tid, plan, guard, **k)
        raise RuntimeError("after the notice")
    monkeypatch.setattr(daemon, "restore_after_compaction", warn_then_raise)
    daemon.halt_carry_forward({}, 7033, "owner message")

    threads.run_all()

    warned = [r for r in replies if r.startswith("⚠️ ")]
    assert len(warned) == 1 and "could not confirm" in warned[0]


def test_an_interrupted_switch_back_with_a_live_pane_is_reported(env, monkeypatch):
    """C5: the guard said no (a dead-pane blip), the final check finds the pane alive and no
    new flow. The owner is told, in words that do not claim an error or a closed terminal."""
    fake, replies, threads, flow = env
    flow()
    calls = {"n": 0}

    def blip(p):
        calls["n"] += 1
        return calls["n"] > 1
    daemon.halt_carry_forward({}, 7033, "owner message")
    monkeypatch.setattr(daemon, "pane_alive", blip)

    threads.run_all()

    assert _models(fake) == []
    warned = [r for r in replies if r.startswith("⚠️ ")]
    assert len(warned) == 1 and "was interrupted" in warned[0]
    assert fake.typed[-1].startswith(NOTICE)
