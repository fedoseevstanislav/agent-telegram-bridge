"""The carry-forward cycle is OFF by default: at the threshold the daemon just compacts.

The owner decided this on 2026-09-21, for every session on the machine: compact automatically
at 50% instead of running the carry-forward cycle, because where the built-in summary carries
the same information the cycle only spends tokens.

The cycle itself is unchanged and still tested (test_carry_forward.py, test_autocf_retry.py) —
it is reachable again by setting TG_BRIDGE_CARRY_FORWARD=1, which is the whole revert. These
tests pin the DEFAULT: no write prompt, no issue record, no carry-forward file, one short
nudge afterwards, and the 50% trigger.
"""

import importlib
import os

from bridge import daemon


def _worker_harness(monkeypatch, *, injected, replies, carry_forward):
    """Drive _carry_forward_worker with every pane interaction stubbed. Phase-1 helpers are
    stubbed to RAISE: under the default they must never be reached at all."""
    monkeypatch.setattr(daemon, "CARRY_FORWARD", carry_forward)
    monkeypatch.setattr(daemon.time, "sleep", lambda *a, **k: None)
    monkeypatch.setattr(daemon, "_cf_cleanup_marker", lambda *a, **k: None)
    monkeypatch.setattr(daemon, "_cf_wait_idle", lambda *a, **k: "idle")
    monkeypatch.setattr(daemon, "pane_alive", lambda pane: True)
    monkeypatch.setattr(daemon, "_cf_wait_compacting", lambda *a, **k: "compacting")
    monkeypatch.setattr(daemon, "_cf_clear_modal", lambda *a, **k: None)
    monkeypatch.setattr(daemon, "_cf_capture_tail", lambda pane: "")
    monkeypatch.setattr(daemon, "_cf_clear_unfinished", lambda *a, **k: None)
    monkeypatch.setattr(daemon, "log", lambda *a, **k: None)
    monkeypatch.setattr(daemon, "reply", lambda cfg, tid, text: replies.append(text))

    def fake_inject(tid, token, pane, text, settle=0.5, release_after=False):
        injected.append(text)
        if release_after:
            daemon._pending_cf.pop(str(tid), None)
        return True
    monkeypatch.setattr(daemon, "_cf_inject_owned", fake_inject)
    monkeypatch.setattr(daemon, "_pending_cf",
                        {"7033": {"token": "t", "pane": "%0", "phase": "write"}})


def test_the_default_compacts_without_writing_a_carry_forward(monkeypatch):
    injected, replies = [], []
    _worker_harness(monkeypatch, injected=injected, replies=replies, carry_forward=False)
    # Phase 1 must not run: reaching either of these is the defect.
    monkeypatch.setattr(daemon, "_cf_wait_done",
                        lambda *a, **k: pytest_fail("waited for a carry-forward marker"))
    monkeypatch.setattr(daemon, "_cf_verify_or_create_issue",
                        lambda *a, **k: pytest_fail("recorded a carry-forward issue"))

    daemon._carry_forward_worker({}, 7033, "%0", "/x/cf.md", "/x/cf.md.done", "t", "sess")

    assert "/compact" in injected
    assert not any(daemon.CF_WRITE_PROMPT[:40] in t for t in injected)
    assert daemon.COMPACT_RESUME_PROMPT in injected
    assert not any(t.startswith("[tg-bridge carry-forward resume]") for t in injected)
    assert not daemon.carry_forward_active(7033)          # kill-switch disarmed
    assert any("Compaction done" in r for r in replies)
    assert not any("carry-forward" in r.lower() for r in replies)


def test_the_nudge_is_one_short_line_that_hands_control_back(monkeypatch):
    # The owner's reason for dropping the cycle was token cost, so the replacement may not
    # quietly become another essay; and it must not order the seat around the way
    # CF_RESUME_PROMPT did ("execute its next steps now"), which is what buried the seat's
    # own plan. It re-anchors and returns control.
    assert len(daemon.COMPACT_RESUME_PROMPT) < 400
    assert "Compaction is complete" in daemon.COMPACT_RESUME_PROMPT
    assert "steer or stop you at any time" in daemon.COMPACT_RESUME_PROMPT
    assert "next steps" not in daemon.COMPACT_RESUME_PROMPT


def test_help_describes_what_actually_happens():
    # The reviewer of PR #336 caught this: both reply() sites were branched on CARRY_FORWARD
    # and /help was not, so the owner typing /help was told the session would write a
    # carry-forward and resume from its next-steps — neither of which happens any more.
    cf_line = next(l for l in daemon.HELP_TEXT.splitlines() if l.startswith("/carryforward"))
    assert "next-steps" not in cf_line
    assert "GitHub issue" not in cf_line
    assert "/compact" in cf_line
    assert "Send any message to halt." in cf_line          # the kill-switch still applies
    assert daemon._HELP_CF_CYCLE in daemon.HELP_TEXT.replace(cf_line, daemon._HELP_CF_CYCLE)


def test_the_flag_brings_the_whole_cycle_back(monkeypatch):
    injected, replies = [], []
    _worker_harness(monkeypatch, injected=injected, replies=replies, carry_forward=True)
    monkeypatch.setattr(daemon, "_cf_wait_done", lambda *a, **k: "done")
    recorded = []
    monkeypatch.setattr(daemon, "_cf_verify_or_create_issue",
                        lambda *a, **k: recorded.append(a) or ("owner/repo#1", "session"))

    daemon._carry_forward_worker({}, 7033, "%0", "/x/cf.md", "/x/cf.md.done", "t", "sess")

    assert any(t.startswith("[tg-bridge carry-forward]") for t in injected)   # write prompt
    assert recorded                                                          # issue recorded
    assert "/compact" in injected
    assert any(t.startswith("[tg-bridge carry-forward resume]") for t in injected)
    assert any("Carry-forward complete" in r for r in replies)


def test_the_env_flag_pairs_the_cycle_with_its_own_threshold(monkeypatch):
    # One switch is the whole revert: TG_BRIDGE_CARRY_FORWARD=1 restores the cycle AND the
    # 60% trigger it ran on, so reverting cannot leave a half-restored machine.
    try:
        for value, cycle, pct in (("1", True, 60), ("true", True, 60),
                                  ("0", False, 50), ("off", False, 50), (None, False, 50)):
            if value is None:
                os.environ.pop("TG_BRIDGE_CARRY_FORWARD", None)
            else:
                os.environ["TG_BRIDGE_CARRY_FORWARD"] = value
            mod = importlib.reload(daemon)
            assert mod.CARRY_FORWARD is cycle, value
            assert mod.AUTOCF_PCT == pct, value
    finally:
        os.environ.pop("TG_BRIDGE_CARRY_FORWARD", None)
        importlib.reload(daemon)


def pytest_fail(msg):
    raise AssertionError(msg)
