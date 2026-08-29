"""#182 — a wait that elapses must say how long it waited.

The bare `(no reply within timeout)` was indistinguishable from an instant failure. A
session has no wall clock between its turns; the only duration signal it gets is how far
apart two records sit in its own transcript. For a LONG wait that signal is exactly
inverted — the longer the listener really waited, the fewer events happened meanwhile, so
the arm and the timeout land adjacent and read as "returned immediately". A live session
drew precisely that conclusion about its 24h listener, stopped re-arming on that basis,
and went dark for a day.

So the notice carries two facts the reader cannot otherwise recover: the elapsed duration,
and the wall-clock instant the wait was ARMED. Both are pinned below, and `armed_at` is
pinned against a fake clock — an implementation that stamps it at RETURN time would print
a perfectly plausible timestamp that is simply the wrong one, and no amount of eyeballing
the string would catch it.
"""

import os
import sys
import time
import types

import pytest

from bridge import cli


EPOCH = 1_700_000_000.0            # 2023-11-14T22:13:20Z, computed once and hard-coded
EPOCH_UTC = "2023-11-14T22:13:20Z"  # so a format change cannot silently agree with itself


class _Clock:
    """A clock that only moves when the code under test sleeps, so `waited` is exact."""

    def __init__(self, start=EPOCH):
        self.now = start

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def _patch_clock(monkeypatch, clock):
    monkeypatch.setattr(cli.time, "time", clock.time)
    monkeypatch.setattr(cli.time, "sleep", clock.sleep)


def _patch_inbox(monkeypatch, batches):
    """`read_new` yields each batch in turn, then [] forever. Cursor advances with it."""
    seq = iter(batches)

    def _read_new(topic_id, cursor):
        batch = next(seq, [])
        return (batch, cursor + len(batch)) if batch else ([], cursor)

    monkeypatch.setattr(cli, "load_cursor", lambda tid: 0)
    monkeypatch.setattr(cli, "read_new", _read_new)


# ---- _fmt_duration: the reader has to be able to check it against a wall clock ----

@pytest.mark.parametrize("seconds,expected", [
    (0, "0s"),
    (45, "45s"),
    (59, "59s"),
    (60, "1m 0s"),          # the unit boundary the seconds-only format hid
    (90, "1m 30s"),
    (3599, "59m 59s"),
    (3600, "1h 0m 0s"),     # the second boundary
    (86400, "24h 0m 0s"),   # the actual listener timeout
    (86399, "23h 59m 59s"),
    (2.4, "2s"),            # rounds rather than truncating toward zero
    (2.6, "3s"),
])
def test_duration_renders_in_the_largest_unit_that_applies(seconds, expected):
    assert cli._fmt_duration(seconds) == expected


# ---- the notice itself -------------------------------------------------------

def test_the_notice_states_both_the_elapsed_time_and_the_arm_instant():
    notice = cli.timeout_notice(EPOCH, 86400)
    assert "24h 0m 0s" in notice, "without the elapsed time a 24h wait reads as instant"
    assert EPOCH_UTC in notice, (
        "without the arm instant the reader cannot place the wait on a wall clock, so two "
        "consecutive timeouts are indistinguishable from one repeated instantly"
    )


def test_the_notice_is_still_recognisable_as_a_timeout():
    # Sessions and the daemon both key off this phrase; extending the line must not rename
    # the event. `bin/tg-bridge` callers grep for it and the skill docs quote it.
    assert cli.timeout_notice(EPOCH, 5).startswith("(no reply within timeout")


def test_the_arm_instant_is_rendered_in_utc():
    # A local-time stamp would be unreadable across the server/laptop split and would drift
    # with DST; gmtime is the only thing that lines up with the ISO timestamps in the inbox.
    #
    # This assertion is VACUOUS on a UTC host — localtime and gmtime agree there, so a
    # localtime implementation sails through (it did, in the first mutation run). Force an
    # offset with a POSIX TZ string (no tzdata needed), and assert the offset actually took
    # effect before trusting the result.
    old = os.environ.get("TZ")
    os.environ["TZ"] = "XXX-4"                      # POSIX sign is inverted: local = UTC+4
    time.tzset()
    try:
        notice = cli.timeout_notice(EPOCH, 5)
        local = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.localtime(EPOCH))
    finally:
        if old is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = old
        time.tzset()

    assert local != EPOCH_UTC, "TZ never took effect — this test proves nothing as written"
    assert EPOCH_UTC in notice
    assert local not in notice, "the arm instant was rendered in server-local time"


# ---- wait_for_messages: armed_at is the ARM, waited is the truth --------------

def test_a_timeout_reports_the_arm_instant_and_the_full_elapsed_wait(monkeypatch):
    clock = _Clock()
    _patch_clock(monkeypatch, clock)
    _patch_inbox(monkeypatch, [])                       # nothing ever arrives

    records, cursor, armed_at, waited = wait = cli.wait_for_messages(42, 90)

    assert len(wait) == 4, "the caller cannot report a wait it is not told about"
    assert records == []
    assert armed_at == EPOCH
    assert waited == 90


def test_an_early_reply_reports_the_time_actually_waited_not_the_timeout(monkeypatch):
    # The load-bearing case. On the timeout path `waited` and `timeout` coincide, so an
    # implementation that just echoes the timeout looks right there and is wrong here.
    clock = _Clock()
    _patch_clock(monkeypatch, clock)
    _patch_inbox(monkeypatch, [[], [], [{"text": "hi"}]])   # arrives on the 3rd poll

    records, cursor, armed_at, waited = cli.wait_for_messages(42, 86400)

    assert records == [{"text": "hi"}]
    assert waited == 2, "reported the timeout instead of the elapsed wait"
    assert armed_at == EPOCH, (
        "armed_at was stamped when the wait ENDED, not when it began — the notice would "
        "name an instant the listener was already finished at"
    )


def test_armed_at_is_the_arm_even_when_the_wait_is_long(monkeypatch):
    # Distinguishes arm-stamping from return-stamping by a margin no rounding can hide.
    clock = _Clock()
    _patch_clock(monkeypatch, clock)
    _patch_inbox(monkeypatch, [])

    _, _, armed_at, waited = cli.wait_for_messages(42, 86400)

    assert armed_at == EPOCH
    assert clock.now == EPOCH + 86400          # the clock really did move that far
    assert armed_at != clock.now


# ---- the two commands that print it ------------------------------------------

class _Captured:
    def __init__(self):
        self.text = ""

    def write(self, s):
        self.text += s
        return len(s)

    def flush(self):
        pass


def _capture(monkeypatch):
    out = _Captured()
    monkeypatch.setattr(sys, "stdout", out)
    return out


def test_recv_prints_the_elapsed_notice_and_still_exits_2(monkeypatch):
    monkeypatch.setattr(cli, "resolve_topic", lambda args: 42)
    monkeypatch.setattr(cli, "require_own_topic", lambda tid, cmd: None)
    monkeypatch.setattr(cli, "wait_for_messages",
                        lambda tid, timeout: ([], 0, EPOCH, 86400))
    out = _capture(monkeypatch)

    args = types.SimpleNamespace(topic="42", wait=86400, peek=False, json=False)
    with pytest.raises(SystemExit) as exc:
        cli.cmd_recv({}, args)

    assert exc.value.code == 2, "exit 2 is how a session tells a timeout from a failure"
    assert "24h 0m 0s" in out.text
    assert EPOCH_UTC in out.text


def test_ask_prints_the_elapsed_notice_and_still_exits_2(monkeypatch):
    monkeypatch.setattr(cli, "resolve_topic", lambda args: 42)
    monkeypatch.setattr(cli, "require_own_topic", lambda tid, cmd: None)
    monkeypatch.setattr(cli, "load_cursor", lambda tid: 0)
    monkeypatch.setattr(cli, "read_new", lambda tid, cursor: ([], cursor))
    monkeypatch.setattr(cli, "drain_and_commit", lambda *a: None)
    monkeypatch.setattr(cli, "send_text", lambda cfg, tid, text: None)
    monkeypatch.setattr(cli, "wait_for_messages",
                        lambda tid, timeout: ([], 0, EPOCH, 900))
    out = _capture(monkeypatch)

    args = types.SimpleNamespace(topic="42", text="q", timeout=900, json=False)
    with pytest.raises(SystemExit) as exc:
        cli.cmd_ask({}, args)

    assert exc.value.code == 2
    assert "15m 0s" in out.text
    assert EPOCH_UTC in out.text


def test_a_wait_that_delivers_prints_no_timeout_notice(monkeypatch):
    # The notice must stay on the empty path: printed unconditionally it would sit in the
    # middle of a delivered message and every session would read a real reply as a timeout.
    monkeypatch.setattr(cli, "resolve_topic", lambda args: 42)
    monkeypatch.setattr(cli, "require_own_topic", lambda tid, cmd: None)
    monkeypatch.setattr(cli, "wait_for_messages",
                        lambda tid, timeout: ([{"text": "hi"}], 1, EPOCH, 3))
    monkeypatch.setattr(cli, "drain_and_commit", lambda *a: None)
    monkeypatch.setattr(cli, "send_typing", lambda cfg, tid: None)
    out = _capture(monkeypatch)

    args = types.SimpleNamespace(topic="42", wait=86400, peek=False, json=False)
    cli.cmd_recv({}, args)          # must NOT raise SystemExit

    assert "no reply within timeout" not in out.text


def test_recv_without_wait_never_reports_a_wait_it_did_not_do(monkeypatch):
    # The non-wait branch leaves armed_at/waited unset; formatting them would crash on
    # None, so this pins that the notice is unreachable there.
    monkeypatch.setattr(cli, "resolve_topic", lambda args: 42)
    monkeypatch.setattr(cli, "require_own_topic", lambda tid, cmd: None)
    monkeypatch.setattr(cli, "load_cursor", lambda tid: 0)
    monkeypatch.setattr(cli, "read_new", lambda tid, cursor: ([], cursor))
    out = _capture(monkeypatch)

    args = types.SimpleNamespace(topic="42", wait=None, peek=False, json=False)
    cli.cmd_recv({}, args)          # no output, no exit

    assert out.text == ""
