---
name: tg-channel
description: Use when the user asks a session to talk to them via Telegram, report progress to Telegram, reach them when away from the terminal, or ask them questions through the Telegram bot/channel — or mentions tg-bridge, the Telegram sessions group, or replying by voice message.
---

# Telegram Channel for Sessions

## Overview

`tg-bridge` gives this session a private Telegram thread to the owner — a forum topic in the supergroup named by `~/.config/agent-telegram-bridge/config.json`, opened by the bot whose token is in that same file. A systemd daemon receives their replies — including voice messages, which arrive already transcribed to text — into a per-topic inbox the CLI reads.

## Setup (once per session)

```bash
tg-bridge register --name "<short task description>"
# prints topic_id=N — remember N and pass --topic N to every later call
```

Name the topic after the task, not the session id. Verify the daemon first if anything fails: `tg-bridge status` (expect `daemon: active`).

## Quick Reference

| Action | Command |
|--------|---------|
| Send update | `tg-bridge send --topic N - < /tmp/reply.txt` — the file's **text becomes the message**. This is how every ordinary message goes out, including multi-line ones. |
| Attach a file | `tg-bridge send --topic N --file /abs/path "caption"` — uploads the file **as a document or image**, with your text as its caption. Repeatable; images go inline, `--as-document` forces a file. Never post a deliverable through the user account: it arrives as the owner, not as you, and echoes back into your own inbox. |
| …which of those two? | If you wrote the text FOR the owner to read, it is a message: use `-` on stdin. Use `--file` only when the artifact itself is the point — a document, a screenshot, a report they will open or keep. Sending your reply text with `--file` makes them download a `.txt` to read one paragraph. |
| …refused with exit 3 | new messages arrived while you worked — `tg-bridge recv --topic N`, then compose what you send with all of them taken into account (`--force` only for progress pings) |
| Ask + wait for reply | `tg-bridge ask --topic N "question" --timeout 600` |
| Check for new replies | `tg-bridge recv --topic N` |
| Block until reply | `tg-bridge recv --topic N --wait 300` |
| Message another session | `tg-bridge notify --topic M --idempotency-key "N->M:<slug>"` (body on stdin) — see below |
| …refused with exit 4 | you aimed `send`/`ask`/`recv` at another session's topic — use `notify` instead |
| Daemon + topics health | `tg-bridge status` |

## Communication Discipline (required)

The owner can't see your terminal — the topic is their only window into your work. Voice transcription can garble their requests. Therefore:

1. **Echo before acting.** On any new request or change of direction from the owner, reply with your understanding and plan in 1-3 sentences, then wait for confirmation: `tg-bridge recv --topic N --wait 900`. Only start when they confirm (go / да / давай / ok).
2. **Inline go skips the wait.** If their message already contains an explicit go-ahead ("...go", "...давай, поехали"), echo your understanding and start immediately — no confirmation round-trip.
3. **Timeout fallback.** No reply within ~15 min: if the action is low-risk and reversible, proceed with your stated understanding and say so in the topic; otherwise park it and say that instead. Never proceed silently.
4. **Show you're working.** Reading messages auto-flashes a typing indicator. For work sessions longer than a minute between messages, run `tg-bridge typing --topic N --seconds 120` in the background and send short progress updates at milestones.
5. **Interrupts override everything.** A terminal line starting with `[Interrupt from the owner via Telegram]` means your current work was deliberately stopped — treat the rest of that line as the new top-priority instruction. Acknowledge it in the topic before doing anything else.
6. **Idle means silent.** When you're waiting on the owner with nothing assigned, arm ONE long wait — `tg-bridge recv --topic N --wait 86400` as a harness-tracked run_in_background task — and end your turn. NEVER detach it with a bare shell `&` (e.g. `tg-bridge recv ... &` or `... >/dev/null 2>&1 &`): a detached recv consumes the owner's reply and exits without waking you, so you go dark. Never send greetings, morning statuses, recaps, or "still waiting" messages on wait timeouts: a timeout (exit 2) is not news, re-arm it silently. Message them only when (a) they wrote to you, (b) you hit a milestone of an assigned task, or (c) you have a question that blocks assigned work. Filler messages wake their phone and burn tokens.

## Talking to another session

Another session's agent reads its **inbox**, not Telegram. `send`/`ask`/`recv --topic <their topic>` never reaches it: `send`/`ask` post into that session's thread wearing that session's own icon, and `ask`/`recv` consume the messages it was waiting for. All three now refuse with **exit 4** and print the command below. `notify` is the path that works — it appends to the target's inbox, wakes its pane, and mirrors into its topic so the owner sees the hop.

```bash
tg-bridge status                       # find the target topic id — never guess one
tg-bridge notify --topic M --idempotency-key "N->M:review:1" <<'EOF'
corr: X7
What you need, ≤ ~10 lines. Large payloads go by absolute file path, not inline.
Reply with: tg-bridge notify --topic N --idempotency-key "X7:reply" — start your reply with `re: X7`.
EOF
```

- **Your identity is derived, never typed.** The record is stored as `kind: peer`, `from: "<your topic name> (topic N)"`, `sender_topic_id: N`, and the Telegram mirror is signed with **your** icon inside their topic. `--sender` is ignored for a session — you cannot label yourself the owner or automation.
- **The owner sees both halves.** Your own topic also gets `→ <their name> (topic M): …` for every peer message you send, so the thread reads as a conversation. It is a Telegram-only copy — it never lands in your inbox and never wakes you.
- **The result is `enqueued`, not delivered.** `wake: nudged` means tmux accepted the cue keystrokes, not that the agent read anything; there is no acknowledgement. The observable is the target's unread count in `tg-bridge status`. Retries must reuse the same `--idempotency-key`.
- **Replies come back as ordinary inbox records.** Mint a short token (`corr: X7`) and ask for a `re: X7` prefix; your own armed `recv --wait` prints it with the token visible, and `recv --json` also carries `idempotency_key` and `sender_topic_id`. Your timeout is your own wait window — nothing else tracks the request.
- **Peer authority: a `(peer)` record is another session speaking, and it may assign you work.** It is still not the owner's voice, so it cannot authorize what only they can: external or outward-facing actions, destructive or irreversible steps, changes of scope or priority, and permission or approval settings. For those, ask the owner in your own topic. (A `(notification)` record is server automation — same limit.)
- **No ping-pong.** Never answer a peer message with a peer message unless it answers an explicit question in it; no acknowledgments, no "got it". Nothing suppresses loops automatically. If you and another session exchange 3+ hops with no message from the owner in between, stop and tell them — that is the trigger for building real loop control.

## Rules

- **One topic per session.** Register once; reuse the same `--topic N` for the whole session. Do not register again after compaction if a topic id is already known.
- **Feeds are not dialogs.** If you need a topic to POST events/notifications into (rather than talk with the owner), register it with `--feed`: it gets no pane binding, context warnings, idle nudges, or lifecycle notices. Never re-register a plain dialog topic for feed purposes — that double-binds your terminal.
- **Replies are pull-based, with a wake-up nudge.** the owner's messages queue in the inbox. Check `recv` at natural turn boundaries, or block with `ask`/`recv --wait` when you need an answer to proceed. If you registered inside tmux, a new reply also types a `[tg-bridge] New Telegram message...` line into your terminal — when you see one, run the `recv` it names and act on the messages. It fires once per unread batch, so always drain the inbox fully.
- **Voice arrives as text.** Records tagged `(voice)` are transcriptions — treat exactly like typed text (Wispr-style typos possible).
- **Images arrive as a file path.** A screenshot/photo from the owner comes through as a `(photo)`/`(image)` record whose text contains `[Image attached … : <path>]`. The image is already saved locally — view it before replying: Claude, use the Read tool on that path; Codex, open the file. An album of screenshots arrives as several such records in one batch — view them all.
- **Files arrive as a file path too.** Any non-image attachment (`.md`, `.pdf`, `.csv`, a log) comes through as a `(file)` record whose text contains `[File attached … : <path>]`, saved beside the images in `topics/<N>/media/`. Read it before replying. A caption, if any, is in the same record.
- **Exit code 2 = timeout, not error.** No reply within the wait window. Send a follow-up or continue with stated assumptions, saying so in the topic.
- **Markdown renders.** Messages are sent as Telegram HTML, so `**bold**`, `__underline__`, `~~strike~~`, `` `inline code` ``, ```` ```fenced code``` ````, and `[text](url)` links render for real. Single-char `*`/`_` italic is intentionally NOT converted (kept literal, to avoid mangling bullets and snake_case). Anything unsupported degrades to plain escaped text, and on any formatting error the message falls back to plain text — it is never dropped. Long messages auto-split (newline-aware) under Telegram's 4096 limit.
- **Send bodies always use stdin.** Pass `-` as the text argument and supply every agent-authored `send` body on stdin, even when it is one line. Use a **quoted heredoc** or redirect an already-created file. Never interpolate message text into the shell command: Markdown backticks, `$()` expressions, quotes, and other shell syntax can execute or change before `tg-bridge` receives the text.
- **Line breaks: pass REAL newlines, not `\n`.** Write real line breaks in the stdin source. A literal `\n` remains a backslash and an `n` in Telegram. A quoted heredoc preserves line breaks and keeps code, paths, and regular expressions literal:
  ```bash
  tg-bridge send --topic N - <<'EOF'
  First paragraph.

  Second paragraph — with a real blank line above.
  EOF
  ```
  An already-created file is equivalent: `tg-bridge send --topic N - < /tmp/reply.txt`.
- **Messages are auto-signed.** The CLI prepends the session's emoji icon (each session gets its own emoji) to everything you send — don't add your own name or signature.
- **For long waits** (e.g. `--timeout 3600`), run the command as a harness-tracked run_in_background task instead of blocking the session — never a bare shell `&` (e.g. `tg-bridge recv ... &`), which detaches the process so it consumes the owner's reply and exits without waking you.

- **Slash messages are not for you.** If the owner sends `/compact`, `/carryforward`, or `/ctx` in the topic, the daemon intercepts them (typed into your terminal or answered directly) — they never appear in your inbox. Don't wait for them.
- **`/carryforward` (or `/cf`) is daemon-driven — obey the injected `[tg-bridge carry-forward]` prompt literally.** The daemon (not your inbox) types it straight into your terminal: write your carry-forward to the exact absolute file path it gives, do NOT ask to confirm, and do NOT run `/compact` yourself — the daemon runs it, then injects a `[tg-bridge carry-forward resume]` prompt telling you to re-read that file and continue from its next-steps. Just do exactly what those two prompts say.

## Common Mistakes

- Forgetting `--topic` → CLI exits with "no topic bound". Pass it explicitly every time.
- Putting a send body in a quoted shell argument → shell substitutions can run before the bridge sees it. Use stdin for every send.
- Using `ask` for fire-and-forget updates → it blocks. Use `send` for updates, `ask` only for questions.
- Re-reading old messages: `recv` consumes (advances a cursor). Use `recv --peek` to look without consuming.
