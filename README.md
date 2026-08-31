# agent-telegram-bridge

Talk to the coding agents running on your machine from Telegram.

Each session gets its own thread in a Telegram forum group. It posts progress there, asks you
questions there, and reads your replies from there — including voice messages, which arrive
already transcribed. You can start a new session from your phone, interrupt one mid-task, or
ask what it is currently doing.

It drives **Claude Code** and **Codex**. Python 3.12, standard library only, no services to
sign up for beyond a Telegram bot.

```
        you, in Telegram                    your machine
   ┌────────────────────────┐        ┌──────────────────────────────┐
   │  Session A   ← thread ─┼────────┼→  daemon (systemd user unit) │
   │  Session B   ← thread ─┼──bot───┼→   │  single getUpdates poll  │
   │  General     ← thread ─┼────────┼→   ▼                          │
   └────────────────────────┘        │  topics/<id>/inbox.jsonl     │
                                     │      ▲                        │
                                     │      │ tg-bridge CLI          │
                                     │  agent session in tmux        │
                                     └──────────────────────────────┘
```

One Telegram topic ↔ one session. The daemon is the only thing that polls Telegram; sessions
pull their own messages out of a per-topic append-only log with the `tg-bridge` CLI.

---

## Start here

| | |
|---|---|
| **[docs/INSTALL.md](docs/INSTALL.md)** | Twenty minutes, most of it in Telegram. Start here. |
| **[SECURITY.md](SECURITY.md)** | Read before you install. The trust boundary is one number, and an admin bot sees every message in its group. |
| **[docs/CONFIGURATION.md](docs/CONFIGURATION.md)** | Every config key and environment variable. |
| **[docs/FEATURES.md](docs/FEATURES.md)** | What you can do from Telegram: every command, in detail. |
| **[docs/OPERATIONS.md](docs/OPERATIONS.md)** | Running it: services, logs, upgrades, what to do when it stops. |
| **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)** | Why it is built this way, and the decisions behind it. |
| **[docs/SPECIFICATION.md](docs/SPECIFICATION.md)** | Exact behaviour and every state-file schema — for forking. |
| **[AGENTS.md](AGENTS.md)** | For the agent, not for you: how a session should use the channel. |

---

## What it does

**Two-way, per session.** A session opens a thread, posts what it needs you to know, and blocks
on your answer when it needs one. You reply in the thread. Messages queue durably: a session
that is busy for an hour still gets everything you sent.

**Voice.** Send a voice message and the session receives text. A voice message beginning with
"stop" interrupts the session, the same as `!` on a typed message.

**Images.** Send a screenshot and the session gets a local path to it and can look at it.

**Files, both ways.** `tg-bridge send --topic N --file report.html` puts a deliverable in the
thread. Photos arrive inline; anything else arrives as a document.

**Start and stop sessions from your phone.** `/claude <name>: <task>` and `/codex <name>: <task>`
open a new session in a fresh tmux session, bound to a new thread. `/stop` interrupts, `/kill`
ends it, `/peek` shows the last lines of its terminal.

**A pinned dashboard.** One message in **General**, edited in place, listing every running
session with its context usage and cost. Produced by ordinary code — no model is involved and
it costs no tokens.

**It tells you when it breaks.** A watchdog alerts the General topic directly through the Bot
API if the daemon dies — the daemon cannot be the messenger for its own death — and again when
it recovers.

**Sessions can message each other.** `tg-bridge notify` delivers into another session's inbox,
wakes it, and mirrors the exchange into both threads so you can follow the conversation.

Full detail in [docs/FEATURES.md](docs/FEATURES.md).

---

## What it is not

- **Not multi-user.** One owner, one Telegram account, no roles. See
  [SECURITY.md](SECURITY.md).
- **Not a hosted service.** It runs on your machine, as your user, under your systemd.
- **Not a sandbox.** It decides *whether* a message reaches an agent. What the agent may then do
  is the agent's own permission model, plus one configuration choice you make deliberately.

---

## Requirements

- Python **3.12** — no third-party runtime dependencies
- **tmux** — sessions run in panes the daemon can type into
- **systemd** with a user instance
- Claude Code and/or Codex, installed and logged in
- A Telegram bot, and a supergroup with Topics enabled

---

## Licence

MIT. See [LICENSE](LICENSE).

Built for one person's daily use and released because the shape turned out to be generally
useful. Issues and pull requests are welcome; so is forking it and going your own way — the
specification exists for exactly that.
