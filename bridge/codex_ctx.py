"""Codex context-usage readout (issue #61).

Codex has no Claude-style statusline writer, but every Codex session appends a
rollout JSONL under ~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl. Each turn emits
an `event_msg` whose payload.type is "token_count", carrying the current request's
input tokens and the model context window — so context-occupancy % is:

    last_token_usage.input_tokens / model_context_window

(total_token_usage is cumulative billing across the whole session — it exceeds the
window and is NOT occupancy.)

A live Codex pane is mapped to its rollout by the session's launch cwd, recorded in
the first `session_meta` record. The live session's rollout is the most-recently
modified one for that cwd.
"""

import json
import os

SESSIONS_DIR = os.path.expanduser("~/.codex/sessions")
PROC_DIR = "/proc"  # overridable so the fd/pid-tree walk is testable without a real /proc


def session_meta(path):
    """Payload of a rollout's first (session_meta) record, or None."""
    try:
        with open(path) as f:
            meta = json.loads(f.readline())
    except (OSError, ValueError):
        return None
    if meta.get("type") != "session_meta":
        return None
    payload = meta.get("payload")
    return payload if isinstance(payload, dict) else None


def _meta_cwd(path):
    """cwd recorded in a rollout's first (session_meta) record, or None."""
    return (session_meta(path) or {}).get("cwd")


def is_subagent_rollout(path):
    """True when the rollout belongs to a sub-agent thread rather than the session the
    user drives. Sub-agents share their parent's cwd AND its `session_id`, but carry
    `parent_thread_id` / `thread_source: subagent` — and they run codex's cheap
    sub-agent profile, so callers that want the pane's own thread must exclude them."""
    meta = session_meta(path) or {}
    return bool(meta.get("parent_thread_id")) or meta.get("thread_source") == "subagent"


def rollout_for_session(session_id):
    """Rollout file whose thread uuid is `session_id` (the bridge registry records
    exactly that uuid for codex sessions), or None."""
    if not session_id or not os.path.isdir(SESSIONS_DIR):
        return None
    suffix = f"-{session_id}.jsonl"
    for root, _dirs, files in os.walk(SESSIONS_DIR):
        for fn in files:
            if fn.startswith("rollout-") and fn.endswith(suffix):
                return os.path.join(root, fn)
    return None


def process_tree(pid):
    """`pid` and every descendant, from /proc. A codex CLI is a node wrapper around the
    native binary, so the process holding the rollout open is usually a child."""
    if not pid:
        return []
    children = {}
    try:
        entries = os.listdir(PROC_DIR)
    except OSError:
        return [pid]
    for entry in entries:
        if not entry.isdigit():
            continue
        try:
            with open(os.path.join(PROC_DIR, entry, "stat"), "rb") as f:
                fields = f.read().rsplit(b")", 1)[-1].split()
            parent = int(fields[1])
        except (OSError, ValueError, IndexError):
            continue
        children.setdefault(parent, []).append(int(entry))

    tree, queue = [], [pid]
    while queue:
        current = queue.pop()
        if current in tree:
            continue
        tree.append(current)
        queue.extend(children.get(current, ()))
    return tree


def open_rollouts_for_pids(pids):
    """Every codex rollout held OPEN by any of `pids`, as (all, roots).

    A live codex process keeps its own rollout open for appending, so this identifies a
    pane's thread exactly — no cwd guessing, and immune to a registry id going stale when
    a session is resumed. A parent also holds its SUB-AGENTS' rollouts open, so `roots`
    drops those (and any rollout whose session_meta cannot be read: unidentified is not
    root). Callers get both lists because "saw nothing" and "saw something unusable" want
    different answers.
    """
    prefix = os.path.join(SESSIONS_DIR, "")
    found = set()
    for pid in pids or ():
        fd_dir = os.path.join(PROC_DIR, str(pid), "fd")
        try:
            entries = os.listdir(fd_dir)
        except OSError:
            continue
        for entry in entries:
            try:
                target = os.readlink(os.path.join(fd_dir, entry))
            except OSError:
                continue
            name = os.path.basename(target)
            if not target.startswith(prefix) or target.endswith(" (deleted)"):
                continue
            if name.startswith("rollout-") and name.endswith(".jsonl"):
                found.add(target)
    roots = []
    for path in sorted(found):
        meta = session_meta(path)
        if meta is None:
            # An opened rollout we cannot classify may BE the pane's thread; treating the
            # rest as unambiguous could pick the wrong one, so the whole set is unusable.
            return sorted(found), []
        if not (meta.get("parent_thread_id") or meta.get("thread_source") == "subagent"):
            roots.append(path)
    return sorted(found), roots


def open_rollout_for_pids(pids):
    """The ONE non-sub-agent rollout held open by any of `pids`, or None when there is
    no such rollout or more than one — an ambiguous answer is worse than none."""
    _all, roots = open_rollouts_for_pids(pids)
    return roots[0] if len(roots) == 1 else None


def open_rollout_for_pid_tree(pid):
    """The rollout held open by the process tree rooted at `pid`, or None."""
    return open_rollout_for_pids(process_tree(pid))


def root_rollouts_for_cwd(cwd):
    """Non-sub-agent rollouts launched in `cwd`, newest first."""
    if not cwd or not os.path.isdir(SESSIONS_DIR):
        return []
    return [path for path in _rollouts_newest_first()
            if _meta_cwd(path) == cwd and not is_subagent_rollout(path)]


def rollout_for_pane(pid, cwd):
    """The rollout of the codex session running under `pid`, or None.

    The process tree's open fds are the exact answer. When they cannot supply one this
    degrades to the cwd lookup, but only where that lookup is itself unambiguous — ONE
    root thread launched in that cwd. Every other case returns None on purpose: this
    readout exists to be trusted, and the cwd lookup's failure mode is answering with
    some OTHER thread's file, which is worse than answering nothing (#123).
    """
    opened, roots = open_rollouts_for_pids(process_tree(pid))
    if len(roots) == 1:
        return roots[0]
    if opened:
        return None
    candidates = root_rollouts_for_cwd(cwd)
    return candidates[0] if len(candidates) == 1 else None


def latest_rollout_for_cwd(cwd):
    """Most-recently-modified rollout whose session was launched in `cwd`, or None.
    Callers pass the cwd of a LIVE pane, so the newest rollout for it is that
    session's (a live session appends continuously, keeping its rollout newest)."""
    if not cwd or not os.path.isdir(SESSIONS_DIR):
        return None
    best, best_mt = None, -1.0
    for root, _dirs, files in os.walk(SESSIONS_DIR):
        for fn in files:
            if not (fn.startswith("rollout-") and fn.endswith(".jsonl")):
                continue
            path = os.path.join(root, fn)
            try:
                mt = os.path.getmtime(path)
            except OSError:
                continue
            if mt > best_mt and _meta_cwd(path) == cwd:
                best, best_mt = path, mt
    return best


def _last_token_info(path):
    """The `info` dict of the last token_count event in a rollout, or None."""
    info = None
    try:
        with open(path) as f:
            for line in f:
                try:
                    o = json.loads(line)
                except ValueError:
                    continue
                pl = o.get("payload") or {}
                if o.get("type") == "event_msg" and pl.get("type") == "token_count":
                    info = pl.get("info")
    except OSError:
        return None
    return info


def _last_rate_limits(path):
    """The `rate_limits` dict of the last token_count event that carries one, or
    None. Some token_count events only carry `info` (no rate_limits) — those are
    skipped so we return the freshest non-empty rate_limits snapshot."""
    rate_limits = None
    try:
        with open(path) as f:
            for line in f:
                try:
                    o = json.loads(line)
                except ValueError:
                    continue
                pl = o.get("payload") or {}
                if o.get("type") == "event_msg" and pl.get("type") == "token_count":
                    rl = pl.get("rate_limits")
                    if rl:
                        rate_limits = rl
    except OSError:
        return None
    return rate_limits


def _usage_from_rollout(path):
    """Rate-limit usage dict from a specific rollout file, or None. `primary` is the 5h
    window, `secondary` is the weekly window."""
    rate_limits = _last_rate_limits(path) if path else None
    if not rate_limits:
        return None
    primary = rate_limits.get("primary") or {}
    secondary = rate_limits.get("secondary") or {}
    return {
        "primary_pct": primary.get("used_percent"),
        "primary_window_min": primary.get("window_minutes"),
        "primary_reset": primary.get("resets_at"),
        "secondary_pct": secondary.get("used_percent"),
        "secondary_window_min": secondary.get("window_minutes"),
        "secondary_reset": secondary.get("resets_at"),
        "plan_type": rate_limits.get("plan_type"),
    }


def usage_for_cwd(cwd):
    """Codex account rate-limit usage for the live session launched in `cwd`, or
    None if no rollout / no rate_limits data is found."""
    return _usage_from_rollout(latest_rollout_for_cwd(cwd))


def _rollouts_newest_first():
    """All rollout files under SESSIONS_DIR (any cwd), newest mtime first."""
    if not os.path.isdir(SESSIONS_DIR):
        return []
    rolls = []
    for root, _dirs, files in os.walk(SESSIONS_DIR):
        for fn in files:
            if not (fn.startswith("rollout-") and fn.endswith(".jsonl")):
                continue
            path = os.path.join(root, fn)
            try:
                rolls.append((os.path.getmtime(path), path))
            except OSError:
                continue
    rolls.sort(reverse=True)
    return [p for _mt, p in rolls]


def latest_rollout_any():
    """Most-recently-modified rollout across ALL cwds, or None."""
    rolls = _rollouts_newest_first()
    return rolls[0] if rolls else None


def usage_latest():
    """Account-wide Codex rate-limit usage from the most recent rollout that CARRIES a
    rate-limit snapshot, or None. Codex limits are account-scoped, so this powers a global
    `/usage codex` with no live pane. A freshly-started session's newest rollout may not
    have logged rate_limits yet, so we skip past it to the newest rollout that has one —
    rather than reporting 'no data' while an older rollout still holds the account usage."""
    for path in _rollouts_newest_first():
        u = _usage_from_rollout(path)
        if u:
            return u
    return None


def ctx_pct_for_cwd(cwd):
    """Context-occupancy % (int) for the live Codex session launched in `cwd`,
    or None if no rollout / no usage data is found.

    Prefer ctx_pct_for_pane(): sub-agent threads write their rollout in the PARENT's cwd,
    so whenever one is busy it is the newest for that cwd and this reports ITS occupancy
    instead of the session's (#123)."""
    path = latest_rollout_for_cwd(cwd)
    if not path:
        return None
    return ctx_pct_from_rollout(path)


def ctx_pct_for_pane(pid, cwd):
    """Context-occupancy % (int) for the codex session running under `pid`, or None.

    Resolves the pane's OWN rollout through its open file descriptors; see
    rollout_for_pane() for when the cwd lookup is allowed to answer instead (#123)."""
    path = rollout_for_pane(pid, cwd)
    if not path:
        return None
    return ctx_pct_from_rollout(path)


def ctx_pct_from_rollout(path):
    """Context-occupancy % from a specific rollout file, or None."""
    info = _last_token_info(path)
    if not info:
        return None
    window = info.get("model_context_window")
    used = (info.get("last_token_usage") or {}).get("input_tokens")
    if not window or used is None:
        return None
    return round(100 * used / window)
