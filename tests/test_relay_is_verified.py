"""The two injections #133 left outside its verification (#154).

#133 made autonomous injections confirm their text reached an input box before pressing Enter.
Two typing paths stayed raw `send-keys` + unconditional Enter because they are user-initiated:
the generic slash-command relay, and `/model`'s first Enter.

Being user-initiated makes them lower severity, not harmless. `pane_is_idle()` reads "no
spinner, no compaction bar" as idle, which any approval, update or resume modal satisfies. So
with a modal already up the command was swallowed, that blind Enter accepted the modal's
DEFAULT — for the rate-limit picker, a cheaper model — and the daemon replied "→ switched
model to X". A report of something that did not happen is worse than a failure: the owner
stops watching for the result.

Both now deliver through `type_line`, which is not a new mechanism here — `/compact` already
rides it from the carry-forward inject.
"""

import types

import pytest

from bridge import daemon

from test_modal_safe_nudge import (CLAUDE_IDLE, CODEX_APPROVAL_MODAL,
                                   CODEX_RATE_LIMIT_MODAL, FakePane)


@pytest.fixture
def topic(monkeypatch):
    """Topic 4242 bound to a live claude pane %1, with replies and logs captured."""
    state = {"replies": [], "logs": []}
    # type_line's swallow streak is module state keyed by pane, and a capped pane refuses to
    # type at all — so a test that models a modal would silently disarm every test after it.
    daemon._swallowed_streak.clear()
    daemon._pending_model.clear()
    monkeypatch.setattr(daemon, "read_registry", lambda: {"4242": {"pane": "%1"}})
    monkeypatch.setattr(daemon, "pane_alive", lambda _p: True)
    monkeypatch.setattr(daemon, "engine_of_pane", lambda _p: "claude")
    monkeypatch.setattr(daemon, "pane_is_idle", lambda _p: True)
    monkeypatch.setattr(daemon, "reply",
                        lambda _cfg, _tid, text: state["replies"].append(text) or True)
    monkeypatch.setattr(daemon, "log", lambda m: state["logs"].append(m))
    monkeypatch.setattr(daemon.time, "sleep", lambda *_a, **_k: None)
    return state


def _pane(monkeypatch, screen, accepts=True):
    pane = FakePane(screen, accepts=accepts)
    monkeypatch.setattr(daemon, "_tmux", pane)
    return pane


# ---- C1 / C2: the slash relay ------------------------------------------------------------

def test_the_relay_delivers_through_the_verified_path(monkeypatch, topic):
    pane = _pane(monkeypatch, CLAUDE_IDLE)

    daemon.handle_command({}, 4242, "/status")

    assert pane.submitted == ["/status"]          # the Enter submitted exactly that command
    assert topic["replies"] == ["→ typed /status into the session terminal"]


@pytest.mark.parametrize("screen", [CODEX_RATE_LIMIT_MODAL, CODEX_APPROVAL_MODAL])
def test_the_relay_presses_no_enter_into_a_modal(monkeypatch, topic, screen):
    """The #133 bug reached through the relay: a modal already up ate the command, and the
    Enter that followed it selected the modal's default."""
    pane = _pane(monkeypatch, screen, accepts=False)

    daemon.handle_command({}, 4242, "/status")

    assert pane.enters == 0
    assert pane.submitted == []


@pytest.mark.parametrize("screen", [CODEX_RATE_LIMIT_MODAL, CODEX_APPROVAL_MODAL])
def test_the_relay_never_claims_a_command_it_did_not_deliver(monkeypatch, topic, screen):
    _pane(monkeypatch, screen, accepts=False)

    daemon.handle_command({}, 4242, "/status")

    assert len(topic["replies"]) == 1
    assert "typed /status" not in topic["replies"][0]
    assert "Couldn't deliver /status" in topic["replies"][0]


def test_an_unreadable_pane_is_reported_not_claimed(monkeypatch, topic):
    """`failed` covers a pane lock held elsewhere, an unreadable pane and an empty line
    alike, so the wording names the possibilities instead of picking one."""
    monkeypatch.setattr(daemon, "_tmux", FakePane(CLAUDE_IDLE, capture_fails=True))

    daemon.handle_command({}, 4242, "/status")

    assert "didn't take it" in topic["replies"][0]
    assert "typed /status" not in topic["replies"][0]


def test_the_relay_status_reaches_the_log(monkeypatch, topic):
    _pane(monkeypatch, CODEX_RATE_LIMIT_MODAL, accepts=False)

    daemon.handle_command({}, 4242, "/status")

    assert any("/status" in m and "swallowed" in m for m in topic["logs"]), topic["logs"]


# ---- C6: a swallow reaches the owner, not just the log -----------------------------------

def test_a_swallowed_relay_command_reaches_the_owner(monkeypatch, topic):
    _pane(monkeypatch, CODEX_RATE_LIMIT_MODAL, accepts=False)

    daemon.handle_command({}, 4242, "/status")

    assert len(topic["replies"]) == 1
    assert "withheld Enter" in topic["replies"][0]


# ---- C3 / C4: /model ---------------------------------------------------------------------

def test_the_model_command_goes_through_the_verified_path(monkeypatch, topic):
    pane = _pane(monkeypatch, CLAUDE_IDLE)
    daemon._pending_model["4242"] = "opus"

    daemon._try_send_model({}, 4242, "opus", "%1", "claude", 1)

    assert pane.submitted == ["/model opus"]
    assert daemon._pending_model.get("4242") is None


@pytest.mark.parametrize("screen", [CODEX_RATE_LIMIT_MODAL, CODEX_APPROVAL_MODAL])
def test_a_swallowed_model_command_is_not_reported_as_a_switch(monkeypatch, topic, screen):
    """The exact failure #154 describes: pane_is_idle() is satisfied by a modal, the alias was
    swallowed, the blind Enter took the modal's default — which for the rate-limit picker is a
    CHEAPER MODEL — and the owner was told the switch had happened."""
    pane = _pane(monkeypatch, screen, accepts=False)
    daemon._pending_model["4242"] = "opus"

    daemon._try_send_model({}, 4242, "opus", "%1", "claude", 1)

    assert pane.enters == 0                       # no Enter reached the picker
    assert len(topic["replies"]) == 1
    assert "switched" not in topic["replies"][0]
    assert "Didn't switch the model" in topic["replies"][0]
    assert "opus" in topic["replies"][0]


def test_the_model_reply_claims_delivery_not_a_switch(monkeypatch, topic):
    """What is observed is that the command reached the composer and was submitted. Whether
    Claude Code honoured it is not observed here, so the reply does not say so (#233)."""
    _pane(monkeypatch, CLAUDE_IDLE)
    daemon._pending_model["4242"] = "opus"

    daemon._try_send_model({}, 4242, "opus", "%1", "claude", 1)

    assert topic["replies"] == ["→ sent /model opus to the session."]


# ---- C5: what must not change ------------------------------------------------------------

def test_the_switch_model_dialog_is_still_confirmed(monkeypatch, topic):
    """Without this the session hangs on the modal. It stays a raw Enter on purpose: it
    answers a dialog positively identified on the pane, which is not a blind submit."""
    pane = FakePane(CLAUDE_IDLE)
    seen = {"extra": 0}
    real = pane.__call__

    def tmux(argv, *a, **k):
        if argv[:2] == ["tmux", "capture-pane"] and pane.submitted:
            # After the command lands, the pane shows the confirmation dialog.
            assert "-S" not in argv, "the confirm capture must not read scrollback"
            return types.SimpleNamespace(returncode=0, stdout="Switch model?\n❯ 1. Yes, switch",
                                         stderr="")
        if argv[:2] == ["tmux", "send-keys"] and argv[-1] == "Enter" and pane.submitted:
            seen["extra"] += 1
        return real(argv, *a, **k)

    monkeypatch.setattr(daemon, "_tmux", tmux)
    daemon._pending_model["4242"] = "opus"

    daemon._try_send_model({}, 4242, "opus", "%1", "claude", 1)

    # The confirming Enter is forwarded to the fake too, which records it as an empty submit —
    # the real pane has a dialog there, not an input box.
    assert pane.submitted[0] == "/model opus"
    assert seen["extra"] == 1                     # exactly one confirming Enter


def test_no_confirming_enter_without_the_dialog(monkeypatch, topic):
    """On a Claude Code without that dialog a stray Enter would submit an empty line."""
    pane = _pane(monkeypatch, CLAUDE_IDLE)
    daemon._pending_model["4242"] = "opus"

    daemon._try_send_model({}, 4242, "opus", "%1", "claude", 1)

    assert pane.enters == 1                       # type_line's, and no other


def test_a_superseded_model_request_still_types_nothing(monkeypatch, topic):
    pane = _pane(monkeypatch, CLAUDE_IDLE)
    daemon._pending_model["4242"] = "sonnet"      # a newer request won

    daemon._try_send_model({}, 4242, "opus", "%1", "claude", 1)

    assert pane.typed == [] and topic["replies"] == []


def test_a_mid_turn_pane_is_still_deferred_not_typed_into(monkeypatch, topic):
    pane = _pane(monkeypatch, CLAUDE_IDLE)
    monkeypatch.setattr(daemon, "pane_is_idle", lambda _p: False)
    monkeypatch.setattr(daemon.threading, "Timer",
                        lambda *a, **k: types.SimpleNamespace(start=lambda: None))
    daemon._pending_model["4242"] = "opus"

    daemon._try_send_model({}, 4242, "opus", "%1", "claude", 1)

    assert pane.typed == []
    assert "mid-turn" in topic["replies"][0]


# ---- what the relay must not swallow -----------------------------------------------------

def test_a_dead_pane_is_still_refused_before_anything_is_typed(monkeypatch, topic):
    pane = _pane(monkeypatch, CLAUDE_IDLE)
    monkeypatch.setattr(daemon, "pane_alive", lambda _p: False)

    daemon.handle_command({}, 4242, "/status")

    assert pane.typed == []
    assert "no live terminal" in topic["replies"][0]


def test_no_confirming_enter_for_a_dialog_left_in_the_scrollback(monkeypatch, topic):
    """Found in review. The capture was `-S -12`, which starts twelve lines up in the
    SCROLLBACK, so a dialog answered earlier in the session still matched and this Enter fired
    with no dialog up — into whatever held the input box. The capture is now the visible pane,
    and both the title and an option line are required."""
    pane = FakePane(CLAUDE_IDLE)
    seen = {"extra": 0}
    real = pane.__call__
    history = ("Switch model?\n❯ 1. Yes, switch\n" * 2) + "...answered ages ago...\n"

    def tmux(argv, *a, **k):
        if argv[:2] == ["tmux", "capture-pane"] and pane.submitted:
            if "-S" in argv:            # the old call: scrollback, where the stale dialog is
                return types.SimpleNamespace(returncode=0, stdout=history, stderr="")
            return real(argv, *a, **k)  # the visible pane: no dialog on it
        if argv[:2] == ["tmux", "send-keys"] and argv[-1] == "Enter" and pane.submitted:
            seen["extra"] += 1
        return real(argv, *a, **k)

    monkeypatch.setattr(daemon, "_tmux", tmux)
    daemon._pending_model["4242"] = "opus"

    daemon._try_send_model({}, 4242, "opus", "%1", "claude", 1)

    assert pane.submitted[0] == "/model opus"
    assert seen["extra"] == 0


def test_the_title_alone_does_not_confirm(monkeypatch, topic):
    """One phrase can appear in ordinary output — a session discussing /model, for instance.
    Requiring the option line too costs nothing and drops that case."""
    pane = FakePane(CLAUDE_IDLE)
    seen = {"extra": 0}
    real = pane.__call__

    def tmux(argv, *a, **k):
        if argv[:2] == ["tmux", "capture-pane"] and pane.submitted:
            return types.SimpleNamespace(
                returncode=0, stdout="here is how you Switch model in this TUI", stderr="")
        if argv[:2] == ["tmux", "send-keys"] and argv[-1] == "Enter" and pane.submitted:
            seen["extra"] += 1
        return real(argv, *a, **k)

    monkeypatch.setattr(daemon, "_tmux", tmux)
    daemon._pending_model["4242"] = "opus"

    daemon._try_send_model({}, 4242, "opus", "%1", "claude", 1)

    assert seen["extra"] == 0
