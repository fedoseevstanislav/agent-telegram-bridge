"""#254: one swallowed nudge is not news; a pane that keeps swallowing is.

`report_blocked_pane` used to fire on the FIRST swallowed injection. #250's logging then
measured what a swallow usually is — the pane rendered the text only after the first decision,
and the next sweep tick delivered it. So the owner was told the session was unreachable while
the message was in fact about to arrive.

The `failed` route already waited for `UNREADABLE_ESCALATE_AFTER` consecutive failures before
escalating. These tests pin the same shape for `swallowed`, using the streak `type_line`
already keeps — and pin that a genuinely stuck pane is still reported.
"""

import pytest

from bridge import daemon


PANE = "%21"
TOPIC = 4112


@pytest.fixture(autouse=True)
def _clean():
    for table in (daemon._swallowed_streak, daemon._blocked_reported):
        table.clear()
    yield
    for table in (daemon._swallowed_streak, daemon._blocked_reported):
        table.clear()


def test_a_first_swallow_is_not_reported():
    """H1. The recoverable case, which is the common one."""
    daemon._swallowed_streak[PANE] = (1, 0.0)
    assert not daemon.pane_is_persistently_swallowing(PANE)


def test_a_pane_at_the_cap_is_reported():
    """H2. Two consecutive unverified injections is a pane that is actually stuck — the
    alarm still fires, one sweep tick later than it used to."""
    daemon._swallowed_streak[PANE] = (daemon.SWALLOW_MAX_ATTEMPTS, 0.0)
    assert daemon.pane_is_persistently_swallowing(PANE)


def test_a_pane_nobody_has_failed_on_is_not_reported():
    """H3. No streak entry at all — a pane whose last injection SUCCEEDED, since type_line
    clears the entry on a send. An isolated swallow can never accumulate into a report."""
    assert not daemon.pane_is_persistently_swallowing(PANE)


def test_the_nudge_path_stays_quiet_on_a_first_swallow(monkeypatch):
    """H4. End to end through maybe_nudge: swallowed once, nothing reaches the topic."""
    reports = []
    monkeypatch.setattr(daemon, "type_line", lambda *a, **k: "swallowed")
    monkeypatch.setattr(daemon, "pane_alive", lambda *_a: True)
    monkeypatch.setattr(daemon, "unread_count", lambda *_a: 1)
    monkeypatch.setattr(daemon, "report_blocked_pane",
                        lambda *a, **k: reports.append(a) or True)

    daemon._swallowed_streak[PANE] = (1, 0.0)
    daemon.maybe_nudge(TOPIC, PANE)
    assert reports == []

    daemon._swallowed_streak[PANE] = (daemon.SWALLOW_MAX_ATTEMPTS, 0.0)
    daemon.maybe_nudge(TOPIC, PANE)
    assert len(reports) == 1        # and the stuck pane is still reported
