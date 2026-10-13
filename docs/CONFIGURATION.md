# Configuration

Connection settings live in `~/.config/agent-telegram-bridge/config.json`.

(Two exceptions, both for one optional key: `openai_api_key` also has legacy fallback sources,
described with that key below. Optional host configuration in the state directory is described
under [Codex context cues](#codex-context-cues).)

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

### Multiple groups and bots

The top-level `bot_token` and `chat_id` always define the default group. Its name is `work`,
or the optional `default_group` string. Add other forum supergroups with `groups`:

```json
{
  "bot_token": "123456789:AAE...",
  "chat_id": -1001234567890,
  "owner_id": 987654321,
  "default_group": "work",
  "groups": [
    {
      "name": "personal",
      "chat_id": -1009876543210,
      "bot_token_file": "~/.config/agent-telegram-bridge/personal-token"
    }
  ]
}
```

Names are unique lowercase slugs: start with a letter, then use letters, digits, underscores
or hyphens, up to 64 characters. The default name and chat id must not appear again in the
list; other chat ids must also be unique negative integers. Each entry requires exactly one
of `bot_token` (a non-empty string without embedded control characters) or `bot_token_file`.
A token file contains the plain token, with an optional trailing newline. Like the config,
it must be a regular file owned by the service user with no group/other permission bits
(`chmod 600`). Checks and reads use the same open descriptor. Invalid entries prevent startup;
diagnostics identify the field without showing the token.

One bot may serve several groups: repeat its token in their entries. The daemon runs one
`getUpdates` poller per distinct token and maps updates to groups by chat id. Chats outside
that bot's configured groups are ignored. In multi-group mode every bot has a cursor keyed
by its token fingerprint, so changing the default bot or promoting an existing bot keeps
their cursors separate. On the first multi-group startup, the current default bot inherits
the existing `offset` before any extra poller starts; `offset` remains its compatibility mirror. The same `owner_id` authorizes ingress in every
group; group membership grants no authority. Restart the daemon after changing the group list.

Telegram thread ids repeat between groups. The bridge therefore uses a **topic key**:
default-group thread 7 stays `7`; personal-group thread 7 is `personal:7`. These keys identify
registry entries, topic folders, inboxes, cursors, outboxes, pending reopen questions,
carry-forward files, warning/compaction state, `.tg-bridge-topic` and `TG_BRIDGE_TOPIC`.
Existing default-group state needs no migration.

Move an existing session with `tg-bridge move --topic 7 --to personal`; it prints the new
key and retains the session and unread inbox. The running daemon processes the move on its
lifecycle poll. See [moving a topic](FEATURES.md#moving-a-topic-between-groups) for refusals,
old-key redirects, and shared-cwd bindings.

```sh
tg-bridge register --group personal --name Learning --bind
tg-bridge send --topic personal:7 - < reply.txt
tg-bridge recv --topic personal:7
tg-bridge notify --topic personal:7 --sender scheduler --idempotency-key reminder-1 < reminder.txt
```

Every topic operation uses that group's token and chat id, and sends only the numeric thread
id to Telegram. `notify` can cross groups: it writes and wakes the target's inbox, mirrors to
the target's group, and echoes in the sender's own group. Fleet dashboard, morning digest,
reboot/recovery summaries and daemon health alerts remain in the default group's General;
their fleet data covers all groups. Spawn failure alerts go to the requesting group's General.
Model watchdog alerts stay in their own topic and identify non-default topic keys.

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

### `revive_model_map` — object

Which model a sleeping Claude session wakes on. A revive normally resumes the session on the
model its transcript last recorded; this maps that model to another one.

```json
"revive_model_map": {
  "claude-opus-5": "claude-opus-5-5[1m]"
}
```

Use it when the fleet moves to a newer model: every parked session that last ran the old one
wakes on the new one, keeping its own effort, without being woken now to switch. It is also
the only way a revived session gets the 1M window back, because the transcript records the
model without the `[1m]` suffix. Sessions whose model is not a key wake as before, and a
missing or malformed map changes nothing.

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
| `TG_BRIDGE_AUTOCF_PCT` | `50` (`60` when the carry-forward cycle is on) | Context percentage at which the daemon compacts the session. |
| `TG_BRIDGE_CARRY_FORWARD` | `0` | `1` restores the pre-2026-09-21 carry-forward cycle (write → GitHub issue → `/compact` → resume-from-next-steps) **and** its 60 % threshold. Off, the daemon types `/compact` at the threshold and the session continues from the built-in summary. |
| `TG_BRIDGE_TOPIC` | unset | Pin the CLI to one topic key (`7` or `personal:7`); old keys follow move pointers. Resolution order is `--topic`, then this, then the pane binding for a moved live session, then a `.tg-bridge-topic` file in the working directory. Ordinary unmoved sessions retain flag/env/file resolution. **Read by the `tg-bridge` CLI on every invocation, not by the daemon** — so it belongs in the environment of whatever runs `tg-bridge`, and restarting the daemon neither sets it nor is needed to change it. |
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
| `topics/<key>/inbox.jsonl` | Every message received in that topic, append-only |
| `topics/<key>/cursor` | How many of those lines the session has consumed |
| `topics/<key>/outbox.jsonl` | A journal of what was SENT: timestamp, message id, and a sha256 — never the text |
| `topics/<key>/media/` | Downloaded images |
| `registry.json` | Topic key → session bindings |
| `topics/<source key>.move.json` | Operator move request and daemon result, retained at the source key |
| `topic-locks/<key>.lock` | Shared operation / exclusive move lock, outside renamed topic folders |
| `offset` | Default bot’s getUpdates cursor; compatibility mirror when groups are configured |
| `offsets/<bot fingerprint>` | Each bot’s authoritative cursor in multi-group mode (SHA-256 of token; no token text) |
| `codex-context-cue.json` | Optional private host configuration for Codex context cues |

**Nothing here is rotated or archived.** After two and a half months of continuous use the
whole directory was 32 MB, so this has not been worth solving; it is stated so you are not
surprised by it in a year. Everything is plaintext and readable by your user account.

### Codex context cues

To type a reminder after a listed Codex session compacts, create
`~/.local/share/agent-telegram-bridge/codex-context-cue.json` on the host. Keep this file private
and out of the repository. The daemon reads it fresh at each context poll; no restart is needed.
An absent, unreadable or malformed file disables the cue. Each distinct configuration problem
is logged at most once during the daemon's lifetime; absence is silent.

This example uses synthetic paths and names:

```json
{
  "cue": "[tg-bridge] Compaction is complete. Re-read your seat's context.",
  "name_prefixes": ["Team"],
  "seats": [
    {"folder": "/srv/agents/seat-a"},
    {
      "folder": "/srv/agents/seat-b",
      "rollout_cwds": ["/srv/agents/seat-b", "/srv/agents/runtime-b"],
      "name": "Team Seat B"
    }
  ]
}
```

`cue` is a nonempty string, typed verbatim. `name_prefixes` is a list of nonempty strings;
the registry name must start with one of them. `seats` is a list of objects with an absolute
`folder` matching the registry cwd. Optional `rollout_cwds` is a list of absolute paths and
defaults to `[folder]`. Optional `name` is a string requiring an exact registry-name match.

The seat must be active, its pane alive, and its folder's `.tg-bridge-topic` must contain the
registered topic id. The live root rollout must match the registry session id and one of the
allowed rollout cwds; native sub-agent rollouts are excluded. Identity is checked again before
typing and Enter. Historical compactions are baselined on the first eligible poll, successful
typing is deduplicated through the registry cursor, and unsuccessful typing is retried.
