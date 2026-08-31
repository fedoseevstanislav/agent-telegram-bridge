"""Unit tests for the /carry forward command (#85) — the pure, side-effect-free
pieces: command matching, precise per-line busy detection, post-/compact modal
detection, the deterministic done-marker gate, the kill-switch-safe ownership-gated
inject, and the 'always a durable GitHub issue' verification/fallback. The full
pane-driving flow is exercised against a throwaway tmux pane, not here."""

import types

import pytest

from bridge import daemon


# ---- command matching --------------------------------------------------------

def test_matches_cf_alias():
    assert daemon.is_carry_forward_command("/cf")
    assert daemon.is_carry_forward_command("/cf now")


def test_matches_carry_forward_two_words():
    assert daemon.is_carry_forward_command("/carry forward")
    assert daemon.is_carry_forward_command("/carry FORWARD")  # case-insensitive
    assert daemon.is_carry_forward_command("/carry")          # bare /carry is lenient


def test_matches_carryforward_no_space():
    # `/carryforward` (no space) is the primary form the owner uses; the daemon must
    # intercept it in a bridge topic (it has to run /compact, which the model can't).
    assert daemon.is_carry_forward_command("/carryforward")
    assert daemon.is_carry_forward_command("/carryforward now")


def test_does_not_match_unrelated_commands():
    assert not daemon.is_carry_forward_command("/compact")
    assert not daemon.is_carry_forward_command("/model opus")
    assert not daemon.is_carry_forward_command("/carrying on")  # /carrying != /carry
    assert not daemon.is_carry_forward_command("")


# ---- precise per-line busy detection -----------------------------------------

def test_busy_footer_is_busy():
    tail = "✻ Working… (12s · ↑ 1.2k tokens · esc to interrupt)"
    assert daemon._cf_line_is_busy(tail)


def test_compaction_footer_is_busy():
    assert daemon._cf_line_is_busy("Compacting conversation… (8s · esc to interrupt)")


def test_compaction_bar_is_busy():
    assert daemon._cf_line_is_busy("▰▰▰▰▱▱▱▱ compacting")


def test_idle_scrollback_is_not_busy():
    # Idle scrollback that merely mentions work-like words must NOT read as busy.
    tail = "I finished the work. Brewed some coffee. Let me know what to do next.\n> "
    assert not daemon._cf_text_is_busy(tail)


def test_v21_spinner_timer_reads_as_busy():
    # Claude Code v2.1.206 no longer renders "esc to interrupt"; an active turn shows
    # only the live spinner elapsed-timer "…(Ns · …)". That MUST read as busy.
    for line in ("✽ Mulling… (10s · ↓ 97 tokens)",
                 "✢ Baking… (4s · thinking with xhigh effort)"):
        assert daemon._cf_line_is_busy(line), line


def test_completed_command_frozen_duration_is_not_busy():
    # BLOCKER 1: a COMPLETED tool-result row leaves a frozen "(8s)" duration in
    # scrollback. A bare "(\d+s" scan matched it → an idle pane read "busy forever".
    # The ⎿ tool-result row must NOT read as busy.
    for line in ("  ⎿  $ sleep 20 && echo hi (8s)",
                 "  ⎿  Ran background shell (12s)",
                 "⎿  Read 40 lines (3s)"):
        assert not daemon._cf_line_is_busy(line), line


def test_idle_pane_with_completed_command_in_scrollback_is_not_busy():
    # BLOCKER 1 (whole-capture): an idle pane whose 25-line tail contains a completed
    # command row plus the idle prompt must read NOT busy — this is the exact hang the
    # e2e missed (busy-forever → /cf timeout).
    capture = (
        "> run a quick sleep\n"
        "  ⎿  $ sleep 20 && echo hi (8s)\n"
        "     hi\n"
        "✻ Cooked for 9s\n"
        "❯ \n"
    )
    assert not daemon._cf_text_is_busy(capture)


def test_live_spinner_amid_scrollback_is_busy():
    # The inverse: a genuinely live spinner line anywhere in the capture reads busy.
    capture = (
        "  ⎿  $ sleep 20 && echo hi (8s)\n"
        "✽ Mulling… (10s · ↓ 97 tokens)\n"
    )
    assert daemon._cf_text_is_busy(capture)


def test_idle_done_summary_and_prompt_read_as_not_busy():
    # The idle done-summary ("Cooked for 14s", no parenthesis), the statusline, the
    # cwd header, and a bare input prompt must all read as NOT busy.
    for line in ("✻ Cooked for 14s",
                 "✻ Baked for 2m 56s",
                 "scratch • Sonnet 5 • 7m 75% W90% • $0.32 • 5%",
                 "/…/scratchpad/cf-e2e/scratch",
                 "❯ "):
        assert not daemon._cf_line_is_busy(line), line


def test_pane_is_idle_delegates_to_busy_detector(monkeypatch):
    # Same root fix on the /model path: pane_is_idle is the negation of _cf_busy.
    monkeypatch.setattr(daemon, "_cf_busy", lambda pane: True)
    assert daemon.pane_is_idle("%1") is False
    monkeypatch.setattr(daemon, "_cf_busy", lambda pane: False)
    assert daemon.pane_is_idle("%1") is True


# ---- compaction-SPECIFIC detection (#101) ------------------------------------

def test_compacting_footer_reads_as_compacting():
    # The live "Compacting conversation… (Ns)" status IS a compaction signal.
    assert daemon._cf_line_is_compacting("Compacting conversation… (8s · esc to interrupt)")


def test_compaction_bar_with_phrase_reads_as_compacting():
    # The ▰▱ progress bar on the compaction status line (phrase present, before the timer).
    assert daemon._cf_line_is_compacting("▰▰▰▰▱▱▱▱ Compacting conversation…")


def test_idle_scrollback_mentioning_compacting_is_not_compacting():
    # #101 review r1 (BLOCKING): idle prose / residue that merely MENTIONS the phrase, with
    # no live "…(Ns" timer or ▰▱ bar, must NOT read as a live compaction — else /compact that
    # never started would false-succeed off stale scrollback (the bug this PR fixes, again).
    for line in ("I checked the Compacting conversation footer and found no issue.",
                 "See the note about 'Compacting conversation' behaviour in #101.",
                 "❯ Compacting conversation"):
        assert not daemon._cf_line_is_compacting(line), line
    capture = (
        "I checked the Compacting conversation footer and found no issue.\n"
        "✻ Cooked for 9s\n"
        "❯ \n"
    )
    assert not daemon._cf_text_is_compacting(capture)  # idle pane, phrase in scrollback


def test_progress_bar_without_phrase_is_not_compacting():
    # A ▰▱ progress bar from some other tool (e.g. a download) is generic-busy but is NOT a
    # compaction — the bar alone must not be read as compacting (review r1 second sample).
    assert daemon._cf_line_is_busy("Downloading data ▰▰▱▱ 50%")          # still generic-busy
    assert not daemon._cf_line_is_compacting("Downloading data ▰▰▱▱ 50%")  # but NOT compacting


def test_ordinary_busy_turn_is_not_compacting():
    # The crux of #101: an ordinary active turn (or a /compact QUEUED behind one) is
    # GENERIC-busy but must NOT read as compacting — that misread was the false success
    # that left t212 uncompacted. Each of these is busy, yet none is a compaction.
    for line in ("✻ Working… (12s · ↑ 1.2k tokens · esc to interrupt)",
                 "✽ Mulling… (10s · ↓ 97 tokens)",
                 "✢ Baking… (4s · thinking with xhigh effort)",
                 "  ⎿  Ran background shell (12s)"):
        assert daemon._cf_line_is_busy(line) or line.lstrip().startswith("⎿"), line
        assert not daemon._cf_line_is_compacting(line), line


def test_completed_tool_row_mentioning_compacting_is_not_compacting():
    # A completed "⎿ …" tool-result row is never a live status, even if its text happens
    # to contain the word — guard mirrors _cf_line_is_busy.
    assert not daemon._cf_line_is_compacting("  ⎿  Wrote Compacting conversation notes (3s)")


def test_text_is_compacting_finds_live_line_amid_scrollback():
    capture = (
        "  ⎿  $ sleep 20 && echo hi (8s)\n"
        "❯ /compact\n"
        "Compacting conversation… (8s · esc to interrupt)\n"
    )
    assert daemon._cf_text_is_compacting(capture)


def test_text_is_not_compacting_for_ordinary_busy_capture():
    # A capture of an ordinary busy turn (the exact #101 false-positive) is NOT compacting.
    capture = (
        "> continue the ingestion\n"
        "✽ Mulling… (10s · ↓ 97 tokens · esc to interrupt)\n"
    )
    assert not daemon._cf_text_is_compacting(capture)


def test_wait_compacting_saw_compaction(monkeypatch):
    monkeypatch.setattr(daemon, "pane_alive", lambda pane: True)
    monkeypatch.setattr(daemon, "_cf_owns", lambda tid, token: True)
    monkeypatch.setattr(daemon, "_cf_compacting", lambda pane: True)
    assert daemon._cf_wait_compacting("1", "t", "%1", 1) == "compacting"
    monkeypatch.setattr(daemon, "_cf_compacting", lambda pane: False)
    assert daemon._cf_wait_compacting("1", "t", "%1", 0.2) == "timeout"


# ---- post-/compact modal detection (SHOULD-FIX 4) ----------------------------

def test_resume_from_summary_modal_detected():
    modal = (
        "Compact conversation?\n"
        "❯ 1. Resume from summary (recommended)\n"
        "  2. Keep full conversation"
    )
    assert daemon._cf_modal_present(modal)


def test_switch_style_confirm_detected():
    assert daemon._cf_modal_present("❯ 1. Yes, proceed\n  2. No")


def test_plain_compaction_footer_is_not_a_modal():
    # The compaction progress footer must NOT be mistaken for a selectable modal.
    assert not daemon._cf_modal_present("Compacting conversation… (8s · esc to interrupt)")


def test_normal_prompt_is_not_a_modal():
    assert not daemon._cf_modal_present("> \nType your message…")


def test_bare_input_cursor_is_not_a_modal():
    # The idle input cursor "❯ " (no numbered/keyword option after it) is not a modal.
    assert not daemon._cf_modal_present("❯ \nType your message…")


def test_prose_mentioning_recommended_is_not_a_modal():
    # SHOULD-FIX 4: review/doc text that merely says "recommended)" or lists a numbered
    # "Resume from summary" WITHOUT the live ❯ selection cursor must NOT trigger a stray
    # Enter. The old regex matched "recommended)" / "Resume from summary" as plain text.
    prose = (
        "In the review I noted option 1. Resume from summary (recommended) is safer,\n"
        "but the daemon should not send Enter just because this text is on screen.\n"
        "See the docs section 'Resume from summary' for details.\n"
        "❯ "
    )
    assert not daemon._cf_modal_present(prose)


# ---- deterministic done-marker gate (bug #2) ---------------------------------

def _fast_gate(monkeypatch):
    monkeypatch.setattr(daemon, "CF_IDLE_INTERVAL", 0.01)
    monkeypatch.setattr(daemon, "CF_IDLE_SAMPLES", 2)
    monkeypatch.setattr(daemon, "pane_alive", lambda pane: True)
    monkeypatch.setattr(daemon, "_cf_owns", lambda tid, token: True)


def test_wait_done_returns_done_when_marker_present_and_idle(monkeypatch, tmp_path):
    _fast_gate(monkeypatch)
    monkeypatch.setattr(daemon, "_cf_busy", lambda pane: False)
    cf = tmp_path / "cf.md"
    cf.write_text("carry-forward body")
    marker = tmp_path / "cf.md.done"
    marker.write_text("")
    assert daemon._cf_wait_done("1", "t", "%1", str(cf), str(marker), 2) == "done"


def test_wait_done_times_out_without_marker(monkeypatch, tmp_path):
    _fast_gate(monkeypatch)
    monkeypatch.setattr(daemon, "_cf_busy", lambda pane: False)
    cf = tmp_path / "cf.md"
    cf.write_text("body")
    marker = tmp_path / "cf.md.done"  # absent
    assert daemon._cf_wait_done("1", "t", "%1", str(cf), str(marker), 0.2) == "timeout"


def test_wait_done_requires_idle_even_with_marker(monkeypatch, tmp_path):
    _fast_gate(monkeypatch)
    monkeypatch.setattr(daemon, "_cf_busy", lambda pane: True)
    cf = tmp_path / "cf.md"
    cf.write_text("body")
    marker = tmp_path / "cf.md.done"
    marker.write_text("")
    assert daemon._cf_wait_done("1", "t", "%1", str(cf), str(marker), 0.2) == "timeout"


def test_wait_done_aborts_when_flow_popped(monkeypatch, tmp_path):
    _fast_gate(monkeypatch)
    monkeypatch.setattr(daemon, "_cf_owns", lambda tid, token: False)  # halted/superseded
    monkeypatch.setattr(daemon, "_cf_busy", lambda pane: False)
    cf = tmp_path / "cf.md"
    cf.write_text("body")
    marker = tmp_path / "cf.md.done"
    marker.write_text("")
    assert daemon._cf_wait_done("1", "t", "%1", str(cf), str(marker), 2) == "aborted"


def test_wait_busy_saw_busy_first(monkeypatch):
    monkeypatch.setattr(daemon, "pane_alive", lambda pane: True)
    monkeypatch.setattr(daemon, "_cf_owns", lambda tid, token: True)
    monkeypatch.setattr(daemon, "_cf_busy", lambda pane: True)
    assert daemon._cf_wait_busy("1", "t", "%1", 1) == "busy"
    monkeypatch.setattr(daemon, "_cf_busy", lambda pane: False)
    assert daemon._cf_wait_busy("1", "t", "%1", 0.2) == "timeout"


# ---- kill-switch-safe ownership-gated inject (BLOCKER 2) ---------------------

class _RunRecorder:
    """Records tmux invocations; returns a benign success result (with optional
    capture stdout for capture-pane calls).

    `echo=True` models a pane that ACCEPTS typed text: since #133 the carry-forward inject
    verifies its text rendered before pressing Enter, so a recorder whose capture-pane
    always answers "" models a pane that swallowed the input and correctly gets no Enter."""
    def __init__(self, capture_stdout="", echo=False):
        self.calls = []
        self._capture_stdout = capture_stdout
        self._echo = echo
        self._typed = []

    def __call__(self, argv, *a, **k):
        self.calls.append(argv)
        stdout = self._capture_stdout
        if "display-message" in argv:
            # Stable geometry fingerprint (#165): type_line discards a capture taken across a
            # resize, so a pane that echoes must also report a pane that isn't moving.
            return types.SimpleNamespace(returncode=0, stdout="100,40,80,0", stderr="")
        if self._echo:
            if "capture-pane" in argv:
                stdout = "\n".join([stdout] + self._typed) if stdout else "\n".join(self._typed)
            elif "-l" in argv:
                self._typed.append(argv[-1])
        return types.SimpleNamespace(returncode=0, stdout=stdout, stderr="")

    def sent_keys(self):
        return [c for c in self.calls if "send-keys" in c]


def test_inject_owned_sends_when_owned(monkeypatch):
    rec = _RunRecorder(echo=True)
    monkeypatch.setattr(daemon.subprocess, "run", rec)
    monkeypatch.setattr(daemon, "_pending_cf", {"1": {"token": "t", "pane": "%1"}})
    ok = daemon._cf_inject_owned("1", "t", "%1", "hello", settle=0)
    assert ok is True
    # exactly two send-keys: the literal text, then Enter
    keys = rec.sent_keys()
    assert len(keys) == 2
    assert keys[0][-2:] == ["-l", "hello"]
    assert keys[1][-1] == "Enter"


def test_inject_owned_noops_after_halt(monkeypatch):
    # BLOCKER 2 (TOCTOU): the halt popped the flow (consuming the user's message) before
    # the worker's inject. The inject MUST no-op — no send-keys may resurrect the session.
    rec = _RunRecorder()
    monkeypatch.setattr(daemon.subprocess, "run", rec)
    monkeypatch.setattr(daemon, "_pending_cf", {})  # halted → no owner
    ok = daemon._cf_inject_owned("1", "t", "%1", "hello", settle=0)
    assert ok is False
    assert rec.sent_keys() == []


def test_inject_owned_noops_when_superseded_by_new_token(monkeypatch):
    # A newer carry-forward run owns the topic (different token) — the stale worker's
    # inject must no-op.
    rec = _RunRecorder()
    monkeypatch.setattr(daemon.subprocess, "run", rec)
    monkeypatch.setattr(daemon, "_pending_cf", {"1": {"token": "NEW", "pane": "%1"}})
    ok = daemon._cf_inject_owned("1", "OLD", "%1", "hello", settle=0)
    assert ok is False
    assert rec.sent_keys() == []


def test_inject_owned_release_after_pops_flow_atomically(monkeypatch):
    # release_after=True disarms the flow in the SAME lock hold as the send (#88 review r2):
    # the resume inject and the kill-switch disarm are atomic, so no redirect can slip in
    # between 'resumed' and 'released' to halt an already-resumed session.
    rec = _RunRecorder(echo=True)
    monkeypatch.setattr(daemon.subprocess, "run", rec)
    monkeypatch.setattr(daemon, "_pending_cf", {"1": {"token": "t", "pane": "%1"}})
    ok = daemon._cf_inject_owned("1", "t", "%1", "resume", settle=0, release_after=True)
    assert ok is True
    assert len(rec.sent_keys()) == 2                  # still sent the text + Enter
    assert "1" not in daemon._pending_cf              # flow popped atomically with the send
    assert not daemon.carry_forward_active("1")       # kill-switch disarmed
    # default (release_after omitted) must NOT pop — existing behaviour preserved
    monkeypatch.setattr(daemon, "_pending_cf", {"1": {"token": "t", "pane": "%1"}})
    daemon._cf_inject_owned("1", "t", "%1", "x", settle=0)
    assert "1" in daemon._pending_cf


def test_clear_modal_noops_after_halt(monkeypatch):
    # BLOCKER 2 also covers the modal-clear Enter: with a modal on screen but the flow
    # halted, no Enter may be sent.
    rec = _RunRecorder(capture_stdout="❯ 1. Resume from summary (recommended)")
    monkeypatch.setattr(daemon.subprocess, "run", rec)
    monkeypatch.setattr(daemon, "_cf_modal_present", lambda text: True)
    monkeypatch.setattr(daemon, "_pending_cf", {})  # halted
    assert daemon._cf_clear_modal("1", "t", "%1") is False
    assert rec.sent_keys() == []  # captured the pane, but sent no Enter


def test_clear_modal_sends_enter_when_owned(monkeypatch):
    rec = _RunRecorder(capture_stdout="❯ 1. Resume from summary (recommended)")
    monkeypatch.setattr(daemon.subprocess, "run", rec)
    monkeypatch.setattr(daemon, "_cf_modal_present", lambda text: True)
    monkeypatch.setattr(daemon, "_pending_cf", {"1": {"token": "t", "pane": "%1"}})
    assert daemon._cf_clear_modal("1", "t", "%1") is True
    keys = rec.sent_keys()
    assert len(keys) == 1 and keys[0][-1] == "Enter"


# ---- 'always a durable GitHub issue' verify + fallback (BLOCKER 3) -----------

def test_read_issue_ref_parses_url(tmp_path):
    m = tmp_path / "cf.md.done"
    m.write_text("recorded to https://github.com/example-org/ops/issues/999999\n")
    assert daemon._cf_read_issue_ref(str(m)) == \
        "https://github.com/example-org/ops/issues/999999"


def test_read_issue_ref_parses_short_form(tmp_path):
    m = tmp_path / "cf.md.done"
    m.write_text("example-org/ops#123")
    assert daemon._cf_read_issue_ref(str(m)) == "example-org/ops#123"


def test_read_issue_ref_none_when_empty(tmp_path):
    m = tmp_path / "cf.md.done"
    m.write_text("")  # empty marker → no ref
    assert daemon._cf_read_issue_ref(str(m)) is None


def test_read_issue_ref_none_when_no_valid_ref(tmp_path):
    m = tmp_path / "cf.md.done"
    m.write_text("done, but I forgot to paste the issue")  # placeholder / prose only
    assert daemon._cf_read_issue_ref(str(m)) is None


def test_read_issue_ref_none_when_missing_file(tmp_path):
    assert daemon._cf_read_issue_ref(str(tmp_path / "nope.done")) is None


def test_verify_uses_session_ref_without_fallback(monkeypatch, tmp_path):
    # Normal path: the session recorded a valid issue ref → daemon uses it, never falls back.
    called = []
    monkeypatch.setattr(daemon, "_cf_create_fallback_issue",
                        lambda *a, **k: called.append(a) or "SHOULD-NOT-BE-USED")
    m = tmp_path / "cf.md.done"
    m.write_text("https://github.com/example-org/ops/issues/42")
    cf = tmp_path / "cf.md"
    cf.write_text("body")
    ref, src = daemon._cf_verify_or_create_issue({}, 1, "sess", str(cf), str(m))
    assert ref == "https://github.com/example-org/ops/issues/42"
    assert src == "session"
    assert called == []  # fallback NOT invoked


def test_verify_falls_back_when_ref_missing(monkeypatch, tmp_path):
    # BLOCKER 3: the marker exists but holds no valid ref (gh failed / model skipped) →
    # the daemon creates the issue itself so the durable-record contract holds.
    called = []
    monkeypatch.setattr(
        daemon, "_cf_create_fallback_issue",
        lambda cfg, tid, name, cf: called.append((tid, name, cf))
        or "https://github.com/example-org/ops/issues/500")
    m = tmp_path / "cf.md.done"
    m.write_text("")  # empty marker
    cf = tmp_path / "cf.md"
    cf.write_text("body")
    ref, src = daemon._cf_verify_or_create_issue({}, 7, "sess", str(cf), str(m))
    assert ref == "https://github.com/example-org/ops/issues/500"
    assert src == "daemon"
    assert called == [(7, "sess", str(cf))]


def test_verify_returns_none_when_fallback_fails(monkeypatch, tmp_path):
    # Degrade gracefully: even the daemon fallback failed (gh down) → (None, None); the
    # worker warns and still compacts (the local CF file is the durable record).
    monkeypatch.setattr(daemon, "_cf_create_fallback_issue", lambda *a, **k: None)
    m = tmp_path / "cf.md.done"
    m.write_text("")
    cf = tmp_path / "cf.md"
    cf.write_text("body")
    assert daemon._cf_verify_or_create_issue({}, 1, "sess", str(cf), str(m)) == (None, None)


def test_fallback_issue_parses_gh_url(monkeypatch, tmp_path):
    # The daemon fallback shells out to gh; parse the created issue URL from stdout.
    monkeypatch.setattr(daemon, "carry_forward_fallback_repo", lambda: "example-org/ops")

    def fake_run(argv, *a, **k):
        assert argv[0] == daemon.GH_BIN and argv[1:3] == ["issue", "create"]
        assert argv[argv.index("--repo") + 1] == "example-org/ops"

        class _R:
            returncode = 0
            stdout = "https://github.com/example-org/ops/issues/777\n"
            stderr = ""
        return _R()
    monkeypatch.setattr(daemon.subprocess, "run", fake_run)
    cf = tmp_path / "cf.md"
    cf.write_text("body")
    assert daemon._cf_create_fallback_issue({}, 1, "sess", str(cf)) == \
        "https://github.com/example-org/ops/issues/777"


def test_fallback_issue_none_when_no_repo_is_configured(monkeypatch, tmp_path, capsys):
    """Unconfigured is not an error: the FILE is what the auto-resume reads (#204 D6)."""
    monkeypatch.setattr(daemon, "carry_forward_fallback_repo", lambda: None)
    monkeypatch.setattr(daemon.subprocess, "run",
                        lambda *a, **k: pytest.fail("gh must not be invoked with no repo"))
    cf = tmp_path / "cf.md"
    cf.write_text("body")

    assert daemon._cf_create_fallback_issue({}, 1, "sess", str(cf)) is None


def test_fallback_issue_none_on_gh_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(daemon, "carry_forward_fallback_repo", lambda: "example-org/ops")

    def fake_run(argv, *a, **k):
        class _R:
            returncode = 1
            stdout = ""
            stderr = "gh: not authenticated"
        return _R()
    monkeypatch.setattr(daemon.subprocess, "run", fake_run)
    cf = tmp_path / "cf.md"
    cf.write_text("body")
    assert daemon._cf_create_fallback_issue({}, 1, "sess", str(cf)) is None


# ---- passive status commands are exempt from the kill-switch (#87) ------------

def test_passive_status_commands_recognized():
    for c in ("/ctx", "/help", "/sessions", "/usage", "/peek"):
        assert daemon.is_passive_status_command(c)
        assert daemon.is_passive_status_command(c + " extra")  # matches on first token
    assert daemon.is_passive_status_command("/ctx  ")          # trailing ws ok (split drops it)


def test_non_passive_commands_and_text_not_recognized():
    # Redirects and plain text are NOT passive — they must still be able to halt.
    for c in ("/stop", "/model opus", "/kill", "/compact", "/carryforward",
              "please do X", "", "/ctxfoo"):
        assert not daemon.is_passive_status_command(c)


def test_leading_whitespace_command_is_not_passive():
    # Mirror handle_message's dispatch gate (text.startswith("/"), no lstrip): a
    # leading-space "  /ctx" is NOT a command there, so it must NOT be classified
    # passive here — otherwise it bypasses the kill-switch AND misses dispatch (#88).
    assert not daemon.is_passive_status_command("  /ctx")
    assert not daemon.is_passive_status_command("\t/help")


def _cf_msg(text, thread_id=33):
    return {"chat": {"id": 1}, "from": {"id": 5},
            "message_thread_id": thread_id, "text": text}


_CF_CFG = {"chat_id": 1, "owner_id": 5}


def test_passive_command_does_not_halt_active_carry_forward(monkeypatch):
    # A read-only /ctx during an active carry-forward must be answered, not halted (#87).
    monkeypatch.setattr(daemon, "carry_forward_active", lambda tid: True)
    halted = []
    monkeypatch.setattr(daemon, "halt_carry_forward",
                        lambda *a, **k: (halted.append(a), True)[1])
    handled = []
    monkeypatch.setattr(daemon, "handle_command",
                        lambda cfg, tid, text: handled.append(text))
    daemon.handle_message(_CF_CFG, _cf_msg("/ctx"))
    assert halted == []          # kill-switch did NOT fire
    assert handled == ["/ctx"]   # routed to the normal command handler instead


def test_real_message_still_halts_active_carry_forward(monkeypatch):
    # A real redirect (plain text) during an active carry-forward still halts it, and
    # handle_message returns before reaching audio/image extraction.
    monkeypatch.setattr(daemon, "carry_forward_active", lambda tid: True)
    halted = []
    monkeypatch.setattr(daemon, "halt_carry_forward",
                        lambda *a, **k: (halted.append(a), True)[1])
    monkeypatch.setattr(daemon, "extract_audio",
                        lambda msg: (_ for _ in ()).throw(
                            AssertionError("reached past the kill-switch")))
    daemon.handle_message(_CF_CFG, _cf_msg("please do X"))
    assert len(halted) == 1      # kill-switch fired


def test_non_passive_command_still_halts_active_carry_forward(monkeypatch):
    # /stop is a redirect (not passive) — it must still halt and never reach handle_command.
    monkeypatch.setattr(daemon, "carry_forward_active", lambda tid: True)
    halted = []
    monkeypatch.setattr(daemon, "halt_carry_forward",
                        lambda *a, **k: (halted.append(a), True)[1])
    handled = []
    monkeypatch.setattr(daemon, "handle_command",
                        lambda cfg, tid, text: handled.append(text))
    daemon.handle_message(_CF_CFG, _cf_msg("/stop"))
    assert len(halted) == 1      # halted
    assert handled == []         # never reached the command handler


def test_passive_command_when_no_carry_forward_is_normal(monkeypatch):
    # With no active carry-forward, a passive command routes normally (no regression).
    monkeypatch.setattr(daemon, "carry_forward_active", lambda tid: False)
    handled = []
    monkeypatch.setattr(daemon, "handle_command",
                        lambda cfg, tid, text: handled.append(text))
    daemon.handle_message(_CF_CFG, _cf_msg("/ctx"))
    assert handled == ["/ctx"]


# ---- worker disarms the kill-switch at resume (#87) ---------------------------

def test_worker_disarms_kill_switch_after_successful_resume(monkeypatch):
    # The crux of #87: once compaction is done and the resume nudge is injected, the
    # carry-forward is complete — the flow is released so NO later message (not even a
    # real one) can trip a spurious "halted". Mock the pane-driving so the happy path
    # runs deterministically; use the REAL _cf_release / carry_forward_active.
    monkeypatch.setattr(daemon.time, "sleep", lambda *a, **k: None)
    monkeypatch.setattr(daemon, "_cf_cleanup_marker", lambda *a, **k: None)
    monkeypatch.setattr(daemon, "_cf_wait_idle", lambda *a, **k: "idle")
    monkeypatch.setattr(daemon, "pane_alive", lambda pane: True)

    def fake_inject(tid, token, pane, text, settle=0.5, release_after=False):
        if release_after:                          # mirror the real atomic disarm
            daemon._pending_cf.pop(str(tid), None)
        return True
    monkeypatch.setattr(daemon, "_cf_inject_owned", fake_inject)
    monkeypatch.setattr(daemon, "_cf_wait_done", lambda *a, **k: "done")
    monkeypatch.setattr(daemon, "_cf_verify_or_create_issue",
                        lambda *a, **k: ("owner/repo#1", "session"))
    monkeypatch.setattr(daemon, "_cf_wait_compacting", lambda *a, **k: "compacting")
    monkeypatch.setattr(daemon, "_cf_clear_modal", lambda *a, **k: None)
    replies = []
    monkeypatch.setattr(daemon, "reply", lambda cfg, tid, text: replies.append(text))
    monkeypatch.setattr(daemon, "_pending_cf",
                        {"33": {"token": "t", "pane": "%0", "phase": "write"}})

    daemon._carry_forward_worker({}, 33, "%0", "/x/cf.md", "/x/cf.md.done", "t", "sess")

    assert not daemon.carry_forward_active(33)                 # kill-switch disarmed
    assert any("Carry-forward complete" in r for r in replies) # resume confirmation sent
    assert not any("halted" in r.lower() for r in replies)     # no spurious halt


def test_worker_aborts_when_busy_but_not_compacting(monkeypatch):
    # #101 REGRESSION: the session is idle enough to inject /compact, but /compact never
    # actually starts compacting (it queued behind the session's own turn / was mangled),
    # so _cf_wait_compacting always times out. The worker MUST retry then abort — NOT report
    # success and resume. Under the OLD generic-busy gate this returned "busy" and the flow
    # falsely resumed an uncompacted session (the t212 bug). Uses the REAL _cf_release.
    monkeypatch.setattr(daemon.time, "sleep", lambda *a, **k: None)
    monkeypatch.setattr(daemon, "_cf_cleanup_marker", lambda *a, **k: None)
    monkeypatch.setattr(daemon, "_cf_wait_idle", lambda *a, **k: "idle")
    monkeypatch.setattr(daemon, "pane_alive", lambda pane: True)

    injected = []

    def fake_inject(tid, token, pane, text, settle=0.5, release_after=False):
        injected.append(text)
        if release_after:
            daemon._pending_cf.pop(str(tid), None)
        return True
    monkeypatch.setattr(daemon, "_cf_inject_owned", fake_inject)
    monkeypatch.setattr(daemon, "_cf_wait_done", lambda *a, **k: "done")
    monkeypatch.setattr(daemon, "_cf_verify_or_create_issue",
                        lambda *a, **k: ("owner/repo#1", "session"))
    monkeypatch.setattr(daemon, "_cf_wait_compacting", lambda *a, **k: "timeout")  # never compacts
    monkeypatch.setattr(daemon, "_cf_clear_modal", lambda *a, **k: None)
    # No hook refusal on the pane — this is the "/compact silently did nothing" case, which
    # still earns all CF_COMPACT_TRIES. Without this the check reads the REAL pane (#155).
    monkeypatch.setattr(daemon, "_cf_hook_block_reason", lambda pane, before: None)
    replies = []
    monkeypatch.setattr(daemon, "reply", lambda cfg, tid, text: replies.append(text))
    monkeypatch.setattr(daemon, "_pending_cf",
                        {"33": {"token": "t", "pane": "%0", "phase": "write"}})

    daemon._carry_forward_worker({}, 33, "%0", "/x/cf.md", "/x/cf.md.done", "t", "sess")

    assert not daemon.carry_forward_active(33)                        # flow released on abort
    assert injected.count("/compact") == daemon.CF_COMPACT_TRIES      # retried, didn't give up early
    assert any("didn't start compacting" in r for r in replies)      # honest abort message
    assert not any("Carry-forward complete" in r for r in replies)   # NO false success
    assert daemon.CF_RESUME_PROMPT.format(path="/x/cf.md") not in injected  # never resumed
    assert injected[-1] == "/compact"                                # aborted before PHASE 3


# ---- PreCompact hook refusal (#155) ------------------------------------------
#
# Real shape, measured on a real session: Claude Code answers a hook-blocked
# /compact in under a second, tmux hard-wraps the stderr at the pane width, and the daemon
# used to re-inject /compact twice more before replying with a generic timeout.

_HOOK_BLOCK_CAPTURE = (
    "  ⎿  Read 40 lines\n"
    "\n"
    "> /compact\n"
    "  ⎿  <local-command-stderr>Compaction blocked by PreCompact hook:\n"
    "     [bash ~/.claude/hooks/pre-compact-issue-check.sh]: PreCompact blocked: no new\n"
    "     carry-forward comment on #42 (example-org/ops) since session start on\n"
    "     this issue.\n"
    "\n"
)


def test_hook_block_text_extracts_wrapped_reason():
    got = daemon._cf_hook_block_text(_HOOK_BLOCK_CAPTURE)
    assert got is not None
    assert "\n" not in got                                   # one readable line for Telegram
    assert "pre-compact-issue-check.sh" in got               # unwrapped across the tmux break
    assert "no new carry-forward comment on #42" in got     # the reason itself survived
    assert "<local-command-stderr>" not in got               # pseudo-tag stripped


def test_hook_block_text_none_for_ordinary_capture():
    assert daemon._cf_hook_block_text(
        "> /compact\n  ⎿  Compacting conversation… (8s · ↓ 1.2k tokens)\n") is None
    assert daemon._cf_hook_block_text("❯ \n") is None


def test_hook_block_text_ignores_prose_about_a_past_block():
    # An operator session reviewing a previous failure has this phrase in its scrollback.
    # Without the "[<command>]" confirmation that prose would abort a healthy carry-forward.
    prose = (
        "  Both failures were the same thing: Compaction blocked by PreCompact hook, and\n"
        "  the daemon reported a generic timeout instead of the reason.\n"
    )
    assert daemon._cf_hook_block_text(prose) is None


def test_hook_block_text_is_capped():
    long_reason = (
        "<local-command-stderr>Compaction blocked by PreCompact hook: [bash /x/h.sh]: "
        + "reason " * 400
    )
    got = daemon._cf_hook_block_text(long_reason)
    assert got is not None
    assert len(got) <= daemon.CF_HOOK_BLOCK_CHARS


def test_hook_block_text_keeps_angle_bracketed_identifiers():
    # The tag strip must remove Claude Code's wrapper ONLY. A general "<[a-z]…>" strip turned
    # `branch <main> must contain List<string>` into `branch must contain List`, deleting the
    # actionable identifier from a message that claims to quote the hook verbatim.
    got = daemon._cf_hook_block_text(
        "<local-command-stderr>Compaction blocked by PreCompact hook: [bash /x/h.sh]: "
        "branch <main> must contain List<string>\n")
    assert "<main>" in got and "List<string>" in got
    assert "<local-command-stderr>" not in got


def test_hook_block_reason_reads_the_pane(monkeypatch):
    monkeypatch.setattr(daemon, "_tmux", lambda *a, **k: types.SimpleNamespace(
        returncode=0, stdout=_HOOK_BLOCK_CAPTURE))
    assert "no new carry-forward comment" in daemon._cf_hook_block_reason("%1", "❯ \n")


_SHORT_STALE = "  ⎿  Compaction blocked by PreCompact hook: [bash /x/h.sh]: no\n\n❯ \n"
_FRESH_OTHER = (
    "  ⎿  <local-command-stderr>Compaction blocked by PreCompact hook:\n"
    "     [bash /x/h.sh]: PreCompact blocked: no comments on tracked issue #999.\n"
)


def test_hook_block_reason_rejects_a_short_stale_refusal(monkeypatch):
    # Codex round 2 of PR #156: a blind 4-line join absorbed the "/compact" we had just
    # echoed, so a SHORT displayed refusal produced different text before and after the
    # injection and slipped through the freshness check. The block must end at its own
    # boundary — blank line, prompt, command echo — and never swallow what follows.
    monkeypatch.setattr(daemon, "_tmux", lambda *a, **k: types.SimpleNamespace(
        returncode=0, stdout=_SHORT_STALE + "> /compact\n❯ \n"))
    assert daemon._cf_hook_block_reason("%1", _SHORT_STALE) is None


def test_hook_block_reason_reports_a_fresh_refusal_below_a_stale_one(monkeypatch):
    # Every refusal from one hook shares a long constant head, so a text-prefix probe let any
    # displayed refusal mask a genuinely NEW one with a different reason. Occurrence matching
    # must report the new block.
    monkeypatch.setattr(daemon, "_tmux", lambda *a, **k: types.SimpleNamespace(
        returncode=0, stdout=_HOOK_BLOCK_CAPTURE + "> /compact\n" + _FRESH_OTHER))
    got = daemon._cf_hook_block_reason("%1", _HOOK_BLOCK_CAPTURE)
    assert got is not None
    assert "#999" in got and "#42" not in got     # the NEW reason, not the displayed one


def test_hook_block_reason_reports_a_repeated_identical_refusal(monkeypatch):
    # Same reason refused twice: the snapshot holds one occurrence, the pane now holds two,
    # so the second is new. Multiset matching, not membership.
    monkeypatch.setattr(daemon, "_tmux", lambda *a, **k: types.SimpleNamespace(
        returncode=0, stdout=_HOOK_BLOCK_CAPTURE + "> /compact\n" + _HOOK_BLOCK_CAPTURE))
    assert daemon._cf_hook_block_reason("%1", _HOOK_BLOCK_CAPTURE) is not None


def test_hook_block_all_reads_every_occurrence_in_pane_order():
    blocks = daemon._cf_hook_block_all(_HOOK_BLOCK_CAPTURE + "> /compact\n" + _FRESH_OTHER)
    assert len(blocks) == 2
    assert "#42" in blocks[0] and "#999" in blocks[1]
    assert "/compact" not in blocks[0]              # the boundary held
    assert daemon._cf_hook_block_text(  # newest = lowest on the pane
        _HOOK_BLOCK_CAPTURE + "> /compact\n" + _FRESH_OTHER).count("#999") == 1


def test_hook_block_reason_rejects_a_refusal_already_on_the_pane(monkeypatch):
    # THE false positive (Codex review of PR #156): a pane DISPLAYING a fully formed past
    # refusal — an operator reading a raw capture of that failure — renders exactly like
    # a live one, "[<command>]" bracket and all. Matching it would abort a carry-forward that
    # still had CF_COMPACT_TRIES-1 attempts left, so the pre-inject snapshot must veto it.
    stale = _HOOK_BLOCK_CAPTURE + "\n> /compact\n❯ \n"
    monkeypatch.setattr(daemon, "_tmux", lambda *a, **k: types.SimpleNamespace(
        returncode=0, stdout=stale))
    assert daemon._cf_hook_block_reason("%1", _HOOK_BLOCK_CAPTURE) is None
    # …and the very same after-capture IS reported when the snapshot was clean.
    assert daemon._cf_hook_block_reason("%1", "  ⎿  Read 40 lines\n❯ \n") is not None


def test_hook_block_reason_none_without_a_snapshot(monkeypatch):
    # Snapshot failed -> freshness is unprovable -> report nothing and let the retries run.
    monkeypatch.setattr(daemon, "_tmux", lambda *a, **k: types.SimpleNamespace(
        returncode=0, stdout=_HOOK_BLOCK_CAPTURE))
    assert daemon._cf_hook_block_reason("%1", None) is None


def test_hook_block_reason_none_on_bad_capture(monkeypatch):
    # Same INVERTED default as _cf_compacting: an unreadable pane must fall through to the
    # ordinary retry path, never abort a carry-forward on unverified state.
    monkeypatch.setattr(daemon, "_tmux", lambda *a, **k: types.SimpleNamespace(
        returncode=1, stdout=""))
    assert daemon._cf_hook_block_reason("%1", "❯ \n") is None
    assert daemon._cf_capture_tail("%1") is None

    def boom(*a, **k):
        raise OSError("tmux gone")
    monkeypatch.setattr(daemon, "_tmux", boom)
    assert daemon._cf_hook_block_reason("%1", "❯ \n") is None
    assert daemon._cf_capture_tail("%1") is None


def test_worker_stops_at_once_when_a_precompact_hook_refuses(monkeypatch):
    # #155: the refusal is deterministic, so retrying is pure waste and the generic
    # "didn't start compacting" reply hides the cause. Exactly ONE /compact, and the
    # hook's own words go to the topic.
    monkeypatch.setattr(daemon.time, "sleep", lambda *a, **k: None)
    monkeypatch.setattr(daemon, "_cf_cleanup_marker", lambda *a, **k: None)
    monkeypatch.setattr(daemon, "_cf_wait_idle", lambda *a, **k: "idle")
    monkeypatch.setattr(daemon, "pane_alive", lambda pane: True)

    injected = []

    def fake_inject(tid, token, pane, text, settle=0.5, release_after=False):
        injected.append(text)
        if release_after:
            daemon._pending_cf.pop(str(tid), None)
        return True
    monkeypatch.setattr(daemon, "_cf_inject_owned", fake_inject)
    monkeypatch.setattr(daemon, "_cf_wait_done", lambda *a, **k: "done")
    monkeypatch.setattr(daemon, "_cf_verify_or_create_issue",
                        lambda *a, **k: ("owner/repo#1", "session"))
    monkeypatch.setattr(daemon, "_cf_wait_compacting", lambda *a, **k: "timeout")
    monkeypatch.setattr(daemon, "_cf_clear_modal", lambda *a, **k: None)
    monkeypatch.setattr(daemon, "_cf_hook_block_reason",
                        lambda pane, before: "PreCompact blocked: no new carry-forward comment on #42.")
    replies = []
    monkeypatch.setattr(daemon, "reply", lambda cfg, tid, text: replies.append(text))
    monkeypatch.setattr(daemon, "_pending_cf",
                        {"33": {"token": "t", "pane": "%0", "phase": "write"}})

    daemon._carry_forward_worker({}, 33, "%0", "/x/cf.md", "/x/cf.md.done", "t", "sess")

    assert injected.count("/compact") == 1                            # no pointless retries
    assert not daemon.carry_forward_active(33)                        # flow released
    assert any("refused by a PreCompact hook" in r for r in replies)
    assert any("no new carry-forward comment on #42" in r for r in replies)  # the real reason
    assert not any("didn't start compacting" in r for r in replies)   # not the generic message
    assert not any("Carry-forward complete" in r for r in replies)    # never resumed
    assert daemon.CF_RESUME_PROMPT.format(path="/x/cf.md") not in injected


def test_worker_still_retries_when_a_displayed_refusal_is_not_ours(monkeypatch):
    # Codex review of PR #156: the cost of a false match is NOT symmetric with a generic
    # timeout — after the first saw-compacting timeout the flow still has CF_COMPACT_TRIES-1
    # attempts that can succeed. Drive the real _cf_hook_block_reason with a pane that
    # displays a past refusal throughout, and let attempt 2 compact: the carry-forward must
    # complete normally.
    monkeypatch.setattr(daemon.time, "sleep", lambda *a, **k: None)
    monkeypatch.setattr(daemon, "_cf_cleanup_marker", lambda *a, **k: None)
    monkeypatch.setattr(daemon, "_cf_wait_idle", lambda *a, **k: "idle")
    monkeypatch.setattr(daemon, "pane_alive", lambda pane: True)
    # The stale refusal is on the pane before AND after every injection.
    monkeypatch.setattr(daemon, "_tmux", lambda *a, **k: types.SimpleNamespace(
        returncode=0, stdout=_HOOK_BLOCK_CAPTURE + "\n> /compact\n❯ \n"))

    injected = []

    def fake_inject(tid, token, pane, text, settle=0.5, release_after=False):
        injected.append(text)
        if release_after:
            daemon._pending_cf.pop(str(tid), None)
        return True
    monkeypatch.setattr(daemon, "_cf_inject_owned", fake_inject)
    monkeypatch.setattr(daemon, "_cf_wait_done", lambda *a, **k: "done")
    monkeypatch.setattr(daemon, "_cf_verify_or_create_issue",
                        lambda *a, **k: ("owner/repo#1", "session"))

    attempts = []

    def flaky_wait_compacting(*a, **k):
        attempts.append(1)
        return "compacting" if len(attempts) >= 2 else "timeout"
    monkeypatch.setattr(daemon, "_cf_wait_compacting", flaky_wait_compacting)
    monkeypatch.setattr(daemon, "_cf_clear_modal", lambda *a, **k: None)
    replies = []
    monkeypatch.setattr(daemon, "reply", lambda cfg, tid, text: replies.append(text))
    monkeypatch.setattr(daemon, "_pending_cf",
                        {"33": {"token": "t", "pane": "%0", "phase": "write"}})

    daemon._carry_forward_worker({}, 33, "%0", "/x/cf.md", "/x/cf.md.done", "t", "sess")

    assert injected.count("/compact") == 2                             # the retry was not stolen
    assert any("Carry-forward complete" in r for r in replies)         # it recovered
    assert not any("refused by a PreCompact hook" in r for r in replies)
    assert not any("didn't start compacting" in r for r in replies)


def test_worker_snapshots_the_pane_before_injecting_compact(monkeypatch):
    # Codex round 2 non-blocking: nothing pinned the ORDER. Freshness is only meaningful if
    # the snapshot precedes the injection, and the two captures must be able to differ —
    # here the fresh refusal appears only AFTER /compact is typed, which is the real sequence.
    monkeypatch.setattr(daemon.time, "sleep", lambda *a, **k: None)
    monkeypatch.setattr(daemon, "_cf_cleanup_marker", lambda *a, **k: None)
    monkeypatch.setattr(daemon, "_cf_wait_idle", lambda *a, **k: "idle")
    monkeypatch.setattr(daemon, "pane_alive", lambda pane: True)

    events = []
    pane_text = ["  ⎿  Read 40 lines\n❯ \n"]

    def fake_tmux(*a, **k):
        events.append("capture")
        return types.SimpleNamespace(returncode=0, stdout=pane_text[0])
    monkeypatch.setattr(daemon, "_tmux", fake_tmux)

    def fake_inject(tid, token, pane, text, settle=0.5, release_after=False):
        if text == "/compact":
            events.append("inject")
            pane_text[0] += _HOOK_BLOCK_CAPTURE      # the refusal lands after the injection
        if release_after:
            daemon._pending_cf.pop(str(tid), None)
        return True
    monkeypatch.setattr(daemon, "_cf_inject_owned", fake_inject)
    monkeypatch.setattr(daemon, "_cf_wait_done", lambda *a, **k: "done")
    monkeypatch.setattr(daemon, "_cf_verify_or_create_issue",
                        lambda *a, **k: ("owner/repo#1", "session"))
    monkeypatch.setattr(daemon, "_cf_wait_compacting", lambda *a, **k: "timeout")
    monkeypatch.setattr(daemon, "_cf_clear_modal", lambda *a, **k: None)
    replies = []
    monkeypatch.setattr(daemon, "reply", lambda cfg, tid, text: replies.append(text))
    monkeypatch.setattr(daemon, "_pending_cf",
                        {"33": {"token": "t", "pane": "%0", "phase": "write"}})

    daemon._carry_forward_worker({}, 33, "%0", "/x/cf.md", "/x/cf.md.done", "t", "sess")

    assert events[:2] == ["capture", "inject"]                  # snapshot precedes the inject
    assert any("refused by a PreCompact hook" in r for r in replies)
    assert any("no new carry-forward comment on #42" in r for r in replies)
