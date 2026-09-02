#!/usr/bin/env python3
"""Move one installed bridge identity to the agent-neutral name exactly once.

The installer stops the legacy units before calling ``--apply``. This helper owns only the
filesystem transaction so it can be exercised against a fake HOME without reaching systemd.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import stat
import sys
import time


LEGACY_SLUG = "claude-telegram-bridge"
CURRENT_SLUG = "agent-telegram-bridge"
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
OWNED_REFERENCE_REPLACEMENTS = (
    (f"/.config/{LEGACY_SLUG}".encode(), f"/.config/{CURRENT_SLUG}".encode()),
    (f"/.local/share/{LEGACY_SLUG}".encode(), f"/.local/share/{CURRENT_SLUG}".encode()),
    *(
        (f"{LEGACY_SLUG}{suffix}".encode(), f"{CURRENT_SLUG}{suffix}".encode())
        for suffix in UNIT_SUFFIXES
    ),
)
CLI_COMMANDS = frozenset(
    {"register", "send", "recv", "ask", "typing", "current-topic", "notify", "status"}
)
PYTHON_NO_ARGUMENT_FLAGS = frozenset(
    {"-b", "-bb", "-B", "-E", "-I", "-O", "-OO", "-q", "-s", "-S", "-u", "-v", "-V", "-x"}
)


class MigrationConflict(RuntimeError):
    """The old and new trees cannot be combined without choosing whose data wins."""


def _legacy_cli_command(cmdline: bytes, home: Path) -> bool:
    """Recognise only the Python invocation shape installed by this product."""
    argv = [part.decode(errors="surrogateescape") for part in cmdline.split(b"\0") if part]
    if len(argv) < 3:
        return False

    # The launcher execs ``python -u <release>/bridge/cli.py <command>``. Do not search the
    # whole argv for the path: an unrelated ``python -c`` process can contain those same words
    # as data, and stopping it would violate the migration's deliberately narrow boundary.
    script_index = 1
    while script_index < len(argv) and argv[script_index] in PYTHON_NO_ARGUMENT_FLAGS:
        script_index += 1
    if script_index + 1 >= len(argv):
        return False

    script = argv[script_index]
    if not os.path.isabs(script):
        return False
    legacy_root = home / ".local" / "share" / LEGACY_SLUG
    try:
        relative = Path(os.path.normpath(script)).relative_to(legacy_root)
    except ValueError:
        return False
    return relative.parts[-2:] == ("bridge", "cli.py") and argv[script_index + 1] in CLI_COMMANDS


def legacy_cli_pids(
    home: Path, proc_root: Path = Path("/proc"), uid: int | None = None
) -> list[int]:
    """Return same-user legacy CLI processes, never shells or sibling Python jobs."""
    owner = os.getuid() if uid is None else uid
    found: list[int] = []
    for entry in proc_root.iterdir():
        if not entry.name.isdecimal():
            continue
        pid = int(entry.name)
        if pid == os.getpid():
            continue
        try:
            if entry.stat().st_uid != owner:
                continue
            cmdline = (entry / "cmdline").read_bytes()
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
        if _legacy_cli_command(cmdline, home):
            found.append(pid)
    return sorted(found)


def quiesce_legacy_cli(
    home: Path,
    timeout: float = 5.0,
    proc_root: Path = Path("/proc"),
) -> dict[str, list[int]]:
    """Terminate exact legacy CLI waiters and require a bounded quiet window."""
    deadline = time.monotonic() + timeout
    quiet_since: float | None = None
    signalled: set[int] = set()
    while True:
        running = legacy_cli_pids(home, proc_root)
        now = time.monotonic()
        if not running:
            quiet_since = quiet_since or now
            if now - quiet_since >= 0.2:
                return {"signalled": sorted(signalled)}
        else:
            quiet_since = None
            for pid in running:
                if pid in signalled:
                    continue
                try:
                    os.kill(pid, signal.SIGTERM)
                except ProcessLookupError:
                    continue
                signalled.add(pid)
        if now >= deadline:
            remaining = legacy_cli_pids(home, proc_root)
            raise MigrationConflict(
                "legacy CLI processes did not stop before migration: "
                + ", ".join(str(pid) for pid in remaining)
            )
        time.sleep(0.05)


def _pair(old: Path, new: Path, rewrite: bool = False) -> tuple[Path, Path, bool]:
    return old, new, rewrite


def migration_pairs(home: Path) -> list[tuple[Path, Path, bool]]:
    """Every installed name that belongs to this product, in data-before-unit order."""
    config = home / ".config"
    state = home / ".local" / "share"
    units = config / "systemd" / "user"
    pairs = [
        _pair(config / LEGACY_SLUG, config / CURRENT_SLUG),
        _pair(state / LEGACY_SLUG, state / CURRENT_SLUG),
    ]
    pairs.extend(
        _pair(units / f"{LEGACY_SLUG}{suffix}", units / f"{CURRENT_SLUG}{suffix}", True)
        for suffix in UNIT_SUFFIXES
    )
    pairs.extend(
        _pair(units / f"{LEGACY_SLUG}{suffix}", units / f"{CURRENT_SLUG}{suffix}", True)
        for suffix in DROPIN_SUFFIXES
    )
    return pairs


def _exists(path: Path) -> bool:
    # Path.exists() follows a symlink, so a dangling link looks absent and can make rename()
    # overwrite a name the user already owns. lexists asks about the directory entry itself.
    return os.path.lexists(path)


def _rewrite_subjects(path: Path) -> list[Path]:
    if path.is_symlink():
        raise MigrationConflict(f"refusing symlink in migration input: {path}")
    if path.is_file():
        return [path]
    if not path.is_dir():
        mode = stat.S_IFMT(path.lstat().st_mode)
        raise MigrationConflict(f"refusing non-file migration input {path} (type {mode:o})")

    files: list[Path] = []
    for root, dirs, names in os.walk(path, followlinks=False):
        here = Path(root)
        for name in (*dirs, *names):
            subject = here / name
            if subject.is_symlink():
                raise MigrationConflict(f"refusing symlink in migration input: {subject}")
        files.extend(here / name for name in names)
    return sorted(files)


def plan_migration(home: Path) -> list[tuple[Path, Path, bool]]:
    """Validate the whole move before returning the old-only pairs that need it."""
    moves: list[tuple[Path, Path, bool]] = []
    for old, new, rewrite in migration_pairs(home):
        old_exists, new_exists = _exists(old), _exists(new)
        if old_exists and new_exists:
            raise MigrationConflict(f"both exist: {old} and {new}")
        if not old_exists:
            continue
        if old.is_symlink():
            raise MigrationConflict(f"refusing symlink in migration input: {old}")
        if rewrite:
            _rewrite_subjects(old)
        moves.append((old, new, rewrite))
    return moves


def _rewrite_owned_references(path: Path) -> bool:
    before = path.read_bytes()
    after = before
    # A drop-in may point at a source checkout whose basename happens to be the legacy slug.
    # That checkout does not move. Rewrite only the paths and unit identities this transaction
    # owns, or a successful migration can leave ExecStart pointing at a directory that does not
    # exist.
    for old, new in OWNED_REFERENCE_REPLACEMENTS:
        after = after.replace(old, new)
    if after == before:
        return False
    path.write_bytes(after)
    return True


def apply_migration(home: Path) -> dict[str, list[str]]:
    """Apply a preflighted migration. A second call returns two empty lists."""
    moves = plan_migration(home)
    moved: list[str] = []
    rewrite_roots: list[Path] = []
    for old, new, rewrite in moves:
        old.rename(new)
        moved.append(str(new))
        if rewrite:
            rewrite_roots.append(new)

    rewritten: list[str] = []
    for root in rewrite_roots:
        for subject in _rewrite_subjects(root):
            if _rewrite_owned_references(subject):
                rewritten.append(str(subject))
    return {"moved": moved, "rewritten": rewritten}


def _plan_payload(home: Path) -> dict[str, object]:
    moves = plan_migration(home)
    return {
        "needed": bool(moves),
        "moves": [
            {"old": str(old), "new": str(new), "rewrite": rewrite}
            for old, new, rewrite in moves
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="validate and print the move plan")
    mode.add_argument("--apply", action="store_true", help="move an old-only installation")
    mode.add_argument(
        "--legacy-cli-pids", action="store_true", help="list exact legacy CLI processes"
    )
    mode.add_argument(
        "--quiesce-legacy-cli", action="store_true", help="stop exact legacy CLI processes"
    )
    parser.add_argument("--home", default=str(Path.home()), help="installation HOME (for tests)")
    args = parser.parse_args()
    home = Path(os.path.abspath(os.path.expanduser(args.home)))
    try:
        if args.check:
            payload = _plan_payload(home)
        elif args.apply:
            payload = apply_migration(home)
        elif args.legacy_cli_pids:
            payload = {"pids": legacy_cli_pids(home)}
        else:
            payload = quiesce_legacy_cli(home)
    except (MigrationConflict, OSError) as exc:
        print(f"migrate_identity.py: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(payload, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
