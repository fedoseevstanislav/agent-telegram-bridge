"""Every typed nudge says who it is for, because a subagent shares the pane (#145).

2026-08-11: the nudge for a topic was surfaced into the terminal of an in-process research
subagent of that session. The subagent — correctly suspicious, and it acted anyway, because
the text told it to — ran `tg-bridge recv`, CONSUMED a message from the owner, and re-posted
it into the topic. Nothing was lost that time, by luck.

The issue also proposed making `recv` refuse a caller that is not the registering session.
That was measured and has no observable: the owning session and its subagents share a pane, a
process tree and an environment, and six live registered topics all reported the same
inherited `CLAUDE_CODE_SESSION_ID`, matching none of their registry ids. A guard on it would
refuse every legitimate `recv`.

So this is addressing, not a guard, and the tests below pin only what addressing can promise:
that every line telling a session to run `recv` names the session it is for, before it says
what to do.
"""

import pytest

from bridge import daemon


@pytest.fixture
def registry(monkeypatch):
    reg = {"4242": {"pane": "%1", "session_id": "11111111-2222-3333-4444-555555555555"}}
    monkeypatch.setattr(daemon, "read_registry", lambda: reg)
    return reg


# ---- the address itself -------------------------------------------------------------------

def test_the_address_names_the_topic_and_the_session(registry):
    line = daemon.nudge_address(4242)
    assert "topic 4242" in line
    assert "11111111-2222-3333-4444-555555555555" in line
    assert "other agents in this terminal ignore this" in line


def test_the_session_id_is_carried_in_full(registry):
    """Abbreviating it would make a later transcript-side check a prefix match. The whole
    point of planting it is that something can match it exactly."""
    sid = registry["4242"]["session_id"]
    assert sid in daemon.nudge_address(4242)
    assert sid[:8] + "…" not in daemon.nudge_address(4242)


def test_no_session_id_means_no_session_clause(registry):
    """A codex session has no id until its first turn completes. "session None" addresses
    nobody, so the clause is dropped rather than filled with a placeholder."""
    registry["4242"].pop("session_id")
    line = daemon.nudge_address(4242)
    assert "None" not in line and "session " not in line.replace("the session registered", "")
    assert "topic 4242" in line


def test_an_unregistered_topic_still_addresses_the_topic(registry):
    line = daemon.nudge_address(9999)
    assert "topic 9999" in line
    assert "None" not in line


# ---- every line that tells a session to run recv carries it -------------------------------

@pytest.mark.parametrize("build", [
    pytest.param(lambda: daemon.sweep_nudge_text(4242, "unread", 0), id="sweep-unread"),
    pytest.param(lambda: daemon.dead_listener_nudge(4242), id="dead-listener"),
])
def test_each_injected_recv_instruction_is_addressed(registry, build):
    text = build()
    assert "tg-bridge recv --topic 4242" in text          # it does tell them to run recv
    assert "other agents in this terminal ignore this" in text


def test_the_message_nudge_is_addressed(registry, monkeypatch):
    typed = []
    monkeypatch.setattr(daemon, "unread_count", lambda _t: 1)
    monkeypatch.setattr(daemon, "pane_alive", lambda _p: True)
    monkeypatch.setattr(daemon, "type_line", lambda _pane, text: typed.append(text) or "sent")

    assert daemon.maybe_nudge(4242, "%1") is True

    assert len(typed) == 1
    assert "other agents in this terminal ignore this" in typed[0]
    assert "11111111-2222-3333-4444-555555555555" in typed[0]


def test_the_address_comes_before_the_instruction(registry, monkeypatch):
    """The ordering IS the mechanism. An agent that reads "run recv" before it reads who the
    line is for has already been told to act; by then the address is an after-the-fact excuse.
    So it sits immediately after the [tg-bridge] marker, in EVERY one of them — the message
    nudge included, which is the one the incident actually happened on."""
    typed = []
    monkeypatch.setattr(daemon, "unread_count", lambda _t: 1)
    monkeypatch.setattr(daemon, "pane_alive", lambda _p: True)
    monkeypatch.setattr(daemon, "type_line", lambda _pane, text: typed.append(text) or "sent")
    daemon.maybe_nudge(4242, "%1")

    for text in (daemon.sweep_nudge_text(4242, "unread", 0),
                 daemon.dead_listener_nudge(4242),
                 typed[0]):
        assert text.index("other agents in this terminal ignore this") < text.index("recv")
        assert text.startswith("[tg-bridge] For the session registered on topic 4242")


def test_a_dead_listener_recap_still_follows_the_address(registry, monkeypatch):
    """The recap re-delivers someone's actual message. It must not get in front of the
    address — that is exactly the text a wrong reader would act on."""
    monkeypatch.setattr(daemon, "recent_inbox_drop",
                        lambda _t, _n: {"from": "owner", "text": "change direction"})
    text = daemon.sweep_nudge_text(4242, "dead-listener", 0)
    assert "change direction" in text
    assert text.index("ignore this") < text.index("change direction")


# ---- what this does NOT claim -------------------------------------------------------------

def test_the_docstring_does_not_call_it_a_guard(registry):
    """It cannot stop a subagent; it can only tell one. Describing it as a guard would let the
    next reader assume a protection that is not there (the AGENTS.md claim rule)."""
    doc = daemon.nudge_address.__doc__ or ""
    assert "not a guard" in doc
    assert "addressing" in doc


# ---- C4: what actually holds a fourth site shut -------------------------------------------

def _recv_instruction_owners():
    """Every place in `bridge.daemon` that BUILDS a line instructing `recv --topic <id>`,
    keyed by the nearest enclosing function, or `<module>` for a module-level constant.

    Walks every string node in the module rather than every function body, because the review
    measured the boundary precisely: a function-level scan caught methods and nested f-strings
    and missed lambdas and module-level templates. This catches those too.

    Docstrings are excluded: they describe such a line rather than build one.

    **What it still cannot catch**, stated rather than claimed away: an instruction assembled
    by concatenation or `.format()` from fragments that individually contain no
    `recv --topic` text. There is no static check for that, and pretending otherwise would be
    the exact failure this test exists to prevent."""
    import ast
    import inspect

    src = inspect.getsource(daemon)
    tree = ast.parse(src)
    parent = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parent[child] = node

    def owner(node):
        """Enclosing function name, or `<module>:NAME` for a module-level constant.

        Module-level ones are keyed by their own NAME, not lumped under one `<module>` key:
        a single key would be a hole the size of the category — the first version of this
        test had one, and a mutation planting a new module-level nudge template walked
        straight through it while the test stayed green."""
        assigned = None
        while node in parent:
            node = parent[node]
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                return node.name          # a function owns it, however it is assigned inside
            if assigned is None and isinstance(node, ast.Assign):
                names = [t.id for t in node.targets if isinstance(t, ast.Name)]
                if names:
                    assigned = names[0]
        return f"<module>:{assigned}" if assigned else "<module>"

    # A docstring describes a line; it does not build one. Excluded by identity so that
    # documenting the mechanism does not register as an instance of it.
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            first = node.body[0] if node.body else None
            if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                    and isinstance(first.value.value, str)):
                docstrings.add(id(first.value))

    found = {}
    for node in ast.walk(tree):
        text = None
        if id(node) in docstrings:
            continue
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            text = node.value
        elif isinstance(node, ast.JoinedStr):
            text = "".join(v.value for v in node.values
                           if isinstance(v, ast.Constant) and isinstance(v.value, str))
        if text and "tg-bridge recv --topic" in text:
            found.setdefault(owner(node), []).append(text)
    return found


def test_every_place_that_builds_a_recv_instruction_is_known_and_classified():
    """C4 as an invariant rather than a hope. Nothing in Python stops someone adding a fourth
    nudge with its own literal prefix — three sites sharing a helper does not, which the
    review said plainly. So enumerate the limbs instead of testing the three examples I already knew about: anything new that builds a `recv --topic` line fails here until it is
    routed through `nudge_text` or classified below WITH a reason.

    The classification is the point. One of these is not a typed line at all and one is a gap
    we are choosing to leave open; neither is hidden by this test passing."""
    addressed = {"dead_listener_nudge", "sweep_nudge_text", "maybe_nudge"}
    classified = {
        "has_live_recv": "builds a pgrep pattern to find the listener process, never typed",
        # KNOWN GAP, not an exemption on the merits: the RESTORE_BRIEFING_* and SPAWN_BOOTSTRAP
        # templates are module-level, do instruct recv, and the restore ones ARE typed into the
        # pane by deliver_briefing — so a subagent could read one. Left unaddressed for now
        # because a revive briefing lands on a session that has just restarted, which is when a
        # subagent is least likely to be reading it. Recorded by name so the next person finds
        # it deliberately instead of having the incident a second time.
        # Each one named individually, so a NEW module-level template fails this test
        # instead of joining a category that was already waved through.
        "<module>:RESTORE_BRIEFING_CLAUDE": "typed by deliver_briefing — known gap, see #145",
        "<module>:RESTORE_BRIEFING_CODEX": "typed by deliver_briefing — known gap, see #145",
        "<module>:RESTORE_FRESH_CLAUDE": "typed by deliver_briefing — known gap, see #145",
        "<module>:RESTORE_FRESH_CODEX": "typed by deliver_briefing — known gap, see #145",
    }
    # The launch prompts: handed to the engine at spawn rather than typed into a running
    # session's pane, so no subagent of an existing session ever reads one.
    classified.update({
        f"<module>:{name}": "launch prompt, not typed into a running session's pane"
        for name in ("SPAWN_BOOTSTRAP", "SPAWN_BOOTSTRAP_NO_TASK",
                     "CODEX_BOOTSTRAP", "CODEX_BOOTSTRAP_NO_TASK")
    })
    found = _recv_instruction_owners()
    expected = addressed | set(classified)

    assert set(found) == expected, (
        f"the set of places building a recv instruction changed: "
        f"added {set(found) - expected}, gone {expected - set(found)}. "
        f"A new one either goes through nudge_text() or is classified here WITH a reason "
        f"— see #145.")


def test_the_sweep_and_message_nudges_route_through_the_constructor():
    import inspect

    for name in ("dead_listener_nudge", "sweep_nudge_text", "maybe_nudge"):
        body = inspect.getsource(getattr(daemon, name))
        assert "nudge_text(" in body, f"{name} builds a recv line without nudge_text()"


def test_nudge_text_is_the_only_place_the_marker_and_address_are_joined():
    """If a site can compose `[tg-bridge] ` with the address itself, the constructor is
    advisory. One join site, checked by counting them."""
    import inspect

    src = inspect.getsource(daemon)
    assert src.count("nudge_address(") == 2, (           # its def, and the one call in nudge_text
        "nudge_address is called outside nudge_text — the address should be joined in exactly "
        "one place")
    joins = [line for line in src.splitlines()
             if "[tg-bridge] " in line and "nudge_address" in line]
    assert len(joins) == 1, joins


def test_an_unreadable_registry_costs_the_address_and_not_the_message(monkeypatch):
    """The regression the review caught: addressing reads the registry, which the nudge path
    never did before, so a malformed registry would have turned a missing address into a
    missing MESSAGE — indistinguishable to the caller from "nothing to nudge"."""
    typed = []

    def _boom():
        raise OSError("registry is unreadable")

    monkeypatch.setattr(daemon, "read_registry", _boom)
    monkeypatch.setattr(daemon, "unread_count", lambda _t: 1)
    monkeypatch.setattr(daemon, "pane_alive", lambda _p: True)
    monkeypatch.setattr(daemon, "type_line", lambda _pane, text: typed.append(text) or "sent")

    assert daemon.maybe_nudge(4242, "%1") is True        # the message still goes
    assert len(typed) == 1
    assert "topic 4242" in typed[0]                       # still addressed by topic
    assert "recv --topic 4242" in typed[0]
    assert "session" in typed[0]                          # "the session registered on topic"
    assert "None" not in typed[0]


def test_the_address_is_built_outside_the_wake_claim_section(monkeypatch):
    """Addressing must not add file I/O inside `validate_wake_claim`'s critical section."""
    order = []
    monkeypatch.setattr(daemon, "read_registry",
                        lambda: order.append("read_registry") or {})
    monkeypatch.setattr(daemon, "unread_count", lambda _t: 1)
    monkeypatch.setattr(daemon, "pane_alive", lambda _p: True)
    monkeypatch.setattr(daemon, "type_line", lambda _pane, _text: "sent")

    import contextlib

    @contextlib.contextmanager
    def _claim(_inbox, _wake_claim):
        order.append("claim-enter")
        yield True
        order.append("claim-exit")

    monkeypatch.setattr(daemon, "validate_wake_claim", _claim)
    daemon.maybe_nudge(4242, "%1")

    assert order.index("read_registry") < order.index("claim-enter"), order


def test_a_non_string_session_id_addresses_nobody_rather_than_looking_like_it_does(registry):
    """`(session 7)` reads as an address and is not one — worse than saying nothing. The
    registry is a JSON file this process does not solely own, so the type is checked rather
    than assumed (review r2)."""
    for bad in (7, True, ["a"], {"b": 1}, None, ""):
        registry["4242"]["session_id"] = bad
        line = daemon.nudge_address(4242)
        assert "(session" not in line, f"{bad!r} rendered as an address: {line}"
        assert "topic 4242" in line


def test_the_enumeration_reaches_a_lambda_and_a_module_level_template():
    """The boundary the review measured: a function-body scan caught methods and nested
    f-strings but missed lambdas and module-level templates. Exercised against the scanner's
    own logic on a synthetic module, because planting one in the real module is what the
    mutation run does."""
    import ast

    fake = (
        'TEMPLATE = f"[tg-bridge] run `tg-bridge recv --topic {tid}`"\n'
        'send = lambda tid: f"run `tg-bridge recv --topic {tid}` now"\n'
        'def legit(tid):\n'
        '    return f"`tg-bridge recv --topic {tid}`"\n'
    )
    tree = ast.parse(fake)
    parent = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parent[child] = node

    def owner(node):
        while node in parent:
            node = parent[node]
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                return node.name
        return "<module>"

    owners = set()
    for node in ast.walk(tree):
        text = None
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            text = node.value
        elif isinstance(node, ast.JoinedStr):
            text = "".join(v.value for v in node.values
                           if isinstance(v, ast.Constant) and isinstance(v.value, str))
        if text and "tg-bridge recv --topic" in text:
            owners.add(owner(node))

    # The module-level template AND the lambda both land under <module>, which is a
    # classified key — so either would fail the enumeration if <module> were not listed.
    assert owners == {"<module>", "legit"}
