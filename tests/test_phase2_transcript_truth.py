"""PHASE 2 asks the transcript what happened, and the pane only when it cannot (#157).

Three issues (#101, #133, #155) and three review rounds on PR #156 were all the same shape:
the daemon deciding what a Claude session just did by reading rendered text out of a tmux
pane. Rendered text cannot prove causation, and each fix bought soundness with more parsing —
a compaction-specific phrase check, a pre-injection snapshot, occurrence multisets, and a
paragraph of append-and-scroll reasoning with a stated residue for repaints.

The transcript answers all three questions exactly, and the part that cost three rounds —
freshness — stops being an argument and becomes a definition: a cursor taken before the
injection makes "after the cursor" mean fresh. A pane that DISPLAYS a past refusal cannot
write a record.

These tests drive the real `bridge.transcript` primitives over a temporary projects tree, not
a stub of them, because the point of the change is that those primitives are the ground truth.
"""

import json
import os
import time

import pytest

from bridge import daemon, transcript

TOKEN = "tok-1"
SID = "11111111-2222-3333-4444-555555555555"

REFUSAL = {
    "type": "system",
    "subtype": "local_command",
    "content": "<local-command-stderr>Compaction blocked by PreCompact hook: "
               "[bash pre-compact-issue-check.sh] write the carry-forward issue first"
               "</local-command-stderr>",
}
SUBMITTED = {"type": "user",
             "message": {"content": "<command-name>/compact</command-name>"}}
COMPLETED = {"type": "user", "isCompactSummary": True,
             "message": {"content": "This session is being continued…"}}


@pytest.fixture
def session(monkeypatch, tmp_path):
    """A live claude topic whose transcript is a real file under a temporary projects tree."""
    projects = tmp_path / "projects"
    projects.mkdir()
    monkeypatch.setattr(transcript, "PROJECTS_DIR", str(projects))
    cwd = str(tmp_path / "repo")
    path = transcript.transcript_path(cwd, SID)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:                       # a session already has history
        f.write(json.dumps({"type": "user", "message": {"content": "hello"}}) + "\n")

    monkeypatch.setattr(daemon, "read_registry",
                        lambda: {"4242": {"pane": "%1", "engine": "claude",
                                          "session_id": SID, "cwd": cwd}})
    monkeypatch.setattr(daemon, "_cf_owns", lambda _t, _k: True)
    monkeypatch.setattr(daemon, "pane_alive", lambda _p: True)
    monkeypatch.setattr(daemon.time, "sleep", lambda *_a, **_k: None)
    # A pane showing no live compaction, so these transcript tests never shell out to tmux
    # (#289 gave `_cf_await_started` a pane read). Tests about the pane override it.
    monkeypatch.setattr(daemon, "_cf_compacting", lambda _p: False)
    return {"path": path, "cwd": cwd}


def _append(path, *records):
    with open(path, "a") as f:
        for record in records:
            f.write(json.dumps(record) + "\n")


# ---- C1 / C2: the refusal comes from the transcript, and is fresh by construction ---------

def test_a_refusal_after_the_cursor_is_reported_with_the_hooks_own_words(session, monkeypatch):
    tpath, tcur = daemon._cf_transcript_cursor(4242)
    assert tpath and tcur                                   # the cursor was taken
    _append(session["path"], SUBMITTED, REFUSAL)

    monkeypatch.setattr(daemon, "_cf_wait_compacting",
                        lambda *_a, **_k: pytest.fail("fell back to the pane"))
    state, reason = daemon._cf_await_started(4242, TOKEN, "%1", 5, tpath, tcur, "before")

    assert state == "refused"
    assert "Compaction blocked by PreCompact hook" in reason
    assert "write the carry-forward issue first" in reason


def test_the_pane_is_not_consulted_when_the_transcript_can_answer(session, monkeypatch):
    tpath, tcur = daemon._cf_transcript_cursor(4242)
    _append(session["path"], SUBMITTED)
    monkeypatch.setattr(daemon, "_cf_compacting",
                        lambda _p: pytest.fail("read the pane for compaction state"))
    monkeypatch.setattr(daemon, "_cf_hook_block_reason",
                        lambda *_a: pytest.fail("read the pane for a refusal"))

    assert daemon._cf_await_started(4242, TOKEN, "%1", 5, tpath, tcur, "before")[0] == "compacting"


# ---- C5: the false-positive family this removes -------------------------------------------

def test_a_refusal_displayed_on_the_pane_but_absent_from_the_transcript_does_not_abort(session):
    """The #155 case, and the reason its fix needed three rounds. A pane can DISPLAY a
    perfectly formed past refusal — an operator reading a diff, a capture pasted into
    scrollback — and matching it aborted a flow that still had retries left. Against the
    transcript there is nothing to match: a rendered refusal is not a record."""
    tpath, tcur = daemon._cf_transcript_cursor(4242)
    pane_showing_an_old_refusal = ("Compaction blocked by PreCompact hook: "
                                   "[bash pre-compact-issue-check.sh] some older reason")

    state, reason = daemon._cf_await_started(4242, TOKEN, "%1", 0.2, tpath, tcur,
                                             pane_showing_an_old_refusal)

    assert (state, reason) == ("timeout", None)      # retries survive, as they should


def test_a_refusal_already_in_the_transcript_before_the_cursor_is_not_this_ones(session):
    """Freshness as a definition rather than an argument: the cursor is taken after it."""
    _append(session["path"], REFUSAL)                # a refusal from an earlier attempt
    tpath, tcur = daemon._cf_transcript_cursor(4242)

    assert daemon._cf_await_started(4242, TOKEN, "%1", 0.2, tpath, tcur, "")[0] == "timeout"


# ---- C3: submitted is not completed --------------------------------------------------------

def test_submitted_starts_the_flow_but_does_not_finish_it(session):
    """`submitted` proves the command reached the session — which is what the pane check was
    trying to infer — and nothing about compaction having run."""
    tpath, tcur = daemon._cf_transcript_cursor(4242)
    _append(session["path"], SUBMITTED)

    assert daemon._cf_await_started(4242, TOKEN, "%1", 5, tpath, tcur, "")[0] == "compacting"
    assert daemon._cf_await_compacted(4242, TOKEN, "%1", 0.2, tpath, tcur) == ("timeout", None)


def test_completion_is_the_clients_own_record(session, monkeypatch):
    tpath, tcur = daemon._cf_transcript_cursor(4242)
    _append(session["path"], SUBMITTED, COMPLETED)

    monkeypatch.setattr(daemon, "_cf_wait_idle", lambda *_a, **_k: "idle")
    assert daemon._cf_await_compacted(4242, TOKEN, "%1", 5, tpath, tcur) == ("compacted", None)


def test_a_completion_inside_the_start_window_also_counts_as_started(session):
    """A small context can finish compacting before the start gate expires."""
    tpath, tcur = daemon._cf_transcript_cursor(4242)
    _append(session["path"], COMPLETED)

    assert daemon._cf_await_started(4242, TOKEN, "%1", 5, tpath, tcur, "")[0] == "compacting"


# ---- C4: the fallback, and never reading an untrusted absence as "nothing happened" --------

def test_no_resolvable_transcript_delegates_to_the_pane(session, monkeypatch):
    """A codex pane or a stale session id. The pane path must run unchanged — and it is the
    SAME function the existing tests stub, not a second copy of it."""
    calls = []
    monkeypatch.setattr(daemon, "_cf_wait_compacting",
                        lambda *a, **k: calls.append(a) or "compacting")

    assert daemon._cf_await_started(4242, TOKEN, "%1", 5, None, None, "")[0] == "compacting"
    assert len(calls) == 1


def test_a_codex_topic_resolves_no_transcript(session, monkeypatch):
    monkeypatch.setattr(daemon, "read_registry",
                        lambda: {"4242": {"pane": "%1", "engine": "codex", "session_id": SID}})
    assert daemon._cf_transcript_cursor(4242) == (None, None)


def test_an_untrusted_cursor_falls_back_rather_than_reading_absence_as_nothing(session,
                                                                              monkeypatch):
    """`records_since` returns UNKNOWN when the file was replaced, truncated or will not
    parse. Treating that as "nothing happened yet" would let a real refusal go unseen until
    the timeout, and then be reported as a generic failure — which is the thing #155 fixed."""
    tpath, tcur = daemon._cf_transcript_cursor(4242)
    with open(tpath, "w") as f:                       # replaced: shorter than the cursor
        f.write("{}\n")
    calls = []
    monkeypatch.setattr(daemon, "_cf_wait_compacting",
                        lambda *a, **k: calls.append(a) or "timeout")
    monkeypatch.setattr(daemon, "_cf_hook_block_reason", lambda *_a: "the pane's answer")

    state, reason = daemon._cf_await_started(4242, TOKEN, "%1", 5, tpath, tcur, "before")

    assert (state, reason) == ("refused", "the pane's answer")
    assert len(calls) == 1                            # it really delegated


def test_the_settle_wait_falls_back_to_the_existing_idle_wait(session, monkeypatch):
    calls = []
    monkeypatch.setattr(daemon, "_cf_wait_idle", lambda *a, **k: calls.append(a) or "idle")

    assert daemon._cf_await_compacted(4242, TOKEN, "%1", 5, None, None) == ("compacted", None)
    assert len(calls) == 1


def test_the_settle_waits_timeout_is_not_reported_as_compacted(session, monkeypatch):
    monkeypatch.setattr(daemon, "_cf_wait_idle", lambda *_a, **_k: "timeout")
    assert daemon._cf_await_compacted(4242, TOKEN, "%1", 5, None, None) == ("timeout", None)


# ---- abort paths keep working ---------------------------------------------------------------

@pytest.mark.parametrize("owns, alive", [(False, True), (True, False)])
def test_a_lost_claim_or_a_dead_pane_aborts_both_waits(session, monkeypatch, owns, alive):
    tpath, tcur = daemon._cf_transcript_cursor(4242)
    monkeypatch.setattr(daemon, "_cf_owns", lambda _t, _k: owns)
    monkeypatch.setattr(daemon, "pane_alive", lambda _p: alive)

    assert daemon._cf_await_started(4242, TOKEN, "%1", 5, tpath, tcur, "")[0] == "aborted"
    assert daemon._cf_await_compacted(4242, TOKEN, "%1", 5, tpath, tcur) == ("aborted", None)


# ---- what the events helper does and does not claim ------------------------------------------

def test_pending_records_are_believed_for_what_they_contain(session):
    """A half-written tail makes ABSENCE unprovable, not presence. A refusal that parsed
    happened, whatever is being written after it."""
    tpath, tcur = daemon._cf_transcript_cursor(4242)
    with open(tpath, "a") as f:
        f.write(json.dumps(REFUSAL) + "\n")
        f.write('{"type": "user", "mess')                   # mid-write

    events = daemon._cf_compact_events(tpath, tcur)
    assert events is not None and events["refusal"]


def test_an_unreadable_transcript_answers_none_rather_than_empty(session):
    assert daemon._cf_compact_events(None, None) is None
    assert daemon._cf_compact_events(session["path"], None) is None


# ---- the ordering that makes freshness a definition -----------------------------------------

def test_the_cursor_is_taken_before_the_compact_is_injected():
    """The property the whole change rests on, and the one my unit tests could not see: they
    pass a cursor in, so they are blind to WHERE the caller takes it. A cursor taken after the
    injection misses a refusal that lands in between — which reads as a timeout and retries a
    deterministic refusal, the exact failure #155 fixed.

    Asserted against the worker's source, because the ordering is a property of the call site
    rather than of any function's behaviour. It proves the order of these two calls and
    nothing else — a mutation that moved the cursor after the inject passed every behavioural
    test in this file."""
    import inspect

    src = inspect.getsource(daemon._carry_forward_worker)
    cursor_at = src.index("_cf_transcript_cursor(tid)")
    inject_at = src.index('_cf_inject_owned(tid, token, pane, "/compact"')
    assert cursor_at < inject_at, (
        "the transcript cursor must be taken BEFORE /compact is injected, or a refusal "
        "arriving in between is invisible to this attempt")

    # And within the same retry attempt, not once outside the loop: each attempt injects
    # again, so each needs its own cursor or attempt 2 would see attempt 1's refusal.
    loop_at = src.index("for _attempt in range(CF_COMPACT_TRIES)")
    assert loop_at < cursor_at


def test_each_retry_attempt_takes_a_fresh_cursor(session, monkeypatch):
    """Behavioural counterpart: attempt 2 must not inherit attempt 1's refusal, or one
    refusal would abort every later attempt for the wrong reason."""
    tpath, first = daemon._cf_transcript_cursor(4242)
    _append(session["path"], REFUSAL)
    assert daemon._cf_await_started(4242, TOKEN, "%1", 0.2, tpath, first, "")[0] == "refused"

    _tpath, second = daemon._cf_transcript_cursor(4242)      # a new attempt, a new cursor
    assert daemon._cf_await_started(4242, TOKEN, "%1", 0.2, tpath, second, "")[0] == "timeout"


# ---- the two the review found ---------------------------------------------------------------

def test_a_refusal_that_lands_after_the_submission_is_still_reported(session, monkeypatch):
    """Reported by the reviewer with a reproduction, and it is the common ordering rather than
    a corner: the `/compact` user record is written on submit, so the START gate legitimately
    sees `submitted` and returns — and the hook's stderr record arrives a beat later, in the
    COMPLETION window. Returning "timeout" there replaces the hook's own words with a generic
    "didn't settle", which is the failure #155 exists to stop, reached by a new route."""
    tpath, tcur = daemon._cf_transcript_cursor(4242)
    _append(session["path"], SUBMITTED)
    assert daemon._cf_await_started(4242, TOKEN, "%1", 5, tpath, tcur, "")[0] == "compacting"

    _append(session["path"], REFUSAL)                       # the hook answers late
    state, reason = daemon._cf_await_compacted(4242, TOKEN, "%1", 5, tpath, tcur)

    assert state == "refused"
    assert "write the carry-forward issue first" in reason


def test_the_worker_reports_a_late_refusal_with_the_hooks_words(session, monkeypatch):
    """The behavioural end of it: what the owner is actually told."""
    src = __import__("inspect").getsource(daemon._carry_forward_worker)
    # The late-refusal branch must use the hook's reason, not the generic settle message.
    late = src[src.index('res, late_refusal = _cf_await_compacted'):]
    branch = late[:late.index('if res == "timeout"')]
    assert 'refused by a PreCompact hook' in branch
    assert '{late_refusal}' in branch


def test_transcript_completion_still_waits_for_the_pane_to_be_ready(session, monkeypatch):
    """C6, and a regression the first cut introduced. isCompactSummary says the compaction is
    done; it does not say the TUI has finished drawing, and PHASE 3 types into that pane a
    moment later. Completion and readiness are two facts from two sources and both are
    required."""
    tpath, tcur = daemon._cf_transcript_cursor(4242)
    _append(session["path"], SUBMITTED, COMPLETED)
    idle_calls = []
    monkeypatch.setattr(daemon, "_cf_wait_idle",
                        lambda *a, **k: idle_calls.append(a) or "timeout")

    state, _reason = daemon._cf_await_compacted(4242, TOKEN, "%1", 5, tpath, tcur)

    assert idle_calls, "transcript-confirmed completion skipped the pane readiness gate"
    # Compacted, but the pane never became ready. NOT "compacted" — and not "timeout" either,
    # which the owner is told means compaction did not settle. It provably did.
    assert state == "not-ready"


def test_the_cursor_proves_novelty_not_authorship(session):
    """Stated in the code and pinned here so it is not quietly upgraded later: a /compact the
    OWNER types after our cursor is structurally identical to ours. What follows is still
    sound — a compaction is running — but nothing here says this worker caused it."""
    doc = daemon._cf_transcript_cursor.__doc__ or ""
    assert "Novelty is not authorship" in doc
    assert "no comment below should say so" in doc

    tpath, tcur = daemon._cf_transcript_cursor(4242)
    _append(session["path"], SUBMITTED)                     # whoever typed it
    assert daemon._cf_await_started(4242, TOKEN, "%1", 5, tpath, tcur, "")[0] == "compacting"


def test_pending_does_not_fall_back_and_the_docstring_says_so(session):
    """C4: the criterion said anything not OK falls back; the code uses PENDING deliberately,
    because a mid-write tail is the ordinary state of a file being appended to and falling
    back on it would hand most polls to the pane. Recorded rather than hidden."""
    doc = daemon._cf_compact_events.__doc__ or ""
    assert "**PENDING does not fall back.**" in doc


def test_a_compaction_that_finished_is_never_reported_as_not_settling(session, monkeypatch):
    """The reply for this case used to say "Compaction didn't settle in time" — false about a
    thing the client's own record proves happened, and it sends the owner looking for the
    wrong failure. The two outcomes are kept apart all the way to the wording."""
    import inspect

    src = inspect.getsource(daemon._carry_forward_worker)
    start = src.index('if res == "not-ready"')          # the worker has an earlier `res` block
    not_ready = src[start:src.index('if res == "timeout"', start)]
    assert "Compaction finished" in not_ready
    assert "didn't settle" not in not_ready


# ---- #289: the transcript is silent for the whole start window on a big context -------------

def test_a_live_compaction_on_the_pane_counts_as_started(session, monkeypatch):
    """C1. On a 300k context Claude Code writes the `/compact` command record only when the
    compaction FINISHES — minutes after the command was typed — so the transcript answers
    nothing at all inside the 25 s start window and a clean absence read as "not submitted".
    The pane meanwhile draws the live "Compacting conversation… (Ns)" status. That is the one
    claim this gate needs, so it must not wait out the timeout."""
    tpath, tcur = daemon._cf_transcript_cursor(4242)
    assert daemon._cf_compact_events(tpath, tcur) == {"refusal": None, "submitted": False,
                                                      "completed": False}  # OK, and empty
    monkeypatch.setattr(daemon, "_cf_compacting", lambda _p: True)

    started = time.monotonic()
    state, reason = daemon._cf_await_started(4242, TOKEN, "%1", 25, tpath, tcur, "before")
    elapsed = time.monotonic() - started

    assert (state, reason) == ("compacting", None)
    assert elapsed < 5, "waited on the transcript while the pane showed a live compaction"


def test_a_quiet_pane_still_times_out_rather_than_inventing_a_compaction(session, monkeypatch):
    """The other half of C1: `_cf_compacting` defaults to False on an unreadable capture, so
    the pane read can only ADD an answer. Nothing here weakens the empty-transcript timeout."""
    tpath, tcur = daemon._cf_transcript_cursor(4242)
    monkeypatch.setattr(daemon, "_cf_compacting", lambda _p: False)

    assert daemon._cf_await_started(4242, TOKEN, "%1", 0.2, tpath, tcur, "")[0] == "timeout"


LATE_WRITE = 1.0        # seconds the client takes to write anything at all (#289: ~2 min)


def _worker_harness(session, monkeypatch, *, compacting, late_write=LATE_WRITE):
    """Drive PHASE 2 of the real worker over the real transcript. Only the pane and the
    surrounding phases are stubbed; `_cf_await_started`, `_cf_await_compacted`, the cursor
    and the retry loop are the code under test.

    The transcript is written the way a big context writes it: NOTHING for `late_write`
    seconds after the `/compact` is typed, and then the summary and the command record
    together. With the start window set to half of that below, the gate expires before the
    file says anything — the #289 timing, at test speed."""
    state = {"injects": [], "replies": [], "typed_at": None, "written": False}

    def fake_sleep(*_a, **_k):
        if (state["typed_at"] is not None and not state["written"]
                and time.monotonic() - state["typed_at"] >= late_write):
            _append(session["path"], COMPLETED, SUBMITTED)   # the observed file order (#289)
            state["written"] = True
    monkeypatch.setattr(daemon.time, "sleep", fake_sleep)

    def fake_inject(tid, token, pane, text, settle=0.5, release_after=False):
        state["injects"].append(text)
        if text == "/compact" and state["typed_at"] is None:
            state["typed_at"] = time.monotonic()
        if release_after:
            daemon._pending_cf.pop(str(tid), None)
        return True

    monkeypatch.setattr(daemon, "_cf_compacting", lambda _p: compacting)
    monkeypatch.setattr(daemon, "_cf_inject_owned", fake_inject)
    monkeypatch.setattr(daemon, "_cf_cleanup_marker", lambda *_a, **_k: None)
    monkeypatch.setattr(daemon, "_cf_clear_unfinished", lambda *_a, **_k: None)
    monkeypatch.setattr(daemon, "_cf_wait_idle", lambda *_a, **_k: "idle")
    monkeypatch.setattr(daemon, "_cf_wait_done", lambda *_a, **_k: "done")
    monkeypatch.setattr(daemon, "_cf_verify_or_create_issue",
                        lambda *_a, **_k: ("owner/repo#1", "session"))
    monkeypatch.setattr(daemon, "_cf_clear_modal", lambda *_a, **_k: None)
    monkeypatch.setattr(daemon, "_cf_capture_tail", lambda _p: "")
    monkeypatch.setattr(daemon, "_cf_hook_block_reason", lambda *_a: None)
    monkeypatch.setattr(daemon, "log", lambda *_a, **_k: None)
    monkeypatch.setattr(daemon, "reply",
                        lambda _cfg, _tid, text: state["replies"].append(text) or True)
    monkeypatch.setattr(daemon, "_pending_cf",
                        {"4242": {"token": TOKEN, "pane": "%1", "phase": "write"}})
    # The start window expires well before the client writes anything — the whole point.
    monkeypatch.setattr(daemon, "CF_COMPACT_START_WAIT", late_write / 2)
    return state


def test_a_late_written_transcript_still_resolves_from_the_first_injection(session,
                                                                          monkeypatch):
    """C2 + C3, end to end, and the failure #289 reports. Before the fix: the start gate saw
    an empty transcript for its whole window and returned "timeout"; the retry took a FRESH
    cursor — already past the summary — and injected a SECOND `/compact`, which the session
    answered "Not enough messages to compact"; the completion wait then watched from a cursor
    no completion record could ever fall after, and the owner was told the compaction "didn't
    settle" about a compaction that had finished."""
    state = _worker_harness(session, monkeypatch, compacting=True)

    daemon._carry_forward_worker({}, 4242, "%1", "/x/cf.md", "/x/cf.md.done", TOKEN, "sess")

    compacts = [t for t in state["injects"] if t == "/compact"]
    assert compacts == ["/compact"], "injected a second /compact while the pane was compacting"
    assert any("resumed from its carry-forward" in r for r in state["replies"]), state["replies"]
    assert not any("didn't settle" in r for r in state["replies"])


def test_the_completion_wait_keeps_the_cursor_from_the_attempt_that_started(session,
                                                                           monkeypatch):
    """C2 stated as the property rather than the outcome: the cursor handed to the completion
    wait is the one taken before the injection that started the compaction, so the records
    written afterwards fall after it. A cursor re-taken later cannot see them."""
    state = _worker_harness(session, monkeypatch, compacting=True)
    cursors = []
    real_cursor = daemon._cf_transcript_cursor
    monkeypatch.setattr(daemon, "_cf_transcript_cursor",
                        lambda tid: cursors.append(real_cursor(tid)) or cursors[-1])
    awaited = []
    real_awaited = daemon._cf_await_compacted
    monkeypatch.setattr(daemon, "_cf_await_compacted",
                        lambda *a: awaited.append(a[-1]) or real_awaited(*a))

    daemon._carry_forward_worker({}, 4242, "%1", "/x/cf.md", "/x/cf.md.done", TOKEN, "sess")

    assert len(cursors) == 1 and len(awaited) == 1
    assert awaited[0] == cursors[0][1]
