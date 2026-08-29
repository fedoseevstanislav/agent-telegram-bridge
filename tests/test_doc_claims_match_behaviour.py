"""Claims about behaviour that documents keep getting wrong, checked against the behaviour.

Two review rounds in a row found the same class of defect: prose that described what the code
used to do. Round 1 found five, round 2 found two more — including the blocking one, where the
two documents that SHIP still told a reader that every spawned session runs with permission
bypass. Nothing failed when that became untrue, so nothing could have caught it but a reader.

`test_doc_symbols.py` pins the citations. This pins the small number of CLAIMS whose staleness
would actually mislead someone: what a fresh install does, and where a secret comes from. It is
deliberately short — a guard that tries to check every sentence gets deleted the first time it
blocks a legitimate edit.
"""

import json
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
DOCS = {p.name: p.read_text() for p in
        [ROOT / "README.md", ROOT / "SECURITY.md", ROOT / "docs" / "INSTALL.md",
         ROOT / "docs" / "CONFIGURATION.md", ROOT / "docs" / "FEATURES.md",
         ROOT / "docs" / "OPERATIONS.md", ROOT / "docs" / "SPECIFICATION.md"]
        if p.is_file()}
SHIPPED = "\n".join(DOCS.values())


def test_no_document_says_a_spawn_is_always_permission_bypassed(tmp_path, monkeypatch):
    """The claim and the behaviour, checked together — neither alone is the point.

    First the behaviour: with no `spawn_flags`, no launch path emits a bypass flag. Then the
    documents: none of them may state the flags as a property of the system rather than as a
    configured choice.
    """
    from bridge import common, daemon

    monkeypatch.setattr(common, "CONFIG_PATH", str(tmp_path / "absent.json"))
    for launch in (daemon.engine_launch("claude"), daemon.engine_launch("codex"),
                   daemon._resume_launch("claude", "SID"), daemon._resume_launch("codex", "SID")):
        assert "--dangerously" not in launch, launch

    # A document may name the flags — it must, to explain the choice — but only alongside the
    # word that makes it conditional. PROSE only: a fenced block showing the config value is
    # the example a reader copies, and demanding a caveat inside JSON is how a guard like this
    # earns its own deletion.
    for name, text in DOCS.items():
        fenced = False
        for number, line in enumerate(text.splitlines(), 1):
            if line.lstrip().startswith("```"):
                fenced = not fenced
                continue
            if fenced or "--dangerously" not in line:
                continue
            conditional = any(word in line.lower() for word in
                              ("spawn_flags", "config", "opt", "if you", "unattended", "choose",
                               "setting", "removes those prompts"))
            assert conditional, f"{name}:{number} states a bypass flag as unconditional: {line}"


def test_the_documented_key_sources_are_the_ones_the_code_reads(tmp_path, monkeypatch):
    """The config is FIRST. Both documents said OpenClaw-only for a while after it was not."""
    from bridge import common

    config = tmp_path / "config.json"
    config.write_text(json.dumps({"bot_token": "1:x", "chat_id": -1, "owner_id": 1,
                                  "openai_api_key": "sk-doc-test-0000000000"}))
    config.chmod(0o600)
    monkeypatch.setattr(common, "CONFIG_PATH", str(config))
    monkeypatch.setattr(common, "OPENCLAW_SECRETS", str(tmp_path / "nope.json"))
    monkeypatch.setattr(common, "OPENCLAW_CONFIG", str(tmp_path / "nope2.json"))

    assert common.openai_api_key() == "sk-doc-test-0000000000"
    assert "openai_api_key" in SHIPPED, "no shipped document mentions the config key at all"


@pytest.mark.parametrize("claim", [
    "the OpenAI key is read from ~/.openclaw",
    "the OpenAI key only in OpenClaw",
    "key is read at runtime from OpenClaw",
])
def test_no_document_still_says_the_key_comes_only_from_the_sibling_tool(claim):
    for name, text in DOCS.items():
        assert claim.lower() not in text.lower(), f"{name} still says: {claim}"
