"""Morning digest: one deterministic daily summary to the group's General topic.

Run by agent-telegram-bridge-digest.timer (06:00 UTC = 09:00 the owner time). Zero
model tokens: everything comes from tmux, the bridge registry/state, the gh
search API, and the statusline usage cache. Deltas are computed against a
snapshot saved on the previous run.
"""

import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bridge.codex_ctx import ctx_pct_for_pane
from bridge.common import api, load_config, read_registry, state_path, secure_process_umask
from bridge.daemon import (
    CTX_STALE, TZ_OFFSET, account_usage_line, context_for, fleet_panes,
    headless_worker_count, issue_queue_config, issue_queue_counts, local_hhmm, pane_cwd, pane_pid,
    registry_by_pane, unread_count,
)

def snapshot_path():
    """Resolved per call, not bound at import.

    `state_path` reads `STATE_DIR` and CREATES the directory, so binding this at module level
    made importing this module a filesystem side effect, and made the path immune to any
    later redirection of the state directory — including the per-test one, which is how a
    digest test could still write into the shared session home (#203 review r2)."""
    return state_path("digest-snapshot.json")


def load_snapshot():
    try:
        with open(snapshot_path()) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_snapshot(snap):
    path = snapshot_path()
    with open(path + ".tmp", "w") as f:
        json.dump(snap, f)
    os.replace(path + ".tmp", path)


def delta(value, previous):
    if previous is None or value is None:
        return ""
    d = value - previous
    if not d:
        return ""
    return f" ({'+' if d > 0 else ''}{round(d, 2) if isinstance(d, float) else d})"


def read_ctx_raw(pane_id):
    """A claude pane's last-known context reading WITHOUT the staleness cutoff — an idle
    session just hasn't redrawn its statusline lately, so its last reading is still its real
    context. Sets _stale so the digest can mark an old reading.

    Delegates to context_for rather than re-reading the file, so the digest inherits the SAME
    guarantees /ctx has (#158): structural validity, and for an aged record proof that the
    process now in the pane is the claude session that wrote it. Reading the file directly
    only checked shape, which left a valid session-A record on display indefinitely once a
    session B took over the pane (Codex round 3 of PR #159). Callers pass claude panes only —
    session_lines handles codex separately."""
    ctx = context_for(pane_id, "claude")
    if ctx is None:
        return None
    ctx = dict(ctx)                       # don't mutate the cached/returned record
    ctx["_stale"] = time.time() - ctx.get("ts", 0) > CTX_STALE
    return ctx


def session_lines(prev_costs, new_costs):
    panes = fleet_panes()
    if panes is None:
        return ["(tmux not running)"]
    by_pane = registry_by_pane()
    rows = []  # (sort_pct, text) — emitted fullest-context-first
    for pane_id, session, title, engine in panes:
        tag = " [codex]" if engine == "codex" else ""
        if pane_id not in by_pane:
            rows.append((-1, f"• {session}{tag} — not connected"))
            continue
        tid, info = by_pane[pane_id]
        if info.get("closed"):
            continue          # The owner closed this topic in Telegram — not part of their morning (#161)
        name = info.get("name") or session
        icon = info.get("icon") or "•"
        unread = unread_count(int(tid))
        unread_flag = f" — {unread} unread!" if unread else ""
        if engine == "codex":
            # codex has no Claude statusline; derive ctx from its session rollout (#61)
            try:
                pct = ctx_pct_for_pane(pane_pid(pane_id), pane_cwd(pane_id))
            except Exception:
                pct = None  # one unreadable pane must not abort the whole digest
            if pct is None:
                rows.append((-1, f"{icon} {name}{tag} — ctx n/a{unread_flag}"))
            else:
                bullet = "⚠️ " if pct >= 70 else f"{icon} "
                rows.append((pct, f"{bullet}{name}{tag} — {pct}% ctx{unread_flag}"))
            continue
        ctx = read_ctx_raw(pane_id)
        if not ctx or "pct" not in ctx:
            rows.append((-1, f"{icon} {name} — ctx ?{unread_flag}"))
            continue
        pct = int(float(ctx["pct"]))
        mark = "~" if ctx.get("_stale") else ""  # ~ = last reading is old (idle session)
        cost_part = ""
        if ctx.get("cost"):
            cost = round(float(ctx["cost"]), 2)
            new_costs[session] = cost
            cost_part = f" · ${cost:.0f}" + delta(cost, prev_costs.get(session))
        bullet = "⚠️ " if pct >= 70 else f"{icon} "
        rows.append((pct, f"{bullet}{name} — {mark}{pct}% ctx{cost_part}{unread_flag}"))
    rows.sort(key=lambda r: r[0], reverse=True)
    return [t for _, t in rows] or ["(no sessions running)"]


def ended_since(prev_ts):
    names = [
        info.get("name", "?")
        for info in read_registry().values()
        if info.get("ended") and info["ended"] > prev_ts
    ]
    return f"Ended since last digest: {', '.join(names)}" if names else None


def issue_queue_digest_line(prev_counts):
    counts = issue_queue_counts()
    if not counts or all(v is None for v in counts.values()):
        return None, prev_counts
    shown = {"ready": "queued", "running": "running", "review": "review",
             "human-review": "human-review", "blocked": "blocked", "failed": "failed"}
    parts = []
    for label, word in shown.items():
        value = counts.get(label)
        if value is None:
            continue
        if value or (prev_counts or {}).get(label):
            parts.append(f"{value} {word}" + delta(value, (prev_counts or {}).get(label)))
    parts.append(f"{headless_worker_count()} workers now")
    configured = issue_queue_config()
    name = configured[1].title() if configured else "Queue"
    return f"🎼 {name}: " + " · ".join(parts), counts


def main():
    secure_process_umask()
    cfg = load_config()
    snap = load_snapshot()
    new_costs = {}

    lines = session_lines(snap.get("costs", {}), new_costs)
    body = ["Sessions (fullest context first):"] + lines

    ended = ended_since(snap.get("ts_iso", "1970"))
    if ended:
        body.append(ended)

    # The snapshot key is "orchestra" for one reason: that is what it was called before the
    # feature was renamed to issue_queue, and a running deployment's digest-snapshot.json still
    # holds it. Renaming it would cost one morning's deltas for nothing — the file is internal
    # state that never ships, and no reader outside this line ever sees the name.
    orch_line, orch_counts = issue_queue_digest_line(snap.get("orchestra"))
    body.append("")
    if orch_line:
        body.append(orch_line)

    usage = account_usage_line()
    if usage:
        body.append(usage)

    daemon_state = subprocess.run(
        ["systemctl", "--user", "is-active", "agent-telegram-bridge.service"],
        capture_output=True, text=True,
    ).stdout.strip()
    body.append(f"Bridge daemon: {daemon_state}")

    weekday = time.strftime("%a %d %b", time.gmtime(time.time() + TZ_OFFSET * 3600))
    text = f"☀️ Morning digest — {weekday}, {local_hhmm()} (UTC+{TZ_OFFSET})\n" + "\n".join(body)
    api(cfg["bot_token"], "sendMessage", {"chat_id": cfg["chat_id"], "text": text})

    save_snapshot({
        "ts_iso": time.strftime("%Y-%m-%dT%H:%M:%S+0000", time.gmtime()),
        "costs": new_costs,
        "orchestra": {k: v for k, v in (orch_counts or {}).items() if v is not None},
    })


if __name__ == "__main__":
    main()
