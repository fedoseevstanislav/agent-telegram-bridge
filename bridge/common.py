"""Shared config, state paths, and Telegram Bot API helpers. Stdlib only."""

import fcntl
import hashlib
import html as _html
import json
import os
import re
import socket
import stat
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass

CONFIG_PATH = os.path.expanduser("~/.config/agent-telegram-bridge/config.json")
STATE_DIR = os.path.expanduser("~/.local/share/agent-telegram-bridge")
OPENCLAW_CONFIG = os.path.expanduser("~/.openclaw/openclaw.json")
# OpenClaw's file-backed secret provider — the sanctioned source since their #706 migration.
OPENCLAW_SECRETS = os.path.expanduser("~/.openclaw/secrets.json")


@dataclass(frozen=True)
class WakeClaim:
    """Append-time cursor generation that owns the next pane wake."""

    cursor: int


# api.telegram.org over IPv6 is unreachable from this host (connect refused) while IPv4
# works. Prefer A records for telegram so a flipped resolver order can never park a
# request on the dead IPv6 route. Scoped to telegram — every other host is untouched.
_real_getaddrinfo = socket.getaddrinfo


def _prefer_ipv4_for_telegram(host, *args, **kwargs):
    res = _real_getaddrinfo(host, *args, **kwargs)
    name = host.decode() if isinstance(host, bytes) else host
    if isinstance(name, str) and name.endswith("telegram.org"):
        v4 = [r for r in res if r[0] == socket.AF_INET]
        if v4:
            return v4
    return res


if getattr(socket.getaddrinfo, "__name__", "") != "_prefer_ipv4_for_telegram":
    socket.getaddrinfo = _prefer_ipv4_for_telegram


def valid_owner_id(value):
    """Return True only for a concrete positive Telegram user id.

    ``bool`` is deliberately rejected even though it is an ``int`` subclass in Python.
    Failing this check must disable ingress, never fall back to trusting the group.
    """
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def load_config():
    # Every read, not once at install time. `scripts/install.sh` checks this file's mode when
    # it runs and never again, so a config that BECOMES group-writable — a later `chmod`, a
    # restore from a backup, an install that was never run — was trusted for the life of the
    # deployment. What that buys a peer account is not merely the token: `spawn_flags` is
    # interpolated into the command used to launch an agent, so a writer of this file chooses
    # what runs as the owner on the next spawn (#245). Same rejection the secret-source files
    # already use; the owner gate cannot help here, because this path never touches Telegram.
    #
    # Judged on the open descriptor, not on the path. `stat(path)` then `open(path)` are two
    # resolutions of the same name, and a peer who can write any DIRECTORY on the way to the
    # config — `~/.config` is group-writable on more hosts than not — can swap what the name
    # means in between and be read after passing the check. `fstat` on the handle already
    # held asks about the bytes actually about to be parsed, so there is no interval to win.
    handle, rejection = _open_secret_file(CONFIG_PATH)
    if rejection:
        raise SystemExit(
            f"refusing to read {CONFIG_PATH}: it {rejection}.\n"
            f"    It holds a bot token and the flags used to launch agents, so anyone who can\n"
            f"    write it chooses what runs as you. Fix with:\n"
            f"        chmod 600 {CONFIG_PATH}")
    with handle as f:
        cfg = json.load(f)
    for key in ("bot_token", "chat_id", "owner_id"):
        if key not in cfg:
            raise SystemExit(f"config missing {key!r} in {CONFIG_PATH}")
    if not valid_owner_id(cfg["owner_id"]):
        raise SystemExit(f"config has invalid 'owner_id' in {CONFIG_PATH}")
    return cfg


def state_path(*parts):
    path = os.path.join(STATE_DIR, *parts)
    # 0700, not the umask's answer. Under the common `umask 002` these become group-writable,
    # and a peer account that can append one well-formed line to `topics/<id>/inbox.jsonl`
    # has put text in front of an agent without passing the Telegram owner gate at all — the
    # authorization model routed around through the filesystem (#245). `exist_ok` means an
    # existing tree keeps its modes; `secure_state_tree` handles those.
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    return path


def secure_process_umask():
    """Make this process create private files, whatever umask it inherited. Returns the old one.

    `UMask=0077` in the unit files is the same fix one layer out, and it is the layer that does
    not arrive on upgrade: the units are installed files, so `git pull` and a release cut both
    leave a running deployment on whatever umask it already had. On the host this was written
    for, one of four bridge units had the directive and the other three ran at `0002`, quietly
    creating group-writable state next to state the daemon had just narrowed.

    Called at every entry point rather than at import, because import-time side effects reach
    anything that merely reads this module — including the test suite, which would then be
    measuring this call instead of the code under test.
    """
    return os.umask(0o077)


def secure_state_tree():
    """Take group and other WRITE off the state directory AND everything inside it, at start.

    The root alone is not the boundary. A tree created under `umask 002` before this existed
    has `topics/<id>/` at 0775 and `inbox.jsonl` at 0664; narrowing only the root leaves every
    one of those writable, and the moment the root's mode is relaxed for any reason the whole
    history is appendable again. The inboxes are the actual target — a peer who can add one
    well-formed line to one of them has put text in front of an agent without passing the
    Telegram owner gate (#245).

    Only the write bits. Read is left alone deliberately: write is what turns "can read the
    owner's history" into "can put words in the owner's agent", it is the bit nothing
    legitimate needs, and this runs without asking. Returns (n_changed, before, after) for
    the root, or None if there was nothing to change — the caller says it out loud rather
    than altering somebody's filesystem in silence.
    """
    try:
        root_mode = stat.S_IMODE(os.stat(STATE_DIR).st_mode)
    except OSError:
        return None
    changed = 0
    # os.walk yields every directory once as `parent`, the root included, so directories come
    # from there and only the files need joining — visiting each entry exactly once keeps the
    # reported count equal to the number of things actually narrowed.
    for parent, _dirnames, filenames in os.walk(STATE_DIR):
        for name in [parent] + [os.path.join(parent, n) for n in filenames]:
            try:
                st = os.lstat(name)
            except OSError:
                continue            # vanished mid-walk; the next start will see it
            if stat.S_ISLNK(st.st_mode):
                continue            # a symlink's own bits are ignored by the kernel
            mode = stat.S_IMODE(st.st_mode)
            if not mode & 0o022:
                continue
            try:
                os.chmod(name, mode & ~0o022)
                changed += 1
            except OSError:
                continue            # not ours to fix; the root result still gets reported
    if not changed:
        return None
    return changed, oct(root_mode), oct(root_mode & ~0o022)


def unsafe_state_ancestors():
    """Directories above the state root that let a peer replace the root itself.

    A 0700 directory inside a 0775 one is not private: write on a directory permits unlinking
    and renaming any entry in it whatever that entry's own mode says, so a peer can move the
    state root aside and put their own in its place. The daemon cannot fix a directory it may
    not own, so it names them and lets the owner decide — an exposure reported is worth more
    than a mode silently changed somewhere nobody asked it to go.
    """
    unsafe = []
    path = os.path.dirname(os.path.abspath(STATE_DIR))
    while True:
        try:
            st = os.stat(path)
        except OSError:
            break
        # The sticky bit is what makes /tmp safe to share: write, but you may only remove
        # your own entries. Without it, group or other write is replacement permission.
        if st.st_mode & 0o022 and not st.st_mode & stat.S_ISVTX:
            unsafe.append(f"{path} (mode {oct(stat.S_IMODE(st.st_mode))})")
        parent = os.path.dirname(path)
        if parent == path:
            break
        path = parent
    return unsafe


def _secret_stat_rejection(st):
    """Why the file this stat result describes is unsafe to read a secret from, or None.

    One judgment, so the path form and the descriptor form below cannot drift apart and
    start disagreeing about what counts as safe.
    """
    if not stat.S_ISREG(st.st_mode):
        return "is not a regular file"
    if st.st_uid != os.getuid():
        return f"is owned by uid {st.st_uid}, not this service's uid {os.getuid()}"
    if st.st_mode & 0o077:
        return f"has group/other permission bits (mode {oct(st.st_mode & 0o777)})"
    return None


def _secret_file_rejection(path):
    """Why ``path`` is unsafe to read a secret from, or None if it is fine.

    Checked before reading, not after: a world-readable or foreign-owned secret file is a
    finding to surface, not something to use and mention later. This form answers about a
    NAME; where the answer decides whether to then read the file, use `_open_secret_file`,
    which answers about the descriptor and so cannot be raced.
    """
    try:
        st = os.stat(path)          # follows symlinks on purpose: the target is what is read
    except FileNotFoundError:
        return "does not exist"
    except OSError as e:
        return f"cannot be stat'ed ({e.strerror})"
    return _secret_stat_rejection(st)


def _open_secret_file(path):
    """Open ``path`` and judge the file that was opened. Returns (handle, None) or (None, why).

    The caller reads from the returned handle, never by reopening the path: that is the whole
    point. Symlinks are followed, as before — the target's ownership and mode are what matter,
    and `fstat` reports the target — but a name swapped after the open no longer reaches us.
    """
    try:
        fd = os.open(path, os.O_RDONLY)
    except FileNotFoundError:
        return None, "does not exist"
    except OSError as e:
        return None, f"cannot be opened ({e.strerror})"
    rejection = _secret_stat_rejection(os.fstat(fd))
    if rejection:
        os.close(fd)
        return None, rejection
    return os.fdopen(fd, "r"), None


SECRET_PATTERN = re.compile(r"sk-[A-Za-z0-9_\-]{8,}")


def redact_secrets(text):
    """Blank out anything key-shaped in text that is about to be logged or persisted.

    Defence in depth for the leak path proved on 2026-08-11: a key containing CR/LF makes
    http.client raise `ValueError: Invalid header value b'Bearer sk-…'` — the exception
    carries the whole key — and the transcription except-block writes that message into
    `inbox.jsonl` and mirrors it to Telegram. `_clean_secret` stops such a key being used
    at all; this stops any other holder of a key-shaped string from persisting it.
    """
    return SECRET_PATTERN.sub("sk-REDACTED", str(text))


def _clean_secret(value):
    """(key, rejection) for one candidate value. The rejection NEVER contains the value.

    Rejects embedded control characters rather than sanitising them: a key with a newline
    in it is a corrupted key, and passing it on is what turns a config typo into the header
    error above. Surrounding whitespace is stripped — an editor's trailing newline is
    ordinary and harmless once removed.
    """
    if not isinstance(value, str):
        return None, f"is {type(value).__name__}, not a string"
    key = value.strip()
    if not key:
        return None, "is empty"
    bad = [c for c in key if ord(c) < 0x20 or ord(c) == 0x7F]
    if bad:
        names = ", ".join(sorted({f"0x{ord(c):02x}" for c in bad}))
        return None, f"contains control characters ({names}) and cannot go in a header"
    return key, None


def _key_from_secrets_file():
    """(key, rejection) from OpenClaw's secret provider. Never returns the value in an error."""
    rejection = _secret_file_rejection(OPENCLAW_SECRETS)
    if rejection:
        return None, f"{OPENCLAW_SECRETS} {rejection}"
    try:
        with open(OPENCLAW_SECRETS) as f:
            data = json.load(f)
    # RecursionError is not a ValueError: deeply nested JSON would otherwise escape this
    # function and skip a perfectly usable fallback.
    except (ValueError, OSError, RecursionError) as e:
        return None, f"{OPENCLAW_SECRETS} is unreadable or malformed ({e.__class__.__name__})"
    if not isinstance(data, dict):
        return None, f"{OPENCLAW_SECRETS} does not contain a JSON object"
    key, why = _clean_secret(data.get("openaiApiKey"))
    if not key:
        return None, f"{OPENCLAW_SECRETS} has no usable 'openaiApiKey': it {why}"
    return key, None


def _key_from_env_vars():
    """(key, rejection) from the legacy env.vars location, validated identically.

    Same normalisation as the primary on purpose: the fallback exists to keep voice working
    during rollout, not to be the lax path that ships a malformed key into a header.
    """
    try:
        with open(OPENCLAW_CONFIG) as f:
            data = json.load(f)
    except (ValueError, OSError, RecursionError) as e:
        return None, f"{OPENCLAW_CONFIG} is unreadable or malformed ({e.__class__.__name__})"
    env = data.get("env") if isinstance(data, dict) else None
    variables = env.get("vars") if isinstance(env, dict) else None
    if not isinstance(variables, dict):
        return None, f"{OPENCLAW_CONFIG} has no env.vars object"
    key, why = _clean_secret(variables.get("OPENAI_API_KEY"))
    if not key:
        return None, f"{OPENCLAW_CONFIG} env.vars has no usable OPENAI_API_KEY: it {why}"
    return key, None


def _key_from_bridge_config():
    """(key, rejection) from this project's OWN config — the only source a fork can use.

    Held to the same standard as the others, including the permission check: this file holds
    the bot token as well, so a world-readable one is already a problem worth naming.
    """
    rejection = _secret_file_rejection(CONFIG_PATH)
    if rejection:
        return None, f"{CONFIG_PATH} {rejection}"
    try:
        with open(CONFIG_PATH) as f:
            data = json.load(f)
    except (ValueError, OSError, RecursionError) as e:
        return None, f"{CONFIG_PATH} is unreadable or malformed ({e.__class__.__name__})"
    if not isinstance(data, dict):
        return None, f"{CONFIG_PATH} does not contain a JSON object"
    key, why = _clean_secret(data.get("openai_api_key"))
    if not key:
        return None, f"{CONFIG_PATH} has no usable 'openai_api_key': it {why}"
    return key, None


def openai_api_key():
    """The OpenAI key used to transcribe voice messages.

    Three sources, in order. **The bridge's own config comes first**, because it is the only
    one a fork can supply: the two below it read a sibling private tool's files, so on any
    other machine voice transcription simply raised, and the operator learned that as
    `[voice message — transcription failed: no OpenAI key: …]` in their inbox (#204).

    1. `openai_api_key` in `~/.config/agent-telegram-bridge/config.json`.
    2. `~/.openclaw/secrets.json` — OpenClaw's sanctioned secret provider.
    3. `openclaw.json` env.vars — a rollout fallback kept only until (2) is proven live (#146).

    env.vars is not merely a third location: it injects the secret into the environment of
    every process OpenClaw spawns, where it reaches child environments, `ps e` and crash
    dumps. OpenClaw's #706 migration moved static secrets out of it for that reason, which
    silently broke voice transcription here on 2026-08-11 because this function read only that
    path. No error below ever includes the value.
    """
    key, own_rejection = _key_from_bridge_config()
    if key:
        return key
    key, rejection = _key_from_secrets_file()
    if key:
        return key
    fallback, fallback_rejection = _key_from_env_vars()
    if fallback:
        print(f"WARNING: falling back to OPENAI_API_KEY in {OPENCLAW_CONFIG} env.vars "
              f"— {rejection}. This fallback is temporary (#146); secrets do not belong in "
              f"env.vars, which is inherited by every spawned process.", file=sys.stderr)
        return fallback
    raise RuntimeError(
        f"no OpenAI key: {own_rejection}; {rejection}; {fallback_rejection}")


# Methods safe to re-send: re-issuing them cannot create a duplicate side effect.
# Everything else (sendMessage, createForumTopic, editForumTopic, ...) is a write whose
# delivery, once the connection is established, must be assumed to have possibly happened.
IDEMPOTENT_METHODS = frozenset({
    "getUpdates", "getMe", "getFile", "getChat", "sendChatAction",
})


class PossiblyDelivered(RuntimeError):
    """A non-idempotent request timed out AFTER the connection was established.

    Telegram may have processed it (the message could already be posted); only the
    response was lost. Retrying would post a duplicate, so the caller must NOT auto-retry
    — it should surface this and verify before any manual resend.
    """


def _never_delivered(exc):
    """True when the failure provably happened before the request reached Telegram
    (connection refused or DNS failure) — which makes a retry safe even for writes."""
    reason = getattr(exc, "reason", None) or exc
    return isinstance(reason, (ConnectionRefusedError, socket.gaierror))


def api(token, method, params=None, timeout=70, retries=3):
    """Call a Bot API method, return .result. Raises RuntimeError on ok=false.

    Retries only on connection-refused/DNS failures (provably not delivered, and they
    fail fast). A read-timeout after connect is NOT retried for non-idempotent methods —
    it raises PossiblyDelivered so a lost ACK can never turn one reply into several.
    """
    return _api_call(token, method, urllib.parse.urlencode(params or {}).encode(),
                     {}, timeout, retries)


def _api_call(token, method, data, headers, timeout, retries):
    """The delivery-classification rules, in ONE place.

    `api` and `api_upload` differ only in how the body is encoded. Duplicating the retry
    logic per encoding is how one of them eventually retries a write it must not (#210)."""
    url = f"https://api.telegram.org/bot{token}/{method}"
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, data=data, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                payload = json.load(resp)
            break
        except urllib.error.HTTPError as e:
            # Telegram answered (with an error) -> outcome known, no duplicate risk.
            try:
                payload = json.load(e)
                break
            except Exception:
                raise RuntimeError(f"{method}: HTTP {e.code}") from e
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            if _never_delivered(e):
                if attempt < retries - 1:
                    time.sleep(0.3 * (attempt + 1))  # refused/DNS fails fast; cheap retry
                    continue
                raise RuntimeError(f"{method}: connection failed after {retries} attempts: {e}") from e
            # Connection established but no response arrived — delivery is unknown.
            if method in IDEMPOTENT_METHODS:
                raise RuntimeError(f"{method}: {e}") from e  # caller's own loop may retry safely
            raise PossiblyDelivered(
                f"{method} timed out after connecting — Telegram may already have it; not retried"
            ) from e
    if not payload.get("ok"):
        raise RuntimeError(f"{method}: {payload.get('error_code')} {payload.get('description')}")
    return payload["result"]


# --- File upload (#210) --------------------------------------------------------
# A session that produces a file deliverable had no way to put it in its own topic. The
# workaround was a user-account Telethon call, which posts as THE OWNER rather than the session,
# echoes straight back into the session's own inbox, and bypasses the outbox ledger.

DOCUMENT_LIMIT = 50 * 1024 * 1024   # Bot API cap for sendDocument
PHOTO_LIMIT = 10 * 1024 * 1024      # smaller cap for sendPhoto
CAPTION_LIMIT = 1024                # Bot API caption cap; text bodies split at 3800
PHOTO_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp"})


def _multipart(fields, file_field, filename, payload):
    """Encode multipart/form-data. Returns (content_type, body).

    Written out rather than pulled from `email.mime` because the body must stay exact
    bytes: a MIME helper may re-encode or line-wrap the payload, which corrupts binary
    uploads in ways that only show up for some files.

    The boundary is REGENERATED until its delimiter form appears nowhere in the parts. A
    random 128-bit boundary colliding by chance is not the case that matters — a payload that
    happens to contain the delimiter, or is crafted to, splits the message into two parts and
    the upload silently becomes something else (#211 review, reproduced with a real parser).
    """
    for _ in range(8):
        boundary = "----tg-bridge-" + os.urandom(16).hex()
        delimiter = f"--{boundary}".encode()
        if delimiter in payload or any(delimiter in str(v).encode()
                                       for v in fields.values() if v is not None):
            continue
        break
    else:                                  # unreachable with 128 random bits, never silent
        raise RuntimeError("could not find a multipart boundary absent from the payload")
    sep = f"--{boundary}\r\n".encode()
    body = bytearray()
    for name, value in fields.items():
        if value is None:
            continue
        body += sep
        body += f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode()
        body += f"{value}\r\n".encode()
    body += sep
    # The filename is quoted into a header, so a quote, CR or LF in it would let a crafted
    # name inject headers or a boundary. Replace rather than reject: the file still sends.
    safe = re.sub(r'[\r\n"]', "_", os.path.basename(filename))
    body += (f'Content-Disposition: form-data; name="{file_field}"; filename="{safe}"\r\n'
             f"Content-Type: application/octet-stream\r\n\r\n").encode()
    body += payload
    body += f"\r\n--{boundary}--\r\n".encode()
    # bytearray, not bytes(): urllib accepts any bytes-like, and a final copy of a 50 MB
    # upload is pure peak RSS for nothing.
    return f"multipart/form-data; boundary={boundary}", body


def api_upload(token, method, params, file_field, filename, payload, timeout=300):
    """`api` for a multipart upload. Same delivery guarantees — sendDocument/sendPhoto are
    writes, so a post-connect timeout raises PossiblyDelivered rather than retrying.

    Takes the PAYLOAD, not a path: re-opening a pathname that was validated earlier is what
    lets a swapped symlink upload different bytes than the ones approved (#211 review)."""
    content_type, body = _multipart(params, file_field, filename, payload)
    # retries=1: the shared classifier only ever retries provably-undelivered failures, and
    # re-uploading tens of megabytes on a DNS blip is not worth it — the caller sees the error.
    return _api_call(token, method, body, {"Content-Type": content_type}, timeout, 1)


def file_send_plan(path, as_document=False):
    """(method, field, size) for `path`, or raise with a one-line reason.

    A courtesy pre-check so a typo in a batch fails before anything is posted. It is NOT the
    authorization: `read_file_for_upload` re-decides everything from the open descriptor,
    because a stat-then-open pair can be swapped in between.

    Images go as photos so they render inline, but ONLY while they fit the photo cap —
    above it Telegram rejects a photo that it would accept as a document, so a large
    screenshot silently becoming an error is worse than it arriving as a file.
    """
    if not os.path.isabs(path):
        raise RuntimeError(f"{path}: give an absolute path")
    if not os.path.exists(path):
        raise RuntimeError(f"{path}: no such file")
    if not os.path.isfile(path):
        raise RuntimeError(f"{path}: not a regular file")
    return _plan_for(path, os.path.getsize(path), as_document)


def _plan_for(path, size, as_document):
    if size == 0:
        raise RuntimeError(f"{path}: file is empty")
    if size > DOCUMENT_LIMIT:
        raise RuntimeError(f"{path}: {size / 1048576:.1f} MB exceeds the Bot API limit of "
                           f"{DOCUMENT_LIMIT // 1048576} MB")
    is_photo = (not as_document
                and os.path.splitext(path)[1].lower() in PHOTO_SUFFIXES
                and size <= PHOTO_LIMIT)
    return ("sendPhoto", "photo", size) if is_photo else ("sendDocument", "document", size)


def read_file_for_upload(path, as_document=False):
    """Open ONCE, then decide everything from that descriptor.

    Returns (method, field, payload, sha256).

    The property this gives you, stated exactly: **the pathname is resolved once**, and the
    check, the size, the uploaded bytes and the hash all describe that one open file. The hash
    is computed from the bytes in hand, so the ledger can never disagree with what was sent.

    The property it does NOT give you: this is not a byte snapshot. Reading through a
    descriptor reads whatever the inode holds at read time, so a process that rewrites that
    same inode in place while the read is in flight changes what is uploaded — and no amount of
    descriptor discipline prevents that, short of copying the file first. What it does prevent
    is redirection: once opened, replacing the pathname or retargeting a symlink cannot change
    which file is uploaded, and cannot make the recorded hash describe different bytes than the
    ones that went out. That distinction was drawn by the #211 round-2 review, which
    demonstrated both halves.

    That is the right boundary for the actual use — a session uploading a file it just wrote —
    and it is why validation lives here rather than in `file_send_plan`. Validating a path and
    then re-opening it for the upload, and a third time for the hash, was a TOCTOU: replacing
    the file or its symlink in between made the upload send different bytes than the ones
    approved, and let the ledger record the pre-swap size beside the post-swap hash. Verified
    in the #211 review, not theoretical.

    O_NONBLOCK so a FIFO cannot hang the open; the descriptor is then required to be a regular
    file, which rejects the FIFO, a device, and a directory alike.
    """
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise RuntimeError(f"{path}: not a regular file")
        method, field, _size = _plan_for(path, info.st_size, as_document)
        # Read from the descriptor, never the name. Read to EOF rather than trusting st_size:
        # a file being appended to would otherwise send a truncated body under a valid header.
        with os.fdopen(fd, "rb") as handle:
            fd = None                       # fdopen owns it now
            payload = handle.read()
    finally:
        if fd is not None:
            os.close(fd)
    if not payload:
        raise RuntimeError(f"{path}: file is empty")
    if len(payload) > DOCUMENT_LIMIT:
        raise RuntimeError(f"{path}: {len(payload) / 1048576:.1f} MB exceeds the Bot API "
                           f"limit of {DOCUMENT_LIMIT // 1048576} MB")
    return method, field, payload, hashlib.sha256(payload).hexdigest()


def send_file(token, chat_id, path, caption=None, thread_id=None, as_document=False):
    """Post one file into a chat/topic AS THE BOT. Returns a delivery dict carrying the
    sha256 of the bytes actually uploaded."""
    method, field, payload, digest = read_file_for_upload(path, as_document)
    params = {"chat_id": chat_id}
    if thread_id:
        params["message_thread_id"] = thread_id
    if caption:
        params["caption"] = md_to_telegram_html(caption)
        params["parse_mode"] = "HTML"
    delivery = {"path": path, "size": len(payload), "method": method,
                "content_sha256": digest}
    try:
        result = api_upload(token, method, params, field, path, payload)
    except PossiblyDelivered as e:
        # Mirror the text path: the caller must be able to journal an honest "maybe".
        e.possibly_delivered_file = delivery
        raise
    except RuntimeError as e:
        if caption and "parse" in str(e).lower():     # same degradation as send_message
            params["caption"] = caption
            params.pop("parse_mode", None)
            try:
                result = api_upload(token, method, params, field, path, payload)
            except PossiblyDelivered as ambiguous:
                ambiguous.possibly_delivered_file = delivery
                raise
        else:
            raise
    return {**delivery, "result": result}


# --- Outbound message formatting (Telegram HTML) -------------------------------
# Messages go out with parse_mode=HTML so agent Markdown renders as real formatting.
# HTML is used over MarkdownV2 because it needs only & < > escaped, whereas MarkdownV2
# escapes ~18 chars and breaks on dynamic text (issue #89).

_TG_HTML_LIMIT = 3800          # split source below the 4096 Bot API cap; leaves headroom
                               # for the HTML tags md_to_telegram_html adds.
_MD_FENCE = re.compile(r"```[^\n`]*\n?(.*?)```", re.DOTALL)   # ```lang\n...``` or ```...```
_MD_INLINE_CODE = re.compile(r"`([^`\n]+)`")
_MD_LINK = re.compile(r"\[([^\]\n]+)\]\((https?://[^\s)]+)\)")
_MD_BOLD = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
_MD_UNDERLINE = re.compile(r"__(.+?)__", re.DOTALL)
_MD_STRIKE = re.compile(r"~~(.+?)~~", re.DOTALL)


def md_to_telegram_html(text):
    """Convert a conservative Markdown subset to Telegram Bot API HTML (parse_mode=HTML).

    Order matters for safety:
      1. Stash fenced/inline code (escaped) so their contents are never re-formatted.
      2. Escape & < > in EVERYTHING ELSE FIRST — so a literal angle-bracket token in
         agent text (e.g. <thinking>, List<int>) can't be read as an HTML tag, which
         Telegram would otherwise silently drop along with the rest of the message.
      3. Inject only Telegram-supported tags: <a> links, <b>/<u>/<s> for ** / __ / ~~.
    Single-char * / _ italic is deliberately NOT converted (too many false positives on
    bullets and snake_case identifiers). Anything unsupported stays as escaped text."""
    stash = []

    def _keep(frag):
        stash.append(frag)
        return f"\x00{len(stash) - 1}\x00"

    text = _MD_FENCE.sub(lambda m: _keep(f"<pre>{_html.escape(m.group(1), quote=False)}</pre>"), text)
    text = _MD_INLINE_CODE.sub(lambda m: _keep(f"<code>{_html.escape(m.group(1), quote=False)}</code>"), text)
    text = _html.escape(text, quote=False)                     # & < >  (leave quotes intact)
    text = _MD_LINK.sub(lambda m: f'<a href="{m.group(2)}">{m.group(1)}</a>', text)  # url already escaped
    text = _MD_BOLD.sub(r"<b>\1</b>", text)
    text = _MD_UNDERLINE.sub(r"<u>\1</u>", text)
    text = _MD_STRIKE.sub(r"<s>\1</s>", text)
    for i, frag in enumerate(stash):
        text = text.replace(f"\x00{i}\x00", frag)
    return text


def split_for_telegram(text, limit=_TG_HTML_LIMIT):
    """Chunk text to <=limit chars, preferring newline boundaries so inline markdown/HTML
    isn't cut mid-token; hard-split any single line longer than limit. Lossless: joining
    the returned chunks reproduces the input exactly (the boundary newline travels with
    its line, never dropped). Never returns []."""
    chunks, cur = [], ""
    lines = text.split("\n")
    last = len(lines) - 1
    for idx, line in enumerate(lines):
        seg = line if idx == last else line + "\n"             # re-attach the split newline
        while len(seg) > limit:                                # a single over-long segment
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.append(seg[:limit])
            seg = seg[limit:]
        if len(cur) + len(seg) > limit:
            if cur:
                chunks.append(cur)
            cur = seg
        else:
            cur += seg
    if cur or not chunks:
        chunks.append(cur)
    return chunks


def send_message(token, chat_id, text, thread_id=None):
    """Send text to a chat/topic as Telegram HTML (real bold/underline/strike/code/links
    from Markdown), auto-splitting long text. On an HTML parse error the offending chunk
    is re-sent as PLAIN text, so a formatting edge case degrades a message, never drops
    it. Non-parse errors (connection failure / PossiblyDelivered) propagate unchanged.

    Return one delivery dict per chunk, carrying the exact text passed to sendMessage and
    its API result. If delivery is ambiguous, attach completed deliveries and the in-flight
    chunk to PossiblyDelivered before re-raising so callers can journal the honest state.
    """
    base = {"chat_id": chat_id}
    if thread_id:
        base["message_thread_id"] = thread_id
    chunks = split_for_telegram(text)
    deliveries = []
    for chunk_index, chunk in enumerate(chunks):
        api_text = md_to_telegram_html(chunk)
        try:
            result = api(token, "sendMessage",
                         {**base, "text": api_text, "parse_mode": "HTML"})
        except PossiblyDelivered as e:
            e.completed_sends = deliveries
            e.possibly_delivered_send = {
                "text": api_text,
                "chunk_index": chunk_index,
                "chunk_count": len(chunks),
            }
            raise
        except RuntimeError as e:
            if "parse" in str(e).lower():                      # bad entities -> plain text
                api_text = chunk
                try:
                    result = api(token, "sendMessage", {**base, "text": api_text})
                except PossiblyDelivered as ambiguous:
                    ambiguous.completed_sends = deliveries
                    ambiguous.possibly_delivered_send = {
                        "text": api_text,
                        "chunk_index": chunk_index,
                        "chunk_count": len(chunks),
                    }
                    raise
            else:
                raise
        deliveries.append({
            "text": api_text,
            "result": result,
            "chunk_index": chunk_index,
            "chunk_count": len(chunks),
        })
    return deliveries


def download_file(token, file_id, dest):
    """Resolve file_id via getFile and download it to dest, without following symlinks.

    A plain `open(dest, "wb")` follows a symlink sitting at `dest`, so anything able to
    plant one in the media directory could redirect an attachment over an arbitrary file
    (#150 review). Instead the body goes to a sibling temp opened `O_EXCL|O_NOFOLLOW` — so
    it cannot itself be a pre-planted link — and is then `os.replace`d onto `dest`, which
    renames over a symlink rather than through it. A symlinked parent directory is refused
    outright: containment cannot be reasoned about once the directory itself is a link.

    Scope, stated rather than implied: this defeats a link planted at `dest` or at the
    immediate parent. It does NOT defeat a symlinked *ancestor* further up, nor a parent
    swapped between the check and the open. Closing those needs `dir_fd`-anchored component
    walking, and every one of them requires write access to the bridge's own state
    directory — the same shared-UID authority that could rewrite `daemon.py` itself. The
    boundary is the UID, not this function; hardening past it buys nothing real and
    complicates the path every download takes.
    """
    directory = os.path.dirname(dest) or "."
    if os.path.islink(directory):
        raise RuntimeError(f"refusing to download into symlinked directory {directory}")
    file_path = api(token, "getFile", {"file_id": file_id})["file_path"]
    url = f"https://api.telegram.org/file/bot{token}/{file_path}"
    tmp = f"{dest}.part-{os.getpid()}"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    fd = os.open(tmp, flags, 0o600)
    try:
        # fdopen FIRST: it takes ownership of the descriptor, so a urlopen failure closes it
        # rather than leaking one fd per failed download.
        with os.fdopen(fd, "wb") as f:
            with urllib.request.urlopen(url, timeout=120) as resp:
                f.write(resp.read())
        os.replace(tmp, dest)      # rename does not follow a symlink at dest
    except BaseException:
        try:
            os.unlink(tmp)         # never leave a partial file that looks downloaded
        except OSError:
            pass
        raise
    return dest


@contextmanager
def _locked_inbox(path):
    """Serialize every writer that can append to one topic inbox."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


@contextmanager
def _locked_wake_transition(path):
    """Serialize wake dispatch with cursor commit without blocking inbox writers."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".wake.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _inbox_snapshot(path, idempotency_field=None, key=None):
    """Return ``(duplicate, wake_claim)`` while the caller holds the inbox lock."""
    line_count = 0
    duplicate = False
    if os.path.exists(path):
        with open(path) as inbox:
            for line in inbox:
                line_count += 1
                if idempotency_field and line.strip():
                    existing = json.loads(line)
                    if (isinstance(existing, dict)
                            and existing.get(idempotency_field) == key):
                        duplicate = True
    cursor_path = os.path.join(os.path.dirname(path), "cursor")
    cursor = 0
    if os.path.exists(cursor_path):
        with open(cursor_path) as cursor_file:
            cursor = int(cursor_file.read().strip() or "0")
    claim = WakeClaim(cursor) if line_count <= cursor else None
    return duplicate, claim


def _fsync_directory(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _append_jsonl_record(path, record, durable=False):
    creates_inbox = durable and not os.path.exists(path)
    with open(path, "a") as inbox:
        inbox.write(json.dumps(record, ensure_ascii=False) + "\n")
        if durable:
            inbox.flush()
            os.fsync(inbox.fileno())
    if creates_inbox:
        # Fsyncing the file persists its CONTENT, not the directory entry that names it, so
        # a brand-new inbox needs its parent synced too. Partial hardening with no guarantee
        # attached: every ancestor of topics/<N> is created by writers that sync nothing
        # (state_path's makedirs, the non-durable Telegram ingress append), and no check here
        # can tell a persisted dirent chain from an unpersisted one. The claim stays
        # process-crash durability with a fsynced file — no OS-crash pathname guarantee.
        _fsync_directory(os.path.dirname(path))


def append_jsonl(path, record):
    """Append a normal inbox record and return its wake claim, if it starts a batch."""
    with _locked_inbox(path):
        _duplicate, wake_claim = _inbox_snapshot(path)
        _append_jsonl_record(path, record)
        return wake_claim


def append_jsonl_once(path, record, idempotency_field="idempotency_key"):
    """Durably append ``record`` once and return its wake claim, when needed.

    The inbox record is the durable idempotency ledger; no parallel state model can drift
    away from it. The per-inbox lock makes concurrent local automation retries resolve to
    one append. The same lock protects the first-unread decision across local and Telegram
    writers, and fsync makes the winning key durable before any caller wakes a pane. Returns
    ``(appended, wake_claim)``; duplicates return ``(False, None)``.
    """
    key = record.get(idempotency_field)
    if not isinstance(key, str) or not key:
        raise ValueError(f"record requires non-empty {idempotency_field!r}")
    with _locked_inbox(path):
        duplicate, wake_claim = _inbox_snapshot(path, idempotency_field, key)
        if duplicate:
            return False, None
        _append_jsonl_record(path, record, durable=True)
        return True, wake_claim


@contextmanager
def validate_wake_claim(path, claim):
    """Hold wake/cursor serialization and report whether ``claim`` still owns the batch."""
    if claim is None:
        yield True
        return
    with _locked_wake_transition(path):
        cursor_path = os.path.join(os.path.dirname(path), "cursor")
        cursor = 0
        if os.path.exists(cursor_path):
            with open(cursor_path) as cursor_file:
                cursor = int(cursor_file.read().strip() or "0")
        yield cursor == claim.cursor


def commit_cursor_if_inbox_current(path, cursor):
    """Advance the sibling cursor only when ``cursor`` still covers the whole inbox.

    The reader prints and flushes outside the writer lock. This compare-and-commit closes
    the remaining gap: if a writer appended meanwhile, the caller must drain those added
    lines before retrying rather than leave an unread record without a nudge owner.
    """
    with _locked_wake_transition(path):
        with _locked_inbox(path):
            line_count = 0
            if os.path.exists(path):
                with open(path) as inbox:
                    line_count = sum(1 for _line in inbox)
            if line_count != cursor:
                return False
            cursor_path = os.path.join(os.path.dirname(path), "cursor")
            with open(cursor_path, "w") as cursor_file:
                cursor_file.write(str(cursor))
            return True


def read_registry():
    path = state_path("registry.json")
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return {}


def write_registry(registry):
    path = state_path("registry.json")
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(registry, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def update_registry(mutator):
    """Read-modify-write registry.json under an exclusive cross-process file lock, so
    concurrent writers — daemon loops, Timer threads, and the SEPARATE `tg-bridge register`
    process — can't lose each other's updates. `mutator(registry)` edits the dict in place;
    its return value is passed back to the caller. Use this for every partial update; a
    plain write_registry() of a stale full snapshot can still clobber."""
    path = state_path("registry.json")
    with open(path + ".lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            registry = {}
            if os.path.exists(path):
                with open(path) as f:
                    registry = json.load(f)
            result = mutator(registry)
            tmp = path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(registry, f, indent=2, ensure_ascii=False)
            os.replace(tmp, path)
            return result
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")
