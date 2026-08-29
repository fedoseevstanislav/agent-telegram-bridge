# Operations

Running it day to day: what is installed, where to look when something is wrong, and how to
upgrade without losing anything.

## What is running

Four services and three timers, all as **your user**, none as root.

| Unit | Type | What it is |
|---|---|---|
| `claude-telegram-bridge.service` | long-running | The daemon. The **only** thing that polls Telegram. |
| `claude-telegram-bridge-watchdog.service` + `.timer` | oneshot, every 5 min | Alerts General if the daemon is down, and again when it recovers. |
| `claude-telegram-bridge-digest.service` + `.timer` | oneshot, daily 06:00 UTC | The morning digest. `Persistent=true`, so a missed run fires at next boot. |
| `claude-telegram-bridge-model-watchdog.service` + `.timer` | oneshot, every 5 min | Notices when a session's model is not what it was started with. |

```bash
systemctl --user status claude-telegram-bridge.service
systemctl --user list-timers 'claude-telegram-bridge*'
journalctl --user -u claude-telegram-bridge.service -f
```

Two things in the main unit are deliberate and should not be "cleaned up":

- **`KillMode=process`.** Without it, restarting the daemon kills the tmux server and every
  session it ever started, because they are in its control group. Your sessions are user work;
  they must outlive a bridge restart. The restart logs `Found left-over process (tmux: server)
  … Ignoring` — that is this working, not a fault.
- **`StartLimitIntervalSec=120` with `RestartSec=5`.** systemd's default start-limit window is
  shorter than five restarts at five seconds, so a crash-looping daemon would never reach
  `failed` and `OnFailure=` would never fire. The watchdog would stay silent through exactly the
  outage it exists for.

## When it stops working

**Nothing arrives, no errors anywhere.** In order of likelihood:

1. The bot is not an administrator of the group any more. Telegram bots do not see ordinary
   messages otherwise, and nothing reports this. [INSTALL.md](INSTALL.md) has the check.
2. The daemon is down. `systemctl --user status`, then the journal.
3. Something else is polling the same token — a second daemon, or a manual `getUpdates` left
   running. Telegram allows exactly one consumer per token; the loser sees `409 Conflict` in the
   journal.

**A session stopped responding but the bridge is fine.** Check the session itself: `/peek` shows
its terminal, `/sessions` shows whether its pane is still alive. A dead pane should already have
posted a "session ended" notice and closed the topic.

**Messages arrive but the session never notices.** The wake cue is best effort. If the session
is not running a `recv --wait`, nothing will nudge it into reading — the messages are safe in
the inbox and it will see them at its next `recv`. `tg-bridge status` shows the unread count per
topic, which is the ground truth.

**Voice arrives as `[voice message — transcription failed: …]`.** The bracket says why.
`no OpenAI key` means it is not configured; anything else is the transcription service.

## Nothing is lost while the daemon is down

Telegram queues updates and the poll is offset-based, so a restart delivers everything from
where it left off. Outgoing `tg-bridge send` keeps working the whole time, because the CLI talks
to the Bot API directly and never goes through the daemon.

## Upgrading

```bash
cd /path/to/your/clone
git pull
systemctl --user restart claude-telegram-bridge.service
```

Two things to know before you do it:

- **The skill is a symlink into the clone.** If you installed the Claude Code skill, `git pull`
  changes the instructions **every running session** sees, immediately — not just sessions
  started afterwards. That is usually what you want for a fix, and occasionally a surprise: a
  session mid-task can pick up a newly documented flag it has never seen.
- **Restarting the daemon does not restart your sessions**, by design (`KillMode=process`
  above). A session started before the upgrade keeps running the agent it was started with.

If you want the stricter discipline — running from a frozen copy so that `git pull` deploys
nothing until you say so — extract a release to its own directory, point `ExecStart` at it with
a drop-in, and make the launcher a wrapper around that copy. Then merging and deploying are
separate acts, which is worth the ceremony once more than one person depends on it.

## State, backup, and size

Everything is under `~/.local/share/claude-telegram-bridge/`. [CONFIGURATION.md](CONFIGURATION.md)
covers the paths you are likely to look at and [SPECIFICATION.md](SPECIFICATION.md) §3 adds the
daemon's own bookkeeping files; neither is exhaustive — the daemon creates state as features
need it, so read the directory, not a list. The parts worth knowing:

- `topics/<id>/inbox.jsonl` — every message received, append-only, plaintext
- `topics/<id>/outbox.jsonl` — a journal of what was **sent**: timestamp, message id, and a
  sha256. Not the text. Nothing reads it back; it exists so you can prove what went out.
- `registry.json` — the topic↔session bindings. Losing this orphans topics; it does not lose
  messages.
- `offset` — the Telegram ack cursor. Losing it replays or skips recent updates.

**Nothing here is rotated, archived, or pruned.** Measured growth has been modest, so pruning
has not been worth the added machinery; it is stated here so you are not surprised later. If
you do prune, prune whole `topics/<id>/` directories for sessions that ended — never
truncate an `inbox.jsonl`, whose line count the sibling `cursor` file indexes into.

To back up: stop the daemon, copy the directory, start it again. It is all plain files.

## Removing a session cleanly

`/kill` in its topic. That interrupts the session, kills the pane, posts the ended notice, and
closes the topic. Deleting a topic in Telegram does not do any of that — the daemon just stops
having anywhere to write.
