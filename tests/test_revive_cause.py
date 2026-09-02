"""A revive has three causes and only one of them is a reboot (#167).

The session is not just shown this wording — it REASONS from it. Reviving topic 1902 by
hand, with no reboot anywhere, the session was handed "restored after a server reboot",
then explained a reaped background listener to itself as "the pre-reboot listener being
terminated". Right conclusion, false premise. The next such inference — a missing tmux
session, a gap in wall-clock time — will be wrong, and it will be reported to the owner as fact.
"""

import itertools

import pytest

from bridge import daemon


def product(*iterables):
    """`itertools.product`, materialised.

    pytest 9 deprecates a generator as `parametrize`'s argvalues and errors on it in 10; every
    call here fed one, which put nine PytestRemovedIn10Warning blocks in front of anyone running
    the suite in a fresh clone. Wrapping at the one place they are all built keeps the call sites
    readable.
    """
    return list(itertools.product(*iterables))


ENGINES = ("claude", "codex")
CAUSES = daemon.RESTORE_CAUSES
# "unknown" is not selectable by a caller, but it is a real table entry and must be complete.
ALL_KEYS = CAUSES + ("unknown",)


# ---- the wording itself ----


@pytest.mark.parametrize("engine,fresh", product(ENGINES, (False, True)))
def test_boot_wording_is_unchanged(engine, fresh):
    """restore_on_boot is the one caller that really is a reboot; its text must not drift."""
    tpl, notice = daemon._restore_wording(engine, "boot", fresh=fresh)
    assert "reboot" in tpl
    assert "reboot" in notice
    assert "did NOT reboot" not in tpl


@pytest.mark.parametrize("engine,fresh,cause",
                         product(ENGINES, (False, True),
                                           ("recovery", "manual", "auto", "unknown")))
def test_only_the_boot_cause_may_assert_a_reboot(engine, fresh, cause):
    """The original bug: claiming a reboot without evidence. Only restore_on_boot's
    boot_id-changed branch has that evidence."""
    tpl, notice = daemon._restore_wording(engine, cause, fresh=fresh)
    assert "after a server reboot" not in tpl
    assert "after a reboot" not in tpl
    assert "lost in a reboot" not in tpl
    assert "The host rebooted" not in tpl
    assert "Reboot" not in notice and "reboot" not in notice


@pytest.mark.parametrize("engine,fresh,cause",
                         product(ENGINES, (False, True),
                                           ("manual", "auto", "unknown")))
def test_causes_without_evidence_deny_nothing_and_admit_it(engine, fresh, cause):
    """The mirror-image bug, which Codex caught on round 1: asserting "the host did NOT
    reboot" is just as unfounded as asserting it did, and is FALSE exactly where restore_cli
    is used — its documented job is reviving sessions a reboot killed. These causes know a
    pane is dead and nothing else, so they must claim nothing else."""
    tpl, _notice = daemon._restore_wording(engine, cause, fresh=fresh)
    assert "did NOT reboot" not in tpl
    assert "everything else on the machine is as it was" not in tpl
    assert "only this terminal changed" not in tpl
    # ...and they must say so, rather than leaving the session to fill the gap itself.
    assert "did NOT determine" in tpl


def test_recovery_admits_it_cannot_tell_a_reboot_from_a_killed_tmux():
    """_all_target_panes_dead's own docstring calls its state "post-reboot/post-kill"; the
    briefing must not resolve that ambiguity it cannot resolve."""
    tpl, notice = daemon._restore_wording("claude", "recovery", fresh=False)
    assert "whether the host rebooted" in tpl          # named as UNDETERMINED, not answered
    assert "The host rebooted" not in tpl
    assert "found dead" in tpl
    # ...and it no longer overstates the scope of the check: _restore_targets filters out
    # feeds, ended entries and entries with no pane, so "every REGISTERED terminal" was false.
    assert "every registered terminal" not in tpl.lower()
    assert "selected for recovery" in tpl
    assert "reboot" not in notice


def test_auto_must_not_claim_its_dead_pane_took_its_background_tasks():
    """Rounds 1 and 2 both put a claim here and both were wrong. `pane_alive` only asks tmux
    whether the pane exists; a process started with setsid, or any child that outlives its
    shell, reparents to init and keeps running — a reviewer demonstrated it. Telling a revived
    session its work is gone can make it duplicate or abandon live work."""
    tpl, _ = daemon._restore_wording("claude", "auto", fresh=False)
    assert "background tasks from your previous terminal are gone" not in tpl.lower()
    assert "whether work you had running is still running" in tpl


@pytest.mark.parametrize("engine,fresh,cause",
                         product(ENGINES, (False, True), ALL_KEYS))
def test_template_still_formats_with_tid_alone(engine, fresh, cause):
    """deliver_briefing does `briefing_tpl.format(tid=tid)` and nothing else — a placeholder
    left unresolved by the cause substitution raises KeyError there, i.e. the session gets no
    briefing at all and (for codex, with an empty inbox) nothing else ever briefs it."""
    tpl, _notice = daemon._restore_wording(engine, cause, fresh=fresh)
    assert "{tid}" in tpl
    text = tpl.format(tid=1902)
    assert "{" not in text.replace("{tid}", "")
    assert "1902" in text


@pytest.mark.parametrize("engine,fresh,cause",
                         product(ENGINES, (False, True), ALL_KEYS))
def test_no_placeholder_token_survives(engine, fresh, cause):
    tpl, _ = daemon._restore_wording(engine, cause, fresh=fresh)
    for token in ("{opening}", "{opening_lc}", "{persisted}",
                  "{fresh_reason}", "{topic_state}", "{fresh_short}"):
        assert token not in tpl


def test_fresh_opening_is_lowercased_mid_sentence():
    """{opening_lc} is spliced after a colon; an upper-case 'Your terminal...' there reads as
    a sentence break and the substitution is the only thing that can fix the case."""
    tpl, _ = daemon._restore_wording("claude", "auto", fresh=True)
    assert "this topic's terminal was found dead" in tpl


def test_unknown_cause_degrades_to_the_weakest_claim_not_the_strongest(monkeypatch):
    """A typo must not become the most confident false statement the bridge can make. It
    still must not raise: a KeyError here silences the briefing entirely."""
    logs = []
    monkeypatch.setattr(daemon, "log", logs.append)
    tpl, notice = daemon._restore_wording("claude", "rebooot", fresh=False)
    assert tpl == daemon._restore_wording("claude", "unknown", fresh=False)[0]
    assert notice == daemon._restore_wording("claude", "unknown", fresh=False)[1]
    assert tpl != daemon._restore_wording("claude", "boot", fresh=False)[0]
    # "whether the host rebooted" is a denial of knowledge, not a claim — what must be absent
    # is any POSITIVE assertion either way.
    assert "The host rebooted" not in tpl
    assert "after a server reboot" not in tpl
    assert "did NOT reboot" not in tpl
    assert any("rebooot" in line for line in logs)


def test_causes_are_covered_in_every_table():
    """A cause added to RESTORE_CAUSES but missed in one table would KeyError at revive time."""
    for cause in ALL_KEYS:
        assert cause in daemon._RESTORE_OPENING
        assert cause in daemon._RESTORE_PERSISTED
        for fresh in (False, True):
            assert (cause, fresh) in daemon._RESTORE_NOTICE


# ---- the callers pass the cause that matches reality ----


def _stub_revive_one(monkeypatch, seen):
    """`cause` is keyword-only with NO default on purpose. A fake that defaults it cannot
    distinguish "the production call passed it" from "the production call omitted it" —
    Codex demonstrated that a fake with a default let both boot-path tests pass while
    `cause="boot"` was deleted from `_restore_targets_now`."""

    def fake(_cfg, tid, _entry, fresh=False, brief=True, taken=None, *, cause,
             fresh_requested=None):
        seen.append((str(tid), cause))
        return "resumed", {"needs_brief": False}

    monkeypatch.setattr(daemon, "revive_one", fake)


def test_manual_revive_says_manual(monkeypatch):
    seen = []
    _stub_revive_one(monkeypatch, seen)
    monkeypatch.setattr(daemon, "read_registry",
                        lambda: {"1902": {"engine": "claude", "session_id": "sid", "pane": "%3"}})

    daemon.revive_topics({"bot_token": "t", "chat_id": -1}, [{"tid": "1902"}])

    assert seen == [("1902", "manual")]


def test_boot_restore_says_boot(monkeypatch):
    seen = []
    _stub_revive_one(monkeypatch, seen)
    monkeypatch.setattr(daemon, "api", lambda *a, **k: None)
    monkeypatch.setattr(daemon, "save_boot_id", lambda boot: None)

    daemon._restore_targets_now({"bot_token": "t", "chat_id": -1}, "boot-2",
                                [("1902", {"name": "S26", "engine": "claude"})])

    assert seen == [("1902", "boot")]


def test_auto_revive_on_a_dead_pane_says_auto(monkeypatch):
    """maybe_auto_revive is the third caller — the issue named only two. It fires when the owner
    writes to a topic whose pane has died, which is not a reboot either."""
    seen = []
    _stub_revive_one(monkeypatch, seen)
    monkeypatch.setattr(daemon, "read_registry",
                        lambda: {"1902": {"engine": "claude", "session_id": "sid", "pane": "%3"}})
    monkeypatch.setattr(daemon, "should_auto_revive", lambda entry: True)
    # maybe_auto_revive now applies the A3 cost gate before reviving, and this entry has no
    # cwd, so its size reads as unknown and it would be asked about instead. That gate has
    # its own tests in test_reopen_choice; this one is about the CAUSE label it passes on.
    monkeypatch.setattr(daemon, "_reopen_needs_asking", lambda entry: False)

    started = []

    class _Thread:
        def __init__(self, target=None, daemon=None, **kw):
            self._target = target

        def start(self):
            started.append(True)
            self._target()

    monkeypatch.setattr(daemon.threading, "Thread", _Thread)

    daemon.maybe_auto_revive({"bot_token": "t", "chat_id": -1}, "1902")

    assert started
    assert seen == [("1902", "auto")]


# ---- the seam: what revive_one ACTUALLY hands to deliver_briefing ----
#
# Round 1 of this PR shipped 42 tests that all passed while inline delivery ignored the
# resolved template entirely and re-derived boot wording. They tested the resolver, and they
# tested the callers with revive_one monkeypatched away — so nothing crossed the seam where
# the bug would actually live. These do.


def _revive_harness(monkeypatch, engine="claude"):
    """Stub revive_one's environment down to the one thing under test: the template it hands
    on. Returns (captured_briefings, captured_notices)."""
    briefed, notices = [], []

    monkeypatch.setattr(daemon, "_tmux",
                        lambda argv, **kw: type("R", (), {"returncode": 1, "stdout": ""})())
    monkeypatch.setattr(daemon, "launch_pane", lambda *a, **k: ("%77", None))
    monkeypatch.setattr(daemon, "reopen_topic", lambda cfg, tid: True)
    monkeypatch.setattr(daemon, "current_boot_id", lambda: "boot-x")
    monkeypatch.setattr(daemon, "update_registry", lambda fn: None)
    monkeypatch.setattr(daemon, "ensure_codex_trust", lambda cwd: None)
    monkeypatch.setattr(daemon, "reply", lambda cfg, tid, text: notices.append(text))
    monkeypatch.setattr(daemon, "deliver_briefing",
                        lambda pane, tid, eng, tpl, *a, **k: briefed.append(tpl))
    return briefed, notices


@pytest.mark.parametrize("cause", CAUSES)
def test_inline_delivery_carries_the_resolved_cause(monkeypatch, cause):
    briefed, notices = _revive_harness(monkeypatch)
    entry = {"engine": "claude", "session_id": "sid", "cwd": "/home/user"}

    daemon.revive_one({"bot_token": "t", "chat_id": -1}, "1902", entry,
                      brief=True, cause=cause)

    expected_tpl, expected_notice = daemon._restore_wording("claude", cause, fresh=False)
    assert briefed == [expected_tpl]
    assert notices == [expected_notice]


@pytest.mark.parametrize("cause", CAUSES)
def test_deferred_task_carries_the_resolved_cause(monkeypatch, cause):
    """brief=False is the mass-restore path: revive_one returns the template in the task and
    _restore_targets_now fans it out to a thread later. The cause must survive that hop."""
    briefed, _ = _revive_harness(monkeypatch)
    entry = {"engine": "claude", "session_id": "sid", "cwd": "/home/user"}

    _status, task = daemon.revive_one({"bot_token": "t", "chat_id": -1}, "1902", entry,
                                      brief=False, cause=cause)

    assert briefed == []          # nothing delivered inline
    assert task["tpl"] == daemon._restore_wording("claude", cause, fresh=False)[0]


def test_retry_timer_carries_the_same_resolved_template(monkeypatch):
    """deliver_briefing re-enters ITSELF via threading.Timer on a swallowed injection, with
    the template as a positional argument. If that argument were rebuilt or replaced, the
    retry would brief with the wrong cause — and the retry is the path a busy pane takes."""
    scheduled = []

    class _Timer:
        def __init__(self, delay, fn, args=()):
            self.fn, self.args = fn, args
            scheduled.append(args)

        def start(self):
            pass

    monkeypatch.setattr(daemon.threading, "Timer", _Timer)
    monkeypatch.setattr(daemon, "has_live_recv", lambda tid: False)
    monkeypatch.setattr(daemon, "pane_alive", lambda pane: True)
    monkeypatch.setattr(daemon, "pane_is_idle", lambda pane: True)
    monkeypatch.setattr(daemon, "type_line",
                        lambda pane, text, settle=None, still_ok=None: "swallowed")
    monkeypatch.setattr(daemon, "report_blocked_pane", lambda *a, **k: None)
    # Bound to this pane: the #238 delivery guard now runs on every attempt.
    monkeypatch.setattr(daemon, "read_registry", lambda: {"1902": {"pane": "%77"}})
    monkeypatch.setattr(daemon, "current_boot_id", lambda: "boot-1")

    tpl, _ = daemon._restore_wording("claude", "auto", fresh=False)
    daemon.deliver_briefing("%77", "1902", "claude", tpl)

    assert scheduled, "a swallowed briefing must schedule a retry"
    args = scheduled[0]
    assert args[3] == tpl, "the retry must carry the SAME resolved template"
    assert "did NOT determine" in args[3]
    assert "reboot" not in args[3].replace("rebooted", "")


# ---- the structural rule that ends this class of bug -------------------------
#
# Rounds 1 and 2 failed identically: every cause-specific "helpful" detail turned out to be
# something the bridge cannot observe. "only this terminal changed" (false after a tmux
# kill), "background tasks from your previous terminal are gone" (false — a detached child
# reparents to init and outlives the pane; a reviewer proved it with `setsid sleep 300`),
# "every registered terminal was dead" (the check filters out feeds and ended entries),
# "why the previous terminal ended" (revive_topics never inspects the old pane).
#
# Fixing the named sentences each round is a losing game. This test enforces the rule
# instead: outside `boot`, no cause may assert host state, fleet state, prior-terminal state,
# or the fate of background work. A future "helpful" clause fails here rather than in review.

# Round 3 called a phrase denylist "structural enforcement". It was not, and the reviewer
# proved it three ways: `All processes started by the prior terminal have stopped`, the exact
# NEW-3 presupposition `The previous terminal ended`, and a claim smuggled into the stripped
# disclaimer all passed 118/118. Enumerated spellings cannot establish that unenumerated prose
# contains no claim, and no automated test can decide whether a sentence is observable.
#
# What a test CAN do is make every change deliberate. The claim-bearing text lives in exactly
# two tables, so both are pinned verbatim below. Any edit — a new sentence, a reworded one,
# a clause moved into the disclaimer — fails here and puts the author in front of the rule
# with the new words in hand. That is the actual defect being fixed: three times a claim got
# in because nobody was forced to look at it.

_AFTER_DEATH = (
    "The bridge did NOT determine why it ended, whether the host rebooted, what happened to "
    "other sessions, or whether work you had running is still running — do not assume any of "
    "them, and do not report a cause you cannot check.")
_UNOBSERVED = (
    "The bridge did NOT determine the state of any previous terminal for this topic, whether "
    "one ended, whether the host rebooted, what happened to other sessions, or whether work "
    "you had running is still running — do not assume any of them, and do not report a cause "
    "you cannot check.")

_PINNED_OPENINGS = {
    "boot": "The host rebooted, so a new terminal was opened for this topic",
    "recovery": "Every terminal selected for recovery was found dead, including this topic's, "
                "and a new one was opened",
    "manual": "A terminal was opened for this topic on request",
    "auto": "This topic's terminal was found dead when a message arrived, so a new one was "
            "opened",
    "reopen": "The owner reopened this topic and asked for this session to be resumed, so a "
              "new terminal was opened",
    "unknown": "A terminal was opened for this topic",
    "parked": "This topic's session was parked by the bridge after sitting idle (its "
              "terminal was deliberately shut down to free memory), and your message "
              "revived it — a new terminal was opened",
}

_PINNED_PERSISTED = {
    "boot": "Every session's terminal is new, and anything that did not survive the reboot "
            "is gone.",
    # Pinned as LITERAL text, not as a reference to the constant. Referencing it made this
    # tautological — editing the disclaimer changed both sides of the comparison, and a claim
    # smuggled into it passed 129/129.
    "recovery": _AFTER_DEATH,
    "auto": _AFTER_DEATH,
    # Its own clause. Pinned as literal text like the others, so a change to what the session
    # is allowed to take as fact has to be made deliberately.
    "reopen": (
        "A terminal for this topic was seen dead before this one was opened, but the bridge "
        "cannot guarantee it was the terminal this session was just resumed into. It did NOT "
        "determine why that one ended, whether the host rebooted, what happened to other "
        "sessions, or whether work you had running is still running — do not assume any of "
        "them, and do not report a cause you cannot check."),
    "manual": _UNOBSERVED,
    "unknown": _UNOBSERVED,
    # Deliberate first-person claim: the bridge DID end this terminal, on policy, and says
    # so; what the shutdown took with it stays undetermined rather than denied (#274 r1 f5).
    "parked": ("The bridge shut that terminal down deliberately because the session was "
               "idle — the host does not have memory to keep idle sessions resident. It "
               "did NOT determine whether background work you had running survived; do "
               "not assume either way."),
}


@pytest.mark.parametrize("cause", ALL_KEYS)
def test_the_claim_bearing_text_is_pinned(cause):
    """If this fails you are ADDING OR CHANGING A CLAIM the bridge makes to a live session.
    Do not update the pinned value to match. Check first that the new sentence is something
    the code actually observed — `should_auto_revive` records only `ended`, `revive_topics`
    never inspects the old pane, `pane_alive` says nothing about detached child processes,
    and `_restore_targets` filters the set `_all_target_panes_dead` checks. Three claims have
    already been shipped that failed that test."""
    assert daemon._RESTORE_OPENING[cause] == _PINNED_OPENINGS[cause]
    assert daemon._RESTORE_PERSISTED[cause] == _PINNED_PERSISTED[cause]


def test_the_two_disclaimers_are_pinned_verbatim():
    """Pinning by reference is not pinning. These are the literal strings; a claim added to a
    disclaimer must fail here, because the disclaimers are the one place the other tests
    deliberately strip before looking."""
    assert daemon._UNDETERMINED_AFTER_DEATH == _AFTER_DEATH
    assert daemon._UNDETERMINED_UNOBSERVED == _UNOBSERVED


def test_only_boot_carries_a_cause_specific_claim():
    """Everything except boot must reuse one of the two shared disclaimers verbatim, so there
    is no per-cause slot for a new claim to live in."""
    for cause in ("recovery", "manual", "auto", "unknown"):
        assert daemon._RESTORE_PERSISTED[cause] in (
            daemon._UNDETERMINED_AFTER_DEATH, daemon._UNDETERMINED_UNOBSERVED)


def test_the_disclaimer_matches_what_each_cause_actually_looked_at():
    """A cause that observed this topic's pane dead may say so; one that never looked must not
    presuppose an ending. Round 3 used a single clause and did both wrongs at once — it told
    recovery and auto "the bridge did NOT determine the state of any previous terminal" one
    sentence after their own opening said it was found dead."""
    for cause in ("recovery", "auto"):
        tpl, _ = daemon._restore_wording("claude", cause)
        assert "found dead" in tpl
        assert "state of any previous terminal" not in tpl, (
            f"{cause} contradicts its own opening")
    for cause in ("manual", "unknown"):
        tpl, _ = daemon._restore_wording("claude", cause)
        assert "found dead" not in tpl
        assert "whether one ended" in tpl, f"{cause} must not presuppose an ending"


# Kept as a cheap second net for the specific claims already shipped once. It is NOT the
# enforcement mechanism — the pinned tables above are.
_ALREADY_SHIPPED_ONCE = (
    "the host rebooted",
    "did not reboot",
    "only this terminal changed",
    "everything else on the machine",
    "background tasks from before are gone",
    "background tasks from your previous terminal are gone",
    "every registered terminal",
)


@pytest.mark.parametrize("engine,fresh,cause",
                         product(ENGINES, (False, True),
                                           ("recovery", "manual", "auto", "unknown")))
def test_no_cause_but_boot_repeats_a_claim_already_shipped_wrong(engine, fresh, cause):
    tpl, _ = daemon._restore_wording(engine, cause, fresh=fresh)
    lowered = (tpl.replace(daemon._UNDETERMINED_AFTER_DEATH, "")
                  .replace(daemon._UNDETERMINED_UNOBSERVED, "")).lower()
    for claim in _ALREADY_SHIPPED_ONCE:
        assert claim.lower() not in lowered, f"{cause} re-asserts {claim!r}"


@pytest.mark.parametrize("cause", ("recovery", "manual", "auto", "unknown"))
def test_every_evidence_free_cause_shares_one_disclaimer(cause):
    """Not merely 'says something vague' — the SAME clause, so there is one place to audit
    and no room for a per-cause variant to drift back into a claim."""
    assert daemon._RESTORE_PERSISTED[cause] in (
        daemon._UNDETERMINED_AFTER_DEATH, daemon._UNDETERMINED_UNOBSERVED)


def test_a_dead_pane_is_not_evidence_that_its_background_work_stopped():
    """`pane_alive` only asks tmux whether the pane exists. A process started with setsid, or
    any child that outlives its shell, reparents to init and keeps running. Telling a revived
    session its work is gone can make it duplicate or abandon live work — the #167 failure
    class exactly, in a new sentence."""
    for cause in ("auto", "recovery"):
        tpl, _ = daemon._restore_wording("claude", cause)
        assert "are gone" not in tpl or "did NOT determine" in tpl
        assert "background tasks from your previous terminal are gone" not in tpl.lower()


# ---- fresh and reopen must state the observed reason ------------------------

def test_forced_fresh_does_not_claim_recovery_was_attempted(monkeypatch):
    """`do_fresh = fresh or not sid`. With an explicit --fresh no resume is attempted, so
    'could NOT be recovered' describes an attempt that never happened."""
    tpl, notice = daemon._restore_wording("claude", "manual", fresh=True, fresh_requested=True)
    assert "explicitly requested" in tpl
    assert "could NOT be recovered" not in tpl
    assert "couldn't be recovered" not in notice


def test_fresh_for_want_of_a_session_id_says_so():
    tpl, notice = daemon._restore_wording("claude", "manual", fresh=True, fresh_requested=False)
    assert "no recoverable session id was stored" in tpl
    assert "no session id stored" in notice


@pytest.mark.parametrize("engine,fresh",
                         product(ENGINES, (False, True)))
def test_a_failed_reopen_is_told_to_the_session_too(engine, fresh):
    """Round 2 found the warning going only to the owner's notice while the briefing still said
    'has been reopened' — the session and the owner were told opposite things about the same
    fact."""
    tpl, _ = daemon._restore_wording(engine, "boot", fresh=fresh, reopened=False)
    assert "FAILED to reopen" in tpl
    assert "and has been reopened" not in tpl
    ok, _ = daemon._restore_wording(engine, "boot", fresh=fresh, reopened=True)
    assert "and has been reopened" in ok
    assert "FAILED to reopen" not in ok


def test_revive_one_carries_a_failed_reopen_into_the_delivered_briefing(monkeypatch):
    """The seam for round 2's NEW-5. Asserting the resolver alone is not enough: `reopened`
    is computed inside `revive_one`, so only an end-to-end call proves it reaches the text
    the session is actually typed."""
    briefed, notices = _revive_harness(monkeypatch)
    monkeypatch.setattr(daemon, "reopen_topic", lambda cfg, tid: False)
    entry = {"engine": "claude", "session_id": "sid", "cwd": "/home/user"}

    daemon.revive_one({"bot_token": "t", "chat_id": -1}, "1902", entry,
                      brief=True, cause="auto")

    assert briefed and "FAILED to reopen" in briefed[0]
    assert "and has been reopened" not in briefed[0]
    # The owner is warned in the same breath, so the two accounts agree.
    assert notices and "topic reopen failed" in notices[0]


def test_revive_one_distinguishes_requested_fresh_from_no_session_id(monkeypatch):
    """The seam for round 2's NEW-4. `do_fresh = fresh or not sid` collapses two different
    reasons; only `revive_one` knows which one applied."""
    briefed, _ = _revive_harness(monkeypatch)
    entry = {"engine": "claude", "session_id": "sid", "cwd": "/home/user"}
    daemon.revive_one({"bot_token": "t", "chat_id": -1}, "1902", entry,
                      brief=True, fresh=True, cause="manual")
    assert briefed and "explicitly requested" in briefed[0]
    assert "no recoverable session id" not in briefed[0]

    briefed2, _ = _revive_harness(monkeypatch)
    no_sid = {"engine": "claude", "cwd": "/home/user"}          # -> do_fresh via `not sid`
    daemon.revive_one({"bot_token": "t", "chat_id": -1}, "1902", no_sid,
                      brief=True, cause="manual")
    assert briefed2 and "no recoverable session id was stored" in briefed2[0]
    assert "explicitly requested" not in briefed2[0]


# ---- the two seams the reviewer called load-bearing --------------------------
#
# Both were "asserted" by inspecting a value rather than running the code that consumes it.
# The reviewer showed what that buys: replacing the fan-out thread's template with boot
# wording, and replacing the retry callback with a no-op, each left 721 tests green.


def test_the_mass_restore_fan_out_thread_delivers_the_resolved_template(monkeypatch):
    """`_restore_targets_now` is the ONLY briefing path for a boot or recovery restore — it
    calls revive_one with brief=False and hands the deferred task to a thread. Inspecting
    task["tpl"] does not prove that thread passes it on."""
    delivered = []
    monkeypatch.setattr(daemon, "api", lambda *a, **k: None)
    monkeypatch.setattr(daemon, "save_boot_id", lambda boot: None)
    monkeypatch.setattr(daemon, "deliver_briefing",
                        lambda pane, tid, engine, tpl, *a, **k: delivered.append((str(tid), tpl)))

    def fake_revive(_cfg, tid, _info, fresh=False, brief=True, taken=None, *, cause,
                    fresh_requested=None):
        tpl, _ = daemon._restore_wording("claude", cause, fresh=False)
        return "resumed", {"pane": "%9", "tid": str(tid), "engine": "claude", "tpl": tpl,
                           "needs_brief": True, "reopened": True}

    monkeypatch.setattr(daemon, "revive_one", fake_revive)

    class _Thread:
        def __init__(self, target=None, args=(), daemon=None):
            self._target, self._args = target, args

        def start(self):
            self._target(*self._args)          # run it, don't just construct it

    monkeypatch.setattr(daemon.threading, "Thread", _Thread)

    daemon._restore_targets_now({"bot_token": "t", "chat_id": -1}, "boot-9",
                                [("1902", {"name": "S26", "engine": "claude"})],
                                cause="recovery")

    assert delivered, "the fan-out thread must actually brief"
    tid, tpl = delivered[0]
    assert tid == "1902"
    assert tpl == daemon._restore_wording("claude", "recovery", fresh=False)[0]
    assert "reboot" not in tpl.replace("whether the host rebooted", "")


def test_the_briefing_retry_actually_re_delivers_the_same_template(monkeypatch):
    """The retry is what a busy pane takes, so it is not a corner. Asserting the timer's
    arguments does not prove the callback is deliver_briefing or that invoking it works."""
    typed = []
    monkeypatch.setattr(daemon, "has_live_recv", lambda tid: False)
    monkeypatch.setattr(daemon, "pane_alive", lambda pane: True)
    monkeypatch.setattr(daemon, "pane_is_idle", lambda pane: True)
    monkeypatch.setattr(daemon, "report_blocked_pane", lambda *a, **k: None)
    monkeypatch.setattr(daemon, "update_registry", lambda fn: None)
    monkeypatch.setattr(daemon, "current_boot_id", lambda: "boot-x")
    # The retry re-checks that the topic is still bound to this pane before typing again —
    # a real guard, and without it staged here the retry abandons and the test proves nothing.
    monkeypatch.setattr(daemon, "read_registry", lambda: {"1902": {"pane": "%77"}})

    attempts = {"n": 0}

    def flaky_type_line(pane, text, settle=None, still_ok=None):
        attempts["n"] += 1
        typed.append(text)
        return "swallowed" if attempts["n"] == 1 else "sent"

    monkeypatch.setattr(daemon, "type_line", flaky_type_line)

    scheduled = []

    class _Timer:
        def __init__(self, delay, fn, args=()):
            self.fn, self.args = fn, args
            scheduled.append(self)

        def start(self):
            self.fn(*self.args)                # run the retry for real

    monkeypatch.setattr(daemon.threading, "Timer", _Timer)

    tpl, _ = daemon._restore_wording("claude", "auto", fresh=False)
    daemon.deliver_briefing("%77", "1902", "claude", tpl)

    assert scheduled, "a swallowed briefing must schedule a retry"
    assert scheduled[0].fn is daemon.deliver_briefing
    assert len(typed) == 2, "the retry must actually type again"
    assert typed[0] == typed[1] == tpl.format(tid="1902")
    assert "did NOT determine" in typed[1]


def test_revive_topics_does_not_report_derived_fresh_as_requested(monkeypatch):
    """Through the WRAPPER, not the callee. `revive_topics` overwrote its own `fresh` local
    when session-id resolution failed, so every derived-fresh manual revive was briefed as
    "a fresh session was explicitly requested". The callee-level test could not see it — the
    wrapper had already collapsed the distinction before revive_one was reached."""
    briefed, _ = _revive_harness(monkeypatch)
    monkeypatch.setattr(daemon, "read_registry", lambda: {"1902": {"engine": "claude"}})
    monkeypatch.setattr(daemon, "context_session_id", lambda pane: None)

    result = daemon.revive_topics({"bot_token": "t", "chat_id": -1}, [{"tid": "1902"}])

    assert result == {"1902": "fresh"}
    assert briefed, "the wrapper must brief"
    assert "no recoverable session id was stored" in briefed[0]
    assert "explicitly requested" not in briefed[0]


def test_revive_topics_still_reports_an_explicit_fresh_as_requested(monkeypatch):
    """The other half — the distinction has to survive in both directions."""
    briefed, _ = _revive_harness(monkeypatch)
    monkeypatch.setattr(daemon, "read_registry",
                        lambda: {"1902": {"engine": "claude", "session_id": "sid"}})

    daemon.revive_topics({"bot_token": "t", "chat_id": -1}, [{"tid": "1902", "fresh": True}])

    assert briefed and "explicitly requested" in briefed[0]
    assert "no recoverable session id" not in briefed[0]


# ---- an opening speaks for the TERMINAL, never for the session ---------------

_SESSION_CONTINUITY_CLAIMS = (
    "this session resumed into it",
    "SAME conversation",
    "SAME session",
    "you were automatically restored",
    "this is the same",
)


@pytest.mark.parametrize("cause", ALL_KEYS)
def test_an_opening_never_speaks_for_the_session(cause):
    """The templates already say whether the session is the same one resumed or a fresh one.
    An opening that ALSO spoke for the session contradicted it: a manual fresh launch was told
    both "You are a FRESH session" and "this session resumed into it", and on the explicit
    --fresh path the very next clause said no attempt to resume was made."""
    opening = daemon._RESTORE_OPENING[cause]
    lowered = opening.lower()
    for claim in _SESSION_CONTINUITY_CLAIMS:
        assert claim.lower() not in lowered, (
            f"{cause}'s opening says {claim!r}; it may describe the terminal only")


@pytest.mark.parametrize("engine,cause", product(ENGINES, ALL_KEYS))
def test_a_fresh_briefing_never_also_claims_continuity(engine, cause):
    """The full rendered text, not just the opening — that is where the contradiction became
    production-reachable and where round 3's per-part assertions could not see it."""
    tpl, _ = daemon._restore_wording(engine, cause, fresh=True)
    assert "FRESH" in tpl
    lowered = tpl.lower()
    for claim in _SESSION_CONTINUITY_CLAIMS:
        assert claim.lower() not in lowered, f"fresh {cause}/{engine} also claims {claim!r}"


@pytest.mark.parametrize("engine,cause", product(ENGINES, ALL_KEYS))
def test_a_resumed_briefing_says_so_exactly_once(engine, cause):
    """The mirror: a resumed briefing must still carry its continuity statement, so the fix
    above cannot be satisfied by deleting it everywhere."""
    tpl, _ = daemon._restore_wording(engine, cause, fresh=False)
    assert "FRESH" not in tpl
    assert ("SAME conversation" in tpl) or ("SAME session" in tpl)


def test_a_compact_resume_asks_the_briefing_to_wait_for_compaction(monkeypatch):
    """#200 at the SEAM. Testing deliver_briefing's own busy-wait proves nothing about
    whether revive_one actually asks for it — the same call-site gap that let three
    mutations live earlier in this repo's history."""
    seen = {}
    monkeypatch.setattr(daemon, "_tmux",
                        lambda argv, **kw: type("R", (), {"returncode": 1, "stdout": ""})())
    monkeypatch.setattr(daemon, "launch_pane", lambda *a, **k: ("%178", None))
    monkeypatch.setattr(daemon, "answer_resume_picker", lambda pane, choice, **k: "answered")
    monkeypatch.setattr(daemon, "read_registry", lambda: {"12999": {"pane": "%178"}})
    monkeypatch.setattr(daemon, "reopen_topic", lambda cfg, tid: True)
    monkeypatch.setattr(daemon, "current_boot_id", lambda: "boot-x")
    monkeypatch.setattr(daemon, "update_registry", lambda fn: None)
    monkeypatch.setattr(daemon, "reply", lambda cfg, tid, text: True)
    monkeypatch.setattr(daemon, "last_model_and_effort_for_session",
                        lambda sid, cwd: ("claude-fable-5", "xhigh"))
    monkeypatch.setattr(daemon, "deliver_briefing",
                        lambda pane, tid, eng, tpl, *a, **k: seen.update(k))

    daemon.revive_one({}, "12999", {"engine": "claude", "session_id": "sid",
                                    "cwd": "/home/user", "pane": "%178"},
                      cause="auto", resume_choice="compact")

    assert seen.get("await_busy") is True, (
        "revive_one did not ask the briefing to wait for compaction to start (#200)"
    )
    assert seen.get("settle") == daemon.COMPACT_SETTLE


# ---- #235: a reopen the owner asked for is not an incident ------------------------------


def test_the_reopen_notice_never_reports_a_death_or_an_arriving_message():
    """The owner closed the topic and reopened it. Telling them their terminal died reads as a
    fault report for something they did on purpose, and it cost one real investigation:

        "I know the topic was closed. What's the meaning of the message? Does it add
         anything? Only makes an impression that something went wrong. This is why I
         actually asked you."
    """
    for choice in ("compact", "full", None):
        _tpl, notice = daemon._restore_wording("claude", "reopen", resume_choice=choice)
        lowered = notice.lower()
        for forbidden in ("died", "dead", "message arrived", "found dead", "⚠️"):
            assert forbidden not in lowered, f"{choice}: {notice!r} still reports an incident"


@pytest.mark.parametrize("choice,expected", [
    ("compact", "from a summary"),
    ("full", "in full"),
    (None, "on request"),
])
def test_the_reopen_notice_names_the_choice_that_was_applied(choice, expected):
    """They waited through two minutes of silent compaction for this answer, and the resume
    picker can fail to apply it — there is already a warning for that case, so the success
    case has to be specific enough to be worth reading."""
    _tpl, notice = daemon._restore_wording("claude", "reopen", resume_choice=choice)

    assert expected in notice, notice
    assert "{resume_how}" not in notice, "placeholder survived substitution"


def test_the_reopen_briefing_does_not_tell_the_session_a_message_arrived():
    """`auto` says "found dead when a message arrived". On a reopen nothing arrived: the owner
    reopened the topic and answered a question. The session reasons from this (#167)."""
    tpl, _notice = daemon._restore_wording("claude", "reopen")

    assert "message arrived" not in tpl
    assert "reopened this topic" in tpl


def test_the_auto_path_is_untouched():
    """The genuine auto-revive — a message arrived for a dead session — still says so."""
    tpl, notice = daemon._restore_wording("claude", "auto")

    assert "message arrived" in tpl
    assert "terminal had died" in notice


def test_the_reopen_briefing_is_true_in_both_directions():
    """It may not attribute the death to the pane just replaced — `mark_ended` stamps by topic
    id and #237 makes that attribution unsafe — and it may not deny the death either, because
    one really was observed; that is why `ended` exists on the entry at all.

    The first attempt at this used `auto`'s clause and overclaimed. The second swapped to the
    unobserved clause and underclaimed, while calling itself "never false" (#236 review r2).
    Both are pinned against here."""
    tpl, _notice = daemon._restore_wording("claude", "reopen")

    assert "was seen dead before this one was opened" in tpl, "denies an observation it made"
    assert "cannot guarantee it was the terminal" in tpl, "attributes the death to this pane"
    assert "did NOT determine why it ended" not in tpl
