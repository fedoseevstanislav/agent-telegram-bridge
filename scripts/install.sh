#!/usr/bin/env bash
# Install the bridge for the current user (#204 §5).
#
# Nothing needs root, and nothing is written outside the locations printed at the end. Those
# are all under $HOME unless you point --prefix somewhere else, which is the one way to make
# that untrue.
#
# Why a script rather than "copy these files": the repository can be cloned anywhere, and both
# the launcher and the systemd units need that absolute path baked in. systemd user units
# cannot expand a variable in ExecStart, so the substitution has to happen at install time.
# Doing it by hand is where a fresh install goes wrong.
#
#   scripts/install.sh [--prefix DIR] [--force]
#
# --prefix  where to put the `tg-bridge` launcher (default: ~/.local/bin)
# --force   overwrite an existing launcher, unit files, or skill symlink

set -euo pipefail

PREFIX="$HOME/.local/bin"
FORCE=0
while [[ $# -gt 0 ]]; do
    case $1 in
        --prefix) PREFIX=${2:?--prefix needs a directory}; shift 2 ;;
        --force)  FORCE=1; shift ;;
        -h|--help) sed -n '2,16p' "$0" | sed -e 's/^# \{0,1\}//'; exit 0 ;;
        *) printf 'install.sh: unknown argument %q\n' "$1" >&2; exit 2 ;;
    esac
done

ROOT=$(cd -- "$(dirname -- "$(readlink -f -- "${BASH_SOURCE[0]}")")/.." && pwd)
UNIT_DIR="$HOME/.config/systemd/user"
CONFIG="$HOME/.config/claude-telegram-bridge/config.json"

die() { printf 'install.sh: %s\n' "$*" >&2; exit 1; }
step() { printf '\n== %s\n' "$*"; }

# --- checks that must pass before anything is written ------------------------------------

step "Checking prerequisites"

PYTHON=$(command -v python3) || die "python3 is not on PATH"
PYVER=$("$PYTHON" -c 'import sys; print("%d.%d" % sys.version_info[:2])')
[[ $PYVER == 3.12 ]] || die "python3 is $PYVER; this project pins 3.12 (see pyproject.toml).
    Install 3.12 and re-run with PATH pointing at it."
printf '  python3   %s (%s)\n' "$PYVER" "$PYTHON"

command -v tmux >/dev/null || die "tmux is not installed.
    The daemon starts agent sessions in tmux panes and types into them; without tmux the
    /claude and /codex commands cannot work. Install it and re-run."
printf '  tmux      %s\n' "$(tmux -V)"

command -v systemctl >/dev/null || die "systemctl is not available.
    This installer targets systemd user services. On a host without systemd, run
    bridge/daemon.py under whatever supervisor you do have and skip this script."
systemctl --user show-environment >/dev/null 2>&1 ||
    die "systemd user instance is not reachable.
    On a headless server this usually means lingering is off. Fix with:
        sudo loginctl enable-linger $USER
    then log in again and re-run."
printf '  systemd   user instance reachable\n'

# The config is the one thing this script will not invent for you: it holds a credential and
# two identifiers that only you can supply. See docs/INSTALL.md step 1.
[[ -f $CONFIG ]] || die "no config at $CONFIG
    Create it first — docs/INSTALL.md step 1 walks through getting each value:
        {\"bot_token\": \"...\", \"chat_id\": -100…, \"owner_id\": …}"
"$PYTHON" - "$CONFIG" <<'PY' || die "config is not usable (see the message above)"
import json, os, stat, sys
path = sys.argv[1]
mode = stat.S_IMODE(os.stat(path).st_mode)
if mode & 0o077:
    sys.exit(f"  {path} is mode {mode:04o}; it holds a bot token. chmod 600 it and re-run.")
try:
    cfg = json.load(open(path))
except json.JSONDecodeError as e:
    sys.exit(f"  {path} is not valid JSON: {e}")
for key, want in (("bot_token", str), ("chat_id", int), ("owner_id", int)):
    if key not in cfg:
        sys.exit(f"  {path} is missing {key!r}")
    if isinstance(cfg[key], bool) or not isinstance(cfg[key], want):
        sys.exit(f"  {path}: {key!r} must be {want.__name__}, got {type(cfg[key]).__name__}")
if cfg["owner_id"] <= 0:
    sys.exit(f"  {path}: 'owner_id' must be a positive Telegram user id")
if cfg["chat_id"] >= 0:
    sys.exit(f"  {path}: 'chat_id' must be the negative -100… supergroup id, not a positive id")
print(f"  config    {path} (owner_id and chat_id present, mode {mode:04o})")
PY

# The installer copies no code: the launcher execs `$ROOT/bridge/cli.py` and every unit's
# ExecStart points back into this clone. The clone IS the deployment, so whoever can write these
# files decides what the bridge runs as you. This is the boundary where that stops being
# theoretical, which is why the check lives here rather than in the test suite — there it fired
# on every `pytest` in a fresh clone, where a 0664 from an ordinary umask 002 is almost always
# harmless, and a check that cries wolf on a normal install teaches people to ignore it (#231).
#
# Calibrated to what is actually exploitable, which is the mode TOGETHER WITH who is in the
# group: world-writable is a write primitive for anybody, while group-writable matters only when
# the group has a member besides you. On the private per-user groups Debian and Ubuntu create it
# does not, and this stays silent.
#
# WHAT IT ESTABLISHES, stated no wider than it is: at this moment, under ordinary Unix
# permissions, no other account can change the bytes under this clone — every file in the
# runtime trees, their directories up to the clone root, and the ownership of both.
#
# WHAT IT DOES NOT. An ACL can grant a named principal write access that the group bit does not
# reveal. A process that opened a file while it was writable keeps that handle after a chmod.
# Root and anything holding CAP_DAC_OVERRIDE ignore all of it, as does whoever controls a
# network or FUSE backing store. Directories ABOVE the clone are the system's to get right. And
# it is a check at one instant, not a guarantee over time — nothing here stops a later chmod.
#
# That list is why the gate refuses a symlink instead of following it, and why the sentence
# above is bounded: three earlier versions of this check claimed more than they verified, each
# time because the claim was written from the intent rather than from an enumeration of the ways
# it could still be false (#233).
#
# `bin/tg-bridge` being executable is deliberately not checked here: `--write` and
# `--check-host` still check it, and the launcher this script writes is a separate file.
"$PYTHON" - "$ROOT" <<'MODES' || die "refusing to install code that someone else can rewrite"
import grp, os, pwd, stat, sys
from pathlib import Path

root = Path(sys.argv[1])
me = pwd.getpwuid(os.getuid())

# EVERY file under the runtime tree, not just `*.py`. Python executes a cached `.pyc` in
# preference to its source when the timestamp matches, and a native extension module is not a
# `.py` at all — a gate that enumerated only source accepted a 0666 `__pycache__/helper.pyc`
# whose bytes then ran instead of the benign file beside it (#231 review, scoped pass).
TREES = ("bridge", "systemd", "skill")
runtime, links = [], []
for name in (*TREES, "bin/tg-bridge"):
    start = root / name
    if not start.exists():
        continue
    if start.is_file():
        runtime.append(start)
        continue
    for here, dirs, files in os.walk(start, followlinks=False):
        for entry in (*dirs, *files):
            path = Path(here) / entry
            # A symlink is REFUSED, not followed. Following it means checking the target's real
            # ancestry, which the lexical parent walk below cannot do; and a clone of this
            # repository contains no symlink in these trees, so refusing costs nothing and is
            # the honest answer to a path whose safety this check cannot establish.
            if path.is_symlink():
                links.append(path)
            elif path.is_file():
                runtime.append(path)
if (root / "bin/tg-bridge").is_symlink():
    links.append(root / "bin/tg-bridge")
if not runtime and not links:
    sys.exit("  no runtime files under %s — is this a complete clone?" % root)

# The DIRECTORIES matter as much as the files. Write permission on a directory is permission to
# unlink what is in it and create something else with the same name, so a 0644 module inside a
# group-writable package directory is replaceable by anyone in that group — the file's own mode
# never comes into it. Checked from each file up to the clone root; above that is the system's.
targets = set(runtime)
for path in runtime:
    parent = path.parent
    while True:
        targets.add(parent)
        if parent == root or parent == parent.parent:
            break
        parent = parent.parent

def other_members(gid):
    """Everyone in this group who is not you. Empty means it is your own private group."""
    try:
        group = grp.getgrgid(gid)
    except KeyError:
        return ["gid %d" % gid]      # unresolvable: treat as shared rather than assume safe
    members = set(group.gr_mem)
    for entry in pwd.getpwall():     # a primary-group member is not listed in gr_mem
        if entry.pw_gid == gid:
            members.add(entry.pw_name)
    return sorted(members - {me.pw_name})

def show(path):
    rel = path.relative_to(root).as_posix() if path != root else "."
    return rel + "/" if path.is_dir() and not path.is_symlink() else rel

foreign, world, shared = [], [], {}
for path in sorted(targets):
    info = path.stat()
    mode = stat.S_IMODE(info.st_mode)
    if info.st_uid != me.pw_uid:
        try:
            owner = pwd.getpwuid(info.st_uid).pw_name
        except KeyError:
            owner = "uid %d" % info.st_uid
        foreign.append("%s (%s)" % (show(path), owner))
    elif mode & 0o002:
        world.append(show(path))
    elif mode & 0o020:
        others = other_members(info.st_gid)
        if others:
            shared.setdefault(", ".join(others), []).append(show(path))

if links:
    sys.exit("  %d symlink(s) in the runtime tree, including %s.\n"
             "  This check cannot establish who controls what they point at, and a clone of\n"
             "  this repository has none. Replace them with real files, or install from a\n"
             "  clean clone." % (len(links), show(links[0])))
# Ownership before modes: the owner of a file can reopen its permissions whenever they like, so
# a mode you like on a file you do not own proves nothing.
if foreign:
    sys.exit("  %d path(s) here belong to somebody else, including %s.\n"
             "  Their owner can change what the bridge runs whenever they choose.\n"
             "  Install from a clone you own." % (len(foreign), foreign[0]))
if world:
    sys.exit("  %d path(s) are WORLD-writable, including %s.\n"
             "  Any account on this host could change what the bridge runs. Fix with:\n"
             "      chmod -R o-w %s" % (len(world), world[0], root))
if shared:
    who = "; ".join("%s can write %d path(s), e.g. %s" % (names, len(paths), paths[0])
                    for names, paths in sorted(shared.items()))
    sys.exit("  paths here are writable by others in their group: %s.\n"
             "  They could change what the bridge runs as you. Fix with:\n"
             "      chmod -R g-w %s" % (who, root))
print("  modes     %d runtime files and %d directories, writable only by you"
      % (len(runtime), len(targets) - len(runtime)))
MODES

# --- write ---------------------------------------------------------------------------------

exists() { [[ -e $1 || -L $1 ]]; }
guard() { exists "$1" && [[ $FORCE -eq 0 ]] &&
    die "$1 already exists. Re-run with --force to replace it."; return 0; }

# A destination another account can write is as dangerous as a source another account can
# write, and until #245 this script checked only the source. The launcher directory is the
# sharper case: spawned agent sessions put $PREFIX first on PATH and invoke `claude` and
# `codex` by bare name, so a peer who can write there replaces the binary and the owner's
# next legitimate /claude runs it as the owner — no agent approval prompt involved, because
# the wrong program is already running. Same for the unit directory: a rewritten unit runs
# on the next reload.
#
# The check is on the whole path, not the leaf. Write permission on a DIRECTORY is permission
# to unlink and rename any entry in it, whatever that entry's own mode says — so a 0755
# launcher directory inside a 0775 parent is not protected at all: a peer renames it aside and
# puts their own in its place. Checking the leaf alone is checking the wrong object, and it was
# the first version of this fix. `-L` throughout: `stat` without it reports a symlink's own
# bits, which are always 777 and which the kernel ignores.
writable_by_others () {
    local mode
    mode=$(printf '%04d' "$(stat -Lc '%a' "$1")")
    # The sticky bit is what makes /tmp safe to share: write, but you may only remove your own
    # entries. Without it, group or other write is permission to replace someone else's.
    if (( 10#${mode:0:1} & 1 )); then return 1; fi
    if (( (10#${mode:2:1} | 10#${mode:3:1}) & 2 )); then return 0; fi
    return 1
}

# Walk one path from its deepest existing component up to /, refusing anything others can
# write or anyone else owns. Ownership is checked at EVERY level, not only at the leaf: an
# ancestor somebody else owns is an ancestor they can chmod, so their write bit is replacement
# authority over everything below it whatever the modes say today.
walk_to_root () {
    local start=$1 what=$2 node me
    me=$(id -un)
    node=$start
    while [[ ! -d $node ]]; do
        node=$(dirname "$node")
    done
    while : ; do
        local owner mode
        owner=$(stat -Lc '%U' "$node")
        mode=$(stat -Lc '%a' "$node")
        # Above $HOME the path is the system's, and root owning / and /home is correct rather
        # than a finding. Refuse a foreign owner only while still inside the user's own tree.
        if [[ $owner != "$me" && $node == "$HOME"/* ]]; then
            die "$node is owned by $owner, not you.
    $what goes below it, and its owner can change its mode at any time, so nothing beneath it
    is yours to rely on."
        fi
        if writable_by_others "$node"; then
            local peers
            peers=$(getent group "$(stat -Lc '%G' "$node")" | cut -d: -f4)
            die "$node is mode $mode — writable by others.
    $what goes below it, and write on a directory is permission to replace anything inside it,
    whatever that thing's own mode says. Group members besides you: ${peers:-none listed}
    Fix with:
        chmod go-w $node"
        fi
        [[ $node == / ]] && break
        node=$(dirname "$node")
    done
}

check_destination () {
    local dir=$1 what=$2 owner resolved
    # Before mkdir, not after: a directory created inside a writable parent is already
    # replaceable by the time you would get around to looking at it.
    if [[ -d $dir ]]; then
        owner=$(stat -Lc '%U' "$dir")
        [[ $owner == "$(id -un)" ]] || die "$dir is owned by $owner, not you.
    $what would be written somewhere you do not control."
    fi
    walk_to_root "$dir" "$what"

    # And again along the RESOLVED path. The lexical walk above follows the name the operator
    # typed; if any component of it is a symlink, the bytes land somewhere else entirely and
    # that somewhere has its own ancestry. A `~/.local/bin -> /srv/shared/bin` with a 0775
    # `/srv/shared` passed the lexical walk cleanly — the destination is only as safe as the
    # directory the kernel actually writes into.
    resolved=$(readlink -f -- "$dir" 2>/dev/null || true)
    if [[ -n $resolved && $resolved != "$dir" ]]; then
        walk_to_root "$resolved" "$what (which $dir resolves to)"
    fi
}

# Every destination, before the first write. Checking each one just before its own write is
# how a refusal on the LAST of them leaves a half-installed system behind: a group-writable
# skill parent let a run write the launcher and units before the final destination check died —
# a shadowing launcher on PATH and rewritten units, with no daemon-reload. A check that can
# refuse must refuse before anything is on disk.
step "Checking where this will be written"
check_destination "$PREFIX" "The tg-bridge launcher"
check_destination "$UNIT_DIR" "The systemd units"
SKILL_LINK="$HOME/.claude/skills/tg-channel"
if [[ -d $HOME/.claude ]]; then
    check_destination "$(dirname "$SKILL_LINK")" "The agent skill link"
fi
printf '  every destination is yours and no other account can write its path\n'

step "Installing the launcher"
mkdir -p "$PREFIX"
LAUNCHER="$PREFIX/tg-bridge"
guard "$LAUNCHER"
# -u so a session's backgrounded `recv` writes each message to its output file immediately;
# block-buffered stdout can hold an inbound message while the read cursor has already moved.
printf '#!/usr/bin/env bash\nexec %q -u %q/bridge/cli.py "$@"\n' "$PYTHON" "$ROOT" > "$LAUNCHER"
chmod 0755 "$LAUNCHER"
printf '  %s -> %s/bridge/cli.py\n' "$LAUNCHER" "$ROOT"

case ":$PATH:" in
    *":$PREFIX:"*) ;;
    *) printf '  NOTE: %s is not on your PATH. Add it to your shell profile:\n' "$PREFIX"
       printf '        export PATH="%s:$PATH"\n' "$PREFIX" ;;
esac

step "Installing systemd user units"
mkdir -p "$UNIT_DIR"
for unit in "$ROOT"/systemd/*.service "$ROOT"/systemd/*.timer; do
    name=$(basename "$unit")
    guard "$UNIT_DIR/$name"
done
# The units ship with a `@@BRIDGE_ROOT@@` placeholder in ExecStart. Rewrite it to wherever this
# clone actually lives, and the interpreter to the python3 just verified.
#
# In python3, not sed, and quoted for systemd rather than pasted in. Both matter and neither is
# hypothetical: `sed s#…#…#` treats `&`, `\` and its own delimiter specially in the REPLACEMENT,
# so a clone under a path containing any of them produced silent corruption; and systemd splits
# ExecStart on whitespace, so a clone under `~/repo with space` generated a command whose second
# argument was `/home/you/repo` — the unit started, failed, and the reason was three files away.
rewrite_units () {
    "$PYTHON" - "$ROOT" "$PYTHON" "$UNIT_DIR" \
        "$ROOT"/systemd/*.service "$ROOT"/systemd/*.timer <<'PY'
import os, re, sys

root, python, unit_dir, *units = sys.argv[1:]

def systemd_quote(value):
    """One ExecStart argument systemd will pass through whole.

    Double quotes are systemd's own quoting; inside them a backslash escapes. Always quoting —
    rather than only when a space is present — keeps one code path, and systemd strips the
    quotes either way.

    `%` is escaped as `%%` because systemd expands `%` SPECIFIERS in ExecStart before it ever
    splits the command: a clone under a directory named `%n` would have that replaced with the
    unit name and start something else entirely, quotes or no quotes.
    """
    return ('"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"')


def refuse_unquotable(value, what):
    """A unit file is line-oriented; a newline in a path cannot be encoded into one.

    Refusing is the honest answer. The alternative was worse than an error: the newline went in
    literally, systemd read the tail as a separate directive, and the unit "installed"
    successfully (#204 review r2, F5).
    """
    if "\n" in value or "\r" in value:
        sys.exit("  %s contains a newline, which cannot appear in a systemd unit: %r\n"
                 "  Move the clone somewhere without one." % (what, value))

refuse_unquotable(root, "the clone path")
refuse_unquotable(python, "the python3 path")

PATTERN = re.compile(r"^ExecStart=\S+\s+\S*/(bridge/[a-z_]+\.py)\s*$")
for unit in units:
    name = os.path.basename(unit)
    out, rewrote, had_exec = [], False, False
    for line in open(unit).read().splitlines(keepends=True):
        if line.startswith("ExecStart="):
            had_exec = True
            found = PATTERN.match(line.rstrip("\n"))
            if found:
                out.append("ExecStart=%s %s\n" % (
                    systemd_quote(python),
                    systemd_quote(os.path.join(root, found.group(1)))))
                rewrote = True
                continue
        out.append(line)
    if had_exec and not rewrote:
        sys.exit("  %s: ExecStart is not the expected '<python> <dir>/bridge/<x>.py' form:\n    %s"
                 % (name, next(l for l in open(unit) if l.startswith("ExecStart="))))
    target = os.path.join(unit_dir, name)
    with open(target, "w") as handle:
        handle.write("".join(out))
    # 0644 explicitly, not whatever the umask gives. Under `umask 002` these come out
    # group-writable, and a rewritten unit executes on the next user-manager reload (#245).
    os.chmod(target, 0o644)
    print("  %s" % target)
PY
}
rewrite_units || die "could not write the unit files (see the message above)"

step "Installing the agent skill"
if [[ -d $HOME/.claude ]]; then
    mkdir -p "$(dirname "$SKILL_LINK")"
    if exists "$SKILL_LINK" && [[ $FORCE -eq 0 ]]; then
        printf '  %s already exists — left alone (use --force to replace)\n' "$SKILL_LINK"
    else
        ln -sfn "$ROOT/skill" "$SKILL_LINK"
        printf '  %s -> %s/skill\n' "$SKILL_LINK" "$ROOT"
    fi
else
    printf '  ~/.claude not found — skipping the Claude Code skill.\n'
    printf '  Codex users: point your agent at %s/skill/SKILL.md instead.\n' "$ROOT"
fi

step "Starting the daemon"
systemctl --user daemon-reload
systemctl --user enable --now claude-telegram-bridge.service
# All three timers, not just the watchdog. The documentation describes the morning digest and
# the model check as things that happen; installing the units but leaving them disabled makes
# the docs wrong for every fresh install, and the absence is invisible until someone wonders
# why no digest ever arrived.
for timer in watchdog digest model-watchdog; do
    systemctl --user enable --now "claude-telegram-bridge-$timer.timer"
    printf '  claude-telegram-bridge-%s.timer enabled\n' "$timer"
done
sleep 2
if ! systemctl --user is-active --quiet claude-telegram-bridge.service; then
    printf '\nThe daemon did not stay up. Its own log says why:\n\n'
    systemctl --user --no-pager status claude-telegram-bridge.service || true
    printf '\n  journalctl --user -u claude-telegram-bridge.service -n 50\n'
    exit 1
fi
printf '  claude-telegram-bridge.service active\n'

cat <<EOF

Installed. Four locations — all under \$HOME unless --prefix moved the launcher:

  $LAUNCHER
  $UNIT_DIR/claude-telegram-bridge*.{service,timer}
  $CONFIG                (you wrote this; it holds your bot token)
  $HOME/.local/share/claude-telegram-bridge/   (state: topics, inboxes, media)

Prove it end to end — from a tmux pane, so the daemon can find you:

  tg-bridge register --name "install test"     # creates a topic; prints its id
  tg-bridge send --topic <id> "hello from the bridge"

You should see the message in a new topic in your supergroup. Reply to it there, then:

  tg-bridge recv --topic <id>

If the topic never appears, the bot is almost certainly not an administrator of the group
with Manage Topics — that failure is silent by design in Telegram. docs/INSTALL.md step 2.
EOF
