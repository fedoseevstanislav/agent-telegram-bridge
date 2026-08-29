from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _service_directives(name):
    directives = {}
    unit = ROOT / "systemd" / name
    for raw_line in unit.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        directives[key] = value
    return directives


def test_bridge_service_only_kills_daemon_process_on_restart():
    directives = _service_directives("claude-telegram-bridge.service")

    assert directives.get("KillMode") == "process"


def test_model_watchdog_service_runs_standalone_script():
    directives = _service_directives("claude-telegram-bridge-model-watchdog.service")

    assert directives.get("Type") == "oneshot"
    assert directives.get("ExecStart") == (
        "/usr/bin/python3 @@BRIDGE_ROOT@@/bridge/model_watchdog.py"
    )


def test_model_watchdog_timer_runs_every_five_minutes():
    directives = _service_directives("claude-telegram-bridge-model-watchdog.timer")

    assert directives.get("OnBootSec") == "2min"
    assert directives.get("OnUnitActiveSec") == "5min"
    assert directives.get("WantedBy") == "timers.target"


def test_no_unit_carries_an_absolute_home_directory():
    """A unit that ships with somebody's home path in it names them, and only runs for them.

    All four services used to hardcode the author's home. `@@BRIDGE_ROOT@@` is substituted by
    scripts/install.sh with wherever the clone actually lives — systemd cannot expand a
    variable in ExecStart, so the substitution has to happen at install time.
    """
    import re

    # `\w`, not `[^\W\d]`: a username may begin with a digit, and such a home directory was
    # slipping through a guard whose whole claim is "no absolute home path" (#219 review).
    # The example is described rather than written out because this file now ships, and the
    # release guard reads a literal home path as a leak wherever it finds one (#231).
    home_like = re.compile(r"(?i:/home/|/users/)\w")
    offenders = []
    for unit in sorted((ROOT / "systemd").glob("*")):
        for number, line in enumerate(unit.read_text().splitlines(), 1):
            if home_like.search(line):
                offenders.append(f"{unit.name}:{number}: {line.strip()}")
    assert not offenders, "; ".join(offenders)


def test_every_service_execstart_is_templated():
    """The installer's rewrite is anchored on this shape; a hand-edit that breaks it is silent."""
    for unit in sorted((ROOT / "systemd").glob("*.service")):
        exec_start = _service_directives(unit.name).get("ExecStart", "")
        assert exec_start.startswith("/usr/bin/python3 @@BRIDGE_ROOT@@/bridge/"), \
            f"{unit.name}: {exec_start!r}"
        assert exec_start.endswith(".py"), f"{unit.name}: {exec_start!r}"
