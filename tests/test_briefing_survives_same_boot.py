"""A claude revive must brief even when the boot id has not changed (#234).

`briefed_boot` exists to stop one revive chain stamping another chain's pane during a mass
restore (#133 r2). `_briefing_still_ours` was applying it to every engine, which overrode the
decision `revive_one` had already made two lines earlier and states in its own comment:

    # re-brief an existing claude pane (deliver_briefing self-guards on has_live_recv so it
    # won't double-arm); re-brief an existing codex pane only if it wasn't already briefed
    # this boot (crash-retry — codex has no recv to detect).

The consequence was invisible until a host stayed up long enough. Measured 2026-08-28 on topic
5935 with 4 weeks 1 day of uptime: the reopen resumed the session and then logged "already
briefed this boot — abandoning briefing", so nothing told the session why it had restarted. Its
only account of the restart became Claude Code's stock compaction preamble — "continued from a
previous conversation that ran out of context" — which was false, and is exactly the wrong
premise #167 exists to stop.
"""

import pytest

from bridge import daemon


# Synthetic on purpose. The first version of this line held the real kernel boot_id of the
# machine the test was written on, copied from /proc while checking the behaviour — a value
# from the author's host, in a tree whose whole premise is that it contains none. The
# sanitisation denylist could not catch it: you cannot list a value nobody knew was there.
BOOT = "00000000-0000-4000-8000-0000000b0071"


@pytest.fixture
def same_boot(monkeypatch):
    """A registry whose topic was briefed during THIS boot, with a live matching pane."""
    def _install(engine, *, pane="%290", live_recv=False):
        entry = {"pane": pane, "briefed_boot": BOOT, "engine": engine}
        monkeypatch.setattr(daemon, "read_registry", lambda: {"5935": entry})
        monkeypatch.setattr(daemon, "current_boot_id", lambda: BOOT)
        monkeypatch.setattr(daemon, "has_live_recv", lambda tid: live_recv)
        return entry
    return _install


def test_the_guard_lets_a_claude_briefing_through_on_a_host_that_has_not_rebooted(same_boot):
    """The regression itself: four weeks of uptime must not silence the cause.

    Named for what it checks. This calls `_briefing_still_ours` and pins its ANSWER; it does
    not drive a delivery, so it does not establish that a briefing was typed."""
    same_boot("claude")

    assert daemon._briefing_still_ours("%290", "5935", "claude", "delivery") is True, (
        "a claude revive dropped its briefing because the boot id had not changed since the "
        "last one — the session is left to invent its own explanation for the restart (#234)"
    )


def test_a_codex_pane_briefed_this_boot_is_not_briefed_again(same_boot):
    """Unchanged, and deliberately so: codex has no `recv` listener to detect a double-arm, so
    the boot stamp is the only crash-retry guard it has."""
    same_boot("codex")

    assert daemon._briefing_still_ours("%290", "5935", "codex", "delivery") is False


def test_a_codex_pane_from_an_earlier_boot_is_briefed(same_boot):
    """The stamp only suppresses within one boot; a real reboot must re-brief."""
    entry = same_boot("codex")
    entry["briefed_boot"] = "an-older-boot-id"

    assert daemon._briefing_still_ours("%290", "5935", "codex", "delivery") is True


def test_a_claude_pane_with_a_live_listener_is_left_alone(same_boot):
    """The guard claude actually relies on. This is what makes dropping the boot check safe:
    a session that already re-armed is not briefed again, detected rather than assumed."""
    same_boot("claude", live_recv=True)

    assert daemon._briefing_still_ours("%290", "5935", "claude", "delivery") is False


@pytest.mark.parametrize("engine", ["claude", "codex"])
def test_the_ownership_guard_still_rejects_a_pane_the_topic_has_left(same_boot, engine):
    """#133 r2, and it must survive this change: one chain typed topic A's briefing into %178
    and then stamped it on %999, which had received nothing.

    Note what this does and does NOT establish. It pins the GUARD's answer. It does not prove
    every delivery consults it: `deliver_briefing` skips this check when it did not wait —
    `attempt > 1 or await_busy or waited` — which the comment there justifies for the inline
    case, where re-reading the registry would race the binding write `revive_one` just handed
    it. That justification does not cover every first attempt: `_restore_targets_now` defers
    briefing with `brief=False` and delivers it later from a background thread, still at
    `attempt=1`. Either way a first-attempt briefing can reach a pane the topic has left.

    That hole is older than this branch and is filed as **#238**; claiming here that it cannot
    happen would be the same overclaim this branch exists to fix (#236 review r1 C8, r2 R4)."""
    same_boot(engine, pane="%999")

    assert daemon._briefing_still_ours("%178", "5935", engine, "delivery") is False
