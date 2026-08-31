"""The suite must never touch the LIVE bridge's state directory (#203).

Nothing else in the suite would notice if that isolation were removed: every other test
passes identically against the live state dir, which is exactly how the dependency survived
this long. These are the assertions that die when the seam is cut.
"""

import os

import pytest

from bridge import common, daemon


def _real_home():
    home = os.environ.get("TG_BRIDGE_TEST_REAL_HOME")
    if not home:
        # conftest publishes this when it swaps HOME. Missing means the isolation block is
        # gone, so the paths below point at the live bridge — fail rather than skip.
        pytest.fail("tests/conftest.py no longer isolates HOME; the suite would read and "
                    "write the live bridge state directory (#203)")
    return home


def test_home_is_not_the_real_home():
    assert os.environ["HOME"] != _real_home()


def test_state_dir_is_outside_the_live_bridge():
    live = os.path.join(_real_home(), ".local", "share", "agent-telegram-bridge")
    assert not common.STATE_DIR.startswith(live)
    assert not daemon.state_path("registry.json").startswith(live)


def test_config_path_is_outside_the_real_home():
    # The bot token lives here. A test that wrote a fixture config to the real path would
    # overwrite the running bridge's credentials.
    assert not common.CONFIG_PATH.startswith(_real_home() + os.sep)


def test_the_import_time_pending_reopen_read_is_isolated(import_time_state_dir):
    # bridge.daemon reads pending-reopens.json while it is being IMPORTED, which is earlier
    # than any fixture can run — so the per-test redirection below cannot be what saved it.
    # This asserts against the value bound at import: the module-scope HOME swap covered it.
    live = os.path.join(_real_home(), ".local", "share", "agent-telegram-bridge")
    assert not import_time_state_dir.startswith(live)
    assert import_time_state_dir.startswith(os.environ["HOME"] + os.sep)


def test_tuning_variables_from_the_host_are_cleared():
    # These become module constants at import time, so a host that exports one silently
    # changes what the suite is testing.
    for name in ("TG_BRIDGE_TZ_OFFSET", "TG_BRIDGE_AUTOCF_PCT",
                 "CLAUDE_CODE_RESUME_TOKEN_THRESHOLD"):
        assert name not in os.environ


# --- state does not survive from one test to the next -----------------------------------
#
# These two run in file order and are a pair: the first writes through the real `state_path`,
# the second proves the write is gone. Before the per-test STATE_DIR patch this leaked —
# `topics/8265`, `topics/33` and `topics/11722` were each created by one test and still there
# for every later one (#203 review, C5).

_LEAK_PROBE = ("topics", "999999", "leak-probe.json")


def test_the_state_dir_is_inside_this_tests_own_tmp_path(tmp_path):
    """The order-INDEPENDENT half, and the load-bearing one.

    `tmp_path` is unique per test by pytest's own contract, so "my state dir is under my
    tmp_path" implies no other test can be sharing it — and it holds when this test runs
    alone, under `-k`, or in any order. The write/absence pair below is a real end-to-end
    check but only while collection order holds: run in reverse, the absence half passes
    against a fixture that is not there (#203 review r2, finding 2).
    """
    assert common.STATE_DIR.startswith(str(tmp_path) + os.sep)


def test_a_test_may_write_state():
    path = daemon.state_path(*_LEAK_PROBE)
    with open(path, "w") as handle:
        handle.write("{}")
    assert os.path.exists(path)


def test_the_previous_test_left_nothing_behind(tmp_path):
    # Assert the invariant too, so this is not vacuous when run on its own.
    assert common.STATE_DIR.startswith(str(tmp_path) + os.sep)
    assert not os.path.exists(daemon.state_path(*_LEAK_PROBE))


def test_no_module_binds_a_state_path_at_import():
    """An import-time `state_path(...)` constant is immune to every later redirection.

    `bridge.digest.SNAPSHOT` was exactly that, so a test calling `save_snapshot()` wrote into
    the shared session home no matter what the per-test fixture did (#203 review r2). This
    catches the next one by construction rather than by someone noticing.
    """
    import ast
    import pathlib

    offenders = []
    for path in sorted(pathlib.Path(common.__file__).parent.glob("*.py")):
        source = path.read_text()
        for node in ast.parse(source).body:      # module level only — nested calls are fine
            if isinstance(node, (ast.Assign, ast.AnnAssign)) and "state_path" in ast.dump(node):
                offenders.append(f"{path.name}:{node.lineno}")
    assert not offenders, f"state_path bound at import: {', '.join(offenders)}"
