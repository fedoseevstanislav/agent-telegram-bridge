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

Topic resolution order: --topic flag, TG_BRIDGE_TOPIC env var, ./.tg-bridge-topic file.
"""

import argparse
import hashlib
import json
import os
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
from bridge.daemon import maybe_nudge, pane_alive

CWD_BINDING_FILE = ".tg-bridge-topic"

# Visually distinct session icons — assigned at register time, shown in message headers
ICONS = ("🦊", "🐙", "🦉", "🐳", "⚡", "🌵", "🎯", "🧩", "🛰️", "🌶️",
         "🪐", "🦜", "🍄", "🗿", "🌊", "🔥")


def pick_icon(registry, topic_id):
    taken = {info.get("icon") for info in registry.values() if not info.get("ended")}
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
    topic = api(cfg["bot_token"], "createForumTopic", {"chat_id": cfg["chat_id"], "name": name})
    topic_id = topic["message_thread_id"]
    entry = {"name": name, "created": now_iso(), "cwd": os.getcwd()}
    if args.feed:
        # event feed, not a dialog: no pane binding -> no warnings, nudges, or lifecycle notices
        entry["feed"] = True
        entry["icon"] = "📡"
    # icon needs the existing registry to avoid collisions; compute + write atomically so a
    # concurrent daemon snapshot/revive can't clobber this new registration.
    def _add(reg):
        if not args.feed:
            entry["icon"] = pick_icon(reg, topic_id)
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


def send_text(cfg, topic_id, text):
    info = read_registry().get(str(topic_id), {})
    icon = info.get("icon")
    if icon:  # one bot account; the per-thread emoji (assigned at register) is the signature
        text = f"{icon} {text}"
    # Render agent Markdown as real Telegram formatting (HTML), splitting + plain-text
    # fallback handled in common.send_message (#89). The emoji prefix is safe literal text.
    try:
        deliveries = send_message(cfg["bot_token"], cfg["chat_id"], text, topic_id)
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
    for path in paths:
        file_send_plan(path, as_document)      # raises with a one-line reason; sends nothing

    if caption and len(caption) > CAPTION_LIMIT:
        send_text(cfg, topic_id, caption)
        caption = None
    elif caption and icon:
        caption = f"{icon} {caption}"

    for index, path in enumerate(paths):
        try:
            delivery = send_file(cfg["bot_token"], cfg["chat_id"], path,
                                 caption=caption if index == 0 else None,
                                 thread_id=topic_id, as_document=as_document)
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
            if "chunk_index" in delivery:
                record["chunk_index"] = int(delivery["chunk_index"])
            if "chunk_count" in delivery:
                record["chunk_count"] = int(delivery["chunk_count"])
            record["delivery"] = "possibly_delivered"
        else:
            record["content_sha256"] = hashlib.sha256(
                delivery["text"].encode("utf-8")
            ).hexdigest()
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


def notify_topic(cfg, topic_id, sender, idempotency_key, text):
    """Enqueue one local event into a topic's inbox, wake its pane, then mirror to Telegram.

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
    info = require_live_dialog(topic_id)
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
    inbox = state_path("topics", str(topic_id), "inbox.jsonl")
    appended, wake_claim = append_jsonl_once(inbox, record)
    if not appended:
        return {"status": "duplicate", "topic_id": topic_id}

    if wake_claim is not None:
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
        icon = caller.get("icon")
        mirror = f"{icon} {from_label}: {text}" if icon else f"{from_label}: {text}"
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
        # Nothing here proves the target agent read it (§4.1 M6 of the design).
        "status": "enqueued",
        "topic_id": topic_id,
        "wake": wake,
        "telegram": telegram,
        "echo": echo,
    }


def cmd_notify(cfg, args):
    text = sys.stdin.read()
    result = notify_topic(
        cfg, args.topic, args.sender, args.idempotency_key, text,
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
        help="read an event from stdin, enqueue it once into a topic's inbox, wake, and mirror",
    )
    p.add_argument("--topic", required=True, type=int, help="target dialog topic id")
    p.add_argument("--sender",
                   help="sender label stored in the inbox; required for automation, IGNORED "
                        "for a session pane (which derives its identity from its own topic)")
    p.add_argument("--idempotency-key", required=True,
                   help="durable retry key, unique per logical event")

    sub.add_parser("status", help="daemon health and registered topics")
    return parser


def main():
    secure_process_umask()
    args = build_parser().parse_args()
    if args.command == "current-topic":
        cmd_current_topic(None, args)
        return
    cfg = load_config()
    {"register": cmd_register, "send": cmd_send, "recv": cmd_recv,
     "ask": cmd_ask, "status": cmd_status, "typing": cmd_typing,
     "notify": cmd_notify}[args.command](cfg, args)


if __name__ == "__main__":
    main()
