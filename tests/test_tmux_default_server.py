"""Every tmux call goes to the user's default tmux server (#407).

A seat running inside another tmux server (`tmux -L <name>` with TMUX_TMPDIR set) had its
`tg-bridge notify` inherit both: the CLI's liveness probe asked that server about a pane that
lives on the default server and refused the notify with "pane is not live" (2026-10-13 and
2026-10-20).
"""

import subprocess

from bridge import daemon


def _capture(monkeypatch):
    seen = {}

    def fake_run(argv, **kwargs):
        seen["argv"], seen["kwargs"] = argv, kwargs
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(daemon.subprocess, "run", fake_run)
    return seen


def test_caller_tmux_server_is_not_inherited(monkeypatch):
    monkeypatch.setenv("TMUX", "/run/user/1000/other-tmux/tmux-1000/other,1,0")
    monkeypatch.setenv("TMUX_TMPDIR", "/run/user/1000/other-tmux")
    monkeypatch.setenv("TMUX_PANE", "%7")
    monkeypatch.setenv("LANG", "C.UTF-8")
    seen = _capture(monkeypatch)

    assert daemon.pane_alive("%1010")

    env = seen["kwargs"]["env"]
    assert "TMUX" not in env and "TMUX_TMPDIR" not in env
    assert env["LANG"] == "C.UTF-8" and env["TMUX_PANE"] == "%7"
    assert seen["kwargs"]["timeout"] == daemon.TMUX_TIMEOUT


def test_explicit_env_is_also_cleaned(monkeypatch):
    seen = _capture(monkeypatch)

    daemon._tmux(["tmux", "list-panes"], env={"TMUX": "x", "TMUX_TMPDIR": "y", "PATH": "/bin"})

    assert seen["kwargs"]["env"] == {"PATH": "/bin"}


def test_dead_pane_still_reports_dead(monkeypatch):
    monkeypatch.setattr(
        daemon.subprocess, "run",
        lambda argv, **kw: subprocess.CompletedProcess(argv, 1, "", "can't find pane"),
    )

    assert not daemon.pane_alive("%999999")
