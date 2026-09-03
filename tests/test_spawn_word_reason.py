"""Every seat this daemon launches carries the owner's words as the shim's spawn reason.

`~/.local/bin/claude` and `~/.local/bin/codex` are shims: they journal one line per launch
to ~/.claude/model-decisions.jsonl and read the stated reason from `CLAUDE_SPAWN_REASON` /
`CODEX_SPAWN_REASON`. A seat started from Telegram had no reason attached, so the one launch
route that always HAS the owner's words in hand — `/claude <name>: <task>` — was the route
producing reasonless rows in the journal the weekly model review reads.

The words come from the command, minus what the daemon consumed (`@path`), and are folded,
capped and shell-quoted in ONE place (`spawn_reason_env`), because an unquoted value here is
a command line built out of a Telegram message.
"""

import shlex

import pytest

from bridge import daemon


CLAUDE_VAR = "CLAUDE_SPAWN_REASON"
CODEX_VAR = "CODEX_SPAWN_REASON"


def reason_value(cmd, var=CLAUDE_VAR):
    """The reason as the SHELL will see it: split the command the way sh would, find the
    assignment token, return its value. Asserting on the raw text would pass for a value
    that quotes wrongly and falls apart when sh parses it."""
    for token in shlex.split(cmd):
        key, sep, value = token.partition("=")
        if sep and key == var:
            return value
    return None


@pytest.fixture
def spawned(monkeypatch):
    """Drive the real `/claude|/codex|/spawn` command end to end and return the shell
    command the daemon hands to tmux."""
    calls = []

    def _tmux(argv, **kw):
        calls.append(argv)
        if "has-session" in argv:                 # the name is free
            return type("R", (), {"returncode": 1, "stdout": "", "stderr": ""})()
        return type("R", (), {"returncode": 0, "stdout": "%77\n", "stderr": ""})()

    monkeypatch.setattr(daemon, "_tmux", _tmux)
    monkeypatch.setattr(daemon, "reply", lambda *a, **k: None)
    monkeypatch.setattr(daemon, "log", lambda *a, **k: None)
    monkeypatch.setattr(daemon, "spawn_flags", lambda engine: "")
    monkeypatch.setattr(daemon, "ensure_codex_trust", lambda cwd: None)
    monkeypatch.setattr(daemon, "verify_spawn", lambda *a, **k: None)
    monkeypatch.setattr(daemon, "SPAWN_VERIFY_DELAY", 0)

    def run(text):
        calls.clear()
        daemon.spawn_session({}, 42, text)
        assert calls and "new-session" in calls[-1], "no pane was launched"
        return calls[-1][-1]                      # the shell command is tmux's last argument

    return run


# ---- C1/C2: the owner's words reach the right engine's variable ----------------

def test_a_claude_spawn_carries_the_spawn_words(spawned):
    assert f"{CLAUDE_VAR}='foo bar'" in spawned("/claude foo bar")


def test_a_codex_spawn_carries_them_in_the_codex_variable(spawned):
    cmd = spawned("/codex port-script: rewrite fetch.sh in python")
    assert reason_value(cmd, CODEX_VAR) == "port-script: rewrite fetch.sh in python"


def test_only_the_launched_engine_s_variable_is_set(spawned):
    # Both would be simpler and is wrong: the env is inherited, so a codex reason exported
    # into a claude seat would be read back by the FIRST `codex exec` that seat runs and
    # journaled as that spawn's stated reason.
    assert reason_value(spawned("/claude alpha"), CODEX_VAR) is None
    assert reason_value(spawned("/codex alpha"), CLAUDE_VAR) is None


def test_the_task_is_carried_with_the_name(spawned):
    cmd = spawned("/claude fix-bot @~: investigate yesterday's token spike")
    assert reason_value(cmd) == "fix-bot: investigate yesterday's token spike"


def test_the_consumed_path_flag_is_not_part_of_the_reason(spawned):
    # `@~` is the daemon's own argument, not a word the owner meant as a reason.
    assert "@~" not in reason_value(spawned("/claude fix-bot @~: do the thing"))


def test_the_legacy_spawn_alias_carries_a_reason_too(spawned):
    assert reason_value(spawned("/spawn helper: assist")) == "helper: assist"


# ---- quoting and cap: the value survives a shell built from a Telegram message ----

@pytest.mark.parametrize("words, expected", [
    ("it's mike's seat", "it's mike's seat"),
    ("cost $HOME $(id) `id`", "cost $HOME $(id) `id`"),
    ('say "hi" & then; stop | now', 'say "hi" & then; stop | now'),
    ("path \\ back", "path \\ back"),
])
def test_hostile_spawn_words_survive_shell_parsing_intact(spawned, words, expected):
    cmd = spawned(f"/claude seat: {words}")
    assert reason_value(cmd) == f"seat: {expected}"


def test_a_multiline_task_becomes_one_line(spawned):
    # Folded, not merely quoted: the journal line is one field, and a reason carrying a
    # newline reads there as a second record.
    cmd = spawned("/claude seat: first line\nsecond line")
    value = reason_value(cmd)
    assert value == "seat: first line second line"
    assert "\n" not in value and "\t" not in value


def test_the_reason_is_capped(spawned):
    cmd = spawned("/claude seat: " + "x" * 500)
    assert len(reason_value(cmd)) == daemon.SPAWN_REASON_MAX


def test_the_cap_is_what_it_claims(spawned):
    assert daemon.SPAWN_REASON_MAX == 200


# ---- C4: never empty --------------------------------------------------------

def test_words_that_sanitise_away_still_leave_a_reason(monkeypatch):
    # Words of nothing but invisible characters strip to "" — and the shim treats an empty
    # value as no reason at all, so that launch would be journaled blind again.
    calls = []
    monkeypatch.setattr(daemon, "_tmux", lambda argv, **kw: (
        calls.append(argv) or type("R", (), {"returncode": 0, "stdout": "%9\n", "stderr": ""})()))
    daemon.launch_pane("my-seat", "/workspace/example", "claude", "claude", "\u200b\u00a0")
    assert reason_value(calls[-1][-1]) == "spawn: my-seat"


def test_the_helper_picks_the_engine_s_own_variable():
    assert daemon.spawn_reason_env("codex", "why", "fb").startswith(CODEX_VAR + "=")
    assert daemon.spawn_reason_env("claude", "why", "fb").startswith(CLAUDE_VAR + "=")


def test_the_helper_never_returns_an_empty_value():
    assert spawn_value(daemon.spawn_reason_env("claude", "", "")) == "spawn"
    assert spawn_value(daemon.spawn_reason_env("claude", None, None)) == "spawn"
    assert spawn_value(daemon.spawn_reason_env("claude", "  ", "given")) == "given"


def spawn_value(assignment):
    return shlex.split(assignment)[0].partition("=")[2]


# ---- C3: revives carry one too ----------------------------------------------

@pytest.fixture
def revive_launch(monkeypatch):
    """Capture the shell command a revive builds, driving the real launch_pane."""
    calls = []

    def _tmux(argv, **kw):
        calls.append(argv)
        # The launch itself "fails", so revive_one returns right after building the command
        # and nothing downstream drives a pane that does not exist. The command under test
        # is complete by then — it is tmux's argument.
        return type("R", (), {"returncode": 1, "stdout": "", "stderr": "stop"})()

    monkeypatch.setattr(daemon, "_tmux", _tmux)
    monkeypatch.setattr(daemon, "log", lambda *a, **k: None)
    monkeypatch.setattr(daemon, "spawn_flags", lambda engine: "")
    monkeypatch.setattr(daemon, "ensure_codex_trust", lambda cwd: None)
    monkeypatch.setattr(daemon, "_name_taken_by_other", lambda name, pane: False)
    monkeypatch.setattr(daemon, "last_model_and_effort_for_session",
                        lambda sid, cwd: ("claude-opus-5", "high"))
    monkeypatch.setattr(daemon, "last_model_for_session", lambda sid: None)

    def run(entry):
        calls.clear()
        status, _ = daemon.revive_one({}, "77", entry, brief=False)
        assert status == "failed"                 # the stub tmux refused; the command is built
        assert calls and "new-session" in calls[-1], "no pane was launched"
        return calls[-1][-1]

    return run


def test_a_resumed_claude_session_carries_a_revive_reason(revive_launch):
    cmd = revive_launch({"name": "alfa-capital", "engine": "claude",
                         "session_id": "abc-123", "cwd": "~"})
    assert reason_value(cmd) == "revive: alfa-capital"


def test_a_fresh_revive_carries_one_too(revive_launch):
    cmd = revive_launch({"name": "alfa-capital", "engine": "claude", "cwd": "~"})
    assert reason_value(cmd) == "revive: alfa-capital"


def test_a_nameless_topic_revives_under_its_topic_id(revive_launch):
    cmd = revive_launch({"engine": "claude", "session_id": "abc-123", "cwd": "~"})
    assert reason_value(cmd) == "revive: 77"
