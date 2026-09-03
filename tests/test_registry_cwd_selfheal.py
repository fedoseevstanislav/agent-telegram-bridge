"""The registry's `cwd` must point where the session's transcript actually is (#78).

`tg-bridge register` captures `os.getcwd()` once and nothing revises it. A session that ran
`register` after a `cd` but whose `claude` was launched elsewhere therefore carries a
directory holding no transcript. `claude --resume` is cwd-scoped, so `revive_one` relaunches
into a directory where the resume finds nothing and the pane dies in about a second — on
every attempt, at every reboot — one such session crashed on both of its revive attempts
while every other session on the host came back.

Revive is only the loud symptom: the same field feeds every `transcript.transcript_path()`
reader, where a wrong cwd is silent — the file is simply absent, which reads as "no data".

The heal never inverts the project-directory name, because that mapping is lossy. It finds
the transcript, reads the cwd out of the file's own records, and writes it only if it maps
back to the file it came from.
"""

import json
import os

import pytest

from bridge import daemon, transcript


def _write_transcript(projects, cwd, sid, records=None):
    """Create ~/.claude/projects/<flattened cwd>/<sid>.jsonl the way Claude Code does."""
    flat = transcript.transcript_path(cwd, sid)
    # transcript_path is PROJECTS_DIR-relative, and PROJECTS_DIR is the patched tmp dir.
    os.makedirs(os.path.dirname(flat), exist_ok=True)
    if records is None:
        records = [{"type": "user", "cwd": cwd, "sessionId": sid}]
    with open(flat, "w") as f:
        for record in records:
            f.write(json.dumps(record) + "\n")
    assert os.path.dirname(flat).startswith(str(projects))
    return flat


@pytest.fixture
def snapshot(monkeypatch, tmp_path):
    """One live claude topic on pane %5, session s-1111, with a settable registry cwd."""
    projects = tmp_path / "projects"
    projects.mkdir()
    monkeypatch.setattr(transcript, "PROJECTS_DIR", str(projects))

    reg = {"3300": {"pane": "%5", "cwd": str(tmp_path / "registered-here"),
                    "engine": "claude", "session_id": "s-1111", "boot_id": "boot-1"}}
    logged = []
    monkeypatch.setattr(daemon, "read_registry", lambda: {k: dict(v) for k, v in reg.items()})
    monkeypatch.setattr(daemon, "pane_alive", lambda _p: True)
    monkeypatch.setattr(daemon, "engine_of_pane", lambda _p: "claude")
    monkeypatch.setattr(daemon, "session_id_for_pane", lambda _p, _e: "s-1111")
    monkeypatch.setattr(daemon, "current_boot_id", lambda: "boot-1")
    monkeypatch.setattr(daemon, "log", lambda msg: logged.append(msg))

    def _update(fn):
        fn(reg)

    monkeypatch.setattr(daemon, "update_registry", _update)
    return {"projects": projects, "reg": reg, "logged": logged, "tmp": tmp_path}


# ---- C1 / C2: a wrong cwd is corrected, from the transcript's own record ------------------

def test_a_cwd_pointing_at_no_transcript_is_corrected(snapshot):
    launched_in = str(snapshot["tmp"] / "actually-launched-here")
    _write_transcript(snapshot["projects"], launched_in, "s-1111")

    daemon.snapshot_once()

    assert snapshot["reg"]["3300"]["cwd"] == launched_in
    assert any("cwd" in m and "s-1111"[:8] in m for m in snapshot["logged"])


def test_the_corrected_cwd_is_one_that_actually_resolves(snapshot):
    """The point of the round-trip check: whatever is written must find the transcript."""
    launched_in = str(snapshot["tmp"] / "actually-launched-here")
    _write_transcript(snapshot["projects"], launched_in, "s-1111")

    daemon.snapshot_once()

    healed = snapshot["reg"]["3300"]["cwd"]
    assert os.path.exists(transcript.transcript_path(healed, "s-1111"))


def test_a_cwd_field_that_does_not_round_trip_is_not_believed(snapshot):
    """A record can carry a cwd that belongs to some other directory — a copied transcript, a
    session whose file was moved. Writing it would swap one wrong directory for another."""
    launched_in = str(snapshot["tmp"] / "actually-launched-here")
    _write_transcript(snapshot["projects"], launched_in, "s-1111",
                      records=[{"type": "user", "cwd": str(snapshot["tmp"] / "somewhere-else")}])
    before = snapshot["reg"]["3300"]["cwd"]

    daemon.snapshot_once()

    assert snapshot["reg"]["3300"]["cwd"] == before
    assert transcript.launch_cwd("s-1111")[0] is None


# ---- C3: the healthy path costs one stat -------------------------------------------------

def test_a_correct_cwd_triggers_no_glob_and_no_write(snapshot, monkeypatch):
    correct = str(snapshot["tmp"] / "registered-here")
    _write_transcript(snapshot["projects"], correct, "s-1111")
    snapshot["reg"]["3300"]["cwd"] = correct

    def _no_glob(*_a, **_k):
        raise AssertionError("globbed the projects tree for a session already resolvable")

    monkeypatch.setattr(transcript.glob, "glob", _no_glob)
    monkeypatch.setattr(daemon, "update_registry",
                        lambda fn: (_ for _ in ()).throw(
                            AssertionError("wrote the registry for an already-correct cwd")))

    daemon.snapshot_once()          # the entry is otherwise unchanged, so nothing else writes

    assert snapshot["reg"]["3300"]["cwd"] == correct


# ---- C4: not answerable means leave it alone ---------------------------------------------

def test_no_transcript_anywhere_leaves_the_cwd_alone(snapshot):
    before = snapshot["reg"]["3300"]["cwd"]
    daemon.snapshot_once()
    assert snapshot["reg"]["3300"]["cwd"] == before
    assert any("no transcript with that name" in m for m in snapshot["logged"])


def test_two_transcripts_with_the_same_name_are_ambiguous_and_not_guessed(snapshot):
    _write_transcript(snapshot["projects"], str(snapshot["tmp"] / "one"), "s-1111")
    _write_transcript(snapshot["projects"], str(snapshot["tmp"] / "two"), "s-1111")
    before = snapshot["reg"]["3300"]["cwd"]

    daemon.snapshot_once()

    assert snapshot["reg"]["3300"]["cwd"] == before
    assert transcript.launch_cwd("s-1111")[0] is None


def test_a_transcript_with_no_cwd_record_leaves_the_cwd_alone(snapshot):
    _write_transcript(snapshot["projects"], str(snapshot["tmp"] / "elsewhere"), "s-1111",
                      records=[{"type": "ai-title", "sessionId": "s-1111"}])
    before = snapshot["reg"]["3300"]["cwd"]

    daemon.snapshot_once()

    assert snapshot["reg"]["3300"]["cwd"] == before


# ---- C5: the write is locked, bound to the session, and touches nothing else -------------

def test_the_write_is_rechecked_under_the_lock_against_the_session_id(snapshot):
    """`info` is read before the pane probes run. If the topic was rebound in that gap, the
    entry under the lock belongs to a different session and must not take this cwd."""
    launched_in = str(snapshot["tmp"] / "actually-launched-here")
    _write_transcript(snapshot["projects"], launched_in, "s-1111")
    rebound = str(snapshot["tmp"] / "some-other-session-cwd")

    def _rebind_then_apply(fn):
        snapshot["reg"]["3300"]["session_id"] = "s-9999"   # a different session got the topic
        snapshot["reg"]["3300"]["cwd"] = rebound
        fn(snapshot["reg"])

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(daemon, "update_registry", _rebind_then_apply)
        daemon.snapshot_once()

    assert snapshot["reg"]["3300"]["cwd"] == rebound


def test_the_heal_changes_no_field_but_cwd(snapshot):
    launched_in = str(snapshot["tmp"] / "actually-launched-here")
    _write_transcript(snapshot["projects"], launched_in, "s-1111")
    before = dict(snapshot["reg"]["3300"])

    daemon.snapshot_once()

    after = snapshot["reg"]["3300"]
    assert set(after) == set(before)
    assert {k: v for k, v in after.items() if k != "cwd"} == \
           {k: v for k, v in before.items() if k != "cwd"}


# ---- C6: codex is not touched ------------------------------------------------------------

def test_a_codex_entry_is_left_alone(snapshot, monkeypatch):
    """`claude --resume` is the cwd-scoped command; codex reads its cwd from its own rollout
    metadata, so healing this field against a claude transcript would be meaningless here."""
    monkeypatch.setattr(daemon, "engine_of_pane", lambda _p: "codex")
    snapshot["reg"]["3300"]["engine"] = "codex"
    _write_transcript(snapshot["projects"], str(snapshot["tmp"] / "elsewhere"), "s-1111")
    before = snapshot["reg"]["3300"]["cwd"]

    def _no_glob(*_a, **_k):
        raise AssertionError("resolved a claude transcript for a codex session")

    monkeypatch.setattr(transcript.glob, "glob", _no_glob)
    daemon.snapshot_once()

    assert snapshot["reg"]["3300"]["cwd"] == before


# ---- the heal runs even when nothing else about the entry changed ------------------------

def test_a_stable_entry_is_still_healed(snapshot):
    """The entry's engine, session id and boot id all match, so snapshot_once takes its
    unchanged-entry shortcut. A cwd can be stable AND wrong; that is this whole bug."""
    launched_in = str(snapshot["tmp"] / "actually-launched-here")
    _write_transcript(snapshot["projects"], launched_in, "s-1111")
    assert snapshot["reg"]["3300"]["boot_id"] == "boot-1"    # nothing for the shortcut to see

    daemon.snapshot_once()

    assert snapshot["reg"]["3300"]["cwd"] == launched_in


# ---- launch_cwd on its own ---------------------------------------------------------------

def test_launch_cwd_reads_a_directory_the_flattening_cannot_be_inverted_from(snapshot):
    """'-home-user--worktrees-12' has many pre-images: a dot, a slash and a dash all flatten
    to '-'. The answer comes from the file, not from the name."""
    tricky = str(snapshot["tmp"] / ".worktrees" / "12-feature-branch")
    _write_transcript(snapshot["projects"], tricky, "s-2222")

    assert transcript.launch_cwd("s-2222") == (tricky, None)
    assert transcript.launch_cwd("s-nonexistent")[0] is None


# ---- the cases the cross-family review found ---------------------------------------------

def test_an_entry_that_does_not_yet_carry_the_session_id_waits_a_cycle(snapshot):
    """Two review rounds live in this one condition. Accepting an entry with no session id —
    to spare a fresh entry one cycle's delay — let a topic rebound to a NEW, still-unstamped
    session on the SAME pane take the old session's directory. A pane check does not close
    that (the pane is the same), and re-reading the live id under the lock would put a tmux
    probe inside the registry lock. The id is the fact the directory belongs to, so the write
    waits for it and the heal lands on the next cycle."""
    launched_in = str(snapshot["tmp"] / "actually-launched-here")
    before = snapshot["reg"]["3300"]["cwd"]
    _write_transcript(snapshot["projects"], launched_in, "s-1111")
    snapshot["reg"]["3300"].pop("session_id")

    daemon.snapshot_once()                       # cycle 1: stamps the id, does not heal
    assert snapshot["reg"]["3300"]["cwd"] == before
    assert snapshot["reg"]["3300"]["session_id"] == "s-1111"

    daemon.snapshot_once()                       # cycle 2: the id is there, so the heal lands
    assert snapshot["reg"]["3300"]["cwd"] == launched_in


def test_a_rebind_to_a_new_unstamped_session_on_the_same_pane_takes_no_cwd(snapshot):
    """The interleaving the cross-family review reproduced: an old session is sampled, then
    the topic rebinds on the same pane to a new session that has not been stamped yet. The
    pane is identical, so only the session id can refuse this."""
    launched_in = str(snapshot["tmp"] / "actually-launched-here")
    _write_transcript(snapshot["projects"], launched_in, "s-1111")
    newcomer = str(snapshot["tmp"] / "the-new-sessions-cwd")

    def _rebind_then_apply(fn):
        snapshot["reg"]["3300"].pop("session_id")      # new session, not stamped yet
        snapshot["reg"]["3300"]["cwd"] = newcomer
        fn(snapshot["reg"])

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(daemon, "update_registry", _rebind_then_apply)
        daemon.snapshot_once()

    assert snapshot["reg"]["3300"]["cwd"] == newcomer


def test_the_log_never_claims_a_heal_the_guard_refused(snapshot):
    """A log line is evidence. One saying `cwd a -> b` when the locked writer declined is the
    #233 class — the next reader will not re-derive it. Written after the lock, from whether
    the write happened."""
    launched_in = str(snapshot["tmp"] / "actually-launched-here")
    _write_transcript(snapshot["projects"], launched_in, "s-1111")

    def _rebind_then_apply(fn):
        snapshot["reg"]["3300"]["session_id"] = "s-9999"
        fn(snapshot["reg"])

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(daemon, "update_registry", _rebind_then_apply)
        daemon.snapshot_once()

    assert not any(f"-> {launched_in}" in m for m in snapshot["logged"])
    assert any("not healed yet" in m for m in snapshot["logged"])


def test_a_relative_cwd_is_refused(snapshot):
    """`transcript_path` calls `os.path.abspath`, which resolves a relative path against
    whatever the READER's cwd happens to be — so '.' can round-trip by accident and would be
    written into the registry as a launch directory."""
    launched_in = str(snapshot["tmp"] / "actually-launched-here")
    _write_transcript(snapshot["projects"], launched_in, "s-1111",
                      records=[{"type": "user", "cwd": ".", "sessionId": "s-1111"}])
    before = snapshot["reg"]["3300"]["cwd"]

    daemon.snapshot_once()

    assert snapshot["reg"]["3300"]["cwd"] == before
    assert transcript.launch_cwd("s-1111") == (None, "its cwd '.' is relative")


def test_a_record_naming_another_session_is_skipped(snapshot):
    """A sidechain or copied record describes someone else's directory. Skipped, not fatal:
    the next record down may be this session's own."""
    launched_in = str(snapshot["tmp"] / "actually-launched-here")
    _write_transcript(snapshot["projects"], launched_in, "s-1111", records=[
        {"type": "user", "cwd": launched_in, "sessionId": "s-1111"},
        {"type": "user", "cwd": str(snapshot["tmp"] / "someone-else"), "sessionId": "s-other"},
    ])

    daemon.snapshot_once()

    assert snapshot["reg"]["3300"]["cwd"] == launched_in


@pytest.mark.parametrize("setup, expected", [
    (None, "no transcript with that name"),
    ("two", "2 transcripts with that name"),
    ("no-cwd", "no record in its transcript carries a cwd"),
    ("relative", "is relative"),
    ("no-round-trip", "does not lead back to it"),
])
def test_each_unanswerable_case_logs_its_own_reason(snapshot, setup, expected):
    """C4 asked for the reason, not for one line covering five different situations: which
    case it is decides whether an operator should do anything about it."""
    here = str(snapshot["tmp"] / "actually-launched-here")
    if setup == "two":
        _write_transcript(snapshot["projects"], str(snapshot["tmp"] / "one"), "s-1111")
        _write_transcript(snapshot["projects"], str(snapshot["tmp"] / "two"), "s-1111")
    elif setup == "no-cwd":
        _write_transcript(snapshot["projects"], here, "s-1111",
                          records=[{"type": "ai-title", "sessionId": "s-1111"}])
    elif setup == "relative":
        _write_transcript(snapshot["projects"], here, "s-1111",
                          records=[{"type": "user", "cwd": "../elsewhere"}])
    elif setup == "no-round-trip":
        _write_transcript(snapshot["projects"], here, "s-1111",
                          records=[{"type": "user", "cwd": str(snapshot["tmp"] / "nope")}])

    daemon.snapshot_once()

    assert any(expected in m for m in snapshot["logged"]), snapshot["logged"]
