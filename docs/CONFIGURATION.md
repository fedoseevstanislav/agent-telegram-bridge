# Configuration

Everything this project owns lives in one file: `~/.config/agent-telegram-bridge/config.json`.

(Two exceptions, both for one optional key: `openai_api_key` also has legacy fallback sources,
described with that key below. Nothing else reads anything outside this file.)

It holds a bot token, so `chmod 600` it and never commit it. The installer refuses to run if
the permissions are looser than that.

```json
{
  "bot_token": "123456789:AAE...",
  "chat_id": -1001234567890,
  "owner_id": 987654321,

  "openai_api_key": "sk-...",
  "spawn_flags": {
    "claude": "--dangerously-skip-permissions",
    "codex": "--dangerously-bypass-approvals-and-sandbox"
  },
  "issue_queue": {"owner": "your-github-user", "label_prefix": "orchestra"},
  "carry_forward_repo": "your-github-user/your-repo"
}
```

The first three are required; the rest are optional and are described below. Unknown keys are
ignored, so you can leave notes to yourself in there.

---

## Required

### `bot_token` — string

From [@BotFather](https://t.me/BotFather). The number before the colon is the bot's own user
id, which the daemon parses out to recognise its own actions.

### `chat_id` — number

The supergroup's id: **negative**, beginning `-100`. A number, not a string. Getting this from
`getUpdates` is [INSTALL.md step 3](INSTALL.md).

If you convert an ordinary group to a supergroup, its id changes. Update this.

### `owner_id` — number

**Your** Telegram user id — positive, and a number rather than an `@username`, because
usernames can be released and re-registered by someone else.

This one value is the entire authorization model: a message is acted on when its sender id
equals it. Group membership grants nothing. If it is missing, zero, negative, or the wrong
type, the daemon refuses ingress rather than falling back to trusting the group — including
`true`, which Python would otherwise happily compare equal to user id 1.

---

## Optional

### `openai_api_key` — string

Enables voice transcription. Without it, a voice message still arrives, carrying
`[voice message — transcription failed: no OpenAI key: …]` instead of the words.

Short audio goes to OpenAI's `gpt-4o-mini-transcribe`. Anything over four minutes is cut into
pieces with ffmpeg and sent to `whisper-1` one piece at a time — in a single request that model
degenerates on long audio, repeating itself and losing most of what was said. Nothing is sent
anywhere else, and the pieces are deleted immediately.

**So `ffmpeg` is worth having installed** if you send long voice notes. Without it a long note
still transcribes, just in one request, with the quality loss that implies.

The key is validated before use, and a rejection never quotes it: a key containing a stray
newline makes the HTTP layer raise an exception whose message contains the whole key, and that
message used to be written to a log and mirrored back into the chat.

### `spawn_flags` — object

Extra flags for sessions the daemon starts (`/claude`, `/codex`) and revives. **The default is
nothing, deliberately.**

```json
"spawn_flags": {
  "claude": "--dangerously-skip-permissions",
  "codex": "--dangerously-bypass-approvals-and-sandbox"
}
```

With no setting, a spawned session keeps its ordinary approval prompts: a message that reaches
it cannot edit files or run commands until you approve that in the terminal. So a mistake in
the *first* half of the threat model — a wrong `owner_id`, a bot in a group you forgot about —
is a nuisance rather than a compromised machine.

Setting the flags above removes those prompts. That is what makes the bridge genuinely useful
from a phone, because nobody is at the keyboard to approve anything — and it means your
Telegram account is effectively a shell on this host. Turn on two-factor authentication and put
a passcode on every device signed into it. [SECURITY.md](../SECURITY.md) is the longer version.

Each engine is independent; setting one does not set the other. A malformed value — a bare
string, a list, anything that is not a string per engine — grants no flags rather than
guessing, because the only safe direction to guess here is fewer permissions.

### `issue_queue` — object

Adds a work-queue line to the pinned dashboard and the morning digest, counting open issues by
lifecycle label across one GitHub owner's repositories.

```json
"issue_queue": {"owner": "your-github-user", "label_prefix": "orchestra"}
```

It counts `<label_prefix>:ready`, `:claimed`, `:running`, `:review`, `:human-review`,
`:blocked` and `:failed`, using the `gh` CLI — so `gh` must be installed and authenticated.
**Leave it out and the line simply does not appear**; nothing else changes.

Both values must look like what they are: `owner` a GitHub login (letters, digits and hyphens,
up to 39 characters), `label_prefix` letters, digits, `.`, `_` or `-`. Anything else turns the
feature off rather than being passed through — they are interpolated into a GitHub *search
expression*, and a value carrying a quote or a space changes what is being searched for.

### `carry_forward_repo` — string, `owner/repo`

Where the daemon files a carry-forward issue when a session runs `/carryforward` without having
recorded one itself.

Leave it out and no issue is created. That is not a failure: the carry-forward **file** is
written either way, and the file is what the automatic resume reads. The issue is the durable
copy for you.

---

## Environment variables

All but one of these are read **once, when the daemon starts**, so changing one needs a
`systemctl --user restart agent-telegram-bridge.service`. Set those in a systemd drop-in
(`systemctl --user edit agent-telegram-bridge.service`), not in your shell profile — the daemon
does not inherit your shell.

`TG_BRIDGE_TOPIC` is the exception: it is read by the **CLI**, per invocation. It goes in the
environment of the session running `tg-bridge`, and the daemon never sees it.

| Variable | Default | What it does |
|---|---|---|
| `TG_BRIDGE_SPAWN_MODEL` | `claude-opus-4-8[1m]` | Model for spawned claude sessions. Spawning with no explicit model rides the account default, which is how one model's disablement once broke every session at once. |
| `TG_BRIDGE_CODEX_MODEL` | `0` | Set to `1` to let `/model <alias>` switch a running **codex** session. It gates typing the slash command into the pane; it adds no process argument. Off by default because the switch is not verified against every codex build. |
| `TG_BRIDGE_TZ_OFFSET` | `3` | Hours from UTC for times shown to you. |
| `TG_BRIDGE_AUTOCF_PCT` | `60` | Context percentage at which a session is asked to carry forward before compaction. |
| `TG_BRIDGE_TOPIC` | unset | Pin the CLI to one topic. Resolution order is `--topic`, then this, then a `.tg-bridge-topic` file in the working directory — there is no `TMUX_PANE` fallback here; that is a separate mechanism the daemon uses to find a pane. **Read by the `tg-bridge` CLI on every invocation, not by the daemon** — so it belongs in the environment of whatever runs `tg-bridge`, and restarting the daemon neither sets it nor is needed to change it. |
| `TG_BRIDGE_DASH_POLL` | `60` | Seconds between fleet-dashboard rebuilds. |
| `TG_BRIDGE_CTX_POLL` | `30` | Seconds between context readings. |
| `TG_BRIDGE_LIFECYCLE_POLL` | `30` | Seconds between session lifecycle checks. |
| `TG_BRIDGE_SNAPSHOT_POLL` | `60` | Seconds between session snapshots. |

Run the daemon with none of them set until something specifically bothers you. The defaults are
what this has been running on.

---

## Where state lives

`~/.local/share/agent-telegram-bridge/`:

| Path | Contents |
|---|---|
| `topics/<id>/inbox.jsonl` | Every message received in that topic, append-only |
| `topics/<id>/cursor` | How many of those lines the session has consumed |
| `topics/<id>/outbox.jsonl` | A journal of what was SENT: timestamp, message id, and a sha256 — never the text |
| `topics/<id>/media/` | Downloaded images |
| `registry.json` | Topic → session bindings |

**Nothing here is rotated or archived.** After two and a half months of continuous use the
whole directory was 32 MB, so this has not been worth solving; it is stated so you are not
surprised by it in a year. Everything is plaintext and readable by your user account.
