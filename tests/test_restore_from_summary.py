"""#277 — a mass restore resumes a large session from its SUMMARY, not its full context.

The message-revive path already asks the owner (#195). Boot and recovery restores revived
unconditionally in full, so a fleet restore re-read a ~350k-token session twice as fresh cache
writes on the most expensive model, for a session that then sat idle all day. Nobody is at the
terminal on boot to answer Claude's own picker, so the daemon answers it: above Claude's own
thresholds the prompt cache has long expired, and the full re-read buys nothing the summary
does not.

Everything here composes with machinery that already existed — `answer_resume_picker`,
`resume_picker_expected`, and `revive_one`'s `resume_choice`. The only new decisions are WHEN
to make the choice automatically and how long a restore may wait for the pickers.
"""

import time

import pytest

from bridge import daemon


BIG = daemon.RESUME_MODAL_TOKENS + 1
OLD = daemon.RESUME_MODAL_AGE_MINUTES + 1


class Revive:
    """Drives revive_one against fakes, recording what reached the picker."""

    def __init__(self, monkeypatch, sample=(BIG, OLD), engine="claude", picker="answered"):
        self.answered = []          # (pane, choice, deadline)
        self.replies = []           # (tid, text)
        self.launched = []          # launch command lines
        self.sample = sample
        self.picker = picker

        monkeypatch.setattr(daemon, "session_cost_sample", lambda sid, cwd: self.sample)
        monkeypatch.setattr(daemon, "engine_of_pane", lambda pane: engine)
        monkeypatch.setattr(daemon, "_tmux", lambda *a, **k: type(
            "R", (), {"returncode": 1, "stdout": ""})())
        monkeypatch.setattr(daemon, "launch_pane",
                            lambda name, cwd, launch, engine, reason:
                            (self.launched.append(launch), "%9")[1])
        monkeypatch.setattr(daemon, "_revive_tmux_name", lambda entry, tid, taken: "rv")
        monkeypatch.setattr(daemon, "reply",
                            lambda cfg, tid, text: self.replies.append((str(tid), text)))
        monkeypatch.setattr(daemon, "deliver_briefing", lambda *a, **k: None)
        monkeypatch.setattr(daemon, "update_registry", lambda fn: None)
        monkeypatch.setattr(daemon, "read_registry", lambda: {})
        monkeypatch.setattr(daemon, "last_model_and_effort_for_session",
                            lambda sid, cwd: ("claude-opus-5", None))
        monkeypatch.setattr(daemon, "_safe_peek", lambda pane, lines=40: "")
        monkeypatch.setattr(daemon, "reopen_topic", lambda *a, **k: True)
        monkeypatch.setattr(daemon, "current_boot_id", lambda: "boot-1")

        def _answer(pane, choice, deadline=None):
            self.answered.append((pane, choice, deadline))
            return self.picker

        monkeypatch.setattr(daemon, "answer_resume_picker", _answer)

    @property
    def choices(self):
        return [c for _p, c, _d in self.answered]


ENTRY = {"name": "big seat", "session_id": "SID", "cwd": "/", "engine": "claude"}


# --------------------------------------------------------------------------- C1


def test_a_large_old_session_is_resumed_from_summary_on_a_mass_restore(monkeypatch):
    r = Revive(monkeypatch)

    status, task = daemon.revive_one({}, "33", dict(ENTRY), brief=False, auto_summary=True)

    assert r.choices == ["compact"]
    assert status == "resumed"
    assert task["resume_choice"] == "compact"
    # it really resumed — the summary choice must never turn into a fresh session
    assert any("--resume" in line for line in r.launched)


def test_without_auto_summary_nothing_is_chosen(monkeypatch):
    # the pre-#277 behaviour, still the default for every other caller
    r = Revive(monkeypatch)

    daemon.revive_one({}, "33", dict(ENTRY), brief=False)

    assert r.answered == []


# --------------------------------------------------------------------------- C2


@pytest.mark.parametrize("sample", [
    (BIG, daemon.RESUME_MODAL_AGE_MINUTES),          # big but not old enough
    (daemon.RESUME_MODAL_TOKENS, OLD),               # old but not big enough
    (None, OLD),                                     # size unreadable
    (BIG, None),                                     # age unreadable
    (None, None),
])
def test_below_either_threshold_or_unknown_no_choice_is_made(monkeypatch, sample):
    r = Revive(monkeypatch, sample=sample)

    daemon.revive_one({}, "33", dict(ENTRY), brief=False, auto_summary=True)

    assert r.answered == []
    assert not any("picker" in t for _tid, t in r.replies), \
        "a session we never chose for must not be warned about a picker"


def test_a_fresh_spawn_is_never_given_a_choice(monkeypatch):
    # A fresh session shows no picker, so a choice here would trip the "you chose X but the
    # picker never appeared" warning on every reopened session.
    r = Revive(monkeypatch)

    daemon.revive_one({}, "33", dict(ENTRY), fresh=True, brief=False, auto_summary=True)

    assert r.answered == []
    assert not any("picker" in t for _tid, t in r.replies)


def test_an_entry_with_no_session_id_is_never_given_a_choice(monkeypatch):
    r = Revive(monkeypatch)
    entry = dict(ENTRY)
    entry.pop("session_id")

    status, _task = daemon.revive_one({}, "33", entry, brief=False, auto_summary=True)

    assert r.answered == [] and status == "fresh"


def test_a_codex_session_is_never_given_a_choice(monkeypatch):
    r = Revive(monkeypatch, engine="codex")
    monkeypatch.setattr(daemon, "ensure_codex_trust", lambda cwd: None)
    entry = dict(ENTRY, engine="codex")

    daemon.revive_one({}, "33", entry, brief=False, auto_summary=True)

    assert r.answered == []


# --------------------------------------------------------------------------- C3


def test_an_explicit_choice_from_the_caller_wins(monkeypatch):
    r = Revive(monkeypatch)

    daemon.revive_one({}, "33", dict(ENTRY), brief=False, auto_summary=True,
                      resume_choice="full")

    assert r.choices == ["full"], "the owner's own answer must not be overridden"


def test_the_message_revive_path_is_unchanged_when_it_passes_no_choice(monkeypatch):
    # auto_summary defaults False, so #195's own flow keeps deciding for itself.
    r = Revive(monkeypatch)

    daemon.revive_one({}, "33", dict(ENTRY), brief=False, cause="reopen")

    assert r.answered == []


# --------------------------------------------------------------------------- C4


def test_the_callers_deadline_is_the_one_the_picker_gets(monkeypatch):
    # Above MIN_PICKER_WINDOW, so the choice is taken; the deadline is passed through
    # untouched rather than being recomputed per session.
    r = Revive(monkeypatch)
    before = time.time()
    given = before + daemon.MIN_PICKER_WINDOW + 30

    daemon.revive_one({}, "33", dict(ENTRY), brief=False, auto_summary=True,
                      picker_deadline=given)

    _pane, _choice, deadline = r.answered[0]
    assert deadline == pytest.approx(given, abs=1)


def test_a_mass_restore_bounds_the_total_picker_wait(monkeypatch):
    """C4: N large sessions must not each add RESUME_MODAL_WAIT before the daemon polls.

    A fake that returns instantly cannot show this — every per-session deadline would sit
    inside the budget anyway, and the test would pass with the shared budget deleted (found by
    review r1, mutation 3). So the clock ADVANCES here, one full per-session wait per target,
    which is the case the budget exists for.
    """
    seen = []
    clock = [1_000_000.0]
    monkeypatch.setattr(daemon.time, "time", lambda: clock[0])

    def _revive(cfg, tid, info, **kw):
        seen.append((tid, kw.get("auto_summary"), kw.get("picker_deadline")))
        clock[0] += daemon.RESUME_MODAL_WAIT      # this session used its whole wait
        return "resumed", {"pane": "%1", "tid": tid, "engine": "claude", "tpl": "",
                           "needs_brief": False, "reopened": None,
                           "resume_choice": "compact"}

    monkeypatch.setattr(daemon, "revive_one", _revive)
    monkeypatch.setattr(daemon, "save_boot_id", lambda b: None)
    monkeypatch.setattr(daemon, "api", lambda *a, **k: {})
    targets = [(str(t), {"name": f"s{t}"}) for t in range(1, 21)]

    start = clock[0]
    daemon._restore_targets_now({"bot_token": "T", "chat_id": 1}, "boot", targets)

    assert [t for t, _a, _d in seen] == [str(t) for t in range(1, 21)]
    assert all(auto for _t, auto, _d in seen), "a mass restore opts every target in"
    # THE property: one budget for the whole restore. Without it the last target's deadline
    # would be its own start + RESUME_MODAL_WAIT, far past the budget.
    ceiling = start + daemon.RESTORE_PICKER_BUDGET
    assert all(d <= ceiling for _t, _a, d in seen), "a per-session wait escaped the budget"
    unbounded = start + 20 * daemon.RESUME_MODAL_WAIT
    assert seen[-1][2] < unbounded - daemon.RESUME_MODAL_WAIT
    # and the budget really is smaller than doing it per session
    assert daemon.RESTORE_PICKER_BUDGET < 20 * daemon.RESUME_MODAL_WAIT


def test_once_the_budget_is_spent_no_choice_is_claimed(monkeypatch):
    """C4. An expired deadline makes `answer_resume_picker` return without looking at the pane.

    Claiming `compact` anyway would leave a rendered picker UNANSWERED — the dark-session bug
    — while telling the owner "you chose compact", a choice they never made. Declining restores
    the pre-#277 path exactly (review r1, C4).
    """
    r = Revive(monkeypatch)

    status, task = daemon.revive_one({}, "33", dict(ENTRY), brief=False, auto_summary=True,
                                     picker_deadline=time.time() - 1)

    assert r.answered == [], "no answer attempt with no time to make one"
    assert task["resume_choice"] is None
    assert not any("You chose" in t for _tid, t in r.replies), \
        "never attribute to the owner a choice they did not make"
    assert status == "resumed"


@pytest.mark.parametrize("outcome", ["absent", "failed"])
def test_a_daemon_choice_that_did_not_land_is_never_reported_as_the_owners(
        monkeypatch, outcome):
    """C4/C1, as the PROPERTY rather than as another timing instance.

    Reviews r2, r3 and r4 each found a different step between the budget check and the picker
    call — sizing, then the pane launch, then a flushing log() — and there is always one more.
    So the reporting no longer depends on the timing: a choice the DAEMON made is never
    attributed to the owner, whatever consumed the window.
    """
    r = Revive(monkeypatch, picker=outcome)
    monkeypatch.setattr(daemon, "_resume_picker_present", lambda screen: False)

    daemon.revive_one({}, "33", dict(ENTRY), brief=False, auto_summary=True)

    assert not any("You chose" in t for _tid, t in r.replies)
    # nothing on the pane -> it simply resumed in full, the ordinary pre-#277 outcome
    assert r.replies == [] or all("resume picker" not in t for _tid, t in r.replies)


@pytest.mark.parametrize("outcome", ["absent", "failed"])
def test_a_picker_left_up_by_a_failed_daemon_choice_is_reported_as_unanswered(
        monkeypatch, outcome):
    # The dark-session case: the pane IS a modal and cannot receive work. It must be said,
    # and said the same way as when there was no answer to give in the first place.
    r = Revive(monkeypatch, picker=outcome)
    monkeypatch.setattr(daemon, "_resume_picker_present", lambda screen: True)

    daemon.revive_one({}, "33", dict(ENTRY), brief=False, auto_summary=True)

    assert not any("You chose" in t for _tid, t in r.replies)
    assert any(t == daemon.UNANSWERED_PICKER_NOTICE for _tid, t in r.replies)


@pytest.mark.parametrize("outcome", ["absent", "failed"])
def test_an_OWNERS_choice_that_did_not_land_is_still_reported_to_them(monkeypatch, outcome):
    # The loud warning must survive for the case it was written for: the owner asked for
    # something and did not get it. Only the daemon's own choice is exempt.
    r = Revive(monkeypatch, picker=outcome)

    daemon.revive_one({}, "33", dict(ENTRY), brief=False, resume_choice="compact")

    assert any("You chose `compact`" in t for _tid, t in r.replies)


@pytest.mark.parametrize("slow_step", ["sizing", "launch"])
def test_no_choice_is_claimed_if_any_step_before_the_picker_eats_the_budget(
        monkeypatch, slow_step):
    """C4, as a class rather than as the two instances review found one at a time.

    Between deciding and answering there are two slow steps — sizing the session (r2) and
    launching the pane (r3). A budget read before either says nothing about whether the
    picker can still be answered, so the gate sits immediately before the picker call with
    nothing in between. Enumerate the steps instead of patching whichever one was reported.
    """
    r = Revive(monkeypatch)
    clock = [1_000_000.0]
    monkeypatch.setattr(daemon.time, "time", lambda: clock[0])
    deadline = clock[0] + daemon.MIN_PICKER_WINDOW + 1     # open when we start

    def _burn(*a, **k):
        clock[0] += 30                                     # ...and shut by this step
        return None

    if slow_step == "sizing":
        monkeypatch.setattr(daemon, "session_cost_sample",
                            lambda sid, cwd: (_burn(), (BIG, OLD))[1])
    else:
        monkeypatch.setattr(daemon, "launch_pane",
                            lambda name, cwd, launch, engine, reason: (_burn(), "%9")[1])

    status, task = daemon.revive_one({}, "33", dict(ENTRY), brief=False, auto_summary=True,
                                     picker_deadline=deadline)

    assert r.answered == [], f"{slow_step} ate the budget but a choice was still claimed"
    assert task["resume_choice"] is None
    assert not any("You chose" in t for _tid, t in r.replies), \
        "never attribute to the owner a choice the daemon made and could not deliver"
    assert status == "resumed"


@pytest.mark.parametrize("remaining", [-3600, -1, 0, daemon.MIN_PICKER_WINDOW - 1])
def test_no_choice_for_any_non_positive_or_too_small_window(monkeypatch, remaining):
    r = Revive(monkeypatch)

    daemon.revive_one({}, "33", dict(ENTRY), brief=False, auto_summary=True,
                      picker_deadline=time.time() + remaining)

    assert r.answered == []
    assert not any("You chose" in t for _tid, t in r.replies)


def test_no_deadline_at_all_means_no_budget_applies(monkeypatch):
    # the single-session paths pass none; they must keep working exactly as before
    r = Revive(monkeypatch)

    daemon.revive_one({}, "33", dict(ENTRY), brief=False, auto_summary=True,
                      picker_deadline=None)

    assert r.choices == ["compact"]


def test_a_deadline_with_room_left_still_takes_the_choice(monkeypatch):
    r = Revive(monkeypatch)

    daemon.revive_one({}, "33", dict(ENTRY), brief=False, auto_summary=True,
                      picker_deadline=time.time() + daemon.RESUME_MODAL_WAIT)

    assert r.choices == ["compact"]


def test_the_restore_summary_counts_and_names_the_summarised_sessions(monkeypatch):
    posted = []

    def _revive(cfg, tid, info, **kw):
        choice = "compact" if tid == "33" else None
        return "resumed", {"pane": "%1", "tid": tid, "engine": "claude", "tpl": "",
                           "needs_brief": False, "reopened": None, "resume_choice": choice}

    monkeypatch.setattr(daemon, "revive_one", _revive)
    monkeypatch.setattr(daemon, "save_boot_id", lambda b: None)
    monkeypatch.setattr(daemon, "api",
                        lambda _t, _m, params: posted.append(params["text"]) or {})

    daemon._restore_targets_now({"bot_token": "T", "chat_id": 1}, "boot",
                                [("33", {"name": "big seat"}), ("34", {"name": "small"})])

    text = posted[0]
    assert "1 resumed from summary." in text
    assert "from summary: big seat" in text
    assert "small" not in text.split("from summary:")[1]


def test_a_restore_reports_nothing_about_summaries_when_none_were_made(monkeypatch):
    posted = []
    monkeypatch.setattr(daemon, "revive_one", lambda cfg, tid, info, **kw: (
        "resumed", {"pane": "%1", "tid": tid, "engine": "claude", "tpl": "",
                    "needs_brief": False, "reopened": None, "resume_choice": None}))
    monkeypatch.setattr(daemon, "save_boot_id", lambda b: None)
    monkeypatch.setattr(daemon, "api",
                        lambda _t, _m, params: posted.append(params["text"]) or {})

    daemon._restore_targets_now({"bot_token": "T", "chat_id": 1}, "boot",
                                [("34", {"name": "small"})])

    assert "from summary" not in posted[0]


def test_a_revive_that_raises_does_not_stop_the_restore(monkeypatch):
    posted = []
    calls = []

    def _revive(cfg, tid, info, **kw):
        calls.append(tid)
        if tid == "33":
            raise RuntimeError("launch exploded")
        return "resumed", {"pane": "%1", "tid": tid, "engine": "claude", "tpl": "",
                           "needs_brief": False, "reopened": None, "resume_choice": None}

    monkeypatch.setattr(daemon, "revive_one", _revive)
    monkeypatch.setattr(daemon, "save_boot_id", lambda b: None)
    monkeypatch.setattr(daemon, "api",
                        lambda _t, _m, params: posted.append(params["text"]) or {})

    daemon._restore_targets_now({"bot_token": "T", "chat_id": 1}, "boot",
                                [("33", {"name": "boom"}), ("34", {"name": "ok"})])

    assert calls == ["33", "34"]
    assert "1 failed" in posted[0]


# --------------------------------------------------------------------------- C5


def test_size_and_age_come_from_one_sample(monkeypatch):
    """C5: reading them separately let the halves disagree — see `_needs_asking_for`."""
    calls = []
    monkeypatch.setattr(daemon, "session_cost_sample",
                        lambda sid, cwd: (calls.append((sid, cwd)), (BIG, OLD))[1])

    assert daemon.resume_picker_expected("SID", "/") is True
    assert len(calls) == 1


@pytest.mark.parametrize("tokens,age,want", [
    (BIG, OLD, True),
    (BIG, daemon.RESUME_MODAL_AGE_MINUTES, False),
    (daemon.RESUME_MODAL_TOKENS, OLD, False),
    (None, OLD, False),
    (BIG, None, False),
    (None, None, False),
])
def test_the_prediction_is_pure_and_unknown_is_never_yes(tokens, age, want):
    # Deliberately the OPPOSITE of _needs_asking_for, where unknown means ask: this one
    # predicts Claude's behaviour, and an unknown gives no grounds to expect a picker.
    assert daemon._picker_expected_for(tokens, age) is want


def test_a_sampling_failure_still_revives_the_session_in_full(monkeypatch):
    """C5. A cost optimisation must never cost the revive.

    Letting a transcript read failure escape would turn "resume this session in full" into
    "this session did not come back at all" — strictly worse than the re-read #277 avoids.
    """
    r = Revive(monkeypatch)

    def _boom(sid, cwd):
        raise OSError("transcript vanished")

    monkeypatch.setattr(daemon, "session_cost_sample", _boom)

    status, task = daemon.revive_one({}, "33", dict(ENTRY), brief=False, auto_summary=True)

    assert status == "resumed", "the session must still come back"
    assert any("--resume" in line for line in r.launched)
    assert r.answered == [] and task["resume_choice"] is None
    assert not any("picker" in t for _tid, t in r.replies)
