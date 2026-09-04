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
    # the stages of one pipeline have their own rules, so sibling topics differ (#296)
    ("POS Extraction", "🔭"),
    ("POS Intake", "📁"),
    ("POS Learning", "📚"),
    ("Model Research", "🔬"),           # research precedes learning and the model/ai rule
    ("Meeting Dedup", "🧼"),           # dedup precedes meeting
    ("realtime-meeting-agent", "📆"),   # meeting precedes agent
    ("Northwind AI transformation", "🤖"),
    ("company-shape", "🏛"),            # a hyphen is a word boundary
    ("acme-fintech", "💰"),
    ("Gym training", "🩺"),
    ("Mesh debug", "🗣"),              # mesh precedes the bridge/debug rule
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
    # the signature is a separate value; since #296 it is the same subject emoji, sent as
    # plain text rather than as the topic's custom_emoji_id
    assert entry["icon"] == "💻"


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
    # the signature needs no id lookup, so it survives a dead sticker call
    assert written["4242"]["icon"] == "💻"


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


# --------------------------------------------------------------------------- #296 (signature)
#
# The SIGNATURE icon now reads the same rule table: subject first, generic palette when no
# rule matches. `reicon` catches up existing topics; hand-set icons are never touched.


def test_a_matching_name_gets_its_subject_emoji_as_the_signature():
    assert cli.pick_icon({}, 4242, "Telegram bridge build") == "💻"
    assert cli.pick_icon({}, 4242, "security") == "👮‍♂️"


def test_a_taken_subject_emoji_falls_through_to_another_matching_rule():
    reg = {"1": {"icon": "💻", "name": "Telegram bridge build"}}
    # 'security review' matches security (👮‍♂️) then review (🔎); 'bridge review' matches
    # bridge (💻, taken) then review (🔎)
    assert cli.pick_icon(reg, 4242, "bridge review") == "🔎"


def test_an_ended_topic_does_not_hold_its_subject_emoji():
    reg = {"1": {"icon": "💻", "name": "Telegram bridge build", "ended": True}}
    assert cli.pick_icon(reg, 4242, "Telegram bridge build") == "💻"


def test_a_name_matching_no_rule_still_gets_the_generic_palette():
    assert cli.pick_icon({}, 4242, "Willow Harbour") == cli.ICONS[0]
    assert cli.pick_icon({}, 4242, None) == cli.ICONS[0]


def test_a_name_whose_every_matching_emoji_is_taken_gets_a_generic_icon():
    # register never shares: an unused generic glyph still separates this session from that one
    reg = {"1": {"icon": "💻", "name": "bridge"}}
    assert cli.pick_icon(reg, 4242, "bridge build") == cli.ICONS[0]


def test_the_generic_fallback_and_its_exhaustion_are_unchanged():
    reg = {str(i): {"icon": icon, "name": "Willow Harbour"}
           for i, icon in enumerate(cli.ICONS)}
    assert cli.pick_icon(reg, 3, "Willow Harbour") == cli.ICONS[3 % len(cli.ICONS)]
    assert cli.pick_icon(reg, 3, "Telegram bridge build") == "💻"   # subject icon is free


def test_register_stamps_the_subject_signature(monkeypatch, tmp_path):
    fake = FakeApi()
    written = _register(monkeypatch, tmp_path, "Telegram bridge build", fake)

    assert written["4242"]["icon"] == "💻"


# --- reicon -----------------------------------------------------------------

SIG_REG = {
    "201": {"name": "Telegram bridge build", "icon": "🦊"},        # generic -> subject
    "202": {"name": "Gym training", "icon": "💪"},                 # hand-set, must not move
    "204": {"name": "Willow Harbour", "icon": "🐙"},               # no rule -> keep
    "205": {"name": "bridge daemon debug", "icon": "🦉"},          # collides with 33
    "206": {"name": "old build", "icon": "🐳", "ended": True},     # ended -> skipped
    "207": {"name": "build feed", "icon": "📡", "feed": True},     # feed -> skipped
    "208": {"name": "security", "icon": "⚡"},                      # generic -> subject
}


def test_the_reicon_plan_covers_only_generic_icons_that_would_change():
    plan = cli.reicon_plan(SIG_REG)

    assert [(tid, old, new) for tid, _n, old, new, _s in plan] == [
        (201, "🦊", "💻"), (205, "🦉", "💻"), (208, "⚡", "👮‍♂️")]


def test_a_hand_set_icon_is_never_listed_and_holds_its_emoji():
    reg = {"202": {"name": "Gym training", "icon": "🩺"},           # hand-set = the rule's own
           "203": {"name": "sleep habit", "icon": "🦊"}}            # same rule, 🩺 already held
    plan = cli.reicon_plan(reg)

    # 202 is never listed; 203 has no free subject emoji, so it shares 202's and says so
    assert plan == [(203, "sleep habit", "🦊", "🩺", 202)]
    assert cli.reicon_plan({"202": reg["202"]}) == []


def test_the_shared_icon_is_reported_with_the_topic_that_holds_it():
    plan = cli.reicon_plan(SIG_REG)
    shared = {tid: holder for tid, _n, _o, _w, holder in plan}

    assert shared == {201: None, 205: 201, 208: None}


def test_reicon_writes_nothing_without_apply(monkeypatch, capsys):
    monkeypatch.setattr(cli, "read_registry", lambda: {k: dict(v) for k, v in SIG_REG.items()})
    monkeypatch.setattr(cli, "update_registry",
                        lambda fn: pytest.fail("registry written during a dry run"))

    cli.cmd_reicon(CFG, type("A", (), {"apply": False})())

    out = capsys.readouterr().out
    assert "topic 201 Telegram bridge build: 🦊 -> 💻" in out
    assert "topic 208 security: ⚡ -> 👮‍♂️" in out
    assert "topic 205 bridge daemon debug: 🦉 -> 💻" in out
    assert "shared with topic 201" in out            # C3: the collision is on the line
    assert "3 topic(s) would change" in out and "--apply" in out
    assert "💪" not in out and "📡" not in out       # hand-set and feed icons never listed


def test_reicon_with_apply_writes_the_registry(monkeypatch, capsys):
    reg = {k: dict(v) for k, v in SIG_REG.items()}
    monkeypatch.setattr(cli, "read_registry", lambda: {k: dict(v) for k, v in reg.items()})
    monkeypatch.setattr(cli, "update_registry", lambda fn: fn(reg))

    cli.cmd_reicon(CFG, type("A", (), {"apply": True})())

    assert reg["201"]["icon"] == "💻" and reg["208"]["icon"] == "👮‍♂️"
    assert reg["205"]["icon"] == "💻"                 # the collision is applied, not skipped
    assert reg["202"]["icon"] == "💪"                 # hand-set untouched
    assert reg["204"]["icon"] == "🐙" and reg["206"]["icon"] == "🐳" and reg["207"]["icon"] == "📡"
    assert "applied to 3" in capsys.readouterr().out
    assert cli.reicon_plan(reg) == []                # rerunning is a no-op


def test_reicon_sends_nothing_to_telegram(monkeypatch):
    fake = FakeApi()
    reg = {k: dict(v) for k, v in SIG_REG.items()}
    monkeypatch.setattr(cli, "api", fake)
    monkeypatch.setattr(cli, "read_registry", lambda: reg)
    monkeypatch.setattr(cli, "update_registry", lambda fn: fn(reg))

    cli.cmd_reicon(CFG, type("A", (), {"apply": True})())

    assert fake.calls == []      # the signature lives in the registry only


def test_reicon_reports_when_there_is_nothing_to_do(monkeypatch, capsys):
    monkeypatch.setattr(cli, "read_registry", lambda: {"204": {"name": "Willow Harbour",
                                                             "icon": "🐙"}})

    cli.cmd_reicon(CFG, type("A", (), {"apply": True})())

    assert "Nothing to reicon" in capsys.readouterr().out


def test_reicon_is_wired_into_the_parser():
    args = cli.build_parser().parse_args(["reicon"])
    assert args.command == "reicon" and args.apply is False
    assert cli.build_parser().parse_args(["reicon", "--apply"]).apply is True
