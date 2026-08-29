"""Unit tests for the unified context readout (#158).

Measured across idle registered panes: nearly every one answered /ctx with "no fresh context
data" while its status line displayed the number the whole time. statusline.sh
rewrites context/<pane>.json on every RENDER — each turn, not only when the number changes —
so `ts` measures activity. An idle session's record stops being rewritten while its
percentage stays exactly what it was, and CTX_STALE was discarding it as if it were wrong.

Age is therefore not the test; validity is. These tests pin both, plus the ordering that
keeps a codex pane from ever reading a leftover claude record."""

import json
import time

from bridge import daemon


def _write_ctx(tmp_path, monkeypatch, pane, **fields):
    """Put a statusline record on disk for `pane`, with sane defaults."""
    root = tmp_path / "state"
    (root / "context").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(daemon, "state_path",
                        lambda *parts, _r=root: str(_r.joinpath(*parts)))
    rec = {"pane": pane, "session_id": "abc-123", "pct": 42, "cost": 1.5, "ts": time.time()}
    rec.update(fields)
    (root / "context" / f"{pane.lstrip('%')}.json").write_text(json.dumps(rec))
    return rec


# ---- what makes a record believable ------------------------------------------

def test_record_with_a_session_id_and_a_sane_pct_is_valid():
    assert daemon._ctx_record_valid({"session_id": "abc", "pct": 42})
    assert daemon._ctx_record_valid({"session_id": "abc", "pct": 0})    # a brand-new session
    assert daemon._ctx_record_valid({"session_id": "abc", "pct": 100})


def test_the_jq_less_zeroed_dump_is_rejected():
    # THE record CTX_STALE could never catch: statusline.sh emitted this on EVERY render when
    # jq was missing from the PATH, so it was permanently fresh and permanently wrong.
    assert not daemon._ctx_record_valid({"pane": "%5", "session_id": "", "pct": 0, "cost": 0})


def test_structurally_broken_records_are_rejected():
    for bad in ({"session_id": "abc"},                      # no pct at all
                {"session_id": "abc", "pct": None},
                {"session_id": "abc", "pct": "n/a"},
                {"session_id": "abc", "pct": 101},          # out of range
                {"session_id": "abc", "pct": -1},
                {"pct": 42},                                # no session id
                None, "", []):
        assert not daemon._ctx_record_valid(bad), bad


# ---- read_context ------------------------------------------------------------

def test_fresh_record_is_returned(tmp_path, monkeypatch):
    _write_ctx(tmp_path, monkeypatch, "%1")
    assert daemon.read_context("%1")["pct"] == 42


def test_aged_record_is_gated_by_default_but_available_on_request(tmp_path, monkeypatch):
    # The default protects session_id_for_pane, which must not persist a stale id from a
    # prior session in a reused pane. /ctx wants the number and passes max_age=None.
    _write_ctx(tmp_path, monkeypatch, "%1", ts=time.time() - 24 * 3600)
    assert daemon.read_context("%1") is None
    assert daemon.read_context("%1", max_age=None)["pct"] == 42


def test_invalid_record_is_rejected_at_any_age(tmp_path, monkeypatch):
    # Validity is not a freshness question — max_age=None must not resurrect a zeroed dump.
    _write_ctx(tmp_path, monkeypatch, "%1", session_id="", pct=0)
    assert daemon.read_context("%1") is None
    assert daemon.read_context("%1", max_age=None) is None


def test_missing_and_malformed_files_are_none(tmp_path, monkeypatch):
    _write_ctx(tmp_path, monkeypatch, "%1")
    assert daemon.read_context("%9", max_age=None) is None          # never rendered
    (tmp_path / "state" / "context" / "1.json").write_text("{not json")
    assert daemon.read_context("%1", max_age=None) is None


# ---- context_for ordering ----------------------------------------------------

def test_codex_pane_never_reads_the_claude_record(monkeypatch):
    # THE regression this ordering exists for: codex panes can retain weeks-old Claude records
    # whose percentages differ from live Codex readings. CTX_STALE was
    # the only thing hiding them, so accepting records at any age without this would report
    # the wrong engine's number with full confidence.
    monkeypatch.setattr(daemon, "read_context",
                        lambda pane, max_age=None: {"pct": 43, "cost": 1.0})
    monkeypatch.setattr(daemon, "_codex_ctx_dict", lambda pane: {"pct": 17})

    monkeypatch.setattr(daemon, "engine_of_pane", lambda pane: "codex")
    assert daemon.context_for("%20") == {"pct": 17}
    assert daemon.context_for("%20", "codex") == {"pct": 17}        # caller-supplied engine


def test_claude_pane_tries_the_gated_read_before_waiving_age(monkeypatch):
    # Order matters: the gated read is free, and only its miss justifies the pane/proc work
    # that has to bound an aged record.
    seen = []
    monkeypatch.setattr(daemon, "engine_of_pane", lambda pane: "claude")
    monkeypatch.setattr(daemon, "pane_alive", lambda pane: True)
    monkeypatch.setattr(daemon, "_pane_start_time", lambda pane: 1000.0)
    monkeypatch.setattr(
        daemon, "read_context",
        lambda pane, max_age=daemon.CTX_STALE: seen.append(max_age) or (
            None if max_age is not None else {"pct": 42, "cost": 132.85, "ts": 2000.0}))

    assert daemon.context_for("%1") == {"pct": 42, "cost": 132.85, "ts": 2000.0}
    assert seen == [daemon.CTX_STALE, None]                # gated first, then waived


def test_supplied_engine_skips_the_fleet_lookup(monkeypatch):
    # warning_loop resolves the engine for every topic on every poll; re-resolving it inside
    # context_for added one tmux subprocess per claude topic per poll.
    def boom(pane):
        raise AssertionError("engine_of_pane must not be called when the caller knows it")
    monkeypatch.setattr(daemon, "engine_of_pane", boom)
    monkeypatch.setattr(daemon, "read_context", lambda pane, max_age=None: {"pct": 31})

    assert daemon.context_for("%7", "claude") == {"pct": 31}


def test_no_record_is_no_data(monkeypatch):
    monkeypatch.setattr(daemon, "engine_of_pane", lambda pane: "claude")
    monkeypatch.setattr(daemon, "read_context", lambda pane, max_age=None: None)
    assert daemon.context_for("%5") is None


def test_unknown_engine_stays_on_the_claude_path(monkeypatch):
    # engine_of_pane returns None for a pane missing from the live fleet; that must keep the
    # claude behaviour rather than silently routing to the codex reader.
    monkeypatch.setattr(daemon, "engine_of_pane", lambda pane: None)
    monkeypatch.setattr(daemon, "read_context", lambda pane, max_age=None: {"pct": 31})
    monkeypatch.setattr(daemon, "_codex_ctx_dict", lambda pane: {"pct": 77})

    assert daemon.context_for("%7") == {"pct": 31}


# ---- the snapshot loop must NOT inherit the relaxed rule ----------------------

def test_session_id_snapshot_still_requires_a_fresh_record(tmp_path, monkeypatch):
    # A reused pane id left over from a prior session must never be persisted into the
    # registry as the live session — that is why read_context keeps a gated default.
    _write_ctx(tmp_path, monkeypatch, "%1", session_id="old-session", ts=time.time() - 3600)
    assert daemon.session_id_for_pane("%1", "claude") is None

    _write_ctx(tmp_path, monkeypatch, "%1", session_id="live-session")
    assert daemon.session_id_for_pane("%1", "claude") == "live-session"


# ---- the age waiver needs a bound of its own ---------------------------------
#
# CTX_STALE was incidentally providing one: a record could outlive its writer by at most 600
# seconds. Waiving age without replacing that made a dead pane's record answer /ctx forever,
# and let a failed session-B write leave session A's number standing indefinitely (Codex
# round 2 of PR #159). The replacement is tighter: the pane must be alive AND the record must
# have been written after the process now in that pane started.

def _claude_pane(monkeypatch, *, aged, alive=True, started=1000.0):
    monkeypatch.setattr(daemon, "engine_of_pane", lambda pane: "claude")
    monkeypatch.setattr(daemon, "pane_alive", lambda pane: alive)
    monkeypatch.setattr(daemon, "_pane_start_time", lambda pane: started)
    monkeypatch.setattr(
        daemon, "read_context",
        lambda pane, max_age=daemon.CTX_STALE: None if max_age is not None else aged)


def test_aged_record_is_accepted_when_this_process_wrote_it(monkeypatch):
    _claude_pane(monkeypatch, aged={"pct": 42, "ts": 2000.0}, started=1000.0)
    assert daemon.context_for("%1") == {"pct": 42, "ts": 2000.0}


def test_aged_record_from_a_previous_occupant_is_refused(monkeypatch):
    # Pane ids get recycled, and a fresh `claude`/`--resume` in the pane is a new process.
    _claude_pane(monkeypatch, aged={"pct": 42, "ts": 500.0}, started=1000.0)
    assert daemon.context_for("%1") is None


def test_dead_pane_reports_nothing(monkeypatch):
    # THE regression: /ctx on a closed session used to go quiet after CTX_STALE; without this
    # it would answer with the dead session's last number forever.
    _claude_pane(monkeypatch, aged={"pct": 42, "ts": 2000.0}, alive=False)
    assert daemon.context_for("%1") is None


def test_unknowable_start_time_fails_closed(monkeypatch):
    _claude_pane(monkeypatch, aged={"pct": 42, "ts": 2000.0}, started=None)
    assert daemon.context_for("%1") is None


def test_fresh_record_skips_the_liveness_and_proc_work(monkeypatch):
    # The hot path must stay free of tmux and /proc calls — warning_loop runs it per topic
    # per poll.
    def boom(pane):
        raise AssertionError("fresh record must answer without probing the pane")
    monkeypatch.setattr(daemon, "engine_of_pane", lambda pane: "claude")
    monkeypatch.setattr(daemon, "pane_alive", boom)
    monkeypatch.setattr(daemon, "_pane_start_time", boom)
    monkeypatch.setattr(daemon, "read_context",
                        lambda pane, max_age=daemon.CTX_STALE: {"pct": 25})

    assert daemon.context_for("%0") == {"pct": 25}


def _stat_line(pid, tpgid, starttime, comm="claude (worker) x"):
    """A /proc/<pid>/stat line. After the last ') ', fields[n] is overall field n+3, so
    fields[5] is tpgid (field 8) and fields[19] is starttime (field 22)."""
    tail = ["S", "1", str(pid), str(pid), "1025", str(tpgid)]   # idx 0-5
    tail += ["0"] * 13                                          # idx 6-18
    tail += [str(starttime)] + ["0"] * 30                       # idx 19 onward
    return f"{pid} ({comm}) " + " ".join(tail) + "\n"


def _fake_proc(monkeypatch, tmp_path, stats, btime=1700000000):
    """Route /proc/<pid>/stat and /proc/stat reads at the given synthetic contents."""
    files = {}
    for pid, text in stats.items():
        p = tmp_path / f"stat-{pid}"
        p.write_text(text)
        files[f"/proc/{pid}/stat"] = p
    ps = tmp_path / "proc-stat"
    ps.write_text(f"cpu 1 2 3\nbtime {btime}\nprocesses 9\n")
    files["/proc/stat"] = ps

    real_open = open

    def fake_open(path, *a, **k):
        target = files.get(str(path))
        if target is not None:
            return real_open(target, *a, **k)
        if str(path).startswith("/proc/"):
            raise FileNotFoundError(path)
        return real_open(path, *a, **k)

    monkeypatch.setattr("builtins.open", fake_open)
    monkeypatch.setattr(daemon.os, "sysconf", lambda name: 100)


def test_pane_start_time_parses_proc(monkeypatch, tmp_path):
    # comm can contain spaces and parentheses, so the parse anchors on the LAST ") ".
    _fake_proc(monkeypatch, tmp_path, {42: _stat_line(42, 42, 777777)})
    monkeypatch.setattr(daemon, "pane_pid", lambda pane: 42)
    assert daemon._pane_start_time("%1") == 1700000000 + 777777 / 100


def test_pane_start_time_follows_the_foreground_process(monkeypatch, tmp_path):
    # THE round-3 finding: tmux's #{pane_pid} is the FIRST process in the pane. For a manually
    # registered session that is a persistent shell older than every record ever written there,
    # so timing the shell left session A trusted for the shell's whole lifetime — weaker than
    # the 600s cutoff it replaced. Time the foreground process group instead.
    _fake_proc(monkeypatch, tmp_path, {
        100: _stat_line(100, 200, 111111, comm="bash"),     # shell: old
        200: _stat_line(200, 200, 999999),                  # claude: started much later
    })
    monkeypatch.setattr(daemon, "pane_pid", lambda pane: 100)
    assert daemon._pane_start_time("%1") == 1700000000 + 999999 / 100


def test_pane_start_time_none_without_a_foreground_group(monkeypatch, tmp_path):
    # tpgid -1 means no controlling terminal: unprovable, so fail closed.
    _fake_proc(monkeypatch, tmp_path, {100: _stat_line(100, -1, 111111)})
    monkeypatch.setattr(daemon, "pane_pid", lambda pane: 100)
    assert daemon._pane_start_time("%1") is None


def test_pane_start_time_none_when_the_foreground_process_is_gone(monkeypatch, tmp_path):
    # It can exit between the pane read and the second /proc open.
    _fake_proc(monkeypatch, tmp_path, {100: _stat_line(100, 200, 111111)})   # no stat for 200
    monkeypatch.setattr(daemon, "pane_pid", lambda pane: 100)
    assert daemon._pane_start_time("%1") is None


def test_pane_start_time_none_without_a_pid(monkeypatch):
    monkeypatch.setattr(daemon, "pane_pid", lambda pane: None)
    assert daemon._pane_start_time("%1") is None


def test_unresolved_engine_does_not_get_the_age_waiver(monkeypatch):
    # An idle shell sitting in a registered pane resolves to engine None. It cannot be shown
    # to be the claude session that wrote the record, so an aged record must not answer.
    _claude_pane(monkeypatch, aged={"pct": 42, "ts": 2000.0}, started=1000.0)
    monkeypatch.setattr(daemon, "engine_of_pane", lambda pane: None)
    assert daemon.context_for("%1") is None
