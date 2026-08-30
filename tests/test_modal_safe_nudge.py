"""#133: the daemon must never press Enter on a pane that didn't accept the text it typed.

A `tmux send-keys Enter` is unconditional. When a codex pane is showing the rate-limit
picker — option 1, "Switch to gpt-5.6-luna", preselected — the nudge text is swallowed by
the widget and the Enter accepts the highlighted option, silently downgrading the session's
model. The message isn't delivered either. Same hazard for approval/update/resume pickers
and for claude's own dialogs.

The guard is behavioural, not cosmetic: type, confirm the text actually rendered in the
input box, and only then press Enter. These tests pin that contract against representative
pane geometry from both engines, because the load-bearing constant is
how far above the bottom each engine draws its input line.
"""

import threading
import time
import types

import pytest

from bridge import daemon


# ---- representative pane fixtures -------------------------------------------
# Derived from observed pane geometry, with scrollback prose and status lines replaced by
# neutral text. What has to survive that substitution is the
# SHAPE: the number of lines below `{INPUT}` and the fact that a modal has no input line at
# all. The detector does not read only those lines — `type_line` squashes the whole capture
# and requires a strict rise in the count of the injected text — so keep the line count and
# the footer structure when you edit these, and change wording freely.

CLAUDE_IDLE = """\
  a completed turn's last three lines of output, which are ordinary scrollback
  and say nothing about whether the pane will accept typed text — the lines
  below the input box are what decides that.
────────────────────────────────────────────────────────────────────────────
❯ {INPUT}
────────────────────────────────────────────────────────────────────────────
  example-repo (main) • Example model • context available
  standard permissions · 1 shell"""

CODEX_IDLE = """\
─ Worked for 1m 27s ─────────────────────────────────────────────────────────
⚠ Optional integrations unavailable (example-search, example-tasks)
› {INPUT}
  example-model high · Context available"""

# The picker from #133. There is no input box at all — printable keys are swallowed and
# Enter accepts the highlighted option.
CODEX_RATE_LIMIT_MODAL = """\
• Ran env -u CLAUDECODE gh issue view 932 -R example-org/example-repo

  Approaching rate limits
  Switch to gpt-5.6-luna for lower credit usage?

› 1. Switch to gpt-5.6-luna
  2. Keep current model
  3. Keep current model (never show again)"""

CODEX_APPROVAL_MODAL = """\
  Would you like to run the following command?

› 1. Yes, just this once
  2. Yes, and don't ask again for this command in this session
  3. No, and tell Codex what to do differently"""

NUDGE = "[tg-bridge] New Telegram message in your topic — run `tg-bridge recv --topic 55` and act on it."

# Captured before any fixture patches it. `_no_sleep` patches daemon.time.sleep, which IS
# the shared time module, so a fake that called time.sleep() would be silenced along with
# type_line's settle — and the concurrency test would lose the delay it needs to overlap.
_REAL_SLEEP = time.sleep


def _counted(screen):
    """What type_line actually counts in — the WHOLE capture, squashed (#165)."""
    return daemon._squash(screen)


class FakePane:
    """tmux stand-in. `accepts` False models a modal: printable keys go nowhere.

    Enter clears the input box, as both TUIs do on submit — a fake that keeps the text would
    let a test assert on state production doesn't guarantee."""

    def __init__(self, screen, accepts=True, width=None, capture_fails=False,
                 capture_fails_after_typing=False):
        self.template = screen
        self.value = ""
        self.accepts = accepts
        self.width = width
        self.capture_fails = capture_fails
        self.capture_fails_after_typing = capture_fails_after_typing
        self.typed = []
        self.submitted = []
        self.enters = 0
        self.trace = None
        self.op_delay = 0
        self.geometries = None   # iterable of fingerprints, one per display-message call

    def geometry(self):
        """`history_size,pane_height,pane_width,alternate_on` — constant unless a test sets
        `geometries` to drive a resize between type_line's two captures."""
        if self.geometries is None:
            return "100,40,80,0"
        return next(self.geometries, "100,40,80,0")

    def _mark(self, step):
        if self.trace is not None:
            self.trace.append((step, threading.current_thread().name))
        if self.op_delay:
            _REAL_SLEEP(self.op_delay)  # widen the window so an unlocked impl interleaves

    def _rendered(self):
        text = self.value
        if self.width:  # a TUI wraps the input line at the pane width
            text = "\n".join(text[i:i + self.width] for i in range(0, len(text), self.width))
        return self.template.replace("{INPUT}", text)

    def __call__(self, argv, **kw):
        if argv[:2] == ["tmux", "display-message"]:
            # Geometry fingerprint (#165 r2). Stable unless a test drives `geometry`.
            return types.SimpleNamespace(returncode=0, stdout=self.geometry(), stderr="")
        if argv[:2] == ["tmux", "capture-pane"]:
            self._mark("capture")
            if self.capture_fails or (self.capture_fails_after_typing and self.typed):
                return types.SimpleNamespace(returncode=1, stdout="", stderr="")
            return types.SimpleNamespace(returncode=0, stdout=self._rendered(), stderr="")
        if argv[:2] == ["tmux", "send-keys"]:
            if argv[-1] == "Enter":
                self._mark("Enter")
                self.enters += 1
                self.submitted.append(self.value)
                self.value = ""          # submit clears the box
            else:
                self._mark("type")
                self.typed.append(argv[-1])
                if self.accepts:
                    self.value += argv[-1]
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")


class FakeSequence:
    """Drives the before/after captures directly, so a test can pin the decision for a pane
    that repaints or scrolls between the two reads — something FakePane's static screen
    cannot express."""

    def __init__(self, screens, geometries=None):
        self.screens = screens
        self.geometries = geometries
        self.enters = 0
        self.typed = []

    def __call__(self, argv, **kw):
        if argv[:2] == ["tmux", "display-message"]:
            geo = "100,40,80,0" if self.geometries is None else next(self.geometries, "100,40,80,0")
            return types.SimpleNamespace(returncode=0, stdout=geo, stderr="")
        if argv[:2] == ["tmux", "capture-pane"]:
            return types.SimpleNamespace(returncode=0, stdout=next(self.screens, ""), stderr="")
        if argv[-1] == "Enter":
            self.enters += 1
        elif "-l" in argv:
            self.typed.append(argv[-1])
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(daemon.time, "sleep", lambda *_a, **_k: None)


@pytest.fixture(autouse=True)
def _reset_pane_state():
    for table in (daemon._blocked_reported, daemon._swallowed_streak, daemon._pane_locks):
        table.clear()
    yield
    for table in (daemon._blocked_reported, daemon._swallowed_streak, daemon._pane_locks):
        table.clear()


def _install(monkeypatch, pane):
    monkeypatch.setattr(daemon, "_tmux", pane)
    return pane


# ---- type_line: the core contract --------------------------------------------

@pytest.mark.parametrize("screen", [CLAUDE_IDLE, CODEX_IDLE])
def test_enter_is_sent_when_the_text_lands(monkeypatch, screen):
    """Both engines' real input-box geometry must read as 'landed' — otherwise the guard
    would block every ordinary nudge."""
    pane = _install(monkeypatch, FakePane(screen))
    assert daemon.type_line("%1", NUDGE) == "sent"
    assert pane.enters == 1
    assert pane.submitted == [NUDGE]   # the Enter submitted exactly the line we typed


@pytest.mark.parametrize("screen", [CODEX_RATE_LIMIT_MODAL, CODEX_APPROVAL_MODAL])
def test_enter_is_withheld_when_a_modal_swallows_the_text(monkeypatch, screen):
    """The #133 bug itself: no Enter may reach a picker, or option 1 gets selected."""
    pane = _install(monkeypatch, FakePane(screen, accepts=False))
    assert daemon.type_line("%1", NUDGE) == "swallowed"
    assert pane.enters == 0


def test_wrapped_input_still_counts_as_landed(monkeypatch):
    """A long nudge wraps across rows; the probe must survive the wrap or every nudge into
    a narrow pane would be misread as swallowed."""
    pane = _install(monkeypatch, FakePane(CLAUDE_IDLE, width=40))
    assert daemon.type_line("%1", NUDGE) == "sent"
    assert pane.enters == 1


def test_codex_caret_alone_is_not_a_modal(monkeypatch):
    """'›' is codex's ordinary input caret, printed on every idle pane. Any detector that
    treats it as a picker marker blocks all codex delivery — this pins that it doesn't."""
    pane = _install(monkeypatch, FakePane(CODEX_IDLE))
    assert "›" in CODEX_IDLE
    assert daemon.type_line("%1", NUDGE) == "sent"


def test_scrollback_residue_does_not_mask_a_live_modal(monkeypatch):
    """An earlier, already-entered nudge sitting just above the picker must not be mistaken
    for the text we just typed — that's why the check counts occurrences instead of asking
    'is it present?'.

    The residue must land INSIDE the counted window or this proves nothing: an earlier draft
    of this test prepended so many rows that the region excluded the residue and the counts
    were 0/0, i.e. it passed without ever exercising the count."""
    residue = f"❯ {NUDGE}\n  2. Keep current model\n› 1. Switch to gpt-5.6-luna"
    assert daemon._echo_probe(NUDGE) in _counted(residue), \
        "residue must be inside the counted window for this test to mean anything"
    pane = _install(monkeypatch, FakePane(residue, accepts=False))
    assert daemon.type_line("%1", NUDGE) == "swallowed"
    assert pane.enters == 0


def test_a_falling_count_is_never_proof(monkeypatch):
    """Two stale copies in the region, a modal swallows the typing, and one stale copy
    scrolls out. The count changes (2 -> 1) but every remaining match is stale — reading a
    change rather than a RISE as proof pressed Enter on the live picker."""
    screens = iter([f"{NUDGE}\n{NUDGE}", NUDGE])
    pane = _install(monkeypatch, FakeSequence(screens))
    assert daemon.type_line("%1", NUDGE) == "swallowed"
    assert pane.enters == 0


def test_a_rising_count_past_stale_copies_is_proof(monkeypatch):
    """The other direction: stale copies present in BOTH captures cancel out, so a genuine
    landing is still recognised and delivery isn't wedged by residue."""
    screens = iter([NUDGE, f"{NUDGE}\n{NUDGE}"])
    pane = _install(monkeypatch, FakeSequence(screens))
    assert daemon.type_line("%1", NUDGE) == "sent"
    assert pane.enters == 1


@pytest.mark.parametrize("before,after,expected", [
    (0, 0, "swallowed"),   # nothing rendered — a modal ate it
    (0, 1, "sent"),
    (1, 1, "swallowed"),   # unresolvable: swallowed, or landed as a stale copy scrolled out
    (1, 2, "sent"),
    (2, 1, "swallowed"),   # a fall is never proof
    (1, 0, "swallowed"),
    (2, 3, "sent"),
])
def test_decision_matrix(monkeypatch, before, after, expected):
    """The whole before/after state space, pinned. Only a strict rise authorises the Enter."""
    pane = _install(monkeypatch, FakeSequence(iter(["\n".join([NUDGE] * n) if n else "idle"
                                                   for n in (before, after)])))
    assert daemon.type_line("%1", NUDGE) == expected
    assert pane.enters == (1 if expected == "sent" else 0)


def test_deep_chrome_still_finds_the_input(monkeypatch):
    """A narrow pane wraps claude's statusline and hint rows, pushing the input box further
    from the bottom. If the region can't reach it, every injection is refused forever."""
    deep = "❯ {INPUT}\n" + "\n".join(f"  wrapped chrome row {i}" for i in range(6))
    assert len([ln for ln in deep.splitlines() if ln.strip()]) == 7
    pane = _install(monkeypatch, FakePane(deep))
    assert daemon.type_line("%1", NUDGE) == "sent"
    assert pane.enters == 1


def test_repeated_unverified_attempts_stop_typing(monkeypatch):
    """Every attempt appends to an input box we can't verify. Unbounded retries grow an
    unsendable line once a minute forever, so the pane is left alone after two."""
    pane = _install(monkeypatch, FakePane(CODEX_RATE_LIMIT_MODAL, accepts=False))
    results = [daemon.type_line("%1", NUDGE) for _ in range(4)]
    assert results == ["swallowed"] * 4
    assert len(pane.typed) == daemon.SWALLOW_MAX_ATTEMPTS   # then it stops adding to the box
    assert pane.enters == 0


def test_a_successful_send_clears_the_streak(monkeypatch):
    _install(monkeypatch, FakePane(CODEX_RATE_LIMIT_MODAL, accepts=False))
    daemon.type_line("%2", NUDGE)
    assert daemon._swallowed_streak["%2"][0] == 1
    _install(monkeypatch, FakePane(CLAUDE_IDLE))
    assert daemon.type_line("%2", NUDGE) == "sent"
    assert "%2" not in daemon._swallowed_streak


def test_the_cap_is_not_a_one_way_door(monkeypatch):
    """Clearing the streak only on a successful send would be a trap: success needs typing
    and the cap blocks typing, so a pane whose modal the human answered could never be
    nudged again. The cap releases on a timer, and a landing then clears it outright."""
    _install(monkeypatch, FakePane(CODEX_RATE_LIMIT_MODAL, accepts=False))
    for _ in range(4):
        assert daemon.type_line("%2", NUDGE) == "swallowed"
    assert daemon._swallowed_streak["%2"][0] == daemon.SWALLOW_MAX_ATTEMPTS

    # human answers the picker; the pane is an ordinary input box again
    pane = _install(monkeypatch, FakePane(CLAUDE_IDLE))
    now = time.time()
    monkeypatch.setattr(daemon.time, "time", lambda: now + daemon.SWALLOW_RETRY_AFTER + 1)
    assert daemon.type_line("%2", NUDGE) == "sent"
    assert pane.enters == 1
    assert "%2" not in daemon._swallowed_streak


def test_a_repainting_modal_cannot_reset_the_cap(monkeypatch):
    """The tempting release signal — "the bottom region changed, so the pane moved on" —
    is unsound: it cannot tell an answered modal from a modal with something ticking under
    it, and the second case would reset the streak every poll and restore the very growth
    the cap exists to stop. Only elapsed time may release it."""
    counter = {"n": 0}

    class Ticking(FakePane):
        def _rendered(self):
            counter["n"] += 1
            return f"tick {counter['n']}\n" + super()._rendered()

    pane = _install(monkeypatch, Ticking(CODEX_RATE_LIMIT_MODAL, accepts=False))
    for _ in range(6):
        assert daemon.type_line("%2", NUDGE) == "swallowed"
    assert len(pane.typed) == daemon.SWALLOW_MAX_ATTEMPTS
    assert pane.enters == 0

    now = time.time()
    monkeypatch.setattr(daemon.time, "time", lambda: now + daemon.SWALLOW_RETRY_AFTER + 1)
    assert daemon.type_line("%2", NUDGE) == "swallowed"
    assert len(pane.typed) == daemon.SWALLOW_MAX_ATTEMPTS + 1   # one more try, then capped again
    assert pane.enters == 0


def test_a_failing_enter_counts_against_the_pane(monkeypatch):
    """The text landed and is sitting unsent in the box. If that doesn't count, every retry
    appends a fresh copy forever — the accumulation the cap exists to bound — and the
    callers never escalate either, because they only escalate on "swallowed"."""
    class EnterFails(FakePane):
        def __call__(self, argv, **kw):
            if argv[:2] == ["tmux", "send-keys"] and argv[-1] == "Enter":
                raise OSError("tmux send-keys Enter failed")
            return super().__call__(argv, **kw)

    pane = _install(monkeypatch, EnterFails(CLAUDE_IDLE))
    results = [daemon.type_line("%2", NUDGE) for _ in range(5)]
    # the first attempts genuinely fail; after that the cap engages and stops typing at all
    assert results[:2] == ["failed", "failed"]
    assert set(results[2:]) == {"swallowed"}
    assert len(pane.typed) == daemon.SWALLOW_MAX_ATTEMPTS   # bounded, not one copy per call


def test_the_timed_retry_spends_one_attempt_not_a_fresh_budget(monkeypatch):
    """Zeroing the streak at the retry would buy SWALLOW_MAX_ATTEMPTS fresh copies every
    window, so a pane that accepts text but never satisfies the check would still grow
    without bound — just more slowly."""
    pane = _install(monkeypatch, FakePane(CODEX_RATE_LIMIT_MODAL, accepts=False))
    now = time.time()
    monkeypatch.setattr(daemon.time, "time", lambda: now)
    for _ in range(4):
        daemon.type_line("%2", NUDGE)
    assert len(pane.typed) == daemon.SWALLOW_MAX_ATTEMPTS

    for window in range(1, 4):   # each window buys exactly ONE more attempt
        monkeypatch.setattr(daemon.time, "time",
                            lambda w=window: now + w * (daemon.SWALLOW_RETRY_AFTER + 1))
        for _ in range(4):
            daemon.type_line("%2", NUDGE)
        assert len(pane.typed) == daemon.SWALLOW_MAX_ATTEMPTS + window
    assert pane.enters == 0


def test_dead_panes_are_pruned(monkeypatch):
    daemon._swallowed_streak["%gone"] = (2, 0.0)
    daemon._swallowed_streak["%live"] = (1, 0.0)
    daemon._pane_locks["%gone"] = threading.Lock()
    monkeypatch.setattr(daemon, "fleet_panes", lambda: [("%live", "s", "t", "claude")])
    daemon._prune_pane_tables()
    assert "%gone" not in daemon._swallowed_streak and "%live" in daemon._swallowed_streak
    assert "%gone" not in daemon._pane_locks


def test_a_failed_fleet_read_prunes_nothing(monkeypatch):
    """An empty fleet read is a tmux hiccup, not evidence that every pane died."""
    daemon._swallowed_streak["%1"] = (2, 0.0)
    monkeypatch.setattr(daemon, "fleet_panes", lambda: [])
    daemon._prune_pane_tables()
    assert "%1" in daemon._swallowed_streak


def test_the_retry_window_is_measured_from_the_cap(monkeypatch):
    """Later failures must not push the clock forward, or a pane nudged more often than the
    window would never get its retry."""
    _install(monkeypatch, FakePane(CODEX_RATE_LIMIT_MODAL, accepts=False))
    now = time.time()
    monkeypatch.setattr(daemon.time, "time", lambda: now)
    for _ in range(daemon.SWALLOW_MAX_ATTEMPTS):
        daemon.type_line("%2", NUDGE)
    capped_at = daemon._swallowed_streak["%2"][1]
    monkeypatch.setattr(daemon.time, "time", lambda: now + 60)
    daemon.type_line("%2", NUDGE)
    assert daemon._swallowed_streak["%2"][1] == capped_at


def test_concurrent_injections_cannot_interleave(monkeypatch):
    """Without a per-pane lock the verification is a TOCTOU check, not an authorization: two
    injectors each read a clean baseline, both type, both see a raised count, and the second
    Enter lands on whatever the first Enter brought up.

    The assertion is on INTERLEAVING, not on the number of Enters — both variants send one
    Enter per call. What the lock buys is that each call's capture→type→capture→Enter is
    indivisible. Each fake tmux op yields the GIL so an unlocked implementation interleaves
    almost immediately (verified: this test fails without the lock)."""
    import threading as th
    pane = _install(monkeypatch, FakePane(CLAUDE_IDLE))
    pane.trace = []
    pane.op_delay = 0.002
    results = []

    def run(name):
        results.append((name, daemon.type_line("%1", NUDGE)))

    threads = [th.Thread(target=run, args=(f"t{i}",), name=f"t{i}") for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert [r for _n, r in results] == ["sent"] * 4
    # the trace is a concatenation of whole sequences, never a mix of two callers' steps
    assert len(pane.trace) == 16
    for i in range(0, 16, 4):
        chunk = pane.trace[i:i + 4]
        assert [step for step, _who in chunk] == ["capture", "type", "capture", "Enter"], chunk
        assert len({who for _step, who in chunk}) == 1, chunk   # all four steps, one caller


def _lock_trace(monkeypatch):
    """Record lock/unlock and every send-keys in one ordered trace, so a test can assert the
    writes happen INSIDE the pane lock rather than merely that the lock was touched."""
    import contextlib
    trace = []
    real = daemon._pane_lock

    @contextlib.contextmanager
    def traced(pane):
        with real(pane):
            trace.append(("lock", pane))
            try:
                yield
            finally:
                trace.append(("unlock", pane))

    def fake_tmux(cmd, *a, **k):
        if cmd[1] == "send-keys":
            trace.append(("write", cmd[3]))
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(daemon, "_pane_lock", traced)
    monkeypatch.setattr(daemon, "_tmux", fake_tmux)
    monkeypatch.setattr(daemon.time, "sleep", lambda *a, **k: None)
    return trace


def _writes_are_locked(trace):
    depth = 0
    for step, _arg in trace:
        if step == "lock":
            depth += 1
        elif step == "unlock":
            depth -= 1
        elif step == "write":
            if depth <= 0:
                return False
    return bool(trace) and any(s == "write" for s, _ in trace)


def test_an_interrupt_writes_under_the_pane_lock(monkeypatch):
    """#165: a long interrupt instruction collapses into a paste chip exactly like any other
    long input. Written inside another injection's capture→type→capture window it raises that
    injection's chip count and authorises ITS Enter — onto whatever the interrupt brought up."""
    trace = _lock_trace(monkeypatch)
    monkeypatch.setattr(daemon, "read_registry", lambda: {"55": {"pane": "%1"}})
    monkeypatch.setattr(daemon, "pane_alive", lambda _p: True)
    monkeypatch.setattr(daemon, "reply", lambda *_a, **_k: True)

    daemon.interrupt_session({}, 55, "please stop and " + "explain yourself " * 40)
    assert _writes_are_locked(trace), trace


def test_the_command_relay_writes_under_the_pane_lock(monkeypatch):
    trace = _lock_trace(monkeypatch)
    monkeypatch.setattr(daemon, "read_registry", lambda: {"55": {"pane": "%1"}})
    monkeypatch.setattr(daemon, "pane_alive", lambda _p: True)
    monkeypatch.setattr(daemon, "reply", lambda *_a, **_k: True)

    daemon.handle_command({}, 55, "/status")
    assert _writes_are_locked(trace), trace


def test_the_model_switch_writes_under_the_pane_lock(monkeypatch):
    trace = _lock_trace(monkeypatch)
    monkeypatch.setattr(daemon, "read_registry", lambda: {"55": {"pane": "%1"}})
    monkeypatch.setattr(daemon, "pane_alive", lambda _p: True)
    monkeypatch.setattr(daemon, "engine_of_pane", lambda _p: "claude")
    monkeypatch.setattr(daemon, "pane_is_idle", lambda _p: True)
    monkeypatch.setattr(daemon, "reply", lambda *_a, **_k: True)
    daemon._pending_model["55"] = "opus"

    daemon._try_send_model({}, 55, "opus", "%1", "claude", 1)
    assert _writes_are_locked(trace), trace


def test_unreadable_pane_types_nothing(monkeypatch):
    """Err toward not acting on an unverifiable pane, like _cf_busy. Nothing typed means
    nothing stranded in an input box either."""
    pane = _install(monkeypatch, FakePane(CLAUDE_IDLE, capture_fails=True))
    assert daemon.type_line("%1", NUDGE) == "failed"
    assert pane.typed == []
    assert pane.enters == 0


def test_capture_failure_after_typing_withholds_enter(monkeypatch):
    pane = _install(monkeypatch, FakePane(CLAUDE_IDLE, capture_fails_after_typing=True))
    assert daemon.type_line("%1", NUDGE) == "swallowed"
    assert pane.enters == 0


def test_send_keys_failure_reports_failed(monkeypatch):
    def boom(argv, **kw):
        if argv[:2] == ["tmux", "capture-pane"]:
            return types.SimpleNamespace(returncode=0, stdout=CLAUDE_IDLE.replace("{INPUT}", ""))
        raise OSError("tmux is gone")
    monkeypatch.setattr(daemon, "_tmux", boom)
    assert daemon.type_line("%1", NUDGE) == "failed"


def test_empty_text_never_presses_enter(monkeypatch):
    pane = _install(monkeypatch, FakePane(CLAUDE_IDLE))
    assert daemon.type_line("%1", "   ") == "failed"
    assert pane.enters == 0


# ---- probe helpers -----------------------------------------------------------

def test_probe_is_the_tail_of_the_line():
    # the tail lands next to the cursor; a wrapped line's head can be rows away
    probe = daemon._echo_probe(NUDGE)
    assert probe == daemon._squash(NUDGE)[-daemon.ECHO_PROBE_CHARS:]
    assert daemon._squash(NUDGE).endswith(probe)


def test_squash_survives_a_wrap():
    assert daemon._squash("act on\n  it.") == daemon._squash("act on it.")


def test_the_counted_window_is_the_whole_capture(monkeypatch):
    """#165: the window must be anchored to the pane, not carved out of it by content.

    The old window was 'the last ten NON-EMPTY rows', so which rows counted depended on what
    was drawn. This pins that no row of the capture is excluded — the property the whole fix
    rests on, and the one a future 'just look at the bottom' optimisation would silently
    destroy.

    Codex round 2 was right that the first version of this test proved less than it claimed:
    it stubbed `_tmux` without inspecting argv, so it would still have passed if production
    had switched to a visible-only or `-S -1` capture. The argv is asserted now."""
    top_marker = "sentinel-at-the-very-top-of-the-capture"
    screen = top_marker + "\n" + "\n".join(f"row {i}" for i in range(60))
    seen = []

    def fake(argv, *_a, **_k):
        seen.append(argv)
        if argv[1] == "display-message":
            return types.SimpleNamespace(returncode=0, stdout="100,40,80,0", stderr="")
        return types.SimpleNamespace(returncode=0, stdout=screen, stderr="")

    monkeypatch.setattr(daemon, "_tmux", fake)
    text, geo = daemon._echo_capture("%1")
    assert daemon._squash(top_marker) in text
    cap = next(a for a in seen if a[1] == "capture-pane")
    assert "-S" in cap and cap[cap.index("-S") + 1] == f"-{daemon.ECHO_CAPTURE_LINES}", cap
    assert geo == "100,40,80,0"


def test_reflow_cannot_admit_residue_into_the_window(monkeypatch):
    """The blocking finding of the Codex review of #163, as a test.

    A modal is up and eats the keystrokes. An old paste chip sits just ABOVE the ten
    non-empty rows the old code counted; then the modal filters one row away, so with the old
    content-defined window that same stale chip slid INTO it and the count rose 0 -> 1 — and
    a rise presses Enter on the modal. Nothing was typed; nothing was pasted. With the window
    anchored to the pane the chip is counted in both captures and cancels out."""
    old_chip = "❯ [Pasted text #7]"
    before = old_chip + "\n" + "\n".join(f"modal row {i}" for i in range(10))
    after = old_chip + "\n" + "\n".join(f"modal row {i}" for i in range(9))
    pane = _install(monkeypatch, FakeSequence(iter([before, after])))
    assert daemon.type_line("%1", NUDGE) == "swallowed"
    assert pane.enters == 0


_GEO = "100,40,80,0"


@pytest.mark.parametrize("after_geo,label", [
    ("99,40,80,0", "history shrank — a resize pulled rows back out of scrollback"),
    ("100,41,80,0", "pane got taller"),
    ("100,40,81,0", "pane got wider, so wrapped rows reflow"),
    ("100,40,80,1", "the alternate screen was switched on"),
    ("garbage", "unparseable fingerprint"),
])
def test_a_moved_window_is_never_proof(monkeypatch, after_geo, label):
    """Codex round 2 disproved "history only ever scrolls up and OUT" (#165 r2).

    `-S -30` counts thirty rows above the CURRENT visible top, so when a resize pulls rows
    back out of history the window slides BACKWARDS and admits older content. A stale chip
    one row outside it then appears, the count rises 0 -> 1, and Enter goes to the modal —
    with nothing typed. Here the chip is present in the after-capture only, exactly as it
    would be; the geometry check is the only thing standing between that and a wrong Enter."""
    before = "\n".join(f"modal row {i}" for i in range(10))
    after = "❯ [Pasted text #7]\n" + before
    # Two fingerprints per capture: _echo_capture brackets each one, so a capture is usable
    # only when the geometry held across it. The resize lands BETWEEN the two captures.
    pane = _install(monkeypatch, FakeSequence(
        iter([before, after]),
        geometries=iter([_GEO, _GEO, after_geo, after_geo])))
    assert daemon.type_line("%1", NUDGE) == "swallowed", label
    assert pane.enters == 0, label


@pytest.mark.parametrize("geometries,label", [
    ([_GEO, "100,41,80,0", _GEO, _GEO], "resized DURING the before-capture"),
    ([_GEO, _GEO, _GEO, "100,41,80,0"], "resized DURING the after-capture"),
])
def test_a_capture_taken_across_a_resize_is_discarded(monkeypatch, geometries, label):
    """#166 review r3: sampling the fingerprint only AFTER each capture left the exact gap
    this check exists to close. Resize between the capture and the sample and BOTH captures
    report the same post-resize geometry — the move is invisible while the residue it
    admitted is not. Each capture is bracketed now, so one taken across a move is unusable."""
    before = "\n".join(f"modal row {i}" for i in range(10))
    after = "❯ [Pasted text #7]\n" + before
    pane = _install(monkeypatch, FakeSequence(iter([before, after]),
                                              geometries=iter(geometries)))
    assert daemon.type_line("%1", NUDGE) in ("swallowed", "failed"), label
    assert pane.enters == 0, label


@pytest.mark.parametrize("raw", ["", "garbage", "1,2,3", "a,b,c,d"])
def test_identical_unparseable_geometry_is_still_moved(raw):
    """Comparing the raw strings first made ("garbage", "garbage") read as "nothing moved",
    which contradicts the whole point of the guard. Parse, then compare."""
    assert daemon._geometry_moved(raw, raw) is True


def test_growing_history_still_delivers(monkeypatch):
    """The other direction must NOT be refused, or the guard costs real liveness: ordinary
    output scrolling into history grows `history_size`, which only ever moves the window's
    top edge forward and evicts. Refusing that would fail every injection into a pane that
    printed a line while we typed."""
    idle = "● done\n────────\n❯ \n────────\n  statusline\n"
    landed = "● done\n────────\n❯ [Pasted text #1]\n────────\n  statusline\n"
    pane = _install(monkeypatch, FakeSequence(
        iter([idle, landed]),
        geometries=iter([_GEO, _GEO, "137,40,80,0", "137,40,80,0"])))
    assert daemon.type_line("%1", "a long payload " * 40) == "sent"
    assert pane.enters == 1


def test_a_short_command_lands_on_a_churning_alternate_screen_pane(monkeypatch):
    """#168, reproduced from the live failure rather than imagined.

    A claude pane runs on the ALTERNATE SCREEN: `history_size` is 0, so `capture-pane -S -30`
    clamps to the ~23 visible rows and the whole counted window is under a kilobyte. When the
    daemon typed `/compact` into a healthy idle pane, one pre-existing occurrence scrolled out
    of that small window as the typed one appeared. #165 r2 refused the rise because the other
    count had fallen, reported the pane as blocked, and told the owner their terminal was stuck on a
    prompt — for a pane with no modal on it at all.

    `/compact` is also the pathological probe: `_echo_probe` returns the whole eight-character
    string, which occurs in ordinary conversation. The observed pane already contained multiple
    occurrences. The literal signal cannot be payload-specific here, which is
    exactly why refusing on any fall is unaffordable.

    What this pins is the transition the veto blocked: the typed text collapses into a chip
    while a stale literal leaves the small window. The chip rose; #165 r2 threw that away
    because the literal fell."""
    before = "stale /compact from earlier\nline B\n❯ \n  statusline"
    after = "line B\n❯ [Pasted text #4]\n  statusline"     # literal evicted, chip appeared
    pane = _install(monkeypatch, FakeSequence(iter([before, after]),
                                              geometries=iter([_GEO, _GEO, _GEO, _GEO])))
    monkeypatch.setattr(daemon, "_echo_probe", lambda _t: "/compact")
    assert daemon.type_line("%1", "/compact") == "sent"
    assert pane.enters == 1


def test_a_net_equal_count_is_still_refused(monkeypatch):
    """The boundary this fix does NOT move, stated so nobody assumes otherwise.

    If one occurrence scrolls out of the window as the typed one appears, the count nets to
    EQUAL — and equal has never been a rise, under any version of this rule. So dropping the
    fall veto does not rescue that case. Whether #168's live failure was this case or the mixed
    one above is unknown, because the refusal logged no counts; that is precisely why the
    logging went in alongside. Only the transcript settles it for good (#157)."""
    before = "stale /compact here\n❯ \n  statusline"
    after = "❯ /compact\n  statusline"                      # one out, one in -> 1 -> 1
    pane = _install(monkeypatch, FakeSequence(iter([before, after]),
                                              geometries=iter([_GEO, _GEO, _GEO, _GEO])))
    monkeypatch.setattr(daemon, "_echo_probe", lambda _t: "/compact")
    assert daemon.type_line("%1", "/compact") == "swallowed"
    assert pane.enters == 0


def test_a_modal_still_blocks_a_short_command(monkeypatch):
    """The other half of #168: relaxing the fall veto must not reopen #133. A modal swallows
    the keystrokes, so NEITHER count rises, and Enter is still withheld."""
    modal = "Approaching rate limits\n❯ 1. Switch to gpt-5.6-luna\n  2. Keep going\n  statusline"
    evicted = "❯ 1. Switch to gpt-5.6-luna\n  2. Keep going\n  statusline"   # churn, but no rise
    pane = _install(monkeypatch, FakeSequence(iter([modal, evicted]),
                                              geometries=iter([_GEO, _GEO, _GEO, _GEO])))
    monkeypatch.setattr(daemon, "_echo_probe", lambda _t: "/compact")
    assert daemon.type_line("%1", "/compact") == "swallowed"
    assert pane.enters == 0


def test_a_refusal_logs_the_counts(monkeypatch, capsys):
    """#168 had to be diagnosed from mechanism because the refusal logged only 'swallowed'.
    The numbers that would have settled it in one line are now in the journal."""
    screen = "nothing relevant here\n❯ \n  statusline"
    _install(monkeypatch, FakeSequence(iter([screen, screen]),
                                       geometries=iter([_GEO, _GEO, _GEO, _GEO])))
    daemon.type_line("%1", NUDGE)
    logged = capsys.readouterr().err
    assert "literal=0->0" in logged and "chip=0->0" in logged, logged
    assert "geo=" in logged


def test_a_pane_that_cannot_be_locked_is_not_typed_into(monkeypatch):
    """Failing closed, not degrading. If the lock can't be taken, another writer may be mid
    capture→type→capture, and typing anyway is precisely the TOCTOU the lock exists to stop."""
    import contextlib as _c

    @_c.contextmanager
    def refuse(_pane, timeout=None):
        raise daemon.PaneLockUnavailable("busy")
        yield  # pragma: no cover

    pane = _install(monkeypatch, FakePane(CLAUDE_IDLE))
    monkeypatch.setattr(daemon, "_pane_lock", refuse)
    assert daemon.type_line("%1", NUDGE) == "failed"
    assert pane.typed == [] and pane.enters == 0


def test_the_pane_lock_is_bounded_and_raises(monkeypatch, tmp_path):
    """An unbounded flock on the message-handler paths is a daemon-wide freeze: one stopped
    `tg-bridge notify` holding a pane lock would block getUpdates for EVERY topic (#110 was
    this same failure with a different lock). It must give up and raise instead."""
    monkeypatch.setattr(daemon, "state_path",
                        lambda *parts: str(tmp_path.joinpath(*parts[-1:])))
    import fcntl
    holder = open(tmp_path / "1.lock", "a")
    fcntl.flock(holder, fcntl.LOCK_EX)          # stand in for the wedged other process
    try:
        started = time.monotonic()
        with pytest.raises(daemon.PaneLockUnavailable):
            with daemon._pane_lock("%1", timeout=0.3):
                pass                             # pragma: no cover
        assert time.monotonic() - started < 5    # bounded, not "forever"
    finally:
        fcntl.flock(holder, fcntl.LOCK_UN)
        holder.close()


def test_an_unopenable_lock_file_refuses_rather_than_degrading(monkeypatch, tmp_path):
    """Silently falling back to thread-only serialization would drop cross-process safety at
    exactly the moment something is already wrong."""
    def boom(*_parts):
        raise OSError("read-only filesystem")
    monkeypatch.setattr(daemon, "state_path", boom)
    with pytest.raises(daemon.PaneLockUnavailable):
        with daemon._pane_lock("%1", timeout=0.3):
            pass                                 # pragma: no cover


def test_cf_clear_modal_takes_the_pane_lock_and_rechecks(monkeypatch):
    """Codex round 2 reproduced enters=2 here: a concurrent type_line typed its payload, this
    unlocked Enter SUBMITTED that payload, type_line then saw its own text in the transcript,
    read it as a rise, and sent a second Enter. Enter can commit another injector's content,
    so unlike the deliberately unlocked halt Escapes it must be serialized — and the modal
    must be re-checked INSIDE the lock, or the check is a TOCTOU."""
    order = []
    import contextlib as _c

    @_c.contextmanager
    def traced(pane, timeout=None):
        order.append(("lock", pane))
        try:
            yield
        finally:
            order.append(("unlock", pane))

    def fake(argv, *_a, **_k):
        if argv[1] == "capture-pane":
            order.append(("capture", argv))
            return types.SimpleNamespace(returncode=0, stdout="❯ 1. Resume this conversation\n"
                                                              "  2. Start fresh\n", stderr="")
        order.append((argv[-1], argv))
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(daemon, "_pane_lock", traced)
    monkeypatch.setattr(daemon, "_tmux", fake)
    monkeypatch.setattr(daemon, "_pending_cf", {"55": {"token": "tok"}})

    assert daemon._cf_clear_modal("55", "tok", "%1") is True
    steps = [s for s, _ in order]
    assert steps == ["lock", "capture", "Enter", "unlock"], order
    # and the capture must be the visible screen only: a ❯-block left in scrollback from
    # BEFORE the compaction would otherwise authorise this Enter against an idle composer.
    cap = next(a for s, a in order if s == "capture")
    assert "-S" not in cap, cap


# ---- call sites --------------------------------------------------------------

def test_maybe_nudge_reports_a_pane_that_keeps_swallowing(monkeypatch):
    """A picker never accepts the text, so the streak reaches the cap and the owner is told.

    #254 moved this from the FIRST swallow to a persistent one: most single swallows are a
    late render that the next tick delivers, and reporting those told the owner a session was
    unreachable while the message was already on its way. The alarm still fires here — it just
    takes the second consecutive failure to do it, which for the sweep is one tick later."""
    pane = _install(monkeypatch, FakePane(CODEX_RATE_LIMIT_MODAL, accepts=False))
    monkeypatch.setattr(daemon, "unread_count", lambda _tid: 1)
    monkeypatch.setattr(daemon, "pane_alive", lambda _p: True)
    monkeypatch.setattr(daemon, "validate_wake_claim", _always_current())
    reported = []
    monkeypatch.setattr(daemon, "report_blocked_pane",
                        lambda tid, p, what: reported.append((tid, p, what)))
    assert daemon.maybe_nudge(55, "%1") is False
    assert pane.enters == 0
    assert reported == []            # one swallow is not yet news

    while not daemon.pane_is_persistently_swallowing("%1"):
        assert daemon.maybe_nudge(55, "%1") is False
    assert reported and reported[0][0] == 55
    assert pane.enters == 0          # and still no Enter on the picker


def test_maybe_nudge_is_silent_on_a_normal_pane(monkeypatch):
    pane = _install(monkeypatch, FakePane(CODEX_IDLE))
    monkeypatch.setattr(daemon, "unread_count", lambda _tid: 1)
    monkeypatch.setattr(daemon, "pane_alive", lambda _p: True)
    monkeypatch.setattr(daemon, "validate_wake_claim", _always_current())
    reported = []
    monkeypatch.setattr(daemon, "report_blocked_pane",
                        lambda *a: reported.append(a))
    assert daemon.maybe_nudge(55, "%1") is True
    assert pane.enters == 1
    assert reported == []


def test_blocked_report_is_rate_limited(monkeypatch):
    monkeypatch.setattr(daemon, "peek_pane", lambda _p, lines=12: "picker")
    monkeypatch.setattr(daemon, "load_config", lambda: {"bot_token": "t", "chat_id": "c"})
    sent = []
    monkeypatch.setattr(daemon, "reply",
                        lambda cfg, tid, text: bool(sent.append(text)) or True)
    assert daemon.report_blocked_pane(55, "%1", "a message") is True
    assert daemon.report_blocked_pane(55, "%1", "a message") is False   # cooldown
    assert len(sent) == 1


def test_a_failed_report_does_not_buy_30_minutes_of_silence(monkeypatch):
    """Stamping the cooldown before the send meant one failed Telegram call suppressed the
    next half hour of escalations for that topic."""
    monkeypatch.setattr(daemon, "peek_pane", lambda _p, lines=12: "picker")
    monkeypatch.setattr(daemon, "load_config", lambda: {"bot_token": "t", "chat_id": "c"})
    attempts = []

    def flaky(cfg, tid, text):
        attempts.append(text)
        if len(attempts) == 1:
            raise OSError("telegram down")
        return True                      # reply() reports delivery since #161

    monkeypatch.setattr(daemon, "reply", flaky)
    assert daemon.report_blocked_pane(55, "%1", "a message") is False
    assert daemon.report_blocked_pane(55, "%1", "a message") is True   # not suppressed
    assert len(attempts) == 2


def test_swallowed_briefing_is_retried(monkeypatch):
    """idle_sweep_loop cannot stand in for this: for codex it only makes a candidate when
    unread > 0, and it sends a generic re-arm nudge rather than the briefing. A pane revived
    into a modal with an empty inbox would otherwise stay unbriefed indefinitely."""
    timers = []
    monkeypatch.setattr(daemon, "pane_alive", lambda _p: True)
    monkeypatch.setattr(daemon, "type_line", lambda *a, **k: "swallowed")
    monkeypatch.setattr(daemon, "report_blocked_pane", lambda *a: True)
    marked = []
    monkeypatch.setattr(daemon, "update_registry", lambda fn: marked.append(fn))
    monkeypatch.setattr(daemon.threading, "Timer",
                        lambda delay, fn, args=(): timers.append((delay, args)) or
                        types.SimpleNamespace(start=lambda: None))

    daemon.deliver_briefing("%1", "4109", "codex", "brief {tid}")
    assert marked == []                                   # never marked briefed
    assert len(timers) == 1
    assert timers[0][0] == daemon.BRIEFING_RETRY_DELAY
    assert timers[0][1][-1] == 2                          # next attempt number


def test_a_retry_abandons_a_rebound_pane(monkeypatch):
    """The timer fires minutes later holding a captured pane. If the topic was rebound in
    the meantime, typing into that pane injects topic A's briefing into topic B's session —
    and then marks topic A briefed at a pane that received nothing."""
    typed = []
    monkeypatch.setattr(daemon, "pane_alive", lambda _p: True)
    monkeypatch.setattr(daemon, "type_line", lambda p, t, **k: typed.append((p, t)) or "sent")
    monkeypatch.setattr(daemon, "read_registry", lambda: {"4109": {"pane": "%new"}})
    marked = []
    monkeypatch.setattr(daemon, "update_registry", lambda fn: marked.append(fn))

    daemon.deliver_briefing("%old", "4109", "codex", "brief {tid}", attempt=2)
    assert typed == []          # nothing typed into the pane that now serves someone else
    assert marked == []         # and the topic is not falsely marked briefed


def test_a_retry_abandons_a_topic_already_briefed(monkeypatch):
    """Repeated revives create independent chains; whichever lands first marks the topic and
    the rest must abandon rather than inject a duplicate briefing."""
    typed = []
    monkeypatch.setattr(daemon, "pane_alive", lambda _p: True)
    monkeypatch.setattr(daemon, "type_line", lambda p, t, **k: typed.append((p, t)) or "sent")
    monkeypatch.setattr(daemon, "current_boot_id", lambda: "boot-1")
    monkeypatch.setattr(daemon, "read_registry",
                        lambda: {"4109": {"pane": "%1", "briefed_boot": "boot-1"}})
    monkeypatch.setattr(daemon, "update_registry", lambda fn: None)

    daemon.deliver_briefing("%1", "4109", "codex", "brief {tid}", attempt=2)
    assert typed == []


def test_first_attempt_does_not_consult_the_registry(monkeypatch):
    """The first call runs inline right after the revive bound the pane; re-reading there
    would race the binding write it was just handed."""
    typed = []
    monkeypatch.setattr(daemon, "pane_alive", lambda _p: True)
    monkeypatch.setattr(daemon, "type_line", lambda p, t, **k: typed.append((p, t)) or "sent")
    monkeypatch.setattr(daemon, "read_registry", lambda: {})   # not yet visible
    monkeypatch.setattr(daemon, "update_registry", lambda fn: None)
    monkeypatch.setattr(daemon, "current_boot_id", lambda: "boot-1")

    daemon.deliver_briefing("%1", "4109", "codex", "brief {tid}")
    assert typed == [("%1", "brief 4109")]


def test_giving_up_on_a_briefing_forces_the_escalation(monkeypatch):
    """The chain ends before the swallowed cap can release, and for a codex pane with an
    empty inbox the sweep is not a fallback — so the last attempt must not go quiet."""
    monkeypatch.setattr(daemon, "pane_alive", lambda _p: True)
    monkeypatch.setattr(daemon, "type_line", lambda *a, **k: "swallowed")
    monkeypatch.setattr(daemon, "read_registry", lambda: {"4109": {"pane": "%1"}})
    monkeypatch.setattr(daemon, "update_registry", lambda fn: None)
    monkeypatch.setattr(daemon, "current_boot_id", lambda: "boot-1")
    monkeypatch.setattr(daemon.threading, "Timer",
                        lambda *a, **k: types.SimpleNamespace(start=lambda: None))
    reports = []
    monkeypatch.setattr(daemon, "report_blocked_pane",
                        lambda tid, p, what: reports.append(what))

    daemon._blocked_reported["4109"] = time.time()          # inside the normal cooldown
    daemon.deliver_briefing("%1", "4109", "codex", "brief {tid}",
                            attempt=daemon.BRIEFING_MAX_ATTEMPTS)
    assert len(reports) == 1 and "final attempt" in reports[0]
    assert "4109" not in daemon._blocked_reported            # cooldown cleared to force it


def test_briefing_retries_are_bounded(monkeypatch):
    timers = []
    monkeypatch.setattr(daemon, "pane_alive", lambda _p: True)
    monkeypatch.setattr(daemon, "type_line", lambda *a, **k: "swallowed")
    monkeypatch.setattr(daemon, "report_blocked_pane", lambda *a: True)
    monkeypatch.setattr(daemon, "update_registry", lambda fn: None)
    monkeypatch.setattr(daemon.threading, "Timer",
                        lambda delay, fn, args=(): timers.append(args) or
                        types.SimpleNamespace(start=lambda: None))

    daemon.deliver_briefing("%1", "4109", "codex", "brief {tid}",
                            attempt=daemon.BRIEFING_MAX_ATTEMPTS)
    assert timers == []                                   # gives up instead of looping


def test_carry_forward_inject_is_verified(monkeypatch):
    """_cf_wait_idle reads 'no spinner, no compaction bar' as idle, which a claude approval
    or model-confirm modal satisfies — so this path could type into a picker and have its
    Enter select the default."""
    pane = _install(monkeypatch, FakePane(CODEX_RATE_LIMIT_MODAL, accepts=False))
    reported = []
    monkeypatch.setattr(daemon, "report_blocked_pane",
                        lambda tid, p, what: reported.append((tid, what)))
    daemon._pending_cf["4109"] = {"token": "tok", "pane": "%1"}
    try:
        assert daemon._cf_inject_owned("4109", "tok", "%1", "carry-forward prompt") is False
        assert pane.enters == 0
        assert reported and reported[0][0] == "4109"
        assert "4109" in daemon._pending_cf          # flow not released on a failed inject
    finally:
        daemon._pending_cf.pop("4109", None)


def test_blocked_report_shows_the_prompt_and_fences_it(monkeypatch):
    monkeypatch.setattr(daemon, "peek_pane",
                        lambda _p, lines=12: "1. Switch to gpt-5.6-luna\nuse `x` here")
    monkeypatch.setattr(daemon, "load_config", lambda: {"bot_token": "t", "chat_id": "c"})
    sent = []
    monkeypatch.setattr(daemon, "reply",
                        lambda cfg, tid, text: bool(sent.append(text)) or True)
    daemon.report_blocked_pane(55, "%1", "a new Telegram message")
    body = sent[0]
    assert "Switch to gpt-5.6-luna" in body          # The owner sees the actual prompt
    assert body.count("```") == 2                    # a backtick in the capture can't break out


def _always_current():
    import contextlib

    @contextlib.contextmanager
    def _ctx(_inbox, _claim):
        yield True
    return _ctx


# ---- Claude Code collapses long input into a chip (#163) ---------------------
#
# The pane never renders the text, so the literal tail probe could not match and #133
# withheld Enter on every long injection — every carry-forward broke the moment it went
# live. Observed examples rendered numbered paste placeholders in the input line.

def _chip_tmux(states):
    """tmux stub whose capture-pane returns states.pop(0) each time, recording send-keys."""
    sent = []

    def fake(cmd, *a, **k):
        if cmd[1] == "display-message":
            return types.SimpleNamespace(returncode=0, stdout="100,40,80,0")
        if cmd[1] == "capture-pane":
            return types.SimpleNamespace(returncode=0, stdout=states.pop(0))
        sent.append(cmd)
        return types.SimpleNamespace(returncode=0, stdout="")
    return fake, sent


def test_a_collapsed_paste_counts_as_delivered(monkeypatch):
    idle = "● done\n────────\n❯ \n────────\n  statusline\n"
    chipped = "● done\n────────\n❯ [Pasted text #1]\n────────\n  statusline\n"
    fake, sent = _chip_tmux([idle, chipped])
    monkeypatch.setattr(daemon, "_tmux", fake)
    monkeypatch.setattr(daemon.time, "sleep", lambda *a, **k: None)
    monkeypatch.setattr(daemon, "_swallowed_streak", {})

    assert daemon.type_line("%1", "a very long carry-forward prompt " * 40) == "sent"
    assert [c[-1] for c in sent] == ["a very long carry-forward prompt " * 40, "Enter"]


def test_an_existing_chip_does_not_authorise_a_new_injection(monkeypatch):
    # Anti-residue, same rule as the literal probe: a chip from an EARLIER injection is not
    # evidence for this one. Only a RISE counts.
    stale = "● done\n────────\n❯ [Pasted text #1]\n────────\n  statusline\n"
    fake, sent = _chip_tmux([stale, stale])
    monkeypatch.setattr(daemon, "_tmux", fake)
    monkeypatch.setattr(daemon.time, "sleep", lambda *a, **k: None)
    monkeypatch.setattr(daemon, "_swallowed_streak", {})

    assert daemon.type_line("%1", "another long prompt " * 40) == "swallowed"
    assert "Enter" not in [c[-1] for c in sent]


def test_a_second_chip_does_authorise_it(monkeypatch):
    one = "● done\n────────\n❯ [Pasted text #1]\n────────\n  statusline\n"
    two = "● done\n────────\n❯ [Pasted text #1] [Pasted text #2]\n────────\n  statusline\n"
    fake, sent = _chip_tmux([one, two])
    monkeypatch.setattr(daemon, "_tmux", fake)
    monkeypatch.setattr(daemon.time, "sleep", lambda *a, **k: None)
    monkeypatch.setattr(daemon, "_swallowed_streak", {})

    assert daemon.type_line("%1", "long prompt " * 40) == "sent"
    assert [c[-1] for c in sent][-1] == "Enter"


def test_a_modal_still_blocks_enter_with_the_chip_rule_in_place(monkeypatch):
    # The whole point of #133: a picker swallows the keystrokes, so NEITHER signal rises.
    modal = ("Approaching rate limits\n❯ 1. Switch to gpt-5.6-luna\n  2. Keep going\n"
             "────────\n  statusline\n")
    fake, sent = _chip_tmux([modal, modal])
    monkeypatch.setattr(daemon, "_tmux", fake)
    monkeypatch.setattr(daemon.time, "sleep", lambda *a, **k: None)
    monkeypatch.setattr(daemon, "_swallowed_streak", {})

    assert daemon.type_line("%1", "please answer the owner " * 40) == "swallowed"
    assert "Enter" not in [c[-1] for c in sent]


def test_a_short_line_still_verifies_literally(monkeypatch):
    # Short text is rendered verbatim and must keep working exactly as before.
    idle = "● done\n────────\n❯ \n────────\n  statusline\n"
    echoed = "● done\n────────\n❯ ping from the owner\n────────\n  statusline\n"
    fake, sent = _chip_tmux([idle, echoed])
    monkeypatch.setattr(daemon, "_tmux", fake)
    monkeypatch.setattr(daemon.time, "sleep", lambda *a, **k: None)
    monkeypatch.setattr(daemon, "_swallowed_streak", {})

    assert daemon.type_line("%1", "ping from the owner") == "sent"
    assert [c[-1] for c in sent][-1] == "Enter"


# ---- every collapse form each engine actually ships (#165) -------------------
#
# Read out of the shipped binaries, not the docs — claude 2.1.235 renders three text forms
# and codex 0.146.0 a fourth. #163 recognised only the first two, so the other two would have
# failed exactly the same way once a payload got big enough.

@pytest.mark.parametrize("chip", [
    "[Pasted text #1]",                     # claude, short paste
    "[Pasted text #2 +180 lines]",          # claude, multi-line paste
    "[...Truncated text #3 +900 lines...]",  # claude, oversized paste
    "[Pasted Content 2317 chars]",          # codex
])
def test_every_shipped_collapse_form_counts_as_delivered(monkeypatch, chip):
    idle = "● done\n────────\n❯ \n────────\n  statusline\n"
    collapsed = f"● done\n────────\n❯ {chip}\n────────\n  statusline\n"
    fake, sent = _chip_tmux([idle, collapsed])
    monkeypatch.setattr(daemon, "_tmux", fake)
    monkeypatch.setattr(daemon.time, "sleep", lambda *a, **k: None)
    monkeypatch.setattr(daemon, "_swallowed_streak", {})

    assert daemon.type_line("%1", "a long payload " * 40) == "sent"
    assert [c[-1] for c in sent][-1] == "Enter"


@pytest.mark.parametrize("placeholder", ["[Image #1]", "[Audio #1]"])
def test_a_non_text_placeholder_is_not_proof_that_text_landed(monkeypatch, placeholder):
    """Claude renders images and audio with the same bracket grammar. They say a DIFFERENT
    payload type arrived, not that our text did, so they must not authorise Enter."""
    idle = "● done\n────────\n❯ \n────────\n  statusline\n"
    other = f"● done\n────────\n❯ {placeholder}\n────────\n  statusline\n"
    fake, sent = _chip_tmux([idle, other])
    monkeypatch.setattr(daemon, "_tmux", fake)
    monkeypatch.setattr(daemon.time, "sleep", lambda *a, **k: None)
    monkeypatch.setattr(daemon, "_swallowed_streak", {})

    assert daemon.type_line("%1", "a long payload " * 40) == "swallowed"
    assert "Enter" not in [c[-1] for c in sent]


def test_a_chip_whose_number_changes_in_place_is_not_a_rise(monkeypatch):
    """One placeholder replaced by another leaves the count equal. Equal is unresolvable, so
    it must fail closed even though the digits changed."""
    one = "● done\n────────\n❯ [Pasted text #1]\n────────\n  statusline\n"
    two = "● done\n────────\n❯ [Pasted text #2]\n────────\n  statusline\n"
    fake, sent = _chip_tmux([one, two])
    monkeypatch.setattr(daemon, "_tmux", fake)
    monkeypatch.setattr(daemon.time, "sleep", lambda *a, **k: None)
    monkeypatch.setattr(daemon, "_swallowed_streak", {})

    assert daemon.type_line("%1", "long prompt " * 40) == "swallowed"
    assert "Enter" not in [c[-1] for c in sent]


# ---- the two signals move independently: pin the whole matrix (#165) ---------

@pytest.mark.parametrize("before,after,expected", [
    ((0, 0), (0, 0), "swallowed"),  # neither rendered — a modal ate it
    ((0, 0), (1, 0), "sent"),       # literal appeared
    ((0, 0), (0, 1), "sent"),       # collapsed into a chip
    ((1, 0), (2, 0), "sent"),       # literal rose past a stale copy
    ((0, 1), (0, 2), "sent"),       # chip rose past a stale chip
    ((1, 0), (0, 1), "sent"),       # mixed: the chip rose. #168 — refusing this cost a
                                    # real carry-forward on an alternate-screen pane, where
                                    # a falling count is ordinary eviction, not a signal.
    ((0, 1), (1, 0), "sent"),       # mixed the other way — the literal rose
    ((1, 1), (0, 0), "swallowed"),  # both fell — everything left is stale
    ((2, 0), (1, 0), "swallowed"),  # a stale copy scrolled out; no new one arrived
    ((1, 1), (1, 1), "swallowed"),  # unchanged — unresolvable, so fail closed
])
def test_the_rise_matrix(monkeypatch, before, after, expected):
    """A rise in one signal AND no fall in the other is proof; anything else is not.

    The mixed transitions are the ones worth pinning, and they have moved twice. #165 accepted
    them; #165 r2 refused them as observationally ambiguous; #168 accepts them again, because
    the refusal broke a live carry-forward and the geometry check added in r2 already covers
    the threat the refusal was blunt cover for. The r2 argument was not wrong about the
    ambiguity — it was wrong that refusing was affordable."""
    calls = {"n": 0}
    sent = []

    def fake(cmd, *a, **k):
        if cmd[1] == "display-message":
            return types.SimpleNamespace(returncode=0, stdout="100,40,80,0")
        if cmd[1] == "capture-pane":
            counts = before if calls["n"] == 0 else after
            calls["n"] += 1
            return types.SimpleNamespace(
                returncode=0,
                stdout="\n".join(["tail-probe-marker"] * counts[0]
                                 + ["[Pasted text #9]"] * counts[1]))
        sent.append(cmd)
        return types.SimpleNamespace(returncode=0, stdout="")

    monkeypatch.setattr(daemon, "_tmux", fake)
    monkeypatch.setattr(daemon, "_echo_probe", lambda _t: "tail-probe-marker")
    monkeypatch.setattr(daemon.time, "sleep", lambda *a, **k: None)
    monkeypatch.setattr(daemon, "_swallowed_streak", {})

    assert daemon.type_line("%1", "some payload") == expected
    assert ("Enter" in [c[-1] for c in sent]) is (expected == "sent")


def test_cf_clear_modal_refuses_enter_on_dont_ask_me_again(monkeypatch):
    """A2, the prohibition with no recovery. Codex round 3 reproduced this exact capture: the
    post-/compact screen IS Claude's three-row resume picker, and the live cursor is on row 3.
    _cf_clear_modal matched any `❯ <n>.` row and pressed Enter, which sets
    resumeReturnDismissed and disables the resume picker on this machine PERMANENTLY —
    silently killing the very feature the reopen-choice flow relays. Leaving the modal up is
    recoverable; a human can answer it. Pressing Enter is not."""
    keys = []

    def fake(argv, *_a, **_k):
        if argv[1] == "capture-pane":
            return types.SimpleNamespace(
                returncode=0,
                stdout="  1. Resume from summary (recommended)\n"
                       "  2. Resume full session as-is\n"
                       "❯ 3. Don't ask me again\n",
                stderr="")
        keys.append(argv[-1])
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(daemon, "_tmux", fake)
    monkeypatch.setattr(daemon, "_pending_cf", {"55": {"token": "tok"}})
    reported = []
    monkeypatch.setattr(daemon, "report_blocked_pane",
                        lambda tid, pane, what: reported.append(what))

    assert daemon._cf_clear_modal("55", "tok", "%1") is False
    assert keys == [], "pressed Enter on 'Don't ask me again' — that is permanent (A2)"
    assert reported and "permanently" in reported[0], "refused silently"


def test_cf_clear_modal_still_clears_an_ordinary_modal(monkeypatch):
    """The A2 guard must be surgical: every other post-/compact modal still gets its Enter,
    or carry-forward stalls behind a dialog nobody is there to dismiss."""
    keys = []

    def fake(argv, *_a, **_k):
        if argv[1] == "capture-pane":
            return types.SimpleNamespace(
                returncode=0,
                stdout="❯ 1. Resume from summary (recommended)\n"
                       "  2. Resume full session as-is\n"
                       "  3. Don't ask me again\n",
                stderr="")
        keys.append(argv[-1])
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(daemon, "_tmux", fake)
    monkeypatch.setattr(daemon, "_pending_cf", {"55": {"token": "tok"}})

    assert daemon._cf_clear_modal("55", "tok", "%1") is True
    assert keys == ["Enter"]
