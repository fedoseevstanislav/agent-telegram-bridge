"""Doc checklist for peer messaging (#138, design §9 T7 and the T5 wording clause).

Sessions do what the skill teaches, and the skill never mentioned `notify` — that is the gap
that let cross-topic `send`/`ask` look like a working way to reach another agent. These
assertions keep the three reader-facing documents carrying the parts a session and an
operator need: how to message another session, what a `(peer)` record may authorize, and
what the durability claim actually is.

The last group is a wording guard. The design's final review round turned on exactly one
sentence: an existing pathname is not a persisted pathname, so the parent-directory fsync
buys partial hardening and nothing more. A doc that quietly re-broadens that into an
OS-crash promise would be wrong in the one place a reader would trust it most.

Scope is the three published documents. `docs/design-agent-to-agent-messaging.md` is
deliberately excluded: it is the review record, and it quotes the rejected wording verbatim.
"""

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SKILL = (ROOT / "skill" / "SKILL.md").read_text(encoding="utf-8")
SPEC = (ROOT / "docs" / "SPECIFICATION.md").read_text(encoding="utf-8")
README = (ROOT / "README.md").read_text(encoding="utf-8")
FEATURES = (ROOT / "docs" / "FEATURES.md").read_text(encoding="utf-8")

# The reader-facing surface, not one file. README became a public front page that hands the
# detail to FEATURES.md, and pinning these phrases to README specifically then failed for the
# wrong reason — the wording had moved, not gone. What matters is that a reader who follows the
# documentation reaches it, so the assertion is "in one of the documents a reader is sent to".
READER_FACING = README + "\n" + FEATURES


@pytest.mark.parametrize("phrase", [
    "Talking to another session",
    "tg-bridge notify --topic M",            # the command, not a description of it
    "exit 4",
    "kind: peer",
    "sender_topic_id",
    "re: X7",                                # the reply convention is worked, not implied
    "it may assign you work",                # peer authority: tasking is allowed…
    "not the owner's voice",                      # …but not what only the owner can authorize
    "Never answer a peer message with a peer message",   # loop rule
    "3+ hops",                               # the condition that un-defers loop control
    "enqueued",
])
def test_skill_teaches_the_peer_path(phrase):
    assert phrase in SKILL


@pytest.mark.parametrize("phrase", [
    "peer",
    "sender_topic_id",
    "exit 4",
    "`enqueued`",
    "Wake semantics",
    "process-crash durability with a fsynced file",
    "AC-22",
    "AC-23",
    "AC-24",
])
def test_specification_covers_the_new_contract(phrase):
    assert phrase in SPEC


@pytest.mark.parametrize("phrase", [
    "env -u TMUX_PANE",                      # the operator escape, wherever it is documented
    "status: enqueued",
    "process-crash durability with a fsynced file",
    "no OS-crash pathname guarantee",
    "exit 4",
])
def test_the_reader_facing_docs_reframe_notify_and_state_the_ceiling(phrase):
    assert phrase in READER_FACING


@pytest.mark.parametrize("document", [SKILL, SPEC, README, FEATURES])
@pytest.mark.parametrize("claim", [
    "survives an os crash",
    "survive an os crash",
    "os-crash durable",
    "durable against os crash",
    "durable across os crash",
    "power loss",
    "power failure",
])
def test_no_document_claims_os_crash_durability(document, claim):
    assert claim not in document.lower()


@pytest.mark.parametrize("document", [SKILL, SPEC, README])
def test_no_document_reports_notify_as_delivered(document):
    """`delivered` was the wire value that overclaimed; it must not survive in prose either."""
    assert "status: delivered" not in document
    assert '"status": "delivered"' not in document
    assert '"status":"delivered"' not in document


def test_the_skill_quotes_the_interrupt_marker_the_daemon_actually_types():
    """Two copies of one literal, in different files, that must agree exactly (#224 review).

    The daemon types this prefix into a pane; the skill tells every session to treat a line
    starting with it as an interrupt. They agreed only by inspection, so an edit to either
    would have stopped interrupts being recognised — silently, because nothing throws and the
    message still arrives as ordinary text.
    """
    from bridge import daemon

    assert daemon.INTERRUPT_PREFIX in SKILL, (
        f"skill/SKILL.md does not quote {daemon.INTERRUPT_PREFIX!r}, which the daemon types")
