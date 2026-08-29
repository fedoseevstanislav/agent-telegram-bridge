"""Where the OpenAI key comes from, and what happens when that source is unsafe (#146).

Voice transcription broke during a secret-hygiene migration because this key was read from one
place — `openclaw.json` `env.vars` — and the migration removed it from there. Failures stayed
silent long enough for messages to be lost.

`env.vars` is not just a second location: OpenClaw injects it into the environment of every
process it spawns, so the value reaches child environments, `ps e` and crash dumps. The
sanctioned source is OpenClaw's `secrets.json` provider, read on demand, and only after the
file has been checked — a world-readable or foreign-owned secret file is a finding, not
something to use anyway. The `env.vars` path survives only as a rollout fallback and is
deleted once this is smoke-tested live.
"""

import json
import os

import pytest

from bridge import common

SECRET = "sk-test-not-a-real-key-0000000000"
FALLBACK = "sk-test-fallback-1111111111"


def _sources(tmp_path, monkeypatch, secrets=..., env_var=..., mode=0o600):
    """Point both key sources at tmp files. Pass None to leave a source absent."""
    secrets_path = tmp_path / "secrets.json"
    if secrets is not ...:
        if secrets is not None:
            secrets_path.write_text(
                secrets if isinstance(secrets, str) else json.dumps(secrets))
            os.chmod(secrets_path, mode)
    monkeypatch.setattr(common, "OPENCLAW_SECRETS", str(secrets_path))

    config_path = tmp_path / "openclaw.json"
    body = {"env": {"vars": {}}}
    if env_var is not ... and env_var is not None:
        body["env"]["vars"]["OPENAI_API_KEY"] = env_var
    config_path.write_text(json.dumps(body))
    monkeypatch.setattr(common, "OPENCLAW_CONFIG", str(config_path))
    return secrets_path, config_path


def test_secrets_file_wins_over_env_vars(tmp_path, monkeypatch, capsys):
    _sources(tmp_path, monkeypatch, secrets={"openaiApiKey": SECRET}, env_var=FALLBACK)

    assert common.openai_api_key() == SECRET
    # No warning: this is the normal, sanctioned path.
    assert capsys.readouterr().err == ""


def test_missing_secrets_file_falls_back_and_warns(tmp_path, monkeypatch, capsys):
    _sources(tmp_path, monkeypatch, secrets=None, env_var=FALLBACK)

    assert common.openai_api_key() == FALLBACK
    err = capsys.readouterr().err
    assert "WARNING" in err and "env.vars" in err
    # The warning must explain why the fallback happened, or the rollout never ends.
    assert "does not exist" in err


@pytest.mark.parametrize("mode", [0o644, 0o640, 0o604, 0o666, 0o620, 0o602, 0o610, 0o601])
def test_any_group_or_other_bit_on_the_secrets_file_is_refused(tmp_path, monkeypatch, capsys, mode):
    """Write- and execute-only bits too: a mask narrowed to 0o044 would still be wrong."""
    _sources(tmp_path, monkeypatch, secrets={"openaiApiKey": SECRET},
             env_var=FALLBACK, mode=mode)

    # Refused, not used-with-a-note: a secret readable beyond its owner is the finding.
    assert common.openai_api_key() == FALLBACK
    assert "permission bits" in capsys.readouterr().err


def test_owner_only_modes_are_accepted(tmp_path, monkeypatch, capsys):
    """Control for the case above: 0o600 and 0o400 carry no group/other bits and must pass."""
    for mode in (0o600, 0o400):
        _sources(tmp_path, monkeypatch, secrets={"openaiApiKey": SECRET},
                 env_var=FALLBACK, mode=mode)
        assert common.openai_api_key() == SECRET
        assert capsys.readouterr().err == ""


@pytest.mark.parametrize("raw", [
    "sk-abc\ndef", "sk-abc\rdef", "sk-abc\r\ndef", "sk-abc\tdef", "sk-abc\x00def",
])
def test_a_key_with_embedded_control_characters_is_refused(tmp_path, monkeypatch, capsys, raw):
    """The leak that made this a security fix, not a cleanup.

    `http.client` rejects a header value containing CR/LF with
    `ValueError: Invalid header value b'Bearer sk-…'` — the message carries the WHOLE key —
    and the daemon's transcription except-block writes that message into `inbox.jsonl` and
    mirrors it to Telegram. Such a key must never leave this function.
    """
    _sources(tmp_path, monkeypatch, secrets={"openaiApiKey": raw}, env_var=FALLBACK)

    assert common.openai_api_key() == FALLBACK
    err = capsys.readouterr().err
    assert "control characters" in err
    assert raw not in err and raw.strip() not in err


def test_the_fallback_is_validated_exactly_like_the_primary(tmp_path, monkeypatch):
    """The rollout fallback must not be the lax path that ships a broken key into a header."""
    _sources(tmp_path, monkeypatch, secrets=None, env_var="sk-abc\ndef")

    with pytest.raises(RuntimeError) as err:
        common.openai_api_key()
    assert "control characters" in str(err.value)
    assert "sk-abc" not in str(err.value)


@pytest.mark.parametrize("body", [
    '{"env": "not-an-object"}', '{"env": {"vars": []}}', '{"env": {}}', '[]',
    '{"env": {"vars": {"OPENAI_API_KEY": 42}}}',
])
def test_malformed_fallback_structure_raises_the_controlled_error(tmp_path, monkeypatch, body):
    """A string where env.vars was expected used to raise AttributeError out of the getter,
    losing the message that names both sources."""
    (tmp_path / "openclaw.json").write_text(body)
    monkeypatch.setattr(common, "OPENCLAW_CONFIG", str(tmp_path / "openclaw.json"))
    monkeypatch.setattr(common, "OPENCLAW_SECRETS", str(tmp_path / "secrets.json"))

    with pytest.raises(RuntimeError) as err:
        common.openai_api_key()
    assert "secrets.json" in str(err.value) and "openclaw.json" in str(err.value)


def test_a_recursionerror_from_the_parser_does_not_hide_a_usable_fallback(
        tmp_path, monkeypatch, capsys):
    """RecursionError is not a ValueError: uncaught, it escaped and skipped the fallback.

    Injected rather than provoked with deeply nested input — this interpreter's C scanner
    parses 20k levels without recursing, so a depth-based test would assert nothing here
    and would silently start passing for the wrong reason on another build.
    """
    _sources(tmp_path, monkeypatch, secrets={"openaiApiKey": SECRET}, env_var=FALLBACK)
    real_load = json.load

    def load(fp, *a, **k):
        if getattr(fp, "name", "").endswith("secrets.json"):
            raise RecursionError("maximum recursion depth exceeded")
        return real_load(fp, *a, **k)

    monkeypatch.setattr(common.json, "load", load)

    assert common.openai_api_key() == FALLBACK
    assert "RecursionError" in capsys.readouterr().err


def test_redact_secrets_blanks_key_shaped_text():
    leaky = "Invalid header value b'Bearer " + SECRET + "\r\nX: y'"

    cleaned = common.redact_secrets(leaky)

    assert SECRET not in cleaned
    assert "sk-REDACTED" in cleaned
    assert "Invalid header value" in cleaned   # the diagnostic survives the redaction


def test_a_directory_in_place_of_the_secrets_file_is_refused(tmp_path, monkeypatch, capsys):
    _sources(tmp_path, monkeypatch, secrets=None, env_var=FALLBACK)
    os.mkdir(common.OPENCLAW_SECRETS)

    assert common.openai_api_key() == FALLBACK
    assert "not a regular file" in capsys.readouterr().err


def test_foreign_owned_secrets_file_is_refused(tmp_path, monkeypatch, capsys):
    _sources(tmp_path, monkeypatch, secrets={"openaiApiKey": SECRET}, env_var=FALLBACK)
    real_stat = os.stat

    class ForeignStat:
        def __init__(self, st):
            self.st_mode, self.st_uid = st.st_mode, os.getuid() + 1

    monkeypatch.setattr(
        common.os, "stat",
        lambda p, *a, **k: ForeignStat(real_stat(p)) if p == common.OPENCLAW_SECRETS
        else real_stat(p, *a, **k))

    # Under a shared UID this is not a hard boundary, but a file that changed owner is a
    # signal something replaced it — do not read a secret out of it.
    assert common.openai_api_key() == FALLBACK
    assert "owned by uid" in capsys.readouterr().err


@pytest.mark.parametrize("content,reason", [
    ("{not json at all", "malformed"),
    ("[]", "JSON object"),
    ('{"openaiApiKey": ""}', "no usable"),
    ('{"openaiApiKey": "   "}', "no usable"),
    ('{"openaiApiKey": 12345}', "no usable"),
    ("{}", "no usable"),
])
def test_unusable_secrets_content_falls_back(tmp_path, monkeypatch, capsys, content, reason):
    _sources(tmp_path, monkeypatch, secrets=content, env_var=FALLBACK)

    assert common.openai_api_key() == FALLBACK
    assert reason in capsys.readouterr().err


def test_surrounding_whitespace_is_stripped(tmp_path, monkeypatch):
    _sources(tmp_path, monkeypatch, secrets={"openaiApiKey": f"  {SECRET}\n"}, env_var=None)

    # A trailing newline from an editor would otherwise travel into the Authorization header.
    assert common.openai_api_key() == SECRET


def test_no_source_at_all_raises_naming_both(tmp_path, monkeypatch):
    _sources(tmp_path, monkeypatch, secrets=None, env_var=None)

    with pytest.raises(RuntimeError) as err:
        common.openai_api_key()

    # The message is what a future operator debugs from: it must name both places.
    assert "secrets.json" in str(err.value)
    assert "openclaw.json" in str(err.value)


@pytest.mark.parametrize("secrets,env_var,raises", [
    ({"openaiApiKey": SECRET}, FALLBACK, False),                 # unsafe mode -> fallback
    ('{"openaiApiKey": "' + SECRET + '"} trailing', FALLBACK, False),   # malformed -> fallback
    ('{"openaiApiKey": "' + SECRET + '"} trailing', None, True),        # nothing usable
    ({"openaiApiKey": SECRET + "\nX"}, FALLBACK, False),         # control chars -> fallback
])
def test_neither_key_ever_reaches_stdout_stderr_or_an_exception(
        tmp_path, monkeypatch, capsys, secrets, env_var, raises):
    """Both sentinels, both streams, every path.

    The earlier version of this test only looked for the PRIMARY key on paths that return
    the FALLBACK, so a leak of the value actually in play would have passed it.
    """
    _sources(tmp_path, monkeypatch, secrets=secrets, env_var=env_var, mode=0o644)

    if raises:
        with pytest.raises(RuntimeError) as err:
            common.openai_api_key()
        for sentinel in (SECRET, FALLBACK):
            assert sentinel not in str(err.value)
            assert sentinel not in repr(err.value)
    else:
        common.openai_api_key()

    out, errtext = capsys.readouterr()
    for sentinel in (SECRET, FALLBACK):
        assert sentinel not in out
        assert sentinel not in errtext


# ---- the bridge's own config (#204) --------------------------------------------------------
#
# Until this existed, the key came ONLY from a sibling private tool's files. On any other
# machine `openai_api_key()` raised, `handle_message` caught it, and the operator's inbox
# filled with `[voice message — transcription failed: no OpenAI key: …]`. A released project
# whose headline feature cannot work on a fresh install is not released.

BRIDGE_KEY = "sk-test-bridge-config-2222222222"


def _bridge_config(tmp_path, monkeypatch, body=..., mode=0o600):
    path = tmp_path / "config.json"
    if body is not ...:
        path.write_text(body if isinstance(body, str) else json.dumps(body))
        os.chmod(path, mode)
    monkeypatch.setattr(common, "CONFIG_PATH", str(path))
    return path


def test_the_bridges_own_config_wins_over_both_openclaw_sources(tmp_path, monkeypatch, capsys):
    _sources(tmp_path, monkeypatch, secrets={"openaiApiKey": SECRET}, env_var=FALLBACK)
    _bridge_config(tmp_path, monkeypatch, {"openai_api_key": BRIDGE_KEY})

    assert common.openai_api_key() == BRIDGE_KEY
    assert capsys.readouterr().err == ""    # the normal path warns about nothing


def test_an_absent_bridge_key_falls_through_silently(tmp_path, monkeypatch, capsys):
    """The operator's own setup has no key here, and must not be nagged about it."""
    _sources(tmp_path, monkeypatch, secrets={"openaiApiKey": SECRET}, env_var=FALLBACK)
    _bridge_config(tmp_path, monkeypatch, {"bot_token": "1:x", "chat_id": -1, "owner_id": 1})

    assert common.openai_api_key() == SECRET
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize("body,mode", [
    ({"openai_api_key": BRIDGE_KEY}, 0o644),        # world-readable: a finding, not a source
    ({"openai_api_key": BRIDGE_KEY + "\nX"}, 0o600),  # control chars -> unusable header value
    ({"openai_api_key": ""}, 0o600),
    ({"openai_api_key": 7}, 0o600),
    ("[]", 0o600),                                   # valid JSON, wrong shape
    ("{not json", 0o600),
    (..., 0o600),                                    # no file at all
])
def test_an_unusable_bridge_key_falls_through_instead_of_raising(
        tmp_path, monkeypatch, body, mode):
    _sources(tmp_path, monkeypatch, secrets={"openaiApiKey": SECRET}, env_var=FALLBACK)
    _bridge_config(tmp_path, monkeypatch, body, mode=mode)

    assert common.openai_api_key() == SECRET


def test_with_no_key_anywhere_the_error_names_all_three_sources(tmp_path, monkeypatch):
    _sources(tmp_path, monkeypatch, secrets=None, env_var=None)
    _bridge_config(tmp_path, monkeypatch)

    with pytest.raises(RuntimeError) as err:
        common.openai_api_key()
    message = str(err.value)
    assert "config.json" in message and "secrets.json" in message and "env.vars" in message


def test_the_bridge_key_never_reaches_an_exception_or_a_stream(tmp_path, monkeypatch, capsys):
    """Same rule as the other two sources: a rejection never carries the value."""
    _sources(tmp_path, monkeypatch, secrets=None, env_var=None)
    _bridge_config(tmp_path, monkeypatch, {"openai_api_key": BRIDGE_KEY + "\nX"})

    with pytest.raises(RuntimeError) as err:
        common.openai_api_key()
    out, errtext = capsys.readouterr()
    for text in (str(err.value), repr(err.value), out, errtext):
        assert BRIDGE_KEY not in text
