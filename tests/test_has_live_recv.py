"""#186 — `has_live_recv` decides whether the self-heal sweep types into a live pane.

It appeared in the suite only as a monkeypatched stub, so its own logic never ran. Three
behaviours carry consequences, and each fails in a different direction:

  * the boundary match — `pgrep -af "tg-bridge recv --topic 6"` matches `--topic 606` as a
    plain substring, so a prefix collision would report a listener that belongs to another
    topic, and the sweep would never heal the session that actually went dark;
  * errors return None so callers err toward "not dark" — returning False instead turns one
    pgrep failure into a fleet-wide dead-listener verdict and nudges every claude pane;
  * a genuine no-match returns False, not None. The call site gates on `recv is False`, so
    the two are not interchangeable: None there disables the healing entirely.

The subprocess is stubbed rather than spawned. What is under test is the parse — pgrep's
own matching is deliberately loose, and the filter that tightens it is the thing that broke.
"""

import types

import pytest

from bridge import daemon


def _pgrep(monkeypatch, stdout, boom=None):
    """Stand in for the pgrep call with a canned process table."""
    calls = []

    def _run(cmd, **kwargs):
        calls.append(cmd)
        if boom:
            raise boom
        return types.SimpleNamespace(stdout=stdout, stderr="", returncode=0)

    monkeypatch.setattr(daemon.subprocess, "run", _run)
    return calls


# A real listener line, as the process table actually renders it: the harness wraps the
# command in `bash -c ... eval '<cmd>' < /dev/null && pwd -P >| /tmp/...`.
WRAPPED = ("1018971 /bin/bash -c source /home/user/.claude/shell-snapshots/snap.sh "
           "&& eval 'tg-bridge recv --topic {tid} --wait 86400' < /dev/null && pwd -P")
BARE = "830538 /home/user/bin/tg-bridge recv --topic {tid} --wait 86400"


def test_a_live_listener_is_found_through_the_shell_wrapper(monkeypatch):
    _pgrep(monkeypatch, WRAPPED.format(tid=4367) + "\n")
    assert daemon.has_live_recv(4367) is True


def test_a_live_listener_is_found_when_invoked_by_absolute_path(monkeypatch):
    _pgrep(monkeypatch, BARE.format(tid=6258) + "\n")
    assert daemon.has_live_recv(6258) is True


def test_no_listener_returns_false_not_none(monkeypatch):
    # `idle_sweep_loop` gates on `recv is False`. None here would silently disable the
    # dead-listener branch for every topic — the sweep would stop healing anything.
    _pgrep(monkeypatch, "")
    assert daemon.has_live_recv(4367) is False


# ---- the boundary match ------------------------------------------------------

@pytest.mark.parametrize("running,asked", [
    (606, 6),        # the case named in the docstring
    (60, 6),
    (12999, 129),
    (13130, 1313),
    (1902, 190),
])
def test_a_longer_topic_id_is_not_mistaken_for_a_shorter_one(monkeypatch, running, asked):
    # pgrep's own pattern is a substring match, so it hands back the 606 line when asked
    # about 6. If the filter stops tightening that, topic 6 reads as "has a listener" and
    # the sweep never heals it — silently, and only for topics whose id is a prefix of a
    # live one.
    _pgrep(monkeypatch, WRAPPED.format(tid=running) + "\n")
    assert daemon.has_live_recv(asked) is False


@pytest.mark.parametrize("tid", [6, 60, 606])
def test_each_id_still_finds_its_own_listener(monkeypatch, tid):
    # The mirror of the above: tightening the match must not overshoot into never matching.
    _pgrep(monkeypatch, WRAPPED.format(tid=tid) + "\n")
    assert daemon.has_live_recv(tid) is True


def test_the_right_listener_is_found_among_several(monkeypatch):
    _pgrep(monkeypatch, "\n".join([
        WRAPPED.format(tid=606),
        WRAPPED.format(tid=60),
        BARE.format(tid=6),
    ]) + "\n")
    assert daemon.has_live_recv(6) is True


def test_a_prefix_sibling_alone_does_not_answer_for_the_topic(monkeypatch):
    _pgrep(monkeypatch, "\n".join([
        WRAPPED.format(tid=606),
        WRAPPED.format(tid=60),
    ]) + "\n")
    assert daemon.has_live_recv(6) is False


def test_the_id_is_matched_at_its_start_too(monkeypatch):
    # 606's listener must not answer for 06 or for a trailing-substring lookalike.
    _pgrep(monkeypatch, WRAPPED.format(tid=606) + "\n")
    assert daemon.has_live_recv(606) is True
    assert daemon.has_live_recv(60) is False


# ---- failure direction -------------------------------------------------------

def test_an_error_returns_none_so_callers_err_toward_not_dark(monkeypatch):
    # False here would read as "no listener anywhere" and, with idle panes, hand the sweep a
    # dead-listener verdict for the whole fleet at once — one pgrep failure, every pane
    # nudged. None is what keeps a broken probe from becoming a broadcast.
    _pgrep(monkeypatch, "", boom=OSError("pgrep: cannot fork"))
    assert daemon.has_live_recv(4367) is None


def test_the_probe_asks_pgrep_for_the_full_command_line(monkeypatch):
    # Without -f, pgrep matches only the process NAME ("bash", "python3"), so every lookup
    # returns nothing and every topic reads as dark.
    calls = _pgrep(monkeypatch, "")
    daemon.has_live_recv(4367)
    assert calls, "has_live_recv never invoked pgrep"
    cmd = calls[0]
    assert cmd[0] == "pgrep"
    assert "-af" in cmd or ("-a" in cmd and "-f" in cmd)
    assert any("4367" in part for part in cmd), "the topic id never reached the probe"
