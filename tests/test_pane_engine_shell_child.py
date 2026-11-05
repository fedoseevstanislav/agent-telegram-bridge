"""pane_engine sees a codex seat whose pane root is a shell (2026-11-02)."""
import os
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from bridge import daemon  # noqa: E402


def _spawn(argv0_script):
    """A shell whose child runs a script named .../codex; returns (shell Popen, script path)."""
    d = tempfile.mkdtemp()
    fake = os.path.join(d, "codex")
    with open(fake, "w") as f:
        f.write("#!/bin/sh\nsleep 30\n")
    os.chmod(fake, 0o755)
    sh = subprocess.Popen(["bash", "-c", f"{argv0_script(fake)}; true"])
    for _ in range(50):
        if daemon._child_pids(sh.pid):
            break
        time.sleep(0.05)
    return sh


def test_codex_child_of_shell_is_codex():
    sh = _spawn(lambda fake: fake)
    try:
        assert daemon.pane_engine("node", sh.pid) == "codex"
    finally:
        for c in daemon._child_pids(sh.pid):
            os.kill(int(c), 9)
        sh.kill()


def test_non_codex_child_is_none():
    sh = subprocess.Popen(["bash", "-c", "sleep 30; true"])
    try:
        time.sleep(0.2)
        assert daemon.pane_engine("node", sh.pid) is None
    finally:
        for c in daemon._child_pids(sh.pid):
            os.kill(int(c), 9)
        sh.kill()


def test_plain_commands_unchanged():
    assert daemon.pane_engine("claude", 1) == "claude"
    assert daemon.pane_engine("codex", 1) == "codex"
    assert daemon.pane_engine("bash", 1) is None


def test_unreadable_pid_is_none():
    assert daemon.pane_engine("node", 999999999) is None
    assert daemon._child_pids(999999999) == []
