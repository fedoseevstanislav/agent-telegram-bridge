"""#195 — reopening a topic asks before resuming, and says what a full resume costs.

#192 made a reopen revive the session outright. On a large session that is the most expensive
thing the bridge can do unprompted: a full resume re-reads the entire context (the session
this was measured on sat at 352,556 tokens, 35% of its window), and the token cost lands
before they have decided they even wants it back. So the reopen now states the size and offers `full` vs `compact`.

Mirrors the /kill confirmation deliberately, including its hardest-won property: **arm only
after the question is delivered**. A live choice behind an undelivered question would act on
an answer they never knew they were giving (PR #162 round 2).

The deliberate non-guarantee: anything that is NOT an answer falls through to the normal
path, which revives. A message to an ended topic has always revived it, and swallowing their
actual instruction to enforce a menu would be worse than an extra full resume.

`compact` resumes with a small `--autocompact` window so Claude compacts it natively, in ONE
read. The first draft revived and THEN drove a carry-forward — three reads of the context to
save it once, which defeats the whole point. The owner caught that; the tests at the bottom pin it.
"""

import json
import os

import pytest

from bridge import daemon, transcript

# Captured before the autouse fixture stubs it out, so the persistence tests can exercise the
# REAL writer. Re-patching daemon._save_pending_reopens onto itself just reinstates the stub.
_REAL_SAVE = daemon._save_pending_reopens


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch, tmp_path):
    monkeypatch.setattr(daemon, "pending_reopens", {})
    # True, not None: the writer now reports whether the question is durably on disk, and a
    # stub that says "not durable" would make every caller emit the reduced-guarantee notice.
    monkeypatch.setattr(daemon, "_save_pending_reopens", lambda: True)
    monkeypatch.setattr(daemon, "_auto_reviving", set())
    monkeypatch.setattr(daemon, "log", lambda *a, **k: None)


def _transcript(tmp_path, monkeypatch, usage, sid="SID", cwd="/home/user"):
    monkeypatch.setattr(transcript, "PROJECTS_DIR", str(tmp_path))
    path = transcript.transcript_path(cwd, sid)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for u in usage:
            fh.write(json.dumps({"type": "assistant", "timestamp": "2026-08-22T09:00:00.000Z",
                                 "message": {"model": "claude-fable-5", "usage": u}}) + "\n")
    return path


def _age(path, minutes):
    """Backdate the transcript. session_age_minutes reads the file's MTIME, so a fixture
    written a moment ago looks brand new and Claude's picker prediction says "no picker" —
    which silently turns every large-session test into a small-session test."""
    old = os.path.getmtime(path) - minutes * 60
    os.utime(path, (old, old))
    return path


ENTRY = {"name": "api-refactor", "engine": "claude", "session_id": "SID",
         "cwd": "/home/user", "pane": "%9", "ended": "2026-08-22T19:06:33+0000"}


@pytest.fixture
def sent(monkeypatch):
    out = []
    monkeypatch.setattr(daemon, "reply", lambda cfg, tid, text: bool(out.append(text)) or True)
    return out


# ---- the size it quotes -------------------------------------------------------

def test_context_size_comes_from_the_last_usage_record(tmp_path, monkeypatch):
    _transcript(tmp_path, monkeypatch, [
        {"input_tokens": 5, "cache_creation_input_tokens": 100, "cache_read_input_tokens": 900},
        {"input_tokens": 6, "cache_creation_input_tokens": 50, "cache_read_input_tokens": 352_500},
    ])
    assert daemon.session_context_tokens("SID", "/home/user") == 352_556


def test_the_last_record_wins_even_when_it_is_smaller(tmp_path, monkeypatch):
    # After a compaction the newest turn is SMALLER; quoting the largest would overstate the
    # cost and push them toward compacting something already compact.
    _transcript(tmp_path, monkeypatch, [
        {"input_tokens": 0, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 400_000},
        {"input_tokens": 0, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 30_000},
    ])
    assert daemon.session_context_tokens("SID", "/home/user") == 30_000


def test_an_unknown_size_is_None_not_zero(tmp_path, monkeypatch):
    monkeypatch.setattr(transcript, "PROJECTS_DIR", str(tmp_path))
    assert daemon.session_context_tokens("missing", "/home/user") is None


def test_the_question_states_the_size_and_both_options(tmp_path, monkeypatch, sent):
    _transcript(tmp_path, monkeypatch, [
        {"input_tokens": 6, "cache_creation_input_tokens": 50, "cache_read_input_tokens": 352_500}])
    assert daemon.offer_reopen_choice({}, 11722, ENTRY) is True
    text = sent[0]
    assert "352,556" in text and "35%" in text
    assert "`full`" in text and "`compact`" in text
    # C2: the message must describe what the code DOES. It promised a carry-forward long
    # after that implementation was deleted, and only the owner reading it caught that. Asserting
    # the options are present does not catch it — the absence is the requirement.
    assert "carry-forward" not in text.lower(), (
        "the question still promises the carry-forward design that was removed (C2)"
    )
    assert "summary" in text.lower()


def test_an_unknown_size_is_admitted_not_invented(tmp_path, monkeypatch, sent):
    monkeypatch.setattr(transcript, "PROJECTS_DIR", str(tmp_path))
    assert daemon.offer_reopen_choice({}, 11722, ENTRY) is True
    assert "size unknown" in sent[0]
    assert "0 tokens" not in sent[0]


# ---- arming ------------------------------------------------------------------

def test_the_choice_is_not_armed_when_the_question_cannot_be_delivered(tmp_path, monkeypatch):
    _transcript(tmp_path, monkeypatch, [{"input_tokens": 1}])
    monkeypatch.setattr(daemon, "reply", lambda cfg, tid, text: False)   # closed/deleted topic
    assert daemon.offer_reopen_choice({}, 11722, ENTRY) is False
    assert daemon.pending_reopens == {}, (
        "a live choice behind an undelivered question — the #162 r2 failure"
    )


def test_asking_twice_does_not_re_arm(tmp_path, monkeypatch, sent):
    _transcript(tmp_path, monkeypatch, [{"input_tokens": 1}])
    assert daemon.offer_reopen_choice({}, 11722, ENTRY) is True
    assert daemon.offer_reopen_choice({}, 11722, ENTRY) is False
    assert len(sent) == 1


def test_no_question_while_a_revive_is_already_running(tmp_path, monkeypatch, sent):
    # revive_one reopens the topic before clearing `ended`, so its own service message would
    # otherwise come straight back here and ask again mid-revive.
    _transcript(tmp_path, monkeypatch, [{"input_tokens": 1}])
    monkeypatch.setattr(daemon, "_auto_reviving", {"11722"})
    assert daemon.offer_reopen_choice({}, 11722, ENTRY) is False
    assert sent == []


# ---- answering ---------------------------------------------------------------

@pytest.fixture
def answer(monkeypatch, sent):
    calls = {"full": [], "compact": []}
    monkeypatch.setattr(daemon, "_revive_with_choice",
                        lambda cfg, tid, entry, choice: calls[choice].append(str(tid)) or True)
    monkeypatch.setattr(daemon, "read_registry", lambda: {"11722": dict(ENTRY)})

    def _arm():
        daemon.pending_reopens["11722"] = {"entry": dict(ENTRY), "tokens": 352_556, "state": "delivered"}
    return _arm, calls


@pytest.mark.parametrize("word", ["full", "Full", "fully", " as-is ", "полностью", "full."])
def test_full_resumes_as_is(answer, word):
    arm, calls = answer
    arm()
    assert daemon.check_pending_reopen({}, 11722, word) is True
    assert calls["full"] == ["11722"] and calls["compact"] == []


@pytest.mark.parametrize("word", ["compact", "COMPACT", "cf", "сжать"])
def test_compact_relays_the_summary_choice(answer, word):
    arm, calls = answer
    arm()
    assert daemon.check_pending_reopen({}, 11722, word) is True
    assert calls["compact"] == ["11722"] and calls["full"] == []


def test_an_unrelated_message_re_asks_and_is_still_delivered(answer, sent):
    """C4. Returning False is what lets their text reach the inbox — it is NOT 'ignored'. The
    premature revive is stopped by the guard in maybe_auto_revive, not by swallowing what they
    typed, so they lose neither the question nor the sentence."""
    arm, calls = answer
    arm()
    assert daemon.check_pending_reopen({}, 11722, "actually, what did we decide on F2?") is False
    assert calls["full"] == [] and calls["compact"] == []
    assert "Reply `compact`" in sent[-1], "the question was not asked again"
    assert "11722" in daemon.pending_reopens, "the question was dropped by a non-answer"


def test_offer_stores_no_deadline(tmp_path, monkeypatch, sent):
    """A1, at the source. Asserting on a dict the test fixture built proves nothing about
    what offer_reopen_choice stores — a reintroduced deadline survived exactly that gap."""
    _transcript(tmp_path, monkeypatch, [{"cache_read_input_tokens": 352_000}])
    assert daemon.offer_reopen_choice({}, 11722, ENTRY) is True
    assert "deadline" not in daemon.pending_reopens["11722"]
    assert set(daemon.pending_reopens["11722"]) == {"entry", "tokens", "state"}


def test_the_question_has_no_deadline(answer):
    """A1. It waited 300s before; that expired unanswered and left a dead session behind an
    open topic with nothing to say so. There is no clock now — an answer works whenever it
    comes."""
    arm, calls = answer
    arm()
    assert "deadline" not in daemon.pending_reopens["11722"]
    assert daemon.check_pending_reopen({}, 11722, "compact") is True
    assert calls["compact"] == ["11722"]


def test_the_choice_is_consumed_once(answer):
    arm, calls = answer
    arm()
    assert daemon.check_pending_reopen({}, 11722, "full") is True
    assert daemon.check_pending_reopen({}, 11722, "full") is False
    assert calls["full"] == ["11722"]


def test_no_pending_choice_is_a_clean_no(answer):
    arm, calls = answer
    assert daemon.check_pending_reopen({}, 11722, "full") is False
    assert calls["full"] == []


# ---- the bridge RELAYS Claude's own picker; it does not reimplement compaction -

def test_the_compact_answer_drives_a_revive_that_answers_the_picker(monkeypatch):
    """Two earlier drafts reimplemented this badly — one resumed then drove a carry-forward
    (three reads of the context to save it once), one substituted `--autocompact`. Claude
    Code already offers "Resume from summary (recommended)" and states the age and tokens
    itself; all the bridge has to do is relay the answer, because nobody is at the terminal."""
    seen = {}
    monkeypatch.setattr(daemon, "revive_one",
                        lambda cfg, tid, entry, **kw: (seen.update(kw) or ("resumed", None)))
    monkeypatch.setattr(daemon.threading, "Thread",
                        lambda target=None, daemon=None, **kw: type(
                            "T", (), {"start": lambda self: target()})())
    daemon._revive_with_choice({}, 11722, dict(ENTRY), "compact")
    assert seen.get("resume_choice") == "compact"


def test_the_full_answer_relays_full(monkeypatch):
    seen = {}
    monkeypatch.setattr(daemon, "revive_one",
                        lambda cfg, tid, entry, **kw: (seen.update(kw) or ("resumed", None)))
    monkeypatch.setattr(daemon.threading, "Thread",
                        lambda target=None, daemon=None, **kw: type(
                            "T", (), {"start": lambda self: target()})())
    daemon._revive_with_choice({}, 11722, dict(ENTRY), "full")
    assert seen.get("resume_choice") == "full"


def test_the_launch_command_carries_no_compaction_flag_of_ours():
    # The substitute is gone: compaction is Claude's, chosen in its picker.
    cmd = daemon._resume_launch("claude", "SID", "claude-fable-5", "medium")
    assert "--autocompact" not in cmd


# ---- answering the picker ------------------------------------------------------

@pytest.fixture
def pane(monkeypatch):
    keys = []
    screen = {"text": ""}
    monkeypatch.setattr(daemon, "peek_pane", lambda p, lines=40: screen["text"])
    monkeypatch.setattr(daemon, "_tmux",
                        lambda cmd, **kw: keys.append(cmd[-1]) or type("R", (), {"returncode": 0})())
    import contextlib
    monkeypatch.setattr(daemon, "_pane_lock", lambda p: contextlib.nullcontext())
    return keys, screen


PICKER = ("This session is 2d 3h old and 352k tokens.\n"
          "1. Resume from summary (recommended)\n"
          "2. Resume full session as-is\n"
          "3. Don't ask me again\n")


def test_compact_selects_option_1(pane):
    keys, screen = pane
    screen["text"] = PICKER
    assert daemon.answer_resume_picker("%9", "compact") == "answered"
    assert keys == ["1", "Enter"]


def test_full_selects_option_2(pane):
    keys, screen = pane
    screen["text"] = PICKER
    assert daemon.answer_resume_picker("%9", "full") == "answered"
    assert keys == ["2", "Enter"]


def test_it_never_selects_dont_ask_me_again(pane):
    # Option 3 sets resumeReturnDismissed and would silently disable the picker for EVERY
    # future resume on this machine. No mapping may ever produce it.
    assert "3" not in daemon.RESUME_MODAL_CHOICE.values()


def test_nothing_is_pressed_when_the_picker_is_not_on_screen(pane):
    # The #133 rule: never key an unverified pane. Here the wrong key is "don't ask again".
    keys, screen = pane
    screen["text"] = "❯ \n  user • Fable 5 • 10%"
    assert daemon.answer_resume_picker("%9", "compact", deadline=daemon.time.time() + 0.1) == "absent"
    assert keys == []


def test_an_unknown_choice_presses_nothing(pane):
    keys, screen = pane
    screen["text"] = PICKER
    assert daemon.answer_resume_picker("%9", "sideways") == "failed"
    assert keys == []


# ---- ask only when Claude will actually offer the choice -----------------------

def test_the_picker_is_predicted_with_claude_s_own_thresholds(tmp_path, monkeypatch):
    _transcript(tmp_path, monkeypatch, [{"cache_read_input_tokens": 352_000}])
    monkeypatch.setattr(daemon, "session_age_minutes", lambda sid, cwd: 200.0)
    assert daemon.resume_picker_expected("SID", "/home/user") is True


def test_a_small_session_is_not_asked_about(tmp_path, monkeypatch):
    # Under Claude's token threshold no picker renders, so asking would leave the bridge
    # waiting to answer something that never appears.
    _transcript(tmp_path, monkeypatch, [{"cache_read_input_tokens": 20_000}])
    monkeypatch.setattr(daemon, "session_age_minutes", lambda sid, cwd: 200.0)
    assert daemon.resume_picker_expected("SID", "/home/user") is False


def test_a_recent_session_is_not_asked_about(tmp_path, monkeypatch):
    _transcript(tmp_path, monkeypatch, [{"cache_read_input_tokens": 352_000}])
    monkeypatch.setattr(daemon, "session_age_minutes", lambda sid, cwd: 5.0)
    assert daemon.resume_picker_expected("SID", "/home/user") is False


def test_unknown_size_or_age_means_no_question(tmp_path, monkeypatch):
    monkeypatch.setattr(transcript, "PROJECTS_DIR", str(tmp_path))
    assert daemon.resume_picker_expected("missing", "/home/user") is False


def test_revive_one_actually_answers_the_picker(tmp_path, monkeypatch):
    """The load-bearing call site. Everything else here tests the pieces; without this, a
    revive that never answers the picker passes the whole file — which is precisely the bug
    being fixed (a headless revive leaving the modal up and the briefing swallowed)."""
    answered = []
    monkeypatch.setattr(daemon, "launch_pane",
                        lambda tmux_name, cwd, launch, engine, reason, prompt=None: ("%77", ""))
    monkeypatch.setattr(daemon, "_tmux",
                        lambda *a, **kw: type("R", (), {"returncode": 1, "stdout": "", "stderr": ""})())
    monkeypatch.setattr(daemon, "answer_resume_picker",
                        lambda pane, choice, **kw: answered.append((pane, choice)) or "answered")
    monkeypatch.setattr(daemon, "last_model_for_session", lambda sid: "claude-fable-5")
    monkeypatch.setattr(daemon, "last_effort_for_session", lambda sid, cwd: "medium")
    monkeypatch.setattr(daemon, "reopen_topic",
                        lambda cfg, tid: (_ for _ in ()).throw(RuntimeError("stop here")))

    entry = {"engine": "claude", "session_id": "SID", "cwd": str(tmp_path), "pane": "%1"}
    try:
        daemon.revive_one({}, "11722", entry, resume_choice="compact")
    except RuntimeError:
        pass                      # everything past the picker is out of scope

    assert answered == [("%77", "compact")], (
        "revive_one launched without answering the resume picker — the session comes back "
        "sitting on a modal and its briefing is swallowed"
    )


def test_a_revive_with_no_choice_does_not_touch_the_picker(tmp_path, monkeypatch):
    # Boot restore and the plain on-message revive pass no choice; they must not press keys.
    answered = []
    monkeypatch.setattr(daemon, "launch_pane",
                        lambda tmux_name, cwd, launch, engine, reason, prompt=None: ("%77", ""))
    monkeypatch.setattr(daemon, "_tmux",
                        lambda *a, **kw: type("R", (), {"returncode": 1, "stdout": "", "stderr": ""})())
    monkeypatch.setattr(daemon, "answer_resume_picker",
                        lambda pane, choice, **kw: answered.append((pane, choice)) or "answered")
    monkeypatch.setattr(daemon, "last_model_for_session", lambda sid: "claude-fable-5")
    monkeypatch.setattr(daemon, "last_effort_for_session", lambda sid, cwd: None)
    monkeypatch.setattr(daemon, "_resume_picker_present", lambda screen: False)
    monkeypatch.setattr(daemon, "_await_live_picker", lambda pane, grace, poll: False)
    monkeypatch.setattr(daemon, "reopen_topic",
                        lambda cfg, tid: (_ for _ in ()).throw(RuntimeError("stop here")))

    entry = {"engine": "claude", "session_id": "SID", "cwd": str(tmp_path), "pane": "%1"}
    try:
        daemon.revive_one({}, "11722", entry)
    except RuntimeError:
        pass
    # With no choice it may only DETECT a picker, never press: answer_resume_picker is not
    # called at all (finding 2 keeps detection, but never guesses on their behalf).
    assert answered == []


# ---- C4/A3: nothing revives while a question is open --------------------------

def test_an_inbound_message_does_not_revive_while_a_question_is_open(monkeypatch):
    """A3. Before this guard, any message to the topic hit maybe_auto_revive and spent the
    whole context on a full resume — the expensive option, chosen by accident, precisely
    because they had not answered yet."""
    revived = []
    monkeypatch.setattr(daemon, "revive_one",
                        lambda cfg, tid, entry, **kw: revived.append(str(tid)) or ("resumed", None))
    monkeypatch.setattr(daemon, "read_registry",
                        lambda: {"11722": dict(ENTRY)})
    daemon.pending_reopens["11722"] = {"entry": dict(ENTRY), "tokens": 1, "state": "delivered"}
    daemon.maybe_auto_revive({}, 11722)
    assert revived == []


def _auto_revive_harness(monkeypatch):
    revived = []
    monkeypatch.setattr(daemon, "revive_one",
                        lambda cfg, tid, entry, **kw: revived.append(str(tid)) or ("resumed", None))
    monkeypatch.setattr(daemon, "read_registry", lambda: {"11722": dict(ENTRY)})
    monkeypatch.setattr(daemon.threading, "Thread",
                        lambda target=None, daemon=None, **kw: type(
                            "T", (), {"start": lambda self: target()})())
    return revived


def test_once_answered_a_cheap_session_revives_without_asking_again(tmp_path, monkeypatch):
    """The guard is released by answering — and a session measurably below Claude's own
    picker thresholds is not asked about a second time (A3 draws the line at large-or-unknown,
    not at every session)."""
    _transcript(tmp_path, monkeypatch, [{"cache_read_input_tokens": 20_000}])
    revived = _auto_revive_harness(monkeypatch)

    daemon.maybe_auto_revive({}, 11722)

    assert revived == ["11722"]


def test_a_lost_question_does_not_become_a_silent_full_resume(tmp_path, monkeypatch, sent):
    """Round 2, finding 2 — the strongest one. A3 was enforced only by the in-memory pending
    key, so it lasted exactly as long as that key: after a crash, a failed write, or any
    restart, the next ordinary message to the still-open topic resumed the entire 352k
    context with no question at all. That is the unapproved spend the whole feature exists
    to prevent, reachable by simply typing 'hi'."""
    _age(_transcript(tmp_path, monkeypatch, [{"cache_read_input_tokens": 352_556}]), 1440)
    revived = _auto_revive_harness(monkeypatch)
    assert daemon.pending_reopens == {}, "fixture must start with the question already lost"

    daemon.maybe_auto_revive({}, 11722)

    assert revived == [], "revived a 352k session without asking, because the record was gone"
    assert "352,556" in sent[0] and "`compact`" in sent[0]
    assert daemon.pending_reopens["11722"]["state"] == "delivered"


def test_an_unaskable_large_session_is_not_revived_unasked(tmp_path, monkeypatch):
    """A3 has no exception for "the question could not be delivered".

    Round 2 deliberately revived here, to avoid stranding the session, and wrote a test that
    REQUIRED it — so the suite was green precisely because the prohibition was broken. That
    is the second time a test of mine has enshrined the defect it was meant to catch. A
    closed or deleted topic has nobody waiting on the session, so the 352k buys nothing, and
    A3 says "including on restart or ANY error path"."""
    _age(_transcript(tmp_path, monkeypatch, [{"cache_read_input_tokens": 352_556}]), 1440)
    revived = _auto_revive_harness(monkeypatch)
    monkeypatch.setattr(daemon, "reply", lambda cfg, tid, text: False)

    daemon.maybe_auto_revive({}, 11722)

    assert revived == [], "spent 352k unasked because the question bounced"
    assert daemon.pending_reopens == {}, "armed a choice behind a question they never received"


# ---- C5: the question survives a daemon restart -------------------------------

def test_a_pending_question_survives_a_restart(tmp_path, monkeypatch):
    """C5. A real save/load round-trip through the on-disk store. In memory only, a restart
    drops the question silently and leaves the dead-session / open-topic pair this feature
    exists to prevent — the same bug wearing a different hat."""
    store = tmp_path / "pending-reopens.json"
    monkeypatch.setattr(daemon, "state_path", lambda *p: str(store))
    monkeypatch.setattr(daemon, "_save_pending_reopens", _REAL_SAVE)
    monkeypatch.setattr(daemon, "pending_reopens",
                        {"11722": {"entry": dict(ENTRY), "tokens": 352_556, "state": "delivered"}})

    daemon._save_pending_reopens()                       # daemon writes it
    assert store.exists(), "the question was never persisted"

    monkeypatch.setattr(daemon, "pending_reopens", {})   # daemon restarts
    assert daemon._load_pending_reopens() == {
        "11722": {"entry": dict(ENTRY), "tokens": 352_556, "state": "delivered"}}


def _real_worker(tmp_path, monkeypatch, status):
    """Answer through the REAL _revive_with_choice, with revive_one reporting `status`.

    Stubbing _revive_with_choice away is what hid round 3's finding 5: the handler's pop and
    the worker's re-arm raced, and neither side was exercised against the other. The worker
    runs inline so "who owns the record" is a fact about the code, not about scheduling."""
    store = tmp_path / "pending-reopens.json"
    monkeypatch.setattr(daemon, "state_path", lambda *p: str(store))
    monkeypatch.setattr(daemon, "_save_pending_reopens", _REAL_SAVE)
    monkeypatch.setattr(daemon, "read_registry", lambda: {"11722": dict(ENTRY)})
    monkeypatch.setattr(daemon, "revive_one",
                        lambda cfg, tid, entry, **kw: (status, None))
    monkeypatch.setattr(daemon.threading, "Thread",
                        lambda target=None, daemon=None, **kw: type(
                            "T", (), {"start": lambda self: target()})())
    monkeypatch.setattr(daemon, "pending_reopens",
                        {"11722": {"entry": dict(ENTRY), "tokens": 1, "state": "delivered"}})
    _REAL_SAVE()                                  # the question is on disk to begin with
    assert daemon._load_pending_reopens() != {}, "fixture did not persist anything"
    return store


def test_answering_clears_the_persisted_question(tmp_path, monkeypatch, sent):
    # Otherwise a restart after an answer would re-ask a question already acted on.
    _real_worker(tmp_path, monkeypatch, "resumed")

    assert daemon.check_pending_reopen({}, 11722, "compact") is True
    assert daemon.pending_reopens == {}
    assert daemon._load_pending_reopens() == {}, (
        "answered, but the question is still on disk — a restart would re-ask it"
    )


def test_a_revive_that_fails_inside_the_worker_gives_the_question_back(tmp_path, monkeypatch,
                                                                       sent):
    """Round 3, finding 5. The handler popped the record after Thread.start() returned; a
    worker that had already run and FAILED saw the key still present, read it as "already
    armed", and did nothing — then the handler popped it. Dead session, no question, and a
    "Resuming…" they could not act on. Answerability must survive a failed worker, on disk."""
    _real_worker(tmp_path, monkeypatch, "failed")

    assert daemon.check_pending_reopen({}, 11722, "compact") is True
    assert daemon.pending_reopens["11722"]["state"] == "delivered", (
        "the question is not answerable again after a failed revive"
    )
    assert daemon._load_pending_reopens()["11722"]["state"] == "delivered", (
        "re-armed in memory only — a restart would still lose it"
    )
    assert any("still down" in m for m in sent), "failed silently"


def test_a_missing_or_corrupt_store_is_an_empty_dict_not_a_crash(tmp_path, monkeypatch):
    missing = tmp_path / "nope.json"
    monkeypatch.setattr(daemon, "state_path", lambda *p: str(missing))
    assert daemon._load_pending_reopens() == {}
    missing.write_text("{not json")
    assert daemon._load_pending_reopens() == {}


# ---- Codex review fixes -------------------------------------------------------

def test_an_unknown_size_is_asked_about_not_silently_revived(tmp_path, monkeypatch):
    """Finding 1. Gating only on the picker prediction meant an unreadable or missing
    transcript — where the cost is UNKNOWN — was revived without a word. Unknown is exactly
    when to ask."""
    monkeypatch.setattr(transcript, "PROJECTS_DIR", str(tmp_path))
    assert daemon._reopen_needs_asking(dict(ENTRY)) is True


def test_a_large_old_session_is_asked_about(tmp_path, monkeypatch):
    """The True side of the gate, with a REAL age rather than a stubbed one. Every other
    large-session test stubs session_age_minutes or bypasses the gate entirely, so a
    prediction that silently said "no picker" would have gone unnoticed."""
    _age(_transcript(tmp_path, monkeypatch, [{"cache_read_input_tokens": 352_556}]), 1440)
    assert daemon._reopen_needs_asking(dict(ENTRY)) is True


def test_a_measurably_small_session_still_reopens_directly(tmp_path, monkeypatch):
    _transcript(tmp_path, monkeypatch, [{"cache_read_input_tokens": 20_000}])
    monkeypatch.setattr(daemon, "session_age_minutes", lambda sid, cwd: 5.0)
    assert daemon._reopen_needs_asking(dict(ENTRY)) is False


def test_a_stale_answer_does_not_relaunch_a_replaced_session(monkeypatch, sent):
    """Finding 5. Between question and answer the topic may have been revived elsewhere or
    re-registered under a new session id. Acting on the snapshot would relaunch the old
    session and overwrite the newer binding."""
    revived = []
    monkeypatch.setattr(daemon, "_revive_with_choice",
                        lambda cfg, tid, entry, choice: revived.append(choice) or True)
    monkeypatch.setattr(daemon, "read_registry",
                        lambda: {"11722": dict(ENTRY, session_id="SID-B")})
    daemon.pending_reopens["11722"] = {"entry": dict(ENTRY), "tokens": 1, "state": "delivered"}

    assert daemon.check_pending_reopen({}, 11722, "compact") is True
    assert revived == [], "relaunched a session the topic is no longer bound to"
    assert "no longer the dead session" in sent[-1]
    assert daemon.pending_reopens == {}


def test_the_question_stays_armed_when_the_revive_will_not_start(monkeypatch, sent):
    """Finding 3. Consuming the answer first meant a failure after the pop lost both the
    question and their choice, leaving a dead session behind a false 'Resuming…'."""
    monkeypatch.setattr(daemon, "_revive_with_choice", lambda cfg, tid, entry, choice: False)
    monkeypatch.setattr(daemon, "read_registry", lambda: {"11722": dict(ENTRY)})
    daemon.pending_reopens["11722"] = {"entry": dict(ENTRY), "tokens": 1, "state": "delivered"}

    assert daemon.check_pending_reopen({}, 11722, "compact") is True
    assert "11722" in daemon.pending_reopens, "the answer was consumed by a revive that never ran"
    assert "still open" in sent[-1]


def test_prose_quoting_the_picker_is_not_the_picker(pane):
    """Finding 6. Matching one phrase would fire on ordinary output — including this
    daemon's own messages about the feature — and send keys into whatever is live."""
    keys, screen = pane
    screen["text"] = "I asked whether to Resume from summary and they never answered."
    assert daemon.answer_resume_picker("%9", "compact", deadline=daemon.time.time() + 0.1) == "absent"
    assert keys == []


def test_enter_is_withheld_when_the_choice_key_does_not_land(monkeypatch, pane):
    """Finding 6. An Enter after a failed choice key accepts whatever row the cursor is on —
    and row 3 is 'Don't ask me again', which disables the picker machine-wide (A2)."""
    keys, screen = pane
    screen["text"] = PICKER
    monkeypatch.setattr(daemon, "_tmux",
                        lambda cmd, **kw: keys.append(cmd[-1]) or type("R", (), {"returncode": 1})())
    assert daemon.answer_resume_picker("%9", "compact") == "failed"
    assert keys == ["1"], "Enter was pressed after the choice key failed"


# ---- round 2: the four findings the first pass left open -----------------------

_CFG = {"chat_id": 1, "owner_id": 5}


def _msg(text, thread_id=11722, message_id=7):
    return {"chat": {"id": 1}, "from": {"id": 5}, "message_id": message_id,
            "message_thread_id": thread_id, "text": text}


@pytest.fixture
def routed(monkeypatch):
    """handle_message stubbed down to the routing decision: which handler got the text."""
    seen = {"commands": [], "interrupts": [], "inbox": []}
    monkeypatch.setattr(daemon, "carry_forward_active", lambda tid: False)
    monkeypatch.setattr(daemon, "append_jsonl",
                        lambda path, record: seen["inbox"].append(record.get("text")) or "claim")
    monkeypatch.setattr(daemon, "schedule_nudge", lambda tid, claim: None)
    monkeypatch.setattr(daemon, "maybe_auto_revive", lambda cfg, tid: None)
    monkeypatch.setattr(daemon, "extract_audio", lambda msg: (None, None))
    # The registry is consulted on every non-answer now (round-2 finding 8 revalidates the
    # re-ask too), so it must be the fixture's, never the live bridge's.
    monkeypatch.setattr(daemon, "read_registry", lambda: {"11722": dict(ENTRY)})
    monkeypatch.setattr(daemon, "handle_command",
                        lambda cfg, tid, text: seen["commands"].append(text))
    monkeypatch.setattr(daemon, "interrupt_session",
                        lambda cfg, tid, text: seen["interrupts"].append(text))
    return seen


def test_a_command_while_the_question_is_open_still_re_asks_it(routed, sent):
    """Finding 8. The `/` and `!` branches return early, so a command typed while the reopen
    question was open slipped past C4 entirely: neither answered nor re-asked, the question
    sat invisible and the session stayed dead. The command must still run — blocking their
    tooling to enforce a menu is the failure mode this feature already rejected once."""
    daemon.pending_reopens["11722"] = {"entry": dict(ENTRY), "tokens": 352_556, "state": "delivered"}

    daemon.handle_message(_CFG, _msg("/status"))

    assert routed["commands"] == ["/status"], "the command was swallowed by the question"
    assert any("Reopening" in m for m in sent), "C4 breached: the question was not re-asked"
    assert "11722" in daemon.pending_reopens
    # A `/command` is addressed to the DAEMON, not to the session, so it runs now and is not
    # queued for replay. Round 2 read C4 literally and called this a violation; the criterion
    # is about session-bound content, and replaying `/status` into a revived session would be
    # a defect, not compliance. Recorded here so the choice is visible rather than implied.
    assert routed["inbox"] == [], "queued a daemon command for replay into the session"


def test_an_interrupt_while_the_question_is_open_still_re_asks_it(routed, sent):
    """Finding 8, the `!` half. There is nothing to interrupt — the session is dead — so the
    one thing that must not happen is silence."""
    daemon.pending_reopens["11722"] = {"entry": dict(ENTRY), "tokens": 352_556, "state": "delivered"}

    daemon.handle_message(_CFG, _msg("!stop"))

    assert any("Reopening" in m for m in sent), "C4 breached: the question was not re-asked"
    # C4's second half, which round 2 found unenforced: the message must still reach the
    # session. There is nothing to interrupt — the session is dead — so routing it to
    # interrupt_session would consume it against a corpse and lose it for good.
    assert routed["interrupts"] == [], "handed an interrupt to a session that does not exist"
    assert routed["inbox"] == ["stop"], "the message was never queued for the revived session"


def test_slash_compact_answers_the_question_instead_of_running_a_command(routed, sent,
                                                                        monkeypatch):
    """Finding 8. `/compact` is muscle memory, and while the session is dead it cannot mean
    Claude's own command — there is nothing running to compact. Routing it to handle_command
    would run a no-op against a corpse and leave the question open."""
    revived = []
    monkeypatch.setattr(daemon, "_revive_with_choice",
                        lambda cfg, tid, entry, choice: revived.append(choice) or True)
    monkeypatch.setattr(daemon, "read_registry", lambda: {"11722": dict(ENTRY)})
    daemon.pending_reopens["11722"] = {"entry": dict(ENTRY), "tokens": 1, "state": "delivered"}

    daemon.handle_message(_CFG, _msg("/compact"))

    assert revived == ["compact"]
    assert routed["commands"] == [], "answered the question AND ran the command"
    # The handler no longer removes the record — the WORKER does, and only on a revive that
    # actually came up (round 3, finding 5). `reviving` is the honest interim state: it is
    # not answerable, and it is not lost either.
    assert daemon.pending_reopens["11722"]["state"] == "reviving"


def test_a_live_session_still_routes_slash_compact_to_the_command(routed, sent):
    """The sigil strip must not leak: with no question pending, `/compact` is the real
    command and has to reach handle_command untouched."""
    daemon.handle_message(_CFG, _msg("/compact"))
    assert routed["commands"] == ["/compact"]


# ---- finding 4: the durability the question promises ---------------------------

def test_the_persisted_question_is_flushed_before_the_rename(tmp_path, monkeypatch):
    """Finding 4. os.replace makes the swap atomic, not durable. Without the fsync, a crash
    between the rename and the kernel's flush leaves an empty or truncated file — which reads
    back as 'no question pending', i.e. exactly the dead session behind an open topic that
    C5 exists to rule out."""
    store = tmp_path / "pending-reopens.json"
    monkeypatch.setattr(daemon, "state_path", lambda *p: str(store))
    monkeypatch.setattr(daemon, "pending_reopens",
                        {"11722": {"entry": dict(ENTRY), "tokens": 352_556, "state": "delivered"}})
    order = []
    real_fsync, real_replace = os.fsync, os.replace
    monkeypatch.setattr(os, "fsync", lambda fd: (order.append("fsync"), real_fsync(fd))[1])
    monkeypatch.setattr(os, "replace",
                        lambda a, b: (order.append("replace"), real_replace(a, b))[1])

    assert _REAL_SAVE() is True
    assert "fsync" in order, "the bytes were never flushed — a crash loses the question"
    assert order.index("fsync") < order.index("replace"), (
        "renamed before flushing, so the atomic swap can publish an empty file")
    assert json.loads(store.read_text())["11722"]["tokens"] == 352_556


def test_a_failed_write_reports_failure_and_leaves_no_half_file(tmp_path, monkeypatch):
    """Finding 4. The writer swallowed every error and returned None, so the caller could not
    tell a persisted question from a lost one."""
    store = tmp_path / "no-such-dir" / "pending-reopens.json"
    monkeypatch.setattr(daemon, "state_path", lambda *p: str(store))
    monkeypatch.setattr(daemon, "pending_reopens", {"11722": {"entry": dict(ENTRY), "state": "delivered"}})

    assert _REAL_SAVE() is False
    assert not (tmp_path / "no-such-dir").exists(), "left a partial write behind"


def test_a_question_that_cannot_be_persisted_is_armed_and_says_so(tmp_path, monkeypatch, sent):
    """Finding 4. Reporting success on a failed write asserts C5 while it does not hold.
    Disarming instead would be strictly worse: the question is already in front of them, so
    their answer would then do nothing at all — a rare durability gap traded for a certain
    dead end."""
    _transcript(tmp_path, monkeypatch, [{"input_tokens": 1}])
    monkeypatch.setattr(daemon, "_save_pending_reopens", lambda: False)

    assert daemon.offer_reopen_choice({}, 11722, ENTRY) is True
    assert "11722" in daemon.pending_reopens, "disarmed a question they can already see"
    assert "could not write" in sent[-1].lower()
    assert "close and reopen" in sent[-1].lower()


def test_a_persisted_question_says_nothing_extra(tmp_path, monkeypatch, sent):
    """The caveat must be the exception, not a permanent footnote on every question."""
    _transcript(tmp_path, monkeypatch, [{"input_tokens": 1}])
    assert daemon.offer_reopen_choice({}, 11722, ENTRY) is True
    assert len(sent) == 1, f"extra message on the happy path: {sent[1:]}"


# ---- finding 9 + the compaction race: what revive_one does to the topic ---------

def _revive_harness(monkeypatch, registry, picker="answered"):
    """revive_one with its environment stubbed down to two observations: whether it reopened
    the topic, and the settle window it gave the briefing."""
    seen = {"reopened": [], "settle": [], "notices": []}
    monkeypatch.setattr(daemon, "_tmux",
                        lambda argv, **kw: type("R", (), {"returncode": 1, "stdout": ""})())
    monkeypatch.setattr(daemon, "launch_pane", lambda *a, **k: ("%77", None))
    monkeypatch.setattr(daemon, "answer_resume_picker", lambda pane, choice, **k: picker)
    monkeypatch.setattr(daemon, "_await_live_picker", lambda pane, grace, poll: False)
    monkeypatch.setattr(daemon, "read_registry", lambda: registry)
    monkeypatch.setattr(daemon, "reopen_topic",
                        lambda cfg, tid: seen["reopened"].append(str(tid)) or True)
    monkeypatch.setattr(daemon, "current_boot_id", lambda: "boot-x")
    monkeypatch.setattr(daemon, "update_registry", lambda fn: None)
    monkeypatch.setattr(daemon, "reply", lambda cfg, tid, text: seen["notices"].append(text))
    monkeypatch.setattr(daemon, "last_model_for_session", lambda sid: "claude-fable-5")
    monkeypatch.setattr(daemon, "last_effort_for_session", lambda sid, cwd: "medium")
    monkeypatch.setattr(daemon, "deliver_briefing",
                        lambda pane, tid, eng, tpl, *a, settle=None, **k:
                        seen["settle"].append(settle))
    return seen


def test_a_close_during_the_revive_is_not_undone(monkeypatch):
    """Finding 9. A revive is not instantaneous — answering the picker alone can hold it for
    RESUME_MODAL_WAIT — and a close inside that window was silently reversed by revive_one's
    own reopen_topic. A close is the one gesture that must never be overridden."""
    registry = {"11722": dict(ENTRY, closed=True)}
    seen = _revive_harness(monkeypatch, registry)

    status, _task = daemon.revive_one({}, "11722", dict(ENTRY), cause="auto",
                                      resume_choice="compact", respect_close=True)

    assert seen["reopened"] == [], "reopened a topic they had just closed"
    assert status == "resumed", "left the session unbound because the topic was closed"
    assert seen["notices"] == [], "announced into a topic they closed"


def test_boot_restore_still_reopens_a_closed_topic(monkeypatch):
    """The guard is scoped. On boot, a closed topic is what a reboot left behind, not a
    decision — #161 restore must still bring it back."""
    registry = {"11722": dict(ENTRY, closed=True)}
    seen = _revive_harness(monkeypatch, registry, picker="absent")

    daemon.revive_one({}, "11722", dict(ENTRY), cause="boot")

    assert seen["reopened"] == ["11722"]


def test_a_compact_resume_waits_out_the_compaction_before_briefing(monkeypatch):
    """The briefing window is sized for a plain resume, but a `compact` answer drops Claude
    straight into compaction. Measured on topic 11722 (2026-08-25): picker answered 15:56:31,
    compaction finished 15:58:21 — 110s against a 20s window. The briefing was skipped, the
    listener never armed, and the session sat dark until it was nudged by hand."""
    seen = _revive_harness(monkeypatch, {"11722": dict(ENTRY)})

    daemon.revive_one({}, "11722", dict(ENTRY), cause="auto", resume_choice="compact")

    assert seen["settle"] == [daemon.COMPACT_SETTLE]
    assert daemon.COMPACT_SETTLE > 110, "shorter than a measured compaction"


def test_a_full_resume_keeps_the_ordinary_briefing_window(monkeypatch):
    """No compaction runs, so the long wait would only delay every ordinary revive."""
    seen = _revive_harness(monkeypatch, {"11722": dict(ENTRY)})

    daemon.revive_one({}, "11722", dict(ENTRY), cause="auto", resume_choice="full")

    assert seen["settle"] == [None]


def test_an_unapplied_compact_choice_keeps_the_ordinary_window(monkeypatch):
    """If the picker was never answered, nothing is compacting — waiting ten minutes for an
    idle pane that is already idle just delays the briefing."""
    seen = _revive_harness(monkeypatch, {"11722": dict(ENTRY)}, picker="absent")

    daemon.revive_one({}, "11722", dict(ENTRY), cause="auto", resume_choice="compact")

    assert seen["settle"] == [None]


# ---- round 3: the fixes, and the paths that had no test at all -----------------

@pytest.mark.parametrize("word", ["!fully", "!!compact", "?full", "!full", "!!!fully"])
def test_a_sigil_message_is_never_an_answer(answer, word):
    """Round 3, finding 3. `.strip(".!?")` takes characters off BOTH ends, so the round-2
    "exact spellings only" fix did not hold: `!fully` still came out as `fully` and
    authorised the 352k full resume. That is C4 (their message was consumed, not delivered)
    and A3 (an expensive resume with no explicit answer) in one line of punctuation."""
    arm, calls = answer
    arm()
    assert daemon.check_pending_reopen({}, 11722, word) is False
    assert calls["full"] == [] and calls["compact"] == []
    assert "11722" in daemon.pending_reopens, "consumed the question on a non-answer"


def test_the_real_answers_still_work_after_the_punctuation_fix(answer):
    arm, calls = answer
    arm()
    assert daemon.check_pending_reopen({}, 11722, "compact!") is True
    assert calls["compact"] == ["11722"]


def test_a_prepared_question_is_re_asked_at_startup(tmp_path, monkeypatch, sent):
    """Round 2, finding 1's other half. A `prepared` record means the daemon died between
    persisting the question and delivering it. It blocks the auto-revive but cannot be
    answered, so without this it is a dead end with no way out but a human noticing."""
    monkeypatch.setattr(daemon, "read_registry", lambda: {"11722": dict(ENTRY)})
    monkeypatch.setattr(daemon, "pending_reopens",
                        {"11722": {"entry": dict(ENTRY), "tokens": 352_556,
                                   "state": "prepared"}})

    daemon.resend_undelivered_reopen_questions({})

    assert any("352,556" in m for m in sent), "the question was never re-delivered"
    assert daemon.pending_reopens["11722"]["state"] == "delivered"


def test_startup_drops_a_prepared_question_for_a_topic_that_moved_on(tmp_path, monkeypatch,
                                                                     sent):
    monkeypatch.setattr(daemon, "read_registry", lambda: {})   # no longer a dead session
    monkeypatch.setattr(daemon, "pending_reopens",
                        {"11722": {"entry": dict(ENTRY), "state": "prepared"}})

    daemon.resend_undelivered_reopen_questions({})

    assert daemon.pending_reopens == {} and sent == []


def test_a_send_that_raises_still_leaves_an_answerable_question(tmp_path, monkeypatch):
    """Round 3, finding 6. reply() can raise — PossiblyDelivered above all, which means they
    may well be looking at the question. The record stayed `prepared`, which is unanswerable
    by design and only re-asked at process start, so the topic was wedged until a restart."""
    _transcript(tmp_path, monkeypatch, [{"input_tokens": 1}])

    def _boom(cfg, tid, text):
        raise RuntimeError("possibly delivered")

    monkeypatch.setattr(daemon, "reply", _boom)

    assert daemon.offer_reopen_choice({}, 11722, ENTRY) is True
    assert daemon.pending_reopens["11722"]["state"] == "delivered", (
        "an ambiguous send left the question unanswerable until the daemon restarts"
    )


def test_a_large_session_is_asked_about_even_when_its_age_is_unreadable(monkeypatch):
    """Round 3, finding 8. The gate read the size, then resume_picker_expected read the
    transcript AGAIN for the age. A stat that failed the second time made the prediction say
    "no picker" for a session already measured at 200k — a known-large resume, no question.
    An unknown age is not permission; the size is what A3 is about."""
    assert daemon._needs_asking_for(200_000, None) is True
    assert daemon._needs_asking_for(None, None) is True
    assert daemon._needs_asking_for(20_000, None) is False
    assert daemon._needs_asking_for(200_000, 5.0) is False      # too new for Claude's picker


def test_concurrent_saves_never_publish_a_partial_file(tmp_path, monkeypatch):
    """Round 3, finding 4. Revive workers write this dict now, so two threads could open the
    SAME fixed `.tmp` with "w" and truncate one another's inode — one renames a half-written
    file into place while the other writes on through an orphaned descriptor, and the next
    _load_pending_reopens turns the corrupt JSON into {}. That is C5 failing silently."""
    import threading as _t
    store = tmp_path / "pending-reopens.json"
    monkeypatch.setattr(daemon, "state_path", lambda *p: str(store))
    shared = {}
    monkeypatch.setattr(daemon, "pending_reopens", shared)

    errors = []

    def writer(n):
        try:
            for i in range(40):
                with daemon._pending_reopen_lock:
                    shared[f"t{n}-{i}"] = {"entry": dict(ENTRY), "tokens": i,
                                           "state": "delivered"}
                assert _REAL_SAVE() is True
                loaded = daemon._load_pending_reopens()
                assert loaded, "published an empty file while memory held entries"
        except Exception as e:                       # noqa: BLE001 - reported below
            errors.append(e)

    threads = [_t.Thread(target=writer, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == [], f"concurrent save corrupted the store: {errors[:2]}"
    assert daemon._load_pending_reopens() == shared
    assert not list(tmp_path.glob("*.tmp")), "left temp files behind"


def test_a_stale_carry_forward_does_not_eat_the_reopen_answer(routed, sent, monkeypatch):
    """Round 3, finding 7. A carry-forward whose pane died leaves `_pending_cf` set until its
    worker's `finally` runs. Reopening the topic inside that window and answering the
    question hit the kill-switch first: it consumed the message to halt a flow that was
    already over, and C4's re-ask and delivery never happened. A pending reopen question
    means the session is dead, so there is no runaway continuation left to stop."""
    halted = []
    monkeypatch.setattr(daemon, "carry_forward_active", lambda tid: True)
    monkeypatch.setattr(daemon, "halt_carry_forward",
                        lambda *a, **k: halted.append(a) or True)
    revived = []
    monkeypatch.setattr(daemon, "_revive_with_choice",
                        lambda cfg, tid, entry, choice: revived.append(choice) or True)
    daemon.pending_reopens["11722"] = {"entry": dict(ENTRY), "tokens": 1, "state": "delivered"}

    daemon.handle_message(_CFG, _msg("compact"))

    assert halted == [], "a dead session's stale carry-forward swallowed the answer"
    assert revived == ["compact"]


def test_the_kill_switch_still_fires_when_no_question_is_pending(routed, sent, monkeypatch):
    """The exemption is scoped to a pending question. A live carry-forward must still be
    haltable by any message — that is the one message that stops a runaway continuation."""
    halted = []
    monkeypatch.setattr(daemon, "carry_forward_active", lambda tid: True)
    monkeypatch.setattr(daemon, "halt_carry_forward",
                        lambda *a, **k: halted.append(a) or True)

    daemon.handle_message(_CFG, _msg("stop doing that"))

    assert len(halted) == 1


def test_main_wires_the_startup_resend(monkeypatch):
    """The seam, not the function. Removing the call from main() left
    test_a_prepared_question_is_re_asked_at_startup green, because it invokes
    resend_undelivered_reopen_questions directly — a surviving mutation, and exactly the
    "tested the resolver, never the call site" gap that has bitten this repo before."""
    called = []
    monkeypatch.setattr(daemon, "load_config", lambda: {"chat_id": 1, "bot_token": "t"})
    monkeypatch.setattr(daemon, "load_offset", lambda: 0)
    monkeypatch.setattr(daemon, "restore_on_boot", lambda cfg: None)
    monkeypatch.setattr(daemon, "resend_undelivered_reopen_questions",
                        lambda cfg: called.append(True))
    monkeypatch.setattr(daemon.threading, "Thread",
                        lambda target=None, daemon=None, **kw: type(
                            "T", (), {"start": lambda self: None})())

    def _stop(*a, **k):
        raise KeyboardInterrupt          # BaseException: main's `except Exception` lets it out

    monkeypatch.setattr(daemon, "api", _stop)

    with pytest.raises(KeyboardInterrupt):
        daemon.main()

    assert called == [True], "main() never re-asks a question left undelivered by a crash"


def test_a_live_carry_forward_is_still_haltable_while_a_revive_is_running(routed, sent,
                                                                          monkeypatch):
    """Round 4, the one regression the round-3 commit introduced.

    Exempting the kill-switch on ANY pending record was worse than the bug it fixed.
    `reviving` outlives the moment revive_one clears `ended`, so the pane is live again while
    the record is still set; a real carry-forward can start in that window, and then every
    ordinary stop message would skip the halt AND be rejected as an answer. An unstoppable
    runaway continuation is precisely what #85's kill-switch exists to prevent."""
    halted = []
    monkeypatch.setattr(daemon, "carry_forward_active", lambda tid: True)
    monkeypatch.setattr(daemon, "halt_carry_forward",
                        lambda *a, **k: halted.append(a) or True)
    daemon.pending_reopens["11722"] = {"entry": dict(ENTRY), "tokens": 1, "state": "reviving"}

    daemon.handle_message(_CFG, _msg("stop runaway"))

    assert len(halted) == 1, "a live carry-forward became unstoppable during the revive"


def test_a_prepared_question_does_not_disarm_the_kill_switch_either(routed, sent, monkeypatch):
    """Same reasoning: only `delivered` means the session is dead and waiting on them."""
    halted = []
    monkeypatch.setattr(daemon, "carry_forward_active", lambda tid: True)
    monkeypatch.setattr(daemon, "halt_carry_forward",
                        lambda *a, **k: halted.append(a) or True)
    daemon.pending_reopens["11722"] = {"entry": dict(ENTRY), "tokens": 1, "state": "prepared"}

    daemon.handle_message(_CFG, _msg("stop runaway"))

    assert len(halted) == 1
# ---- #198: a topic whose session was never recorded ----------------------------

# The shape that ACTUALLY occurs: all 9 ended/no-session-id entries in the live registry —
# topic 15569 included — also have NO engine, because snapshot_once discarded it and cannot
# backfill an ended entry. Tests written with engine="codex" never exercised the real case.
NO_SID = {"name": "test-theme", "pane": "%162",
          "ended": "2026-08-26T11:24:22+0000", "session_id": None}
NO_SID_CODEX = dict(NO_SID, engine="codex")


def test_a_codex_session_killed_before_its_first_turn_is_unrevivable():
    """The live failure, 2026-08-26 topic 15569. Codex does not open its rollout-*.jsonl —
    and codex_session_id_for_pane reads the id from that open fd — until it has answered
    something. Measured: eight samples over two minutes returned None, then one real turn
    produced an id within 10s. So a session killed two minutes after spawning has no
    session_id at all, and should_auto_revive rejects it."""
    assert daemon.should_auto_revive(dict(NO_SID)) is False
    assert daemon.unrevivable_reason(dict(NO_SID)) == "no-session-id"


def test_a_resumable_or_live_or_feed_topic_is_not_called_unrevivable():
    assert daemon.unrevivable_reason(dict(ENTRY)) is None                    # has a session id
    assert daemon.unrevivable_reason({"session_id": None}) is None           # not ended
    assert daemon.unrevivable_reason(dict(NO_SID, feed=True)) is None        # a feed, not a session
    assert daemon.unrevivable_reason(None) is None


def test_reopening_an_unrevivable_topic_says_so_instead_of_nothing(monkeypatch, sent):
    """The defect the owner actually hit: they reopened the topic to demo the revive and got
    silence. A topic that is open while its session is gone, with nothing said, is the exact
    dead end #195 exists to remove — reached from a different direction."""
    monkeypatch.setattr(daemon, "read_registry", lambda: {"15569": dict(NO_SID)})
    # handle_message records the open/closed state BEFORE anything else, through the real
    # registry writer. Unstubbed, this test wrote topic 15569's live production entry — and
    # failed outright with EROFS where that directory is read-only, i.e. it reported on the
    # environment rather than on the code. Same trap as round 2's finding 9.
    monkeypatch.setattr(daemon, "set_topic_closed", lambda tid, closed: None)
    monkeypatch.setattr(daemon, "update_registry", lambda fn: None)
    revived = []
    monkeypatch.setattr(daemon, "maybe_auto_revive",
                        lambda cfg, tid: revived.append(str(tid)))

    daemon.handle_message(_CFG, {"chat": {"id": 1}, "from": {"id": 5}, "message_id": 3,
                                 "message_thread_id": 15569, "forum_topic_reopened": {}})

    assert sent, "reopening an unrevivable topic said nothing at all"
    assert "nothing to resume" in sent[0]
    assert revived == [], "tried to resume a session that does not exist"
    assert daemon.pending_reopens["15569"]["kind"] == "fresh"
    # No engine recorded — so it must NOT offer a bare `fresh`, which would guess.
    assert "will not guess" in sent[0]
    assert "`fresh codex`" in sent[0] and "`fresh claude`" in sent[0]


def test_a_known_codex_topic_is_offered_a_bare_fresh_and_the_likely_cause(monkeypatch, sent):
    monkeypatch.setattr(daemon, "read_registry", lambda: {"15569": dict(NO_SID_CODEX)})
    monkeypatch.setattr(daemon, "set_topic_closed", lambda tid, closed: None)
    monkeypatch.setattr(daemon, "update_registry", lambda fn: None)

    daemon.handle_message(_CFG, {"chat": {"id": 1}, "from": {"id": 5}, "message_id": 3,
                                 "message_thread_id": 15569, "forum_topic_reopened": {}})

    assert "`fresh` to start a NEW codex session" in sent[0]
    # ...and the cause is offered as the likely explanation, not asserted as established
    # fact: an id can also be missing because a lookup failed or the daemon was down.
    assert "Most likely" in sent[0] and "first turn" in sent[0]


def test_an_unknown_engine_is_never_guessed(monkeypatch, sent):
    """The blocker. All 9 ended id-less entries in the live registry — including the topic
    the owner actually hit — have NO engine, because snapshot_once discarded it and cannot backfill
    an ended entry. A bare `fresh` there would have launched Claude for their Codex topic and
    rebound the topic to Claude permanently."""
    monkeypatch.setattr(daemon, "read_registry", lambda: {"15569": dict(NO_SID)})
    started = []
    monkeypatch.setattr(daemon, "_start_fresh_session",
                        lambda cfg, tid, entry: started.append(entry.get("engine")) or True)
    daemon.pending_reopens["15569"] = {"entry": dict(NO_SID), "tokens": None,
                                       "state": "delivered", "kind": "fresh"}

    assert daemon.check_pending_reopen({}, 15569, "fresh") is False
    assert started == [], "guessed an engine for a topic that has none recorded"
    assert "will not guess" in sent[-1]

    assert daemon.check_pending_reopen({}, 15569, "fresh codex") is True
    assert started == ["codex"], "did not use the engine they named"


def test_answering_fresh_starts_a_new_session_in_the_same_topic(monkeypatch, sent):
    monkeypatch.setattr(daemon, "read_registry", lambda: {"15569": dict(NO_SID_CODEX)})
    started = {}
    monkeypatch.setattr(daemon, "revive_one",
                        lambda cfg, tid, entry, **kw: (started.update(kw, tid=str(tid))
                                                       or ("fresh", None)))
    monkeypatch.setattr(daemon.threading, "Thread",
                        lambda target=None, daemon=None, **kw: type(
                            "T", (), {"start": lambda self: target()})())
    daemon.pending_reopens["15569"] = {"entry": dict(NO_SID_CODEX), "tokens": None,
                                       "state": "delivered", "kind": "fresh"}

    assert daemon.check_pending_reopen({}, 15569, "fresh") is True

    assert started.get("tid") == "15569"
    assert started.get("fresh") is True, "resumed instead of starting fresh"
    assert started.get("fresh_requested") is True, (
        "a fresh session they ASKED for must not be described to it as a fallback"
    )
    assert daemon.pending_reopens == {}, "the worker did not consume the question"


def test_a_non_answer_re_asks_the_fresh_question_and_is_still_delivered(monkeypatch, sent):
    monkeypatch.setattr(daemon, "read_registry", lambda: {"15569": dict(NO_SID)})
    daemon.pending_reopens["15569"] = {"entry": dict(NO_SID), "tokens": None,
                                       "state": "delivered", "kind": "fresh"}

    assert daemon.check_pending_reopen({}, 15569, "what happened?") is False
    assert "nothing to resume" in sent[-1]
    assert daemon.pending_reopens["15569"]["kind"] == "fresh", "dropped the question"


def test_a_fresh_question_is_never_answered_by_a_resume_word(monkeypatch, sent):
    """`compact` and `full` mean nothing here — there is no context to compact. Sharing the
    resume path would also have destroyed these: it validates with should_auto_revive, which
    is False for these entries by definition."""
    monkeypatch.setattr(daemon, "read_registry", lambda: {"15569": dict(NO_SID)})
    revived = []
    monkeypatch.setattr(daemon, "_revive_with_choice",
                        lambda cfg, tid, entry, choice: revived.append(choice) or True)
    daemon.pending_reopens["15569"] = {"entry": dict(NO_SID), "tokens": None,
                                       "state": "delivered", "kind": "fresh"}

    assert daemon.check_pending_reopen({}, 15569, "compact") is False
    assert revived == []
    assert daemon.pending_reopens["15569"]["kind"] == "fresh"


def test_a_prepared_fresh_question_survives_a_restart(monkeypatch, sent):
    """The startup resend validated every record with should_auto_revive, which rejects these
    entries by definition — so it would have silently dropped exactly the questions this
    branch exists for, and re-worded the rest as a resume choice."""
    monkeypatch.setattr(daemon, "read_registry", lambda: {"15569": dict(NO_SID)})
    monkeypatch.setattr(daemon, "pending_reopens",
                        {"15569": {"entry": dict(NO_SID), "state": "prepared",
                                   "kind": "fresh"}})

    daemon.resend_undelivered_reopen_questions({})

    assert sent and "nothing to resume" in sent[0], "dropped or mis-worded on restart"
    assert daemon.pending_reopens["15569"]["state"] == "delivered"


def test_a_failed_fresh_start_stays_a_fresh_question(monkeypatch, sent):
    """Review round 1, finding 1. _rearm_after_failed_revive rebuilt the record without
    `kind`, so a failed fresh launch silently became a RESUME record: their next `fresh` then
    entered the resume branch, failed should_auto_revive — which rejects these entries by
    definition — and was discarded as "the topic changed". Neither answerable by the word it
    advertised nor revivable by the path it had switched to."""
    monkeypatch.setattr(daemon, "read_registry", lambda: {"15569": dict(NO_SID_CODEX)})
    monkeypatch.setattr(daemon, "revive_one", lambda cfg, tid, entry, **kw: ("failed", None))
    monkeypatch.setattr(daemon.threading, "Thread",
                        lambda target=None, daemon=None, **kw: type(
                            "T", (), {"start": lambda self: target()})())
    daemon.pending_reopens["15569"] = {"entry": dict(NO_SID_CODEX), "tokens": None,
                                       "state": "delivered", "kind": "fresh"}

    assert daemon.check_pending_reopen({}, 15569, "fresh") is True

    rec = daemon.pending_reopens["15569"]
    assert rec["kind"] == "fresh", "a failed fresh start turned into a resume question"
    assert rec["state"] == "delivered", "left unanswerable after the failure"
    assert any("reply `fresh` to retry" in m.lower() for m in sent)


def test_a_retry_after_a_failed_fresh_start_reaches_the_fresh_launcher(monkeypatch, sent):
    """The half that proves the record is still USABLE, not merely still labelled."""
    monkeypatch.setattr(daemon, "read_registry", lambda: {"15569": dict(NO_SID_CODEX)})
    started = []
    monkeypatch.setattr(daemon, "_start_fresh_session",
                        lambda cfg, tid, entry: started.append(entry.get("engine")) or True)
    # Start from a REAL fresh record and let the re-arm rebuild it. Hand-writing `kind` here
    # would prove only that a corrected record routes, which is not the claim (round 2).
    daemon.pending_reopens["15569"] = {"entry": dict(NO_SID_CODEX), "tokens": None,
                                       "state": "reviving", "kind": "fresh"}
    daemon._rearm_after_failed_revive({}, "15569", dict(NO_SID_CODEX))

    assert daemon.pending_reopens["15569"]["kind"] == "fresh"
    assert daemon.check_pending_reopen({}, 15569, "fresh") is True
    assert started == ["codex"]


def test_a_stale_fresh_record_cannot_disarm_the_kill_switch(routed, sent, monkeypatch):
    """Review round 1, finding 2. The fresh branch validated the registry only on an ANSWER,
    so a stale `delivered` record survived every non-answer. handle_message exempts a
    `delivered` record from the carry-forward kill-switch, so a live carry-forward on a topic
    that had since been rebound became unhaltable — reproduced with halt_calls=0 and the stop
    text merely queued."""
    halted = []
    monkeypatch.setattr(daemon, "carry_forward_active", lambda tid: True)
    monkeypatch.setattr(daemon, "halt_carry_forward",
                        lambda *a, **k: halted.append(a) or True)
    # The topic was rebound to a live, resumable session after the question was asked.
    monkeypatch.setattr(daemon, "read_registry", lambda: {"11722": dict(ENTRY)})
    daemon.pending_reopens["11722"] = {"entry": dict(NO_SID), "tokens": None,
                                       "state": "delivered", "kind": "fresh"}

    daemon.handle_message(_CFG, _msg("stop runaway"))

    assert len(halted) == 1, (
        "a live carry-forward stayed unhaltable because a STALE question was still delivered"
    )
    assert routed["inbox"] == [], "queued the halt message instead of halting"


def test_a_stale_fresh_record_is_dropped_on_a_non_answer_too(monkeypatch, sent):
    """Validation must run before BOTH branches, not only the answer one."""
    monkeypatch.setattr(daemon, "read_registry", lambda: {"15569": dict(ENTRY)})
    daemon.pending_reopens["15569"] = {"entry": dict(NO_SID), "tokens": None,
                                       "state": "delivered", "kind": "fresh"}

    assert daemon.check_pending_reopen({}, 15569, "anything at all") is False
    assert daemon.pending_reopens == {}
    assert "changed since I asked" in sent[-1]


# ---- review round 2: exact parsing, one parser, real identity ------------------

@pytest.mark.parametrize("word,known,expected", [
    ("fresh", "codex", "codex"),            # bare word, engine known
    ("fresh", None, None),                  # bare word, engine unknown -> not an answer
    ("fresh codex", None, "codex"),
    ("fresh claude", None, "claude"),
    ("new codex", None, "codex"),           # a synonym is still an answer
    ("заново claude", None, "claude"),
    ("fresh claude please", "codex", None),  # PREFIX matching launched claude here
    ("fresh codex is not what I want", None, None),
    ("fresh idea", "codex", None),          # 'idea' is not an engine -> not an answer
    ("codex fresh", None, None),            # not the advertised shape
    ("please start fresh codex", None, None),
    ("fresh gpt", None, None),
    ("", "codex", None),
])
def test_a_fresh_answer_is_parsed_exactly(word, known, expected):
    """Review round 2, regression 1. The parser checked token 1 for a fresh synonym and token
    2 for an engine and IGNORED everything after, so `fresh codex is not what I want`
    launched Codex. Launching the wrong engine rebinds the topic to it permanently, so this
    is the strict kind of parser."""
    assert daemon.fresh_answer_engine(word, known) == expected


def test_the_stale_branch_uses_the_same_parser_as_the_live_branch(monkeypatch, sent):
    """Review round 2, regression 2. The two branches disagreed: `fresh idea` was consumed
    and LOST by the stale branch while the live branch rejected it, and `new codex` was an
    answer to the live branch but ordinary text to the stale one."""
    monkeypatch.setattr(daemon, "read_registry", lambda: {"15569": dict(ENTRY)})  # moved on

    for word, consumed in (("fresh idea", False), ("new codex", True), ("fresh", True)):
        daemon.pending_reopens["15569"] = {"entry": dict(NO_SID_CODEX), "tokens": None,
                                           "state": "delivered", "kind": "fresh"}
        assert daemon.check_pending_reopen({}, 15569, word) is consumed, (
            f"{word!r}: stale branch disagreed with the live parser"
        )


def test_a_different_dead_session_in_the_same_topic_is_not_the_one_we_asked_about(monkeypatch,
                                                                                   sent):
    """Review round 2. unrevivable_reason alone is not identity — another ended, id-less
    session in the same topic satisfies it just as well, so a replaced binding read as
    unchanged. `ended` is the discriminator the resume path gets from `session_id`."""
    replaced = dict(NO_SID_CODEX, ended="2026-08-26T19:00:00+0000")
    monkeypatch.setattr(daemon, "read_registry", lambda: {"15569": replaced})
    started = []
    monkeypatch.setattr(daemon, "_start_fresh_session",
                        lambda cfg, tid, entry: started.append(entry) or True)
    daemon.pending_reopens["15569"] = {"entry": dict(NO_SID_CODEX), "tokens": None,
                                       "state": "delivered", "kind": "fresh"}

    assert daemon.check_pending_reopen({}, 15569, "fresh") is True
    assert started == [], "restarted a topic whose session had already been replaced"
    assert "changed since I asked" in sent[-1]


def test_a_current_question_still_exempts_the_kill_switch(routed, sent, monkeypatch,
                                                          tmp_path):
    """The exemption must survive its own hardening: a question that DOES still describe a
    dead topic is why #198's stale-carry-forward fix exists at all."""
    halted = []
    monkeypatch.setattr(daemon, "carry_forward_active", lambda tid: True)
    monkeypatch.setattr(daemon, "halt_carry_forward",
                        lambda *a, **k: halted.append(a) or True)
    monkeypatch.setattr(daemon, "read_registry", lambda: {"11722": dict(NO_SID_CODEX)})
    # The non-answer falls through to inbox routing, which resolves a REAL path under
    # ~/.local/share/agent-telegram-bridge and creates topics/11722 there. conftest isolates
    # only the `panes` subtree (round 3, low finding).
    monkeypatch.setattr(daemon, "state_path", lambda *p: str(tmp_path.joinpath(*p)))
    daemon.pending_reopens["11722"] = {"entry": dict(NO_SID_CODEX), "tokens": None,
                                       "state": "delivered", "kind": "fresh"}

    daemon.handle_message(_CFG, _msg("hello?"))

    assert halted == [], "halted a carry-forward for a session that is already dead"
    assert any("nothing to resume" in m for m in sent), "did not re-ask"


def test_a_broken_registry_read_still_halts_a_carry_forward(routed, sent, monkeypatch):
    """Review round 3. reopen_question_still_current is evaluated BEFORE the kill-switch's own
    try/except, so a raising registry read aborted handle_message outright — and main() still
    advances and saves the Telegram offset, so the one message that stops a runaway
    continuation was gone for good. Validation must fail toward halting."""
    halted = []
    monkeypatch.setattr(daemon, "carry_forward_active", lambda tid: True)
    monkeypatch.setattr(daemon, "halt_carry_forward",
                        lambda *a, **k: halted.append(a) or True)

    def _boom():
        raise OSError("registry unavailable")

    monkeypatch.setattr(daemon, "read_registry", _boom)
    daemon.pending_reopens["11722"] = {"entry": dict(NO_SID_CODEX), "tokens": None,
                                       "state": "delivered", "kind": "fresh"}

    daemon.handle_message(_CFG, _msg("stop runaway"))

    assert len(halted) == 1, "lost the halt message because validation raised"


# ---- #200: the window before compaction starts ---------------------------------


class _FastTime:
    """time.time that advances a second per call, so the real waits do not sleep."""

    def __init__(self):
        self._t = 0.0

    def time(self):
        self._t += 1.0
        return self._t

    def sleep(self, _s):
        pass


def _briefing_pane(monkeypatch, compacting, busy=None):
    """deliver_briefing against a scripted pane timeline.

    `compacting` and `busy` are per-sample sequences consumed by _cf_compacting / _cf_busy;
    the last value repeats. Records the sample index at which the briefing was typed, and any
    retry scheduled."""
    seen = {"typed_at": None, "n": 0, "retries": []}
    busy = busy if busy is not None else compacting

    def _at(seq):
        return seq[min(seen["n"], len(seq) - 1)]

    def _compacting(pane):
        v = _at(compacting)
        seen["n"] += 1
        return v

    monkeypatch.setattr(daemon, "_cf_compacting", _compacting)
    monkeypatch.setattr(daemon, "_cf_busy", lambda pane: _at(busy))
    monkeypatch.setattr(daemon, "pane_is_idle", lambda pane: not _at(busy))
    monkeypatch.setattr(daemon, "pane_alive", lambda pane: True)
    monkeypatch.setattr(daemon, "has_live_recv", lambda tid: False)
    monkeypatch.setattr(daemon, "read_registry", lambda: {"12999": {"pane": "%178"}})
    monkeypatch.setattr(daemon, "current_boot_id", lambda: "boot-x")
    monkeypatch.setattr(daemon, "update_registry", lambda fn: None)
    monkeypatch.setattr(daemon, "time", _FastTime())
    monkeypatch.setattr(daemon.threading, "Timer",
                        lambda delay, fn, args=None, kwargs=None: type(
                            "T", (), {"start": lambda self: seen["retries"].append(
                                (args, kwargs))})())

    def _type(pane, text, settle=0.4, still_ok=None):
        seen["typed_at"] = seen["n"]
        return "sent"

    monkeypatch.setattr(daemon, "type_line", _type)
    return seen


def test_a_compact_briefing_is_not_typed_into_the_pre_compaction_gap(monkeypatch):
    """#200, the original symptom. The pane is idle for about a second between the picker
    being answered and compaction rendering, so "idle now" was not "ready"."""
    # not compacting (the gap), then compacting, then done and staying idle.
    seen = _briefing_pane(monkeypatch, [False, True, True, False, False, False, False])

    daemon.deliver_briefing("%178", "12999", "claude", "brief {tid}",
                            await_busy=True, settle=daemon.COMPACT_SETTLE)

    assert seen["typed_at"] is not None, "never briefed at all"
    assert seen["typed_at"] >= 3, "typed during the pre-compaction gap"


def test_a_single_idle_repaint_during_compaction_does_not_release_the_briefing(monkeypatch):
    """Review finding 2. One idle capture is not compaction-complete — this file already
    requires CF_IDLE_SAMPLES consecutive not-busy samples for carry-forward, because a status
    repaint reads as idle for a single capture. `compacting, idle, compacting` must not
    deliver on that middle sample."""
    seen = _briefing_pane(monkeypatch,
                          [True, False, True, False, True, False, False, False, False])

    daemon.deliver_briefing("%178", "12999", "claude", "brief {tid}",
                            await_busy=True, settle=daemon.COMPACT_SETTLE)

    assert seen["typed_at"] is None or seen["typed_at"] >= 8, (
        f"delivered on a single idle repaint mid-compaction (sample {seen['typed_at']})"
    )


def test_a_capture_error_is_not_mistaken_for_compaction_starting(monkeypatch):
    """Review finding 1. `pane_is_idle` is `not _cf_busy()`, and _cf_busy reports BUSY for
    every capture error by design. A generic busy gate therefore accepted one transient
    capture failure as "compaction started" and then briefed in the gap. _cf_compacting is
    compaction-specific and fails to False."""
    # _cf_compacting never true (bad captures read False); _cf_busy true once — the error.
    seen = _briefing_pane(monkeypatch, [False] * 60,
                          busy=[True] + [False] * 59)

    daemon.deliver_briefing("%178", "12999", "claude", "brief {tid}",
                            await_busy=True, settle=daemon.COMPACT_SETTLE)

    assert seen["typed_at"] is not None, "a capture blip stranded the briefing entirely"
    # The blip must NOT be accepted as "compaction started". With a generic busy gate it is,
    # and the wait then releases after ~4 samples; the compaction-specific gate keeps looking
    # for a real compaction and consumes the whole grace first (measured: sample 19).
    assert seen["typed_at"] >= 10, (
        f"a transient capture error short-circuited the start gate (typed at "
        f"{seen['typed_at']})"
    )


def test_a_compaction_that_outlasts_the_window_retries_instead_of_going_dark(monkeypatch):
    """Review finding 3. The old code fell through to the ordinary 20s wait, returned without
    typing, and scheduled NOTHING — which is exactly how topic 12999 was left dark: its retry
    gave up while compaction was still running."""
    seen = _briefing_pane(monkeypatch, [True])          # compacting forever

    daemon.deliver_briefing("%178", "12999", "claude", "brief {tid}",
                            await_busy=True, settle=5)

    assert seen["typed_at"] is None, "injected mid-compaction"
    assert seen["retries"], "gave up on the briefing with no retry scheduled"
    args, kwargs = seen["retries"][0]
    assert args[4] == 2 and kwargs["await_busy"] is True, (
        "the retry dropped the compact context and will give up after RESTORE_SETTLE"
    )


def test_a_compact_briefing_is_not_stalled_when_compaction_never_starts(monkeypatch):
    """The grace must not become a new way to strand a session."""
    seen = _briefing_pane(monkeypatch, [False])

    daemon.deliver_briefing("%178", "12999", "claude", "brief {tid}",
                            await_busy=True, settle=daemon.COMPACT_SETTLE)

    assert seen["typed_at"] is not None, "stalled waiting for a compaction that never ran"


def test_an_ordinary_revive_does_not_wait_for_a_busy_phase(monkeypatch):
    """await_busy is opt-in: requiring a compaction on a plain resume would delay every
    ordinary revive by the whole grace window."""
    seen = _briefing_pane(monkeypatch, [False])

    daemon.deliver_briefing("%178", "12999", "claude", "brief {tid}")

    assert seen["typed_at"] == 0, "waited for a compaction on a plain resume"


def test_a_rebinding_during_the_compaction_wait_abandons_the_briefing(monkeypatch):
    """Review round 2. The ownership checks run BEFORE a wait that can now last
    COMPACT_SETTLE. Reproduced: attempt 2 validated 12999 -> %178, the binding moved to %999
    during the wait, and it typed into %178 then stamped briefed_boot on %999 — which had
    received nothing. That is the #133 r2 failure reached through a longer wait."""
    seen = _briefing_pane(monkeypatch, [True, True, False, False, False, False, False])
    registry = {"12999": {"pane": "%178"}}
    monkeypatch.setattr(daemon, "read_registry", lambda: registry)
    marked = []
    monkeypatch.setattr(daemon, "update_registry", lambda fn: marked.append(True))

    real_settled = daemon._compaction_settled

    def _settled(pane, tid, window):
        out = real_settled(pane, tid, window)
        registry["12999"]["pane"] = "%999"      # rebound while we waited
        return out

    monkeypatch.setattr(daemon, "_compaction_settled", _settled)

    daemon.deliver_briefing("%178", "12999", "claude", "brief {tid}",
                            await_busy=True, settle=daemon.COMPACT_SETTLE)

    assert seen["typed_at"] is None, "typed into a pane the topic had left"
    assert marked == [], "marked a pane briefed that never received the briefing"


def test_a_plain_first_attempt_now_consults_the_registry(monkeypatch):
    """CONTRACT CHANGE (#238). The inline path used to be exempt from revalidation, on the
    theory that the re-read races the binding write revive_one just handed down. It does
    not: update_registry commits under an exclusive file lock before deliver_briefing is
    called, so this chain's own bind is always visible — an absent or different binding
    means the topic moved on, and typing anyway puts one topic's operating instructions
    into another topic's pane (#236 review r1)."""
    seen = _briefing_pane(monkeypatch, [False])
    monkeypatch.setattr(daemon, "read_registry", lambda: {})     # topic no longer bound here

    daemon.deliver_briefing("%178", "12999", "claude", "brief {tid}")

    assert seen["typed_at"] is None, (
        "typed a first-attempt briefing into a pane the registry does not bind (#238)"
    )


def test_the_retry_carries_the_compaction_window_not_just_the_flag(monkeypatch):
    """Review round 2, finding 2. Asserting only await_busy let a mutation drop `settle`,
    which silently reverts the retry to RESTORE_SETTLE — it then gives up after 20s while
    compaction is still running, which is exactly how topic 12999 went dark."""
    seen = _briefing_pane(monkeypatch, [True])          # compacting forever

    daemon.deliver_briefing("%178", "12999", "claude", "brief {tid}",
                            await_busy=True, settle=5)

    assert seen["retries"], "no retry scheduled"
    _args, kwargs = seen["retries"][0]
    assert kwargs.get("settle") == 5 and kwargs.get("await_busy") is True


def test_a_rebind_during_the_ordinary_idle_wait_abandons_the_briefing(monkeypatch):
    """Review round 3. A plain first attempt still waits up to RESTORE_SETTLE in the ordinary
    idle loop, and that wait was not covered by the pre-type revalidation. Reproduced: the
    first sample was busy so the loop slept, the topic was rebound, the second sample released
    the wait, and the briefing went into the pane the topic had left."""
    seen = _briefing_pane(monkeypatch, [False])
    registry = {"12999": {"pane": "%178"}}
    monkeypatch.setattr(daemon, "read_registry", lambda: registry)

    # Busy first so the loop really sleeps, then idle so it releases. Without the advance the
    # pane looks permanently busy, the loop times out, and the test passes for no reason at
    # all — which is exactly how this one first survived its own mutation.
    samples = iter([False, True, True, True])

    def _idle(pane):
        out = next(samples, True)
        registry["12999"]["pane"] = "%999"     # rebound while the loop was sleeping
        return out

    monkeypatch.setattr(daemon, "pane_is_idle", _idle)

    daemon.deliver_briefing("%178", "12999", "claude", "brief {tid}")

    assert seen["typed_at"] is None, "typed into a pane the topic had left mid-wait"


def test_briefed_boot_is_never_stamped_on_a_pane_that_did_not_receive_it(monkeypatch):
    """Review round 3. Ownership can change while type_line waits for its pane lock and types.
    _mark stamped whatever pane was then bound, so the registry claimed a pane was briefed
    when it had received nothing — the #133 r2 failure at the far end of the function."""
    seen = _briefing_pane(monkeypatch, [False])
    registry = {"12999": {"pane": "%178"}}
    monkeypatch.setattr(daemon, "read_registry", lambda: registry)
    marks = []
    monkeypatch.setattr(daemon, "update_registry", lambda fn: (fn(registry), marks.append(1)))

    def _type(pane, text, settle=0.4, still_ok=None):
        seen["typed_at"] = seen["n"]
        registry["12999"]["pane"] = "%999"     # rebound while we were typing
        return "sent"

    monkeypatch.setattr(daemon, "type_line", _type)

    daemon.deliver_briefing("%178", "12999", "claude", "brief {tid}")

    assert seen["typed_at"] is not None, "did not deliver at all"
    assert "briefed_boot" not in registry["12999"], (
        "stamped briefed_boot on %999, which never received the briefing"
    )
