"""tg-bridge CLI: a Claude Code session's interface to its Telegram topic.

Commands:
  register --name NAME        create a forum topic for this session, print its id
  send TEXT                   send text into the session's topic
  recv [--wait N] [--peek]    print new inbox messages (optionally block up to N seconds)
  ask TEXT [--timeout N]      send, then block until a reply arrives (or timeout)
  current-topic               print the live topic bound to TMUX_PANE as JSON
  notify --topic N ...        enqueue an event into another topic's inbox, wake its pane,
                              mirror to Telegram (the sanctioned session -> session path)
  status                      daemon health + registered topics
  retheme [--apply]           give existing topics a theme-relevant forum icon (dry by default)
  reicon [--apply]            give existing topics a subject-matched signature icon (dry too)

Topic resolution order: --topic flag, TG_BRIDGE_TOPIC env var, ./.tg-bridge-topic file.
"""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bridge.common import (
    secure_process_umask,
    CAPTION_LIMIT, PossiblyDelivered, api, append_jsonl_once,
    commit_cursor_if_inbox_current, file_send_plan, load_config, now_iso, read_registry,
    send_file, send_message, state_path, update_registry,
)
from bridge import codex_ctx
from bridge.daemon import maybe_nudge, pane_alive

CWD_BINDING_FILE = ".tg-bridge-topic"

# Visually distinct session icons — the fallback palette for the signature emoji shown in
# message headers, used when the topic's name matches no subject rule (see pick_icon, #296)
ICONS = ("🦊", "🐙", "🦉", "🐳", "⚡", "🌵", "🎯", "🧩", "🛰️", "🌶️",
         "🪐", "🦜", "🍄", "🗿", "🌊", "🔥")

# Telegram custom-emoji ids are decimal digit strings; see custom_emoji_id (#292).
_CUSTOM_EMOJI_ID = re.compile(r"[0-9]+")


# ---- theme-relevant forum-topic icons (#278) ----
#
# Two DIFFERENT icons, deliberately. `ICONS`/`pick_icon` above is the message SIGNATURE — one
# emoji per session, so a line in General says who is speaking. This is the forum TOPIC icon,
# the glyph Telegram shows in the topic list, which says what the topic is ABOUT so the list
# can be scanned by subject. A topic keeps both.
#
# The rules below are read by both (#296): the signature is drawn from this table first and
# falls back to `ICONS` only when nothing matches, because "what the topic is about" beats a
# random glyph at telling two sessions apart, and one table means one place to edit. They stay
# two icons — the topic icon must come from Telegram's bot-settable set and is addressed by a
# custom_emoji_id, while the signature is the plain emoji, prepended as text.
#
# Bots may only use the icons in `getForumTopicIconStickers` (112 of them; premium custom
# emoji are settable from a user account only), and each is addressed by a `custom_emoji_id`
# that belongs to Telegram, not to us — so the ids are resolved at runtime from that call and
# never written down here. The rules below name EMOJI; the resolution to an id is a lookup.
#
# First match wins, so order is precedence: the specific rules come before the general ones.
# Since the signature reads this table too (#296), stages of one pipeline that used to share a
# rule — intake, extraction, memory; research, learning — have their own, so sibling topics get
# different glyphs instead of one broad subject's.
# `\b` boundaries matter — an unanchored "ops" matched "Chronops" and "ai" matched "email".
# Every emoji here was checked against a live `getForumTopicIconStickers` response — a rule
# naming a glyph outside that set silently degrades to no icon, which looks like a matching
# bug rather than the typo it is (`tests/test_topic_icons.py` pins the whole table against a
# recorded copy of the set).
TOPIC_ICON_RULES = (
    (r"\b(security|secret|auth|token|vuln|sanitiser|sanitizer)\b", "👮‍♂️"),
    (r"\b(dedup|dedupe|duplicates?)\b", "🧼"),
    (r"\b(mesh|messaging|chatter)\b", "🗣"),
    (r"\b(meeting|calendar|schedule|agenda|standup)\b", "📆"),
    (r"\b(release|launch|ship|milestone|rollout)\b", "🏁"),
    (r"\b(review|audit|verify|qa)\b", "🔎"),
    (r"\b(test|experiment|eval|trial|benchmark)\b", "🧪"),
    (r"\b(bridge|daemon|cli|repo|build|deploy|refactor|bug|patch|debug|infra)\b", "💻"),
    (r"\b(intake|ingest(ion)?|inbox|capture)\b", "📁"),
    (r"\b(extract(ion|or)?|parser?|mining)\b", "🔭"),
    (r"\b(graph|memory|ontology|embedding|recall)\b", "🧠"),
    (r"\b(research|study|survey)\b", "🔬"),
    (r"\b(learn(ing)?|paper|wiki|knowledge|docs?)\b", "📚"),
    (r"\b(client|consult(ing)?|deck|proposal|pitch|gtm|sales|lead)\b", "💼"),
    (r"\b(growth|metric|analytics|revenue|funnel|market(ing)?|seo)\b", "📈"),
    (r"\b(money|invoice|billing|budget|cost|pricing|tax|fintech|finance)\b", "💰"),
    (r"\b(company|legal|hr|hiring|org|corp)\b", "🏛"),
    (r"\b(gym|health|training|sleep|food|habit|medical)\b", "🩺"),
    (r"\b(home|house|flat|estate|apartment|renovation)\b", "🏠"),
    (r"\b(travel|trip|flight|visa|hotel)\b", "✈️"),
    (r"\b(idea|design|concept|vision|strategy|plan(ning)?)\b", "💡"),
    (r"\b(news|digest|feed|report)\b", "📰"),
    (r"\b(content|writing|article|blog|copy)\b", "✍️"),
    (r"\b(video|film|movie|recording)\b", "🎬"),
    (r"\b(agent|bot|model|llm|ai|orchestra|prompt)\b", "🤖"),
)
_ICON_SET_CACHE = []   # [ {emoji: custom_emoji_id} ] — one slot, so "fetched and empty" is
                       # distinguishable from "not fetched yet" without a sentinel global
ICON_SET_TIMEOUT = 5   # s; this call sits in front of registration — see icon_emoji_ids


def theme_emoji(name):
    """The themed emoji for a topic name, or None when nothing matches.

    No match means NO icon — the topic keeps Telegram's default. Falling back to a generic
    glyph would make the list uniform again, which is the thing this feature exists to fix,
    and a wrong-but-confident icon is worse for scanning than an honest blank."""
    if not isinstance(name, str):
        return None
    for pattern, emoji in TOPIC_ICON_RULES:
        if re.search(pattern, name, re.IGNORECASE):
            return emoji
    return None


def icon_emoji_ids(cfg):
    """{emoji: custom_emoji_id} for the icons a BOT may use, fetched at most once per process.

    Every failure degrades to an empty map, i.e. to "no themed icon": the payload is not ours,
    the ids can change under us, and none of that is a reason to fail a registration. The
    cache is populated even when empty so one dead call does not become one call per topic.

    Bounded hard, because this runs BEFORE `createForumTopic` and a session is waiting on it:
    `api`'s defaults are `timeout=70, retries=3`, so the default would put up to ~3.5 minutes
    of cosmetics in front of the one call that actually registers the session. A decoration
    gets one short attempt — 5 s, no retry — and anything slower is treated exactly like a
    failure, which is "no themed icon"."""
    if _ICON_SET_CACHE:
        return _ICON_SET_CACHE[0]
    table = {}
    try:
        stickers = api(cfg["bot_token"], "getForumTopicIconStickers", {},
                       timeout=ICON_SET_TIMEOUT, retries=1)
        if not isinstance(stickers, list):
            raise TypeError(f"sticker set is {type(stickers).__name__}, not a list")
        for sticker in stickers:
            if not isinstance(sticker, dict):
                continue
            emoji, emoji_id = sticker.get("emoji"), sticker.get("custom_emoji_id")
            if isinstance(emoji, str) and isinstance(emoji_id, str) and emoji_id:
                table.setdefault(emoji, emoji_id)
    except Exception:
        table = {}
    _ICON_SET_CACHE.append(table)
    return table


def theme_icon_id(cfg, name):
    """`custom_emoji_id` for a topic name, or None. None at any step means "leave the default":
    no rule matched, the API is unreachable, or the rule's emoji is not in the free set."""
    emoji = theme_emoji(name)
    return icon_emoji_ids(cfg).get(emoji) if emoji else None


def subject_icon(name, taken):
    """(emoji, shared) for a topic name from the #278 rule table, or (None, False) — no match.

    Several rules can match one name and table order is precedence, so the alternates of a
    name are its later matches: the first emoji no live topic carries wins, which keeps two
    neighbours on the same broad subject apart when the name gives anything to tell them by.
    `shared` is True when every matching emoji is already carried; the caller decides whether
    sharing beats its own fallback."""
    if not isinstance(name, str):
        return None, False
    matches = [emoji for pattern, emoji in TOPIC_ICON_RULES
               if re.search(pattern, name, re.IGNORECASE)]
    for emoji in matches:
        if emoji not in taken:
            return emoji, False
    return (matches[0], True) if matches else (None, False)


def pick_icon(registry, topic_id, name=None):
    """The message-signature emoji for a topic: its subject's icon when one is free (#296),
    otherwise the generic distinct-from-neighbours palette exactly as before.

    A subject icon that is already carried is NOT taken here: at register time an unused
    generic glyph still separates this session from that one, and sharing is left to `reicon`,
    where the alternative is a glyph that says nothing."""
    taken = {info.get("icon") for info in registry.values() if not info.get("ended")}
    emoji, shared = subject_icon(name, taken)
    if emoji and not shared:
        return emoji
    for icon in ICONS:
        if icon not in taken:
            return icon
    return ICONS[int(topic_id) % len(ICONS)]  # palette exhausted; collisions acceptable


def resolve_topic(args):
    if getattr(args, "topic", None):
        return int(args.topic)
    if os.environ.get("TG_BRIDGE_TOPIC"):
        return int(os.environ["TG_BRIDGE_TOPIC"])
    if os.path.exists(CWD_BINDING_FILE):
        with open(CWD_BINDING_FILE) as f:
            return int(f.read().strip())
    raise SystemExit(
        "no topic bound: pass --topic ID, set TG_BRIDGE_TOPIC, or run `tg-bridge register` first"
    )


def cmd_register(cfg, args):
    name = args.name or f"session {now_iso()}"
    # The themed icon rides the CREATE call rather than a follow-up editForumTopic: one round
    # trip, and a topic is never briefly wrong. A None means "leave Telegram's default".
    params = {"chat_id": cfg["chat_id"], "name": name}
    emoji_id = theme_icon_id(cfg, name)
    if emoji_id:
        params["icon_custom_emoji_id"] = emoji_id
    topic = api(cfg["bot_token"], "createForumTopic", params)
    topic_id = topic["message_thread_id"]
    entry = {"name": name, "created": now_iso(), "cwd": os.getcwd()}
    if args.feed:
        # event feed, not a dialog: no pane binding -> no warnings, nudges, or lifecycle notices
        entry["feed"] = True
        entry["icon"] = "📡"
    # icon needs the existing registry to avoid collisions; compute + write atomically so a
    # concurrent daemon snapshot/revive can't clobber this new registration.
    if emoji_id:
        # Remember the themed emoji, not its id: ids belong to Telegram and can be reissued,
        # and this is only ever compared against a freshly computed emoji. There is no Bot API
        # call that reads a topic's current icon back, so what we set is the only record of it.
        entry["topic_icon"] = theme_emoji(name)

    def _add(reg):
        if not args.feed:
            entry["icon"] = pick_icon(reg, topic_id, name)
            if os.environ.get("TMUX_PANE"):
                entry["pane"] = os.environ["TMUX_PANE"]
        reg[str(topic_id)] = entry
    update_registry(_add)
    if args.bind:
        with open(CWD_BINDING_FILE, "w") as f:
            f.write(str(topic_id))
    print(f"topic_id={topic_id}")
    print(f"Pass --topic {topic_id} to send/recv/ask, or export TG_BRIDGE_TOPIC={topic_id}")
    if entry.get("feed"):
        print("Feed topic: no context warnings, idle nudges, or lifecycle notices")
    elif "pane" in entry:
        print(f"Idle nudges enabled: new replies will be typed into tmux pane {entry['pane']}")


def retheme_plan(cfg, registry):
    """[(topic_id, name, current_emoji, wanted_emoji)] for live dialog topics needing a change.

    Skipped: feed and ended topics (nobody navigates to them), names that match no rule (they
    keep Telegram's default rather than being given a guess), and topics already carrying the
    wanted emoji. `topic_icon` is what THIS tool last set — the Bot API has no call that reads
    a topic's icon back, so a topic themed by hand looks unthemed here and will be re-set to
    the same glyph at worst."""
    plan = []
    for tid, info in sorted(registry.items(), key=lambda kv: int(kv[0])):
        if not isinstance(info, dict) or info.get("feed") or info.get("ended"):
            continue
        name = info.get("name")
        want = theme_emoji(name)
        if not want or info.get("topic_icon") == want:
            continue
        plan.append((int(tid), name, info.get("topic_icon"), want))
    return plan


def cmd_retheme(cfg, args):
    """Give existing topics their theme icon. Prints the plan; writes only under --apply.

    A bulk edit of the owner's forum is not something to do as a side effect of being run, so
    the default is a dry run and `--apply` is the whole of the difference."""
    plan = retheme_plan(cfg, read_registry())
    if not plan:
        print("Nothing to retheme.")
        return
    for tid, name, current, want in plan:
        print(f"  topic {tid:>6}  {current or '—'} -> {want}  {name}")
    if not args.apply:
        print(f"{len(plan)} topic(s) would change. Re-run with --apply to write them.")
        return
    ids = icon_emoji_ids(cfg)
    changed = 0
    for tid, name, _current, want in plan:
        emoji_id = ids.get(want)
        if not emoji_id:
            print(f"  topic {tid}: {want} is not in the bot-settable set — skipped")
            continue
        try:
            api(cfg["bot_token"], "editForumTopic", {
                "chat_id": cfg["chat_id"], "message_thread_id": tid,
                "icon_custom_emoji_id": emoji_id,
            })
        except Exception as e:
            # One topic that refuses the edit (closed, deleted, rights revoked) must not stop
            # the rest: this is a batch, and a half-applied batch is the normal outcome.
            print(f"  topic {tid}: failed ({e})")
            continue
        update_registry(lambda reg, t=str(tid), w=want:
                        reg[t].__setitem__("topic_icon", w) if t in reg else None)
        changed += 1
    print(f"Retheme applied to {changed} topic(s).")


def reicon_plan(registry):
    """[(topic_id, name, current, wanted, shared_with)] for live topics whose SIGNATURE icon
    would change (#296).

    Only an icon still from the generic `ICONS` palette is a candidate — that is the set this
    command exists to replace. An icon outside it was either set by hand, or is a feed's 📡, or
    is a subject icon a post-#296 registration already chose; the registry does not record
    which, and none of the three wants overwriting. Those topics are skipped and their icons
    are held against the plan so a subject icon never duplicates one.

    Topics are walked in id order and each assignment is held too, so an earlier topic's icon
    is taken for the later ones. `shared_with` is the topic already holding the emoji when no
    other matching rule was free: this command exists to replace glyphs that say nothing, and
    a shared subject icon still says what the topic is about, so the collision is reported
    rather than avoided."""
    holder = {}          # emoji -> topic id currently expected to carry it
    changeable = []
    for tid, info in sorted(registry.items(), key=lambda kv: int(kv[0])):
        if not isinstance(info, dict) or info.get("ended"):
            continue
        icon = info.get("icon")
        if info.get("feed") or icon not in ICONS:
            holder.setdefault(icon, int(tid))
            continue
        changeable.append((int(tid), info.get("name"), icon))
    plan = []
    for tid, name, icon in changeable:
        want, shared = subject_icon(name, set(holder))
        if not want or want == icon:
            holder.setdefault(icon, tid)     # keeps what it has; still occupies that glyph
            continue
        plan.append((tid, name, icon, want, holder.get(want) if shared else None))
        holder.setdefault(want, tid)
    return plan


def cmd_reicon(cfg, args):
    """Give existing topics a signature icon matched to their subject. Dry unless --apply.

    Registry-only: the signature is prepended to outgoing text, so nothing is sent to Telegram
    and no past message changes — only what future messages from these topics are stamped
    with."""
    plan = reicon_plan(read_registry())
    if not plan:
        print("Nothing to reicon.")
        return
    for tid, name, old, new, shared_with in plan:
        line = f"  topic {tid} {name}: {old} -> {new}"
        if shared_with:
            line += f"  (shared with topic {shared_with}: no other subject icon free)"
        print(line)
    if not args.apply:
        print(f"{len(plan)} topic(s) would change. Re-run with --apply to write them.")
        return

    def _write(reg):
        for tid, _name, _old, new, _shared in plan:
            if str(tid) in reg:
                reg[str(tid)]["icon"] = new
    update_registry(_write)
    print(f"Reicon applied to {len(plan)} topic(s).")


def send_typing(cfg, topic_id):
    try:
        api(cfg["bot_token"], "sendChatAction", {
            "chat_id": cfg["chat_id"], "message_thread_id": topic_id, "action": "typing",
        })
    except Exception:
        pass  # indicator is best-effort, never fail the actual command


def cmd_typing(cfg, args):
    topic_id = resolve_topic(args)
    deadline = time.time() + args.seconds
    while True:
        send_typing(cfg, topic_id)
        if time.time() + 4 >= deadline:
            break
        time.sleep(4)  # Telegram typing status lasts ~5s; refresh to sustain it


def custom_emoji_id(topic_id, info):
    """The registry's optional `icon_custom_emoji_id` for this topic, or None (#292).

    Digits only, as a string: the value is interpolated into an HTML attribute, and what
    Telegram does with a malformed entity is not established here. Anything else is dropped
    with a line on stderr, and the message goes out with the plain icon."""
    raw = info.get("icon_custom_emoji_id")
    if raw is None:
        return None
    if isinstance(raw, str) and _CUSTOM_EMOJI_ID.fullmatch(raw):
        return raw
    print(f"warning: topic {topic_id} icon_custom_emoji_id is not a digit string ({raw!r}); "
          "sending the plain icon", file=sys.stderr)
    return None


def send_text(cfg, topic_id, text):
    info = read_registry().get(str(topic_id), {})
    icon = info.get("icon")
    emoji_id = custom_emoji_id(topic_id, info)
    if icon:  # one bot account; the per-thread emoji (assigned at register) is the signature
        text = f"{icon} {text}"
    # Render agent Markdown as real Telegram formatting (HTML), splitting + plain-text
    # fallback handled in common.send_message (#89). The emoji prefix is safe literal text;
    # when the topic names a custom emoji, send_message wraps that leading icon in a
    # <tg-emoji> entity after the HTML conversion (#292).
    # The keyword is passed only when the topic asks for it, so the ordinary call is the
    # pre-#292 one.
    extra = {"icon_custom_emoji": (icon, emoji_id)} if icon and emoji_id else {}
    try:
        deliveries = send_message(cfg["bot_token"], cfg["chat_id"], text, topic_id, **extra)
    except PossiblyDelivered as e:
        journal_possibly_delivered(topic_id, "send", e, text)
        raise
    journal_deliveries(topic_id, "send", deliveries)


def send_files(cfg, topic_id, paths, caption=None, as_document=False):
    """Post files into the topic as the BOT, and journal each one (#210).

    The caption rides the FIRST file only — Telegram allows one per message, and repeating it
    on each would read as the same thing said three times. A caption over the Bot API's 1024
    cap is sent as its own message first instead of being truncated: losing the end of an
    explanation to fit a limit is worse than two messages.

    Every path is validated BEFORE anything is posted. A three-file call whose third path is
    a typo must not leave two files already in the topic.
    """
    info = read_registry().get(str(topic_id), {})
    icon = info.get("icon")
    emoji_id = custom_emoji_id(topic_id, info)
    for path in paths:
        file_send_plan(path, as_document)      # raises with a one-line reason; sends nothing

    if caption and len(caption) > CAPTION_LIMIT:
        send_text(cfg, topic_id, caption)
        caption = None
    elif caption and icon:
        caption = f"{icon} {caption}"

    for index, path in enumerate(paths):
        try:
            extra = ({"icon_custom_emoji": (icon, emoji_id)}
                     if index == 0 and caption and icon and emoji_id else {})
            delivery = send_file(cfg["bot_token"], cfg["chat_id"], path,
                                 caption=caption if index == 0 else None,
                                 thread_id=topic_id, as_document=as_document, **extra)
        except PossiblyDelivered as e:
            # The text path journals a lost ACK; a file must too, or an ambiguous upload
            # leaves no evidence at all and a human cannot tell whether to resend (#211
            # review). message_id is null because there is no result to take one from.
            append_outbox_file_record(topic_id, getattr(e, "possibly_delivered_file", None)
                                      or {"path": path}, possibly_delivered=True)
            raise
        append_outbox_file_record(topic_id, delivery)


def append_outbox_file_record(topic_id, delivery, possibly_delivered=False):
    """Journal a sent file. The document itself is not stored — its sha256 is, exactly as
    the text journal stores a hash and not the message.

    The hash comes from the delivery, which computed it over the bytes it actually uploaded.
    Re-reading the path here would let a file changed since the upload be described by the
    ledger as the thing that was sent."""
    try:
        record = {
            "ts": now_iso(),
            "message_id": (None if possibly_delivered
                           else int(delivery["result"]["message_id"])),
            "kind": "file",
            "path": delivery.get("path"),
            "size": delivery.get("size"),
            "content_sha256": delivery.get("content_sha256"),
        }
        if possibly_delivered:
            record["delivery"] = "possibly_delivered"
        path = state_path("topics", str(topic_id), "outbox.jsonl")
        with open(path, "a", encoding="utf-8") as outbox:
            outbox.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as e:
        # Same boundary as the text journal: a ledger failure must never look like a send
        # failure, because the file IS already in the topic.
        try:
            print(f"warning: outbox journal failed for topic {topic_id}: {e}", file=sys.stderr)
        except Exception:
            pass


def append_outbox_record(topic_id, kind, delivery, possibly_delivered=False):
    """Append one best-effort delivery record without affecting Telegram delivery."""
    try:
        record = {
            "ts": now_iso(),
            "message_id": (
                None if possibly_delivered else int(delivery["result"]["message_id"])
            ),
            "kind": kind,
        }
        if possibly_delivered:
            if "text" in delivery:
                record["content_sha256"] = hashlib.sha256(
                    delivery["text"].encode("utf-8")
                ).hexdigest()
                # Length only, never the body: the journal stays content-free (same
                # boundary as the hash), but a reader can cost deliveries by size.
                record["content_chars"] = len(delivery["text"])
            if "chunk_index" in delivery:
                record["chunk_index"] = int(delivery["chunk_index"])
            if "chunk_count" in delivery:
                record["chunk_count"] = int(delivery["chunk_count"])
            record["delivery"] = "possibly_delivered"
        else:
            record["content_sha256"] = hashlib.sha256(
                delivery["text"].encode("utf-8")
            ).hexdigest()
            record["content_chars"] = len(delivery["text"])
            record["chunk_index"] = int(delivery["chunk_index"])
            record["chunk_count"] = int(delivery["chunk_count"])
        line = json.dumps(record, ensure_ascii=False) + "\n"
        path = state_path("topics", str(topic_id), "outbox.jsonl")
        with open(path, "a", encoding="utf-8") as outbox:
            outbox.write(line)
    except Exception as e:
        try:
            print(f"warning: outbox journal failed for topic {topic_id}: {e}", file=sys.stderr)
        except Exception:
            pass


def journal_deliveries(topic_id, kind, deliveries):
    """Journal all completed chunks; tolerate legacy/test send_message stand-ins."""
    if not deliveries:
        return
    try:
        for delivery in deliveries:
            append_outbox_record(topic_id, kind, delivery)
    except Exception as e:
        # append_outbox_record contains its own boundary; this remains reachable when a
        # legacy/test send_message stand-in returns an iterable that raises during iteration.
        try:
            print(f"warning: outbox journal failed for topic {topic_id}: {e}", file=sys.stderr)
        except Exception:
            pass


def journal_possibly_delivered(topic_id, kind, error, _fallback_text):
    """Journal completed chunks plus the one whose Telegram ACK was lost."""
    journal_deliveries(topic_id, kind, getattr(error, "completed_sends", None))
    delivery = getattr(error, "possibly_delivered_send", None) or {}
    append_outbox_record(topic_id, kind, delivery, possibly_delivered=True)


UNREAD_EXIT = 3  # distinct from `recv --wait`'s timeout exit (2)


def unread_before_send(topic_id):
    """Records that arrived and have NOT been read yet, without advancing the cursor.

    A reply is formed before it is sent; anything that landed in between makes it stale.
    Peeking (not draining) keeps the caller's own `recv` the single place the cursor
    moves, so nothing is lost if the agent is interrupted between the check and the read.
    """
    records, _cursor = read_new(topic_id, load_cursor(topic_id))
    return records


def is_feed_topic(topic_id):
    """Feed topics are outbound-only: nobody drains their inbox, so a stray inbound
    record would block every later post forever. They are exempt from the unread guard."""
    return bool(read_registry().get(str(topic_id), {}).get("feed"))


def cmd_send(cfg, args):
    topic_id = resolve_topic(args)
    # Before the unread guard below, and before stdin is consumed: a cross-topic sender must
    # never be shown the target's records nor told to run `recv --topic <target>`.
    require_own_topic(topic_id, "send")
    files = getattr(args, "files", None) or []
    if args.text is None and not files:
        raise SystemExit("send: give message text, - for stdin, or --file PATH")
    # Validate paths BEFORE stdin is read. A typo used to return NOT SENT having already
    # consumed the caller's only copy of a piped caption, so the retry had nothing to send
    # (#211 review). Nothing here posts, so a bad path still costs nothing.
    try:
        for path in files:
            file_send_plan(path, args.as_document)
    except RuntimeError as e:
        raise SystemExit(f"NOT SENT — {e}")
    text = "" if args.text is None else (args.text if args.text != "-" else sys.stdin.read())
    if not args.force and not is_feed_topic(topic_id):
        pending = unread_before_send(topic_id)
        if pending:
            # Refuse rather than post a reply written before these arrived (#135). The
            # cursor is untouched, so the caller's own `recv` still delivers them.
            print(f"NOT SENT — {len(pending)} unread message(s) arrived in topic "
                  f"{topic_id} while you were working:")
            print_records(pending, getattr(args, "json", False))
            # Spell out the recovery: the preview above did NOT consume anything, so a
            # blind retry hits this same refusal forever.
            print(f"This was a PREVIEW — it did not mark them read. Run "
                  f"`tg-bridge recv --topic {topic_id}` to consume them, then compose what "
                  f"you send with ALL of it taken into account — how many messages that "
                  f"takes is your judgement. `--force` sends anyway (progress pings only).")
            sys.exit(UNREAD_EXIT)
    try:
        if files:
            send_files(cfg, topic_id, files, caption=text or None,
                       as_document=args.as_document)
        else:
            send_text(cfg, topic_id, text)
    except PossiblyDelivered as e:
        # A read-timeout after connect almost always means Telegram already posted the
        # message. Resending is exactly what turned one reply into several — so do NOT
        # retry, and exit 0 so an autonomous session won't loop and duplicate.
        print(f"⚠️ {e}")
        print("NOT resent (a lost ACK usually means it already posted). "
              "Do not retry blindly; resend once only if it truly did not appear.")
        return
    except RuntimeError as e:
        raise SystemExit(f"NOT SENT — {e}")
    print(f"sent {len(files)} file(s)" if files else "sent")


def _check_pane_live(topic_id, pane):
    try:
        live = pane_alive(pane)
    except subprocess.TimeoutExpired:
        raise SystemExit(f"topic {topic_id} pane {pane} liveness check timed out")
    except Exception as exc:
        raise SystemExit(f"topic {topic_id} pane {pane} liveness check failed: {exc}")
    if not live:
        raise SystemExit(f"topic {topic_id} pane {pane} is not live")


def require_live_dialog(topic_id):
    """Return the local registry entry for a live, pane-bound dialog topic."""
    topic_id = int(topic_id)
    info = read_registry().get(str(topic_id))
    if not isinstance(info, dict):
        raise SystemExit(f"topic {topic_id} is not in the local registry")
    if info.get("feed"):
        raise SystemExit(f"topic {topic_id} is a feed, not a dialog")
    if info.get("ended"):
        raise SystemExit(f"topic {topic_id} ended at {info['ended']}")
    pane = info.get("pane")
    if not pane:
        raise SystemExit(f"topic {topic_id} has no bound pane")
    _check_pane_live(topic_id, pane)
    return info


def _pane_dialog_topic(pane):
    """Registry half of pane resolution: the one non-feed, non-ended topic bound to ``pane``.

    Split out of ``current_topic_metadata`` so ``caller_topic`` can reuse the lookup while
    turning its failures into "unknown caller" instead of exiting; the metadata command
    still runs this plus the liveness probe, so its contract is unchanged.
    """
    bound = [
        (topic_id, info)
        for topic_id, info in read_registry().items()
        if isinstance(info, dict) and info.get("pane") == pane
    ]
    eligible = [
        (topic_id, info) for topic_id, info in bound
        if not info.get("feed") and not info.get("ended")
    ]
    if len(eligible) > 1:
        ids = ", ".join(topic_id for topic_id, _info in eligible)
        raise SystemExit(f"ambiguous live topics for TMUX_PANE {pane}: {ids}")
    if not eligible:
        if any(info.get("feed") for _topic_id, info in bound):
            raise SystemExit(f"TMUX_PANE {pane} is bound only to a feed topic")
        if any(info.get("ended") for _topic_id, info in bound):
            raise SystemExit(f"TMUX_PANE {pane} is bound only to an ended topic")
        raise SystemExit(f"no topic in the local registry is bound to TMUX_PANE {pane}")
    return eligible[0]


def current_topic_metadata(pane=None):
    """Resolve exactly one live non-feed registry topic bound to ``pane``/TMUX_PANE."""
    pane = pane or os.environ.get("TMUX_PANE")
    if not pane:
        raise SystemExit("TMUX_PANE is not set")
    topic_id, info = _pane_dialog_topic(pane)
    _check_pane_live(topic_id, pane)
    return {
        "topic_id": int(topic_id),
        "name": info.get("name"),
        "pane": pane,
        "engine": info.get("engine"),
        "cwd": info.get("cwd"),
    }


def caller_topic():
    """The dialog topic this process is speaking for, or None when it can't be resolved.

    The pane is probed for liveness because a set TMUX_PANE does NOT prove the process is
    running in that pane: a detached script keeps the variable it inherited, and a pane can
    die while its registry entry still looks live (the daemon's lifecycle sweep stamps
    `ended` only on its next poll). Without the probe such a process would speak as a session
    that no longer exists — writing a durable, the owner-visible peer attribution in its name —
    and would be blocked from its own work by the ownership guard.

    Every failure resolves to "unknown caller": no TMUX_PANE, an unregistered pane, an
    ambiguous binding, feed- or ended-only, a dead pane, and a tmux timeout or error alike.
    That keeps both callers of this function at their pre-existing behaviour — The ownership
    guard is skipped and `notify` requires `--sender` — so a hung tmux server degrades peer
    identity instead of breaking `send` for every session.
    """
    pane = os.environ.get("TMUX_PANE")
    if not pane:
        return None
    try:
        topic_id, info = _pane_dialog_topic(pane)
        _check_pane_live(topic_id, pane)   # raises SystemExit on dead pane, timeout, error
    except SystemExit:
        return None
    return {"topic_id": int(topic_id), "name": info.get("name"), "icon": info.get("icon")}


def peer_label(caller):
    """How a peer session signs itself: derived from the registry, never caller-supplied."""
    name = caller.get("name")
    return f"{name} (topic {caller['topic_id']})" if name else f"topic {caller['topic_id']}"


OWNERSHIP_EXIT = 4  # foreign live dialog; 2 = recv timeout, 3 = unread backlog

# What each command would actually have done to the other session. The refusal is read by an
# agent that just made this mistake, so it must not overstate: `send` posts without
# consuming, `recv` consumes without posting, and only `ask` does both.
FOREIGN_TOPIC_CONSEQUENCE = {
    "send": "would post into that session's Telegram thread, which only the owner reads — the "
            "agent there reads its inbox, so it would never see this",
    "ask": "would post into that session's Telegram thread, which that agent never reads, "
           "and would drain the unread messages it is waiting for",
    "recv": "would consume the unread messages that session is waiting for, so they would "
            "never reach it",
}


def require_own_topic(topic_id, command):
    """Refuse `send`/`ask`/`recv` aimed at another live session's dialog topic (exit 4).

    None of them reaches the target agent's inbox, which is the only thing it reads: a
    cross-topic `send`/`ask` posts under the target's own icon (so the owner reads it as that
    session speaking) while the agent behind it sees nothing, and `ask`/`recv` eat the unread
    backlog and cursor that agent depends on. `notify` is the path that exists.

    In `cmd_send` this must fire BEFORE the unread guard: that refusal's recovery text names
    `recv --topic <target>`, i.e. it would coach a cross-poster into exactly the cursor theft.

    A tripwire, not a security boundary — host access already types into any pane. Exempt by
    construction: the caller's own topic, feed/ended/unregistered targets (nobody is waiting
    on them), and an unresolvable caller (cron, an external orchestrator, an admin's `env -u TMUX_PANE`).
    """
    caller = caller_topic()
    if caller is None:
        return
    topic_id = int(topic_id)
    if caller["topic_id"] == topic_id:
        return
    info = read_registry().get(str(topic_id))
    if not isinstance(info, dict) or info.get("feed") or info.get("ended"):
        return
    name = info.get("name") or "unnamed"
    print(f"REFUSED — topic {topic_id} ({name}) is another live session's topic; "
          f"you are topic {caller['topic_id']}.")
    print(f"`{command} --topic {topic_id}` {FOREIGN_TOPIC_CONSEQUENCE[command]}.")
    print(f"To message that session, enqueue into its inbox instead — body on stdin:")
    print(f"  tg-bridge notify --topic {topic_id} "
          f"--idempotency-key \"{caller['topic_id']}->{topic_id}:<slug>\" < body.txt")
    sys.exit(OWNERSHIP_EXIT)


def outbound_echo(cfg, caller, target_id, target_name, text):
    """Show the sender's own topic what it just sent. Telegram only — never an inbox record.

    The target's mirror alone leaves each thread holding only the half it received: the
    first real peer exchange (topics 6258 ↔ 8713) put four questions in one topic and their
    four answers in the other, so neither read as a conversation. This posts the outbound
    half where the sender's own thread already carries its icon.

    Deliberately NOT a record: appending to the sender's inbox would hand it its own message
    back and wake it for it. Deliberately after the target mirror, and never raising: a
    cosmetic echo must not decide whether a delivered message reports success.

    Returns the outcome for the result JSON, alongside `wake` and `telegram`:
    "posted"/"ambiguous"/"failed", or "skipped" when there is nothing to echo (automation
    with no topic of its own, or a session notifying itself).
    """
    if caller is None or caller["topic_id"] == int(target_id):
        return "skipped"
    # An unnamed target degrades to the bare id rather than "topic N (topic N)".
    label = f"{target_name} (topic {target_id})" if target_name else f"topic {target_id}"
    icon = caller.get("icon")
    line = f"→ {label}: {text}"
    try:
        send_message(cfg["bot_token"], cfg["chat_id"],
                     f"{icon} {line}" if icon else line, caller["topic_id"])
        return "posted"
    except PossiblyDelivered:
        return "ambiguous"
    except Exception:
        return "failed"


def cmd_current_topic(_cfg, _args):
    print(json.dumps(current_topic_metadata(), ensure_ascii=False, sort_keys=True))


def notify_topic(cfg, topic_id, sender, idempotency_key, text, owner_only=False):
    """Enqueue one local event into a topic's inbox, wake its pane, then mirror to Telegram.

    An ended parked topic has no live pane to nudge. Its appended inbox record instead asks
    the lifecycle sweep to revive it; every other target keeps `require_live_dialog`'s contract.

    `owner_only`: the event is for the OWNER reading the topic, not for the agent — a usage
    alert, a daily pulse. It is posted to Telegram with the same attribution and the same
    idempotency, but recorded in `notices.jsonl` instead of the inbox, so `recv` never returns
    it and the pane is not woken (usage alerts were waking an idle seat for nothing).

    Two sender paths, decided by whether the caller resolves to a dialog topic of its own:

    * a session pane — identity is DERIVED (`kind: "peer"`, `from` and `sender_topic_id`
      from the registry) and `--sender` is ignored, so a session cannot label its traffic
      "the owner" or borrow the `notification` kind that means trusted local automation;
    * anything else (cron, an external orchestrator, headless) — unchanged `notification` record with the
      required free-text `--sender`.
    """
    idempotency_key = (idempotency_key or "").strip()
    if not idempotency_key:
        raise SystemExit("--idempotency-key must not be empty")
    if not text.strip():
        raise SystemExit("notification stdin must not be empty")

    topic_id = int(topic_id)
    caller = caller_topic()
    if caller is not None:
        kind, from_label = "peer", peer_label(caller)
    else:
        kind, from_label = "notification", (sender or "").strip()
        if not from_label:
            raise SystemExit(
                "--sender must not be empty (required for a caller with no dialog topic "
                "of its own; a session pane derives its identity instead)"
            )
    target = read_registry().get(str(topic_id))
    parked = (isinstance(target, dict) and bool(target.get("ended"))
              and bool(target.get("parked")) and not target.get("feed"))
    info = target if parked else require_live_dialog(topic_id)
    record = {
        "ts": now_iso(),
        "from": from_label,
        "kind": kind,
        "text": text,
        "provenance": "local-notify",
        "idempotency_key": idempotency_key,
    }
    if caller is not None:
        record["sender_topic_id"] = caller["topic_id"]
    ledger = "notices.jsonl" if owner_only else "inbox.jsonl"
    inbox = state_path("topics", str(topic_id), ledger)
    appended, wake_claim = append_jsonl_once(inbox, record)
    if not appended:
        return {"status": "duplicate", "topic_id": topic_id}

    if owner_only:
        wake = "not-requested"
    elif parked:
        wake = "parked-revive-requested"
    elif wake_claim is not None:
        nudge_result = maybe_nudge(topic_id, info["pane"], wake_claim)
        if nudge_result is True:
            wake = "nudged"
        elif nudge_result is None:
            wake = "already-unread"
        else:
            wake = "failed"
    else:
        wake = "already-unread"

    # The mirror deliberately does NOT go through send_text: that prefixes the TARGET topic's
    # icon, and the icon is the sender's signature — a peer message would appear in the
    # target's own thread signed as the target. Compose the attribution here and call
    # send_message directly, keeping the same HTML/splitting/plain-text-fallback path.
    if caller is not None:
        # The icon IS the signature (owner request, 2026-09-03: no "Name (topic N):" in the body);
        # the name+topic label stays in the inbox record and in the sender's own echo.
        icon = caller.get("icon")
        mirror = f"{icon} {text}" if icon else f"{from_label}: {text}"
    else:
        mirror = f"{from_label}: {text}"  # un-iconed: no session identity to sign with
    try:
        deliveries = send_message(cfg["bot_token"], cfg["chat_id"], mirror, topic_id)
    except PossiblyDelivered as e:
        telegram = "ambiguous"
        journal_possibly_delivered(topic_id, "notify", e, mirror)
    except Exception:
        telegram = "failed"
    else:
        telegram = "posted"
        journal_deliveries(topic_id, "notify", deliveries)

    echo = outbound_echo(cfg, caller, topic_id, info.get("name"), text)
    return {
        # Not "delivered": the record is durably enqueued and the pane wake is best effort.
        # Nothing here proves the target agent read it (§4.1 M6 of the design). An owner-only
        # notice was never enqueued for the agent at all, and says so.
        "status": "posted" if owner_only else "enqueued",
        "topic_id": topic_id,
        "wake": wake,
        "telegram": telegram,
        "echo": echo,
    }


def cmd_notify(cfg, args):
    text = sys.stdin.read()
    # The flag is passed only when set, so the call shape every existing caller and test
    # stub knows stays the same for an ordinary notify.
    extra = {"owner_only": True} if getattr(args, "owner_only", False) else {}
    result = notify_topic(
        cfg, args.topic, args.sender, args.idempotency_key, text, **extra,
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    if result.get("telegram") in ("ambiguous", "failed"):
        print(
            f"warning: Telegram delivery {result['telegram']}; local inbox delivery is durable",
            file=sys.stderr,
        )


def read_new(topic_id, cursor):
    """Return (records, new_cursor) for inbox lines beyond cursor (a line count)."""
    inbox = state_path("topics", str(topic_id), "inbox.jsonl")
    if not os.path.exists(inbox):
        return [], cursor
    with open(inbox) as f:
        lines = f.read().splitlines()
    return [json.loads(line) for line in lines[cursor:]], len(lines)


def load_cursor(topic_id):
    path = state_path("topics", str(topic_id), "cursor")
    if os.path.exists(path):
        with open(path) as f:
            return int(f.read().strip() or 0)
    return 0


def save_cursor(topic_id, cursor):
    inbox = state_path("topics", str(topic_id), "inbox.jsonl")
    return commit_cursor_if_inbox_current(inbox, cursor)


def print_records(records, as_json):
    for r in records:
        if as_json:
            print(json.dumps(r, ensure_ascii=False))
        else:
            tag = "" if r["kind"] == "text" else f" ({r['kind']})"
            print(f"[{r['ts']}] {r['from']}{tag}: {r['text']}")


def drain_and_commit(topic_id, records, cursor, as_json):
    """Print/flush records, then atomically commit only a stable inbox cursor."""
    while True:
        if records:
            print_records(records, as_json)
            sys.stdout.flush()  # emitted output is durable before its cursor can advance
        if save_cursor(topic_id, cursor):
            return
        records, cursor = read_new(topic_id, cursor)


def _fmt_duration(seconds):
    """A duration a reader can check against a wall clock: "24h 0m 0s", "15m 0s", "45s"."""
    seconds = int(round(seconds))
    h, rem = divmod(seconds, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h}h {m}m {sec}s"
    if m:
        return f"{m}m {sec}s"
    return f"{sec}s"


def timeout_notice(armed_at, waited):
    """What a session sees when a wait elapses with nothing to show for it (#182).

    The bare "(no reply within timeout)" carried no duration, and a session has no wall
    clock between turns — the only duration signal it gets is how far apart two records sit
    in its own transcript. For a long wait that signal is exactly backwards: the longer the
    wait really was, the fewer events happened during it, so the arm and the timeout end up
    ADJACENT and it reads as an instant failure. A real session concluded its 24h listener
    was "returning exit 2 almost immediately every time", stopped re-arming on that basis,
    and went dark for a day. Stating the elapsed time makes that conclusion refutable from
    the text itself."""
    return (f"(no reply within timeout — waited {_fmt_duration(waited)}, "
            f"armed {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(armed_at))})")


def wait_for_messages(topic_id, timeout):
    """Returns (records, cursor, armed_at, waited) — the last two so the caller can report
    what the wait actually cost, not just that it ended."""
    cursor = load_cursor(topic_id)
    armed_at = time.time()
    deadline = armed_at + timeout
    while time.time() < deadline:
        records, new_cursor = read_new(topic_id, cursor)
        if records:
            return records, new_cursor, armed_at, time.time() - armed_at
        time.sleep(1)
    return [], cursor, armed_at, time.time() - armed_at


def cmd_recv(cfg, args):
    topic_id = resolve_topic(args)
    if not args.peek:
        # Only the committing path is guarded: `--peek` moves no cursor, so it stays the
        # sanctioned read-only look at another topic (including ended ones, for forensics).
        require_own_topic(topic_id, "recv")
    armed_at = waited = None
    if args.wait:
        records, new_cursor, armed_at, waited = wait_for_messages(topic_id, args.wait)
    else:
        records, new_cursor = read_new(topic_id, load_cursor(topic_id))
    if records:
        if not args.peek:
            drain_and_commit(topic_id, records, new_cursor, args.json)
            send_typing(cfg, topic_id)  # show the owner the messages were read and work resumed
        else:
            print_records(records, args.json)
            sys.stdout.flush()
    elif args.wait:
        print(timeout_notice(armed_at, waited))
        sys.exit(2)


def cmd_ask(cfg, args):
    topic_id = resolve_topic(args)
    # Before the drain below: an `ask` at a foreign topic used to eat that session's whole
    # unread backlog, post as it, and then swallow its next inbound message.
    require_own_topic(topic_id, "ask")
    # Print + durably flush anything ALREADY unread BEFORE advancing past it, so `ask`
    # never silently drops a message that arrived before it (#105 — same silent-loss class
    # as the recv drain). Then advance the cursor and wait for the actual reply.
    pending, cursor = read_new(topic_id, load_cursor(topic_id))
    drain_and_commit(topic_id, pending, cursor, args.json)
    try:
        send_text(cfg, topic_id, args.text)
    except PossiblyDelivered as e:
        # Likely already posted; wait for the reply rather than resending (a resend
        # would duplicate the prompt).
        print(f"⚠️ {e}; waiting for a reply anyway (do not resend).", file=sys.stderr)
    records, new_cursor, armed_at, waited = wait_for_messages(topic_id, args.timeout)
    if not records:
        print(timeout_notice(armed_at, waited))
        sys.exit(2)
    drain_and_commit(topic_id, records, new_cursor, args.json)
    send_typing(cfg, topic_id)


def cmd_status(cfg, args):
    result = subprocess.run(
        ["systemctl", "--user", "is-active", "agent-telegram-bridge.service"],
        capture_output=True, text=True,
    )
    print(f"daemon: {result.stdout.strip() or result.stderr.strip()}")
    registry = read_registry()
    for topic_id, info in sorted(registry.items(), key=lambda kv: int(kv[0])):
        unread = len(read_new(topic_id, load_cursor(int(topic_id)))[0])
        ended = f", ended {info['ended']}" if info.get("ended") else ""
        print(f"topic {topic_id}: {info['name']} (created {info['created']}, unread {unread}{ended})")


def build_parser():
    parser = argparse.ArgumentParser(prog="tg-bridge", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("register", help="create a forum topic for this session")
    p.add_argument("--name", help="topic name (default: timestamp)")
    p.add_argument("--bind", action="store_true", help=f"write {CWD_BINDING_FILE} in cwd")
    p.add_argument("--feed", action="store_true",
                   help="event feed topic: no pane binding, warnings, nudges, or lifecycle notices")

    p = sub.add_parser("send", help="send text or files to the topic")
    p.add_argument("text", nargs="?", help="message text, or - for stdin; "
                                           "with --file it becomes the caption")
    p.add_argument("--topic", help="topic id")
    p.add_argument("--file", action="append", dest="files", metavar="PATH",
                   help="absolute path to send as a file (repeatable)")
    p.add_argument("--as-document", action="store_true",
                   help="send images as documents instead of inline photos")
    p.add_argument("--force", action="store_true",
                   help="send even if unread messages are waiting (progress pings)")

    p = sub.add_parser("recv", help="print new inbox messages")
    p.add_argument("--topic", help="topic id")
    p.add_argument("--wait", type=int, help="block up to N seconds for a message")
    p.add_argument("--peek", action="store_true", help="do not advance the read cursor")
    p.add_argument("--json", action="store_true", help="print raw JSONL records")

    p = sub.add_parser("ask", help="send text and wait for a reply")
    p.add_argument("text", help="message text")
    p.add_argument("--topic", help="topic id")
    p.add_argument("--timeout", type=int, default=300, help="seconds to wait (default 300)")
    p.add_argument("--json", action="store_true", help="print raw JSONL records")

    p = sub.add_parser("typing", help="show a typing indicator in the topic while working")
    p.add_argument("--topic", help="topic id")
    p.add_argument("--seconds", type=int, default=5, help="how long to sustain it (default 5)")

    sub.add_parser(
        "current-topic",
        help="print the one live non-feed topic bound to TMUX_PANE as JSON",
    )

    p = sub.add_parser(
        "notify",
        help="read an event from stdin, enqueue it once into a topic's inbox, wake, and mirror "
             "(--owner-only: mirror for the owner, no inbox record, no wake)",
    )
    p.add_argument("--topic", required=True, type=int, help="target dialog topic id")
    p.add_argument("--sender",
                   help="sender label stored in the inbox; required for automation, IGNORED "
                        "for a session pane (which derives its identity from its own topic)")
    p.add_argument("--idempotency-key", required=True,
                   help="durable retry key, unique per logical event")
    p.add_argument("--owner-only", action="store_true",
                   help="post for the owner only: same attribution and idempotency, but the "
                        "event is recorded in notices.jsonl, never in the agent's inbox, and "
                        "the pane is not woken (usage alerts, pulses)")

    sub.add_parser("status", help="daemon health and registered topics")

    p = sub.add_parser(
        "retheme",
        help="show which live topics would get a theme icon; --apply to write them",
    )
    p.add_argument("--apply", action="store_true",
                   help="actually call editForumTopic (without this, nothing is written)")

    p = sub.add_parser(
        "reicon",
        help="show which live topics would get a subject-matched signature icon; --apply to "
             "write them",
    )
    p.add_argument("--apply", action="store_true",
                   help="actually write the registry (without this, nothing is written)")
    return parser


def main():
    secure_process_umask()
    if os.environ.get("TMUX_PANE"):
        try:
            codex_ctx.record_current_pane_rollout()
        except Exception:
            pass
    args = build_parser().parse_args()
    if args.command == "current-topic":
        cmd_current_topic(None, args)
        return
    cfg = load_config()
    {"register": cmd_register, "send": cmd_send, "recv": cmd_recv,
     "ask": cmd_ask, "status": cmd_status, "typing": cmd_typing,
     "notify": cmd_notify, "retheme": cmd_retheme,
     "reicon": cmd_reicon}[args.command](cfg, args)


if __name__ == "__main__":
    main()
