"""#260: the installed identity moves once, without splitting state or daemon ownership."""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
MIGRATOR_PATH = ROOT / "scripts" / "migrate_identity.py"
INSTALL_PATH = ROOT / "scripts" / "install.sh"
OLD = "claude-telegram-bridge"
NEW = "agent-telegram-bridge"
UNIT_SUFFIXES = (
    ".service",
    "-watchdog.service",
    "-watchdog.timer",
    "-digest.service",
    "-digest.timer",
    "-model-watchdog.service",
    "-model-watchdog.timer",
)
DROPIN_SUFFIXES = (
    ".service.d",
    "-watchdog.service.d",
    "-digest.service.d",
    "-model-watchdog.service.d",
)
LEGACY_ALLOWED = {
    "docs/INSTALL.md",                         # one-time upgrade instructions
    "docs/OPERATIONS.md",                      # one-time upgrade runbook
    "docs/superpowers/specs/2026-07-02-reboot-restore-and-model-command-design.md",
    "scripts/install.sh",                      # locates and stops the old installation
    "scripts/migrate_identity.py",             # maps old names to new names
    "tests/test_identity_migration.py",         # proves that mapping
}


def _load_migrator():
    spec = importlib.util.spec_from_file_location("identity_migrator", MIGRATOR_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _old_install(home: Path) -> None:
    config = home / ".config" / OLD
    state = home / ".local" / "share" / OLD
    units = home / ".config" / "systemd" / "user"
    config.mkdir(parents=True)
    state.mkdir(parents=True)
    units.mkdir(parents=True, exist_ok=True)
    (config / "config.json").write_text('{"owner_id": 7}\n')
    (state / "offset").write_text("41\n")
    (state / "topics").mkdir()
    (state / "topics" / "inbox.jsonl").write_text('{"text":"leave stored history alone"}\n')
    for suffix in UNIT_SUFFIXES:
        (units / f"{OLD}{suffix}").write_text(
            f"ExecStart={home}/.local/share/{OLD}/releases/abc/bridge/daemon.py\n"
        )
    for suffix in DROPIN_SUFFIXES:
        dropin = units / f"{OLD}{suffix}"
        dropin.mkdir()
        (dropin / "30-release.conf").write_text(
            f"ExecStart={home}/.local/share/{OLD}/releases/abc/bridge/daemon.py\n"
        )
    # Match an installed private tree even on a test host whose umask is 0002; the real
    # installer correctly refuses group-writable destinations before it changes anything.
    for directory in (home, *(path for path in home.rglob("*") if path.is_dir())):
        directory.chmod(0o700)


def _snapshot(root: Path) -> dict[str, bytes | None]:
    result: dict[str, bytes | None] = {}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        result[rel] = path.read_bytes() if path.is_file() else None
    return result


def test_old_only_install_moves_to_new_identity_and_rewrites_units(tmp_path):
    home = tmp_path / "home"
    _old_install(home)
    old_history = (home / ".local" / "share" / OLD / "topics" / "inbox.jsonl").read_bytes()

    migrator = _load_migrator()
    result = migrator.apply_migration(home)

    assert result["moved"]
    assert not (home / ".config" / OLD).exists()
    assert not (home / ".local" / "share" / OLD).exists()
    assert (home / ".config" / NEW / "config.json").read_text() == '{"owner_id": 7}\n'
    assert (home / ".local" / "share" / NEW / "offset").read_text() == "41\n"
    assert (home / ".local" / "share" / NEW / "topics" / "inbox.jsonl").read_bytes() == old_history

    units = home / ".config" / "systemd" / "user"
    assert not (units / f"{OLD}.service").exists()
    assert not (units / f"{OLD}.service.d").exists()
    assert OLD not in (units / f"{NEW}.service").read_text()
    assert OLD not in (units / f"{NEW}.service.d" / "30-release.conf").read_text()
    assert NEW in (units / f"{NEW}.service.d" / "30-release.conf").read_text()


def test_rewrite_preserves_an_unmoved_checkout_path(tmp_path):
    home = tmp_path / "home"
    _old_install(home)
    dropin = (
        home
        / ".config"
        / "systemd"
        / "user"
        / f"{OLD}.service.d"
        / "30-release.conf"
    )
    checkout = home / OLD
    dropin.write_text(
        f"Environment=BRIDGE_STATE={home}/.local/share/{OLD}\n"
        f"After={OLD}-watchdog.service\n"
        f"ExecStart=/usr/bin/python3 {checkout}/bridge/daemon.py\n"
    )
    migrator = _load_migrator()

    migrator.apply_migration(home)

    migrated = (
        home
        / ".config"
        / "systemd"
        / "user"
        / f"{NEW}.service.d"
        / "30-release.conf"
    ).read_text()
    assert f"BRIDGE_STATE={home}/.local/share/{NEW}" in migrated
    assert f"After={NEW}-watchdog.service" in migrated
    assert f"ExecStart=/usr/bin/python3 {checkout}/bridge/daemon.py" in migrated


def test_successful_migration_is_idempotent(tmp_path):
    home = tmp_path / "home"
    _old_install(home)
    migrator = _load_migrator()
    migrator.apply_migration(home)
    before = _snapshot(home)

    result = migrator.apply_migration(home)

    assert result == {"moved": [], "rewritten": []}
    assert _snapshot(home) == before


def test_old_and_new_pair_refuses_without_mutating_either(tmp_path):
    home = tmp_path / "home"
    _old_install(home)
    new_config = home / ".config" / NEW
    new_config.mkdir()
    (new_config / "config.json").write_text('{"owner_id": 8}\n')
    before = _snapshot(home)
    migrator = _load_migrator()

    with pytest.raises(migrator.MigrationConflict, match="both exist"):
        migrator.apply_migration(home)

    assert _snapshot(home) == before


def test_symlink_inside_a_migrated_dropin_is_refused_before_any_move(tmp_path):
    home = tmp_path / "home"
    _old_install(home)
    dropin = home / ".config" / "systemd" / "user" / f"{OLD}.service.d"
    (dropin / "unsafe.conf").symlink_to("/tmp/elsewhere")
    before = _snapshot(home)
    migrator = _load_migrator()

    with pytest.raises(migrator.MigrationConflict, match="symlink"):
        migrator.apply_migration(home)

    assert _snapshot(home) == before


def _installer_migration_function() -> str:
    source = INSTALL_PATH.read_text()
    found = re.search(r"^migrate_legacy_install \(\) \{\n(.*?)^\}\n", source, re.S | re.M)
    assert found, "install.sh has no top-level migrate_legacy_install function"
    return "migrate_legacy_install () {\n" + found.group(1) + "}\n"


def _fake_systemctl(tmp_path: Path) -> tuple[Path, Path]:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "systemctl.log"
    fake = bindir / "systemctl"
    fake.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "if [[ $2 == cat ]]; then exit 0; fi\n"
        "if [[ $2 == disable ]]; then\n"
        f"  [[ -d $HOME/.local/share/{OLD} ]]\n"
        f"  [[ ! -e $HOME/.local/share/{NEW} ]]\n"
        "  printf '%s\\n' \"$*\" >> \"$SYSTEMCTL_LOG\"\n"
        "  exit 0\n"
        "fi\n"
        "exit 0\n"
    )
    fake.chmod(0o755)
    return bindir, log


def _run_installer_migration(home: Path, bindir: Path, log: Path) -> subprocess.CompletedProcess:
    script = (
        "set -euo pipefail\n"
        + _installer_migration_function()
        + "migrate_legacy_install\n"
    )
    env = {
        **os.environ,
        "HOME": str(home),
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "PYTHON": os.environ.get("PYTHON", "python3"),
        "ROOT": str(ROOT),
        "MIGRATOR": str(MIGRATOR_PATH),
        "SYSTEMCTL_LOG": str(log),
    }
    return subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True)


def test_installer_stops_old_units_before_the_state_move(tmp_path):
    home = tmp_path / "home"
    _old_install(home)
    bindir, log = _fake_systemctl(tmp_path)

    result = _run_installer_migration(home, bindir, log)

    assert result.returncode == 0, result.stderr
    calls = log.read_text().splitlines()
    assert any(f"disable --now {OLD}.service" in call for call in calls)
    assert (home / ".local" / "share" / NEW / "offset").read_text() == "41\n"


def test_installer_conflict_refuses_before_stopping_any_unit(tmp_path):
    home = tmp_path / "home"
    _old_install(home)
    (home / ".config" / NEW).mkdir()
    bindir, log = _fake_systemctl(tmp_path)

    result = _run_installer_migration(home, bindir, log)

    assert result.returncode != 0
    assert "both exist" in result.stderr
    assert not log.exists(), "a unit was stopped before the filesystem conflict was found"
    assert (home / ".config" / OLD).is_dir()


def test_installer_starts_the_new_daemon_only_after_legacy_migration():
    source = INSTALL_PATH.read_text()
    assert source.index("migrate_legacy_install") < source.index(
        "systemctl --user enable --now agent-telegram-bridge.service"
    )


def test_full_installer_rehearsal_switches_from_old_to_new_units(tmp_path):
    home = tmp_path / "home"
    _old_install(home)
    config = home / ".config" / OLD / "config.json"
    config.write_text('{"bot_token":"1:x","chat_id":-100123,"owner_id":7}\n')
    config.chmod(0o600)
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    log = tmp_path / "systemctl.log"

    systemctl = fake_bin / "systemctl"
    systemctl.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "case $2 in\n"
        "  show-environment) exit 0 ;;\n"
        "  cat) [[ -e $HOME/.config/systemd/user/$3 ]] ;;\n"
        "  disable)\n"
        f"    [[ -d $HOME/.local/share/{OLD} && ! -e $HOME/.local/share/{NEW} ]]\n"
        "    printf 'disable %s\\n' \"$4\" >> \"$SYSTEMCTL_LOG\" ;;\n"
        "  daemon-reload) printf 'reload\\n' >> \"$SYSTEMCTL_LOG\" ;;\n"
        "  enable)\n"
        f"    [[ ! -e $HOME/.local/share/{OLD} && -d $HOME/.local/share/{NEW} ]]\n"
        "    printf 'enable %s\\n' \"$4\" >> \"$SYSTEMCTL_LOG\" ;;\n"
        "  is-active) exit 0 ;;\n"
        "  *) exit 0 ;;\n"
        "esac\n"
    )
    systemctl.chmod(0o755)
    tmux = fake_bin / "tmux"
    tmux.write_text("#!/usr/bin/env bash\nprintf 'tmux 3.5a\\n'\n")
    tmux.chmod(0o755)
    sleep = fake_bin / "sleep"
    sleep.write_text("#!/usr/bin/env bash\nexit 0\n")
    sleep.chmod(0o755)

    env = {
        **os.environ,
        "HOME": str(home),
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "SYSTEMCTL_LOG": str(log),
    }
    result = subprocess.run(
        ["bash", str(INSTALL_PATH), "--prefix", str(home / "bin")],
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr + result.stdout
    calls = log.read_text().splitlines()
    assert calls[: len(UNIT_SUFFIXES)] == [f"disable {OLD}{suffix}" for suffix in UNIT_SUFFIXES]
    assert calls[-4:] == [
        f"enable {NEW}.service",
        f"enable {NEW}-watchdog.timer",
        f"enable {NEW}-digest.timer",
        f"enable {NEW}-model-watchdog.timer",
    ]
    assert calls.index("reload") > calls.index(f"disable {OLD}-model-watchdog.timer")
    assert not any(OLD in call for call in calls[calls.index("reload") + 1 :])
    units = home / ".config" / "systemd" / "user"
    assert sorted(path.name for path in units.glob(f"{NEW}*.*") if path.is_file()) == sorted(
        f"{NEW}{suffix}" for suffix in UNIT_SUFFIXES
    )
    assert not list(units.glob(f"{OLD}*"))
    assert (home / ".local" / "share" / NEW / "offset").read_text() == "41\n"


def test_migrator_cli_reports_a_machine_readable_plan(tmp_path):
    home = tmp_path / "home"
    _old_install(home)

    result = subprocess.run(
        ["python3", str(MIGRATOR_PATH), "--check", "--home", str(home)],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["needed"] is True
    assert any(item["old"].endswith(f"/.config/{OLD}") for item in payload["moves"])


def test_legacy_slug_remains_only_in_migration_or_historical_context():
    tracked = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        capture_output=True,
    )
    if tracked.returncode == 0:
        paths = (ROOT / raw.decode() for raw in tracked.stdout.split(b"\0") if raw)
    else:
        # The public artifact deliberately has no .git directory.  Generated test environments
        # are not part of that artifact, so ignore only their conventional cache directories.
        ignored = {".git", ".venv", ".pytest_cache", "__pycache__"}
        paths = (
            path
            for path in ROOT.rglob("*")
            if path.is_file() and not ignored.intersection(path.relative_to(ROOT).parts)
        )
    hits = set()
    for path in paths:
        rel = path.relative_to(ROOT).as_posix()
        if path.is_file() and OLD.encode() in path.read_bytes():
            hits.add(rel)
    assert hits <= LEGACY_ALLOWED, f"legacy product identity escaped migration/history: {hits}"
    assert {"scripts/install.sh", "scripts/migrate_identity.py", "tests/test_identity_migration.py"} <= hits


def test_no_active_product_title_still_calls_the_bridge_claude():
    offenders = []
    for root in (ROOT / "bridge", ROOT / "systemd", ROOT / "docs"):
        for path in root.rglob("*"):
            if path.is_file() and b"claude telegram bridge" in path.read_bytes().lower():
                offenders.append(path.relative_to(ROOT).as_posix())
    assert not offenders, offenders
