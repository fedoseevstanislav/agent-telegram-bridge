"""#178 — every pane the bridge launches must opt out of background-shell reaping.

Claude Code 2.1.x attaches a `memoryPressure` handler to each backgrounded shell and kills
it when that fires. On Linux the check is `os.freemem() < tengu_bg_low_mem_mb` (default
1024 MB) — `Bun.ant.memoryPressureLevel()` is macOS-only — and this host sits at that line.
Every session parks `tg-bridge recv --wait 86400` as a background task, so the listeners
were being reaped, and each reap woke the session for a turn that drained an empty inbox
and re-armed. Those turns land in its context: idle sessions climbed to a context warning
doing nothing.

Confirmed by prediction: killing one 963 MB session pushed MemFree over the threshold and
listener kills went from 13 in 45 minutes to 0 in the following 35.

The variable is read at launch, so it protects only sessions started afterwards. That makes
`launch_pane` the one place it belongs — and the one place a regression can silently undo
it, since a spawned session gives no signal that it is reapable.
"""

import shlex

import pytest

from bridge import daemon


VAR = "CLAUDE_CODE_DISABLE_BG_SHELL_PRESSURE_REAP"


@pytest.fixture
def launched(monkeypatch):
    """Capture the shell command launch_pane hands to tmux."""
    calls = []

    def _tmux(argv, **kw):
        calls.append(argv)
        return type("R", (), {"returncode": 0, "stdout": "%77\n", "stderr": ""})()

    monkeypatch.setattr(daemon, "_tmux", _tmux)

    def run(**kw):
        pane, err = daemon.launch_pane(
            kw.get("name", "sess"), kw.get("cwd", "/home/user"),
            kw.get("launch", "claude --dangerously-skip-permissions"),
            kw.get("engine", "claude"), kw.get("reason", "a stated reason"),
            kw.get("prompt"),
        )
        assert (pane, err) == ("%77", "")
        return calls[-1][-1]                 # the shell command is tmux's last argument

    return run


def test_a_spawned_pane_opts_out_of_the_reaper(launched):
    assert f"{VAR}=1" in launched()


def test_a_revived_pane_opts_out_too(launched):
    # revive_one reaches tmux through this same function with no prompt argument; a fix
    # applied only on the spawn path would leave every restored session reapable.
    assert f"{VAR}=1" in launched(prompt=None)


def test_the_optout_is_set_for_the_engine_not_for_env_itself(launched):
    # `env -u CLAUDECODE VAR=1 <engine>` — the assignment has to land after `env` and
    # before the command, or it is either a shell-local that `env` drops or an argument
    # handed to claude. Pin the order rather than mere presence.
    cmd = launched(launch="claude --model x")
    assert cmd.index("env -u CLAUDECODE") < cmd.index(VAR) < cmd.index("claude --model x")


def test_the_value_is_truthy_to_the_reader(launched):
    # The binary tests the variable for truthiness, so an empty value silently re-enables
    # reaping while still looking "set" in a process listing.
    cmd = launched()
    value = cmd.split(f"{VAR}=", 1)[1].split()[0]
    assert value and value not in ("0", '""', "''")


def test_the_hardened_path_and_claudecode_scrub_survive(launched):
    # The opt-out is inserted into an existing command line; neither of the two things that
    # line already had to get right may be displaced.
    cmd = launched()
    assert f"PATH={shlex.quote(daemon._session_path())}" in cmd
    assert "env -u CLAUDECODE" in cmd


def test_the_prompt_stays_the_last_argument(launched):
    # The bootstrap prompt is the engine's positional initial-prompt argument. If the
    # assignment were appended after it, the prompt would absorb it.
    cmd = launched(launch="claude --model x", prompt="do the thing")
    assert cmd.endswith(shlex.quote("do the thing"))
    assert cmd.index(VAR) < cmd.index(shlex.quote("do the thing"))


# ---- the process boundary, not the string ----------------------------------
#
# Every test above asserts on the command TEXT with daemon._tmux replaced, and Codex's review
# of #179 showed that is not the same property. This shape satisfies all of them and still
# leaves the engine without the variable:
#
#     PATH=... exec env -u CLAUDECODE VAR=1 env -u VAR claude --model x
#
# What we actually care about is what the engine process ends up with, so run the real command
# through real tmux and read the environment back.

import os
import shutil
import subprocess
import tempfile
import time


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux not available")
def test_the_engine_process_really_receives_the_optout():
    """End to end through real tmux: build the command launch_pane emits, substitute a
    harmless `env` dump for the engine, and assert on the dumped environment.

    Creates and removes exactly one tmux session of its own and touches no other pane."""
    fd, probe = tempfile.mkstemp(prefix="reap-probe-", suffix=".env")
    os.close(fd)
    session = f"reapoptout-test-{os.getpid()}"
    captured = {}
    try:
        # The engine stands in as `sh -c 'env > probe'` — everything else is verbatim what
        # launch_pane builds, including the assignment's position in the command line.
        launch = f"sh -c {shlex.quote(f'env > {probe}')}"
        shell_cmd = (f"PATH={shlex.quote(daemon._session_path())} exec env -u CLAUDECODE "
                     f"{daemon.SPAWN_ENV} {launch}")
        subprocess.run(
            ["tmux", "new-session", "-d", "-P", "-F", "#{pane_id}", "-s", session,
             "-c", os.path.expanduser("~"), shell_cmd],
            capture_output=True, text=True, timeout=20, check=True,
        )
        for _ in range(50):                       # the dump is written once `sh` runs
            if os.path.getsize(probe):
                break
            time.sleep(0.1)
        with open(probe) as f:
            for line in f:
                key, _, value = line.rstrip("\n").partition("=")
                captured[key] = value
    finally:
        subprocess.run(["tmux", "kill-session", "-t", f"={session}"],
                       capture_output=True, timeout=20)
        os.unlink(probe)

    assert captured, "the probe never ran; nothing was verified"
    assert captured.get(VAR) == "1", f"the engine did not receive {VAR}"
    assert captured.get("PATH") == daemon._session_path()
    assert "CLAUDECODE" not in captured
