"""The two config values reach a GitHub search expression, so "non-blank" is not enough (#222).

`quote()` protects the transport and `subprocess.run` takes a list, so neither value can create
a shell argument or a new CLI option. What they CAN do is change what is being searched for: an
owner of `x is:pr`, or a prefix carrying a `"`, rewrites the query; a newline in either breaks
the dashboard line they are rendered into. The review found that, and it is the reason these
are validated against the shape of a name GitHub could actually have rather than against
emptiness.

Every rejection resolves the same way the rest of this config does: the feature is off.
"""

import json

import pytest

from bridge import daemon


@pytest.fixture
def config_file(tmp_path, monkeypatch):
    from bridge import common

    path = tmp_path / "config.json"
    monkeypatch.setattr(common, "CONFIG_PATH", str(path))

    def _write(**keys):
        path.write_text(json.dumps({"bot_token": "1:x", "chat_id": -1, "owner_id": 1, **keys}))
        path.chmod(0o600)   # the mode the installer requires (#245)
    return _write


def test_a_well_formed_queue_is_accepted(config_file):
    config_file(issue_queue={"owner": "octo-cat", "label_prefix": "orchestra"})
    assert daemon.issue_queue_config() == ("octo-cat", "orchestra")


@pytest.mark.parametrize("owner", [
    'x is:pr',                 # a second search term
    'x" OR "y',                # closes the quoted label and adds an alternative
    "x\ny",                    # breaks the line it is rendered into
    "x y",
    "-leading-hyphen".upper() and "-x",
    "x" * 40,                  # longer than a GitHub login
    "user/repo",
    "",
    "   ",
    None, 7, True, ["x"], {"owner": "x"},
])
def test_a_malformed_owner_turns_the_feature_off(config_file, owner):
    config_file(issue_queue={"owner": owner, "label_prefix": "orchestra"})
    assert daemon.issue_queue_config() is None
    assert daemon.issue_queue_counts() == {}


@pytest.mark.parametrize("prefix", [
    'a" is:pr label:"b',
    "a\nb",
    "a b",
    "a:b",                     # the separator itself
    "",
    "   ",
    None, 7, True, ["a"],
])
def test_a_malformed_label_prefix_turns_the_feature_off(config_file, prefix):
    config_file(issue_queue={"owner": "octo-cat", "label_prefix": prefix})
    assert daemon.issue_queue_config() is None
    assert daemon.issue_queue_counts() == {}


@pytest.mark.parametrize("repo", [
    "  /  ",                   # one slash, two "non-empty" halves, not a repository
    "owner//repo",
    "owner",
    "owner/repo/extra",
    "own er/repo",
    "owner/re\npo",
    "/repo",
    "owner/",
    "",
    None, 7, True, ["owner/repo"],
])
def test_a_malformed_carry_forward_repo_turns_the_fallback_off(config_file, repo):
    config_file(carry_forward_repo=repo)
    assert daemon.carry_forward_fallback_repo() is None


def test_a_well_formed_repo_is_accepted(config_file):
    config_file(carry_forward_repo="octo-cat/notes.git-mirror")
    assert daemon.carry_forward_fallback_repo() == "octo-cat/notes.git-mirror"


def test_the_accepted_query_contains_nothing_but_the_configured_names(config_file):
    """The whole point: what reaches GitHub is one owner and one label, and nothing else."""
    from urllib.parse import unquote

    config_file(issue_queue={"owner": "octo-cat", "label_prefix": "orchestra"})
    owner, prefix = daemon.issue_queue_config()
    query = f'user:{owner} is:issue is:open label:"{prefix}:ready"'

    assert unquote(query) == query.replace("%20", " ")
    assert query.count('"') == 2 and query.count("is:") == 2
