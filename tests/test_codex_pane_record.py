"""Codex pane records let a sandboxed daemon use a seat-observed rollout (#331)."""

import json
import sys

from bridge import cli, codex_ctx


UUID_A = "01900000-0000-7000-8000-000000000001"
UUID_B = "01900000-0000-7000-8000-000000000002"


def _rollout(path, cwd="/srv/seat"):
    path.write_text(json.dumps({
        "type": "session_meta", "payload": {"cwd": cwd, "thread_source": "user"},
    }) + "\n")
    return str(path)


def _fake_proc(tmp_path, monkeypatch, processes):
    """Render process status, start ticks, and fd links into a fake `/proc` tree."""
    root = tmp_path / "proc"
    root.mkdir()
    for pid, spec in processes.items():
        entry = root / str(pid)
        if "fds" in spec:
            (entry / "fd").mkdir(parents=True)
            for number, target in enumerate(spec["fds"]):
                (entry / "fd" / str(number)).symlink_to(target)
        else:
            entry.mkdir()
        tail = ["S", str(spec["ppid"])] + ["0"] * 17 + [str(spec["start_ticks"])]
        (entry / "stat").write_text(f"{pid} ({spec['name']}) " + " ".join(tail) + "\n")
        (entry / "status").write_text(
            f"Name:\t{spec['name']}\nPPid:\t{spec['ppid']}\n")
    monkeypatch.setattr(codex_ctx, "PROC_DIR", str(root))
    return root


def _sessions(tmp_path, monkeypatch):
    directory = tmp_path / "sessions"
    directory.mkdir()
    monkeypatch.setattr(codex_ctx, "SESSIONS_DIR", str(directory))
    return directory


def test_verified_record_resolves_when_fd_route_is_blind_and_cwd_is_ambiguous(tmp_path, monkeypatch):
    sessions = _sessions(tmp_path, monkeypatch)
    own = _rollout(sessions / f"rollout-test-{UUID_A}.jsonl")
    _rollout(sessions / f"rollout-test-{UUID_B}.jsonl")
    _fake_proc(tmp_path, monkeypatch, {
        20: {"name": "shell", "ppid": 1, "start_ticks": 1234},  # no fd directory
    })

    assert codex_ctx.write_pane_record(20, own)
    assert codex_ctx.rollout_for_pane("20", "/srv/seat") == own


def test_recycled_pid_record_falls_through_to_ambiguous_cwd(tmp_path, monkeypatch):
    sessions = _sessions(tmp_path, monkeypatch)
    own = _rollout(sessions / f"rollout-test-{UUID_A}.jsonl")
    _rollout(sessions / f"rollout-test-{UUID_B}.jsonl")
    proc = _fake_proc(tmp_path, monkeypatch, {
        20: {"name": "shell", "ppid": 1, "start_ticks": 1234},
    })

    assert codex_ctx.write_pane_record(20, own)
    stat = proc / "20" / "stat"
    stat.write_text("20 (shell) S 1 " + "0 " * 17 + "5678\n")

    assert codex_ctx.recorded_rollout_for_pane(20) is None
    assert codex_ctx.rollout_for_pane(20, "/srv/seat") is None


def test_fd_rollout_wins_over_a_conflicting_verified_record(tmp_path, monkeypatch):
    sessions = _sessions(tmp_path, monkeypatch)
    from_fd = _rollout(sessions / f"rollout-test-{UUID_A}.jsonl")
    recorded = _rollout(sessions / f"rollout-test-{UUID_B}.jsonl")
    _fake_proc(tmp_path, monkeypatch, {
        20: {"name": "shell", "ppid": 1, "start_ticks": 1234, "fds": [from_fd]},
    })

    assert codex_ctx.write_pane_record(20, recorded)
    assert codex_ctx.rollout_for_pane(20, "/srv/seat") == from_fd


def test_seat_writes_only_one_root_rollout_from_its_tmux_ancestry(tmp_path, monkeypatch):
    sessions = _sessions(tmp_path, monkeypatch)
    own = _rollout(sessions / f"rollout-test-{UUID_A}.jsonl")
    second = _rollout(sessions / f"rollout-test-{UUID_B}.jsonl")
    processes = {
        10: {"name": "tmux: server", "ppid": 1, "start_ticks": 10},
        20: {"name": "shell", "ppid": 10, "start_ticks": 20},
        30: {"name": "codex", "ppid": 20, "start_ticks": 30, "fds": [own]},
        40: {"name": "sh", "ppid": 30, "start_ticks": 40},
    }
    proc = _fake_proc(tmp_path, monkeypatch, processes)
    monkeypatch.setattr(codex_ctx.os, "getpid", lambda: 40)

    assert codex_ctx.record_current_pane_rollout()
    with open(codex_ctx._pane_record_path(20)) as f:
        record = json.load(f)
    assert record["pid"] == 20
    assert record["start_ticks"] == 20
    assert record["session_id"] == UUID_A
    assert record["rollout"] == own
    assert isinstance(record["ts"], str)

    (proc / "30" / "fd" / "1").symlink_to(second)
    assert not codex_ctx.record_current_pane_rollout()

    for link in (proc / "30" / "fd").iterdir():
        link.unlink()
    assert not codex_ctx.record_current_pane_rollout()


def test_cli_hook_skips_proc_without_tmux_and_does_not_block_commands(monkeypatch):
    called = []
    monkeypatch.setattr(cli, "secure_process_umask", lambda: None)
    monkeypatch.setattr(cli, "cmd_current_topic", lambda _cfg, _args: called.append("command"))
    monkeypatch.setattr(cli.codex_ctx, "record_current_pane_rollout",
                        lambda: (_ for _ in ()).throw(AssertionError("read proc")))
    monkeypatch.setattr(sys, "argv", ["tg-bridge", "current-topic"])

    cli.main()
    assert called == ["command"]

    monkeypatch.setenv("TMUX_PANE", "%20")
    monkeypatch.setattr(cli.codex_ctx, "record_current_pane_rollout",
                        lambda: (_ for _ in ()).throw(RuntimeError("record failure")))
    cli.main()
    assert called == ["command", "command"]
