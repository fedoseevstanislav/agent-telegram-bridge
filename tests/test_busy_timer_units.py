"""#181 — the busy check must recognise the spinner timer in every unit it is rendered in.

`_CF_SPINNER_TIMER_RE` matched `…(48s` but not `…(4m 18s`. Claude Code switches the elapsed
timer to minutes once a turn passes 60 seconds, so **any pane busy for a minute or more read
as idle** — and every gate built on `pane_is_idle` / `_cf_busy` inverts exactly for the long
turns worth not interrupting:

  * the self-heal sweep types a nudge into a pane that is mid-turn (the swallow class,
    #133/#173), and a busy pane is where a nudge is most likely to be lost;
  * auto-carry-forward fires `/compact` into a live turn;
  * the revive path briefs a session that has not settled.

Found live on pane %24, whose status line read `✢ Fiddle-faddling… (4m 18s · ↓ 1.5k tokens ·
thought for 1s)` while `_cf_busy` reported idle.

The fix anchors on the ellipsis and the first unit (`…\\(\\d+[hms]`) rather than enumerating
formats. Both directions are pinned below, because widening a busy-detector is exactly the
change that can start reading frozen tool-result durations as live work.
"""

import pytest

from bridge import daemon


# Real captures. The live ones are what the pane shows DURING a turn; the frozen ones are
# scrollback residue that must never pin an idle pane to busy (#85 blocker 1).
LIVE = [
    "✽ Mulling… (10s · ↓ 200 tokens)",
    "✢ Fiddle-faddling… (4m 18s · ↓ 1.5k tokens · thought for 1s)",   # the #181 case
    "✽ Working… (1h 2m 3s · ↓ 9k tokens)",
    "✻ Compacting conversation… (8s)",
    "✽ Thinking…  (59s · ↑ 1.2k tokens)",                             # tab before the paren
]

FROZEN_OR_IDLE = [
    "  ⎿  Tip: Connect Claude to your IDE · /ide",
    "  ⎿  ran the deploy script (1m 36s · 4 lines)",                   # completed tool row
    "  ⎿  read 40 lines (8s)",                                         # the #85 residue shape
    "  ⎿  Read the deploy runbook… (8s)",                              # ellipsis + timer:
    "  ⎿  Ran the migration… (2m 14s · 12 lines)",                     # only the ⎿ guard
    '     2>&1) && { echo "$out"; break; }; done (1m 36s',              # wrapped continuation
    "     · 4 lines)",                                                 # its wrap tail
    "❯ ",
    "  user • Fable 5 • 4h55m 99% W47% • $32.38 • 11%",               # status line durations
    "  ⏵⏵ bypass permissions on (shift+tab to cycle) · ← for agents",
    "new task? /clear to save 202.3k tokens",
]


@pytest.mark.parametrize("line", LIVE)
def test_a_live_turn_reads_as_busy_in_every_unit(line):
    assert daemon._cf_line_is_busy(line) is True, (
        "a pane mid-turn reads as idle, so the daemon may type into it or compact it (#181)"
    )


@pytest.mark.parametrize("line", FROZEN_OR_IDLE)
def test_frozen_durations_and_chrome_never_read_as_busy(line):
    assert daemon._cf_line_is_busy(line) is False, (
        "scrollback residue pins an idle pane to busy, which blocks every nudge to it"
    )


def test_the_minutes_case_specifically_is_not_lost_again():
    # The whole defect in one assertion, on the exact string captured from pane %24.
    assert daemon._cf_text_is_busy(
        "  ⎿  Tip: Connect Claude to your IDE · /ide\n"
        "✢ Fiddle-faddling… (4m 18s · ↓ 592 tokens)\n"
        "❯ \n"
    )


def test_a_truncated_tool_row_is_why_the_tool_result_guard_matters():
    # A tool row truncated with an ellipsis puts "… (8s)" on the line, which the spinner
    # pattern matches on sight. Nothing but the ⎿ guard separates that frozen duration from a
    # live turn — and without it, scrollback pins an idle pane to busy forever (#85).
    assert daemon._cf_line_is_busy("  ⎿  Read the deploy runbook… (8s)") is False
    assert daemon._cf_line_is_busy("  ⎿  Ran the migration… (2m 14s · 12 lines)") is False
    # ...while the same text on a live status line IS busy.
    assert daemon._cf_line_is_busy("✽ Ran the migration… (2m 14s · 12 lines)") is True


def test_the_wrapped_tool_row_is_why_the_ellipsis_anchor_matters():
    # A completed tool row can wrap so its frozen "(1m 36s" lands on a continuation line that
    # does NOT start with ⎿, so the ⎿ guard cannot catch it. What keeps it out is that the
    # duration is not preceded by the spinner's ellipsis. Pin that, or a future widening of
    # the pattern re-introduces the false positive silently.
    wrapped = (
        "  ⎿  $ for i in 1 2 3; do\n"
        '     out=$(~/tools/telegram-client/scripts/tg read 100200300)\n'
        '     2>&1) && { echo "$out"; break; }; done (1m 36s\n'
        "     · 4 lines)\n"
        "❯ \n"
    )
    assert daemon._cf_text_is_busy(wrapped) is False


# ---- the compaction detector shares the same regex ---------------------------

def test_a_long_compaction_is_still_recognised_as_compacting():
    # _cf_line_is_compacting requires the phrase AND a live signature on the same line, and
    # that signature is this same timer — so the seconds-only pattern also made a compaction
    # past 60s invisible.
    assert daemon._cf_line_is_compacting("✻ Compacting conversation… (2m 14s)") is True
    assert daemon._cf_line_is_compacting("✻ Compacting conversation… (14s)") is True


def test_prose_about_compaction_is_still_not_a_compaction():
    # #101: text merely mentioning the footer must not read as live, and a completed row
    # carrying the phrase must not either.
    assert daemon._cf_line_is_compacting("we saw the Compacting conversation footer earlier") is False
    assert daemon._cf_line_is_compacting("  ⎿  Compacting conversation… (2m 14s)") is False


def test_the_progress_bar_still_stands_alone():
    # _CF_BAR_RE is independent of the timer; the bar shows before the timer appears.
    assert daemon._cf_line_is_busy("▰▰▱▱▱ compacting") is True
    assert daemon._cf_line_is_compacting("▰▰▱▱▱ Compacting conversation…") is True
