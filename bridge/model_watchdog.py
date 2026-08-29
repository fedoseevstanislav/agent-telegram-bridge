"""Alert a registered Claude session's Telegram topic when its model drifts.

This is a standalone systemd-timer target. It reads the bridge registry and
Claude transcript tails, but it does not need the bridge daemon to be running.
"""

import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bridge.common import (  # noqa: E402
    PossiblyDelivered,
    api,
    load_config,
    read_registry,
    secure_process_umask,
    state_path,
)
from bridge import codex_ctx  # noqa: E402
from bridge.daemon import (  # noqa: E402
    engine_of_pane,
    pane_alive,
    pane_pid,
    read_context,
)
# Shared with the daemon's carry-forward transcript reads (#157). They live in a leaf module
# because model_watchdog imports FROM daemon, so daemon importing this file back would be a
# cycle. Re-exported here so this module's own callers and tests keep their names.
from bridge.transcript import (  # noqa: E402
    PROJECTS_DIR,
    TAIL_BYTES,
    read_tail_records,
    transcript_path,
)

STATE_NAME = "model-watchdog.json"
# Codex rollouts are an order of magnitude larger than Claude transcripts (21 MB on a
# day-long session) and `thread_settings_applied` only fires at turn starts — measured
# gaps between two settings events reach 7.5 MB. Steady state therefore reads only the
# bytes appended since the last sweep; these bounds apply to the one-time baseline read.
CODEX_BASELINE_TAIL_BYTES = 2 * 1024 * 1024
# A model switch lands as two events (model, then effort) milliseconds apart; don't act
# on a settings event younger than this, so a sweep can't catch it half-applied.
CODEX_SETTLE_SECONDS = 5
# Events closer together than this belong to the same settings write, so if the last of
# them is still fresh the whole burst waits for the next sweep.
CODEX_BURST_SECONDS = 1
FALLBACK_QUOTE_CHARS = 1500
TZ_OFFSET = int(os.environ.get("TG_BRIDGE_TZ_OFFSET", "3"))


def log(message):
    print(f"[model-watchdog] {message}", flush=True)


def load_state():
    try:
        with open(state_path(STATE_NAME), encoding="utf-8") as f:
            state = json.load(f)
        if not isinstance(state, dict):
            return {}
        return {sid: value for sid, value in state.items() if isinstance(value, dict)}
    except (OSError, ValueError):
        return {}


def save_state(state):
    path = state_path(STATE_NAME)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False, sort_keys=True)
    os.replace(tmp, path)




def scan_transcript(path):
    fallbacks = []
    last_model = None
    last_model_ts = None
    for record in read_tail_records(path):
        if (record.get("type") == "system"
                and record.get("subtype") == "model_refusal_fallback"
                and isinstance(record.get("timestamp"), str)
                and record["timestamp"]):
            fallbacks.append(record)

        if record.get("type") != "assistant":
            continue
        message = record.get("message")
        model = message.get("model") if isinstance(message, dict) else None
        if isinstance(model, str) and model and model != "<synthetic>":
            last_model = model
            last_model_ts = record.get("timestamp")

    return {
        "fallbacks": fallbacks,
        "last_model": last_model,
        "last_model_ts": last_model_ts,
    }


def user_time(timestamp=None):
    try:
        parsed = datetime.fromisoformat((timestamp or "").replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        parsed = datetime.now(timezone.utc)
    local = parsed.astimezone(timezone(timedelta(hours=TZ_OFFSET)))
    return f"{local:%Y-%m-%d %H:%M} UTC{TZ_OFFSET:+d}"


def fallback_alert(event, current_model):
    content = str(event.get("content") or "(no fallback detail)")
    if len(content) > FALLBACK_QUOTE_CHARS:
        content = content[:FALLBACK_QUOTE_CHARS] + "…"
    model = event.get("fallbackModel") or current_model or "unknown model"
    return (
        f"🚨 Model fallback detected at {user_time(event.get('timestamp'))}.\n"
        f"“{content}”\n"
        f"session now runs {model}; use /model in the pane to revert."
    )


def transition_alert(previous_model, current_model, timestamp):
    return (
        f"⚠️ Model identity changed at {user_time(timestamp)}:\n"
        f"{previous_model} → {current_model}\n"
        "Use /model in the pane to revert if this change was not intentional."
    )


def send_topic(cfg, topic_id, text):
    api(cfg["bot_token"], "sendMessage", {
        "chat_id": cfg["chat_id"],
        "message_thread_id": int(topic_id),
        "text": text,
    })


def _send_without_ambiguous_retry(cfg, topic_id, text):
    try:
        send_topic(cfg, topic_id, text)
    except PossiblyDelivered as exc:
        # Telegram may already have posted this non-idempotent send. Watermark it so the
        # next timer tick cannot turn a lost acknowledgement into a duplicate alert.
        log(f"topic {topic_id}: {exc}; advancing watermark without retry")


def check_transcript(cfg, topic_id, session_id, path, state):
    scan = scan_transcript(path)
    fallbacks = scan["fallbacks"]
    current_model = scan["last_model"]

    if session_id not in state:
        state[session_id] = {
            "last_fallback_ts": max(
                (event["timestamp"] for event in fallbacks),
                default=None,
            ),
            "last_model": current_model,
        }
        return

    session_state = state[session_id]
    session_state.setdefault("last_fallback_ts", None)
    session_state.setdefault("last_model", None)
    fallback_watermark = session_state["last_fallback_ts"]
    new_fallbacks = [
        event for event in fallbacks
        if fallback_watermark is None or event["timestamp"] > fallback_watermark
    ]
    new_fallbacks.sort(key=lambda event: event["timestamp"])

    for event in new_fallbacks:
        _send_without_ambiguous_retry(
            cfg,
            topic_id,
            fallback_alert(event, current_model),
        )
        session_state["last_fallback_ts"] = event["timestamp"]
        save_state(state)

    previous_model = session_state["last_model"]
    if current_model and previous_model and current_model != previous_model:
        _send_without_ambiguous_retry(
            cfg,
            topic_id,
            transition_alert(previous_model, current_model, scan["last_model_ts"]),
        )
        session_state["last_model"] = current_model
        save_state(state)
    elif current_model and not previous_model:
        session_state["last_model"] = current_model


def _codex_settings(record):
    """(model, effort) from a codex `thread_settings_applied` event, or None.

    The full record shape is enforced — `type == "event_msg"` AND the payload type —
    so an unrelated record that merely nests a similar payload cannot move state.
    """
    if record.get("type") != "event_msg":
        return None
    payload = record.get("payload")
    if not isinstance(payload, dict) or payload.get("type") != "thread_settings_applied":
        return None
    settings = payload.get("thread_settings")
    if not isinstance(settings, dict):
        return None
    model = settings.get("model")
    if not isinstance(model, str) or not model:
        return None
    effort = settings.get("reasoning_effort")
    return model, (effort if isinstance(effort, str) and effort else None)


def codex_label(model, effort):
    return f"{model} {effort}" if model and effort else (model or "unknown model")


def read_settings_events(path, start, max_bytes=None):
    """Settings events in ``path`` from byte ``start`` on.

    Returns ``(events, consumed)`` where each event is
    ``{"start", "end", "ts", "label"}`` in file-byte terms and ``consumed`` is the offset
    of the end of the last COMPLETE line — a rollout is appended to live, so the final
    line may be half-written and must not be consumed. Returns ``(None, None)`` when the
    file is shorter than ``start``, i.e. it was truncated or replaced under us.

    Reading from a stored offset is what keeps this affordable: a full rollout reaches
    21 MB and the gap between two settings events reaches 7.5 MB, so neither a full parse
    nor a fixed tail is safe AND cheap. Only baselining pays for a tail read.
    """
    clamped = False
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        if start > size:
            return None, None
        if max_bytes is not None and size - start > max_bytes:
            start, clamped = size - max_bytes, True
            f.seek(start - 1)
            # A clamp landing exactly ON a line boundary leaves a COMPLETE first record;
            # only a clamp that cut into a line has a partial head to drop.
            clamped = f.read(1) != b"\n"
        f.seek(start)
        data = f.read()

    if clamped:
        # A caller-supplied offset is always a line boundary, so its first line must
        # never be dropped — only a mid-record clamp has a partial head.
        head = data.find(b"\n")
        if head == -1:
            return [], start
        start += head + 1
        data = data[head + 1:]

    events = []
    pos = start
    for raw in data.splitlines(keepends=True):
        line_start, pos = pos, pos + len(raw)
        if not raw.endswith(b"\n"):
            break  # half-written trailing line: leave it for the next sweep
        if b"thread_settings_applied" not in raw:
            continue
        try:
            record = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(record, dict):
            continue
        settings = _codex_settings(record)
        if settings:
            events.append({
                "start": line_start,
                "end": pos,
                "ts": record.get("timestamp"),
                "label": codex_label(*settings),
            })
    consumed = start + data.rfind(b"\n") + 1 if b"\n" in data else start
    return events, consumed


def _event_age(timestamp):
    """Seconds since a rollout event, or None when the timestamp is unusable."""
    try:
        parsed = datetime.fromisoformat((timestamp or "").replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - parsed).total_seconds()


def settled_events(events):
    """``events`` split at the first one too fresh to trust.

    A model switch is written as TWO events milliseconds apart (model first, effort
    second). A sweep landing between them would alert on the half-applied pair and then
    alert again — so a fresh event, AND every event in the same burst before it, are left
    unconsumed for the next tick.

    Only an age inside [0, CODEX_SETTLE_SECONDS) defers: a timestamp in the future (clock
    skew, a bad writer) would otherwise defer that offset forever and silence the session.
    """
    cut = None
    for index, event in enumerate(events):
        age = _event_age(event["ts"])
        if age is not None and 0 <= age < CODEX_SETTLE_SECONDS:
            cut = index
            break
    if cut is None:
        return events, None
    # Walk back over the rest of the burst: those events are half of the same switch.
    while cut > 0:
        gap = _event_age(events[cut - 1]["ts"])
        fresh = _event_age(events[cut]["ts"])
        if gap is None or fresh is None or gap - fresh > CODEX_BURST_SECONDS:
            break
        cut -= 1
    return events[:cut], events[cut]["start"]


def flap_alert(label, seen, timestamp):
    return (
        f"⚠️ Model identity flipped and came back by {user_time(timestamp)}:\n"
        f"{label} → {' → '.join(seen)} → {label}\n"
        "It ran on the wrong model in between — something is rewriting this session's "
        "settings."
    )


def baseline_codex(path):
    """(label, consumed offset) for a thread seen for the first time.

    Tries a short tail first and escalates to the WHOLE file, because most rollouts
    answer from the last megabyte while a bounded escalation could not tell "no settings
    event in range" (label wrongly unknown, so the next drift is adopted as the baseline
    and never reported) from "no settings event at all".
    """
    label = None
    consumed = 0
    for max_bytes in (CODEX_BASELINE_TAIL_BYTES, None):
        events, consumed = read_settings_events(path, 0, max_bytes)
        if events is None:
            return None, 0
        if events:
            label = events[-1]["label"]
            break
    return label, consumed


def file_identity(path):
    """(device, inode) of a rollout, or None. Stored beside the offset so a file swapped
    in under the same name — which would make the stored offset point into unrelated
    bytes — re-baselines instead of being read as a continuation."""
    try:
        stat = os.stat(path)
    except OSError:
        return None
    return [stat.st_dev, stat.st_ino]


def _baseline_entry(path, entry=None):
    label, consumed = baseline_codex(path)
    entry = entry if isinstance(entry, dict) else {}
    entry.update({"last_model": label, "offset": consumed, "file": file_identity(path)})
    return entry


def check_codex_rollout(cfg, topic_id, thread_id, path, state):
    entry = state.get(thread_id)
    if (not isinstance(entry, dict) or not isinstance(entry.get("offset"), int)
            or entry.get("file") != file_identity(path)):
        state[thread_id] = _baseline_entry(path, entry)
        save_state(state)
        return

    events, consumed = read_settings_events(path, entry["offset"])
    if events is None:  # truncated under us — re-baseline instead of guessing
        state[thread_id] = _baseline_entry(path, entry)
        save_state(state)
        return

    events, unsettled_at = settled_events(events)
    if unsettled_at is not None:
        consumed = unsettled_at
    if not events:
        if consumed != entry["offset"]:
            entry["offset"] = consumed
            save_state(state)
        return

    previous = entry.get("last_model")
    current = events[-1]["label"]
    if previous and current != previous:
        _send_without_ambiguous_retry(
            cfg, topic_id, transition_alert(previous, current, events[-1]["ts"]),
        )
    elif previous:
        # Net-unchanged but it did not stay put: the session ran on the other model
        # between two sweeps, which is the same failure with a shorter fuse.
        seen = [e["label"] for e in events if e["label"] != previous]
        if seen:
            _send_without_ambiguous_retry(
                cfg, topic_id, flap_alert(previous, seen, events[-1]["ts"]),
            )
    entry["last_model"] = current
    entry["offset"] = consumed
    save_state(state)


def codex_rollout_path(pane, session_id):
    """Rollout file a live codex pane is writing, or None.

    Resolved from the pane process tree's OPEN file descriptors, which is the only exact
    answer: a resumed codex session writes a NEW rollout (so the registry's session_id
    goes stale) and several sessions share a cwd (so newest-for-cwd can hand back another
    topic's rollout — a false alert here and a missed one there). The registry id is the
    fallback for when /proc is unreadable; there is deliberately no cwd fallback.
    """
    path = codex_ctx.open_rollout_for_pid_tree(pane_pid(pane))
    if path:
        return path
    path = codex_ctx.rollout_for_session(session_id)
    # Both fallback outcomes are worth a line in the journal: a session that is watched
    # by registry id only, and one that is not watched at all, are otherwise silent.
    log(f"pane {pane}: no open rollout fd; "
        + (f"falling back to registry id {session_id}" if path else "session unresolved"))
    return path


def check_codex(cfg, topic_id, info, pane, state):
    path = codex_rollout_path(pane, info.get("session_id"))
    if not path or not os.path.isfile(path):
        return
    # Key on the rollout's own thread uuid, not the registry's: a resumed codex session
    # writes a new rollout, which must baseline on its own rather than inherit.
    thread_id = os.path.basename(path)[: -len(".jsonl")][-36:]
    check_codex_rollout(cfg, topic_id, thread_id, path, state)


def sweep(cfg):
    state = load_state()
    registry = read_registry()
    # Match the daemon's context-warning routing: a pane may re-register without ending
    # its older topic, so only the most recent (highest-id) non-feed topic owns alerts.
    latest_topic = {}
    for raw_topic_id, info in registry.items():
        try:
            if not isinstance(info, dict) or info.get("ended") or info.get("feed"):
                continue
            pane = info.get("pane")
            if pane and int(raw_topic_id) > int(latest_topic.get(pane, "-1")):
                latest_topic[pane] = raw_topic_id
        except Exception as exc:
            log(f"topic {raw_topic_id}: {exc}")

    for raw_topic_id, info in registry.items():
        try:
            if not isinstance(info, dict) or info.get("ended") or info.get("feed"):
                continue
            pane = info.get("pane")
            if not pane or latest_topic.get(pane) != raw_topic_id:
                continue
            engine = engine_of_pane(pane)
            if not pane_alive(pane) or engine not in ("claude", "codex"):
                continue
            if engine == "codex":
                check_codex(cfg, int(raw_topic_id), info, pane, state)
                continue
            context = read_context(pane)
            session_id = context.get("session_id") if context else None
            cwd = info.get("cwd")
            if not session_id or not cwd:
                continue
            path = transcript_path(cwd, session_id)
            if not os.path.isfile(path):
                continue
            check_transcript(cfg, int(raw_topic_id), session_id, path, state)
        except Exception as exc:
            log(f"topic {raw_topic_id}: {exc}")
    save_state(state)


def main():
    secure_process_umask()
    sweep(load_config())


if __name__ == "__main__":
    main()
