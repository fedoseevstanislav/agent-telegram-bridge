"""Theme-relevant forum-topic icons (#278).

Two different icons live on a topic and this feature touches only one of them. `pick_icon`
assigns the message SIGNATURE (who is speaking); this assigns the forum TOPIC icon (what the
topic is about), which is what the topic list is scanned by. A topic keeps both.

Bots may only set icons from `getForumTopicIconStickers`. FREE_SET below is a recorded copy of
that call's emoji (112 of them, read live on 2026-09-02) and it is what pins the rule table:
a rule naming a glyph outside the set degrades silently to "no icon", which reads like a
matching bug rather than the typo it is.
"""

import json

import pytest

from bridge import cli


FREE_SET = ["‼️", "⁉️", "☕️", "⚡️", "⚽️", "⛅️", "✅", "✈️", "✍️", "❓", "❗️", "❤️", "⭐️", "🆒",
            "🍓", "🍔", "🍕", "🍣", "🍹", "🍽", "🎂", "🎃", "🎄", "🎉", "🎓", "🎖", "🎙", "🎟",
            "🎤", "🎨", "🎩", "🎬", "🎭", "🎮", "🎵", "🎶", "🏀", "🏁", "🏆", "🏔", "🏕", "🏖",
            "🏛", "🏠", "🏴‍☠️", "🐈", "🐟", "👀", "👑", "👜", "👠", "👨‍👩‍👧‍👦", "👮‍♂️", "👶",
            "💃", "💄", "💅", "💉", "💊", "💎", "💘", "💡", "💬", "💰", "💱", "💸", "💻", "💼",
            "📁", "📆", "📈", "📉", "📚", "📝", "📣", "📰", "📱", "📺", "🔎", "🔝", "🔞", "🔥",
            "🔬", "🔭", "🔮", "🕺", "🖨", "🗣", "🗳", "🚂", "🚗", "🛃", "🛍", "🛒", "🛥", "🤖",
            "🤡", "🤰", "🦄", "🦠", "🦮", "🧠", "🧪", "🧮", "🧳", "🧼", "🩺", "🪖", "🪙", "🪩",
            "🪪", "🫦"]


@pytest.fixture(autouse=True)
def _clear_icon_cache():
    cli._ICON_SET_CACHE.clear()
    yield
    cli._ICON_SET_CACHE.clear()


class FakeApi:
    """Records every Bot API call and answers the two this feature makes."""

    DEFAULT = object()   # distinct from a payload that really is None

    def __init__(self, stickers=DEFAULT, fail=None, topic_id=4242):
        self.calls = []
        self.stickers = ([{"emoji": e, "custom_emoji_id": f"id-{i}"}
                          for i, e in enumerate(FREE_SET)]
                         if stickers is FakeApi.DEFAULT else stickers)
        self.fail = fail          # method name that should raise
        self.topic_id = topic_id
        self.kwargs = []          # (method, kwargs) — timeout/retries are load-bearing here

    def __call__(self, _token, method, params, **kw):
        self.calls.append((method, params))
        self.kwargs.append((method, kw))
        if self.fail == method:
            raise RuntimeError("telegram is down")
        if method == "getForumTopicIconStickers":
            return self.stickers
        if method == "createForumTopic":
            return {"message_thread_id": self.topic_id}
        return {}

    def of(self, method):
        return [p for m, p in self.calls if m == method]

    def count(self, method):
        return sum(1 for m, _p in self.calls if m == method)


CFG = {"bot_token": "T", "chat_id": -100}


# --------------------------------------------------------------------------- C4 (matching)


def test_every_rule_names_an_emoji_the_bot_may_actually_set():
    # The whole point of the recorded set: a typo'd or premium glyph is invisible at runtime.
    for pattern, emoji in cli.TOPIC_ICON_RULES:
        assert emoji in FREE_SET, f"rule {pattern!r} names {emoji!r}, absent from the free set"


@pytest.mark.parametrize("name,want", [
    # topic names in the live registry's own shapes; third-party organisation names are
    # invented stand-ins (see FORBIDDEN in test_no_production_identifiers.py)
    ("Telegram bridge build", "💻"),
    ("security", "👮‍♂️"),
    ("release-sanitiser", "👮‍♂️"),      # 'sanitiser' is the security rule, which precedes release
    ("POS Memory", "🧠"),
    ("POS Extraction", "🧠"),
    ("POS Intake", "🧠"),
    ("POS Learning", "📚"),
    ("Model Research", "📚"),           # research precedes the generic model/ai rule
    ("Meeting Dedup", "📆"),
    ("realtime-meeting-agent", "📆"),   # meeting precedes agent
    ("Northwind AI transformation", "🤖"),
    ("company-shape", "🏛"),            # a hyphen is a word boundary
    ("acme-fintech", "💰"),
    ("Gym training", "🩺"),
    ("Murmur debug", "💻"),
    # no rule matches -> no icon, deliberately
    ("POS Choice", None),
    ("Willow Harbour", None),
    ("Vault Size", None),
    ("Beacon Holdings", None),
])
def test_theme_emoji_for_real_topic_names(name, want):
    assert cli.theme_emoji(name) == want


def test_matching_is_case_insensitive_and_first_rule_wins():
    assert cli.theme_emoji("SECURITY REVIEW") == "👮‍♂️"   # security precedes review
    assert cli.theme_emoji("security review") == "👮‍♂️"
    assert cli.theme_emoji("Review of the deck") == "🔎"   # review precedes client/deck


def test_word_boundaries_prevent_substring_matches():
    # An unanchored "ai" matched "email" and "ops" matched "Chronops" — the reason for \b.
    assert cli.theme_emoji("email cleanup") is None
    assert cli.theme_emoji("Chronops rollout") == "🏁"     # matches 'rollout', not 'ops'
    assert cli.theme_emoji("plainly") is None


@pytest.mark.parametrize("name", [None, 42, b"bytes", ""])
def test_a_non_string_or_empty_name_never_raises(name):
    assert cli.theme_emoji(name) is None


# --------------------------------------------------------------------------- C1 / C2 / C3


def _register(monkeypatch, tmp_path, name, fake):
    monkeypatch.setattr(cli, "api", fake)
    written = {}
    monkeypatch.setattr(cli, "update_registry",
                        lambda fn: fn(written) or written)
    args = type("A", (), {"name": name, "feed": False, "bind": False})()
    monkeypatch.setattr(cli.os, "getcwd", lambda: str(tmp_path))
    monkeypatch.delenv("TMUX_PANE", raising=False)
    cli.cmd_register(CFG, args)
    return written


def test_register_sets_the_themed_icon_on_the_create_call(monkeypatch, tmp_path, capsys):
    fake = FakeApi()
    written = _register(monkeypatch, tmp_path, "Telegram bridge build", fake)

    created = fake.of("createForumTopic")
    assert len(created) == 1
    assert created[0]["name"] == "Telegram bridge build"
    assert created[0]["icon_custom_emoji_id"] == f"id-{FREE_SET.index('💻')}"
    # one round trip: the icon rides create, never a follow-up edit
    assert fake.count("editForumTopic") == 0
    entry = written["4242"]
    assert entry["topic_icon"] == "💻"
    # A4: the message-signature emoji is untouched and still comes from the ICONS palette
    assert entry["icon"] in cli.ICONS


def test_a_name_matching_no_rule_creates_the_topic_with_no_icon(monkeypatch, tmp_path):
    fake = FakeApi()
    written = _register(monkeypatch, tmp_path, "Willow Harbour", fake)

    assert "icon_custom_emoji_id" not in fake.of("createForumTopic")[0]
    assert "topic_icon" not in written["4242"]
    assert written["4242"]["icon"] in cli.ICONS      # registration still succeeded


def test_a_rule_emoji_absent_from_the_free_set_falls_back_to_no_icon(monkeypatch, tmp_path):
    # The set is Telegram's and can shrink under us; a missing glyph is not a failure.
    fake = FakeApi(stickers=[{"emoji": "🎉", "custom_emoji_id": "id-party"}])
    _register(monkeypatch, tmp_path, "Telegram bridge build", fake)

    assert "icon_custom_emoji_id" not in fake.of("createForumTopic")[0]


def test_a_failing_sticker_call_does_not_break_registration(monkeypatch, tmp_path):
    fake = FakeApi(fail="getForumTopicIconStickers")
    written = _register(monkeypatch, tmp_path, "Telegram bridge build", fake)

    assert fake.count("createForumTopic") == 1
    assert "icon_custom_emoji_id" not in fake.of("createForumTopic")[0]
    assert written["4242"]["icon"] in cli.ICONS


@pytest.mark.parametrize("stickers", [
    None, "not a list", 42, {"a": 1}, [], [{}], [{"emoji": "💻"}], [{"custom_emoji_id": "x"}],
    # a tuple is iterable but is not a list: the criterion says non-list degrades (r1 finding)
    ({"emoji": "💻", "custom_emoji_id": "id-real"},),
    [{"emoji": 1, "custom_emoji_id": 2}], [{"emoji": "💻", "custom_emoji_id": ""}],
])
def test_a_malformed_sticker_payload_degrades_to_no_icon(monkeypatch, tmp_path, stickers):
    fake = FakeApi(stickers=stickers)
    _register(monkeypatch, tmp_path, "Telegram bridge build", fake)

    assert "icon_custom_emoji_id" not in fake.of("createForumTopic")[0]


def test_the_icon_lookup_cannot_stall_registration(monkeypatch, tmp_path):
    """C2 forbids DELAYING registration, and this call sits in front of createForumTopic.

    `api` defaults to timeout=70, retries=3 — up to ~3.5 minutes of cosmetics ahead of the one
    call that registers the session. The decoration gets one short attempt instead."""
    fake = FakeApi()
    _register(monkeypatch, tmp_path, "Telegram bridge build", fake)

    kw = dict(fake.kwargs)["getForumTopicIconStickers"]
    assert kw["timeout"] == cli.ICON_SET_TIMEOUT <= 10
    assert kw["retries"] == 1
    # the create call itself keeps the library defaults — it is the one that matters
    assert dict(fake.kwargs)["createForumTopic"] == {}


def test_the_sticker_set_is_fetched_at_most_once_per_process(monkeypatch):
    fake = FakeApi()
    monkeypatch.setattr(cli, "api", fake)

    for _ in range(5):
        cli.theme_icon_id(CFG, "Telegram bridge build")

    assert fake.count("getForumTopicIconStickers") == 1


def test_a_failed_fetch_is_cached_too_so_it_is_not_retried_per_topic(monkeypatch):
    fake = FakeApi(fail="getForumTopicIconStickers")
    monkeypatch.setattr(cli, "api", fake)

    for _ in range(5):
        assert cli.theme_icon_id(CFG, "Telegram bridge build") is None

    assert fake.count("getForumTopicIconStickers") == 1


# --------------------------------------------------------------------------- C5 (retheme)


REG = {
    "33": {"name": "Telegram bridge build"},
    "11445": {"name": "security"},
    "1061": {"name": "Gym training", "topic_icon": "🩺"},   # already correct
    "9999": {"name": "POS Choice"},                          # no rule
    "8888": {"name": "old build", "ended": True},
    "7777": {"name": "build feed", "feed": True},
}


def test_the_plan_covers_only_live_topics_that_would_change():
    plan = cli.retheme_plan(CFG, REG)

    assert [(tid, want) for tid, _n, _c, want in plan] == [(33, "💻"), (11445, "👮‍♂️")]


def test_retheme_writes_nothing_without_apply(monkeypatch, capsys):
    fake = FakeApi()
    monkeypatch.setattr(cli, "api", fake)
    monkeypatch.setattr(cli, "read_registry", lambda: dict(REG))
    monkeypatch.setattr(cli, "update_registry",
                        lambda fn: pytest.fail("registry written during a dry run"))

    cli.cmd_retheme(CFG, type("A", (), {"apply": False})())

    assert fake.count("editForumTopic") == 0
    out = capsys.readouterr().out
    assert "2 topic(s) would change" in out and "--apply" in out
    assert "💻" in out and "👮‍♂️" in out


def test_retheme_with_apply_edits_each_changed_topic_once(monkeypatch, capsys):
    fake = FakeApi()
    reg = {k: dict(v) for k, v in REG.items()}
    monkeypatch.setattr(cli, "api", fake)
    monkeypatch.setattr(cli, "read_registry", lambda: reg)
    monkeypatch.setattr(cli, "update_registry", lambda fn: fn(reg))

    cli.cmd_retheme(CFG, type("A", (), {"apply": True})())

    edits = fake.of("editForumTopic")
    assert [e["message_thread_id"] for e in edits] == [33, 11445]
    assert edits[0]["icon_custom_emoji_id"] == f"id-{FREE_SET.index('💻')}"
    assert reg["33"]["topic_icon"] == "💻" and reg["11445"]["topic_icon"] == "👮‍♂️"
    assert "applied to 2" in capsys.readouterr().out
    # rerunning is a no-op now that the registry records what was set
    assert cli.retheme_plan(CFG, reg) == []


def test_one_refused_topic_does_not_stop_the_batch(monkeypatch, capsys):
    reg = {k: dict(v) for k, v in REG.items()}

    class Refusing(FakeApi):
        def __call__(self, token, method, params, **kw):
            if method == "editForumTopic" and params.get("message_thread_id") == 33:
                self.calls.append((method, params))
                raise RuntimeError("topic was closed")
            return super().__call__(token, method, params, **kw)

    fake = Refusing()
    monkeypatch.setattr(cli, "api", fake)
    monkeypatch.setattr(cli, "read_registry", lambda: reg)
    monkeypatch.setattr(cli, "update_registry", lambda fn: fn(reg))

    cli.cmd_retheme(CFG, type("A", (), {"apply": True})())

    assert "topic_icon" not in reg["33"]          # not recorded — it never landed
    assert reg["11445"]["topic_icon"] == "👮‍♂️"   # the rest of the batch still ran
    assert "applied to 1" in capsys.readouterr().out


def test_retheme_reports_when_there_is_nothing_to_do(monkeypatch, capsys):
    fake = FakeApi()
    monkeypatch.setattr(cli, "api", fake)
    monkeypatch.setattr(cli, "read_registry", lambda: {"9999": {"name": "POS Choice"}})

    cli.cmd_retheme(CFG, type("A", (), {"apply": True})())

    assert fake.count("editForumTopic") == 0
    assert "Nothing to retheme" in capsys.readouterr().out


def test_retheme_is_wired_into_the_parser():
    args = cli.build_parser().parse_args(["retheme"])
    assert args.command == "retheme" and args.apply is False
    assert cli.build_parser().parse_args(["retheme", "--apply"]).apply is True
