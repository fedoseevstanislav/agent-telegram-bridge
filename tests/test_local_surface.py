"""#245 — the owner pin governs Telegram, and nothing that arrives another way.

The security pass for the public release traced every ingress and found no unauthenticated
remote path. Then it pointed at the filesystem, where the gate does not exist: on a host where
another account shares your group, that account can replace the binary your next spawn invokes,
write shell fragments into the config the spawn reads, or append a record to a topic inbox that
your agent will read as if you had sent it. None of that passes through Telegram, so none of it
meets `owner_id`.

These tests pin the four narrowings that answer it.
"""

import ast
import json
import os
import pathlib
import re
import stat
import subprocess

import pytest

from bridge import common


ROOT = pathlib.Path(__file__).resolve().parent.parent
INSTALL = ROOT / "scripts/install.sh"


@pytest.fixture(autouse=True)
def _umask_is_not_leaked():
    """Restore the process umask around every test in this file.

    `daemon.main()` calls `secure_process_umask()`, which is the point of it — and a test that
    calls `main()` therefore changes the umask for every test that runs after it in the same
    process. That silently changed what `mkdir` produced in a later fixture and made an
    installer test take a different branch depending on file order. A suite whose results
    depend on execution order cannot be used to judge a mutation.
    """
    before = os.umask(0o022)
    os.umask(before)
    yield
    os.umask(before)


# ---- the config is checked on every read, not once at install time ----------

def _config(tmp_path, monkeypatch, mode=0o600, **extra):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"bot_token": "1:x", "chat_id": -1001, "owner_id": 42, **extra}))
    path.chmod(mode)
    monkeypatch.setattr(common, "CONFIG_PATH", str(path))
    return path


def test_a_private_config_is_read_normally(tmp_path, monkeypatch):
    _config(tmp_path, monkeypatch)
    assert common.load_config()["owner_id"] == 42


@pytest.mark.parametrize("mode", [0o660, 0o640, 0o606, 0o604, 0o666, 0o644])
def test_a_config_others_can_read_or_write_is_refused(tmp_path, monkeypatch, mode):
    # Write is the sharp end — `spawn_flags` from this file goes into the command that launches
    # an agent — but read alone hands over the bot token, so both are refused.
    _config(tmp_path, monkeypatch, mode=mode)
    with pytest.raises(SystemExit) as excinfo:
        common.load_config()
    assert "chmod 600" in str(excinfo.value)


def test_the_refusal_says_what_is_at_stake(tmp_path, monkeypatch):
    # An error that only says "bad permissions" gets chmod'ed back the moment it is
    # inconvenient. This one has to say why the file is worth protecting.
    _config(tmp_path, monkeypatch, mode=0o660)
    with pytest.raises(SystemExit) as excinfo:
        common.load_config()
    message = str(excinfo.value)
    assert "bot token" in message and "runs as you" in message


def test_the_config_is_rechecked_after_it_changes(tmp_path, monkeypatch):
    # THE case install-time checking misses: it was fine when the installer ran, and is not now.
    path = _config(tmp_path, monkeypatch)
    assert common.load_config()["owner_id"] == 42
    path.chmod(0o664)
    with pytest.raises(SystemExit):
        common.load_config()


def test_the_file_that_was_opened_is_the_file_that_was_judged(tmp_path, monkeypatch):
    # `stat(path)` then `open(path)` resolve the same name twice, and a peer who can write any
    # DIRECTORY on the way to the config — `~/.config` is group-writable on more hosts than
    # not — changes what the name means in between: the check passes on the safe file, the
    # daemon parses theirs, and `spawn_flags` from it goes into the next agent command.
    #
    # The peer is played by os.stat itself. Resolving the config by name to judge it is the
    # act that hands over the interval, so the swap is triggered by that call and by nothing
    # else. Code that judges the open descriptor never makes the call, so the swap never
    # happens and the safe file is what gets read. Written this way so that reverting
    # load_config to stat-then-open FAILS this test — the previous version asserted about
    # the helper rather than about load_config, and survived exactly that revert (#245 review).
    safe = _config(tmp_path, monkeypatch)
    hostile = tmp_path / "hostile.json"
    hostile.write_text(json.dumps({"bot_token": "1:evil", "chat_id": -1, "owner_id": 1}))
    hostile.chmod(0o666)

    real_stat, swapped = os.stat, []

    def stat_then_swap(path, *args, **kwargs):
        result = real_stat(path, *args, **kwargs)
        if not swapped and str(path) == str(safe):
            swapped.append(True)
            hostile.replace(safe)
        return result

    monkeypatch.setattr(common.os, "stat", stat_then_swap)
    assert common.load_config()["bot_token"] == "1:x", (
        "the config was judged by name and then read by name; the file that passed the check "
        "is not the file that was parsed")


def test_a_config_symlinked_to_an_exposed_file_is_refused(tmp_path, monkeypatch):
    # The target is what gets read, so the target is what must be judged. `fstat` on the open
    # descriptor reports the target, which is why following the link stays safe here.
    target = tmp_path / "elsewhere.json"
    target.write_text(json.dumps({"bot_token": "1:x", "chat_id": -1, "owner_id": 42}))
    target.chmod(0o664)
    link = tmp_path / "config.json"
    link.symlink_to(target)
    monkeypatch.setattr(common, "CONFIG_PATH", str(link))
    with pytest.raises(SystemExit) as excinfo:
        common.load_config()
    assert "chmod 600" in str(excinfo.value)


def test_a_config_that_is_a_directory_is_refused(tmp_path, monkeypatch):
    # os.open succeeds on a directory; only the fstat catches it. Without the regular-file
    # check this becomes an IsADirectoryError traceback instead of a stated refusal.
    d = tmp_path / "config.json"
    d.mkdir()
    monkeypatch.setattr(common, "CONFIG_PATH", str(d))
    with pytest.raises(SystemExit) as excinfo:
        common.load_config()
    assert "not a regular file" in str(excinfo.value)


# ---- state the daemon creates is private -----------------------------------

def test_state_directories_are_created_private(tmp_path, monkeypatch):
    # Under umask 0 the 0700 argument is the ONLY thing making this directory private, so
    # removing it fails the test. Run under the CI runner's 022 and 0755 would pass too, which
    # is a test of the umask rather than of the code (#245 review).
    monkeypatch.setattr(common, "STATE_DIR", str(tmp_path / "state"))
    old = os.umask(0)
    try:
        common.state_path("topics", "33", "inbox.jsonl")
    finally:
        os.umask(old)
    made = pathlib.Path(common.STATE_DIR) / "topics" / "33"
    assert stat.S_IMODE(made.stat().st_mode) == 0o700


def _tree(tmp_path, monkeypatch, root_mode=0o775):
    """A state tree as `umask 002` would have left it before any of this existed."""
    root = tmp_path / "state"
    (root / "topics" / "33").mkdir(parents=True)
    inbox = root / "topics" / "33" / "inbox.jsonl"
    inbox.write_text('{"text": "hello"}\n')
    # chmod, not mkdir(mode=): mkdir's mode is masked by the umask, so on a runner with
    # umask 022 the fixture arrived already safe and the assertion proved nothing.
    inbox.chmod(0o664)
    (root / "topics" / "33").chmod(0o775)
    (root / "topics").chmod(0o775)
    root.chmod(root_mode)
    monkeypatch.setattr(common, "STATE_DIR", str(root))
    return root, inbox


def test_the_state_root_loses_group_and_other_write(tmp_path, monkeypatch):
    root, _ = _tree(tmp_path, monkeypatch)
    changed, before, after = common.secure_state_tree()
    assert before == "0o775" and after == "0o755"
    assert not stat.S_IMODE(root.stat().st_mode) & 0o022
    assert changed == 4      # root, topics, topics/33, inbox.jsonl


def test_everything_inside_the_tree_loses_it_too(tmp_path, monkeypatch):
    # THE hole in narrowing only the root: the inbox is the thing a peer wants to append to,
    # and it sat at 0664 underneath a root that had just been reported as fixed (#245 review).
    root, inbox = _tree(tmp_path, monkeypatch)
    common.secure_state_tree()
    for path in (root / "topics", root / "topics" / "33", inbox):
        assert not stat.S_IMODE(path.stat().st_mode) & 0o022, f"{path} is still writable"


def test_read_bits_on_an_existing_tree_are_left_alone(tmp_path, monkeypatch):
    # Deliberately narrow. Write is what turns "can read the owner's history" into "can put
    # words in front of the owner's agent", and it is the bit nothing legitimate needs. Taking
    # read away too would be a larger unrequested change to somebody's filesystem.
    root, inbox = _tree(tmp_path, monkeypatch)
    common.secure_state_tree()
    assert stat.S_IMODE(root.stat().st_mode) == 0o755
    assert stat.S_IMODE(inbox.stat().st_mode) == 0o644


def test_an_already_private_tree_is_not_touched(tmp_path, monkeypatch):
    root = tmp_path / "state"
    (root / "topics").mkdir(parents=True)
    (root / "topics").chmod(0o700)
    root.chmod(0o700)
    monkeypatch.setattr(common, "STATE_DIR", str(root))
    assert common.secure_state_tree() is None


def test_a_symlink_in_the_tree_is_not_chmoded(tmp_path, monkeypatch):
    # chmod follows symlinks. Narrowing one would reach out of the state tree and change the
    # mode of whatever it points at, which is somebody else's file.
    root, _ = _tree(tmp_path, monkeypatch)
    outside = tmp_path / "outside.txt"
    outside.write_text("not ours")
    outside.chmod(0o664)
    (root / "link.jsonl").symlink_to(outside)
    common.secure_state_tree()
    assert stat.S_IMODE(outside.stat().st_mode) == 0o664


def test_a_missing_state_root_does_not_raise(tmp_path, monkeypatch):
    monkeypatch.setattr(common, "STATE_DIR", str(tmp_path / "nope"))
    assert common.secure_state_tree() is None


# ---- a private directory inside a writable one is not private --------------

def test_a_writable_ancestor_is_reported(tmp_path, monkeypatch):
    # The error the first version of this fix made: 0700 on the leaf, 0775 on its parent, and
    # a peer renames the leaf aside and installs their own. The daemon may not own the parent,
    # so it names it rather than changing it.
    parent = tmp_path / "share"
    parent.mkdir()
    parent.chmod(0o775)
    root = parent / "state"
    root.mkdir()
    root.chmod(0o700)
    monkeypatch.setattr(common, "STATE_DIR", str(root))
    reported = common.unsafe_state_ancestors()
    assert any(str(parent) in line for line in reported), reported
    assert "0o775" in " ".join(reported)


def test_a_private_ancestor_chain_reports_nothing(tmp_path, monkeypatch):
    parent = tmp_path / "share"
    parent.mkdir()
    parent.chmod(0o755)
    root = parent / "state"
    root.mkdir()
    monkeypatch.setattr(common, "STATE_DIR", str(root))
    # tmp_path's own ancestry is pytest's to arrange; assert only about what this test made.
    assert not [line for line in common.unsafe_state_ancestors() if str(parent) in line]


def test_a_sticky_ancestor_is_not_reported(tmp_path, monkeypatch):
    # /tmp is 1777 and shared on purpose: the sticky bit means you may only remove your own
    # entries, so write there is not permission to replace someone else's directory.
    parent = tmp_path / "shared"
    parent.mkdir()
    parent.chmod(0o1777)
    root = parent / "state"
    root.mkdir()
    monkeypatch.setattr(common, "STATE_DIR", str(root))
    assert not [line for line in common.unsafe_state_ancestors() if str(parent) in line]


# ---- the daemon actually calls it, before it reads anything ----------------

def _main_call_lines():
    """Line number of each call in `daemon.main`, by name. Parsed, not string-searched.

    Searching the source text finds a name in a COMMENT as readily as in a call. The first
    version of the ordering test below did that and failed on a comment that named the very
    function it was checking for — a check that cannot tell code from prose about code.
    """
    tree = ast.parse((ROOT / "bridge/daemon.py").read_text())
    main = next((n for n in tree.body
                 if isinstance(n, ast.FunctionDef) and n.name == "main"), None)
    assert main, "daemon.main is no longer a top-level function"
    lines = {}
    for node in ast.walk(main):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            lines.setdefault(node.func.id, node.lineno)
    return lines


def test_the_daemon_secures_state_before_it_reads_any():
    # Every test above exercises the function; none of them fails if the CALL is deleted from
    # daemon.main(), which is where it has to happen and where it was in the wrong place —
    # load_offset() ran first and read state the remediation had not reached yet (#245 review,
    # #241). Assert the seam, not just the part that is easy to call.
    calls = _main_call_lines()
    assert "secure_state_tree" in calls, "daemon.main no longer secures the state tree"
    assert "load_offset" in calls, "daemon.main no longer reads the offset"
    assert calls["secure_state_tree"] < calls["load_offset"], (
        "state is read before it is secured — the first reader wins the race the "
        "remediation exists to close")
    assert "unsafe_state_ancestors" in calls, (
        "daemon.main no longer warns about ancestors it cannot fix")


def test_nothing_reads_state_at_import_time():
    """The claim "before anything reads state" is about the PROCESS, not about main().

    Asserting the order of lines inside `main` cannot see a read that happens at import, and
    one did: `pending_reopens = _load_pending_reopens()` sat at module scope and parsed a file
    from the state directory before `main` had begun. Two comments claimed the remediation came
    first and were believed because they were comments (#233). This asks the process.
    """
    source = (ROOT / "bridge/daemon.py").read_text()
    tree = ast.parse(source)
    offenders = []
    for node in tree.body:                      # top level only — this is what import executes
        if isinstance(node, (ast.FunctionDef, ast.ClassDef, ast.Import, ast.ImportFrom)):
            continue
        for sub in ast.walk(node):
            if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name) and (
                    sub.func.id in ("state_path", "load_offset", "read_registry")
                    or sub.func.id.startswith("_load")):
                offenders.append(f"{sub.func.id}() at line {sub.lineno}")
    assert not offenders, (
        "these run at import, before main() can secure the state tree: " + ", ".join(offenders))


def test_the_pending_questions_are_loaded_after_the_tree_is_secured():
    calls = _main_call_lines()
    assert "_load_pending_reopens" in calls, (
        "the pending questions are no longer loaded in main — check they did not move back "
        "to module scope, where they are read at import")
    assert calls["secure_state_tree"] < calls["_load_pending_reopens"]


# ---- the daemon says what it changed, and not more ---------------------------

def test_the_report_does_not_call_an_already_private_root_writable(tmp_path, monkeypatch):
    # `secure_state_tree` returns the ROOT's before and after. When the root was already
    # private and only entries inside it were not, those are equal — and the first version of
    # the daemon's log line said "state directory was mode 0o700, which let other local
    # accounts write to it", which is false about 0o700. The caller has to be able to tell the
    # two cases apart, so the return value has to make them distinguishable.
    root = tmp_path / "state"
    (root / "topics").mkdir(parents=True)
    (root / "topics" / "inbox.jsonl").write_text("{}\n")
    (root / "topics" / "inbox.jsonl").chmod(0o664)
    (root / "topics").chmod(0o700)
    root.chmod(0o700)
    monkeypatch.setattr(common, "STATE_DIR", str(root))

    changed, before, after = common.secure_state_tree()
    assert changed == 1, "only the inbox needed narrowing"
    assert before == after == "0o700", (
        "the root did not change, so before and after must be equal — otherwise the caller "
        "cannot tell 'the directory was open' from 'something inside it was'")


# ---- the units run with a private umask ------------------------------------

@pytest.mark.parametrize("unit", sorted(p.name for p in (ROOT / "systemd").glob("*.service")))
def test_every_service_unit_sets_a_private_umask(unit):
    # Without this the daemon inherits the umask of whoever started the user manager, and
    # `umask 002` makes every inbox it writes group-writable.
    text = (ROOT / "systemd" / unit).read_text()
    assert "UMask=0077" in text, f"{unit} would inherit the login umask"


# ---- the installer will not write into a directory others can write --------

def _destination_check():
    """The real `check_destination`, lifted out of the installer rather than reimplemented.

    A copy of the logic keeps passing after somebody edits the installer, which is the one
    thing a test of the installer must not do.
    """
    source = INSTALL.read_text()
    lifted = ""
    for name in ("writable_by_others", "walk_to_root", "check_destination"):
        found = re.search(rf"^{name} \(\) \{{\n(.*?)^\}}\n", source, re.S | re.M)
        assert found, f"{name} is no longer a top-level function in scripts/install.sh"
        lifted += f"{name} () {{\n" + found.group(1) + "}\n"
    return lifted


def _run_check(tmp_path, mode, parent_mode=0o755, make_target=True):
    """Run the real check against a destination inside a parent whose mode we also control."""
    parent = tmp_path / "parent"
    parent.mkdir()
    target = parent / "dest"
    if make_target:
        target.mkdir()
        target.chmod(mode)
    parent.chmod(parent_mode)
    script = (f'set -euo pipefail\nHOME={tmp_path}\n'
              + _destination_check()
              + 'die() { printf "%s\\n" "$*" >&2; exit 1; }\n'
              + f'check_destination "{target}" "The launcher"\n')
    result = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    parent.chmod(0o755)      # so pytest can clean up after a 0500 fixture
    return result


@pytest.mark.parametrize("mode", [0o700, 0o755, 0o750, 0o705])
def test_a_destination_only_you_can_write_is_accepted(tmp_path, mode):
    assert _run_check(tmp_path, mode).returncode == 0


@pytest.mark.parametrize("mode", [0o775, 0o757, 0o777, 0o770, 0o707])
def test_a_destination_others_can_write_is_refused(tmp_path, mode):
    result = _run_check(tmp_path, mode)
    assert result.returncode != 0
    assert "chmod go-w" in result.stderr


def test_the_refusal_names_who_else_could_write(tmp_path):
    # "Writable by others" is abstract; a group with a service account in it is not. The
    # exposure on the machine this was found on was `www-data` in the owner's own group.
    result = _run_check(tmp_path, 0o775)
    assert "Group members besides you" in result.stderr


def test_a_writable_parent_is_refused_however_private_the_destination():
    # The error the first version made. Write on a directory is permission to rename any entry
    # in it, so a 0700 destination inside a 0775 parent is replaceable wholesale and its own
    # mode never comes into it.
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        result = _run_check(pathlib.Path(tmp), 0o700, parent_mode=0o775)
    assert result.returncode != 0, "a private directory inside a writable one was accepted"
    assert "chmod go-w" in result.stderr and "parent" in result.stderr


def test_a_sticky_parent_is_accepted(tmp_path):
    # /tmp is 1777 by design: writable, but you may only remove your own entries, so it is not
    # replacement permission. Refusing it would fail every install under a shared scratch dir.
    assert _run_check(tmp_path, 0o755, parent_mode=0o1777).returncode == 0


def test_the_check_runs_before_the_directory_is_made(tmp_path):
    # The destination does not exist yet — the ordinary first install. Checking only after
    # `mkdir -p` means the directory has already been created inside a writable parent, where
    # a peer can replace it before the launcher is even written.
    result = _run_check(tmp_path, 0o755, parent_mode=0o775, make_target=False)
    assert result.returncode != 0, "a not-yet-created destination skipped the check entirely"


def test_the_installer_checks_every_destination_it_writes_to():
    source = INSTALL.read_text()
    calls = re.findall(r'check_destination "\$(?:\(dirname "\$)?(\w+)', source)
    assert set(calls) == {"PREFIX", "UNIT_DIR", "CONFIG_DIR", "STATE_PARENT", "SKILL_LINK"}, (
        "the launcher, units, migrated config/state, and skill are the five destinations "
        f"where a peer account can change what runs or what the bridge reads; found {calls}")


def test_every_check_precedes_every_write():
    """Not "each check before its own write" — ALL checks before the FIRST write.

    Checking each destination just before writing it means a refusal on the last one leaves
    the earlier ones already installed: a launcher on PATH shadowing the real command, and
    rewritten unit files, with no daemon-reload and no way for the operator to know which
    half ran. The installer either writes nothing or it writes everything.
    """
    source = INSTALL.read_text()
    last_check = max(source.rindex(c) for c in (
        'check_destination "$PREFIX"',
        'check_destination "$UNIT_DIR"',
        'check_destination "$(dirname "$SKILL_LINK")"'))
    first_write = min(source.index(w) for w in ('mkdir -p "$PREFIX"', 'mkdir -p "$UNIT_DIR"'))
    assert last_check < first_write, (
        "a destination is checked after the first thing is written, so a refusal can leave a "
        "half-installed system behind")


def test_units_are_written_with_an_explicit_mode():
    source = INSTALL.read_text()
    assert "os.chmod(target, 0o644)" in source, (
        "unit files written at the umask's discretion are group-writable under umask 002, and "
        "a rewritten unit runs on the next daemon-reload")


# ---- what the round-2 mutation pass proved was NOT held ----------------------
#
# A cross-family seat reverted every production line on this branch one at a time. Ten
# survived a green suite. Each test below kills one of them. They are grouped here rather
# than filed beside their subject because what they have in common is the reason they were
# missing: every one asserts a BEHAVIOUR that no test was watching — a log that is emitted,
# a descriptor that is closed, a walk that survives a hostile filesystem, a symlink that is
# followed — while the tests that existed asserted the shape of the code around it.


def test_the_daemon_actually_says_what_it_narrowed(tmp_path, monkeypatch, capsys):
    # Replacing the whole reporting block with `pass` left the suite green. Silently changing
    # modes on somebody's filesystem is the thing the block exists to prevent, so the block
    # working is the feature — not its presence in the source.
    from bridge import daemon
    root = tmp_path / "state"
    (root / "topics").mkdir(parents=True)
    (root / "topics" / "inbox.jsonl").write_text("{}\n")
    (root / "topics" / "inbox.jsonl").chmod(0o664)
    root.chmod(0o775)
    monkeypatch.setattr(common, "STATE_DIR", str(root))

    said = []
    monkeypatch.setattr(daemon, "log", said.append)
    monkeypatch.setattr(daemon, "load_config", lambda: {"bot_token": "1:x", "chat_id": -1,
                                                        "owner_id": 1})
    monkeypatch.setattr(daemon, "restore_on_boot",
                        lambda cfg: (_ for _ in ()).throw(SystemExit))
    with pytest.raises(SystemExit):
        daemon.main()

    narrowed = " ".join(said)
    assert "0o775" in narrowed and "0o755" in narrowed, (
        f"the daemon changed modes without saying which: {said}")
    assert "inbox" in narrowed, "it did not say why a peer writing there would matter"


def test_the_daemon_names_an_ancestor_it_cannot_fix(tmp_path, monkeypatch):
    # The other `pass`-able block. An exposure the daemon deliberately does not fix is worth
    # nothing unless it reaches the operator.
    from bridge import daemon
    parent = tmp_path / "share"
    parent.mkdir()
    parent.chmod(0o775)
    root = parent / "state"
    root.mkdir()
    root.chmod(0o700)
    monkeypatch.setattr(common, "STATE_DIR", str(root))

    said = []
    monkeypatch.setattr(daemon, "log", said.append)
    monkeypatch.setattr(daemon, "load_config", lambda: {"bot_token": "1:x", "chat_id": -1,
                                                        "owner_id": 1})
    monkeypatch.setattr(daemon, "restore_on_boot",
                        lambda cfg: (_ for _ in ()).throw(SystemExit))
    with pytest.raises(SystemExit):
        daemon.main()

    warned = [line for line in said if str(parent) in line]
    assert warned, f"the writable ancestor was found and never mentioned: {said}"
    assert "WARNING" in warned[0]


def test_a_rejected_config_does_not_leak_its_descriptor(tmp_path, monkeypatch):
    # Removing `os.close(fd)` on the rejection path survived the suite. A daemon that refuses
    # a config once does not care; one that re-reads on a schedule runs out of descriptors.
    path = tmp_path / "config.json"
    path.write_text("{}")
    path.chmod(0o666)
    monkeypatch.setattr(common, "CONFIG_PATH", str(path))
    before = len(os.listdir("/proc/self/fd"))
    for _ in range(64):
        handle, rejection = common._open_secret_file(str(path))
        assert handle is None and rejection
    after = len(os.listdir("/proc/self/fd"))
    assert after <= before + 2, f"descriptors leaked on the rejection path: {before} -> {after}"


def test_the_walk_survives_an_entry_that_cannot_be_changed(tmp_path, monkeypatch):
    # `except OSError: continue` in both loops. Turning either into a raise survived, because
    # nothing exercised a tree the process cannot fully modify — which is exactly the tree a
    # shared host produces. A daemon that dies on startup because one entry resisted a chmod
    # has turned a hardening step into an outage.
    root = tmp_path / "state"
    (root / "topics").mkdir(parents=True)
    (root / "topics" / "inbox.jsonl").write_text("{}\n")
    (root / "topics" / "inbox.jsonl").chmod(0o664)
    root.chmod(0o775)
    monkeypatch.setattr(common, "STATE_DIR", str(root))

    real_chmod = os.chmod

    def refuse_the_inbox(path, mode, *args, **kwargs):
        if str(path).endswith("inbox.jsonl"):
            raise PermissionError(13, "Operation not permitted")
        return real_chmod(path, mode, *args, **kwargs)

    monkeypatch.setattr(common.os, "chmod", refuse_the_inbox)
    result = common.secure_state_tree()          # must not raise
    assert result, "the walk gave up entirely because one entry refused"
    assert not stat.S_IMODE(root.stat().st_mode) & 0o022, "the root was left open"


def test_the_walk_survives_an_entry_that_vanishes(tmp_path, monkeypatch):
    root = tmp_path / "state"
    (root / "topics").mkdir(parents=True)
    doomed = root / "topics" / "gone.jsonl"
    doomed.write_text("{}\n")
    doomed.chmod(0o664)
    root.chmod(0o775)
    monkeypatch.setattr(common, "STATE_DIR", str(root))

    real_lstat = os.lstat

    def vanish(path, *args, **kwargs):
        if str(path).endswith("gone.jsonl"):
            raise FileNotFoundError(2, "No such file or directory")
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(common.os, "lstat", vanish)
    assert common.secure_state_tree(), "a file disappearing mid-walk aborted the whole walk"


def test_an_unreadable_ancestor_stops_the_walk_without_raising(tmp_path, monkeypatch):
    # `break` on OSError while walking upward. A raise here kills daemon startup on any host
    # where one directory above the state root cannot be stat'ed.
    root = tmp_path / "state"
    root.mkdir()
    monkeypatch.setattr(common, "STATE_DIR", str(root))

    real_stat = os.stat

    def blind(path, *args, **kwargs):
        if str(path) == str(tmp_path):
            raise PermissionError(13, "Permission denied")
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(common.os, "stat", blind)
    assert common.unsafe_state_ancestors() == []   # must not raise


def test_the_process_creates_private_files_whatever_umask_it_inherited(tmp_path):
    # UMask=0077 lives in the unit files, and unit files are not replaced by `git pull` or by
    # a release cut — so a code-only upgrade left three of four bridge units at 0002. This is
    # the same guarantee one layer in, where the upgrade actually reaches.
    old = os.umask(0o002)
    try:
        assert common.secure_process_umask() == 0o002
        probe = tmp_path / "created.json"
        probe.write_text("{}")
        assert not stat.S_IMODE(probe.stat().st_mode) & 0o077, (
            "a file created after securing the umask is still readable by the group")
    finally:
        os.umask(old)


def _entry_points_that_touch_state():
    """Every module that both runs as a program and reaches the state directory.

    Discovered, not listed. The first version of this test named the four modules that had
    already been fixed, so it passed while `bridge/watchdog.py` — a program that writes
    `watchdog.json` — went on inheriting the login umask. A list written by the person who
    did the fixing cannot find what that person missed; it can only agree with them.
    """
    found = []
    for path in sorted((ROOT / "bridge").glob("*.py")):
        text = path.read_text()
        if '__name__ == "__main__"' in text and "state_path" in text:
            found.append(path.stem)
    assert len(found) >= 4, f"entry-point discovery is broken, found only {found}"
    return found


@pytest.mark.parametrize("entry", _entry_points_that_touch_state())
def test_every_entry_point_secures_its_own_umask(entry):
    # Several processes write state, not one. The daemon having it is not the deployment
    # having it — on the host this was found on, the digest, watchdog and model-watchdog
    # units all ran at 0002 while the daemon ran at 0077.
    module = __import__(f"bridge.{entry}", fromlist=["main"])
    assert callable(getattr(module, "secure_process_umask", None)), (
        f"bridge/{entry}.py calls secure_process_umask() but never imported it — the call "
        "raises NameError at runtime and no import-time check can see it")
    tree = ast.parse(pathlib.Path(f"bridge/{entry}.py").read_text())
    main = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "main")
    called = [n.func.id for n in ast.walk(main)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)]
    assert "secure_process_umask" in called, f"bridge/{entry}.py main() does not secure it"


# ---- the installer follows the link before it trusts the path ---------------

def test_a_destination_symlinked_into_a_shared_directory_is_refused(tmp_path):
    # The lexical walk follows the name that was typed. If a component of it is a symlink the
    # bytes land elsewhere, and that elsewhere has its own ancestry — which the four reverted
    # `stat -L` mutations and this case both go through.
    shared = tmp_path / "shared"
    shared.mkdir()
    real = shared / "bin"
    real.mkdir()
    real.chmod(0o755)                # chmod, not the umask's answer: under 002 this arrives
                                     # 0775 and the LEXICAL walk refuses it, so the test would
                                     # pass without ever reaching the resolved walk it is for
    shared.chmod(0o775)              # the exposure is here, one level above the target
    lexical = tmp_path / "private"
    lexical.mkdir()
    lexical.chmod(0o755)             # likewise: 0775 here refuses on the LEXICAL walk, and
    os.chmod(tmp_path, 0o755)        # so does the fixture root. Every mode that decides which
                                     # branch runs is set, never left to the ambient umask.
    link = lexical / "dest"
    link.symlink_to(real)

    script = ('set -euo pipefail\nHOME=%s\n' % tmp_path
              + _destination_check()
              + 'die() { printf "%s\\n" "$*" >&2; exit 1; }\n'
              + f'check_destination "{link}" "The launcher"\n')
    result = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    shared.chmod(0o755)
    assert result.returncode != 0, (
        "a destination resolving into a directory others can write was accepted; the lexical "
        "path was clean and the bytes would have landed in the shared one")
    assert "resolves to" in result.stderr


def test_a_foreign_owned_ancestor_inside_your_home_is_refused(tmp_path):
    # Ownership was checked only at the leaf. An ancestor somebody else owns is one they can
    # chmod whenever they like, so today's modes below it prove nothing.
    script = ('set -euo pipefail\nHOME=%s\n' % tmp_path
              + _destination_check()
              + 'die() { printf "%s\\n" "$*" >&2; exit 1; }\n'
              + 'stat () { if [[ $* == *"%s"* && $* == *%%U* ]]; then echo somebodyelse; '
                'else command stat "$@"; fi; }\n' % tmp_path
              + f'check_destination "{tmp_path}/a/b" "The launcher"\n')
    (tmp_path / "a" / "b").mkdir(parents=True)
    result = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    assert result.returncode != 0, "an ancestor owned by another account was accepted"
    assert "owned by somebodyelse" in result.stderr


def test_the_ancestor_walk_is_a_named_function_the_tests_can_lift():
    # `_destination_check` extracts the real functions from install.sh rather than copying
    # them. If the walk is inlined back into check_destination, the extraction silently stops
    # covering it and every installer test above quietly narrows.
    assert "walk_to_root () {" in INSTALL.read_text()


def test_the_destination_owner_check_asks_about_the_target(tmp_path):
    # `stat -c '%U'` on a symlink reports the LINK's owner; the link is yours, the directory it
    # points into may not be. Two UIDs are needed to observe that for real, so the harness
    # supplies the second: `stat` answers differently with and without `-L`, which is exactly
    # the difference the flag exists to make. Without `-L` this check asks whether you own the
    # signpost instead of whether you own the ground.
    real = tmp_path / "elsewhere"
    real.mkdir()
    real.chmod(0o755)
    link = tmp_path / "dest"
    link.symlink_to(real)
    os.chmod(tmp_path, 0o755)

    fake_stat = (
        'stat () {\n'
        '    if [[ $1 == -Lc && $2 == "%U" && $3 == "' + str(link) + '" ]]; then\n'
        '        echo somebodyelse\n'          # the TARGET's owner
        '    elif [[ $1 == -c && $2 == "%U" && $3 == "' + str(link) + '" ]]; then\n'
        '        echo "$(id -un)"\n'           # the LINK's owner — you
        '    else\n'
        '        command stat "$@"\n'
        '    fi\n'
        '}\n')
    script = (f'set -euo pipefail\nHOME={tmp_path}\n'
              + _destination_check() + fake_stat
              + 'die() { printf "%s\\n" "$*" >&2; exit 1; }\n'
              + f'check_destination "{link}" "The launcher"\n')
    result = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    assert result.returncode != 0, (
        "the destination resolves into a directory owned by another account and was accepted; "
        "the ownership question was asked about the symlink instead of about its target")
    assert "somebodyelse" in result.stderr


def test_a_foreign_owned_destination_outside_your_home_is_refused(tmp_path):
    """`--prefix /opt/tools` where /opt/tools belongs to somebody else.

    The ancestor walk deliberately tolerates a foreign owner above $HOME — root owning `/` and
    `/home` is correct, not a finding — so outside the home tree the destination's own
    ownership check is the only thing left asking whose directory this is. That makes it
    load-bearing exactly where the walk stops looking.
    """
    dest = tmp_path / "opt" / "tools"
    dest.mkdir(parents=True)
    dest.chmod(0o755)
    (tmp_path / "opt").chmod(0o755)
    os.chmod(tmp_path, 0o755)
    home = tmp_path / "home"          # $HOME elsewhere, so `dest` is outside it
    home.mkdir()

    fake_stat = (
        'stat () {\n'
        '    if [[ $2 == "%U" && $3 == "' + str(dest) + '" ]]; then echo somebodyelse\n'
        '    else command stat "$@"; fi\n'
        '}\n')
    script = (f'set -euo pipefail\nHOME={home}\n'
              + _destination_check() + fake_stat
              + 'die() { printf "%s\\n" "$*" >&2; exit 1; }\n'
              + f'check_destination "{dest}" "The launcher"\n')
    result = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    assert result.returncode != 0, (
        "a destination owned by another account, outside $HOME where the ancestor walk stops "
        "checking ownership, was accepted")
    assert "owned by somebodyelse" in result.stderr
