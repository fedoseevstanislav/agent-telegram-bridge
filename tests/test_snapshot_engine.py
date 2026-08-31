"""#198 — the snapshot must record a pane's engine even before a session id exists.

`snapshot_once` resolved the engine, then the session id, then `continue`d when the id was
falsy — discarding an engine it had already determined. That is not hypothetical: a **codex
session has no session_id until it completes its first turn**, because it does not open its
`rollout-*.jsonl` before then and `codex_session_id_for_pane` reads the id from that open fd.

Measured 2026-08-26 on a bare codex pane: eight samples over two minutes all returned
`engine='codex' sid=None`, then one real turn produced an id within 10s.

So every session killed before it did anything left `engine: null` behind — ten such entries
were in the registry — and every later decision about that topic then guessed "claude".
Topic 15569 was the one the owner hit: they killed a codex demo two minutes after spawning it,
reopened the topic, and the bridge could not even tell which engine it had been.
"""

import pytest

from bridge import daemon


LIVE = {"name": "test-theme", "pane": "%162"}


@pytest.fixture
def snap(monkeypatch):
    """snapshot_once over one registry entry, with the pane probes stubbed."""
    state = {"registry": {"15569": dict(LIVE)}, "writes": []}

    def _update(fn):
        fn(state["registry"])
        state["writes"].append({k: dict(v) for k, v in state["registry"].items()})

    monkeypatch.setattr(daemon, "read_registry", lambda: state["registry"])
    monkeypatch.setattr(daemon, "update_registry", _update)
    monkeypatch.setattr(daemon, "current_boot_id", lambda: "boot-x")
    monkeypatch.setattr(daemon, "pane_alive", lambda pane: True)
    monkeypatch.setattr(daemon, "log", lambda *a, **k: None)
    return state


def test_engine_is_recorded_before_the_first_turn(snap, monkeypatch):
    monkeypatch.setattr(daemon, "engine_of_pane", lambda pane: "codex")
    monkeypatch.setattr(daemon, "session_id_for_pane", lambda pane, engine: None)

    daemon.snapshot_once()

    assert snap["registry"]["15569"]["engine"] == "codex", (
        "threw away an engine it had already resolved, because no session id existed yet"
    )
    assert snap["registry"]["15569"].get("session_id") is None, (
        "invented a session id that codex has not created yet"
    )


def test_the_engine_write_does_not_repeat_once_recorded(snap, monkeypatch):
    """Writes only on change — the loop runs every 60s against every registered topic, and a
    session can sit pre-first-turn for a long time."""
    monkeypatch.setattr(daemon, "engine_of_pane", lambda pane: "codex")
    monkeypatch.setattr(daemon, "session_id_for_pane", lambda pane, engine: None)

    daemon.snapshot_once()
    daemon.snapshot_once()
    daemon.snapshot_once()

    assert len(snap["writes"]) == 1, f"re-wrote an unchanged engine: {len(snap['writes'])} writes"


def test_the_full_stamp_still_wins_once_the_session_id_appears(snap, monkeypatch):
    monkeypatch.setattr(daemon, "engine_of_pane", lambda pane: "codex")
    monkeypatch.setattr(daemon, "session_id_for_pane", lambda pane, engine: None)
    daemon.snapshot_once()

    monkeypatch.setattr(daemon, "session_id_for_pane", lambda pane, engine: "01a03dd8")
    daemon.snapshot_once()

    entry = snap["registry"]["15569"]
    assert (entry["engine"], entry["session_id"], entry["boot_id"]) == (
        "codex", "01a03dd8", "boot-x")


def test_an_unrecognised_engine_is_not_recorded(snap, monkeypatch):
    """The pre-existing guard: only claude and codex are ever stamped. A pane running
    something else must not be labelled, or a revive would launch the wrong TUI."""
    monkeypatch.setattr(daemon, "engine_of_pane", lambda pane: "bash")
    monkeypatch.setattr(daemon, "session_id_for_pane", lambda pane, engine: None)

    daemon.snapshot_once()

    assert snap["registry"]["15569"].get("engine") is None
    assert snap["writes"] == []


def test_an_ended_or_feed_topic_is_never_stamped(snap, monkeypatch):
    monkeypatch.setattr(daemon, "engine_of_pane", lambda pane: "codex")
    monkeypatch.setattr(daemon, "session_id_for_pane", lambda pane, engine: None)
    snap["registry"]["15569"]["ended"] = "2026-08-26T11:24:22+0000"
    snap["registry"]["9999"] = {"pane": "%99", "feed": True}

    daemon.snapshot_once()

    assert snap["writes"] == []


def test_an_engine_change_is_not_stamped_over_an_existing_session_id(snap, monkeypatch):
    """Review round 1, finding 4. A missing sid does not only mean "none exists yet" — the
    lookup can be transient or stale. Writing the engine alone then pairs a NEW engine with
    the OLD engine's id, and `_resume_launch` builds `claude --resume <CODEX-SID>`. Half of an
    (engine, session_id) pair must never be replaced while the other half still stands."""
    snap["registry"]["15569"].update({"engine": "codex", "session_id": "CODEX-SID"})
    monkeypatch.setattr(daemon, "engine_of_pane", lambda pane: "claude")
    monkeypatch.setattr(daemon, "session_id_for_pane", lambda pane, engine: None)

    daemon.snapshot_once()

    entry = snap["registry"]["15569"]
    assert (entry["engine"], entry["session_id"]) == ("codex", "CODEX-SID"), (
        "paired a new engine with the previous engine's session id"
    )
    assert snap["writes"] == []


def test_a_session_id_arriving_concurrently_blocks_the_engine_write(snap, monkeypatch):
    """Review round 2. The gate reads `info`, a snapshot taken before the pane probes ran.
    A concurrent update that adds a session id in that gap left the gate satisfied and the
    write produced (claude, CODEX-SID) anyway. The mutator must re-check under the lock."""
    monkeypatch.setattr(daemon, "engine_of_pane", lambda pane: "claude")
    monkeypatch.setattr(daemon, "session_id_for_pane", lambda pane, engine: None)

    real_update = daemon.update_registry

    def racing(fn):
        # Another writer lands between the gate and the mutator.
        snap["registry"]["15569"].update({"engine": "codex", "session_id": "CODEX-SID"})
        real_update(fn)

    monkeypatch.setattr(daemon, "update_registry", racing)

    daemon.snapshot_once()

    entry = snap["registry"]["15569"]
    assert (entry["engine"], entry["session_id"]) == ("codex", "CODEX-SID"), (
        "wrote a new engine over a session id that arrived concurrently"
    )
