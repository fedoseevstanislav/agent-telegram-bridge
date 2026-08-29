"""What a spawned session is allowed to do is a configuration choice (#204 D9).

The daemon starts agent sessions from Telegram messages. Whether those sessions keep their
ordinary approval prompts used to be a constant compiled into `ENGINES`: every install, public
or not, spawned with `--dangerously-skip-permissions`. That makes a wrong `owner_id` — or a bot
added to the wrong group — the difference between a nuisance and an unattended shell.

So the default is now NOTHING, and the permissive flags are opted into per engine. These tests
pin the default, the opt-in, and the direction the code fails in when the config is malformed:
fewer permissions, never more.
"""

import json
import shlex

import pytest

from bridge import daemon


@pytest.fixture
def config(monkeypatch):
    """Set `spawn_flags` for one test. Nothing here reads the real config file."""
    def _set(value):
        monkeypatch.setattr(daemon, "load_config",
                            lambda: {"spawn_flags": value} if value is not None else {})
    return _set


# ---- the default -------------------------------------------------------------------------

def test_a_fresh_install_spawns_claude_with_no_permission_flags(config):
    config(None)
    launch = daemon.engine_launch("claude")

    assert "--dangerously-skip-permissions" not in launch
    assert launch.startswith("claude --model ")     # the model pin is NOT optional (#158)


def test_a_fresh_install_spawns_codex_with_no_permission_flags(config):
    config(None)
    assert daemon.engine_launch("codex") == "codex"


def test_a_fresh_install_revives_without_permission_flags(config):
    """A revive is a spawn. It was the second copy of the same hardcoded string."""
    config(None)
    assert daemon._resume_launch("codex", "SID") == "codex resume SID"
    assert "--dangerously" not in daemon._resume_launch("claude", "SID", model="m")


# ---- the opt-in --------------------------------------------------------------------------

def test_configured_flags_reach_a_claude_spawn(config):
    config({"claude": "--dangerously-skip-permissions"})
    launch = daemon.engine_launch("claude")

    assert launch.startswith("claude --dangerously-skip-permissions --model ")


def test_configured_flags_reach_a_codex_spawn(config):
    config({"codex": "--dangerously-bypass-approvals-and-sandbox"})
    assert daemon.engine_launch("codex") == "codex --dangerously-bypass-approvals-and-sandbox"


def test_each_engine_is_configured_independently(config):
    """Opting codex in must not opt claude in. They are different blast radii."""
    config({"codex": "--dangerously-bypass-approvals-and-sandbox"})

    assert "--dangerously" not in daemon.engine_launch("claude")
    assert "--dangerously" in daemon.engine_launch("codex")


def test_a_claude_revive_keeps_flags_after_the_model_and_effort(config):
    config({"claude": "--dangerously-skip-permissions"})
    launch = daemon._resume_launch("claude", "SID", model="claude-opus-5", effort="high")

    assert launch == ("claude --resume SID --model claude-opus-5 "
                      "--dangerously-skip-permissions --effort high")


# ---- failing in the safe direction ---------------------------------------------------------

@pytest.mark.parametrize("value", [
    "--dangerously-skip-permissions",   # a bare string, not a per-engine mapping
    ["--dangerously-skip-permissions"],
    {"claude": ["--dangerously-skip-permissions"]},
    {"claude": True},
    {"claude": None},
    {},
])
def test_a_malformed_setting_grants_nothing(config, value):
    """Guessing at a malformed permission setting can only guess in one safe direction."""
    config(value)

    assert "--dangerously" not in daemon.engine_launch("claude")
    assert daemon.engine_launch("codex") == "codex"


def test_an_unreadable_config_grants_nothing(monkeypatch):
    """The daemon has already loaded its config to get here, so this is defence in depth —
    but a config that becomes unreadable mid-run must not fall back to permissive."""
    def boom():
        raise OSError("config vanished")

    monkeypatch.setattr(daemon, "load_config", boom)
    assert daemon.spawn_flags("claude") == ""
    assert daemon.engine_launch("codex") == "codex"


def test_no_launch_string_has_a_double_space(config):
    """Composing from parts is how the flags became optional; empty parts must vanish."""
    config(None)
    for launch in (daemon.engine_launch("claude"), daemon.engine_launch("codex"),
                   daemon._resume_launch("codex", "SID"),
                   daemon._resume_launch("claude", "SID", model="m")):
        assert "  " not in launch, launch


# ---- through the REAL config file ---------------------------------------------------------
#
# Everything above patches `load_config`, which tests the SHAPE of the setting and not the file
# it comes from. The round-1 review found the gap that hides in exactly that distance: a config
# whose entire content is `true`, `7` or `null` is valid JSON, so `json.load` succeeds and
# `load_config` raises TypeError on `key not in cfg` — and that propagated out of `spawn_flags`,
# so every spawn and every revive failed. No bypass flag was emitted, so it was never a
# permission problem; it was an availability one, caused by a typo in a file nothing else
# validates at that moment.

@pytest.fixture
def config_file(tmp_path, monkeypatch):
    """Point the REAL loader at a file this test writes. No mock in the path under test."""
    from bridge import common

    path = tmp_path / "config.json"
    monkeypatch.setattr(common, "CONFIG_PATH", str(path))
    return path


@pytest.mark.parametrize("content,why", [
    ("true", "valid JSON, but a bool"),
    ("7", "valid JSON, but a number"),
    ("null", "valid JSON, but null"),
    ('"spawn_flags"', "valid JSON, but a string"),
    ("[]", "valid JSON, but a list"),
    ("{not json", "not JSON at all"),
    ("", "an empty file"),
    ('{"bot_token": "1:x"}', "an object missing the required keys"),
])
def test_a_malformed_config_file_grants_no_flags_and_does_not_raise(config_file, content, why):
    config_file.write_text(content)

    assert daemon.spawn_flags("claude") == "", why
    assert daemon.engine_launch("codex") == "codex", why
    assert daemon._resume_launch("codex", "SID") == "codex resume SID", why


def test_a_missing_config_file_grants_no_flags(config_file):
    assert not config_file.exists()
    assert daemon.spawn_flags("claude") == ""
    assert daemon.engine_launch("codex") == "codex"


def test_a_real_config_file_supplies_the_flags(config_file):
    config_file.write_text(json.dumps({
        "bot_token": "1:x", "chat_id": -1001, "owner_id": 5,
        "spawn_flags": {"claude": "--dangerously-skip-permissions",
                        "codex": "--dangerously-bypass-approvals-and-sandbox"}}))
    config_file.chmod(0o600)   # the mode the installer requires (#245)

    assert daemon.engine_launch("codex") == "codex --dangerously-bypass-approvals-and-sandbox"
    assert daemon.engine_launch("claude").startswith(
        "claude --dangerously-skip-permissions --model ")


# ---- the contract that made this change safe to ship ----------------------------------------

def test_the_operators_configured_launches_are_what_the_constants_used_to_produce(config_file):
    """Byte-identical to the pre-change strings, or the whole fleet changes behaviour silently.

    These four literals are what `ENGINES[...]["launch"]` and `_resume_launch` produced when the
    flags were constants. Moving them into config is only a refactor if the output is unchanged
    for someone who configures what the constants said — so the claim is pinned here rather than
    checked once by hand at review time (#204 review, F3).
    """
    config_file.write_text(json.dumps({
        "bot_token": "1:x", "chat_id": -1001, "owner_id": 5,
        "spawn_flags": {"claude": "--dangerously-skip-permissions",
                        "codex": "--dangerously-bypass-approvals-and-sandbox"}}))
    config_file.chmod(0o600)   # the mode the installer requires (#245)
    model = shlex.quote(daemon.SPAWN_MODEL)

    assert daemon.engine_launch("claude") == (
        f"claude --dangerously-skip-permissions --model {model}")
    assert daemon.engine_launch("codex") == (
        "codex --dangerously-bypass-approvals-and-sandbox")
    assert daemon._resume_launch("codex", "SID") == (
        "codex --dangerously-bypass-approvals-and-sandbox resume SID")
    assert daemon._resume_launch("claude", "SID", model="m", effort="high") == (
        "claude --resume SID --model m --dangerously-skip-permissions --effort high")


@pytest.mark.parametrize("configured", ["  --flag  ", "--flag\t", "\n--flag"])
def test_whitespace_around_a_configured_flag_never_reaches_the_command(config_file, configured):
    config_file.write_text(json.dumps({
        "bot_token": "1:x", "chat_id": -1001, "owner_id": 5,
        "spawn_flags": {"codex": configured}}))
    config_file.chmod(0o600)   # the mode the installer requires (#245)

    assert daemon.engine_launch("codex") == "codex --flag"
    assert daemon._resume_launch("codex", "SID") == "codex --flag resume SID"


def test_a_deeply_nested_config_grants_no_flags(config_file):
    """`json.load` raises RecursionError, which is not a ValueError (#204 review r2, F4)."""
    config_file.write_text("[" * 200000 + "]" * 200000)

    assert daemon.spawn_flags("claude") == ""
    assert daemon.engine_launch("codex") == "codex"
    assert daemon._resume_launch("codex", "SID") == "codex resume SID"


def test_a_claude_revive_with_no_effort_and_no_model_is_pinned_too(config_file):
    """The golden set omitted this shape, so a regression in it passed (#204 review r2, F6)."""
    config_file.write_text(json.dumps({
        "bot_token": "1:x", "chat_id": -1001, "owner_id": 5,
        "spawn_flags": {"claude": "--dangerously-skip-permissions"}}))
    config_file.chmod(0o600)   # the mode the installer requires (#245)
    model = shlex.quote(daemon.SPAWN_MODEL)

    assert daemon._resume_launch("claude", "SID") == (
        f"claude --resume SID --model {model} --dangerously-skip-permissions")
    assert daemon._resume_launch("claude", "SID", model="m") == (
        "claude --resume SID --model m --dangerously-skip-permissions")
