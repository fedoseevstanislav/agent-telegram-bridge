"""Context cues use real root-rollout metadata, never its private message text."""
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from bridge import cli, codex_ctx, common, daemon


TID = "7001"
READER_TID = "7002"
SID = "00000000-0000-4000-8000-000000000001"
CUE = "[tg-bridge] Compaction is complete. Re-read your seat's context."
LEAD_SEATS = [
    ("seat-c", "Team Seat C"),
    ("seat-d", "Team Seat D"),
    ("seat-e", "Team Seat E"),
]


def append(path, kind="compacted", **extra):
    with path.open("a") as f:
        f.write(json.dumps({"type": kind, "timestamp": "2026-10-02T23:48:19.717Z",
                            **extra}) + "\n")


@pytest.fixture
def seat(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(daemon, "_CONTEXT_CUE_PROBLEMS", set())
    config = {
        "cue": CUE,
        "name_prefixes": ["Team", "Research", "Planning"],
        "seats": [
            {"folder": str(tmp_path / "seat-a")},
            {"folder": str(tmp_path / "seat-b"),
             "rollout_cwds": [str(tmp_path / "seat-b"), str(tmp_path / "runtime-b")]},
            *({"folder": str(tmp_path / folder), "name": name} for folder, name in LEAD_SEATS),
        ],
    }
    Path(common.state_path("codex-context-cue.json")).write_text(json.dumps(config))
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    rollout = sessions / f"rollout-2026-09-22T19-34-05-{SID}.jsonl"
    cwd = os.path.expanduser("~/seat-a")
    for folder, tid in (("seat-a", TID), ("seat-b", READER_TID)):
        seat_folder = Path(os.path.expanduser(f"~/{folder}"))
        seat_folder.mkdir(exist_ok=True)
        (seat_folder / ".tg-bridge-topic").write_text(tid + "\n")
    append(rollout, "session_meta", payload={"id": SID, "cwd": cwd})
    proc = tmp_path / "proc"
    fd = proc / "123" / "fd"
    fd.mkdir(parents=True)
    (proc / "123" / "stat").write_text("123 (codex) S 1 " + "0 " * 20)
    (fd / "4").symlink_to(rollout)
    monkeypatch.setattr(codex_ctx, "SESSIONS_DIR", str(sessions))
    monkeypatch.setattr(codex_ctx, "PROC_DIR", str(proc))
    monkeypatch.setattr(daemon, "pane_pid", lambda pane: 123)
    monkeypatch.setattr(daemon, "pane_alive", lambda pane: True)
    monkeypatch.setattr(daemon, "engine_of_pane", lambda pane: "codex")
    monkeypatch.setattr(daemon, "log", lambda *a: None)
    calls = []

    def type_line(pane, text, **kwargs):
        if kwargs["still_ok"]():
            calls.append((pane, text))
            return "sent"
        return "abandoned"

    monkeypatch.setattr(daemon, "type_line", type_line)
    common.write_registry({TID: {"name": "Team Seat A", "cwd": cwd, "pane": "%fake", "engine": "codex",
                                    "session_id": SID}})
    return rollout, calls


def poll(tid=TID):
    info = common.read_registry()[tid]
    daemon._codex_context_cue(tid, info, info["pane"])


@pytest.fixture(params=LEAD_SEATS, ids=[folder for folder, _ in LEAD_SEATS])
def lead_seat(seat, request):
    rollout, calls = seat
    folder, name = request.param
    cwd = os.path.expanduser(f"~/{folder}")
    Path(cwd).mkdir(exist_ok=True)
    (Path(cwd) / ".tg-bridge-topic").write_text(TID + "\n")
    rollout.write_text(json.dumps({"type": "session_meta", "payload": {
        "id": SID, "cwd": cwd}}) + "\n")
    common.update_registry(lambda reg: reg[TID].update(cwd=cwd, name=name))
    return rollout, calls


def test_lead_fixed_cue_baselines_dedupes_and_retries(lead_seat, monkeypatch):
    rollout, calls = lead_seat
    append(rollout)  # New registration does not revive historical compactions.
    poll()
    assert calls == []
    baseline = common.read_registry()[TID]["codex_compaction_cursor"]
    append(rollout, payload={"text": "private data must never be typed"})
    real_type_line = daemon.type_line
    monkeypatch.setattr(daemon, "type_line", lambda *a, **k: "swallowed")
    poll()
    assert common.read_registry()[TID]["codex_compaction_cursor"] == baseline
    monkeypatch.setattr(daemon, "type_line", real_type_line)
    poll()
    poll()  # Reloaded registry receipt also covers normal daemon restarts.
    assert calls == [("%fake", CUE)]
    assert "private data" not in calls[0][1]


@pytest.mark.parametrize("mismatch", [
    "name", "other-lead-name", "root-name", "folder", "launch-folder", "sid",
    "subagent", "topic", "marker", "ended", "parked", "feed", "closed",
])
def test_lead_refuses_unrelated_stale_and_native_sessions(lead_seat, mismatch):
    rollout, calls = lead_seat
    poll()
    baseline = common.read_registry()[TID]["codex_compaction_cursor"]
    append(rollout)
    row = common.read_registry()[TID]
    if mismatch in ("name", "other-lead-name", "root-name"):
        name = {"name": "Team utility", "root-name": "Team Seat A"}.get(
            mismatch, next(name for _, name in LEAD_SEATS if name != row["name"]))
        common.update_registry(lambda reg: reg[TID].update(name=name))
    elif mismatch in ("folder", "launch-folder", "subagent"):
        rows = rollout.read_text().splitlines()
        meta = json.loads(rows[0])
        if mismatch == "subagent":
            meta["payload"]["parent_thread_id"] = SID
        else:
            alias = row["cwd"] + "-other"
            meta["payload"]["cwd"] = alias
            if mismatch == "folder":
                Path(alias).mkdir(exist_ok=True)
                (Path(alias) / ".tg-bridge-topic").write_text(TID + "\n")
                common.update_registry(lambda reg: reg[TID].update(cwd=alias))
        rollout.write_text(json.dumps(meta) + "\n" + "\n".join(rows[1:]) + "\n")
    elif mismatch == "sid":
        common.update_registry(lambda reg: reg[TID].update(session_id="stale"))
    elif mismatch == "topic":
        (Path(row["cwd"]) / ".tg-bridge-topic").write_text("99999\n")
    elif mismatch == "marker":
        (Path(row["cwd"]) / ".tg-bridge-topic").unlink()
    else:
        common.update_registry(lambda reg: reg[TID].update({mismatch: True}))
    poll()
    assert calls == []
    assert common.read_registry()[TID]["codex_compaction_cursor"] == baseline


@pytest.mark.parametrize("gate", ["before-printable", "before-enter"])
@pytest.mark.parametrize("change", ["name", "pane", "sid", "pid", "topic", "rollout", "subagent", "engine", "parked"])
def test_lead_rebinding_at_terminal_gate_abandons_cue(lead_seat, monkeypatch, gate, change):
    rollout, calls = lead_seat
    poll()
    baseline = common.read_registry()[TID]["codex_compaction_cursor"]
    append(rollout)

    def rebind(pane, text, **kwargs):
        if gate == "before-enter":
            assert kwargs["still_ok"]()
        if change in ("name", "pane", "sid", "parked"):
            field, value = {"name": ("name", "Team utility"), "pane": ("pane", "%other"),
                            "sid": ("session_id", "stale"), "parked": ("parked", True)}[change]
            common.update_registry(lambda reg: reg[TID].update({field: value}))
        elif change == "pid":
            monkeypatch.setattr(daemon, "pane_pid", lambda pane: 456)
        elif change == "topic":
            cwd = common.read_registry()[TID]["cwd"]
            (Path(cwd) / ".tg-bridge-topic").write_text("99999\n")
        elif change in ("rollout", "subagent"):
            rows = rollout.read_text().splitlines()
            meta = json.loads(rows[0])
            meta["payload"]["id" if change == "rollout" else "parent_thread_id"] = "stale"
            rollout.write_text(json.dumps(meta) + "\n" + "\n".join(rows[1:]) + "\n")
        else:
            monkeypatch.setattr(daemon, "engine_of_pane", lambda pane: "claude")
        if kwargs["still_ok"]():
            calls.append((pane, text))
            return "sent"
        return "abandoned"

    monkeypatch.setattr(daemon, "type_line", rebind)
    poll()
    assert calls == []
    assert common.read_registry()[TID]["codex_compaction_cursor"] == baseline


def test_lead_folder_registration_with_other_team_name_never_wakes(lead_seat, monkeypatch):
    rollout, calls = lead_seat
    cwd = common.read_registry()[TID]["cwd"]
    monkeypatch.chdir(cwd)
    monkeypatch.setenv("TMUX_PANE", "%fake")
    monkeypatch.setattr(cli, "api", lambda *a, **kw: {"message_thread_id": 99999})
    monkeypatch.setattr(cli, "theme_icon_id", lambda *a: None)
    cli.cmd_register({"chat_id": 1, "bot_token": "synthetic"},
                     SimpleNamespace(name="Team utility", feed=False, bind=True))
    common.update_registry(lambda reg: reg["99999"].update(session_id=SID))
    poll("99999")
    append(rollout)
    poll("99999")
    assert calls == []
    assert "codex_compaction_cursor" not in common.read_registry()["99999"]


def test_actual_loop_types_fixed_cue_at_next_30_second_poll(seat, monkeypatch):
    rollout, calls = seat
    sleeps = []

    class Stop(BaseException):
        pass

    def sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 2:
            append(rollout, payload={"text": "untrusted instructions must never be typed"})
        if len(sleeps) == 3:
            raise Stop

    monkeypatch.setattr(daemon.time, "sleep", sleep)
    monkeypatch.setattr(daemon, "load_autocf_exempt", lambda: set())
    with pytest.raises(Stop):
        daemon.warning_loop({})
    assert sleeps[:2] == [30, 30]
    assert calls == [("%fake", CUE)]
    assert "untrusted instructions" not in calls[0][1]


@pytest.mark.parametrize("folder,launch_folder", [
    ("seat-a", "seat-a"),
    ("seat-b", "seat-b"),
    # A registered folder may differ from its existing runtime cwd.
    ("seat-b", "runtime-b"),
])
def test_both_seats_dedupe_success_after_persisted_receipt(seat, folder, launch_folder):
    rollout, calls = seat
    cwd = os.path.expanduser(f"~/{folder}")
    rollout.write_text(json.dumps({"type": "session_meta", "payload": {
        "id": SID, "cwd": os.path.expanduser(f"~/{launch_folder}")}}) + "\n")
    name = "Team Seat B" if folder == "seat-b" else "Team Seat A"
    common.update_registry(lambda reg: reg[TID].update(cwd=cwd, name=name))
    tid = READER_TID if folder == "seat-b" else TID
    if tid != TID:
        common.update_registry(lambda reg: reg.update({tid: reg.pop(TID)}))
    append(rollout)  # historical compaction is only baselined
    poll(tid)
    assert calls == []
    append(rollout)
    poll(tid)
    poll(tid)  # registry is reloaded; no in-memory dedupe state survives a restart
    assert len(calls) == 1
    append(rollout)
    poll(tid)
    assert len(calls) == 2


@pytest.mark.parametrize("flag", ["ended", "parked", "feed", "closed"])
def test_parked_ended_feed_closed_never_wake(seat, flag):
    rollout, calls = seat
    poll()
    append(rollout)
    common.update_registry(lambda reg: reg[TID].update({flag: True}))
    poll()
    assert calls == []


@pytest.mark.parametrize("mismatch", ["cwd", "sid", "subagent", "ambiguous", "no-fd"])
def test_refuses_unrelated_or_unverified_root_association(seat, mismatch, monkeypatch):
    rollout, calls = seat
    poll()
    append(rollout)
    if mismatch == "cwd":
        common.update_registry(lambda reg: reg[TID].update(cwd="/unrelated"))
    elif mismatch == "sid":
        common.update_registry(lambda reg: reg[TID].update(session_id="wrong"))
    elif mismatch == "subagent":
        rows = rollout.read_text().splitlines()
        meta = json.loads(rows[0])
        meta["payload"]["parent_thread_id"] = SID
        rollout.write_text(json.dumps(meta) + "\n" + "\n".join(rows[1:]) + "\n")
    elif mismatch == "ambiguous":
        other = rollout.with_name("rollout-second.jsonl")
        other.write_text(rollout.read_text())
        (Path(codex_ctx.PROC_DIR) / "123" / "fd" / "5").symlink_to(other)
        monkeypatch.setattr(codex_ctx, "recorded_rollout_for_pane", lambda pid: rollout)
    else:
        (Path(codex_ctx.PROC_DIR) / "123" / "fd" / "4").unlink()
        # A sole cwd candidate is insufficient authority to type a cue.
    poll()
    assert calls == []


def test_partial_record_and_message_mentions_are_not_events(seat):
    rollout, calls = seat
    poll()
    append(rollout, "event_msg", payload={"type": "compacted"})
    with rollout.open("a") as f:
        f.write('{"type":"compacted"')
    poll()
    assert calls == []
    with rollout.open("a") as f:
        f.write('}\n')
    poll()
    assert len(calls) == 1


def test_failed_typing_never_advances_compaction_receipt(seat, monkeypatch):
    rollout, calls = seat
    poll()
    baseline = common.read_registry()[TID]["codex_compaction_cursor"]
    append(rollout)
    monkeypatch.setattr(daemon, "type_line", lambda *a, **k: "swallowed")
    poll()
    assert common.read_registry()[TID]["codex_compaction_cursor"] == baseline
    assert calls == []


def test_crash_after_typing_before_persistence_may_repeat_fixed_cue(seat, monkeypatch):
    rollout, calls = seat
    poll()
    append(rollout)

    class Crash(BaseException):
        pass

    real_update = daemon.update_registry
    monkeypatch.setattr(daemon, "update_registry", lambda fn: (_ for _ in ()).throw(Crash()))
    with pytest.raises(Crash):
        poll()
    monkeypatch.setattr(daemon, "update_registry", real_update)
    poll()
    assert calls == [("%fake", CUE)] * 2
    poll()
    assert len(calls) == 2


def test_binding_change_inside_typing_abandons_cue(seat, monkeypatch):
    rollout, calls = seat
    poll()
    append(rollout)

    def rebind(pane, text, **kwargs):
        common.update_registry(lambda reg: reg[TID].update(pane="%replacement"))
        assert not kwargs["still_ok"]()
        return "abandoned"

    monkeypatch.setattr(daemon, "type_line", rebind)
    poll()
    assert calls == []


@pytest.mark.parametrize("gate", ["before-printable", "before-enter"])
def test_live_root_change_with_stale_registry_abandons_cue(seat, monkeypatch, gate):
    rollout, calls = seat
    poll()
    baseline = common.read_registry()[TID]["codex_compaction_cursor"]
    append(rollout)
    new_sid = "00000000-0000-4000-8000-000000000002"
    other = rollout.with_name(f"rollout-replacement-{new_sid}.jsonl")
    append(other, "session_meta", payload={"id": new_sid,
                                         "cwd": os.path.expanduser("~/seat-a")})
    fd = Path(codex_ctx.PROC_DIR) / "123" / "fd" / "4"

    def replace_live_root(pane, text, **kwargs):
        if gate == "before-enter":
            assert kwargs["still_ok"]()  # the first gate sees the original session
        fd.unlink()
        fd.symlink_to(other)
        if kwargs["still_ok"]():
            calls.append((pane, text))
            return "sent"
        return "abandoned"

    monkeypatch.setattr(daemon, "type_line", replace_live_root)
    poll()
    assert common.read_registry()[TID]["session_id"] == SID  # registry is still stale
    assert common.read_registry()[TID]["codex_compaction_cursor"] == baseline
    assert calls == []


def test_unrelated_topic_from_configured_folder_never_wakes(seat):
    rollout, calls = seat
    poll()
    common.update_registry(lambda reg: reg.update({"99999": dict(reg[TID])}))
    append(rollout)
    poll("99999")
    assert calls == []


def test_current_topic_cannot_borrow_the_other_seat_folder(seat):
    rollout, calls = seat
    cwd = os.path.expanduser("~/seat-b")
    rollout.write_text(json.dumps({"type": "session_meta", "payload": {
        "id": SID, "cwd": cwd}}) + "\n")
    common.update_registry(lambda reg: reg[TID].update(cwd=cwd))
    poll()
    append(rollout)
    poll()
    assert calls == []


def test_topic_binding_change_inside_typing_abandons_cue(seat, monkeypatch):
    rollout, calls = seat
    poll()
    baseline = common.read_registry()[TID]["codex_compaction_cursor"]
    append(rollout)

    def rebind_topic(pane, text, **kwargs):
        assert kwargs["still_ok"]()
        cwd = common.read_registry()[TID]["cwd"]
        (Path(cwd) / ".tg-bridge-topic").write_text("99999\n")
        assert not kwargs["still_ok"]()
        return "abandoned"

    monkeypatch.setattr(daemon, "type_line", rebind_topic)
    poll()
    assert common.read_registry()[TID]["codex_compaction_cursor"] == baseline
    assert calls == []


def test_missing_topic_binding_fails_closed(seat):
    rollout, calls = seat
    poll()
    cwd = common.read_registry()[TID]["cwd"]
    (Path(cwd) / ".tg-bridge-topic").unlink()
    append(rollout)
    poll()
    assert calls == []


def test_truncated_rollout_rebaselines_without_historical_wake(seat):
    rollout, calls = seat
    append(rollout, payload={"text": "x" * 200})
    poll()
    meta = rollout.read_text().splitlines()[0]
    rollout.write_text(meta + "\n")
    append(rollout)
    poll()
    assert calls == []
    append(rollout)
    poll()
    assert len(calls) == 1


@pytest.mark.parametrize("name", [None, "", 4, [], "Unrelated utility"])
def test_non_member_name_fails_closed(seat, name):
    rollout, calls = seat
    common.update_registry(lambda reg: reg[TID].update(name=name))
    poll()
    append(rollout)
    poll()
    assert calls == []
    assert "codex_compaction_cursor" not in common.read_registry()[TID]


def test_normal_unrelated_registration_cannot_gain_membership(seat, monkeypatch):
    rollout, calls = seat
    cwd = common.read_registry()[TID]["cwd"]
    monkeypatch.chdir(cwd)
    monkeypatch.setenv("TMUX_PANE", "%fake")
    monkeypatch.setattr(cli, "api", lambda *a, **kw: {"message_thread_id": 99999})
    monkeypatch.setattr(cli, "theme_icon_id", lambda *a: None)
    cli.cmd_register({"chat_id": 1, "bot_token": "synthetic"},
                     SimpleNamespace(name="Unrelated utility", feed=False, bind=True))
    common.update_registry(lambda reg: reg["99999"].update(session_id=SID))
    assert (Path(cwd) / ".tg-bridge-topic").read_text() == "99999"
    poll("99999")
    append(rollout)
    poll("99999")
    assert calls == []
    assert "codex_compaction_cursor" not in common.read_registry()["99999"]


@pytest.mark.parametrize("gate", ["before-printable", "before-enter"])
@pytest.mark.parametrize("name", ["Unrelated utility", "Team successor"])
def test_membership_change_at_terminal_gate_abandons_cue(seat, monkeypatch, gate, name):
    rollout, calls = seat
    poll()
    baseline = common.read_registry()[TID]["codex_compaction_cursor"]
    append(rollout)

    def rename(pane, text, **kwargs):
        if gate == "before-enter":
            assert kwargs["still_ok"]()
        common.update_registry(lambda reg: reg[TID].update(name=name))
        if kwargs["still_ok"]():
            calls.append((pane, text))
            return "sent"
        return "abandoned"

    monkeypatch.setattr(daemon, "type_line", rename)
    poll()
    assert calls == []
    assert common.read_registry()[TID]["codex_compaction_cursor"] == baseline


@pytest.mark.parametrize("gate", ["before-printable", "before-enter"])
def test_changed_pane_pid_with_same_rollout_abandons_cue(seat, monkeypatch, gate):
    rollout, calls = seat
    poll()
    baseline = common.read_registry()[TID]["codex_compaction_cursor"]
    append(rollout)
    replacement = Path(codex_ctx.PROC_DIR) / "456"
    (replacement / "fd").mkdir(parents=True)
    (replacement / "stat").write_text("456 (codex) S 1 " + "0 " * 20)
    (replacement / "fd" / "4").symlink_to(rollout)

    def replace_pid(pane, text, **kwargs):
        if gate == "before-enter":
            assert kwargs["still_ok"]()
        monkeypatch.setattr(daemon, "pane_pid", lambda pane: 456)
        if kwargs["still_ok"]():
            calls.append((pane, text))
            return "sent"
        return "abandoned"

    monkeypatch.setattr(daemon, "type_line", replace_pid)
    poll()
    assert calls == []
    assert common.read_registry()[TID]["codex_compaction_cursor"] == baseline


@pytest.mark.parametrize("problem", [
    "absent", "unreadable", "bad-json", "nested-json", "encoding", "top-level", "cue-missing",
    "cue-type", "cue-empty", "cue-whitespace", "prefixes-missing", "prefixes-type",
    "prefix-type", "prefix-empty", "seats-missing", "seats-type", "seat-type",
    "folder-missing", "folder-type", "folder-relative", "folder-nul",
    "cwds-type", "cwd-type", "cwd-relative", "cwd-nul", "name-type",
])
def test_invalid_config_disables_cue_without_poll_errors(seat, monkeypatch, problem):
    rollout, calls = seat
    poll()
    baseline = common.read_registry()[TID]["codex_compaction_cursor"]
    append(rollout)
    path = Path(common.state_path("codex-context-cue.json"))
    config = json.loads(path.read_text())
    if problem == "absent":
        path.unlink()
    elif problem == "unreadable":
        import builtins

        real_open = builtins.open

        def unreadable(file, *args, **kwargs):
            if os.fspath(file) == str(path):
                raise PermissionError(13, "synthetic denied read")
            return real_open(file, *args, **kwargs)

        monkeypatch.setattr(builtins, "open", unreadable)
    elif problem == "bad-json":
        path.write_text('{"cue":')
    elif problem == "nested-json":
        # The C decoder can exceed Python's recursion limit; use its deeper failure boundary.
        path.write_text("[" * 10000 + "0" + "]" * 10000)
    elif problem == "encoding":
        path.write_bytes(b"\xff")
    else:
        field, value = {
            "cue-type": ("cue", 7), "cue-empty": ("cue", ""),
            "cue-whitespace": ("cue", " \n\t"), "prefixes-type": ("name_prefixes", "Team"),
            "prefix-type": ("name_prefixes", [7]), "prefix-empty": ("name_prefixes", [""]),
            "seats-type": ("seats", {}), "seat-type": ("seats", [7]),
            "folder-type": ("folder", []), "folder-relative": ("folder", "seat-a"),
            "folder-nul": ("folder", "/synthetic\0"), "cwds-type": ("rollout_cwds", "/seat-a"),
            "cwd-type": ("rollout_cwds", [7]), "cwd-relative": ("rollout_cwds", ["seat-a"]),
            "cwd-nul": ("rollout_cwds", ["/synthetic\0"]), "name-type": ("name", 7),
        }.get(problem, (None, None))
        if problem == "top-level":
            config = []
        elif problem.endswith("-missing"):
            key = {"cue-missing": "cue", "prefixes-missing": "name_prefixes",
                   "seats-missing": "seats", "folder-missing": "folder"}[problem]
            target = config["seats"][0] if key == "folder" else config
            del target[key]
        else:
            target = config["seats"][0] if field in ("folder", "rollout_cwds", "name") else config
            target[field] = value
        path.write_text(json.dumps(config))

    logs = []
    monkeypatch.setattr(daemon, "log", logs.append)
    sleeps = []

    class Stop(BaseException):
        pass

    def sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 3:
            raise Stop

    # Direct calls must also fail closed, rather than relying on the loop's exception handler.
    poll()
    poll()
    monkeypatch.setattr(daemon.time, "sleep", sleep)
    monkeypatch.setattr(daemon, "load_autocf_exempt", lambda: set())
    with pytest.raises(Stop):
        daemon.warning_loop({})
    assert calls == []
    assert common.read_registry()[TID]["codex_compaction_cursor"] == baseline
    assert not any("reminder error" in line for line in logs)
    assert logs == [] if problem == "absent" else len(logs) == 1


def test_config_reloads_and_cue_is_verbatim(seat):
    rollout, calls = seat
    poll()
    path = Path(common.state_path("codex-context-cue.json"))
    config = json.loads(path.read_text())
    config["cue"] = "  [tg-bridge] Synthetic cue: read context.\nKeep this text verbatim.  "
    path.write_text(json.dumps(config))
    append(rollout, payload={"text": "private transcript content"})
    poll()
    assert calls == [("%fake", config["cue"])]

    config["name_prefixes"] = ["Other"]
    path.write_text(json.dumps(config))
    append(rollout)
    poll()
    assert len(calls) == 1
    config["name_prefixes"] = ["Team"]
    path.write_text(json.dumps(config))
    poll()
    assert len(calls) == 2


@pytest.mark.parametrize("prefix", ["Team", "Research", "Planning"])
def test_each_configured_prefix_is_eligible(seat, prefix):
    rollout, calls = seat
    common.update_registry(lambda reg: reg[TID].update(name=f"{prefix} Seat A"))
    poll()
    append(rollout)
    poll()
    assert calls == [("%fake", CUE)]


def test_distinct_config_problems_log_once_each(seat, monkeypatch):
    path = Path(common.state_path("codex-context-cue.json"))
    valid = path.read_text()
    logs = []
    monkeypatch.setattr(daemon, "log", logs.append)
    for text in ('{"cue":', '[]', valid, '{"cue":', '[]'):
        path.write_text(text)
        poll()
    assert logs == ["Codex context cue disabled: invalid JSON",
                    "Codex context cue disabled: expected an object"]
