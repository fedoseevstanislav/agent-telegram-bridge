"""Bridge-driven compactions run on Sonnet 5.5 xhigh, then switch back (#353).

The fake Claude below behaves the way a probe pane on Claude Code 2.1.286 did (2026-09-30):
`/model` on a cached conversation opens "Switch model?" and does nothing until it is
answered; after a compaction it switches without asking; each command writes its
`<local-command-stdout>` record to the transcript and rewrites settings.json.
"""

import contextlib
import json

import pytest

from bridge import daemon, transcript

PREV_MODEL, PREV_EFFORT = "claude-opus-5-5", "high"
SETTINGS_BYTES = b'{\n  "model": "claude-fable-5-1[1m]",\n  "effortLevel": "medium"\n}'
DIALOG = ("   Switch model?\n   Your next response will be slower and use more tokens\n"
          "   ❯ 1. Yes, switch to Sonnet 5.5\n     2. No, go back\n")


def _record(path, rec):
    with open(path, "a") as f:
        f.write(json.dumps(rec) + "\n")


class FakeClaude:
    def __init__(self, tmp_path, *, confirm=True, effort=PREV_EFFORT, model=PREV_MODEL):
        self.transcript = str(tmp_path / "session.jsonl")
        msg = {"model": model, "content": [{"type": "text", "text": "done"}]}
        rec = {"type": "assistant", "message": msg}
        if effort:
            rec["effort"] = effort
        _record(self.transcript, rec)
        # A synthetic record after it must be ignored (C2: last NON-synthetic assistant).
        _record(self.transcript, {"type": "assistant", "message": {"model": "<synthetic>"}})
        self.settings = tmp_path / "claude" / "settings.json"
        self.settings.parent.mkdir()
        self.settings.write_bytes(SETTINGS_BYTES)
        self.cached, self.confirm = True, confirm
        self.dialog = None
        self.typed, self.enters = [], 0
        self.on_type = None

    def _apply(self, text):
        cmd, arg = text.split(" ", 1)
        if not self.confirm:
            return
        _record(self.transcript, {"type": "user", "message": {"content": (
            f"<command-name>{cmd}</command-name>\n<command-message>{cmd[1:]}"
            f"</command-message>\n<command-args>{arg}</command-args>")}})
        out = (f"Set model to `{arg}` and saved as your default for new sessions"
               if cmd == "/model" else
               f"Set effort level to {arg} (saved as your default for new sessions): x")
        _record(self.transcript, {"type": "user", "message": {
            "content": f"<local-command-stdout>{out}</local-command-stdout>"}})
        # What the real client does to the global file on every one of these.
        self.settings.write_bytes(self.settings.read_bytes() + f"\n// {arg}\n".encode())

    def type_line(self, pane, text, settle=0.3, still_ok=None):
        self.typed.append(text)
        if text.startswith("/model "):
            if self.cached:
                self.dialog = text
            else:
                self._apply(text)
        elif text.startswith("/effort "):
            self._apply(text)
        elif text == "/compact":
            self.cached = False
        if self.on_type:
            self.on_type(text)
        return "sent"

    def tmux(self, argv, **kw):
        out = ""
        if "capture-pane" in argv:
            out = DIALOG if self.dialog else "❯ \n"
        elif "send-keys" in argv and argv[-1] == "Enter":
            self.enters += 1
            if self.dialog:
                text, self.dialog = self.dialog, None
                self._apply(text)
        return type("R", (), {"returncode": 0, "stdout": out})()

    def commands(self):
        return [t for t in self.typed if not t.startswith("[tg-bridge] Your carry-forward")]


@pytest.fixture
def fake(tmp_path, monkeypatch):
    f = FakeClaude(tmp_path)
    _install(monkeypatch, f)
    return f


def _install(monkeypatch, f):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(f.settings.parent))
    monkeypatch.setattr(daemon.transcript, "transcript_path", lambda cwd, sid: f.transcript)
    monkeypatch.setattr(daemon, "type_line", f.type_line)
    monkeypatch.setattr(daemon, "_tmux", f.tmux)
    monkeypatch.setattr(daemon, "_pane_lock", lambda pane, timeout=None: contextlib.nullcontext())
    monkeypatch.setattr(daemon, "_argv_model", lambda pane: None)
    monkeypatch.setattr(daemon, "pane_alive", lambda pane: True)
    monkeypatch.setattr(daemon, "log", lambda *a, **k: None)
    monkeypatch.setattr(daemon.time, "sleep", lambda *a, **k: None)


def _worker(monkeypatch, fake, *, engine="claude"):
    """Drive the real carry-forward worker (compact-only default) around the fake pane."""
    replies = []
    monkeypatch.setattr(daemon, "CARRY_FORWARD", False)
    monkeypatch.setattr(daemon, "read_registry", lambda: {"7033": {
        "session_id": "SID", "cwd": "/srv/seat", "engine": engine}})
    monkeypatch.setattr(daemon, "_cf_wait_idle", lambda *a, **k: "idle")
    monkeypatch.setattr(daemon, "_cf_clear_modal", lambda *a, **k: None)
    monkeypatch.setattr(daemon, "_cf_capture_tail", lambda pane: "")
    monkeypatch.setattr(daemon, "_cf_clear_unfinished", lambda *a, **k: None)
    monkeypatch.setattr(daemon, "_cf_await_started", lambda *a, **k: ("compacting", None))
    monkeypatch.setattr(daemon, "_cf_await_compacted", lambda *a, **k: ("compacted", None))
    monkeypatch.setattr(daemon, "reply", lambda cfg, tid, text: replies.append(text) or True)
    monkeypatch.setattr(daemon, "load_config", lambda: {})
    monkeypatch.setattr(daemon, "_pending_cf",
                        {"7033": {"token": "t", "pane": "%0", "phase": "settle"}})
    return replies


def _run(monkeypatch, fake, **kw):
    replies = _worker(monkeypatch, fake, **kw)
    daemon._carry_forward_worker({}, 7033, "%0", "/x/cf.md", "/x/cf.md.done", "t", "seat")
    return replies


# ---- C1 + C3: switch, answer the dialog, compact, switch back ----

def test_compaction_runs_on_sonnet_and_switches_back_after_it(monkeypatch, fake):
    replies = _run(monkeypatch, fake)
    assert fake.commands() == [
        f"/model {daemon.COMPACT_MODEL}", f"/effort {daemon.COMPACT_EFFORT}", "/compact",
        f"/model {PREV_MODEL}", f"/effort {PREV_EFFORT}", daemon.COMPACT_RESUME_PROMPT]
    assert daemon.COMPACT_MODEL == "claude-sonnet-5-5[1m]" and daemon.COMPACT_EFFORT == "xhigh"
    assert any("Compaction done" in r for r in replies)
    assert not any("could not confirm" in r for r in replies)


def test_the_switch_model_dialog_is_answered_once_and_the_flow_does_not_wait_on_it(
        monkeypatch, fake):
    _run(monkeypatch, fake)
    # One dialog (the cached conversation, before compaction), one Enter. After compaction
    # the fake, like the real client, switches without asking.
    assert fake.enters == 1
    assert fake.dialog is None


def test_a_dialog_nobody_confirms_does_not_hang_the_flow(monkeypatch, tmp_path):
    f = FakeClaude(tmp_path, confirm=False)
    _install(monkeypatch, f)
    monkeypatch.setattr(daemon, "MODEL_CMD_WAIT", 0.05)
    _run(monkeypatch, f)  # returning at all is the check: MODEL_CMD_WAIT bounds each wait
    # Neither switch confirmed → the switch back was attempted, and nothing was compacted.
    assert f.commands() == [f"/model {daemon.COMPACT_MODEL}", f"/model {PREV_MODEL}"]
    assert not daemon.carry_forward_active(7033)


# ---- C2: previous model and effort come from the session's own record ----

def test_previous_pair_comes_from_the_transcript_not_the_global_settings(monkeypatch, fake):
    plan = daemon.compaction_plan("SID", "/srv/seat", "%0")
    assert (plan["model"], plan["effort"]) == (PREV_MODEL, PREV_EFFORT)
    assert b"fable" in SETTINGS_BYTES  # the global default says something else entirely


@pytest.mark.parametrize("effort, model", [(None, PREV_MODEL), (PREV_EFFORT, None)])
def test_unknown_model_or_effort_compacts_exactly_as_before(monkeypatch, tmp_path, effort, model):
    f = FakeClaude(tmp_path, effort=effort, model=model or "<synthetic>")
    _install(monkeypatch, f)
    _run(monkeypatch, f)
    assert f.commands() == ["/compact", daemon.COMPACT_RESUME_PROMPT]
    assert f.settings.read_bytes() == SETTINGS_BYTES


def test_already_on_exactly_the_compaction_pair_sends_no_switch(monkeypatch, tmp_path):
    f = FakeClaude(tmp_path, model="claude-sonnet-5-5", effort="xhigh")
    _install(monkeypatch, f)
    monkeypatch.setattr(daemon, "_argv_model", lambda pane: "claude-sonnet-5-5[1m]")
    _run(monkeypatch, f)
    assert f.commands() == ["/compact", daemon.COMPACT_RESUME_PROMPT]


def test_the_standard_window_of_the_same_model_is_still_switched(monkeypatch, tmp_path):
    # Review r1, C1: `claude-sonnet-5-5` is not `claude-sonnet-5-5[1m]`; and an effort that is
    # already xhigh still gets its `/effort xhigh`.
    f = FakeClaude(tmp_path, model="claude-sonnet-5-5", effort="xhigh")
    _install(monkeypatch, f)
    _run(monkeypatch, f)
    assert f.commands()[:3] == [f"/model {daemon.COMPACT_MODEL}",
                                f"/effort {daemon.COMPACT_EFFORT}", "/compact"]
    assert f.commands()[3:5] == ["/model claude-sonnet-5-5", "/effort xhigh"]


def test_a_failed_switch_goes_back_before_compacting(monkeypatch, fake):
    # Review r1, C1: never compact on a model nobody confirmed.
    monkeypatch.setattr(daemon, "MODEL_CMD_WAIT", 0.05)
    orig = fake._apply
    fake._apply = lambda text: None if text.startswith("/effort xhigh") else orig(text)
    _worker(monkeypatch, fake)
    starts = iter([("timeout", None), ("compacting", None)])  # first /compact never starts
    monkeypatch.setattr(daemon, "_cf_await_started", lambda *a, **k: next(starts))
    daemon._carry_forward_worker({}, 7033, "%0", "/x/cf.md", "/x/cf.md.done", "t", "seat")
    typed = fake.commands()
    i = typed.index("/compact")
    assert typed[:i] == [f"/model {daemon.COMPACT_MODEL}", f"/effort {daemon.COMPACT_EFFORT}",
                         f"/model {PREV_MODEL}", f"/effort {PREV_EFFORT}"]
    assert typed.count(f"/model {daemon.COMPACT_MODEL}") == 1  # no second switch on retry


def test_a_synthetic_assistant_record_is_not_the_previous_model(monkeypatch, fake):
    # Review r1, C2: an isMeta assistant record after the real one must be skipped.
    _record(fake.transcript, {"type": "assistant", "isMeta": True, "effort": "low",
                              "message": {"model": "claude-fable-5-1"}})
    _record(fake.transcript, {"type": "user", "effort": "low",
                              "message": {"model": "claude-fable-5-1"}})
    plan = daemon.compaction_plan("SID", "/srv/seat", "%0")
    assert (plan["model"], plan["effort"]) == (PREV_MODEL, PREV_EFFORT)


def test_the_context_window_is_restored_from_the_newest_typed_model(monkeypatch, fake):
    # Assistant records say `claude-opus-5-5` for both windows; the /model that set it keeps
    # the suffix, and restoring without it would shrink the session's window.
    fake._apply("/model claude-opus-5-5[1m]")
    fake.settings.write_bytes(SETTINGS_BYTES)
    assert daemon.compaction_plan("SID", "/srv/seat", "%0")["model"] == "claude-opus-5-5[1m]"


def test_the_context_window_falls_back_to_the_launch_argv(monkeypatch, fake):
    monkeypatch.setattr(daemon, "_argv_model", lambda pane: "claude-opus-5-5[1m]")
    assert daemon.compaction_plan("SID", "/srv/seat", "%0")["model"] == "claude-opus-5-5[1m]"
    # An argv naming a different model is not evidence about this one.
    monkeypatch.setattr(daemon, "_argv_model", lambda pane: "claude-fable-5-1[1m]")
    assert daemon.compaction_plan("SID", "/srv/seat", "%0")["model"] == PREV_MODEL


# ---- C4: settings.json byte-identical ----

def test_settings_json_is_byte_identical_after_the_flow(monkeypatch, fake):
    seen = []
    fake.on_type = lambda text: seen.append(fake.settings.read_bytes())
    _run(monkeypatch, fake)
    assert fake.settings.read_bytes() == SETTINGS_BYTES
    # And the fake really did rewrite it in between, so the check above is not vacuous.
    assert daemon._read_bytes(str(fake.settings)) == SETTINGS_BYTES
    assert any(b != SETTINGS_BYTES for b in seen)


def test_settings_json_is_put_back_even_when_a_halt_lands_mid_switch(monkeypatch, fake):
    _halt_when(monkeypatch, fake, f"/effort {daemon.COMPACT_EFFORT}")
    _run(monkeypatch, fake)
    assert fake.settings.read_bytes() == SETTINGS_BYTES


# ---- C5: an unconfirmed restore gets exactly one notice naming the model ----

def test_unconfirmed_restore_sends_one_notice_naming_the_model_left_on(monkeypatch, fake):
    monkeypatch.setattr(daemon, "MODEL_CMD_WAIT", 0.05)

    def stop_confirming(text):
        if text == "/compact":
            fake.confirm = False
    fake.on_type = stop_confirming
    replies = _run(monkeypatch, fake)
    notices = [r for r in replies if "could not confirm the switch back" in r]
    assert len(notices) == 1
    assert f"The session is on `{daemon.COMPACT_MODEL}` at `{daemon.COMPACT_EFFORT}`" in notices[0]
    assert f"/model {PREV_MODEL}" in notices[0]


def test_a_failed_compaction_still_switches_back(monkeypatch, fake):
    _worker(monkeypatch, fake)
    monkeypatch.setattr(daemon, "_cf_await_compacted", lambda *a, **k: ("timeout", None))
    daemon._carry_forward_worker({}, 7033, "%0", "/x/cf.md", "/x/cf.md.done", "t", "seat")
    assert fake.commands()[-2:] == [f"/model {PREV_MODEL}", f"/effort {PREV_EFFORT}"]


# ---- C6: codex topics are unchanged ----

def test_codex_topics_get_no_model_switch(monkeypatch, fake):
    _worker(monkeypatch, fake, engine="codex")
    assert daemon._cf_compaction_plan("7033", "t", "%0") is None
    assert "model_plan" not in daemon._pending_cf["7033"]


# ---- C7: an owner message halts every new phase, and says where the model is ----

def _halt_when(monkeypatch, fake, trigger):
    """Halt from the owner's side the first time `trigger` has been typed, at the point the
    worker next looks at the pane — outside every lock, as a real Telegram message would."""
    state = {"done": False, "replies": []}

    def alive(pane):
        if not state["done"] and trigger in fake.typed:
            state["done"] = True
            daemon.halt_carry_forward({}, 7033, "owner message")
        return True
    monkeypatch.setattr(daemon, "pane_alive", alive)
    return state


@pytest.mark.parametrize("trigger, phase_left_on", [
    (f"/model {daemon.COMPACT_MODEL}", (PREV_MODEL, PREV_EFFORT)),
    (f"/model {PREV_MODEL}", (daemon.COMPACT_MODEL, daemon.COMPACT_EFFORT)),
])
def test_owner_message_halts_the_switch_and_restore_phases(monkeypatch, fake, trigger,
                                                            phase_left_on):
    _halt_when(monkeypatch, fake, trigger)
    replies = _run(monkeypatch, fake)
    typed = fake.commands()
    after = typed[typed.index(trigger) + 1:]
    assert after == [], f"typed after the halt: {after}"
    assert daemon.COMPACT_RESUME_PROMPT not in typed
    halt = [r for r in replies if r.startswith("🛑 Carry-forward halted")]
    assert len(halt) == 1
    model, effort = phase_left_on
    assert f"`{model}` at `{effort}` effort" in halt[0]
    assert f"`{trigger}` was sent" in halt[0]
    assert not any("could not confirm" in r for r in replies)  # a halt is not a failure


def test_the_new_phases_are_named_on_the_flow_record(monkeypatch, fake):
    phases = []
    real = daemon._cf_set_phase
    monkeypatch.setattr(daemon, "_cf_set_phase",
                        lambda tid, token, phase: phases.append(phase) or real(tid, token, phase))
    _run(monkeypatch, fake)
    assert phases[:4] == ["compact", "switch", "compact", "restore"]


# ---- the reopen path: absent picker → bridge /compact ----

def test_reopen_compaction_switches_first_and_the_briefing_switches_back(monkeypatch, fake):
    monkeypatch.setattr(daemon, "pane_is_idle", lambda pane: True)
    monkeypatch.setattr(daemon, "_cf_capture_tail", lambda pane: "")
    monkeypatch.setattr(daemon, "_cf_compacting", lambda pane: True)
    replies = []
    monkeypatch.setattr(daemon, "reply", lambda cfg, tid, text: replies.append(text) or True)
    monkeypatch.setattr(daemon, "load_config", lambda: {})
    plan = daemon.compaction_plan("SID", "/srv/seat", "%77", PREV_MODEL, PREV_EFFORT)

    assert daemon._compact_after_absent_picker("%77", "302", plan) == ("injected", None)
    assert fake.commands() == [f"/model {daemon.COMPACT_MODEL}",
                               f"/effort {daemon.COMPACT_EFFORT}", "/compact"]

    monkeypatch.setattr(daemon, "_compaction_settled", lambda *a: True)
    monkeypatch.setattr(daemon, "has_live_recv", lambda tid: False)
    monkeypatch.setattr(daemon, "_briefing_still_ours", lambda *a: True)
    monkeypatch.setattr(daemon, "update_registry", lambda fn: None)
    daemon.deliver_briefing("%77", "302", "claude", "BRIEF {tid}", await_busy=True,
                            settle=1, restore=plan)
    assert fake.commands()[3:] == [f"/model {PREV_MODEL}", f"/effort {PREV_EFFORT}",
                                   "BRIEF 302"]
    assert fake.settings.read_bytes() == SETTINGS_BYTES
    assert replies == []


@pytest.mark.parametrize("live_recv, ours", [(True, True), (False, False)])
def test_reopen_switch_back_happens_even_when_the_briefing_is_skipped(monkeypatch, fake,
                                                                      live_recv, ours):
    # Review r1, C5: a live recv (attempt 1) or a rebound topic (a retry) skips the briefing;
    # neither may skip the switch back.
    monkeypatch.setattr(daemon, "_compaction_settled", lambda *a: True)
    monkeypatch.setattr(daemon, "has_live_recv", lambda tid: live_recv)
    monkeypatch.setattr(daemon, "_briefing_still_ours", lambda *a: ours)
    plan = daemon.compaction_plan("SID", "/srv/seat", "%77", PREV_MODEL, PREV_EFFORT)
    plan["now"] = {"model": daemon.COMPACT_MODEL, "effort": daemon.COMPACT_EFFORT}
    daemon.deliver_briefing("%77", "302", "claude", "BRIEF {tid}", attempt=1 if ours else 2,
                            await_busy=True, settle=1, restore=plan)
    assert fake.commands() == [f"/model {PREV_MODEL}", f"/effort {PREV_EFFORT}"]


def test_reopen_compaction_that_never_starts_switches_back_at_once(monkeypatch, fake):
    monkeypatch.setattr(daemon, "pane_is_idle", lambda pane: True)
    monkeypatch.setattr(daemon, "_cf_capture_tail", lambda pane: "")
    monkeypatch.setattr(daemon, "_cf_compacting", lambda pane: False)
    monkeypatch.setattr(daemon, "_cf_hook_block_reason", lambda *a: "blocked: [x] no")
    plan = daemon.compaction_plan("SID", "/srv/seat", "%77", PREV_MODEL, PREV_EFFORT)
    assert daemon._compact_after_absent_picker("%77", "302", plan)[0] == "refused"
    assert fake.commands()[-2:] == [f"/model {PREV_MODEL}", f"/effort {PREV_EFFORT}"]


@pytest.mark.parametrize("effort", [PREV_EFFORT, None])
def test_revive_hands_the_launch_pair_to_the_compaction_and_the_briefing(monkeypatch, effort):
    seen, briefings = [], []
    monkeypatch.setattr(daemon, "_tmux",
                        lambda *a, **k: type("R", (), {"returncode": 1, "stdout": ""})())
    monkeypatch.setattr(daemon, "launch_pane", lambda *a, **k: ("%77", None))
    monkeypatch.setattr(daemon, "_revive_tmux_name", lambda *a: "revive")
    monkeypatch.setattr(daemon, "last_model_and_effort_for_session",
                        lambda *a: (PREV_MODEL, effort))
    monkeypatch.setattr(daemon, "read_registry", lambda: {})
    monkeypatch.setattr(daemon, "update_registry", lambda fn: None)
    monkeypatch.setattr(daemon, "reopen_topic", lambda *a: True)
    monkeypatch.setattr(daemon, "current_boot_id", lambda: "boot")
    monkeypatch.setattr(daemon, "answer_resume_picker", lambda *a, **k: "absent")
    monkeypatch.setattr(daemon, "reply", lambda cfg, tid, text: True)
    monkeypatch.setattr(daemon, "log", lambda *a, **k: None)
    monkeypatch.setattr(daemon, "_argv_model", lambda pane: None)
    monkeypatch.setattr(daemon, "_compact_after_absent_picker",
                        lambda pane, tid, plan: seen.append(plan) or ("injected", None))
    monkeypatch.setattr(daemon, "deliver_briefing",
                        lambda pane, tid, engine, tpl, **kw: briefings.append(kw))
    entry = {"name": "seat", "session_id": "SID", "cwd": "/srv/seat", "engine": "claude"}
    daemon.revive_one({}, "302", entry, cause="reopen", resume_choice="compact")
    if effort:
        assert (seen[0]["model"], seen[0]["effort"]) == (PREV_MODEL, PREV_EFFORT)
        assert briefings[0]["restore"] is seen[0]
    else:
        assert seen == [None]
        assert "restore" not in briefings[0]


def test_transcript_readers_ignore_prose_about_the_commands():
    prose = {"type": "assistant", "message": {"content": [{"type": "text", "text": (
        "<command-name>/model</command-name><command-args>claude-x[1m]</command-args> "
        "<local-command-stdout>Set model to x</local-command-stdout>")}]}}
    assert transcript.last_model_command([prose]) is None
    assert not transcript.local_stdout_has([prose], "Set model to")


# ---- review r2 ----

def test_no_compaction_when_neither_the_switch_nor_the_switch_back_confirms(monkeypatch, fake):
    # C1: a session on an unknown model is not compacted, and the one notice says so.
    monkeypatch.setattr(daemon, "MODEL_CMD_WAIT", 0.05)
    fake.confirm = False
    replies = _run(monkeypatch, fake)
    assert "/compact" not in fake.commands()
    notices = [r for r in replies if "could not confirm the switch back" in r]
    assert len(notices) == 1 and notices[0].startswith("⚠️ I did not compact")
    assert not daemon.carry_forward_active(7033)


def test_reopen_does_not_compact_when_neither_switch_confirms(monkeypatch, fake):
    monkeypatch.setattr(daemon, "MODEL_CMD_WAIT", 0.05)
    monkeypatch.setattr(daemon, "pane_is_idle", lambda pane: True)
    monkeypatch.setattr(daemon, "load_config", lambda: {})
    monkeypatch.setattr(daemon, "reply", lambda cfg, tid, text: True)
    fake.confirm = False
    plan = daemon.compaction_plan("SID", "/srv/seat", "%77", PREV_MODEL, PREV_EFFORT)
    assert daemon._compact_after_absent_picker("%77", "302", plan) == ("failed", None)
    assert "/compact" not in fake.commands()


def test_a_halt_right_after_a_command_is_submitted_names_it(monkeypatch, fake):
    # C7: the owner's halt waits on _cf_lock, so the earliest it can run is the instant the
    # worker's guard releases it after typing `/model`. Halt exactly there.
    replies = _worker(monkeypatch, fake)
    fake.cached = False
    real_guard, state = daemon._cf_guard, {"done": False}

    def guard_then_halt(tid, token):
        inner = real_guard(tid, token)

        @contextlib.contextmanager
        def guard():
            with inner() as ok:
                yield ok
            if not state["done"] and f"/model {daemon.COMPACT_MODEL}" in fake.typed:
                state["done"] = True
                daemon.halt_carry_forward({}, 7033, "owner message")
        return guard
    monkeypatch.setattr(daemon, "_cf_guard", guard_then_halt)
    daemon._carry_forward_worker({}, 7033, "%0", "/x/cf.md", "/x/cf.md.done", "t", "seat")
    halt = [r for r in replies if r.startswith("🛑 Carry-forward halted")]
    assert len(halt) == 1
    assert f"`/model {daemon.COMPACT_MODEL}` was sent" in halt[0]
    assert fake.commands() == [f"/model {daemon.COMPACT_MODEL}"]
