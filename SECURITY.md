# Security model

Read this before you install. This project connects a Telegram chat to coding agents running
on your machine. That is a useful thing and a dangerous one, and the difference is entirely in
how you configure it.

## What the bridge actually does

A Telegram bot receives messages. The daemon on your host reads them and, depending on the
message, appends it to a per-topic file, types it into an existing terminal pane, or **starts a
new agent session in a new tmux pane**. Text that arrives in Telegram becomes input to a
program with access to your files.

So the security question is not "is the transport encrypted" (Telegram's is). It is: **who is
allowed to put text into that pipe, and what is the program on the other end permitted to do?**

## The trust boundary is one number

`owner_id` in `~/.config/claude-telegram-bridge/config.json` is a Telegram user id. It is the
whole authorization model. A message is acted on when its sender id equals that number.

Consequences worth stating plainly:

- **Group membership grants nothing.** Being in the supergroup does not make someone trusted.
  If `owner_id` is missing or malformed the daemon refuses ingress outright rather than falling
  back to trusting the group — see `valid_owner_id` in `bridge/common.py`. The fallback that
  would feel helpful here is the one that would hand your shell to a stranger.
- **`owner_id` is a user id, not a username.** Usernames can be released and re-registered by
  someone else; numeric ids cannot.
- **Forum service events are authorized too.** "Topic closed" and "topic reopened" arrive as
  ordinary updates and cause real state changes, including reviving a session. They are checked
  against the same owner id, plus the bot's own id for its own echoes
  (`service_event_is_trusted` in `bridge/daemon.py`).

## The bot must be a group administrator, and that is not a small grant

Telegram bots run in privacy mode by default, which means they do not see ordinary messages —
only commands and replies. The bridge therefore needs the bot promoted to administrator in the
supergroup, with **Manage Topics**.

The failure mode if you skip this is silent: the bot receives nothing, no error appears
anywhere, and the bridge simply never reacts. The failure mode if you *do* it is that the bot
can read every message in that group. **Use a supergroup created for this purpose and nothing
else.** Do not add the bot to a group you also use for anything real.

## Who can write the clone

The installer copies no code. The launcher it writes execs `bridge/cli.py` inside your clone,
and every systemd unit's `ExecStart` points back into it — so **the clone is the deployment**,
and anyone who can write those files decides what the bridge runs as you.

`scripts/install.sh` checks this before it writes anything. It refuses a runtime tree that is
world-writable, group-writable in a group with a member other than you, owned by another
account, or that contains a symlink — and it names who can write what. It stays quiet for the
ordinary `0664` that a umask of `002` produces in a private per-user group, because there the
group has exactly one member and demanding a `chmod` would protect nobody.

**What that check does not establish**, stated because a security paragraph that overstates its
guarantee is worse than none. It reads ownership and Unix mode bits at one instant. A POSIX ACL
can grant a named account write access the group bit does not reveal. A process that opened a
file while it was writable keeps that handle afterwards. Root, anything holding
`CAP_DAC_OVERRIDE`, and whoever controls a network or FUSE backing store are outside it
entirely, as are the directories above your clone. And nothing stops a `chmod` five minutes
later. It is a gate at install time, not a property that holds forever.

Worth checking on a shared host: `getent group $(id -gn)` tells you who else is in your primary
group. A service account in there — a web server user is the common one — can rewrite the
bridge's code, and through the skill symlink the instructions your running sessions read.

## Spawned sessions and their permissions

When the daemon starts or revives an agent, the command is built by `engine_launch` and
`_resume_launch` in `bridge/daemon.py`, which take their flags from `spawn_flags` in your
config. The permissiveness of that command is the second half of your threat model, and it is a
deliberate configuration choice:

- A **constrained** launch leaves the agent's normal approval prompts in place. A message that
  reaches the agent still cannot edit files or run commands without you approving it in the
  terminal. This is the default for a fresh install, and it is the right default: it means a
  mistake in the first half of the model — a wrong `owner_id`, a bot in the wrong group — is
  not immediately a compromised machine.
- An **unattended** launch (`--dangerously-skip-permissions` for Claude Code,
  `--dangerously-bypass-approvals-and-sandbox` for Codex) removes those prompts. It is what
  makes the bridge genuinely useful from a phone, because nobody is at the keyboard to approve
  anything. It also means anything that gets past `owner_id` runs unattended on your host.

Choose the second only when you have understood the first. If you switch, the honest way to
think about it is that your Telegram account has become a shell on that machine — so the
account needs the protection a shell deserves: two-factor authentication enabled, and a
passcode on every device that is signed in.

## Secrets

- The bot token lives in the config file. Nothing else is required to impersonate your bot, so
  treat that file as a credential: `chmod 600`, and never commit it.
- The optional voice-transcription key lives in the same file. That file and the sanctioned
  secrets file are both **permission-checked before they are read, on every read** — the loader
  refuses one that is world- or group-readable, or owned by another user, and says why
  (`_secret_file_rejection`, `bridge/common.py`). On every read rather than once at install
  time, because the config also holds `spawn_flags`: whoever can write it chooses what runs as
  you the next time an agent is launched (#245). One source is deliberately not checked: the
  legacy `env.vars` fallback, which is being removed and warns loudly on every use. If you are
  still relying on it, that warning is the point.
- A rejected key is never quoted back. Not decoration: a token containing a stray newline makes
  the HTTP layer raise an exception whose *message contains the whole token*, and that message
  was being written into the inbox and mirrored back into the chat.
- `redact_secrets` blanks key-shaped strings out of that transcription-failure path
  specifically. It is **not** a global filter — the daemon's ordinary `log()` writes what it is
  given — so treat it as the fix for one known incident, not as a safety net for a new log line
  you write. Keep the secret out of the string instead.
- Nothing in this repository should ever contain a real token. If you fork it, keep it that
  way.

## What is not defended

Stated so you are not surprised:

- **The Telegram account itself.** Compromise it and you have the bridge. There is no second
  factor here; `owner_id` is an identity check, not an authentication.
- **Content-level filtering.** A trusted sender's message is passed through as-is. There is no
  attempt to detect a hostile instruction inside otherwise-legitimate text, including text you
  forward from somewhere else. Forwarding an untrusted document into a topic where an
  unattended agent is listening is you handing that document your permissions.
- **Multi-user use.** One owner. There are no roles, no per-topic permissions, and no audit
  trail designed to be shown to anyone but you.
- **The host — and anyone who shares a group with you on it.** Files under
  `~/.local/share/claude-telegram-bridge/` hold the full message history in plaintext. Anyone
  with your user account can read them, and that much is by design.

  What is *not* by design, and is worth checking on a shared machine: the owner pin governs
  what arrives from Telegram, and nothing that arrives by any other route. A local account that
  can **write** your state directory can append a record to a topic's inbox, and the next
  `recv` hands it to your agent exactly as if you had sent it. A local account that can write
  the directory your launcher lives in can replace the `claude` or `codex` binary your next
  spawn invokes by name. The daemon now creates its state `0700`, takes group and other write
  off the state directory *and everything inside it* before it reads any of it, runs under
  `UMask=0077`, and the installer refuses to write a launcher, a unit or the skill link into a
  directory somebody else can write.

  The check is on the whole path, not the leaf. Write permission on a **directory** is
  permission to unlink and rename any entry in it, whatever that entry's own mode says — so a
  `0700` state directory inside a `0775` parent is not private at all: a peer renames it aside
  and puts their own there. The installer refuses on any writable ancestor, and the daemon
  names ones it finds above its state directory rather than changing a directory that may not
  be its owner's to change. The sticky bit is the exception the walk knows about: `/tmp` is
  `1777` on purpose, and write there does not let you remove somebody else's entry.

  The config is judged on the descriptor that was opened, not on the path. `stat` then `open`
  resolve the same name twice, and a peer who can write a directory on the way can change what
  it means in between, so the file that passed the check is not the file that gets parsed.

  None of that helps if you widen the modes afterwards. `umask 002` — common on shared hosts,
  and the default on some — is what usually produces the exposure, and it produces it silently.

## Reporting a problem

Open an issue if it is not sensitive. If it is, say so in the issue without the details and ask
for a private channel first.
