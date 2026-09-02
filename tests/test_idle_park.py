"""Park sessions idle past the threshold; a message revives them (#273).

The host hits OOM: each claude process holds ~half a GB, and sessions sit idle for
days-to-weeks. Parking past the cache TTL costs nothing but the RAM being reclaimed —
the next message re-reads the context either way, and the existing auto-revive brings
the same conversation back.

Round-2 shape (#274): every authority fence runs FRESH after the deliberate recheck
sleep and again under the claim; ownership is a unique token, not a timestamp; `parked`
lands inside the claim. The tests here include the reviewer's five r2 interleavings.
"""

import os
import time
import types

import pytest

from bridge import common, daemon


@pytest.fixture
def registry():
    def _seed(entries):
        common.update_registry(lambda reg: (reg.clear(), reg.update(entries)))
    yield _seed
    common.update_registry(lambda reg: reg.clear())


def _one_sweep(monkeypatch, on_recheck=None):
    """Run exactly one idle_park_loop iteration. Only the POLL sleep ends the loop; the
    in-park recheck sleep passes through — optionally firing `on_recheck`, which is how
    the r2 interleavings inject events INTO the deliberate delay."""
    calls = {"n": 0}

    def _sleep(secs):
        if secs != daemon.IDLE_PARK_POLL:
            if on_recheck is not None:
                on_recheck()
            return
        calls["n"] += 1
        if calls["n"] > 1:
            raise KeyboardInterrupt

    monkeypatch.setattr(daemon.time, "sleep", _sleep)


@pytest.fixture
def park_env(registry, monkeypatch):
    """A parkable world: live idle pane, ancient transcript, stable old occupant,
    tmux recorded not run."""
    tmux_calls = []

    def _tmux(argv, **kw):
        tmux_calls.append(argv)
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(daemon, "_tmux", _tmux)
    monkeypatch.setattr(daemon, "pane_alive", lambda p: True)
    monkeypatch.setattr(daemon, "pane_is_idle", lambda p: True)
    monkeypatch.setattr(daemon, "engine_of_pane", lambda p: "claude")
    monkeypatch.setattr(daemon, "_pane_hosts_session", lambda p, i: True)
    occupant = (4242, time.time() - 8 * 3600)  # older than the idleness; STABLE across
    monkeypatch.setattr(daemon, "_pane_occupant", lambda p: occupant)  # repeated looks
    monkeypatch.setattr(daemon, "_session_last_activity",
                        lambda info: time.time() - 7 * 3600)  # 7h > 6h default
    monkeypatch.setattr(daemon, "unread_count", lambda tid: 0)
    monkeypatch.setattr(daemon, "recent_inbox_drop", lambda tid, now, window=None: None)
    replies = []
    monkeypatch.setattr(daemon, "reply", lambda cfg, tid, t: replies.append((tid, t)) or True)
    return tmux_calls, replies


def test_an_idle_session_is_parked(registry, park_env, monkeypatch):
    """C1: kill exactly the pane, stamp ended+parked, release the claim, notify —
    topic left open."""
    tmux_calls, replies = park_env
    registry({"5935": {"pane": "%1", "name": "x", "session_id": "sid"}})
    _one_sweep(monkeypatch)

    with pytest.raises(KeyboardInterrupt):
        daemon.idle_park_loop({"bot_token": "t", "chat_id": 1})

    kills = [c for c in tmux_calls if "kill-pane" in c or "kill-session" in c]
    assert kills == [["tmux", "kill-pane", "-t", "%1"]], (
        "not surgical (C4): the kill must be the exact examined pane, never the "
        "enclosing session (#274 r1, finding 1)"
    )
    entry = common.read_registry()["5935"]
    assert entry.get("ended"), "not stamped ended"
    assert entry.get("parked") is True
    assert "park_claim" not in entry, "the claim token must be released after the kill"
    assert entry.get("session_id") == "sid", "the resume handle must survive (C3)"
    assert len(replies) == 1 and "Parked" in replies[0][1]
    assert not any("closeForumTopic" in str(c) for c in tmux_calls)


def test_a_parked_entry_is_revivable(registry, park_env, monkeypatch):
    """C3: after the park, the existing message path would revive it."""
    registry({"5935": {"pane": "%1", "name": "x", "session_id": "sid"}})
    _one_sweep(monkeypatch)
    with pytest.raises(KeyboardInterrupt):
        daemon.idle_park_loop({"bot_token": "t", "chat_id": 1})

    assert daemon.should_auto_revive(common.read_registry()["5935"])


@pytest.mark.parametrize("why,mutate", [
    ("young transcript", lambda mp, reg: mp.setattr(
        daemon, "_session_last_activity", lambda info: time.time() - 60)),
    ("mid-turn pane", lambda mp, reg: mp.setattr(
        daemon, "pane_is_idle", lambda p: False)),
    ("exempted topic", lambda mp, reg: mp.setattr(
        daemon, "load_park_exempt", lambda: {"5935"})),
    ("feed topic", lambda mp, reg: common.update_registry(
        lambda r: r["5935"].__setitem__("feed", True))),
    ("dead pane", lambda mp, reg: mp.setattr(daemon, "pane_alive", lambda p: False)),
    ("unageable session", lambda mp, reg: mp.setattr(
        daemon, "_session_last_activity", lambda info: None)),
    ("already ended", lambda mp, reg: common.update_registry(
        lambda r: r["5935"].__setitem__("ended", "2026-01-01T00:00:00+0000"))),
    ("active carry-forward", lambda mp, reg: mp.setattr(
        daemon, "carry_forward_active", lambda tid: True)),
    ("unread message waiting", lambda mp, reg: mp.setattr(
        daemon, "unread_count", lambda tid: 1)),
    ("engine mismatch (recycled pane)", lambda mp, reg: mp.setattr(
        daemon, "engine_of_pane", lambda p: "codex")),
    ("pane does not host the session", lambda mp, reg: mp.setattr(
        daemon, "_pane_hosts_session", lambda p, i: False)),
    ("busy on the second idle look", lambda mp, reg: mp.setattr(
        daemon, "pane_is_idle",
        lambda p, _seen=iter([True, False]): next(_seen, False))),
    ("occupant unresolvable", lambda mp, reg: mp.setattr(
        daemon, "_pane_occupant", lambda p: None)),
    ("occupant younger than the idleness", lambda mp, reg: mp.setattr(
        daemon, "_pane_occupant", lambda p, _o=(4242, time.time() - 60): _o)),
    ("stale claim token on the entry", lambda mp, reg: common.update_registry(
        lambda r: r["5935"].__setitem__("park_claim", "deadbeef"))),
    ("message drained by a dying listener (r4 f3)", lambda mp, reg: mp.setattr(
        daemon, "recent_inbox_drop",
        lambda tid, now, window=None: {"from": "S", "text": "hi"})),
    ("exemption file unreadable (r4 f4)", lambda mp, reg: mp.setattr(
        daemon, "load_park_exempt", lambda: None)),
])
def test_never_parked_when(registry, park_env, monkeypatch, why, mutate):
    """C2: every gate refuses on its own."""
    tmux_calls, replies = park_env
    registry({"5935": {"pane": "%1", "name": "x", "session_id": "sid"}})
    mutate(monkeypatch, None)
    _one_sweep(monkeypatch)

    with pytest.raises(KeyboardInterrupt):
        daemon.idle_park_loop({"bot_token": "t", "chat_id": 1})

    assert not any("kill-pane" in c or "kill-session" in c for c in tmux_calls), why
    assert replies == [], why


# ---- The r2 interleavings: events landing INSIDE the deliberate recheck sleep ----

def test_an_occupant_change_during_the_recheck_is_refused(registry, park_env, monkeypatch):
    """#274 r2, finding 1 / r3, finding 1: the pane's FOREGROUND occupant changes while
    the sweep sleeps — including the documented shell-hosted case where #{pane_pid} is a
    persistent shell and only the foreground group changes underneath it. The post-sleep
    battery re-resolves the occupant and refuses."""
    tmux_calls, replies = park_env
    registry({"5935": {"pane": "%1", "name": "x", "session_id": "sid"}})
    old = (100, time.time() - 8 * 3600)
    new_fg = (200, time.time() - 9 * 3600)  # same shell, new foreground group — old
    occupants = iter([old])                 # enough to predate the transcript too
    monkeypatch.setattr(daemon, "_pane_occupant", lambda p: next(occupants, new_fg))
    _one_sweep(monkeypatch)

    with pytest.raises(KeyboardInterrupt):
        daemon.idle_park_loop({"bot_token": "t", "chat_id": 1})

    assert not any("kill-pane" in c for c in tmux_calls), (
        "killed a pane whose foreground occupant changed (#274 r2 f1 / r3 f1)"
    )
    assert "ended" not in common.read_registry()["5935"]


def test_an_exemption_added_during_the_recheck_is_honored(registry, park_env, monkeypatch):
    """#274 r3, finding 3: the operator exempts the topic while the sweep sleeps. The
    battery re-reads the exemption file itself, never the sweep's snapshot."""
    tmux_calls, replies = park_env
    registry({"5935": {"pane": "%1", "name": "x", "session_id": "sid"}})
    exempt = {"now": set()}
    monkeypatch.setattr(daemon, "load_park_exempt", lambda: set(exempt["now"]))
    _one_sweep(monkeypatch, on_recheck=lambda: exempt.__setitem__("now", {"5935"}))

    with pytest.raises(KeyboardInterrupt):
        daemon.idle_park_loop({"bot_token": "t", "chat_id": 1})

    assert not any("kill-pane" in c for c in tmux_calls), (
        "killed a topic the operator had just exempted (#274 r3, finding 3)"
    )
    assert "ended" not in common.read_registry()["5935"]


def test_an_exempt_file_turning_unreadable_during_the_recheck_fails_closed(
        registry, park_env, monkeypatch):
    """#274 r4, finding 4, isolated to the BATTERY: the file is readable at the sweep
    screen and turns unreadable during the sleep — the battery must fail closed on its
    own, not ride the sweep's earlier read."""
    tmux_calls, _ = park_env
    registry({"5935": {"pane": "%1", "name": "x", "session_id": "sid"}})
    state = {"exempt": set()}
    monkeypatch.setattr(daemon, "load_park_exempt", lambda: state["exempt"])
    _one_sweep(monkeypatch, on_recheck=lambda: state.__setitem__("exempt", None))

    with pytest.raises(KeyboardInterrupt):
        daemon.idle_park_loop({"bot_token": "t", "chat_id": 1})

    assert not any("kill-pane" in c for c in tmux_calls), (
        "an unreadable exemption authority was treated as empty (#274 r4, finding 4)"
    )
    assert "ended" not in common.read_registry()["5935"]


def test_a_message_that_beats_the_kill_is_repaired_by_an_immediate_revive(
        registry, park_env, monkeypatch):
    """#274 r3, finding 2: no battery is atomic against the kill. A message that lands
    after its own gate passed loses the race — so _park_one re-checks AFTER the kill and
    revives on the spot instead of stranding the topic until a second message."""
    tmux_calls, replies = park_env
    registry({"5935": {"pane": "%1", "name": "x", "session_id": "sid"}})
    inbox = {"n": 0}
    monkeypatch.setattr(daemon, "unread_count", lambda tid: inbox["n"])
    revived = []
    monkeypatch.setattr(daemon, "maybe_auto_revive",
                        lambda cfg, tid, cause="auto": revived.append(str(tid)))

    def _tmux(argv, **kw):
        tmux_calls.append(argv)
        if argv[:2] == ["tmux", "kill-pane"]:
            inbox["n"] = 1  # the message was in flight; it lost the race by a hair
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(daemon, "_tmux", _tmux)
    _one_sweep(monkeypatch)

    with pytest.raises(KeyboardInterrupt):
        daemon.idle_park_loop({"bot_token": "t", "chat_id": 1})

    assert any("kill-pane" in c for c in tmux_calls)  # the race WAS lost
    assert revived == ["5935"], (
        "a message that beat the kill left the topic stranded (#274 r3, finding 2)"
    )


def test_the_repair_runs_even_when_the_park_notice_fails(registry, park_env, monkeypatch):
    """#274 r4, finding 1: nothing between the kill and the repair may skip the repair —
    every post-kill step is individually guarded."""
    tmux_calls, _ = park_env
    registry({"5935": {"pane": "%1", "name": "x", "session_id": "sid"}})
    inbox = {"n": 0}
    monkeypatch.setattr(daemon, "unread_count", lambda tid: inbox["n"])
    monkeypatch.setattr(daemon, "reply",
                        lambda cfg, tid, t: (_ for _ in ()).throw(OSError("telegram down")))
    revived = []
    monkeypatch.setattr(daemon, "maybe_auto_revive",
                        lambda cfg, tid, cause="auto": revived.append(str(tid)))

    def _tmux(argv, **kw):
        tmux_calls.append(argv)
        if argv[:2] == ["tmux", "kill-pane"]:
            inbox["n"] = 1
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(daemon, "_tmux", _tmux)
    _one_sweep(monkeypatch)

    with pytest.raises(KeyboardInterrupt):
        daemon.idle_park_loop({"bot_token": "t", "chat_id": 1})

    assert revived == ["5935"], (
        "a failed park notice skipped the repair for a kill that already happened "
        "(#274 r4, finding 1)"
    )


def test_an_unreadable_authority_counts_as_a_lost_race(registry, park_env, monkeypatch):
    """#274 r4, finding 1: each repair authority is sampled in its own guard, and a read
    failure means the race cannot be proven won — repair, don't assume."""
    tmux_calls, _ = park_env
    registry({"5935": {"pane": "%1", "name": "x", "session_id": "sid"}})
    state = {"killed": False}
    monkeypatch.setattr(daemon, "unread_count",
                        lambda tid: (_ for _ in ()).throw(OSError("inbox unreadable"))
                        if state["killed"] else 0)
    revived = []
    monkeypatch.setattr(daemon, "maybe_auto_revive",
                        lambda cfg, tid, cause="auto": revived.append(str(tid)))

    def _tmux(argv, **kw):
        tmux_calls.append(argv)
        if argv[:2] == ["tmux", "kill-pane"]:
            state["killed"] = True
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(daemon, "_tmux", _tmux)
    _one_sweep(monkeypatch)

    with pytest.raises(KeyboardInterrupt):
        daemon.idle_park_loop({"bot_token": "t", "chat_id": 1})

    assert revived == ["5935"], (
        "an unreadable inbox was treated as a won race (#274 r4, finding 1)"
    )


def test_a_drained_message_is_reappended_before_the_revive(registry, park_env, monkeypatch):
    """#274 r4, finding 3: a message the dying recv drained (cursor advanced, invisible
    to unread_count) is re-appended to the inbox so the revive replays it — a duplicate
    that says so beats a silent lost turn."""
    tmux_calls, _ = park_env
    registry({"5935": {"pane": "%1", "name": "x", "session_id": "sid"}})
    state = {"killed": False}
    rec = {"from": "S", "text": "the message that was drained", "message_id": 7}
    monkeypatch.setattr(daemon, "recent_inbox_drop",
                        lambda tid, now, window=None: rec if state["killed"] else None)
    appended = []
    monkeypatch.setattr(daemon, "append_jsonl",
                        lambda path, record: appended.append((path, record)))
    revived = []
    monkeypatch.setattr(daemon, "maybe_auto_revive",
                        lambda cfg, tid, cause="auto": revived.append(str(tid)))

    def _tmux(argv, **kw):
        tmux_calls.append(argv)
        if argv[:2] == ["tmux", "kill-pane"]:
            state["killed"] = True
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(daemon, "_tmux", _tmux)
    _one_sweep(monkeypatch)

    with pytest.raises(KeyboardInterrupt):
        daemon.idle_park_loop({"bot_token": "t", "chat_id": 1})

    assert revived == ["5935"]
    (path, record), = appended
    assert path.endswith("topics/5935/inbox.jsonl")
    assert "the message that was drained" in record["text"]
    assert "re-delivery" in record["text"], (
        "a replayed message must say it may be a duplicate (#274 r4, finding 3)"
    )


def test_a_repair_the_revive_machinery_declines_still_tells_the_owner(
        registry, park_env, monkeypatch):
    """#274 r4, finding 2: maybe_auto_revive may legitimately do nothing (a pending
    choice, an undeliverable question). When nothing durable is in flight the repair
    says so in the topic — the notice is the recovery path the owner can always see."""
    tmux_calls, replies = park_env
    registry({"5935": {"pane": "%1", "name": "x", "session_id": "sid"}})
    inbox = {"n": 0}
    monkeypatch.setattr(daemon, "unread_count", lambda tid: inbox["n"])
    monkeypatch.setattr(daemon, "maybe_auto_revive",
                        lambda cfg, tid, cause="auto": None)  # declines silently

    def _tmux(argv, **kw):
        tmux_calls.append(argv)
        if argv[:2] == ["tmux", "kill-pane"]:
            inbox["n"] = 1
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(daemon, "_tmux", _tmux)
    _one_sweep(monkeypatch)

    with pytest.raises(KeyboardInterrupt):
        daemon.idle_park_loop({"bot_token": "t", "chat_id": 1})

    warnings = [t for _tid, t in replies if "revive did not come up" in t]
    assert warnings, (
        "the revive declined and the owner was never told (#274 r4, finding 2)"
    )


def test_a_repair_with_a_real_revive_does_not_warn(registry, park_env, monkeypatch):
    """The inverse pin: when the revive actually claims the topic, no scary notice."""
    tmux_calls, replies = park_env
    registry({"5935": {"pane": "%1", "name": "x", "session_id": "sid"}})
    inbox = {"n": 0}
    monkeypatch.setattr(daemon, "unread_count", lambda tid: inbox["n"])

    def _revive(cfg, tid, cause="auto"):
        common.update_registry(lambda r: (r["5935"].pop("ended", None),
                                          r["5935"].pop("parked", None)))
    monkeypatch.setattr(daemon, "maybe_auto_revive", _revive)

    def _tmux(argv, **kw):
        tmux_calls.append(argv)
        if argv[:2] == ["tmux", "kill-pane"]:
            inbox["n"] = 1
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(daemon, "_tmux", _tmux)
    _one_sweep(monkeypatch)

    with pytest.raises(KeyboardInterrupt):
        daemon.idle_park_loop({"bot_token": "t", "chat_id": 1})

    assert not any("parked in the same instant" in t for _tid, t in replies)


def test_the_repair_runs_even_when_log_itself_raises(registry, park_env, monkeypatch):
    """#274 r5, finding 1: `log` writes to stderr and can raise; after a successful kill
    even that must not skip the repair — the repair sits in a finally."""
    tmux_calls, _ = park_env
    registry({"5935": {"pane": "%1", "name": "x", "session_id": "sid"}})
    state = {"killed": False}
    inbox = {"n": 0}
    monkeypatch.setattr(daemon, "unread_count", lambda tid: inbox["n"])
    real_log = daemon.log
    monkeypatch.setattr(daemon, "log",
                        lambda msg: (_ for _ in ()).throw(OSError("stderr closed"))
                        if state["killed"] else real_log(msg))
    revived = []
    monkeypatch.setattr(daemon, "maybe_auto_revive",
                        lambda cfg, tid, cause="auto": revived.append(str(tid)))

    def _tmux(argv, **kw):
        tmux_calls.append(argv)
        if argv[:2] == ["tmux", "kill-pane"]:
            state["killed"] = True
            inbox["n"] = 1
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(daemon, "_tmux", _tmux)
    _one_sweep(monkeypatch)

    with pytest.raises(KeyboardInterrupt):
        daemon.idle_park_loop({"bot_token": "t", "chat_id": 1})

    assert revived == ["5935"], (
        "a raising log skipped the repair after a successful kill (#274 r5, finding 1)"
    )


def test_a_failed_replay_append_forces_the_resend_notice(registry, park_env, monkeypatch):
    """#274 r5, finding 2: when the re-delivery append fails, the drained record stays
    behind the cursor and nothing will replay it — even a successful revive must not
    silence the notice, and the notice must ask for a resend."""
    tmux_calls, replies = park_env
    registry({"5935": {"pane": "%1", "name": "x", "session_id": "sid"}})
    state = {"killed": False}
    rec = {"from": "S", "text": "drained", "message_id": 7}
    monkeypatch.setattr(daemon, "recent_inbox_drop",
                        lambda tid, now, window=None: rec if state["killed"] else None)
    monkeypatch.setattr(daemon, "append_jsonl",
                        lambda path, record: (_ for _ in ()).throw(OSError("disk full")))

    def _revive(cfg, tid, cause="auto"):  # the revive SUCCEEDS
        common.update_registry(lambda r: (r["5935"].pop("ended", None),
                                          r["5935"].pop("parked", None)))
    monkeypatch.setattr(daemon, "maybe_auto_revive", _revive)

    def _tmux(argv, **kw):
        tmux_calls.append(argv)
        if argv[:2] == ["tmux", "kill-pane"]:
            state["killed"] = True
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(daemon, "_tmux", _tmux)
    _one_sweep(monkeypatch)

    with pytest.raises(KeyboardInterrupt):
        daemon.idle_park_loop({"bot_token": "t", "chat_id": 1})

    resends = [t for _tid, t in replies if "resend" in t]
    assert resends, (
        "a failed replay append was silenced by the successful revive — lost turn "
        "(#274 r5, finding 2)"
    )


def test_durability_is_an_outcome_not_the_in_flight_marker(registry, park_env, monkeypatch):
    """#274 r5, finding 3: a transient _auto_reviving marker is not durable — only a
    live entry or a pending question is. A revive 'in flight' that never lands must
    still produce the owner notice."""
    tmux_calls, replies = park_env
    registry({"5935": {"pane": "%1", "name": "x", "session_id": "sid"}})
    inbox = {"n": 0}
    monkeypatch.setattr(daemon, "unread_count", lambda tid: inbox["n"])

    def _revive_that_dies(cfg, tid, cause="auto"):
        # simulate the worker being active at snapshot time then failing: the marker
        # is set and cleared, the entry stays ended
        with daemon._auto_revive_lock:
            daemon._auto_reviving.add(str(tid))
        with daemon._auto_revive_lock:
            daemon._auto_reviving.discard(str(tid))
    monkeypatch.setattr(daemon, "maybe_auto_revive", _revive_that_dies)

    def _tmux(argv, **kw):
        tmux_calls.append(argv)
        if argv[:2] == ["tmux", "kill-pane"]:
            inbox["n"] = 1
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(daemon, "_tmux", _tmux)
    _one_sweep(monkeypatch)

    with pytest.raises(KeyboardInterrupt):
        daemon.idle_park_loop({"bot_token": "t", "chat_id": 1})

    assert any("revive did not come up" in t for _tid, t in replies), (
        "a transient in-flight marker was treated as a durable outcome "
        "(#274 r5, finding 3)"
    )


def test_a_prepared_reopen_record_is_not_durable(registry, park_env, monkeypatch):
    """#274 r6, finding 1: a `prepared` reopen record is unanswerable and blocks every
    future auto-revive until restart — the repair must not call it durable; it clears
    the stuck record (so the next message re-asks) and warns the owner."""
    tmux_calls, replies = park_env
    registry({"5935": {"pane": "%1", "name": "x", "session_id": "sid"}})
    inbox = {"n": 0}
    monkeypatch.setattr(daemon, "unread_count", lambda tid: inbox["n"])

    def _revive_stuck_prepared(cfg, tid, cause="auto"):
        with daemon._pending_reopen_lock:
            daemon.pending_reopens[str(tid)] = {"entry": {}, "tokens": 1,
                                                "state": "prepared"}
    monkeypatch.setattr(daemon, "maybe_auto_revive", _revive_stuck_prepared)

    def _tmux(argv, **kw):
        tmux_calls.append(argv)
        if argv[:2] == ["tmux", "kill-pane"]:
            inbox["n"] = 1
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(daemon, "_tmux", _tmux)
    _one_sweep(monkeypatch)

    try:
        with pytest.raises(KeyboardInterrupt):
            daemon.idle_park_loop({"bot_token": "t", "chat_id": 1})

        assert any("revive did not come up" in t for _tid, t in replies), (
            "an unanswerable prepared record was treated as durable (#274 r6, f1)"
        )
        with daemon._pending_reopen_lock:
            assert "5935" not in daemon.pending_reopens, (
                "the stuck prepared record was left to block every future revive"
            )
    finally:
        with daemon._pending_reopen_lock:
            daemon.pending_reopens.pop("5935", None)


def test_a_delivered_reopen_question_is_durable(registry, park_env, monkeypatch):
    """The inverse pin: an answerable delivered question IS the ask-first durable
    outcome — no warning, record kept."""
    tmux_calls, replies = park_env
    registry({"5935": {"pane": "%1", "name": "x", "session_id": "sid"}})
    inbox = {"n": 0}
    monkeypatch.setattr(daemon, "unread_count", lambda tid: inbox["n"])

    def _revive_asks(cfg, tid, cause="auto"):
        with daemon._pending_reopen_lock:
            daemon.pending_reopens[str(tid)] = {"entry": {}, "tokens": 1,
                                                "state": "delivered"}
    monkeypatch.setattr(daemon, "maybe_auto_revive", _revive_asks)

    def _tmux(argv, **kw):
        tmux_calls.append(argv)
        if argv[:2] == ["tmux", "kill-pane"]:
            inbox["n"] = 1
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(daemon, "_tmux", _tmux)
    _one_sweep(monkeypatch)

    try:
        with pytest.raises(KeyboardInterrupt):
            daemon.idle_park_loop({"bot_token": "t", "chat_id": 1})

        assert not any("parked in the same instant" in t for _tid, t in replies)
        with daemon._pending_reopen_lock:
            assert daemon.pending_reopens.get("5935", {}).get("state") == "delivered"
    finally:
        with daemon._pending_reopen_lock:
            daemon.pending_reopens.pop("5935", None)


def test_a_clean_park_does_not_trigger_the_repair(registry, park_env, monkeypatch):
    """The repair only fires on an actually-lost race — a clean park must not revive
    the session it just parked."""
    registry({"5935": {"pane": "%1", "name": "x", "session_id": "sid"}})
    revived = []
    monkeypatch.setattr(daemon, "maybe_auto_revive",
                        lambda cfg, tid, cause="auto": revived.append(str(tid)))
    _one_sweep(monkeypatch)

    with pytest.raises(KeyboardInterrupt):
        daemon.idle_park_loop({"bot_token": "t", "chat_id": 1})

    assert common.read_registry()["5935"].get("parked") is True
    assert revived == []


def test_a_message_arriving_during_the_recheck_is_refused(registry, park_env, monkeypatch):
    """#274 r2, finding 2: a message lands in the inbox while the sweep sleeps. The
    unread gate runs AFTER the sleep now, so the park refuses and the entry stays live —
    the message is delivered normally, not stranded behind a kill."""
    tmux_calls, replies = park_env
    registry({"5935": {"pane": "%1", "name": "x", "session_id": "sid"}})
    inbox = {"n": 0}
    monkeypatch.setattr(daemon, "unread_count", lambda tid: inbox["n"])
    _one_sweep(monkeypatch, on_recheck=lambda: inbox.__setitem__("n", 1))

    with pytest.raises(KeyboardInterrupt):
        daemon.idle_park_loop({"bot_token": "t", "chat_id": 1})

    assert not any("kill-pane" in c for c in tmux_calls), (
        "killed a session with a message waiting (#274 r2, finding 2)"
    )
    entry = common.read_registry()["5935"]
    assert "ended" not in entry and "parked" not in entry


def test_transcript_activity_during_the_recheck_is_refused(registry, park_env, monkeypatch):
    """#274 r2, finding 2's delivered-message edge: the session's transcript moved during
    the sleep (a message was typed in and answered). The post-sleep ager sees it."""
    tmux_calls, _ = park_env
    registry({"5935": {"pane": "%1", "name": "x", "session_id": "sid"}})
    age = {"last": time.time() - 7 * 3600}
    monkeypatch.setattr(daemon, "_session_last_activity", lambda info: age["last"])
    _one_sweep(monkeypatch, on_recheck=lambda: age.__setitem__("last", time.time()))

    with pytest.raises(KeyboardInterrupt):
        daemon.idle_park_loop({"bot_token": "t", "chat_id": 1})

    assert not any("kill-pane" in c for c in tmux_calls), (
        "killed a session that became active during the recheck (#274 r2, finding 2)"
    )


def test_a_carry_forward_acquired_during_the_recheck_is_refused(
        registry, park_env, monkeypatch):
    """#274 r2, finding 4: /cf installs its pending record while the sweep sleeps; the
    ownership check runs after the sleep and refuses."""
    tmux_calls, _ = park_env
    registry({"5935": {"pane": "%1", "name": "x", "session_id": "sid"}})
    cf = {"active": False}
    monkeypatch.setattr(daemon, "carry_forward_active", lambda tid: cf["active"])
    _one_sweep(monkeypatch, on_recheck=lambda: cf.__setitem__("active", True))

    with pytest.raises(KeyboardInterrupt):
        daemon.idle_park_loop({"bot_token": "t", "chat_id": 1})

    assert not any("kill-pane" in c for c in tmux_calls), (
        "killed underneath an in-flight carry-forward (#274 r2, finding 4)"
    )


def test_an_error_after_the_claim_still_undoes_it(registry, park_env, monkeypatch):
    """#274 r2, finding 3: there is no post-claim statement outside the try — any error
    between claim and kill runs the token-guarded undo, leaving the entry live."""
    tmux_calls, replies = park_env
    registry({"5935": {"pane": "%1", "name": "x", "session_id": "sid"}})
    real_gates = daemon._park_gates_hold

    def _gates(tid, pane, pid, cutoff, claim_token=None):
        if claim_token is not None:
            raise OSError("registry read failed under the claim")
        return real_gates(tid, pane, pid, cutoff, claim_token)

    monkeypatch.setattr(daemon, "_park_gates_hold", _gates)
    _one_sweep(monkeypatch)

    with pytest.raises(KeyboardInterrupt):
        daemon.idle_park_loop({"bot_token": "t", "chat_id": 1})

    entry = common.read_registry()["5935"]
    assert "ended" not in entry and "parked" not in entry and "park_claim" not in entry, (
        "an error after the claim left a live pane stamped ended (#274 r2, finding 3)"
    )
    assert not any("kill-pane" in c for c in tmux_calls)


def test_a_same_second_same_pane_restamp_survives_the_undo(
        registry, park_env, monkeypatch):
    """#274 r2, finding 3's ABA: while the parker is between claim and kill, a revive
    clears its claim and another legitimate actor restamps the SAME pane within the SAME
    second. The undo compares the unique token, so pane+timestamp equality can no longer
    fool it — the restamp survives."""
    tmux_calls, _ = park_env
    registry({"5935": {"pane": "%1", "name": "x", "session_id": "sid"}})
    restamp = {}

    def _tmux(argv, **kw):
        tmux_calls.append(argv)
        if argv[:2] == ["tmux", "kill-pane"]:
            def _swap(r):
                e = r["5935"]
                restamp["stamp"] = e["ended"]  # identical timestamp, identical pane
                e.pop("park_claim", None)      # our claim is gone — not ours any more
                e.pop("parked", None)
            common.update_registry(_swap)
            return types.SimpleNamespace(returncode=1, stdout="", stderr="")
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(daemon, "_tmux", _tmux)
    _one_sweep(monkeypatch)

    with pytest.raises(KeyboardInterrupt):
        daemon.idle_park_loop({"bot_token": "t", "chat_id": 1})

    assert common.read_registry()["5935"].get("ended") == restamp["stamp"], (
        "the undo popped a same-pane, same-second stamp that was not ours "
        "(#274 r2, finding 3)"
    )


def test_the_parked_flag_is_visible_the_instant_the_claim_lands(
        registry, park_env, monkeypatch):
    """#274 r2, finding 5: `parked` is written IN the claim, not after the kill — a
    message racing the gap snapshots an entry that already says why it ended."""
    tmux_calls, _ = park_env
    registry({"5935": {"pane": "%1", "name": "x", "session_id": "sid"}})
    seen = {}

    def _tmux(argv, **kw):
        tmux_calls.append(argv)
        if argv[:2] == ["tmux", "kill-pane"]:
            e = common.read_registry()["5935"]
            seen["parked"] = e.get("parked")
            seen["ended"] = bool(e.get("ended"))
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(daemon, "_tmux", _tmux)
    _one_sweep(monkeypatch)

    with pytest.raises(KeyboardInterrupt):
        daemon.idle_park_loop({"bot_token": "t", "chat_id": 1})

    assert seen == {"parked": True, "ended": True}, (
        "a snapshot taken before the kill missed the parked flag (#274 r2, finding 5)"
    )


def test_the_choice_path_reports_the_parked_cause(registry, monkeypatch):
    """#274 r2, finding 5: a parked LARGE claude session goes through the resume-choice
    question; the revive after the answer must still say the bridge parked it, not that
    the owner reopened the topic."""
    registry({"5935": {"pane": "%1", "name": "x", "session_id": "sid",
                       "ended": "2026-09-01T00:00:00+0000", "parked": True}})
    seen = []
    monkeypatch.setattr(daemon, "revive_one",
                        lambda cfg, tid, e, brief=True, cause=None, **k:
                        seen.append(cause) or ("resumed", {}))
    monkeypatch.setattr(daemon.threading, "Thread",
                        lambda target=None, daemon=True: type(
                            "T", (), {"start": lambda self: target()})())
    daemon.pending_reopens.pop("5935", None)

    assert daemon._revive_with_choice({"bot_token": "t", "chat_id": 1}, "5935",
                                      common.read_registry()["5935"], "compact")
    assert seen == ["parked"], (
        "the choice path reported 'reopen' for a bridge-parked session (#274 r2, f5)"
    )


def test_the_choice_path_keeps_reopen_for_unparked_sessions(registry, monkeypatch):
    """The inverse pin: a session that died on its own and was reopened by the owner
    still reports 'reopen' — the parked cause never leaks onto real reopens."""
    registry({"5935": {"pane": "%1", "name": "x", "session_id": "sid",
                       "ended": "2026-09-01T00:00:00+0000"}})
    seen = []
    monkeypatch.setattr(daemon, "revive_one",
                        lambda cfg, tid, e, brief=True, cause=None, **k:
                        seen.append(cause) or ("resumed", {}))
    monkeypatch.setattr(daemon.threading, "Thread",
                        lambda target=None, daemon=True: type(
                            "T", (), {"start": lambda self: target()})())
    daemon.pending_reopens.pop("5935", None)

    assert daemon._revive_with_choice({"bot_token": "t", "chat_id": 1}, "5935",
                                      common.read_registry()["5935"], "compact")
    assert seen == ["reopen"]


# ---- Claim/kill races carried over from r1 ----

def test_a_failed_kill_undoes_the_claim(registry, park_env, monkeypatch):
    """C1/C3 (#274 r1, finding 3): a kill that fails must not leave `ended` on a live
    pane — and the undo pops only this sweep's own claim token."""
    tmux_calls, replies = park_env

    def _tmux(argv, **kw):
        tmux_calls.append(argv)
        if argv[:2] == ["tmux", "kill-pane"]:
            return types.SimpleNamespace(returncode=1, stdout="", stderr="server gone")
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(daemon, "_tmux", _tmux)
    registry({"5935": {"pane": "%1", "name": "x", "session_id": "sid"}})
    _one_sweep(monkeypatch)

    with pytest.raises(KeyboardInterrupt):
        daemon.idle_park_loop({"bot_token": "t", "chat_id": 1})

    entry = common.read_registry()["5935"]
    assert "ended" not in entry and "parked" not in entry and "park_claim" not in entry, (
        "claim not undone"
    )
    assert replies == []


def test_the_undo_never_pops_a_newer_stamp(registry, park_env, monkeypatch):
    """The r1 ABA: our claim is cleared by a revive, a NEWER legitimate stamp lands on a
    different pane, our undo runs — the newer stamp must survive."""
    tmux_calls, replies = park_env
    registry({"5935": {"pane": "%1", "name": "x", "session_id": "sid"}})
    newer = "2026-09-01T23:59:59+0000"

    def _tmux(argv, **kw):
        tmux_calls.append(argv)
        if argv[:2] == ["tmux", "kill-pane"]:
            common.update_registry(lambda r: (
                r["5935"].pop("park_claim", None),
                r["5935"].update({"pane": "%2", "ended": newer})))
            return types.SimpleNamespace(returncode=1, stdout="", stderr="")
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(daemon, "_tmux", _tmux)
    _one_sweep(monkeypatch)

    with pytest.raises(KeyboardInterrupt):
        daemon.idle_park_loop({"bot_token": "t", "chat_id": 1})

    assert common.read_registry()["5935"].get("ended") == newer, (
        "the guarded undo popped a stamp that was not ours (#274 r1, finding 3)"
    )


def test_a_revive_between_claim_and_gates_aborts_the_park(registry, park_env, monkeypatch):
    """#274 r1, finding 2: a revive lands immediately after the claim — the under-claim
    battery sees the token gone and abandons; nothing is killed."""
    tmux_calls, replies = park_env
    registry({"5935": {"pane": "%1", "name": "x", "session_id": "sid"}})
    real_claim = daemon._claim_park

    def _claim_then_revive(tid, pane):
        token = real_claim(tid, pane)
        if token:  # the revive lands immediately after the claim, as _bind would
            common.update_registry(lambda r: (
                r["5935"].pop("ended", None), r["5935"].pop("parked", None),
                r["5935"].pop("park_claim", None)))
        return token

    monkeypatch.setattr(daemon, "_claim_park", _claim_then_revive)
    _one_sweep(monkeypatch)

    with pytest.raises(KeyboardInterrupt):
        daemon.idle_park_loop({"bot_token": "t", "chat_id": 1})

    assert not any("kill-pane" in c for c in tmux_calls), (
        "killed a pane a revive had just claimed (#274 r1, finding 2)"
    )


def test_a_revive_during_the_under_claim_battery_still_aborts_before_the_kill(
        registry, park_env, monkeypatch):
    """The LAST window that is still checkable: a revive lands while the under-claim
    battery is mid-pass — the final token look immediately before the kill catches it."""
    tmux_calls, replies = park_env
    registry({"5935": {"pane": "%1", "name": "x", "session_id": "sid"}})

    def _idle_and_revive(pane):
        # pane_is_idle is the battery's last gate; a revive landing here has passed
        # every registry check already
        if common.read_registry().get("5935", {}).get("park_claim"):
            common.update_registry(lambda r: (
                r["5935"].pop("ended", None), r["5935"].pop("parked", None),
                r["5935"].pop("park_claim", None)))
        return True

    monkeypatch.setattr(daemon, "pane_is_idle", _idle_and_revive)
    _one_sweep(monkeypatch)

    with pytest.raises(KeyboardInterrupt):
        daemon.idle_park_loop({"bot_token": "t", "chat_id": 1})

    assert not any("kill-pane" in c for c in tmux_calls), (
        "killed a pane a revive claimed during the under-claim battery"
    )


def test_a_rebound_topic_is_not_parked(registry, park_env, monkeypatch):
    """The #237 fence carries over: if a revive rebinds the topic between the sweep's
    read and the claim, the claim refuses and nothing is killed."""
    tmux_calls, replies = park_env
    registry({"5935": {"pane": "%NEW", "name": "x", "session_id": "sid"}})
    monkeypatch.setattr(daemon, "read_registry",
                        lambda: {"5935": {"pane": "%1", "name": "x", "session_id": "sid"}})
    _one_sweep(monkeypatch)

    with pytest.raises(KeyboardInterrupt):
        daemon.idle_park_loop({"bot_token": "t", "chat_id": 1})

    assert not any("kill-pane" in c or "kill-session" in c for c in tmux_calls)
    assert "ended" not in common.read_registry()["5935"]


def test_a_parked_entry_revives_with_the_parked_cause(registry, park_env, monkeypatch):
    """#274 r1, finding 5: the ordinary-message revive must say the bridge parked the
    session, not that the terminal died."""
    registry({"5935": {"pane": "%1", "name": "x", "session_id": "sid"}})
    _one_sweep(monkeypatch)
    with pytest.raises(KeyboardInterrupt):
        daemon.idle_park_loop({"bot_token": "t", "chat_id": 1})

    entry = common.read_registry()["5935"]
    assert entry.get("parked") is True

    seen = []
    monkeypatch.setattr(daemon, "revive_one",
                        lambda cfg, tid, e, brief=True, cause=None, **k:
                        seen.append(cause) or ("resumed", {}))
    monkeypatch.setattr(daemon, "should_auto_revive", lambda e: True)
    monkeypatch.setattr(daemon, "_reopen_needs_asking", lambda e: False)
    monkeypatch.setattr(daemon.threading, "Thread",
                        lambda target=None, daemon=True: type(
                            "T", (), {"start": lambda self: target()})())
    daemon.pending_reopens.pop("5935", None)
    daemon.maybe_auto_revive({"bot_token": "t", "chat_id": 1}, "5935")

    assert seen == ["parked"], (
        "a parked session's revive reported a death that was not one (#274 r1, f5)"
    )


# ---- Primitive parsing ----

def test_pane_occupant_fails_closed(monkeypatch):
    """No pane pid, or a pid whose /proc entry is gone → None, never a guess."""
    monkeypatch.setattr(daemon, "pane_pid", lambda p: None)
    assert daemon._pane_occupant("%1") is None
    monkeypatch.setattr(daemon, "pane_pid", lambda p: 999999999)
    assert daemon._pane_occupant("%1") is None


def test_pane_occupant_resolves_the_foreground_group_not_the_shell(monkeypatch):
    """#274 r3, finding 1: for a shell-hosted pane the identity is the FOREGROUND
    process group, not #{pane_pid}. Against the real /proc: the occupant of a pane whose
    first process is this test process is whatever its tpgid names — either a well-formed
    (tpgid, past-epoch) pair or a fail-closed None (no controlling terminal), never the
    shell pid with tpgid ignored."""
    monkeypatch.setattr(daemon, "pane_pid", lambda p: os.getpid())
    occ = daemon._pane_occupant("%1")
    if occ is not None:
        tpgid, started = occ
        assert tpgid > 0 and 0 < started <= time.time()
        fields = daemon._proc_stat_fields(os.getpid())
        assert tpgid == int(fields[5]), "identity must be the tpgid, not the first pid"


def test_activity_comes_from_the_session_artifact_not_the_ctx_record(monkeypatch, tmp_path):
    """A1: the ager stats the transcript/rollout; the stale-by-weeks ctx record is never
    consulted."""
    t = tmp_path / "sid.jsonl"
    t.write_text("{}")
    monkeypatch.setattr(daemon.transcript, "transcript_path",
                        lambda cwd, sid: str(t))
    reads = []
    monkeypatch.setattr(daemon, "read_context",
                        lambda *a, **k: reads.append(1) or None)

    last = daemon._session_last_activity(
        {"session_id": "sid", "engine": "claude", "cwd": "/x"})

    assert last == pytest.approx(t.stat().st_mtime)
    assert reads == [], "parked on the statusline ctx record (A1)"


def test_zero_hours_disables_the_loop():
    """C5: the thread is only started when IDLE_PARK_HOURS > 0 — pinned at the source
    level because main() cannot be driven in a unit test."""
    import inspect
    src = inspect.getsource(daemon.main)
    assert "IDLE_PARK_HOURS > 0" in src.split("idle_park_loop")[0].rsplit("\n", 2)[-1] or \
        "if IDLE_PARK_HOURS > 0:" in src, "the disable gate is gone"
