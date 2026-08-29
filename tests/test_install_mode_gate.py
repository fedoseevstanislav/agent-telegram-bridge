"""The installer must refuse a clone that someone else can rewrite (#231 review).

`scripts/install.sh` copies no code. The launcher it writes execs `$ROOT/bridge/cli.py`, and
every unit's `ExecStart` points back into the clone — so the clone IS the deployment, and anyone
who can write those files decides what the bridge runs as you.

That check used to live, by accident, in `generate_runtime_manifest.py --check`: it refused any
group- or world-writable runtime file, and the shipped test suite ran it. It was in the wrong
place twice over. It fired on `pytest` in a fresh clone, where a 0664 from an ordinary umask of
002 is almost always harmless — so the first thing a stranger did produced a red suite and the
words "group/world writable" — and it did nothing at all for the documented install path, which
never called it. A check that cries wolf on a normal install teaches people to ignore it.

These tests run the gate's REAL source, extracted from `scripts/install.sh`, rather than a copy
of its logic — a copy would keep passing after somebody edited the installer.
"""

import os
import pathlib
import re
import subprocess
import sys

import pytest


ROOT = pathlib.Path(__file__).resolve().parent.parent
INSTALL = ROOT / "scripts/install.sh"

RUNTIME = ("bridge/cli.py", "bridge/daemon.py", "bin/tg-bridge",
           "systemd/claude-telegram-bridge.service", "skill/SKILL.md",
           # Nested, because the first version of the gate globbed `bridge/*.py` while the
           # manifest deliberately recurses, and a module one directory down is executed exactly
           # like a top-level one (#231 review r2).
           "bridge/extensions/nested.py")


def _gate_source():
    """The gate's own text, taken out of the installer's `MODES` heredoc.

    Anchored on the heredoc delimiters rather than on line numbers so an edit above it does not
    silently start testing the wrong block; if the delimiters ever change, this raises instead
    of quietly testing nothing.
    """
    body = INSTALL.read_text()
    # `[^\n]*` because the heredoc line does not end at the delimiter — it carries the `|| die`
    # that turns a non-zero exit into the refusal. Anchoring on `\n` straight after `<<'MODES'`
    # matched nothing, and the assert below is what said so instead of testing an empty string.
    found = re.search(r"<<'MODES'[^\n]*\n(.*?)\nMODES\n", body, re.DOTALL)
    assert found, "scripts/install.sh no longer has a MODES heredoc — this test is testing air"
    return found.group(1)


def _dirs(root):
    """Every directory the gate walks: each runtime file's parents, up to and including root."""
    seen = {root}
    for rel in RUNTIME:
        parent = (root / rel).parent
        while parent != root:
            seen.add(parent)
            parent = parent.parent
    return seen


def _tree(tmp_path, mode, dirmode=0o755, gid=None):
    """A clone-shaped tree with EVERY property the gate reads set explicitly.

    The directories are set too, not just the files. Leaving them at whatever pytest's tmp_path
    happened to be meant the two accept-cases failed for a reason that had nothing to do with
    what they were testing — the gate reads the containing directories, so a fixture that does
    not control them is not describing the case it claims to.
    """
    for rel in RUNTIME:
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# runtime file\n")
    for directory in _dirs(tmp_path):
        if gid is not None:
            os.chown(directory, -1, gid)
        directory.chmod(dirmode)
    for rel in RUNTIME:                      # after the directories: chmod order does not matter
        if gid is not None:                  # but chown-then-chmod on the files reads clearer
            os.chown(tmp_path / rel, -1, gid)
        (tmp_path / rel).chmod(mode)
    return tmp_path


def _run(tmp_path):
    return subprocess.run([sys.executable, "-c", _gate_source(), str(tmp_path)],
                          capture_output=True, text=True)


def test_a_private_tree_installs_silently(tmp_path):
    """0600 is unambiguous: nobody else can write it, so the gate must not say anything."""
    result = _run(_tree(tmp_path, 0o600, dirmode=0o700))

    assert result.returncode == 0, result.stdout + result.stderr
    assert "writable only by you" in result.stdout


def test_a_world_writable_tree_is_refused(tmp_path):
    """No umask produces this by accident, and any user on the host can rewrite the code."""
    result = _run(_tree(tmp_path, 0o666))

    assert result.returncode != 0
    assert "WORLD-writable" in result.stdout + result.stderr
    assert "chmod -R o-w" in result.stdout + result.stderr


def _gids_by_sharing():
    """The current user's groups, split by whether anyone else is in them.

    Both halves are found from the real group database rather than assumed, because the whole
    point of the gate is that 0664 means different things in different groups — and the
    assumption can be wrong on ordinary hosts: a primary group may contain a peer account,
    so its apparently "private" group is not private at all.
    """
    import grp
    import pwd

    me = pwd.getpwuid(os.getuid()).pw_name
    private, shared = [], []
    for gid in {os.getgid(), *os.getgroups()}:
        try:
            group = grp.getgrgid(gid)
        except KeyError:
            continue
        members = set(group.gr_mem)
        for entry in pwd.getpwall():
            if entry.pw_gid == gid:
                members.add(entry.pw_name)
        (shared if members - {me} else private).append(gid)
    return private, shared


def test_group_writable_in_a_group_with_no_other_members_is_accepted(tmp_path):
    """The ordinary umask-002 result, where the group has exactly one member: you.

    This is the case that made the old check a false alarm. Refusing it demands a chmod that
    protects nobody from nobody, and a check that fires on a normal install stops being read.
    """
    private, _ = _gids_by_sharing()
    if not private:
        pytest.skip("this host puts another user in every group this account belongs to")

    result = _run(_tree(tmp_path, 0o664, dirmode=0o775, gid=private[0]))

    assert result.returncode == 0, result.stdout + result.stderr
    assert "writable only by you" in result.stdout


def test_group_writable_in_a_shared_group_is_refused(tmp_path):
    """The genuinely exploitable case: another account can rewrite what the bridge executes."""
    _, shared = _gids_by_sharing()
    if not shared:
        pytest.skip("every group this account belongs to has no other members")

    result = _run(_tree(tmp_path, 0o664, dirmode=0o755, gid=shared[0]))

    assert result.returncode != 0
    assert "writable by others in their group" in result.stdout + result.stderr
    assert "chmod -R g-w" in result.stdout + result.stderr


def test_a_writable_directory_is_refused_even_when_every_file_is_0644(tmp_path):
    """The vector the first gate missed, and the reason "file modes" was the wrong frame.

    Write permission on a directory is permission to unlink what is in it and create something
    else under the same name. A 0644 module inside a group-writable package directory is
    replaceable by anyone in that group; the file's own mode never enters into it.
    """
    _, shared = _gids_by_sharing()
    if not shared:
        pytest.skip("every group this account belongs to has no other members")

    result = _run(_tree(tmp_path, 0o644, dirmode=0o775, gid=shared[0]))

    assert result.returncode != 0, (
        "0644 files inside a shared group-writable directory are replaceable, and the gate "
        "reported them as writable only by me.\n" + result.stdout + result.stderr
    )
    assert "writable by others in their group" in result.stdout + result.stderr


def test_a_symlink_in_the_runtime_tree_is_refused(tmp_path):
    """Refused rather than followed, because this check cannot establish where it leads.

    A symlinked package directory is the sharper half: `glob` does not descend one, so the gate
    saw nothing while Python imported and ran the module behind it. Following the link instead
    would mean checking the TARGET's real ancestry, which the lexical parent walk cannot do — and
    a clone of this repository contains no symlink in these trees, so refusing costs nothing.
    """
    tree = _tree(tmp_path, 0o644, dirmode=0o755)
    outside = tmp_path.parent / (tmp_path.name + "-outside")
    outside.mkdir(exist_ok=True)
    (outside / "leak.py").write_text("print('executed')\n")
    (tree / "bridge/extensions_link").symlink_to(outside)

    result = _run(tree)

    assert result.returncode != 0
    assert "symlink(s) in the runtime tree" in result.stdout + result.stderr


def test_bytes_python_would_execute_that_are_not_dot_py_are_checked(tmp_path):
    """Cached bytecode runs in preference to matching source, and it is not a `.py`.

    The gate globbed `bridge/**/*.py` until a reviewer put a 0666 `__pycache__/*.pyc` beside a
    benign 0644 module and showed the cached bytes executing instead. The enumeration is now
    every file in the runtime trees.
    """
    tree = _tree(tmp_path, 0o644, dirmode=0o755)
    cache = tree / "bridge/__pycache__"
    cache.mkdir()
    cache.chmod(0o755)
    cached = cache / "cli.cpython-312.pyc"
    cached.write_bytes(b"\x00" * 16)
    cached.chmod(0o666)

    result = _run(tree)

    assert result.returncode != 0
    assert "WORLD-writable" in result.stdout + result.stderr
    assert "__pycache__" in result.stdout + result.stderr


@pytest.mark.skip(reason="creating a file owned by another account needs privileges this suite "
                         "must not have; the branch is exercised by inspection only")
def test_a_runtime_file_owned_by_someone_else_is_refused():
    """Documented, not run. Its owner can reopen the permissions whenever they like, so a mode
    you like on a file you do not own proves nothing — but constructing the case needs root, and
    the symlink route that used to stand in for it is now refused earlier and for its own
    reason."""


def test_the_gate_runs_before_the_installer_writes_anything(tmp_path):
    """Order is the whole point: a refusal after the first write leaves a half-install behind."""
    body = INSTALL.read_text()
    gate_at = body.index("<<'MODES'")
    write_at = body.index("# --- write ---")

    assert gate_at < write_at, "the mode gate moved below the section that writes files"
