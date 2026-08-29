"""#110: every tmux call must be bounded by a timeout, and a hung tmux send-keys must never
leave _cf_lock held — that lock is taken by the main getUpdates loop on every message, so a
tmux call that hangs while a carry-forward holds the lock froze the entire daemon."""

import subprocess
import types

from bridge import daemon


# ---- _tmux bounds every call -------------------------------------------------

def test_tmux_defaults_timeout(monkeypatch):
    seen = {}
    def fake_run(argv, **kw):
        seen["argv"] = argv
        seen["kw"] = kw
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")
    monkeypatch.setattr(daemon.subprocess, "run", fake_run)
    daemon._tmux(["tmux", "list-panes", "-t", "%1"], capture_output=True)
    assert seen["argv"][0] == "tmux"
    assert seen["kw"]["timeout"] == daemon.TMUX_TIMEOUT   # bounded
    assert seen["kw"]["capture_output"] is True           # other kwargs passed through


def test_tmux_respects_explicit_timeout(monkeypatch):
    seen = {}
    monkeypatch.setattr(daemon.subprocess, "run",
                        lambda argv, **kw: seen.update(kw) or types.SimpleNamespace(returncode=0))
    daemon._tmux(["tmux", "x"], timeout=99)
    assert seen["timeout"] == 99                          # setdefault doesn't clobber


def test_no_raw_tmux_subprocess_calls_remain():
    # a new raw `subprocess.run(["tmux"...])` would reintroduce an unbounded (freeze-capable)
    # call; every tmux invocation must route through _tmux.
    import os
    src = open(os.path.join(os.path.dirname(__file__), "..", "bridge", "daemon.py")).read()
    assert 'subprocess.run(["tmux"' not in src
    # and the multi-line form: no `subprocess.run(` line immediately followed by `["tmux"`
    lines = src.splitlines()
    for i, ln in enumerate(lines[:-1]):
        if ln.rstrip().endswith("subprocess.run("):
            assert not lines[i + 1].lstrip().startswith('["tmux"'), \
                f"unbounded multi-line tmux call near line {i+1}"


# ---- the freeze itself: _cf_lock must be released even when tmux hangs --------

def test_cf_inject_releases_lock_on_tmux_timeout(monkeypatch):
    tid = "987654"
    daemon._pending_cf[tid] = {"token": "tok", "pane": "%9"}
    try:
        def boom(argv, **kw):
            raise subprocess.TimeoutExpired(cmd="tmux", timeout=daemon.TMUX_TIMEOUT)
        monkeypatch.setattr(daemon, "_tmux", boom)

        result = daemon._cf_inject_owned(tid, "tok", "%9", "some text")
        assert result is False                              # inject reported failure cleanly

        # THE fix: the lock the main loop needs must be free after a hung tmux inject.
        got = daemon._cf_lock.acquire(blocking=False)
        assert got, "_cf_lock left held after a timed-out inject — the getUpdates loop would freeze"
        daemon._cf_lock.release()
    finally:
        daemon._pending_cf.pop(tid, None)


def test_cf_clear_modal_releases_lock_on_tmux_timeout(monkeypatch):
    # _cf_clear_modal sends Enter under _cf_lock too; a hung send-keys there must also free
    # the lock (#110, round-2 review NIT).
    tid = "434343"
    daemon._pending_cf[tid] = {"token": "tok", "pane": "%9"}
    monkeypatch.setattr(daemon, "_cf_modal_present", lambda text: True)   # force the under-lock path
    calls = {"n": 0}
    def fake(argv, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            return types.SimpleNamespace(returncode=0, stdout="modal")    # capture-pane ok
        raise subprocess.TimeoutExpired(cmd="tmux", timeout=daemon.TMUX_TIMEOUT)  # the Enter hangs
    monkeypatch.setattr(daemon, "_tmux", fake)
    try:
        daemon._cf_clear_modal(tid, "tok", "%9")                          # must not raise
        got = daemon._cf_lock.acquire(blocking=False)
        assert got, "_cf_lock left held after a timed-out modal-clear"
        daemon._cf_lock.release()
    finally:
        daemon._pending_cf.pop(tid, None)


def test_halt_carry_forward_survives_tmux_timeout(monkeypatch):
    # halt runs in the main-message path; a hung pane-interrupt must NOT skip the user reply
    # or propagate (#110). The flow is already popped, so the halt has functionally succeeded.
    tid = "424242"
    daemon._pending_cf[tid] = {"token": "t", "pane": "%9", "phase": "compact"}
    replies = []
    monkeypatch.setattr(daemon, "pane_alive", lambda p: True)
    monkeypatch.setattr(daemon, "reply", lambda cfg, t, text: replies.append(text))
    def boom(argv, **kw):
        raise subprocess.TimeoutExpired(cmd="tmux", timeout=daemon.TMUX_TIMEOUT)
    monkeypatch.setattr(daemon, "_tmux", boom)
    try:
        result = daemon.halt_carry_forward({}, int(tid), "user message")
        assert result is True                          # halt reported success, did NOT raise
        assert any("halted" in r for r in replies)     # user still got the confirmation
        assert tid not in daemon._pending_cf           # flow popped
        got = daemon._cf_lock.acquire(blocking=False)
        assert got
        daemon._cf_lock.release()
    finally:
        daemon._pending_cf.pop(tid, None)


def test_cf_inject_wrong_token_frees_lock():
    # The ownership-mismatch early return must also leave the lock free
    tid = "987655"
    daemon._pending_cf[tid] = {"token": "REAL", "pane": "%9"}
    try:
        assert daemon._cf_inject_owned(tid, "WRONG", "%9", "text") is False
        got = daemon._cf_lock.acquire(blocking=False)
        assert got
        daemon._cf_lock.release()
    finally:
        daemon._pending_cf.pop(tid, None)
