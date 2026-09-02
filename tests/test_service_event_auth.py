"""Forum service messages must pass the owner pin before they change anything (#206).

The lifecycle they drive is not cosmetic: `forum_topic_reopened` reaches `maybe_auto_revive`
and `offer_fresh_restart`, which SPAWN a session with `--dangerously-skip-permissions`. Every
case below is an actor shape that reached that code before this change.
"""

import json

import pytest

from bridge import common, daemon

OWNER = 7
BOT_ID = 123456789
CFG = {"chat_id": 1, "owner_id": OWNER, "bot_token": f"{BOT_ID}:AA-not-a-real-token"}


@pytest.fixture(autouse=True)
def _fresh_reject_coalescer():
    """#212 coalesces identical rejection lines inside a window; several tests here reject
    the same (kind, topic, sender) key and each assert on its OWN log line, so the window
    state must not leak between them."""
    daemon._svc_rejects.clear()
    yield
    daemon._svc_rejects.clear()


@pytest.fixture
def registry(tmp_path, monkeypatch):
    # STATE_DIR, not daemon.state_path: `read_registry` lives in bridge.common and resolves
    # through common's own module global, so patching only the daemon name leaves the reopen
    # branch reading an empty registry and the test proves nothing.
    monkeypatch.setattr(common, "STATE_DIR", str(tmp_path))
    root = tmp_path / "registry.json"
    root.write_text(json.dumps({"33": {"name": "bridge", "closed": True}}))
    monkeypatch.setattr(daemon, "log", lambda *a, **k: None)
    return root


def _read(root):
    return json.loads(root.read_text())


def _reopen(**extra):
    return {"chat": {"id": 1}, "message_thread_id": 33, "forum_topic_reopened": {}, **extra}


def _close(**extra):
    return {"chat": {"id": 1}, "message_thread_id": 33, "forum_topic_closed": {}, **extra}


@pytest.fixture
def revivals(monkeypatch):
    """Record every lifecycle call the reopen branch can make."""
    calls = []
    for name in ("maybe_auto_revive", "offer_fresh_restart", "offer_reopen_choice"):
        monkeypatch.setattr(daemon, name, lambda *a, _n=name, **k: calls.append(_n))
    monkeypatch.setattr(daemon, "unrevivable_reason", lambda entry: None)
    monkeypatch.setattr(daemon, "should_auto_revive", lambda entry: True)
    monkeypatch.setattr(daemon, "_reopen_needs_asking", lambda entry: False)
    return calls


# ---- accepted ---------------------------------------------------------------------------

def test_the_owner_may_close_a_topic(registry):
    daemon.handle_message(CFG, _close(**{"from": {"id": OWNER}}))
    assert _read(registry)["33"]["closed"] is True


def test_the_owner_may_reopen_a_topic_and_it_revives(registry, revivals):
    daemon.handle_message(CFG, _reopen(**{"from": {"id": OWNER}}))
    assert "closed" not in _read(registry)["33"]
    assert revivals == ["maybe_auto_revive"]


def test_the_bridges_own_echo_is_trusted(registry, revivals):
    # revive_one reopens the topic itself and Telegram echoes that back. Rejecting our own
    # echo would log a false alarm on every revive and bury a real one.
    daemon.handle_message(CFG, _reopen(**{"from": {"id": BOT_ID, "is_bot": True}}))
    assert "closed" not in _read(registry)["33"]


# ---- rejected ---------------------------------------------------------------------------

def test_a_service_message_with_no_sender_changes_nothing(registry, revivals):
    # This is the shape the previous test suite asserted SHOULD work.
    daemon.handle_message(CFG, _reopen())
    assert _read(registry)["33"]["closed"] is True
    assert revivals == []


def test_another_group_member_cannot_reopen(registry, revivals):
    daemon.handle_message(CFG, _reopen(**{"from": {"id": OWNER + 1}}))
    assert _read(registry)["33"]["closed"] is True
    assert revivals == []


def test_an_anonymous_admin_cannot_reopen(registry, revivals):
    # Anonymous admins post as the group itself: sender_chat, no `from`. The Bot API does not
    # say WHICH admin acted, so there is no actor to authorize.
    daemon.handle_message(CFG, _reopen(sender_chat={"id": 1, "type": "supergroup"}))
    assert _read(registry)["33"]["closed"] is True
    assert revivals == []


def test_another_bot_cannot_reopen(registry, revivals):
    daemon.handle_message(CFG, _reopen(**{"from": {"id": BOT_ID + 1, "is_bot": True}}))
    assert _read(registry)["33"]["closed"] is True
    assert revivals == []


@pytest.mark.parametrize("field", ["from", "sender_chat"])
@pytest.mark.parametrize("value", ["spoofed", [1], 7, True])
def test_a_truthy_non_mapping_actor_is_logged_rather_than_raising(
        registry, revivals, monkeypatch, field, value):
    """The denial must survive being explained.

    `msg.get("from") or {}` leaves a truthy non-dict exactly as it found it, so the rejection
    LOG LINE — not the gate — raised AttributeError on `.get("id")`. The event was already
    denied, so nothing unauthorized ran; what broke is the operator's only evidence that a
    rejection happened, which surfaced as a traceback from main's catch-all instead
    (#208 round-2 review, X1).
    """
    lines = []
    monkeypatch.setattr(daemon, "log", lines.append)
    daemon.handle_message(CFG, _reopen(**{field: value}))
    assert _read(registry)["33"]["closed"] is True
    assert revivals == []
    assert any("ignored forum reopen" in line and "untrusted sender" in line
               for line in lines), lines


def test_a_member_cannot_close_a_topic(registry):
    open_registry = {"33": {"name": "bridge"}}
    path = str(registry)
    with open(path, "w") as handle:
        json.dump(open_registry, handle)
    daemon.handle_message(CFG, _close(**{"from": {"id": OWNER + 1}}))
    assert "closed" not in _read(registry)["33"]


def test_a_missing_owner_pin_trusts_nobody(registry, revivals):
    # valid_owner_id semantics: an absent or malformed pin must disable ingress, never fall
    # back to trusting the group. True is deliberately not a valid owner id.
    for broken in (None, 0, -1, True, "7"):
        daemon.handle_message({**CFG, "owner_id": broken}, _reopen(**{"from": {"id": OWNER}}))
    assert _read(registry)["33"]["closed"] is True
    assert revivals == []


# ---- the bot-id derivation --------------------------------------------------------------

def test_the_bot_id_comes_from_the_token_prefix():
    assert daemon.bot_user_id(CFG) == BOT_ID


@pytest.mark.parametrize("token", [
    None, "", "no-colon", ":secret", "abc:secret", 12345, ["1:x"], {"a": 1},
    "0:secret",                       # zero is not an id
    "١٢٣:secret",                     # Arabic-Indic digits: str.isdigit() is True and
                                      # int() parses them — ASCII-only is the real test
    "9" * 20 + ":secret",             # wider than any id Telegram can have issued
    " 123:secret", "123 :secret", "+123:secret", "12_3:secret",
])
def test_an_implausible_token_yields_no_bot_id_rather_than_raising(token):
    # A bad token already fails at startup; this must never be what crashes update handling.
    assert daemon.bot_user_id({"bot_token": token}) is None


def test_no_bot_id_does_not_make_an_unidentified_sender_trusted():
    # `sender_id is None` and `bot_user_id(...) is None` must not compare equal into a pass.
    assert not daemon.service_event_is_trusted({"owner_id": OWNER, "bot_token": "bad"},
                                               {"forum_topic_reopened": {}})


# ---- fails closed on every malformed shape, and never raises -----------------------------

@pytest.mark.parametrize("sender_id", [
    True,          # bool is an int subclass and True == 1 — an id of 1 must not be satisfiable
    float(OWNER),  # 7.0 == 7
    str(OWNER),
    0, -1, None, [OWNER], {"id": OWNER},
    1 << 53,       # wider than 52 significant bits
])
def test_an_implausible_sender_id_is_denied(sender_id):
    assert not daemon.service_event_is_trusted(CFG, _reopen(**{"from": {"id": sender_id}}))


@pytest.mark.parametrize("sender", [None, "someone", 7, [], {"no_id": 1}])
def test_a_from_that_is_not_a_user_object_is_denied(sender):
    assert not daemon.service_event_is_trusted(CFG, _reopen(**{"from": sender}))


@pytest.mark.parametrize("owner", [True, 1.0, "7", 0, -1, None, [7]])
def test_an_implausible_owner_pin_authorizes_nobody(owner):
    # Including the mirror of the sender case: a float or bool OWNER must not match a real id.
    msg = _reopen(**{"from": {"id": 1}})
    assert not daemon.service_event_is_trusted({**CFG, "owner_id": owner}, msg)


@pytest.mark.parametrize("cfg,msg", [
    (None, {"from": {"id": OWNER}}),
    ("cfg", {"from": {"id": OWNER}}),
    (CFG, None),
    (CFG, "msg"),
    (CFG, []),
])
def test_malformed_arguments_deny_instead_of_raising(cfg, msg):
    assert daemon.service_event_is_trusted(cfg, msg) is False
