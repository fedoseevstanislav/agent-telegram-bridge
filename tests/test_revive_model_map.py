"""A sleeping session can wake on a newer model than the one its transcript recorded.

The transcript is what revive reads, and it cannot be edited after the fact, so moving a
parked fleet to a newer model — Opus 5 to Opus 5.5 with the 1M window, 2026-09-23 — needs a
map applied at wake time. These pin that the map is applied, that anything else keeps today's
behaviour, and that a broken config never breaks a revive.
"""
import shlex

import pytest

from bridge import daemon

MAP = {"revive_model_map": {"claude-opus-5": "claude-opus-5-5[1m]"}}


def _model_in(launch):
    argv = shlex.split(launch)
    return argv[argv.index("--model") + 1]


def test_a_mapped_model_wakes_on_its_successor(monkeypatch):
    monkeypatch.setattr(daemon, "load_config", lambda: MAP)

    launch = daemon._resume_launch("claude", "SID", model="claude-opus-5", effort="xhigh")

    assert _model_in(launch) == "claude-opus-5-5[1m]"
    assert "--effort xhigh" in launch              # the seat keeps its own effort


def test_the_successor_is_shell_quoted_so_the_1m_brackets_survive(monkeypatch):
    monkeypatch.setattr(daemon, "load_config", lambda: MAP)

    launch = daemon._resume_launch("claude", "SID", model="claude-opus-5")

    assert "'claude-opus-5-5[1m]'" in launch


def test_an_unmapped_model_wakes_as_it_was(monkeypatch):
    monkeypatch.setattr(daemon, "load_config", lambda: MAP)

    launch = daemon._resume_launch("claude", "SID", model="claude-fable-5-1", effort="medium")

    assert _model_in(launch) == "claude-fable-5-1"
    assert "--effort medium" in launch


@pytest.mark.parametrize("config", [
    {},
    {"revive_model_map": None},
    {"revive_model_map": "claude-opus-5-5"},
    {"revive_model_map": ["claude-opus-5", "claude-opus-5-5"]},
    {"revive_model_map": {"claude-opus-5": 5}},
    {"revive_model_map": {"claude-opus-5": ""}},
    {"revive_model_map": {"claude-opus-5": "   "}},
    {"revive_model_map": {"claude-opus-5": "claude-opus-5-5[1m]", "other": 7}},
    {"revive_model_map": {"claude-opus-5": "claude-opus-5-5[1m]", "": "x"}},
    None,
    7,
])
def test_a_config_the_map_cannot_use_leaves_the_model_alone(monkeypatch, config):
    monkeypatch.setattr(daemon, "load_config", lambda: config)

    assert _model_in(daemon._resume_launch("claude", "SID", model="claude-opus-5")) == "claude-opus-5"


@pytest.mark.parametrize("error", [OSError, ValueError, TypeError, RecursionError, SystemExit])
def test_an_unreadable_config_never_breaks_a_revive(monkeypatch, error):
    def broken():
        raise error("config")

    monkeypatch.setattr(daemon, "load_config", broken)

    assert _model_in(daemon._resume_launch("claude", "SID", model="claude-opus-5")) == "claude-opus-5"


def test_codex_revives_ignore_the_map(monkeypatch):
    monkeypatch.setattr(daemon, "load_config",
                        lambda: {"revive_model_map": {"gpt-6-astra": "gpt-6-sol"}})

    assert daemon._resume_launch("codex", "SID") == "codex resume SID"


def test_a_session_with_no_recorded_model_still_takes_the_spawn_default(monkeypatch):
    monkeypatch.setattr(daemon, "SPAWN_MODEL", "claude-opus-test[1m]")
    monkeypatch.setattr(daemon, "load_config",
                        lambda: {"revive_model_map": {"claude-opus-test[1m]": "other"}})

    assert _model_in(daemon._resume_launch("claude", "SID")) == "claude-opus-test[1m]"
