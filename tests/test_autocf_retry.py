"""#239 — a carry-forward that ends before compaction must not disable auto-CF forever.

The armed flag is only ever cleared by context falling below the re-arm point, and that
drop is produced by the compaction. A run that dies before compacting therefore leaves the
topic armed with nothing that can ever unarm it: auto carry-forward is finished for the life
of the session. These tests pin both halves of the fix — the worker recording the outcome,
and `_process_autocf` acting on that record exactly once per cooldown.
"""

import json
import time

from bridge import daemon


class _StopLoop(Exception):
    """Breaks warning_loop's `while True` from its sleep, which is outside the body's
    `except Exception` — so one poll runs and the loop exits without being restructured."""


def _run_one_warning_poll(monkeypatch, registry, engine="claude"):
    """Run one poll of warning_loop over `registry`; return the topics it fired."""
    sleeps = {"n": 0}

    def fake_sleep(_seconds):
        sleeps["n"] += 1
        if sleeps["n"] > 1:
            raise _StopLoop
    monkeypatch.setattr(daemon.time, "sleep", fake_sleep)
    monkeypatch.setattr(daemon, "read_registry", lambda: registry)
    monkeypatch.setattr(daemon, "load_autocf_exempt", lambda: set())
    monkeypatch.setattr(daemon, "engine_of_pane", lambda pane: engine)
    monkeypatch.setattr(daemon, "context_for", lambda pane, eng=None: {"pct": 95})
    monkeypatch.setattr(daemon, "carry_forward_active", lambda tid: False)
    fired = []
    monkeypatch.setattr(daemon, "handle_carry_forward",
                        lambda cfg, tid, cmd, info, pane: bool(fired.append(tid)) or True)
    monkeypatch.setattr(daemon, "reply", lambda *a, **k: True)
    monkeypatch.setattr(daemon, "log", lambda *a, **k: None)
    try:
        daemon.warning_loop({})
    except _StopLoop:
        pass
    return fired


# ---- worker: what it records on the way out ----------------------------------

def _worker_harness(monkeypatch, *, replies, wait_idle="idle", wait_done="done",
                    compacting="compacting"):
    """Stub every pane-driving dependency of _carry_forward_worker. The REAL _cf_release,
    _cf_owns and the #239 record helpers stay in place — those are what is under test."""
    monkeypatch.setattr(daemon.time, "sleep", lambda *a, **k: None)
    monkeypatch.setattr(daemon, "_cf_cleanup_marker", lambda *a, **k: None)
    monkeypatch.setattr(daemon, "_cf_wait_idle", lambda *a, **k: wait_idle)
    monkeypatch.setattr(daemon, "pane_alive", lambda pane: True)
    monkeypatch.setattr(daemon, "_cf_wait_done", lambda *a, **k: wait_done)
    monkeypatch.setattr(daemon, "_cf_verify_or_create_issue",
                        lambda *a, **k: ("owner/repo#1", "session"))
    monkeypatch.setattr(daemon, "_cf_wait_compacting", lambda *a, **k: compacting)
    monkeypatch.setattr(daemon, "_cf_clear_modal", lambda *a, **k: None)
    monkeypatch.setattr(daemon, "_cf_hook_block_reason", lambda pane, before: None)
    monkeypatch.setattr(daemon, "log", lambda *a, **k: None)

    def fake_inject(tid, token, pane, text, settle=0.5, release_after=False):
        if release_after:
            daemon._pending_cf.pop(str(tid), None)
        return True
    monkeypatch.setattr(daemon, "_cf_inject_owned", fake_inject)
    monkeypatch.setattr(daemon, "reply", lambda cfg, tid, text: replies.append(text))
    monkeypatch.setattr(daemon, "_pending_cf",
                        {"55": {"token": "t", "pane": "%0", "phase": "write"}})


def _run_worker(token="t"):
    """Run the worker exactly as handle_carry_forward would: record opened, then the flow."""
    daemon._cf_mark_unfinished("55", token)
    daemon._carry_forward_worker({}, 55, "%0", "/x/cf.md", "/x/cf.md.done", token, "sess")


def test_starting_a_carry_forward_opens_the_record(monkeypatch):
    # The line the rest of this file depends on. Every other test opens the record the way
    # handle_carry_forward does; without this one, deleting that call from production code
    # breaks nothing — which is exactly how it went unnoticed until a mutation run.
    monkeypatch.setattr(daemon, "pane_alive", lambda pane: True)
    monkeypatch.setattr(daemon, "engine_of_pane", lambda pane: "claude")
    monkeypatch.setattr(daemon, "reply", lambda *a, **k: True)
    monkeypatch.setattr(daemon, "log", lambda *a, **k: None)
    # The assertion is made INSIDE start(): "the record exists by the end of the function"
    # would still pass with the call moved after the worker was launched, which is a window
    # in which a run exists with nothing on disk saying so (#240 review r3, finding 10).
    at_start = []
    monkeypatch.setattr(daemon.threading, "Thread",
                        lambda **kw: type("T", (), {
                            "start": lambda _s: at_start.append(daemon._cf_unfinished_since("55"))
                        })())
    monkeypatch.setattr(daemon, "_pending_cf", {})
    assert daemon.handle_carry_forward({}, 55, "/carryforward", {"name": "s"}, "%0") is True
    assert len(at_start) == 1 and at_start[0] is not None    # open BEFORE the worker existed


def test_a_run_whose_record_cannot_be_written_does_not_start(monkeypatch):
    # A run with no record is one whose failure can never re-arm the topic — silently the
    # #239 state again. If the state directory is unwritable, say so and start nothing
    # (#240 review r3, finding 2).
    monkeypatch.setattr(daemon, "pane_alive", lambda pane: True)
    monkeypatch.setattr(daemon, "engine_of_pane", lambda pane: "claude")
    replies = []
    monkeypatch.setattr(daemon, "reply", lambda cfg, tid, text: replies.append(text) or True)
    monkeypatch.setattr(daemon, "log", lambda *a, **k: None)
    monkeypatch.setattr(daemon, "_cf_mark_unfinished", lambda tid, token: False)
    started = []
    monkeypatch.setattr(daemon.threading, "Thread",
                        lambda **kw: type("T", (), {"start": lambda _s: started.append(1)})())
    monkeypatch.setattr(daemon, "_pending_cf", {})
    assert daemon.handle_carry_forward({}, 55, "/carryforward", {"name": "s"}, "%0") is False
    assert started == []                                  # no worker
    assert not daemon.carry_forward_active(55)            # and no pending flow left behind
    assert any("state directory" in r for r in replies)


def test_a_worker_thread_that_will_not_start_leaves_nothing_behind(monkeypatch):
    # Thread creation can fail outright under resource exhaustion. The topic would otherwise
    # keep a pending flow no worker will ever release, plus a record for a run that never
    # happened (#240 review r3, finding 6).
    monkeypatch.setattr(daemon, "pane_alive", lambda pane: True)
    monkeypatch.setattr(daemon, "engine_of_pane", lambda pane: "claude")
    monkeypatch.setattr(daemon, "reply", lambda *a, **k: True)
    monkeypatch.setattr(daemon, "log", lambda *a, **k: None)

    def wont_start(**kw):
        raise RuntimeError("can't start new thread")
    monkeypatch.setattr(daemon.threading, "Thread", wont_start)
    monkeypatch.setattr(daemon, "_pending_cf", {})
    assert daemon.handle_carry_forward({}, 55, "/carryforward", {"name": "s"}, "%0") is False
    assert daemon._cf_unfinished_since("55") is None
    assert not daemon.carry_forward_active(55)


def test_a_carry_forward_that_never_starts_opens_no_record(monkeypatch):
    # The start notice could not be delivered, so no run exists (#161). A record here would
    # grant a retry for a carry-forward that never happened.
    monkeypatch.setattr(daemon, "pane_alive", lambda pane: True)
    monkeypatch.setattr(daemon, "engine_of_pane", lambda pane: "claude")
    monkeypatch.setattr(daemon, "reply", lambda *a, **k: False)      # undeliverable topic
    monkeypatch.setattr(daemon, "log", lambda *a, **k: None)
    monkeypatch.setattr(daemon, "_pending_cf", {})
    assert daemon.handle_carry_forward({}, 55, "/carryforward", {"name": "s"}, "%0") is False
    assert daemon._cf_unfinished_since("55") is None


def test_settle_timeout_leaves_the_record_open(monkeypatch):
    # THE reported case: the session was mid-turn for the whole settle window, so the run
    # ends in phase 1 having changed no context at all.
    replies = []
    _worker_harness(monkeypatch, replies=replies, wait_idle="timeout")
    _run_worker()
    assert daemon._cf_unfinished_since("55") is not None


def test_the_abort_notice_promises_the_retry_and_still_offers_the_manual_one(monkeypatch):
    # C5. The old text ("Try /cf again once it's idle") was written for a bridge that never
    # retried; keeping it would send the owner to do by hand what is already coming.
    replies = []
    _worker_harness(monkeypatch, replies=replies, wait_idle="timeout")
    _run_worker()
    [notice] = [r for r in replies if "stayed mid-turn" in r]
    assert "aborted" in notice
    assert "/cf" in notice                      # the manual route is still named
    assert "again on its own" in notice         # ...and so is the automatic one


def test_an_exempt_topic_is_not_promised_a_retry_it_will_never_get(monkeypatch):
    # The other direction of C5, and the mutant that survived round 2: an exempt topic never
    # auto-fires, so "the daemon will try again on its own" would be a promise the exemption
    # itself prevents. Round 2's mutation run only checked the wording could not become
    # unconditionally MANUAL; it could still have become unconditionally AUTOMATIC.
    replies = []
    _worker_harness(monkeypatch, replies=replies, wait_idle="timeout")
    monkeypatch.setattr(daemon, "load_autocf_exempt", lambda: {"55"})
    _run_worker()
    [notice] = [r for r in replies if "stayed mid-turn" in r]
    assert "/cf" in notice
    assert "again on its own" not in notice


def test_the_abort_notice_survives_an_unreadable_exemption_file(monkeypatch):
    # C5 says the notice SHALL fire. Working out which wording to use must not be able to
    # stop it — json.load can raise RecursionError, which load_autocf_exempt does not catch
    # and the worker's outer handler would turn into silence (#240 review r2, finding 4).
    replies = []
    _worker_harness(monkeypatch, replies=replies, wait_idle="timeout")

    def explode():
        raise RecursionError("maximum recursion depth exceeded")
    monkeypatch.setattr(daemon, "load_autocf_exempt", explode)
    _run_worker()
    [notice] = [r for r in replies if "stayed mid-turn" in r]
    assert "/cf" in notice


def test_write_timeout_leaves_the_record_open(monkeypatch):
    # A different phase-1 exit: the session took the prompt but never signalled completion.
    replies = []
    _worker_harness(monkeypatch, replies=replies, wait_done="timeout")
    monkeypatch.setattr(daemon, "_tmux", lambda *a, **k: None)
    _run_worker()
    assert daemon._cf_unfinished_since("55") is not None


def test_compact_that_never_starts_leaves_the_record_open(monkeypatch):
    # A phase-2 exit: /compact was injected CF_COMPACT_TRIES times and never took.
    replies = []
    _worker_harness(monkeypatch, replies=replies, compacting="timeout")
    _run_worker()
    assert daemon._cf_unfinished_since("55") is not None


def test_an_exception_leaves_the_record_open(monkeypatch):
    # Nothing has to run for the record to survive — which is the point: the exception path,
    # like a kill -9, simply never reaches the line that would close it.
    replies = []
    _worker_harness(monkeypatch, replies=replies)
    monkeypatch.setattr(daemon, "_cf_wait_done", lambda *a, **k: 1 / 0)
    _run_worker()
    assert daemon._cf_unfinished_since("55") is not None


def test_a_run_that_compacts_closes_its_record(monkeypatch):
    # C3: the run did compact, so it is not the case #239 is about, and the hysteresis is
    # left to do its ordinary job. A failure record here would grant an unearned retry.
    replies = []
    _worker_harness(monkeypatch, replies=replies)
    _run_worker()
    assert any("Carry-forward complete" in r for r in replies)   # really the happy path
    assert daemon._cf_unfinished_since("55") is None


def test_a_run_that_compacts_does_not_close_another_runs_record(monkeypatch):
    # A compacted run closes ITS record. An earlier run's, opened under a different token,
    # is a retry someone else is still owed and must survive (#240 review r2, finding 2).
    replies = []
    daemon._cf_mark_unfinished("55", "older-run")
    _worker_harness(monkeypatch, replies=replies)
    daemon._carry_forward_worker({}, 55, "%0", "/x/cf.md", "/x/cf.md.done", "t", "sess")
    assert any("Carry-forward complete" in r for r in replies)   # it really compacted
    assert daemon._cf_unfinished_since("55") is not None


def test_the_record_closes_before_the_resume_not_after_the_run(monkeypatch):
    # Between confirming the compaction and the end of the run lie a settle, an injection and
    # a reply. A daemon killed in that window must not leave an open record for a run that
    # demonstrably compacted (#240 review r3, finding 3) — so the close happens on the line
    # where the compaction is confirmed, and by the time the resume prompt is typed the record
    # is already gone.
    replies = []
    _worker_harness(monkeypatch, replies=replies)
    at_inject = []
    real_inject = daemon._cf_inject_owned

    def watching_inject(tid, token, pane, text, settle=0.5, release_after=False):
        if text == daemon.CF_RESUME_PROMPT.format(path="/x/cf.md"):
            at_inject.append(daemon._cf_unfinished_since("55"))
        return real_inject(tid, token, pane, text, settle=settle, release_after=release_after)
    monkeypatch.setattr(daemon, "_cf_inject_owned", watching_inject)
    _run_worker()
    assert at_inject == [None]      # already closed when the session was resumed


def test_a_recent_run_keeps_the_topic_armed_even_if_the_flag_was_lost(monkeypatch):
    # autocf.json is persisted at the END of a poll, so a daemon killed between firing a run
    # and that write comes back with the flag missing. Without the record standing in for it,
    # the very first tick fires again — once per crash, cooldown bypassed (#240 review r3,
    # finding 4).
    rec = _patch_side_effects(monkeypatch)
    monkeypatch.setattr(daemon, "AUTOCF_RETRY_COOLDOWN", 900)
    _aged("55", 60)                      # a run started a minute ago; the daemon then died
    armed = {}                           # ...and the flag never made it to disk
    assert daemon._process_autocf({}, "55", {}, "%0", 95, "claude", set(), armed) is False
    assert rec["cf"] == []
    assert armed["55"] is True           # the record is holding the topic armed


def test_record_writes_and_deletes_happen_under_the_lock(monkeypatch):
    # A missing lock produces no symptom until two threads interleave, so a race test would
    # be flaky and a mutation run finds nothing. Assert the property directly instead: the
    # filesystem operation itself runs inside the critical section. Without it, a worker
    # closing its own record can read its token, lose the file to a newer run, and delete
    # that newer run's record — leaving a failed run with no retry (#240 review r3, finding 1).
    held = []
    real_replace, real_unlink = daemon.os.replace, daemon.os.unlink
    monkeypatch.setattr(daemon.os, "replace",
                        lambda a, b: held.append(daemon._cf_record_lock.locked()) or real_replace(a, b))
    monkeypatch.setattr(daemon.os, "unlink",
                        lambda a: held.append(daemon._cf_record_lock.locked()) or real_unlink(a))
    daemon._cf_mark_unfinished("55", "t")
    daemon._cf_clear_unfinished("55", "t")
    assert held == [True, True]
    assert daemon._cf_unfinished_since("55") is None


def test_a_record_too_malformed_to_attribute_is_removed(monkeypatch):
    # It can never be closed by the run that owns it, so leaving it would grant an unearned
    # retry forever. The next run rewrites the file anyway (#240 review r3, finding 7).
    with open(daemon._cf_unfinished_path("55"), "w") as f:
        f.write("1756000000")            # epoch only: no token to match against
    daemon._cf_clear_unfinished("55", "some-run")
    assert daemon._cf_unfinished_since("55") is None


# ---- the record itself -------------------------------------------------------

def test_the_record_is_read_back_from_a_file(monkeypatch):
    # Named for what it checks: a record written by some OTHER process — which is exactly
    # what a pre-restart daemon is — is what the running one reads.
    with open(daemon._cf_unfinished_path("55"), "w") as f:
        f.write("1756000000 tok\n")
    assert daemon._cf_unfinished_since("55") == 1756000000.0


def test_the_record_is_written_atomically(monkeypatch):
    # A daemon killed mid-write would otherwise leave a truncated file, which reads as "no
    # record" — the retry lost in exactly the case that needs it most.
    seen = []
    real_replace = daemon.os.replace
    monkeypatch.setattr(daemon.os, "replace",
                        lambda a, b: seen.append((a, b)) or real_replace(a, b))
    daemon._cf_mark_unfinished("55", "t")
    assert seen and seen[0][0].endswith(".tmp") and not seen[0][1].endswith(".tmp")
    assert daemon._cf_unfinished_since("55") is not None


def test_a_missing_or_corrupt_record_reads_as_no_record():
    assert daemon._cf_unfinished_since("nosuchtopic") is None
    with open(daemon._cf_unfinished_path("55"), "w") as f:
        f.write("not a number")
    assert daemon._cf_unfinished_since("55") is None


def test_only_the_run_that_opened_a_record_can_close_it():
    daemon._cf_mark_unfinished("55", "run-B")
    daemon._cf_clear_unfinished("55", "run-A")      # an older worker, finally reaching its end
    assert daemon._cf_unfinished_since("55") is not None
    daemon._cf_clear_unfinished("55", "run-B")
    assert daemon._cf_unfinished_since("55") is None


def test_the_consumer_closes_a_record_without_a_token():
    # The warning loop is not a run; it has no token to match and closes unconditionally.
    daemon._cf_mark_unfinished("55", "run-B")
    daemon._cf_clear_unfinished("55")
    assert daemon._cf_unfinished_since("55") is None


# ---- _process_autocf: acting on the record -----------------------------------

def _patch_side_effects(monkeypatch):
    rec = {"cf": [], "replies": []}
    monkeypatch.setattr(daemon, "handle_carry_forward",
                        lambda cfg, tid, cmd, info, pane: bool(rec["cf"].append(tid)) or True)
    monkeypatch.setattr(daemon, "reply",
                        lambda cfg, tid, text: bool(rec["replies"].append(text)) or True)
    monkeypatch.setattr(daemon, "log", lambda *a, **k: None)
    monkeypatch.setattr(daemon, "carry_forward_active", lambda tid: False)
    return rec


def _aged(tid, seconds, token="old-run"):
    """Write an unfinished-run record `seconds` old."""
    with open(daemon._cf_unfinished_path(tid), "w") as f:
        f.write(f"{time.time() - seconds:.0f} {token}\n")


def test_re_arms_once_the_cooldown_has_passed(monkeypatch):
    # C1. Context is still high — the drop that normally re-arms never came, precisely
    # because the carry-forward that would have caused it failed.
    rec = _patch_side_effects(monkeypatch)
    monkeypatch.setattr(daemon, "AUTOCF_RETRY_COOLDOWN", 900)
    _aged("55", 901)
    armed = {"55": True}
    fired = daemon._process_autocf({}, "55", {}, "%0", 95, "claude", set(), armed)
    assert fired is True
    assert rec["cf"] == [55]
    assert armed["55"] is True                       # armed again by the new run
    assert daemon._cf_unfinished_since("55") is None    # record consumed, not re-used


def test_does_not_re_arm_inside_the_cooldown(monkeypatch):
    # C2, the half that stops a storm: the session that fails this gate is a busy one, and
    # a busy session would fail it again on the very next tick.
    rec = _patch_side_effects(monkeypatch)
    monkeypatch.setattr(daemon, "AUTOCF_RETRY_COOLDOWN", 900)
    _aged("55", 60)
    armed = {"55": True}
    fired = daemon._process_autocf({}, "55", {}, "%0", 95, "claude", set(), armed)
    assert fired is False
    assert rec["cf"] == [] and rec["replies"] == []   # not even a notice
    assert armed["55"] is True
    assert daemon._cf_unfinished_since("55") is not None  # still pending, not discarded


def test_many_ticks_inside_the_cooldown_fire_nothing(monkeypatch):
    # C2 as the warning loop actually runs it: one failure, then a tick every poll.
    rec = _patch_side_effects(monkeypatch)
    monkeypatch.setattr(daemon, "AUTOCF_RETRY_COOLDOWN", 900)
    _aged("55", 10)
    armed = {"55": True}
    for _ in range(20):
        daemon._process_autocf({}, "55", {}, "%0", 95, "claude", set(), armed)
    assert rec["cf"] == []


def test_one_re_arm_per_failure_not_per_tick(monkeypatch):
    # Consuming the record is what bounds it: after the retry is granted, the following
    # ticks find nothing to act on, so a second run can only come from a second failure.
    rec = _patch_side_effects(monkeypatch)
    monkeypatch.setattr(daemon, "AUTOCF_RETRY_COOLDOWN", 900)
    _aged("55", 901)
    armed = {"55": True}
    for _ in range(20):
        daemon._process_autocf({}, "55", {}, "%0", 95, "claude", set(), armed)
    assert rec["cf"] == [55]


def test_does_not_re_arm_while_a_carry_forward_is_running(monkeypatch):
    # The record is only ever written as a run ends, so one seen while a run is active
    # belongs to an EARLIER run. Acting on it would arm a second fire behind the live one.
    rec = _patch_side_effects(monkeypatch)
    monkeypatch.setattr(daemon, "carry_forward_active", lambda tid: True)
    monkeypatch.setattr(daemon, "AUTOCF_RETRY_COOLDOWN", 900)
    _aged("55", 901)
    armed = {"55": True}
    fired = daemon._process_autocf({}, "55", {}, "%0", 95, "claude", set(), armed)
    assert fired is False
    assert rec["cf"] == []
    assert armed["55"] is True
    assert daemon._cf_unfinished_since("55") is not None  # kept for a tick that can use it


def test_an_exempt_topic_is_untouched_by_the_retry(monkeypatch):
    # C4. #119 means "never auto-CF here", and a pending record must not be a way back in.
    rec = _patch_side_effects(monkeypatch)
    monkeypatch.setattr(daemon, "AUTOCF_RETRY_COOLDOWN", 900)
    _aged("55", 901)
    armed = {"55": True}
    fired = daemon._process_autocf({}, "55", {}, "%0", 95, "claude", {"55"}, armed)
    assert fired is False
    assert rec["cf"] == [] and rec["replies"] == []


def test_a_compacted_run_stays_armed_until_the_context_drops(monkeypatch):
    # C3: with no record, the hysteresis is exactly what it was — armed at 95%, re-armed
    # only by the post-compaction drop below AUTOCF_REARM_PCT.
    rec = _patch_side_effects(monkeypatch)
    armed = {"55": True}
    assert daemon._process_autocf({}, "55", {}, "%0", 95, "claude", set(), armed) is False
    assert armed["55"] is True
    assert daemon._process_autocf({}, "55", {}, "%0", 10, "claude", set(), armed) is False
    assert armed["55"] is False
    assert rec["cf"] == []


def test_an_older_worker_cannot_close_a_newer_runs_record(monkeypatch):
    # THE interleaving round 2 named: manual worker A is halted after compacting; a warning
    # tick starts auto-CF B, which fails and leaves its record open; A finally returns. If A
    # closed the record, the armed flag would be left with nothing to clear it — #239 again.
    replies = []
    _worker_harness(monkeypatch, replies=replies)
    daemon._cf_mark_unfinished("55", "B")            # the newer run, still owed a retry
    daemon._carry_forward_worker({}, 55, "%0", "/x/cf.md", "/x/cf.md.done", "A", "sess")
    assert daemon._cf_unfinished_since("55") is not None


def test_a_halted_worker_that_never_compacted_closes_nothing(monkeypatch):
    # The other direction: A is halted before compacting, so it has nothing to say. It must
    # not touch B's record either.
    replies = []
    _worker_harness(monkeypatch, replies=replies, wait_idle="timeout")
    daemon._cf_mark_unfinished("55", "B")
    daemon._carry_forward_worker({}, 55, "%0", "/x/cf.md", "/x/cf.md.done", "A", "sess")
    assert daemon._cf_unfinished_since("55") is not None


# ---- the record is per-topic auto-CF state, and dies with the rest of it -----

def test_exempting_a_topic_drops_its_pending_retry(monkeypatch):
    # Otherwise un-exempting later meets a stale record and fires on the next tick with the
    # cooldown already spent (#240 review r1, finding 1). #119 means "not here".
    _patch_side_effects(monkeypatch)
    _aged("55", 10)
    daemon._process_autocf({}, "55", {}, "%0", 95, "claude", {"55"}, {"55": True})
    assert daemon._cf_unfinished_since("55") is None


def test_a_closed_topic_drops_its_pending_retry(monkeypatch):
    # warning_loop drops warn/armed state for a topic closed in Telegram so a reopen starts
    # clean; the record has to go with them or the reopen is not clean.
    _aged("55", 10)
    _run_one_warning_poll(monkeypatch, {"55": {"pane": "%0", "closed": True}})
    assert daemon._cf_unfinished_since("55") is None


def test_a_codex_topic_drops_its_pending_retry(monkeypatch):
    # Same for a topic whose pane now reads as codex: the bridge's context machinery leaves
    # codex alone entirely, and a later Claude reuse must not inherit a pending retry.
    _aged("55", 10)
    _run_one_warning_poll(monkeypatch, {"55": {"pane": "%0"}}, engine="codex")
    assert daemon._cf_unfinished_since("55") is None


def test_a_record_survives_a_daemon_that_died_mid_run(monkeypatch):
    # THE restart hole. A worker is a daemon thread: a daemon that exits mid-run never
    # reaches its `finally`, and autocf.json — already persisted — still says armed. Because
    # the record is opened when the run STARTS, the dead run leaves it behind by itself; the
    # new daemon needs to infer nothing (#240 review r2, finding 1).
    # Opened by handle_carry_forward, not by hand: the claim is about the whole
    # start -> death -> restart path, so the start has to be the real one.
    monkeypatch.setattr(daemon, "pane_alive", lambda pane: True)
    monkeypatch.setattr(daemon, "engine_of_pane", lambda pane: "claude")
    monkeypatch.setattr(daemon, "reply", lambda *a, **k: True)
    monkeypatch.setattr(daemon, "log", lambda *a, **k: None)
    monkeypatch.setattr(daemon.threading, "Thread",
                        lambda **kw: type("T", (), {"start": lambda _s: None})())
    monkeypatch.setattr(daemon, "_pending_cf", {})
    assert daemon.handle_carry_forward({}, 55, "/carryforward", {"name": "s"}, "%0") is True
    daemon._pending_cf.clear()                        # the daemon dies: the flow state is RAM
    with open(daemon.state_path("autocf.json"), "w") as f:
        json.dump({"55": True}, f)
    _run_one_warning_poll(monkeypatch, {})            # a fresh daemon starts
    assert daemon._cf_unfinished_since("55") is not None   # nothing consumed it yet
    # patched AFTER the poll: _run_one_warning_poll stubs the same names, and a later
    # monkeypatch of one wins — which is how an earlier version of this test asserted on a
    # recorder nothing was writing to.
    rec = _patch_side_effects(monkeypatch)
    monkeypatch.setattr(daemon, "AUTOCF_RETRY_COOLDOWN", 900)
    _aged("55", 901, token="the-dead-run")                 # ...and once the cooldown passes
    armed = {"55": True}
    assert daemon._process_autocf({}, "55", {}, "%0", 95, "claude", set(), armed) is True
    assert rec["cf"] == [55]


def test_a_restart_after_a_successful_run_grants_no_retry(monkeypatch):
    # The converse, and what round 2 broke by inferring: a run that compacted closed its
    # record before the daemon died. A restart must not resurrect it as a failure and fire
    # an unearned carry-forward — C3 holds across restarts, not only within one process.
    replies = []
    _worker_harness(monkeypatch, replies=replies)
    _run_worker()                                # a real run, opened and compacted
    assert any("Carry-forward complete" in r for r in replies)
    with open(daemon.state_path("autocf.json"), "w") as f:
        json.dump({"55": True}, f)
    _run_one_warning_poll(monkeypatch, {})       # the daemon dies and comes back
    assert daemon._cf_unfinished_since("55") is None
    rec = _patch_side_effects(monkeypatch)       # after the poll; see the test above
    monkeypatch.setattr(daemon, "AUTOCF_RETRY_COOLDOWN", 900)
    armed = {"55": True}
    # occupancy still above the re-arm point: only the hysteresis may clear this flag
    assert daemon._process_autocf({}, "55", {}, "%0", 95, "claude", set(), armed) is False
    assert armed["55"] is True and rec["cf"] == []


def test_a_malformed_autocf_file_does_not_kill_the_warning_loop(monkeypatch):
    # Round 2 found a `[]` in autocf.json killing the thread before `while True`, from code
    # that read it outside the loop's exception handler. There is no such code now; this
    # pins that there never is again.
    with open(daemon.state_path("autocf.json"), "w") as f:
        f.write("[]")            # VALID json, wrong shape — the case that loads and then bites
    _aged("55", 901)
    monkeypatch.setattr(daemon, "AUTOCF_RETRY_COOLDOWN", 900)
    fired = _run_one_warning_poll(monkeypatch, {"55": {"pane": "%0"}})
    # "did not raise" is not the claim, and neither is "the record was consumed" — the consume
    # happens before the line that would throw. The claim is that the loop still WORKS: an
    # aged record on a live claude topic at 95% must produce an actual carry-forward, which it
    # cannot if every poll dies partway through inside the loop's own exception handler
    # (#240 review r3, finding 10; the first version of this test missed the mutant).
    assert daemon._cf_unfinished_since("55") is None
    assert fired == [55]


def test_a_malformed_warnings_file_does_not_stop_the_loop_either(monkeypatch):
    # warnings.json is loaded by the same two lines with the same gap, and its `.get` is
    # reached BEFORE the auto-CF step — so wrong-shaped JSON there takes the retry down too.
    # Guarded together; tested together, or the guard is just an untested assertion.
    with open(daemon.state_path("warnings.json"), "w") as f:
        f.write("[]")
    _aged("55", 901)
    monkeypatch.setattr(daemon, "AUTOCF_RETRY_COOLDOWN", 900)
    fired = _run_one_warning_poll(monkeypatch, {"55": {"pane": "%0"}})
    assert fired == [55]


def test_codex_is_still_never_fired_by_the_retry(monkeypatch):
    # The engine gate lives in _autocf_decide and the retry runs before it. Re-arming a
    # codex topic must not reach handle_carry_forward, which has no /compact flow.
    rec = _patch_side_effects(monkeypatch)
    monkeypatch.setattr(daemon, "AUTOCF_RETRY_COOLDOWN", 900)
    _aged("55", 901)
    armed = {"55": True}
    fired = daemon._process_autocf({}, "55", {}, "%0", 95, "codex", set(), armed)
    assert fired is False
    assert rec["cf"] == []
