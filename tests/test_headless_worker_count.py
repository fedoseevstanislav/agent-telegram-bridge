"""headless_worker_count asks pgrep only for processes with this process's effective uid.

Another user's interactive sessions run under their own tmux server, which `tmux list-panes`
here cannot see, so without the user filter they were counted as headless workers (three
sandboxed sessions of another account showed as "3 workers now" in the morning digest)."""
import os
import subprocess

from bridge import daemon


def test_pgrep_is_limited_to_this_user(monkeypatch):
    calls = []

    def fake_run(argv, **kw):
        calls.append(argv)
        out = "" if argv[0] == "pgrep" else "1\n"
        return subprocess.CompletedProcess(argv, 0, stdout=out, stderr="")

    monkeypatch.setattr(daemon.subprocess, "run", fake_run)
    monkeypatch.setattr(daemon, "_tmux", fake_run)
    assert daemon.headless_worker_count() == 0
    pgreps = [a for a in calls if a[0] == "pgrep"]
    assert len(pgreps) == 2
    for argv in pgreps:
        assert argv[1:3] == ["-u", str(os.geteuid())]
