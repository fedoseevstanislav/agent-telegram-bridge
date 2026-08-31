"""One-time manual restore of bridge sessions that a reboot killed (#74).

The auto path (daemon.restore_on_boot) only fires on a boot_id change and skips `ended`
entries. This CLI is the deliberate, operator-driven path for reviving already-`ended`
reboot victims — it is independent of the boot gate.

Usage:
    python -m bridge.restore_cli --topics 13,212,606 [--fresh 33] \
        [--codex 2136:<session_uuid>]
    python -m bridge.restore_cli --list          # show revivable topics + resolved session_id

`--topics`  comma-separated topic ids to resume (session_id resolved from each topic's old
            statusline context file; claude only).
`--fresh`   comma-separated topic ids to reopen FRESH (no resume; prior context lost).
`--codex`   comma-separated <topic>:<session_uuid> pairs to resume as codex with an explicit,
            verified session id (bypasses inference).
"""

import argparse
import sys

from bridge import daemon
from bridge.common import load_config, read_registry


def _parse_ids(s):
    return [t.strip() for t in (s or "").split(",") if t.strip()]


def list_revivable():
    reg = read_registry()
    print(f"{'topic':>6} {'ended?':<6} {'engine':<7} {'name':<28} {'resolved session_id':<38}")
    for tid, info in reg.items():
        if info.get("feed"):
            continue
        pane = info.get("pane")
        sid = info.get("session_id") or (daemon.context_session_id(pane) if pane else None)
        engine = info.get("engine") or ("claude" if sid else "?")
        ended = "yes" if info.get("ended") else "no"
        print(f"{tid:>6} {ended:<6} {engine:<7} {info.get('name','')[:28]:<28} {str(sid or '-'):<38}")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Manual one-time bridge session restore (#74)")
    ap.add_argument("--topics", help="comma-separated topic ids to resume")
    ap.add_argument("--fresh", help="comma-separated topic ids to reopen fresh (no resume)")
    ap.add_argument("--codex", help="comma-separated <topic>:<session_uuid> pairs to resume as codex")
    ap.add_argument("--list", action="store_true", help="list revivable topics and exit")
    args = ap.parse_args(argv)

    if args.list:
        list_revivable()
        return 0

    cfg = load_config()
    specs = []
    fresh_ids = set(_parse_ids(args.fresh))
    for tid in _parse_ids(args.topics):
        specs.append({"tid": tid, "fresh": tid in fresh_ids})
    for tid in fresh_ids:
        if tid not in _parse_ids(args.topics):
            specs.append({"tid": tid, "fresh": True})
    for pair in _parse_ids(args.codex):
        tid, _, uuid = pair.partition(":")
        if not (tid and uuid):
            print(f"bad --codex pair: {pair!r} (want <topic>:<uuid>)", file=sys.stderr)
            return 2
        specs.append({"tid": tid, "engine": "codex", "session_id": uuid})

    if not specs:
        ap.error("nothing to do — pass --topics/--fresh/--codex or --list")

    results = daemon.revive_topics(cfg, specs)
    for tid, status in results.items():
        print(f"topic {tid}: {status}")
    return 0 if all(s != "failed" for s in results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
