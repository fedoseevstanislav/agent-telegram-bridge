# Features

Everything you can do from Telegram, and what happens on the machine when you do it.

Two rules make the rest make sense:

1. **One topic is one session.** The routing key is Telegram's `message_thread_id`.
2. **Messages are pulled, not pushed.** A message lands in that topic's `inbox.jsonl` and stays
   there until the session reads it. Nothing is typed into a session's terminal except short
   wake-up cues — so a busy session loses nothing, and no message content is ever injected as
   input the agent didn't ask for.

---

## Talking to a session

Write in its topic. That is the whole interface.

**Voice** is transcribed before it reaches the session (needs `openai_api_key`, see
[CONFIGURATION.md](CONFIGURATION.md)); the session sees text and a `(voice)` tag. Transcription
can garble words — sessions are told to echo their understanding back before acting on anything
consequential.

**Photos and image documents** are downloaded to `topics/<id>/media/` and delivered as a record
carrying the local path plus your caption. The session opens it from there. An album arrives as
several records in one batch. Non-image documents are ignored.

**`!<text>`** interrupts: the session's current turn is stopped, and your text is delivered as
its next instruction, marked as an interrupt. A **voice message beginning with "stop"** does the
same, because a transcript cannot carry a `!`.

If the session registered inside tmux, a new message also types a short `[tg-bridge] New
Telegram message…` cue into its pane — once per unread batch, never the message itself.

### From the session's side

```bash
tg-bridge register --name "short task description"   # creates the topic, prints its id
tg-bridge send --topic N - < /tmp/reply.txt          # the file's text becomes the message
tg-bridge send --topic N --file ./report.html "here" # uploads the file, text is its caption
tg-bridge ask --topic N "which one?" --timeout 600   # send, then block for a reply
tg-bridge recv --topic N --wait 86400                # block until something arrives
tg-bridge typing --topic N --seconds 120             # keep the typing indicator alive
tg-bridge status                                     # daemon health and every topic
```

`send` refuses with **exit 3** when that topic has unread messages — you wrote while it was
working, and it should read you before it answers. `--force` overrides for progress pings.

---

## Control commands

Anything starting with `/` in a topic is a command, never inbox content.

| Command | What it does |
|---|---|
| `/help` | The command list. Works in General too. |
| `/sessions` | Every tmux pane running an agent — not just registered ones — with name, bound topic, context %, cost, unread flag. Codex panes tagged `[codex]`; unconnected ones flagged. |
| `/ctx` | This session's context-window usage. |
| `/usage` | Both account meters in one message — Claude (5h, week, Fable week) and Codex — every window as **% left** with its reset time, from any topic. `/usage claude` / `/usage codex` show just that one line; `/usage codex` from a Codex topic scopes to that session's rollout. |
| `/stop` | Interrupt the current turn (Escape into the pane). |
| `/kill` | End the session. Asks first; reply `yes` within 60 seconds. Anything else cancels and is passed through to the session. |
| `/peek` | The last ~25 visible lines of the session's terminal. |
| `/claude <name> [@~/path][: <task>]` | Start a Claude Code session. |
| `/codex <name> [@~/path][: <task>]` | Start a Codex session. |
| `/carryforward`, `/cf` | Have the session write a durable handover, then compact, then resume from it. |
| any other `/command` | Typed into the session's pane and executed, with a confirmation reply. |

Plain text in **General** gets an immediate hint instead of vanishing: no session reads General.

**Topic icons.** A new topic gets a forum icon matched to its name — 💻 for a build, 📆 for a
meeting, 🧠 for graph or memory work, 👮‍♂️ for security — so the topic list can be scanned by
subject. A name that matches nothing keeps Telegram's default rather than being given a guess.
Existing topics are caught up with `tg-bridge retheme`, which prints what it would change and
writes only with `--apply`. Bots may only use Telegram's free icon set, so the palette is
whatever `getForumTopicIconStickers` offers.

The per-session **signature emoji** in message headers is a second, separate icon, and it is
matched from the same name rules: a build topic signs 💻, a security topic 👮‍♂️. When another
live topic already carries that emoji, a later rule the name also matches is used instead, and
a name matching nothing falls back to the old palette of visually distinct glyphs. Existing
topics are caught up with `tg-bridge reicon` — dry by default, `--apply` writes the registry.
It changes only icons still from that generic palette, so an icon set by hand — or already
matched to its subject — stays; when two topics can only end up with the same subject emoji the
dry run says so on the line and applies it anyway, a shared subject icon being more useful than
a random one.

### Starting a session

`/claude review-auth @~/src/api: check the token refresh path` opens a tmux session named
`review-auth`, working directory `~/src/api`, and gives it that task. The bootstrap makes it
register its own topic, echo back what it understood, and wait for your go — an explicit "go"
inside the task skips the wait. The task is optional: `/claude notes` starts a session that
greets you and waits.

The daemon checks 20 seconds later that the session is still running and reports to General if
it is not. Name collisions and bad paths are refused immediately.

**Permissions.** By default a spawned session keeps its ordinary approval prompts, so it cannot
edit files or run commands until you approve that at the terminal. That is usually not what you
want from a phone — but it is the right default, and turning it off is a decision you make in
`spawn_flags` after reading [SECURITY.md](../SECURITY.md).

The model is pinned explicitly rather than riding the account default, because a deprecated
default breaks every spawn at once — which is exactly how one model's disablement once took out
a whole fleet.

### Carry-forward

A model cannot compact itself, so the daemon acts as its hands. `/carryforward` makes the
session write a dense handover — current state and concrete next steps — to a file in the state
directory, records it to a GitHub issue (creating one if there is no focus issue), types
`/compact`, clears the resume modal, waits for compaction to actually finish, and then tells the
session to re-read the file and continue.

Any message in the topic during that flow aborts it. Claude Code only.

---

## Things that run on their own

**Fleet dashboard.** One pinned message in General, edited in place: every running session with
context and cost, plus account usage. Rebuilt every 60 seconds and edited only when the content
actually changes. Deterministic code — no model, no tokens. Delete the message and the daemon
recreates it.

**Context warnings.** A session's topic is warned when its context usage first crosses 20%, then
every further 10%. The mark re-arms when usage drops, so a compact does not silence it.

**5-hour limit → low priority.** The limit is account-wide, so when it is reached every live
Claude session is switched to low-priority mode automatically — the same `/low-priority` you
would type yourself — and each topic gets one line saying so and when the window resets. A
session that is mid-turn gets it once it settles, retried a few times over half a minute and
then left alone until the window resets. At most one switch per pane per window, and Codex
sessions are never touched.

**Morning digest.** One summary a day to General: sessions with their overnight cost deltas,
sessions that ended, account usage, daemon health. Also zero tokens.

**Restore after a reboot.** Sessions come back automatically, and a big, old session is resumed
**from its summary** rather than re-reading its whole history — past a few hours the model's
cache has expired, so the full re-read costs a great deal and buys nothing. The restore message
names the sessions that were. A whole restore has one time budget for this; if a long restore
uses it up, the sessions after that come back the old way rather than holding up the fleet. When
you revive a session yourself by writing to it, you are still asked which you want.

**Lifecycle.** When a registered session's pane dies, its topic gets a "session ended" notice and
is closed, and the registry is stamped — which stops nudges and warnings for it. Reopening the
topic offers to revive the session where that is possible.

**Watchdog.** If the daemon dies, a timer alerts General **directly through the Bot API**, with a
`systemctl status` excerpt — a daemon cannot report its own death. One alert per outage, one on
recovery. Meanwhile Telegram queues your messages (the poll is offset-based, so nothing is lost)
and outgoing `tg-bridge send` keeps working, because the CLI talks to the Bot API itself.

---

## Sessions talking to each other

A session's agent reads its **inbox**, not Telegram. So posting into another session's topic does
not reach it — and worse, `recv` there would eat the backlog it was waiting for. Those commands
refuse with **exit 4** and print the right one:

```bash
tg-bridge notify --topic 8265 --idempotency-key 'review:1' <<'EOF'
corr: X7
Seats finished for issue #125. Reply with: notify --topic <mine> --idempotency-key "X7:reply"
EOF
```

`notify` appends to the target's inbox, wakes its pane, and mirrors the message into both
topics — signed with the **sender's** icon, so a peer message can never appear wearing the
recipient's signature. Your own topic shows `→ <them>: …` so both threads read as whole
conversations.

The name is the honest ceiling, and so is every word of the result. `status: enqueued` means the
record is written and fsynced — **process-crash durability with a fsynced file**, so a sender
killed after exit 0 cannot lose it. It is not more than that: when the append is what creates the
inbox file, the parent directory is fsynced too, but the directories above it are made by writers
that sync nothing, so there is **no OS-crash pathname guarantee**. That distinction is the one a
reader would most reasonably assume away, which is why it is spelled out.

`wake: nudged` means tmux accepted the keystrokes, not that anyone read anything. Nothing
acknowledges receipt — the observable is the target's unread count in `tg-bridge status`. Retries
must reuse the same idempotency key; a repeat is a no-op that appends nothing.

The ownership guard that produces exit 4 is a tripwire against a known mistake, not a security
boundary — anyone with host access can type into any pane. An operator who needs the old
behaviour can bypass it with `env -u TMUX_PANE tg-bridge …`.

**Feed topics** (`register --feed`) are for events rather than conversation: no pane binding, so
no context warnings, idle nudges, or lifecycle notices.

---

## Identity

All sessions share one bot account, so the bridge signs for them. Each session gets a distinct
emoji at registration; its messages carry an `<icon> <name>` header, and the same icon marks it
in the dashboard and `/sessions`. Daemon machinery speaks with a `⚙️` prefix, so you can always
tell the bridge from a session at a glance.
