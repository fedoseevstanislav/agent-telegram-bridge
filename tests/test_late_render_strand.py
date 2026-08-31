"""#250: measure the late render before changing what a late render authorises.

`type_line` types the wake line and presses Enter only on a strict rise in its probe between
a capture taken before the keystrokes and one taken `settle` after (#133 — Enter on a modal
accepts the highlighted option). One 0.3s sample decides, so a pane that repaints later reads
exactly like a modal, permanently: on 2026-08-30 pane %290 logged `literal=0->0` while nine
minutes into a shell command, with the line plainly in its box afterwards, and the topic was
told every thirty minutes that its session was unreachable.

Acting on a later look was written twice and refused twice, for the same reason both times.
The evidence is a COUNT of the text anywhere in the capture, standing in for "our text is in
the input box"; that holds only while our keystroke is the sole thing that can move the
count. Widen the window and an older identical wake line, repainted behind a still-open
picker, moves it instead — reproduced against the real function (#253).

So the later samples observe and authorise nothing. These tests pin exactly that: the
decision is the first sample's, unchanged, and the extra looks can only produce a log line.
"""

import types

import pytest

from bridge import daemon


CLAUDE_IDLE = """\
  scrollback that says nothing about whether the pane will accept typed text
────────────────────────────────────────────────────────────────────────────
❯ {INPUT}
────────────────────────────────────────────────────────────────────────────
  example-repo (main) • Opus 5 (1M context) • 37m 91% W78% • $12.34 • 70%
  ⏵⏵ bypass permissions on · 1 shell"""

# A picker has no input box at all: printable keys are swallowed, Enter takes the highlight.
CODEX_RATE_LIMIT_MODAL = """\
  Approaching rate limits
  Switch to gpt-5.6-luna for lower credit usage?

› 1. Switch to gpt-5.6-luna
  2. Keep current model"""

NUDGE = "[tg-bridge] New Telegram message in your topic — run `tg-bridge recv --topic 33` and act on it."
PANE = "%290"


def _screen(text=""):
    return CLAUDE_IDLE.replace("{INPUT}", text)


# The reviewer's reproduction, kept as a fixture: the picker is still up — it swallowed our
# keystrokes — and the session's scrollback repaints an EARLIER copy of the same wake line.
# The count rises with our text nowhere near the input box.
MODAL_WITH_REPAINTED_NUDGE = CODEX_RATE_LIMIT_MODAL + "\n  " + NUDGE

CHIP = _screen("[Pasted text #7 +12 lines]")


class Tmux:
    """Serves a scripted sequence of captures, so a test can say exactly when the pane
    repaints. The last screen repeats once the script runs out."""

    def __init__(self, screens, geometries=None):
        self.screens = list(screens)
        self.geometries = list(geometries) if geometries else None
        self.enters = 0
        self.typed = []

    def _pop(self, seq, default):
        if not seq:
            return default
        return seq.pop(0) if len(seq) > 1 else seq[0]

    def __call__(self, argv, **kw):
        if argv[:2] == ["tmux", "display-message"]:
            return types.SimpleNamespace(
                returncode=0, stdout=self._pop(self.geometries, "100,40,80,0"), stderr="")
        if argv[:2] == ["tmux", "capture-pane"]:
            return types.SimpleNamespace(returncode=0, stdout=self._pop(self.screens, ""), stderr="")
        if argv[-1] == "Enter":
            self.enters += 1
        elif "-l" in argv:
            self.typed.append(argv[-1])
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")


@pytest.fixture
def slept(monkeypatch):
    waits = []
    monkeypatch.setattr(daemon.time, "sleep", waits.append)
    return waits


@pytest.fixture(autouse=True)
def _reset_pane_state():
    tables = (daemon._blocked_reported, daemon._swallowed_streak, daemon._pane_locks)
    for table in tables:
        table.clear()
    yield
    for table in tables:
        table.clear()


def test_a_late_render_authorises_nothing(monkeypatch, slept):
    """G1. The whole point. The text appears at sample 1 — and Enter is still withheld,
    because a count that moves after our keystroke has settled is not proof our keystroke
    moved it."""
    tmux = Tmux([_screen(), _screen(), _screen(NUDGE)])
    monkeypatch.setattr(daemon, "_tmux", tmux)

    assert daemon.type_line(PANE, NUDGE) == "swallowed"
    assert tmux.enters == 0


def test_the_reviewers_modal_repaint_does_not_press_enter(monkeypatch, slept):
    """G2. The reproduction that refused the previous two attempts: a picker that swallowed
    our keystrokes, plus an older identical wake line repainting during the extra wait. Acting
    on that rise sends Enter, and the picker's highlighted option is a model switch."""
    tmux = Tmux([CODEX_RATE_LIMIT_MODAL,             # before
                 CODEX_RATE_LIMIT_MODAL,             # first sample: nothing
                 MODAL_WITH_REPAINTED_NUDGE])        # scrollback repaint, box still empty
    monkeypatch.setattr(daemon, "_tmux", tmux)

    assert daemon.type_line(PANE, NUDGE) == "swallowed"
    assert tmux.enters == 0


def test_the_first_sample_still_decides_exactly_as_before(monkeypatch, slept):
    """G3. No regression: a pane that echoes within `settle` is delivered, on both signals."""
    for screen in (_screen(NUDGE), CHIP):
        tmux = Tmux([_screen(), screen])
        monkeypatch.setattr(daemon, "_tmux", tmux)

        assert daemon.type_line(PANE, NUDGE) == "sent"
        assert tmux.enters == 1
        assert slept == [0.3]
        slept.clear()
        daemon._swallowed_streak.clear()


def test_a_confirmed_pane_is_never_kept_waiting(monkeypatch, slept):
    """G4. The extra looks are for the panes that failed; a delivered one must not pay them."""
    tmux = Tmux([_screen(), _screen(NUDGE)])
    monkeypatch.setattr(daemon, "_tmux", tmux)

    daemon.type_line(PANE, NUDGE)
    assert slept == [0.3]


def test_the_observation_stops_at_the_first_thing_it_sees(monkeypatch, slept):
    """G5. Once a late render has been recorded there is nothing further to learn, and the
    pane lock is held throughout."""
    tmux = Tmux([_screen(), _screen(), _screen(NUDGE)])
    monkeypatch.setattr(daemon, "_tmux", tmux)

    daemon.type_line(PANE, NUDGE)
    assert slept == [0.3, 0.6]


def test_the_wait_is_bounded_and_the_ladder_is_not_empty(monkeypatch, slept):
    """G6. type_line holds the pane lock across the waits and the sweep walks panes in turn.
    Asserting against ECHO_EXTRA_WAITS alone would pass with an empty ladder — a gap the
    review caught — so the ladder's existence is pinned against the constant."""
    assert len(daemon.ECHO_EXTRA_WAITS) >= 1
    tmux = Tmux([_screen()])
    monkeypatch.setattr(daemon, "_tmux", tmux)

    daemon.type_line(PANE, NUDGE)
    assert len(slept) >= 2
    assert slept == [0.3] + list(daemon.ECHO_EXTRA_WAITS)
    assert sum(slept) <= 3.0


def test_a_window_that_moved_is_not_re_sampled(monkeypatch, slept):
    """G7. Re-reading a window that moved cannot make two incomparable counts comparable.
    The screen must NOT show the text, or the first sample would end the loop and this would
    pass without the abstain ever running — how an earlier version of this test missed it."""
    tmux = Tmux([_screen(), _screen()],
                geometries=["100,40,80,0", "100,40,80,0", "50,40,80,0", "50,40,80,0"])
    monkeypatch.setattr(daemon, "_tmux", tmux)

    assert daemon.type_line(PANE, NUDGE) == "swallowed"
    assert tmux.enters == 0
    assert slept == [0.3]


def test_nothing_is_retyped_while_observing(monkeypatch, slept):
    """G8. Every extra copy in an unverifiable box is one that has to come back out — the
    reason the swallow cap exists at all."""
    tmux = Tmux([_screen(), _screen(), _screen(), _screen(NUDGE)])
    monkeypatch.setattr(daemon, "_tmux", tmux)

    daemon.type_line(PANE, NUDGE)
    assert tmux.typed == [NUDGE]
