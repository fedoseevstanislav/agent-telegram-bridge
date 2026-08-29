#!/usr/bin/env python3
"""Generate or verify the bridge's deterministic runtime source manifest."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import shutil
import stat
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "security/runtime-manifest.json"
# `bridge/**/*.py`, not `bridge/*.py`. The non-recursive form made the manifest only as
# complete as the layout it was written against: an imported `bridge/extensions/leak.py` was
# absent from the manifest AND from the sanitization scan derived from it, while every test
# stayed green (#209 review F2). security/README.md claims every runtime source is hash-bound,
# so the glob has to hold for a layout nobody has created yet.
RUNTIME_GLOBS = (
    "bin/tg-bridge",
    "bridge/**/*.py",
    "skill/SKILL.md",
    "systemd/*.service",
    "systemd/*.timer",
)
EXTERNAL_COMMANDS = (
    {"name": "bash", "required": True, "purpose": "tg-bridge launcher"},
    {"name": "gh", "required": True, "purpose": "fleet counts and durable carry-forward"},
    {"name": "pgrep", "required": True, "purpose": "session and worker discovery"},
    {"name": "python3", "required": True, "purpose": "bridge runtime"},
    {"name": "systemctl", "required": True, "purpose": "service health and watchdog"},
    {"name": "tmux", "required": True, "purpose": "interactive session routing"},
    {"name": "claude", "required": False, "purpose": "Claude session spawn feature"},
    {"name": "codex", "required": False, "purpose": "Codex session spawn feature"},
    {"name": "ffmpeg", "required": False, "purpose": "voice-transcription format fallback"},
    {"name": "ffprobe", "required": False, "purpose": "voice attachment duration probe"},
    {"name": "node", "required": False, "purpose": "Codex launcher runtime"},
)
COMMAND_FALLBACKS = {
    "gh": (Path.home() / "bin/gh",),
    "ffmpeg": (Path("/home/linuxbrew/.linuxbrew/bin/ffmpeg"),),
    "ffprobe": (Path("/home/linuxbrew/.linuxbrew/bin/ffprobe"),),
}


def runtime_files() -> list[Path]:
    files: set[Path] = set()
    for pattern in RUNTIME_GLOBS:
        files.update(path for path in ROOT.glob(pattern) if path.is_file())
    return sorted(files, key=lambda path: path.relative_to(ROOT).as_posix())


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def undeclared_imports(files: list[Path]) -> dict[str, list[str]]:
    allowed = set(sys.stdlib_module_names) | {"bridge"}
    offenders: dict[str, list[str]] = {}
    for path in files:
        if path.suffix != ".py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imports: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(alias.name.split(".", 1)[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                imports.add(node.module.split(".", 1)[0])
        unknown = sorted(imports - allowed)
        if unknown:
            offenders[path.relative_to(ROOT).as_posix()] = unknown
    return offenders


def build_manifest() -> dict[str, object]:
    files = runtime_files()
    offenders = undeclared_imports(files)
    if offenders:
        details = ", ".join(f"{path}: {names}" for path, names in offenders.items())
        raise RuntimeError(f"undeclared third-party runtime imports: {details}")
    return {
        "schemaVersion": 1,
        "python": {
            "implementation": "CPython",
            "version": "3.12.*",
            "packages": [],
            "policy": "stdlib-only; third-party imports fail manifest verification",
        },
        "externalCommands": list(EXTERNAL_COMMANDS),
        "files": {
            path.relative_to(ROOT).as_posix(): f"sha256:{sha256(path)}" for path in files
        },
    }


def check_file_modes(files: list[Path]) -> None:
    unsafe = [
        path.relative_to(ROOT).as_posix()
        for path in files
        if stat.S_IMODE(path.stat().st_mode) & 0o022
    ]
    if unsafe:
        raise RuntimeError(f"runtime files are group/world writable: {unsafe}")
    if not os.access(ROOT / "bin/tg-bridge", os.X_OK):
        raise RuntimeError("bin/tg-bridge is not executable")


def check_host() -> None:
    if sys.implementation.name != "cpython" or sys.version_info[:2] != (3, 12):
        raise RuntimeError(
            f"runtime requires CPython 3.12, got {sys.implementation.name} "
            f"{sys.version_info.major}.{sys.version_info.minor}"
        )
    missing = []
    for item in EXTERNAL_COMMANDS:
        name = item["name"]
        fallbacks = COMMAND_FALLBACKS.get(name, ())
        available = shutil.which(name) or next(
            (str(path) for path in fallbacks if path.is_file() and os.access(path, os.X_OK)),
            None,
        )
        if item["required"] and not available:
            missing.append(name)
    if missing:
        raise RuntimeError(f"required external commands are missing: {missing}")


def main() -> int:
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true")
    mode.add_argument("--check", action="store_true")
    parser.add_argument("--check-host", action="store_true")
    args = parser.parse_args()

    files = runtime_files()
    # `--check` answers "does the tree still hash to the manifest", and that question does not
    # depend on file modes. It used to check them anyway, so the first thing anyone does with a
    # fresh clone — `pytest` — printed a red suite and the words "group/world writable", which
    # reads like a security finding and is usually caused by nothing but a umask of 002 (#231).
    #
    # This is NOT the claim that modes on a clone are harmless. They are not: `scripts/install.sh`
    # points every unit's ExecStart back into the clone and runs it in place, so the clone is the
    # deployment, and a group-writable file in a SHARED group is somebody else's write primitive
    # against the bridge (#231 review). The answer is to check it at the boundary that turns the
    # clone into running code, which is the installer — where it can also weigh the mode against
    # who is actually in the group, instead of failing a test run that cannot know.
    #
    # So modes are still enforced here in the two places that are about trusting a tree rather
    # than describing one: `--write`, because blessing a group-writable file into the manifest is
    # how a real loose mode gets legitimised, and `--check-host`, which is what
    # `scripts/verify_deploy_snapshot.sh` passes before a deployment.
    if args.write or args.check_host:
        check_file_modes(files)
    expected = build_manifest()
    rendered = json.dumps(expected, indent=2, sort_keys=True) + "\n"

    if args.write:
        MANIFEST.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        MANIFEST.write_text(rendered, encoding="utf-8")
    else:
        actual = MANIFEST.read_text(encoding="utf-8")
        if actual != rendered:
            raise RuntimeError(
                "security/runtime-manifest.json is stale; run "
                "scripts/generate_runtime_manifest.py --write"
            )
    if args.check_host:
        check_host()

    print(f"runtime_manifest=PASS files={len(files)} python_packages=0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
