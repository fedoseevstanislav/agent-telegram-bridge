# Install

Twenty minutes, most of it in Telegram. Nothing here needs root, and by default everything
the installer writes lives under `$HOME` (`--prefix` can put the launcher elsewhere, if you
point it somewhere else).

Read [SECURITY.md](../SECURITY.md) first if you have not. The install is easy; the decision
about what a message from Telegram is allowed to do on your machine is the part that matters.

---

## 0. Prerequisites

| Requirement | Why |
|---|---|
| **Python 3.12** | The project pins `==3.12.*`. It has **no third-party runtime dependencies** — standard library only — so there is no virtualenv to create and nothing to `pip install`. |
| **tmux** | The daemon starts agent sessions in tmux panes and types replies into them. Without tmux, sending and receiving still work, but `/claude` and `/codex` cannot start anything. |
| **systemd with a user instance** | The daemon and its watchdog run as user services. |
| **An agent CLI** | Claude Code and/or Codex, already installed and logged in. The bridge does not manage them; it launches whatever is on your `PATH`. |
| **A Telegram account** | Yours. It becomes the only account allowed to drive the bridge. |

On a headless server, systemd user services stop when you log out unless lingering is on:

```bash
sudo loginctl enable-linger "$USER"
```

Log in again afterwards. The installer checks this and tells you if it is missing.

---

## 1. Create the bot

1. Message [@BotFather](https://t.me/BotFather) and send `/newbot`.
2. Give it a display name and a username ending in `bot`.
3. BotFather replies with a token that looks like `123456789:AAExxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx`.

**That token is a credential.** Anyone holding it *is* your bot: they can read everything it
can read and post as it. Keep it out of shell history, screenshots, and commits. If it leaks,
`/revoke` in BotFather immediately.

Keep the chat with BotFather open — you come back to it in step 3.

---

## 2. Create the group and give the bot access

The bridge needs a **supergroup with Topics enabled**, because one Telegram topic is one agent
session.

1. In Telegram, create a **new group**. Add the bot as a member while creating it.
2. Open the group → *Edit* → turn on **Topics**. This converts it to a supergroup.
3. *Edit* → *Administrators* → **promote the bot to administrator**, and make sure
   **Manage Topics** is enabled.

Three things people get wrong here, each of which fails silently:

- **The bot must be an administrator.** Telegram bots run in *privacy mode*, which means an
  ordinary bot member never receives normal group messages — only commands and direct replies.
  Nothing reports this. The bot simply sits there and the bridge never reacts. Promoting it to
  admin lifts the restriction.
- **You do not need 200 members.** Plenty of documentation still repeats an old rule that
  Topics require a 200-member group. It is stale. A two-member supergroup — you and the bot —
  runs Topics fine; that is exactly the shape this project is developed against.
- **Use a group created for this and nothing else.** An administrator bot sees every message in
  the group. Do not bolt this onto a group you use for anything real.

---

## 3. Find your two numbers

The config needs the group's `chat_id` and your own `owner_id`.

Send any message in the group's **General** topic, then ask the Bot API what it saw. Replace
`$TOKEN` with your token:

```bash
TOKEN='123456789:AAE...'
curl -s "https://api.telegram.org/bot$TOKEN/getUpdates" |
  python3 -m json.tool | grep -A6 '"chat"\|"from"'
```

You are looking for two fields:

- `chat.id` — a **negative** number beginning `-100`. That is `chat_id`.
- `from.id` — a positive number, on the message *you* just sent. That is `owner_id`.

```json
"from": { "id": 987654321, "first_name": "..." },
"chat": { "id": -1001234567890, "title": "...", "is_forum": true }
```

Check `is_forum: true` in that output. If it is absent or false, Topics are not on — go back to
step 2.

If `getUpdates` returns `{"ok":true,"result":[]}`: the bot is not an administrator (step 2), or
you sent the message before adding it. Send another message and retry.

> **Do this only before the daemon starts.** Telegram allows exactly one `getUpdates` consumer
> per token. Once the bridge is running it *is* that consumer, and a manual `getUpdates` steals
> updates from it. After install, use `tg-bridge status` instead.

---

## 4. Write the config

```bash
mkdir -p ~/.config/claude-telegram-bridge
cat > ~/.config/claude-telegram-bridge/config.json <<'EOF'
{
  "bot_token": "123456789:AAE...",
  "chat_id": -1001234567890,
  "owner_id": 987654321
}
EOF
chmod 600 ~/.config/claude-telegram-bridge/config.json
```

`chat_id` and `owner_id` are **numbers, not strings** — no quotes. `owner_id` is a numeric user
id, never an `@username`: usernames can be given up and re-registered by someone else.

`owner_id` is the entire authorization model. A message is acted on when its sender id matches
it. Group membership grants nothing, and if this value is missing or malformed the daemon
refuses ingress rather than falling back to trusting the group.

See [CONFIGURATION.md](CONFIGURATION.md) for the optional keys — voice transcription, spawn
permissions, models, timers.

---

## 5. Install

```bash
git clone https://github.com/fedoseevstanislav/agent-telegram-bridge.git ~/agent-telegram-bridge
cd ~/agent-telegram-bridge
./scripts/install.sh
```

The clone can live anywhere — including a path with spaces in it; the installer resolves its
own location and quotes it for systemd. It:

- checks Python 3.12, tmux, the systemd user instance, your config, and whether another account
  can write the clone (**before writing anything**);
- writes a `tg-bridge` launcher to `~/.local/bin` (`--prefix` to change it);
- generates the systemd user units with this clone's path substituted in, into
  `~/.config/systemd/user/`;
- symlinks `skill/` to `~/.claude/skills/tg-channel` if you use Claude Code;
- enables and starts the daemon and all three timers — the watchdog, the morning digest, and the model check.

It refuses to overwrite anything that already exists — pass `--force` when you mean to
reinstall.

If `~/.local/bin` is not on your `PATH`, the installer says so. Add it and open a new shell.

---

## 6. Prove it works

**From inside tmux** — that is how the daemon locates your pane to wake it:

```bash
tmux new -s bridge-test
tg-bridge register --name "install test"     # prints a topic id
tg-bridge send --topic <id> "hello from the bridge"
```

A new topic named *install test* should appear in your group with that message in it.

Reply to it in Telegram, then read the reply back:

```bash
tg-bridge recv --topic <id>
```

If both directions worked, you are done. Delete the test topic in Telegram whenever you like.

---

## Troubleshooting

**Nothing appears in Telegram, and no command errors.**
Almost always the bot is not an administrator with Manage Topics. Confirm:

```bash
TOKEN=$(python3 -c 'import json;print(json.load(open("'"$HOME"'/.config/claude-telegram-bridge/config.json"))["bot_token"])')
CHAT=$(python3 -c 'import json;print(json.load(open("'"$HOME"'/.config/claude-telegram-bridge/config.json"))["chat_id"])')
BOT=$(curl -s "https://api.telegram.org/bot$TOKEN/getMe" | python3 -c 'import json,sys;print(json.load(sys.stdin)["result"]["id"])')
curl -s "https://api.telegram.org/bot$TOKEN/getChatMember?chat_id=$CHAT&user_id=$BOT" | python3 -m json.tool
```

Want `"status": "administrator"` and `"can_manage_topics": true`.

**The daemon will not stay running.**

```bash
systemctl --user status claude-telegram-bridge.service
journalctl --user -u claude-telegram-bridge.service -n 50
```

`config missing 'chat_id'` and friends mean step 4. A `409 Conflict` from Telegram means
something else is polling the same token — another copy of the daemon, or a `getUpdates` left
running from step 3.

**Messages stop arriving after a while.**
Check the daemon is still up. While it is down, messages queue at Telegram and are delivered on
restart — `getUpdates` is offset-based, so nothing is lost. Outgoing `tg-bridge send` keeps
working regardless, because the CLI talks to the Bot API directly.

**The daemon died and nobody told me.**
It should have: a watchdog timer posts to the group's **General** topic on the down-transition
and again on recovery, talking to the Bot API directly — the daemon cannot be the messenger for
its own death. If that alert never arrives, the watchdog timer is not enabled:

```bash
systemctl --user list-timers 'claude-telegram-bridge*'
```

**Voice messages arrive as `[voice message — transcription failed: …]`.**
The reason is in the brackets, and `no OpenAI key: …` means transcription is simply not
configured. See [CONFIGURATION.md](CONFIGURATION.md). Everything else keeps working; only the
audio is not turned into text.

---

## Uninstall

```bash
systemctl --user disable --now claude-telegram-bridge.service
systemctl --user disable --now claude-telegram-bridge-watchdog.timer
systemctl --user disable --now claude-telegram-bridge-digest.timer
systemctl --user disable --now claude-telegram-bridge-model-watchdog.timer
rm -f ~/.config/systemd/user/claude-telegram-bridge*
systemctl --user daemon-reload
rm -f ~/.local/bin/tg-bridge ~/.claude/skills/tg-channel
```

That leaves two things deliberately: `~/.config/claude-telegram-bridge/` (your token) and
`~/.local/share/claude-telegram-bridge/` (every message ever sent or received, in plaintext).
Remove them when you actually mean to.

Delete the bot in BotFather with `/deletebot` if you are finished with it — otherwise the token
stays valid.
