import atexit
import os
import shutil
import sys
import tempfile

import pytest

# ---------------------------------------------------------------------------
# Isolate HOME for the whole session, BEFORE any test module imports bridge.*
# ---------------------------------------------------------------------------
# Everything the bridge persists hangs off HOME: STATE_DIR is
# `~/.local/share/claude-telegram-bridge`, CONFIG_PATH is `~/.config/...`, and the codex and
# claude readers look under `~/.codex` and `~/.claude`. Left pointing at the real home, the
# suite reads and writes the LIVE bridge's state while the daemon is running (#203). An audit
# hook proved that collection alone opened live state and a full run created multiple topic
# directories there.
#
# Three reasons this belongs at module scope rather than in a fixture:
#
#  * `bridge.daemon` loads pending-reopens at IMPORT time. No fixture — not even a
#    session-scoped autouse one — runs early enough to cover that; conftest module code does.
#  * The old fixture redirected only `daemon.state_path` and only the `panes` subtree, so
#    every `bridge.common` caller (`read_registry`, `update_registry`, `append_jsonl`, …)
#    resolved to the live path through common's OWN module-level `state_path`.
#  * Redirecting HOME covers state, config, and both engines' data directories in one move,
#    instead of a growing list of individually patched paths — each of which is a hole until
#    someone remembers to add it.
#
# Verified: the full suite is green under an isolated HOME, so the dependency was ambient,
# not structural.
REAL_HOME = os.path.expanduser("~")
ISOLATED_HOME = tempfile.mkdtemp(prefix="tg-bridge-tests-home-")
os.environ["HOME"] = ISOLATED_HOME
# Published for the guard test in test_state_isolation.py. Deleting the isolation deletes
# this too, which is what makes the guard fail instead of silently passing.
os.environ["TG_BRIDGE_TEST_REAL_HOME"] = REAL_HOME
atexit.register(shutil.rmtree, ISOLATED_HOME, ignore_errors=True)

# Tuning variables are read ONCE, into module constants, when bridge.daemon is imported — so
# a host that exports any of them changes what the suite is testing, and no fixture can undo
# it afterwards. Clear them here, in the same window that owns HOME. Named explicitly rather
# than by prefix sweep: these are the ones that become constants, and an unexplained wildcard
# would quietly swallow future variables that tests may legitimately want to set.
for _name in ("TG_BRIDGE_TZ_OFFSET", "TG_BRIDGE_TOPIC", "TG_BRIDGE_SPAWN_MODEL",
              "TG_BRIDGE_CODEX_MODEL", "TG_BRIDGE_SNAPSHOT_POLL", "TG_BRIDGE_LIFECYCLE_POLL",
              "TG_BRIDGE_DASH_POLL", "TG_BRIDGE_CTX_POLL", "TG_BRIDGE_AUTOCF_PCT",
              "CLAUDE_CODE_RESUME_TOKEN_THRESHOLD", "CLAUDE_CODE_RESUME_THRESHOLD_MINUTES"):
    os.environ.pop(_name, None)

# A tripwire, not a formality. If a plugin or a rootdir conftest ever imports bridge before
# this file runs, the constants above are already bound to the real home and the isolation
# silently does nothing — the exact failure mode #203 is about. Fail loudly instead.
_TOO_LATE = [name for name in sys.modules if name == "bridge" or name.startswith("bridge.")]
if _TOO_LATE:
    raise RuntimeError(
        "bridge modules were imported before tests/conftest.py could isolate HOME "
        f"({', '.join(sorted(_TOO_LATE))}); their state paths are bound to the real home "
        "and the suite would read and write the live bridge (#203)"
    )


# Imported here, deliberately AFTER the swap above and BEFORE any per-test redirection, so
# this records the state directory the bridge actually bound while it was being imported —
# the one the import-time pending-reopens read used. Nothing else can observe that value:
# the autouse fixture below repoints STATE_DIR for every test.
from bridge import common as _common  # noqa: E402  (must follow the HOME swap)

IMPORT_TIME_STATE_DIR = _common.STATE_DIR


@pytest.fixture
def import_time_state_dir():
    return IMPORT_TIME_STATE_DIR


@pytest.fixture(autouse=True)
def _isolated_state_dir(monkeypatch, tmp_path):
    """Give every test its own state directory, not just its own session.

    Session-scoped HOME isolation stops the suite reaching the LIVE bridge, but it leaves one
    mutable state tree shared by the whole suite, so whatever a test writes is visible to every
    test after it. Review of #203 found topic directories from several otherwise unrelated test
    modules, all created by `state_path`, which mkdirs the parent of whatever it returns. No
    assertion depends on them today; the point is that none can start to.

    Patching `bridge.common.STATE_DIR` — rather than any one module's `state_path` — is what
    makes this complete. `state_path` is a single function object that `daemon`, `cli` and
    the rest import BY NAME, and it reads `STATE_DIR` from common's globals at call time, so
    one assignment redirects every caller. The previous version patched `daemon.state_path`
    and only for the `panes` subtree, which left every `bridge.common` caller
    (`read_registry`, `update_registry`, `append_jsonl`, …) resolving elsewhere.

    Tests that set `common.STATE_DIR` themselves still win: monkeypatch applies this first
    and restores both in order.

    The original hazard is worth keeping written down. Against the real home, the `panes`
    redirect this replaces was the only thing stopping pytest from contending with the daemon
    for the pane locks of LIVE panes — `_pane_lock` takes a real flock — i.e. stalling a real
    injection; and its absence made the suite fail wholesale wherever the state directory is
    read-only (#166 review r3: 65 tests failed on lock acquisition, not on their assertions).
    """
    from bridge import common

    monkeypatch.setattr(common, "STATE_DIR", str(tmp_path / "state"))


@pytest.fixture(autouse=True)
def _no_ambient_caller_pane(monkeypatch):
    """Unbind TMUX_PANE for every test unless the test sets it itself.

    `send`/`ask`/`recv` and `notify` now resolve the CALLER's own topic from TMUX_PANE. Left
    inherited, the pane of whoever runs pytest could collide with a fake pane id in a
    fixture registry, and unrelated tests would start
    refusing with exit 4 or taking the peer path depending on the machine.
    """
    monkeypatch.delenv("TMUX_PANE", raising=False)
