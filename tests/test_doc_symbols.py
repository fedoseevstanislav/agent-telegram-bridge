"""Every symbol the specification names must exist, and it must not cite line numbers (#204).

The spec carried 55 references of the form `daemon.py:1393-1510`. Every single one was wrong:
`handle_message` had moved from 1393 to 2210, `spawn_session` from 941 to 1590, `api` from 96
to 224. A line number in a document is a claim that goes stale on the next commit and that
nothing checks — so the reader who follows one lands in unrelated code and concludes the
document is unreliable, which by then it is.

Symbols do not drift. So the spec cites `(`handle_message`, `daemon.py`)`, and this test makes
that citation a checkable claim rather than a decoration.
"""

import ast
import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent

# The documents that SHIP. The internal working notes under docs/ are excluded for the same
# reason the release allowlist drops them: they describe how something came to be decided, not
# how the shipped system behaves, and nobody follows a citation in them.
INTERNAL = {"design-agent-to-agent-messaging.md", "mini-tz-reopen-choice.md"}
DOCS = [p for p in
        sorted((ROOT / "docs").glob("*.md")) + [ROOT / "README.md", ROOT / "AGENTS.md",
                                                ROOT / "SECURITY.md"]
        if p.is_file() and p.name not in INTERNAL and "superpowers" not in p.parts]

# "(`handle_message`, `daemon.py`)" and "(`maybe_nudge` `daemon.py`, `schedule_nudge` …)"
CITATION = re.compile(r"\(([^()]*`[a-z_][a-z0-9_]*`[^()]*`[a-z_]+\.py`[^()]*)\)")
IDENTIFIER = re.compile(r"`([a-z_][a-z0-9_]*)`")
MODULE = re.compile(r"`([a-z_]+\.py)`")


def _defined_names(module):
    """Every top-level and nested def/class/assignment name in a bridge module."""
    tree = ast.parse((ROOT / "bridge" / module).read_text())
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            names.add(node.id)
    return names


@pytest.mark.parametrize("doc", DOCS, ids=lambda p: p.name)
def test_no_document_cites_a_line_number(doc):
    """A line number cannot be kept true, and all 55 of the previous ones were wrong."""
    stale = [f"{doc.name}:{n}: {m.group(0)}"
             for n, line in enumerate(doc.read_text().splitlines(), 1)
             for m in [re.search(r"[a-z_]+\.py:\d+", line)] if m]
    assert not stale, ("cite the symbol, not the line — line numbers go stale silently: "
                       + "; ".join(stale))


@pytest.mark.parametrize("doc", DOCS, ids=lambda p: p.name)
def test_every_cited_symbol_exists_in_the_module_it_is_cited_with(doc):
    missing = []
    for citation in CITATION.findall(doc.read_text()):
        modules = MODULE.findall(citation)
        if len(modules) != 1:
            continue                     # ambiguous which module owns which name; skip
        module = modules[0]
        if not (ROOT / "bridge" / module).is_file():
            continue                     # not a bridge module (scripts/, tests/)
        defined = _defined_names(module)
        for name in IDENTIFIER.findall(citation):
            if name.endswith(".py") or name in defined:
                continue
            missing.append(f"{doc.name}: `{name}` cited with {module}, not defined there")
    assert not missing, "; ".join(missing)
