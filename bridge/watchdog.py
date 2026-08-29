"""Watchdog: alert the owner in the group's General topic when the bridge daemon is down.

Sends DIRECTLY via the Bot API — never through the daemon (it's the thing being
watched). Invoked two ways: the main unit's OnFailure= (restart loop exhausted)
and a periodic timer (catches manual stops and anything OnFailure misses).
Alerts once per down-transition; sends a recovery notice on the up-transition.
"""

import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bridge.common import api, load_config, now_iso, secure_process_umask, state_path

SERVICE = "claude-telegram-bridge.service"
STATUS_EXCERPT_CHARS = 800


def service_active():
    out = subprocess.run(
        ["systemctl", "--user", "is-active", SERVICE], capture_output=True, text=True,
    )
    return out.stdout.strip() == "active"


def status_excerpt():
    out = subprocess.run(
        ["systemctl", "--user", "status", SERVICE, "--no-pager", "-n", "5"],
        capture_output=True, text=True,
    )
    text = (out.stdout or out.stderr).strip()
    return text[-STATUS_EXCERPT_CHARS:] or "(no status output)"


def load_state():
    try:
        with open(state_path("watchdog.json")) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(state):
    path = state_path("watchdog.json")
    with open(path + ".tmp", "w") as f:
        json.dump(state, f)
    os.replace(path + ".tmp", path)


def send_general(cfg, text):
    api(cfg["bot_token"], "sendMessage", {"chat_id": cfg["chat_id"], "text": text})


def main():
    secure_process_umask()
    cfg = load_config()
    state = load_state()
    if service_active():
        if state.get("down_since"):
            send_general(cfg, f"✅ Bridge daemon is back up (was down since {state['down_since']}).")
            save_state({})
        return
    if state.get("down_since"):
        return  # already alerted for this outage
    down_since = now_iso()
    send_general(cfg, (
        f"🚨 Bridge daemon is DOWN ({down_since}) — sessions can't receive your messages "
        f"(they queue at Telegram and arrive once it's back).\n"
        f"Fix: ssh in, `systemctl --user restart {SERVICE}`.\n\n"
        f"Status:\n{status_excerpt()}"
    ))
    save_state({"down_since": down_since})


if __name__ == "__main__":
    main()
