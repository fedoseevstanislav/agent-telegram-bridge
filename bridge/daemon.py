"""Long-polling daemon: sole getUpdates consumer for the bridge bot.

Routes group messages by forum topic (message_thread_id) into per-topic JSONL
inboxes under the state dir. Voice/audio messages are downloaded and
transcribed before being written to the inbox.
"""

import contextlib
import fcntl
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime
from urllib.parse import quote

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bridge.common import (
    api, append_jsonl, download_file, load_config, now_iso, openai_api_key,
    read_registry, redact_secrets, secure_process_umask, secure_state_tree,
    send_message, state_path,
    unsafe_state_ancestors, update_registry, valid_owner_id, validate_wake_claim,
)
from bridge.transcribe import transcribe
from bridge import codex_ctx, transcript

POLL_TIMEOUT = 50
ALLOWED_UPDATES = '["message"]'  # edited messages never enter a privileged command path
TMUX_TIMEOUT = 10  # s; bound every tmux call so a hung tmux op can't freeze a daemon thread —
                   # a tmux send-keys under _cf_lock once hung and froze the whole getUpdates
                   # loop (which takes _cf_lock per message). See _tmux and #110.
PANE_LOCK_TIMEOUT = TMUX_TIMEOUT  # s; same reason as above, for the per-pane lock. #110 was
                   # exactly this failure with a different lock, and _pane_lock is now taken
                   # on the synchronous message-handler paths — so it must never be unbounded.
NUDGE_DELAY = 4  # seconds; lets a blocking ask/recv --wait consume the reply first

# Echo verification (#133): never press Enter on a pane that didn't take the text we typed —
# a modal swallows the text and reads the Enter as "accept the highlighted option", which is
# how codex sessions silently switched to a cheaper model. See type_line.
ECHO_CAPTURE_LINES = 30  # rows of scrollback above the visible pane to include in the capture.
                         # The window is the WHOLE capture, never a bottom slice of it — see
                         # _echo_capture for why a content-defined window is unsafe (#165).
ECHO_PROBE_CHARS = 40    # chars of the line's tail used as the probe
SWALLOW_MAX_ATTEMPTS = 2  # consecutive unverified injections into one pane before we stop
                          # typing: each attempt appends to an input box we cannot verify
ECHO_EXTRA_WAITS = (0.6, 1.2)  # further waits before giving up on an injection, after the
                               # first `settle` sample. One 0.3s sample called a busy pane's
                               # late repaint a modal, and the verdict was permanent (#250).
                               # A ladder rather than a deadline: type_line holds the pane
                               # lock while it waits, so the cost has to be legible — at most
                               # 0.3+0.6+1.2s — and it must not depend on a clock, which a
                               # test that stubs sleep would otherwise spin against.
SWALLOW_RETRY_AFTER = 600  # s after which a capped pane is tried once more regardless — the
                           # cap must never become a one-way door (see type_line)
BRIEFING_RETRY_DELAY = 120  # s before re-trying a revival briefing the pane didn't take
BRIEFING_MAX_ATTEMPTS = 3
BLOCKED_REPORT_COOLDOWN = 1800  # s between "your terminal is stuck on a prompt" reports
UNREADABLE_ESCALATE_AFTER = 3  # consecutive sweeps where the pane could not be read/locked
                               # before we say so in the topic. Not 1: a single unreadable
                               # capture during a repaint is ordinary and retrying is the
                               # right answer to it (#188)
CTX_POLL = int(os.environ.get("TG_BRIDGE_CTX_POLL", "30"))  # context warning poll interval
CTX_STALE = 600  # ignore context files older than this (session likely closed)
LIFECYCLE_POLL = int(os.environ.get("TG_BRIDGE_LIFECYCLE_POLL", "30"))  # pane liveness poll interval
WARN_START = 20  # first warning threshold (%)
WARN_STEP = 10  # then every additional 10%

# Auto carry-forward (#92): when a session's context crosses AUTOCF_PCT, the daemon
# auto-fires the full carry-forward procedure (write → issue → /compact → resume), so a
# session self-manages its context without the owner typing /carryforward. 0 disables it.
# Re-arms once context falls below AUTOCF_REARM_PCT (post-compact) so it can fire again.
AUTOCF_PCT = int(os.environ.get("TG_BRIDGE_AUTOCF_PCT", "60"))
AUTOCF_REARM_PCT = max(0, AUTOCF_PCT - 10)  # hysteresis so it can't flap at the boundary
# ...but a run that ends BEFORE compaction never lowers the context, so the post-compact
# drop that re-arms the topic never comes and auto-CF is dead for the session's life
# (#239). Such a run leaves a failure record, and this is how long the daemon waits before
# honouring it. It is a rate limit, not a prediction: the precise condition would be "the
# pane has been sustained-idle since the failure", which is more state to carry for a
# retry that costs one notice. The busiest sessions — the ones that fail this gate and
# the ones whose context climbs — are exactly the ones that must not be retried every tick.
AUTOCF_RETRY_COOLDOWN = 15 * 60

# Periodic self-heal sweep: catch sessions that went "dark" (turn ended without a
# live, harness-tracked recv consuming the inbox) — interrupts/transient 529s that
# skipped re-arming, or a detached `&` recv that drained the cursor then exited.
SWEEP_POLL = 60  # seconds between sweeps
SWEEP_GRACE = 150  # seconds a topic must stay a dark candidate before we act (avoid racing the normal arm gap)
SWEEP_COOLDOWN = 600  # min seconds between re-nudges of the same topic
RECENT_DROP_WINDOW = 1200  # #105: only re-deliver the last inbox message if it arrived this
                           # recently — a drop by a just-died listener is fresh; re-injecting
                           # an old, already-handled message on every dead-listener sweep isn't.

SERVICE_KEYS = (
    "forum_topic_created", "forum_topic_edited", "forum_topic_closed",
    "forum_topic_reopened", "new_chat_members", "left_chat_member", "pinned_message",
)


def log(msg):
    print(f"[{now_iso()}] {msg}", file=sys.stderr, flush=True)


def load_offset():
    path = state_path("offset")
    if os.path.exists(path):
        with open(path) as f:
            return int(f.read().strip() or 0)
    return 0


def save_offset(offset):
    path = state_path("offset")
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(str(offset))
    os.replace(tmp, path)


def unread_count(thread_id):
    inbox = state_path("topics", str(thread_id), "inbox.jsonl")
    if not os.path.exists(inbox):
        return 0
    with open(inbox) as f:
        total = sum(1 for _ in f)
    cursor_path = os.path.join(os.path.dirname(inbox), "cursor")
    cursor = 0
    if os.path.exists(cursor_path):
        with open(cursor_path) as f:
            cursor = int(f.read().strip() or 0)
    return total - cursor


def last_inbox_message(thread_id):
    """The last inbox record (a dict with 'from'/'text') for a topic, or None. Used to
    RE-DELIVER a message that a dying/reaped recv drained — cursor advanced but the session
    never saw it (#105). Best-effort: returns None on any read/parse error, including a last
    line that is valid JSON but not an object (e.g. `[]`)."""
    inbox = state_path("topics", str(thread_id), "inbox.jsonl")
    if not os.path.exists(inbox):
        return None
    try:
        last = None
        with open(inbox, encoding="utf-8", errors="replace") as f:
            for line in f:
                if line.strip():
                    last = line
        if not last:
            return None
        rec = json.loads(last)
        return rec if isinstance(rec, dict) else None
    except (OSError, ValueError):
        return None


def recent_inbox_drop(tid, now, window=RECENT_DROP_WINDOW):
    """The last inbox record IF the inbox was last written within `window` seconds of `now`
    — i.e. recent enough that a drop by a just-died listener is plausible — else None (#105).

    Gates re-delivery on freshness (via file mtime) so a routine dead-listener sweep over a
    session that's simply idle doesn't re-inject an old, already-handled message every time."""
    try:
        inbox = state_path("topics", str(tid), "inbox.jsonl")   # may makedirs -> OSError
        if now - os.path.getmtime(inbox) > window:
            return None
    except OSError:
        return None
    return last_inbox_message(tid)


def dead_listener_nudge(tid, rec=None):
    """Text nudged into a topic whose background recv listener has died (#105).

    If `rec` (a recent inbox record) is given, its text is re-delivered as a recap, because a
    dying/reaped recv may have advanced the cursor without the session ever seeing the
    message. The caller passes `rec` only when a drop is plausible (see recent_inbox_drop), so
    an old, already-handled message isn't re-injected. Phrased so the agent judges whether it's
    actually new — avoids a blind duplicate action. The snippet is newline-flattened (single
    tmux send-keys line) and capped, with truncation flagged explicitly so a long message is
    never presented as if complete."""
    recap = ""
    if rec and rec.get("text"):
        body = rec["text"]
        snippet = body[:400].replace("\n", " ")
        more = "" if len(body) <= 400 else " […truncated — run recv; the full text may not be recoverable]"
        recap = (
            f" A dying listener may have dropped a message without waking you — "
            f"most recent, from {rec.get('from')}: \"{snippet}\"{more}. If you have NOT "
            f"already handled it, act on it now."
        )
    return (
        f"[tg-bridge] Your background listener for topic {tid} isn't "
        f"running, so new replies won't wake you.{recap} Drain with "
        f"`tg-bridge recv --topic {tid}`, then re-arm "
        f"`tg-bridge recv --topic {tid} --wait 86400` via run_in_background "
        f"— NEVER a detached `&` shell job."
    )


def sweep_nudge_text(tid, flavor, now):
    """The self-heal nudge text for a dark topic, dispatched by `flavor` (#105). This is the
    exact call idle_sweep_loop makes, factored out so the dispatch is unit-testable:
      * "unread" — messages sit undrained in the inbox; tell the session to drain + re-arm.
      * otherwise — the recv listener has died; re-deliver a FRESH dropped message if any."""
    if flavor == "unread":
        return (
            f"[tg-bridge] You have undelivered messages in topic {tid} — "
            f"run `tg-bridge recv --topic {tid}` now and drain the inbox. "
            f"For claude: then re-arm ONE long background wait with "
            f"run_in_background (never a detached `&`)."
        )
    return dead_listener_nudge(tid, recent_inbox_drop(tid, now))


def _tmux(argv, **kwargs):
    """Run a tmux command (argv[0] == "tmux") with a bounded timeout (#110). A tmux call
    with no timeout can hang a daemon thread forever; when that thread holds _cf_lock — the
    carry-forward injectors do, for halt/inject atomicity — the whole getUpdates loop (which
    takes _cf_lock on every message) freezes with it. Bounding every tmux call means a hung
    tmux op raises TimeoutExpired, which unwinds out of any held lock, so a stuck tmux/CF
    degrades to a failed op instead of a daemon-wide freeze. Callers pass their usual kwargs
    (check / capture_output / text); only `timeout` is defaulted here."""
    kwargs.setdefault("timeout", TMUX_TIMEOUT)
    return subprocess.run(argv, **kwargs)


def pane_alive(pane):
    # display-message exits 0 even for dead panes; list-panes actually validates the target
    return _tmux(
        ["tmux", "list-panes", "-t", pane], capture_output=True,
    ).returncode == 0


def pane_cwd(pane):
    """Working directory of a tmux pane (where its engine was launched) — used to
    map a Codex pane to its session rollout for the context readout."""
    out = _tmux(
        ["tmux", "display-message", "-p", "-t", pane, "#{pane_current_path}"],
        capture_output=True, text=True,
    ).stdout.strip()
    return out or None


def pane_pid(pane):
    """PID of a tmux pane's process, or None. The engine's own rollout/transcript is held
    by that process or one of its children, so this is the root of an exact lookup."""
    out = _tmux(
        ["tmux", "display-message", "-p", "-t", pane, "#{pane_pid}"],
        capture_output=True, text=True,
    ).stdout.strip()
    try:
        return int(out)
    except (TypeError, ValueError):
        return None


_blocked_reported = {}  # tid -> last time we told the topic its pane is stuck on a prompt
_blocked_lock = threading.Lock()
_unreadable_streak = {}  # tid -> consecutive sweeps whose nudge came back "failed" (#188).
                         # Only idle_sweep_loop writes it, and that is one thread, so it
                         # needs no lock of its own.
_swallowed_streak = {}  # pane -> (consecutive unverified injections, when the cap was hit)
_pane_locks = {}        # pane -> lock serializing the type→verify→Enter sequence
_pane_locks_lock = threading.Lock()


class PaneLockUnavailable(Exception):
    """The pane could not be locked, so writing to it would be unserialized. Every caller
    must treat this as "the write did not happen" and never fall through to typing."""


@contextlib.contextmanager
def _pane_lock(pane, timeout=None):
    """Serialize everything that types into one pane — across daemon threads AND across
    processes. `tg-bridge notify` runs maybe_nudge() in its own process, so a thread lock
    alone leaves the original race open: the daemon and a notify process each read a clean
    baseline, both type, both see a rise, and the second Enter lands on whatever the first
    Enter brought up. The flock is the same idiom the inbox already uses (bridge/common.py).

    The flock alone would already exclude the daemon's own threads — each call opens the
    file separately, and separate open-file-descriptions contend even within one process
    (measured, not assumed). The thread lock is kept so it still serializes in-process if
    the lock file is unusable.

    BOUNDED, and it raises rather than degrading (#165 review r2). Both matter because this
    lock is now taken on the synchronous message-handler paths (interrupt, slash relay,
    /model). An unbounded `flock` there is a daemon-wide freeze: one stopped or wedged
    `tg-bridge notify` holding the pane lock would block the handler forever, so getUpdates
    never resumes and EVERY topic stops receiving control messages. And silently degrading to
    thread-only on an unopenable lock file would drop cross-process serialization at exactly
    the moment something is already wrong. Failing an injection is recoverable — it is
    retried, and the caller tells the owner; freezing the bridge or typing unserialized is not."""
    if timeout is None:
        timeout = PANE_LOCK_TIMEOUT
    # ONE deadline across both acquisitions. Giving each its own would let a thread holder
    # burn 9.9s and a cross-process holder another 9.9s, so the bound the handler paths
    # actually see would be double the one named here (#166 review r3).
    deadline = time.monotonic() + timeout
    with _pane_locks_lock:
        local = _pane_locks.setdefault(pane, threading.Lock())
    if not local.acquire(timeout=max(0.0, deadline - time.monotonic())):
        raise PaneLockUnavailable(f"timed out waiting for the in-process lock on pane {pane}")
    try:
        try:
            # state_path creates the directory, so it fails on a read-only or full
            # filesystem just like the open does — both mean "no cross-process lock".
            path = state_path("panes", pane.lstrip("%") + ".lock")
            handle = open(path, "a")
        except OSError as e:
            raise PaneLockUnavailable(f"can't open the lock file for pane {pane}: {e}") from e
        try:
            while True:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise PaneLockUnavailable(
                            f"another process has held pane {pane} for {timeout}s")
                    time.sleep(0.05)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)
        finally:
            handle.close()
    finally:
        local.release()


def _squash(text):
    """Drop ALL whitespace. A TUI wraps a long input line at the pane width, so the text we
    typed is split by newlines and padding that were never in the string we sent; squashing
    both sides makes the comparison survive any wrap point."""
    return re.sub(r"\s+", "", text or "")


def _echo_probe(text):
    """The fragment of an injected line to look for in the pane — its TAIL, squashed.

    The tail is what lands next to the cursor: a wrapped line puts its head several rows up
    (and, in a horizontally-scrolling box, off-screen entirely), but its last characters are
    always on the input box's final rendered row."""
    return _squash(text)[-ECHO_PROBE_CHARS:]


def _echo_capture(pane):
    """The pane's visible screen plus ECHO_CAPTURE_LINES rows of scrollback above it,
    whitespace-squashed, or None if the capture failed.

    The WHOLE capture, never a bottom slice of it. #133 counted inside the last ten NON-EMPTY
    rows, which is a window defined by content rather than position: a modal that filters one
    row away, a blank row that appears or disappears, a resize that reflows — any of those
    change which rows fall inside it. Residue sitting just above could therefore slide in and
    raise the count with nothing having been typed, and a raised count presses Enter on the
    modal this check exists to protect against (#165, Codex review of #163). Generic residue
    like a paste chip made that far likelier than it ever was for the payload-specific literal.

    A window anchored to the bottom of the pane is stable under append-only output, and
    losing a stale copy can only lower a count — which fails closed. Residue is handled by
    COUNTING, not by narrowness: a chip already on screen is counted in `before` as well as
    `after`, so only a genuinely new occurrence is a rise.

    "Anchored" is not "fixed", which is why this returns the pane's geometry with the text.
    `-S -30` counts thirty rows above the CURRENT visible top, so the absolute rows it covers
    move when the split between history and screen moves. Growing history is the safe
    direction — the top edge advances and can only evict. **Shrinking history is not**: a
    resize that pulls rows back out of history moves the top edge backwards and admits older
    content, so a stale chip one row outside the window slides in and its count rises with
    nothing typed (#165 review r2 — Codex disproved "nothing can move down into it" with
    exactly this). Same for a width change, which reflows wrapped rows, and for the alternate
    screen, which swaps the grid wholesale. type_line compares these and abstains, rather
    than trusting a count taken across two different windows."""
    fmt = "#{history_size},#{pane_height},#{pane_width},#{alternate_on}"

    def _geo():
        got = _tmux(["tmux", "display-message", "-p", "-t", pane, fmt],
                    capture_output=True, text=True)
        return None if got.returncode != 0 else got.stdout.strip()

    try:
        # BRACKET the capture. Sampling the fingerprint only afterwards leaves a gap the
        # check was added to close: resize between the capture and the sample and BOTH
        # captures report the same post-resize geometry, so the move is invisible while the
        # admitted residue is not (#166 review r3, reproduced). A capture is usable only if
        # the geometry was the same immediately before and immediately after it.
        before_raw = _geo()
        out = _tmux(
            ["tmux", "capture-pane", "-p", "-t", pane, "-S", f"-{ECHO_CAPTURE_LINES}"],
            capture_output=True, text=True,
        )
        if out.returncode != 0:
            return None
        after_raw = _geo()
    except Exception:
        return None  # unreadable pane — "unknown", never "nothing there"
    before, after = _parse_geometry(before_raw), _parse_geometry(after_raw)
    if before is None or after is None or before != after:
        return None  # the pane moved under this very capture, or told us nothing usable
    return _squash(out.stdout), after_raw


def _parse_geometry(raw):
    """`history_size,pane_height,pane_width,alternate_on` as ints, or None if it isn't that.
    Parsing before comparing is what stops two identical UNPARSEABLE fingerprints reading as
    "nothing moved" — equality on raw strings made `("garbage", "garbage")` fail open."""
    try:
        parsed = tuple(int(x) for x in raw.split(","))
    except (ValueError, AttributeError):
        return None
    return parsed if len(parsed) == 4 else None


def _geometry_moved(before_geo, after_geo):
    """True when the two captures cannot be compared because the window moved under them.

    Only a SHRINKING history is dangerous; a growing one evicts, which the strict-rise rule
    already handles safely. Height, width and alternate-screen changes are all disqualifying:
    they move or replace the grid the row window is measured against. An unparseable
    fingerprint is treated as moved — unknown is never "fine", which is why this parses
    BEFORE comparing rather than short-circuiting on raw string equality."""
    b, a = _parse_geometry(before_geo), _parse_geometry(after_geo)
    if b is None or a is None:
        return True
    if b == a:
        return False
    return a[0] < b[0] or a[1:] != b[1:]


# Neither engine renders long input literally: past some size it is collapsed into a chip, so
# the text never appears in the pane at all. The tail probe therefore could never match, and
# #133 withheld Enter on input that had landed perfectly — every carry-forward broke the
# moment #133 went live (#163). These are the collapse markers, read out of the shipped
# binaries rather than the docs: claude 2.1.235 renders "[Pasted text #N]", "[Pasted text #N
# +M lines]" and "[...Truncated text #N +M lines...]"; codex 0.146.0 renders "[Pasted Content
# N chars]". "[Image #N]" and "[Audio #N]" are deliberately absent — a different payload type
# is not proof that TEXT landed. Counting them (rather than testing for presence) keeps the
# anti-residue property the literal probe has: a chip left by an EARLIER injection does not
# authorise this one, only a NEW one does.
_ECHO_CHIPS = tuple(_squash(m) for m in ("[Pasted text", "[...Truncated text", "[Pasted Content"))


def _count_signals(capture, probe):
    """(literal-tail count, collapse-chip count) in an already-squashed capture."""
    return capture.count(probe), sum(capture.count(chip) for chip in _ECHO_CHIPS)


def _echo_counts(pane, probe):
    """(counts, geometry) over a fresh capture of `pane`, or None if the capture failed. A
    pane that took the text shows one signal or the other; a modal that swallowed the
    keystrokes shows neither, which is what keeps #133's protection intact."""
    captured = _echo_capture(pane)
    if captured is None:
        return None
    capture, geo = captured
    return _count_signals(capture, probe), geo


def type_line(pane, text, settle=0.3):
    """Type `text` into a pane and press Enter ONLY once the text is confirmed to have
    reached an input box (#133). Returns "sent", "swallowed", or "failed".

    `tmux send-keys Enter` is unconditional. When the pane is showing a modal — codex's
    "Approaching rate limits / 1. Switch to gpt-5.6-luna" picker (option 1 preselected), an
    approval prompt, the update or resume picker — the printable text is swallowed by the
    widget and the Enter ACCEPTS THE HIGHLIGHTED OPTION. That is how sessions silently
    switched to a cheaper model mid-run, and the message was never delivered either.

    This checks behaviour, not appearance. Pattern-matching each engine's modals would have
    to track their rendering across releases, and the obvious tell is a trap: codex's '›' is
    the ordinary input caret, printed on every idle pane. Instead: if the text we typed does
    not show up, the keystrokes did not land in an input box, so the Enter is withheld.

    Erring toward NOT typing on an unreadable pane matches _cf_busy — an unverified pane is
    never acted on. A withheld nudge is retried by idle_sweep_loop; a wrong Enter is not
    recoverable.

    The whole capture→type→capture→Enter sequence holds a per-pane lock. Without it the
    verification is a TOCTOU check rather than an authorization: two injectors (a nudge timer
    and the idle sweep, say) can each read a clean baseline, both type, both see a raised
    count, and the second Enter then lands on whatever the first Enter brought up — which is
    exactly the picker this function exists to avoid. The in-process lock covers daemon
    threads, and the state-file flock it also takes serializes `tg-bridge notify`
    subprocesses calling maybe_nudge(). It is bounded and raises rather than degrading, so a
    pane that cannot be locked is not typed into at all.

    Every other daemon path that WRITES to a pane takes the same lock (#165) — interrupts,
    the slash-command relay, model switches — because a write landing inside this window
    corrupts it in the dangerous direction: a long interrupt instruction collapses into a
    chip and raises the count this function reads as proof. Two paths deliberately stay
    outside it, both bare Escapes on the halt/abort routes: they exist to interrupt a session
    that may be mid-injection, so blocking them on that injection's lock would defeat them."""
    probe = _echo_probe(text)
    if not probe:
        return "failed"
    with contextlib.ExitStack() as stack:
        try:
            stack.enter_context(_pane_lock(pane))
        except PaneLockUnavailable as e:
            # Never fall through to typing: unserialized is exactly the TOCTOU this lock
            # exists to prevent. The caller retries, and the streak cap is untouched because
            # nothing was typed into the pane.
            log(f"type_line: {e} — not typing")
            return "failed"
        captured = _echo_capture(pane)
        if captured is None:
            log(f"type_line: can't read pane {pane} — not typing")
            return "failed"
        capture, geo_before = captured
        streak, capped_at = _swallowed_streak.get(pane, (0, 0.0))
        if streak >= SWALLOW_MAX_ATTEMPTS:
            # Every attempt appends to an input box we can't verify, so repeated tries just
            # grow an unsendable line (the sweep would retry once a minute forever). Stop
            # adding to it — but on a timer, never permanently. Clearing the streak only on a
            # successful send would be a trap: success needs typing, typing is what the cap
            # blocks, so a pane whose modal the human answered could never be nudged again.
            #
            # Time is the only sound release signal. "The bottom region changed" looks like
            # better evidence — a waiting modal repaints nothing — but it cannot tell a modal
            # that was answered from a modal with something ticking underneath, and the
            # second case would reset the streak on every poll and restore exactly the growth
            # this cap exists to stop.
            if time.time() - capped_at < SWALLOW_RETRY_AFTER:
                log(f"type_line: pane {pane} still capped after {SWALLOW_MAX_ATTEMPTS} "
                    f"unverified attempts — not typing again")
                return "swallowed"
            # Spend ONE attempt and re-arm the window; do NOT zero the streak. Zeroing would
            # buy SWALLOW_MAX_ATTEMPTS fresh copies every window, so a pane that accepts text
            # but never satisfies the strict-rise check would still grow without bound, just
            # more slowly. Staying at the cap means a single failure re-locks immediately, so
            # the worst case is one stranded copy per window. A success clears the entry.
            log(f"type_line: pane {pane} spending one retry after {streak} unverified attempts")
            capped_at = time.time()
            _swallowed_streak[pane] = (streak, capped_at)

        def _stranded():
            """Count this attempt against the pane: the text may be sitting in an input box
            we could not confirm, and every further attempt appends another copy."""
            nxt = min(streak + 1, SWALLOW_MAX_ATTEMPTS)
            _swallowed_streak[pane] = (
                nxt, time.time() if nxt == SWALLOW_MAX_ATTEMPTS and streak < nxt else capped_at)

        before = _count_signals(capture, probe)
        try:
            _tmux(["tmux", "send-keys", "-t", pane, "-l", text], check=True, capture_output=True)
        except Exception as e:
            log(f"type_line: send-keys failed for pane {pane}: {e}")
            return "failed"
        # #250: one 0.3s sample decides, and a pane that repaints later than that reads
        # exactly like a modal that swallowed the keystrokes. The verdict was permanent — the
        # text sat unsent in the input box, the topic was repeatedly told its session was
        # unreachable, and only a human pressing Enter delivered it. In the observation that
        # exposed this, the first sample logged `literal=0->0` on a busy pane, while a later
        # look found the line plainly in its input box.
        #
        # The obvious fix — look again, and act on what the later look sees — was written
        # twice and refused twice by review, for the same reason both times. The evidence is a
        # COUNT of the text anywhere in the capture, which stands in for "our text is in the
        # input box". That proxy holds only while the sole thing that can change the count is
        # our own keystroke. Widen the window and the pane's renderer gets in: an older
        # identical wake line repainted behind a still-open picker raises the count, and the
        # Enter that follows accepts the picker's highlighted option — the exact failure #133
        # exists to prevent. Reproduced against this function, not argued (#253).
        #
        # So the extra samples are OBSERVATION ONLY. They authorise nothing: `confirmed` is
        # decided by the first sample alone, exactly as before, and this code path cannot
        # press an Enter that main would not have pressed. What the later samples do is
        # measure — how often, and how late, a render arrives that the single sample missed.
        # That distribution is the thing nobody has, and without it a longer window is a
        # safety change traded for an unquantified benefit (#252).
        confirmed = False
        cumulative = 0.0
        for sample, wait in enumerate((settle,) + ECHO_EXTRA_WAITS):
            time.sleep(wait)
            cumulative += wait
            measured = _echo_counts(pane, probe)
            if measured is None:
                _stranded()
                log(f"type_line: pane {pane} unreadable after typing — Enter withheld")
                return "swallowed"
            after, geo_after = measured
            if _geometry_moved(geo_before, geo_after):
                break            # abstain below; re-sampling a moved window proves nothing
            rose = after[0] > before[0] or after[1] > before[1]
            if sample == 0:
                confirmed = rose
                if confirmed:
                    break        # the decision, unchanged: first sample, both signals
            elif rose:
                # Not a delivery. A number for #252, and deliberately not acted on: this same
                # rise is what a repaint behind a modal produces.
                log(f"type_line: pane {pane} rendered at sample {sample} (+{cumulative:.1f}s), "
                    f"after the {settle}s decision had already been taken — literal "
                    f"{before[0]}->{after[0]} chip {before[1]}->{after[1]}. Enter NOT sent "
                    f"(#250/#252); the text is stranded in the box.")
                break
        # The two counts must come from the SAME window or comparing them means nothing. A
        # resize can move the row window backwards and admit residue, which reads as a rise
        # with nothing typed (#165 review r2). Abstain instead of guessing; the caller retries.
        if _geometry_moved(geo_before, geo_after):
            _stranded()
            log(f"type_line: pane {pane} geometry moved during verification "
                f"({geo_before!r} -> {geo_after!r}) — Enter withheld")
            return "swallowed"
        # A RISE in either signal, measured across a window proven not to have moved. Equal or
        # falling counts are unresolvable (swallowed, or landed while a stale copy scrolled
        # out), so only a rise is evidence that THIS text rendered — as its literal tail if the
        # pane echoed it, or as a new collapse chip if the engine folded it away (#163).
        #
        # #165 r2 additionally REFUSED a rise whenever the other signal fell, on the argument
        # that residue leaving as content arrives produces the same pair. That veto is gone
        # (#168). In the observed failure the daemon typed `/compact` into a healthy idle pane,
        # refused its own injection, and falsely reported a blocked prompt. The reason is that a
        # claude pane runs on the
        # ALTERNATE SCREEN — `history_size` is 0, so `capture-pane -S -30` clamps to ~23 visible
        # rows and the whole window is under a kilobyte. Content leaves a window that small
        # constantly, so a falling count is the ordinary case there, not a signal.
        #
        # The veto and the geometry check above were aimed at the SAME threat — residue
        # appearing without an injection. The geometry check addresses it precisely, by proving
        # the two counts describe the same rows; the veto addressed it by refusing a whole class
        # of legitimate transitions. Keeping the precise one and dropping the blunt one is the
        # trade this makes, deliberately, with the failure it caused on record in #168.
        #
        # Residual, deliberately open: text the pane renders FOR THE FIRST TIME that happens
        # to quote a collapse marker, or a repaint that re-draws an erased one, still raises
        # the count at unchanged geometry. No substring can tell either from a real paste;
        # only a causal receipt can (#157).
        if not confirmed:
            _stranded()
            # Log the numbers. #168 had to be diagnosed from mechanism because this line said
            # only "swallowed" — the one thing that would have settled it in a second was the
            # counts themselves.
            log(f"type_line: pane {pane} swallowed the text — Enter withheld (modal/picker up?) "
                f"probe={probe[:24]!r} literal={before[0]}->{after[0]} "
                f"chip={before[1]}->{after[1]} geo={geo_after}")
            return "swallowed"
        try:
            _tmux(["tmux", "send-keys", "-t", pane, "Enter"], check=True, capture_output=True)
        except Exception as e:
            # The text DID land and is now sitting unsent in the box. This counts against the
            # pane exactly like a swallow, or a failing Enter would append a fresh copy on
            # every retry forever — the accumulation this cap exists to bound (#133 review r2).
            _stranded()
            log(f"type_line: Enter failed for pane {pane}: {e}")
            return "failed"
        _swallowed_streak.pop(pane, None)
        return "sent"


def _prune_pane_tables():
    """Drop per-pane bookkeeping for panes that no longer exist. Panes churn (every revive
    makes a new one), and both tables are keyed by pane id, so without this they grow for
    the daemon's lifetime. Called from the sweep, which already enumerates live panes."""
    try:
        live = {pane_id for pane_id, _s, _t, _e in (fleet_panes() or [])}
    except Exception:
        return
    if not live:
        return  # an empty/failed fleet read is not evidence that every pane died
    for table in (_swallowed_streak, _pane_locks):
        for pane in [p for p in table if p not in live]:
            table.pop(pane, None)


MODAL_LEAD = (
    "⛔ This session's terminal is waiting on a prompt — it can't receive messages "
    "until that prompt is answered, and I won't press Enter on it (that is how a "
    "session gets switched to a cheaper model by accident)."
)
UNREADABLE_LEAD = (
    "⛔ This session's terminal can't be read at all, so nothing can be delivered to it. "
    "That is not a prompt waiting for you — the pane itself is unreachable (a full-screen "
    "program, or a stuck process holding it). It needs a look."
)


def pane_is_persistently_swallowing(pane):
    """True once a pane has swallowed enough consecutive injections to be worth telling its
    owner about (#254).

    A single swallow is usually not a stuck pane. #250's logging measured the common case:
    the text renders only after the first decision, and the next sweep tick delivers it.
    Reporting on that first swallow told the owner their session was unreachable while it was
    in fact about to receive the message. An alarm that fires on the recoverable case trains
    its reader to ignore it, and then the unrecoverable one is missed too.

    The other failure mode already works this way: the `failed` route escalates only after
    UNREADABLE_ESCALATE_AFTER consecutive failures. This gives `swallowed` the same shape,
    using the streak type_line already keeps. A genuinely stuck pane is still reported, one
    sweep tick later than before."""
    return _swallowed_streak.get(pane, (0, 0.0))[0] >= SWALLOW_MAX_ATTEMPTS


def report_blocked_pane(thread_id, pane, what, lead=MODAL_LEAD):
    """Tell the topic that its session can't be reached, and show the terminal. Without this
    the session just goes quiet: #133's evidence is a codex pane held on the rate-limit picker
    while every delivery silently failed. Rate-limited per topic
    so a stuck pane doesn't become a message loop.

    `lead` names WHICH way it is unreachable. The default is the modal case. #188 added the
    unreadable-pane case, and the two must not share copy: telling the owner to answer a prompt
    when there is no prompt sends them looking for something that isn't there.

    The cooldown is per daemon process. `tg-bridge notify` runs maybe_nudge() in its own
    process (bridge/cli.py), which starts with an empty table, so a burst of local
    notifications into a stuck pane can still produce one report each — bounded by the
    notification rate, not self-feeding."""
    tid = str(thread_id)
    now = time.time()
    with _blocked_lock:
        if now - _blocked_reported.get(tid, 0) < BLOCKED_REPORT_COOLDOWN:
            return False
        # Claim the slot so two threads can't both send, but stamp the REAL time only after
        # the send succeeds — a failed report must not buy 30 minutes of silence.
        _blocked_reported[tid] = now
    try:
        tail = peek_pane(pane, lines=12)
    except Exception:
        tail = "(couldn't read the terminal)"
    tail = tail.replace("`", "'")  # a stray fence in the capture would break out of ours
    try:
        delivered = reply(load_config(), thread_id, (
            f"{lead}\n\n"
            f"Undelivered: {what}\n\n"
            f"```\n{tail}\n```"
        ))
        if not delivered:      # closed/deleted topic: nothing was said, so don't sit on the
            with _blocked_lock:  # cooldown as though it had been
                if _blocked_reported.get(tid) == now:
                    _blocked_reported.pop(tid, None)
            return False
    except Exception as e:
        with _blocked_lock:  # release the slot: nothing was delivered
            if _blocked_reported.get(tid) == now:
                _blocked_reported.pop(tid, None)
        log(f"blocked-pane report failed for topic {tid}: {e}")
        return False
    log(f"blocked pane {pane} reported (topic {tid})")
    return True


def maybe_nudge(thread_id, pane, wake_claim=None):
    """Type a wake-up line into the session's tmux pane if its inbox is still unread."""
    try:
        inbox = state_path("topics", str(thread_id), "inbox.jsonl")
        with validate_wake_claim(inbox, wake_claim) as claim_is_current:
            if not claim_is_current:
                return None
            if unread_count(thread_id) <= 0:
                return False  # a blocking ask/recv already consumed it
            if not pane_alive(pane):
                log(f"nudge skipped, pane {pane} gone (topic {thread_id})")
                return False
            text = (
                f"[tg-bridge] New Telegram message in your topic — "
                f"run `tg-bridge recv --topic {thread_id}` and act on it."
            )
            status = type_line(pane, text)
            if status != "sent":
                log(f"nudge not delivered to pane {pane} (topic {thread_id}): {status}")
                if status == "swallowed" and pane_is_persistently_swallowing(pane):
                    report_blocked_pane(thread_id, pane, "a new Telegram message")
                return False
            log(f"nudged pane {pane} for topic {thread_id}")
            return True
    except Exception as e:
        log(f"nudge failed for topic {thread_id}: {e}")
        return False


_auto_reviving = set()
_auto_revive_lock = threading.Lock()


def should_auto_revive(info):
    return (isinstance(info, dict) and bool(info.get("ended"))
            and bool(info.get("session_id")) and not info.get("feed"))


def maybe_auto_revive(cfg, thread_id, cause="auto"):
    tid = str(thread_id)
    if tid in pending_reopens:
        # A3: a reopen question is open for this topic. An inbound message must not revive
        # it — that would spend the full context on the expensive option precisely because
        # they had not answered yet. Their message still reaches the inbox and is delivered once
        # they choose (C4).
        return
    entry = read_registry().get(tid)
    if not should_auto_revive(entry):
        return
    # A3 is a property of the SESSION, not of how the revive happened to be triggered. Gating
    # only on a pending record made the guarantee last exactly as long as that record: round
    # 2, finding 2 showed that once it is lost, the next ordinary message resumes the whole
    # 350k context with no question at all — the precise unapproved spend this feature was
    # built to stop. Asking here costs them nothing, because handle_message has already put
    # their message in the inbox: it is delivered the moment they choose (C4).
    if (entry.get("engine") or "claude") == "claude" and _reopen_needs_asking(entry):
        if not offer_reopen_choice(cfg, thread_id, entry):
            # A3 has no exception for "the question could not be delivered". Round 2 chose to
            # revive anyway, to avoid stranding the session; round 3 was right to reject that
            # — a closed or deleted topic has nobody waiting on the session, so the spend buys
            # nothing, and A3 says "including on restart or ANY error path". The message is
            # already in the inbox; reopening the topic asks again.
            log(f"auto-revive topic {tid}: needs a choice and the question could not be "
                f"delivered — leaving it down rather than spending unasked")
        return
    with _auto_revive_lock:
        if tid in _auto_reviving:
            return
        _auto_reviving.add(tid)

    def _revive():
        try:
            # brief=True consumes the returned briefing task inline; brief=False is only
            # needed by mass restore, which fans those slow tasks out to separate threads.
            # cause="auto": this session's pane died and we relaunched it on an inbound
            # message. Nothing rebooted — telling it otherwise makes it misread its own
            # reaped background tasks as reboot fallout (#167).
            status, _task = revive_one(cfg, tid, entry, brief=True, cause=cause)
            log(f"auto-revive topic {tid}: {status}")
        except Exception as e:
            log(f"auto-revive topic {tid} failed: {e}")
        finally:
            with _auto_revive_lock:
                _auto_reviving.discard(tid)

    try:
        threading.Thread(target=_revive, daemon=True).start()
    except Exception as e:
        with _auto_revive_lock:
            _auto_reviving.discard(tid)
        log(f"auto-revive thread failed for topic {tid}: {e}")


def schedule_nudge(thread_id, wake_claim):
    if wake_claim is None:
        return  # session already has unread messages and was nudged for the batch
    info = read_registry().get(str(thread_id), {})
    if info.get("ended"):
        return
    pane = info.get("pane")
    if pane:
        threading.Timer(NUDGE_DELAY, maybe_nudge, args=(thread_id, pane, wake_claim)).start()


def mark_ended(thread_id):
    def _end(reg):
        if str(thread_id) in reg:
            reg[str(thread_id)]["ended"] = now_iso()
    update_registry(_end)


def lifecycle_loop(cfg):
    """Detect dead session terminals: notify the topic, close it, retire the registry entry."""
    while True:
        time.sleep(LIFECYCLE_POLL)
        try:
            for thread_id, info in read_registry().items():
                pane = info.get("pane")
                if not pane or info.get("ended"):
                    continue  # no pane = liveness unknowable; ended = already handled
                if pane_alive(pane):
                    continue
                mark_ended(thread_id)
                name = info.get("name", "?")
                try:
                    reply(cfg, int(thread_id), f"🔚 Session '{name}' ended — its terminal is gone. Closing this topic.")
                    api(cfg["bot_token"], "closeForumTopic", {
                        "chat_id": cfg["chat_id"], "message_thread_id": int(thread_id),
                    })
                except Exception as e:
                    log(f"lifecycle notice failed for topic {thread_id}: {e}")
                log(f"session ended: topic {thread_id} ({name}), pane {pane} gone")
        except Exception as e:
            log(f"lifecycle_loop error: {e}")


def set_topic_closed(thread_id, closed):
    """Record a forum topic's open/closed state on its registry entry (#161).

    Telegram offers bots no way to READ this — there is no getForumTopic, and sendChatAction
    is not enforced against closed topics (measurement returned OK for open and long-dead
    topics alike). So the state is only ever learned from an event: the
    forum_topic_closed / forum_topic_reopened service messages, or a send that Telegram
    rejects with TOPIC_CLOSED. Absent an event, a topic counts as open."""
    tid = str(thread_id or "")
    if not tid or tid == "0":                     # General is not a closable topic
        return

    def _stamp(reg):
        entry = reg.get(tid)
        if entry is None:
            return
        if closed:
            entry["closed"] = True
        else:
            entry.pop("closed", None)
    update_registry(_stamp)


_TOPIC_GONE_MARKERS = (
    "topic_closed", "topic is closed",
    # A DELETED topic is equally undeliverable and equally not part of the morning list.
    # The first version deliberately excluded it, which left a deleted topic in the digest
    # for ever with no event that could ever clear it (Codex review of PR #162).
    "topic_deleted", "topic was deleted",
)


def _is_topic_gone_error(exc):
    """True when Telegram says the destination topic is closed or deleted — i.e. this topic
    can no longer receive messages. api() carries Telegram's description on the exception."""
    return any(m in str(exc).lower() for m in _TOPIC_GONE_MARKERS)


def reply(cfg, thread_id, text):
    """Send a daemon notice to a topic. Returns True when it was delivered.

    HTML-render agent/daemon Markdown + auto-split + plain-text fallback (#89). The ⚙️ prefix
    is safe literal text; thread_id 0 (General) takes no thread id.

    A send rejected because the topic is closed/deleted is the only way to learn about a topic
    that was closed BEFORE the bridge started tracking the events (#161), so it is classified
    and swallowed — the message is already undeliverable, and raising would turn a closed topic
    into a daemon error on every context warning. It returns FALSE though: callers that treat a
    notice as having been delivered before starting side effects must be able to tell. Auto
    carry-forward posted its start notice and then compacted and resumed the session regardless,
    leaving the owner with neither the notice nor the kill-switch (Codex review of PR #162).
    Any other failure propagates unchanged."""
    try:
        send_message(cfg["bot_token"], cfg["chat_id"], f"⚙️ {text}", thread_id or None)
        return True
    except RuntimeError as e:
        if not _is_topic_gone_error(e):
            raise
        set_topic_closed(thread_id, True)
        log(f"topic {thread_id} can no longer receive messages ({e}) — marked closed, "
            f"reply dropped")
        return False


def _ctx_record_valid(ctx):
    """True for a statusline record that can be believed (#158).

    Age is not what makes a record wrong — an invalid record is. This rejects the zeroed dump
    statusline.sh used to emit when jq was missing from a daemon-spawned session's PATH:
    every substitution came back empty and the `${pct:-0}` defaults turned that into
    {"session_id":"", "pct":0, "cost":0}, rewritten on EVERY render, so it stayed permanently
    fresh and permanently wrong — and CTX_STALE could never catch it (Codex review of PR
    #159). An empty session_id is never legitimate; a real brand-new session at 0% carries
    its id. The producer no longer writes those, but old ones are still on disk."""
    if not isinstance(ctx, dict) or not ctx.get("session_id"):
        return False
    try:
        pct = float(ctx["pct"])
    except (KeyError, TypeError, ValueError):
        return False
    return 0 <= pct <= 100


def read_context(pane, max_age=CTX_STALE):
    """The pane's statusline record, or None. `max_age=None` accepts it at any age.

    statusline.sh runs on every RENDER — each turn, not only when the number changes — so
    `ts` measures ACTIVITY, not correctness, and an idle session's record simply stops being
    rewritten while its percentage stays exactly what it was. Callers needing proof of a LIVE
    session keep the default: session_id_for_pane must not persist a stale id from a prior
    session in a reused pane. Callers that only want the current number pass max_age=None."""
    path = state_path("context", pane.lstrip("%") + ".json")
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            ctx = json.load(f)
    except (ValueError, OSError):
        return None
    if not _ctx_record_valid(ctx):
        return None
    if max_age is not None and time.time() - ctx.get("ts", 0) > max_age:
        return None
    return ctx


def engine_of_pane(pane):
    """'claude'/'codex'/None for a pane, by looking it up in the live fleet."""
    if not pane:
        return None
    panes = fleet_panes()
    if not panes:
        return None
    for pane_id, _session, _title, engine in panes:
        if pane_id == pane:
            return engine
    return None


def _pane_locators(pane):
    """(pid, cwd) for a pane, each degrading to None on its own. One tmux hiccup must not
    cost the other locator: either one alone can still resolve the session's rollout."""
    locators = []
    for lookup in (pane_pid, pane_cwd):
        try:
            locators.append(lookup(pane))
        except Exception as e:
            log(f"pane {pane}: {lookup.__name__} failed: {e}")
            locators.append(None)
    return tuple(locators)


def _codex_ctx_dict(pane):
    """Rollout-derived {'pct': N} for a known-codex pane, or None. Used by the
    dashboard loops, which already hold the pane's engine so they skip the
    engine lookup context_for() would otherwise redo. Never raises."""
    pid, cwd = _pane_locators(pane)
    try:
        pct = codex_ctx.ctx_pct_for_pane(pid, cwd)
    except Exception as e:
        log(f"codex ctx lookup failed for pane {pane}: {e}")
        return None
    return {"pct": pct} if pct is not None else None


def _proc_stat_fields(pid):
    """/proc/<pid>/stat from field 3 onward, as a list. comm can contain spaces and
    parentheses, so the split anchors on the LAST ") ". fields[n] is overall field n+3."""
    with open(f"/proc/{pid}/stat") as f:
        return f.read().rsplit(") ", 1)[1].split()


def _pane_start_time(pane):
    """Unix time the pane's FOREGROUND process started, or None if it can't be established.

    Waiving the age check needs a bound on WHOSE record it is: context/<pane>.json is keyed by
    pane, and both pane ids and panes get reused. A record written before the process now in
    the pane started belongs to a previous occupant.

    tmux's `#{pane_pid}` is the FIRST process in the pane, which for a bridge-launched pane is
    the engine itself (launch_pane execs it) but for a manually registered one is a persistent
    shell older than every record ever written there — so using it alone left session A trusted
    for the shell's whole lifetime, weaker than the 600s cutoff it replaced (Codex round 3 of
    PR #159). So resolve the foreground process group (tpgid) and time that instead, failing
    closed when it can't be read."""
    pid = pane_pid(pane)
    if not pid:
        return None
    try:
        fields = _proc_stat_fields(pid)
        tpgid = int(fields[5])                        # foreground process group, field 8
        if tpgid <= 0:
            return None                               # no controlling terminal — unprovable
        if tpgid != pid:
            fields = _proc_stat_fields(tpgid)
        ticks = int(fields[19])                       # starttime, field 22, in clock ticks
        with open("/proc/stat") as f:
            btime = next(int(line.split()[1]) for line in f if line.startswith("btime "))
        return btime + ticks / os.sysconf("SC_CLK_TCK")
    except (OSError, ValueError, IndexError, StopIteration):
        return None


def context_for(pane, engine=None):
    """Unified context readout for a pane (#158). Returns {'pct': N} (claude may also carry
    'cost'), or None when there is genuinely nothing to report. Pass `engine` when the caller
    has already resolved it — otherwise this costs a fleet lookup, and warning_loop resolves
    the engine for every topic on every poll anyway.

    Codex is resolved FIRST. A pane that used to run Claude keeps that session's
    context/<pane>.json forever. Measurements found stale Claude values, weeks old, beside
    different live Codex readings. Only CTX_STALE was hiding them; reading the file first
    would report those confidently wrong numbers.

    For a claude pane a FRESH record answers immediately. An AGED one is accepted only once
    it is shown to belong to the process currently in that pane, because age alone is not the
    right test: the record is only rewritten when Claude Code re-renders its statusline — i.e.
    on activity — so an idle session's record ages out while its percentage stays exactly what
    it was. In measurement, nearly every idle pane answered /ctx with "no fresh context data"
    while its status line was still displaying the number.

    What CTX_STALE WAS providing, incidentally, was a 600-second bound on how long a record
    could outlive its writer. Waiving age without replacing that bound made a dead pane's
    record answer /ctx forever, and let a failed session-B write leave session A's number
    standing indefinitely (Codex round 2 of PR #159). The bound is now explicit and tighter:
    the pane must be alive, and the record must predate nothing — it must have been written
    AFTER the current process started. Anything unprovable (no pane, no pid, unreadable
    /proc) returns None rather than a number."""
    resolved = engine or engine_of_pane(pane)
    if resolved == "codex":
        return _codex_ctx_dict(pane)      # never let a stale claude file shadow this
    ctx = read_context(pane)
    if ctx is not None:
        return ctx                        # fresh: no tmux, no /proc, nothing to prove
    aged = read_context(pane, max_age=None)
    # An unresolved engine cannot be shown to be the claude session that wrote the record, so
    # it does not get the waiver — an idle shell sitting in a registered pane must not answer
    # with a long-dead session's number.
    if aged is None or resolved != "claude" or not pane_alive(pane):
        return None
    started = _pane_start_time(pane)
    if started is None or aged.get("ts", 0) < started:
        return None                       # a previous occupant of this pane id wrote it
    return aged


# The marker a session looks for to know its turn was deliberately stopped. Named, because
# skill/SKILL.md quotes it verbatim and a session matches on it — two copies of a literal that
# must agree is a silent failure waiting for the day somebody edits one (#224 review).
INTERRUPT_PREFIX = "[Interrupt from the owner via Telegram]"

STOP_WORDS = ("stop", "стоп")  # voice transcripts can't carry '!' — leading stop-word interrupts


def interrupt_session(cfg, thread_id, instruction=None):
    """Escape kills the session's current turn; optional instruction becomes its next input."""
    pane = read_registry().get(str(thread_id), {}).get("pane")
    if not pane or not pane_alive(pane):
        reply(cfg, thread_id, "Can't interrupt: no live terminal bound to this topic.")
        return
    # Under the pane lock (#165): a long instruction collapses into a paste chip exactly like
    # any other long input, so writing it inside another injection's capture→type→capture
    # window would raise that injection's chip count and authorise ITS Enter. The reply()
    # calls stay outside — they are network I/O and must not hold a pane lock.
    try:
        with _pane_lock(pane):
            _tmux(["tmux", "send-keys", "-t", pane, "Escape"], check=True, capture_output=True)
            if instruction:
                time.sleep(1)
                line = f"{INTERRUPT_PREFIX} {instruction}"
                _tmux(["tmux", "send-keys", "-t", pane, "-l", line], check=True, capture_output=True)
                time.sleep(0.3)
                _tmux(["tmux", "send-keys", "-t", pane, "Enter"], check=True, capture_output=True)
    except PaneLockUnavailable as e:
        # Say so rather than swallowing it: they pressed stop and nothing stopped.
        log(f"interrupt: {e} (topic {thread_id})")
        reply(cfg, thread_id, "Couldn't interrupt — the terminal is locked by another "
                              "delivery that hasn't finished. Try again in a moment.")
        return
    if instruction:
        reply(cfg, thread_id, "⏹ Interrupted — your instruction was handed to the session.")
    else:
        reply(cfg, thread_id, "⏹ Interrupted — session stopped, awaiting your instruction.")
    log(f"interrupt -> pane {pane} (topic {thread_id}, instruction={bool(instruction)})")


def is_voice_interrupt(transcript):
    first = transcript.strip().lstrip("!¡").split()[:1]
    return bool(first) and first[0].strip(".,!?…").lower() in STOP_WORDS


def peek_pane(pane, lines=25):
    out = _tmux(
        ["tmux", "capture-pane", "-t", pane, "-p"], check=True, capture_output=True, text=True,
    ).stdout
    visible = [line.rstrip() for line in out.splitlines() if line.strip()]
    return "\n".join(visible[-lines:]) or "(terminal is blank)"


def pane_engine(command, pane_pid):
    """Engine a tmux pane runs: 'claude', 'codex', or None. The codex CLI is a node
    script, so its panes report 'node' — disambiguate via the process argv."""
    if command in ("claude", "codex"):
        return command
    if command == "node":
        try:
            with open(f"/proc/{pane_pid}/cmdline", "rb") as f:
                argv = f.read().decode(errors="replace").split("\0")
        except (OSError, ValueError):
            return None
        if any(a == "codex" or a.endswith("/codex") for a in argv[:3]):
            return "codex"
    return None


def fleet_panes():
    """All tmux panes running a session engine: list of (pane_id, session, title, engine),
    or None if tmux is down."""
    out = _tmux(
        ["tmux", "list-panes", "-a", "-F",
         "#{pane_id}\t#{session_name}\t#{pane_current_command}\t#{pane_pid}\t#{pane_title}"],
        capture_output=True, text=True,
    )
    if out.returncode != 0:
        return None
    panes = []
    for row in out.stdout.splitlines():
        pane_id, session, command, pane_pid, title = (row.split("\t") + [""] * 5)[:5]
        engine = pane_engine(command, pane_pid)
        if engine:
            panes.append((pane_id, session, title.lstrip("✳⠐⠂✶ ").strip() or "(no title)", engine))
    return panes


def registry_by_pane():
    return {
        info.get("pane"): (tid, info)
        for tid, info in read_registry().items()
        if info.get("pane") and not info.get("ended")
    }


def sessions_overview():
    """All tmux panes running a session engine, merged with bridge registry info."""
    panes = fleet_panes()
    if panes is None:
        return "tmux isn't running — no sessions."
    by_pane = registry_by_pane()
    lines = []
    for pane_id, session, title, engine in panes:
        icon = by_pane[pane_id][1].get("icon", "•") if pane_id in by_pane else "•"
        name = by_pane[pane_id][1].get("name", session) if pane_id in by_pane else session
        tag = " [codex]" if engine == "codex" else ""
        line = f"{icon} {name} — {title}{tag}"
        if pane_id in by_pane:
            tid, info = by_pane[pane_id]
            ctx = read_context(pane_id)
            if ctx is None and engine == "codex":
                ctx = _codex_ctx_dict(pane_id)
            pct = f"{int(float(ctx['pct']))}% ctx" if ctx else "ctx ?"
            cost = f", ${ctx['cost']:.2f}" if ctx and "cost" in ctx else ""
            unread = unread_count(int(tid))
            line += f"\n  topic {tid} '{info.get('name', '?')}' | {pct}{cost} | unread {unread}"
        else:
            line += "\n  not connected to the bridge"
        lines.append(line)
    if not lines:
        return "No sessions running in tmux."
    headless = [
        f"{info.get('icon', '•')} {info.get('name', '?')} — topic {tid}, "
        + ("feed (no warnings/nudges)" if info.get("feed") else "registered without tmux (can't inspect)")
        for tid, info in read_registry().items()
        if not info.get("pane") and not info.get("ended")
    ]
    return "Sessions in tmux:\n" + "\n".join(lines + headless)


TZ_OFFSET = int(os.environ.get("TG_BRIDGE_TZ_OFFSET", "3"))  # user-facing times, hours from UTC


def local_hhmm(epoch=None):
    t = time.gmtime((epoch if epoch is not None else time.time()) + TZ_OFFSET * 3600)
    return time.strftime("%H:%M", t)


def local_datetime(epoch):
    return time.strftime("%a %d %b %H:%M", time.gmtime(epoch + TZ_OFFSET * 3600))


def reset_epoch(block):
    try:
        return datetime.fromisoformat((block.get("resets_at") or "").replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


DASH_POLL = int(os.environ.get("TG_BRIDGE_DASH_POLL", "60"))  # dashboard rebuild interval
DASH_ORCH_EVERY = 5  # refresh issue-queue counts every Nth tick (gh search API is rate-limited)
DASH_TS_REFRESH = 300  # re-edit for the timestamp alone at most this often
USAGE_CACHE = "/tmp/claude-usage-cache.json"  # written by ~/.claude/statusline.sh
GH_BIN = shutil.which("gh") or os.path.expanduser("~/bin/gh")
QUEUE_LABELS = ("ready", "claimed", "running", "review", "human-review", "blocked", "failed")


# Both values are interpolated into a GitHub SEARCH EXPRESSION, and `quote()` protects the
# transport, not the meaning: an owner of `x is:pr` or a prefix carrying a `"` changes what is
# being searched for, and a newline in either breaks the dashboard line it is rendered into
# (#222 review). "Non-blank" was never the contract — "a name GitHub could actually have" is.
# Anything else is a malformed setting, and a malformed setting means the feature is off.
_GH_OWNER = re.compile(r"\A[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})\Z")
_LABEL_PREFIX = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]{0,48}\Z")
_GH_REPO = re.compile(r"\A[A-Za-z0-9._-]{1,100}/[A-Za-z0-9._-]{1,100}\Z")


def _matches(pattern, value):
    return isinstance(value, str) and bool(pattern.match(value.strip()))


def issue_queue_config():
    """(owner, label_prefix) for the dashboard's work-queue line, or None if unconfigured.

    This used to be one GitHub account and one label prefix compiled in, which made the line
    meaningless anywhere else and named the author in a file that ships (#204 D6). Absent
    config means the feature is OFF — `issue_queue_counts` returns nothing and both line
    builders already drop a line with no counts, so there is no extra branch to get wrong.

        "issue_queue": {"owner": "your-github-user", "label_prefix": "work"}
    """
    try:
        cfg = load_config()
    except (OSError, ValueError, TypeError, RecursionError, SystemExit):
        return None
    queue = cfg.get("issue_queue") if isinstance(cfg, dict) else None
    if not isinstance(queue, dict):
        return None
    owner, prefix = queue.get("owner"), queue.get("label_prefix")
    if not _matches(_GH_OWNER, owner) or not _matches(_LABEL_PREFIX, prefix):
        return None
    return owner.strip(), prefix.strip()


def issue_queue_counts():
    """Open-issue count per `<prefix>:<label>` across the configured owner's repositories."""
    configured = issue_queue_config()
    if configured is None:
        return {}
    owner, prefix = configured
    env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
    counts = {}
    for label in QUEUE_LABELS:
        q = quote(f'user:{owner} is:issue is:open label:"{prefix}:{label}"')
        try:
            out = subprocess.run(
                [GH_BIN, "api", f"search/issues?q={q}&per_page=1", "-q", ".total_count"],
                capture_output=True, text=True, timeout=30, env=env,
            )
            counts[label] = int(out.stdout.strip()) if out.returncode == 0 else None
        except (subprocess.TimeoutExpired, ValueError):
            counts[label] = None
    return counts


def ppid_of(pid):
    try:
        with open(f"/proc/{pid}/stat") as f:
            return int(f.read().rsplit(")", 1)[1].split()[1])
    except (OSError, ValueError, IndexError):
        return None


def headless_worker_count():
    """claude/codex processes with no tmux pane in their ancestry — headless workers."""
    matched = set()
    for args in (("-x", "claude"), ("-f", "codex exec")):
        out = subprocess.run(["pgrep", *args], capture_output=True, text=True)
        matched.update(int(p) for p in out.stdout.split())
    panes = _tmux(["tmux", "list-panes", "-a", "-F", "#{pane_pid}"],
                           capture_output=True, text=True)
    pane_pids = set(int(p) for p in panes.stdout.split()) if panes.returncode == 0 else set()
    count = 0
    for pid in matched:
        cur, headless = pid, None
        for _ in range(20):  # walk up; depth-capped
            if cur in pane_pids:
                headless = False
                break
            if cur != pid and cur in matched:
                headless = False  # codex exec wrapper + vendor child: count only the topmost
                break
            cur = ppid_of(cur)
            if not cur or cur == 1:
                headless = True
                break
        if headless or headless is None:  # cap exhausted: no pane found above, count it
            count += 1
    return count


def issue_queue_line(counts):
    if not counts or all(v is None for v in counts.values()):
        return None
    running = (counts.get("claimed") or 0) + (counts.get("running") or 0)
    parts = [
        f"{running} running",
        f"{counts.get('ready') or 0} queued",
        f"{counts.get('review') or 0} review",
        f"{counts.get('human-review') or 0} human-review",
    ]
    parts += [f"{counts[k]} {k}" for k in ("blocked", "failed") if counts.get(k)]
    try:
        parts.append(f"{headless_worker_count()} workers")
    except Exception as e:
        log(f"worker count failed: {e}")
    configured = issue_queue_config()
    name = configured[1].title() if configured else "Queue"
    return f"🎼 {name}: " + " · ".join(parts)


def _scoped_weekly_pct(u, model_name):
    """Percent + reset epoch for a model-scoped weekly limit from the usage cache. Model-scoped
    usage (e.g. Fable) is NOT a top-level field — it's a `weekly_scoped` entry inside limits[]
    whose scope.model.display_name matches. Returns (percent:int, reset_epoch) or None if absent."""
    for lim in u.get("limits") or []:
        if lim.get("group") != "weekly":
            continue
        model = (lim.get("scope") or {}).get("model") or {}
        if (model.get("display_name") or "").casefold() == model_name.casefold():
            pct = lim.get("percent")
            if pct is None:  # entry present but percent unknown — omit rather than report a false 0%
                return None
            return round(pct), reset_epoch(lim)
    return None


def account_usage_line():
    try:
        with open(USAGE_CACHE) as f:
            u = json.load(f)
    except (OSError, ValueError):
        return None
    five = u.get("five_hour") or {}
    week = u.get("seven_day") or {}
    line = f"👤 Account: 5h {round(five.get('utilization', 0))}% used"
    five_reset = reset_epoch(five)
    if five_reset:
        line += f" (resets {local_hhmm(five_reset)} UTC+{TZ_OFFSET})"
    line += f" · week {round(week.get('utilization', 0))}% used"
    week_reset = reset_epoch(week)
    if week_reset:
        line += f" (resets {local_datetime(week_reset)})"
    fable = _scoped_weekly_pct(u, "Fable")
    if fable is not None:
        fpct, freset = fable
        line += f" · Fable week {fpct}% used"
        if freset:
            line += f" (resets {local_datetime(freset)})"
    return line


def _codex_window_label(window_min):
    """Human label for a codex rate-limit window from its length in minutes (#103). Codex
    reports each limit's `window_minutes` (5h window = 300, weekly = 10080). The
    primary/secondary SLOT does NOT fix the window — a pro account's `primary` can itself be
    the weekly window — so the label must come from the length, not the slot. Returns None
    when the length is unknown (caller falls back to "?")."""
    if not isinstance(window_min, (int, float)):
        return None
    m = round(window_min)
    if m <= 360:                       # ~5h (300) and shorter
        return f"{round(m / 60)}h" if m >= 60 else f"{m}m"
    if 9000 <= m <= 11520:             # ~weekly (10080), ±1 day slack
        return "weekly"
    if m < 1440:                       # sub-daily
        return f"{round(m / 60)}h"
    return f"{round(m / 1440)}d"       # multi-day (e.g. 1440 -> 1d)


def codex_usage_line(cwd=None):
    """Codex account usage, mirroring the claude /usage style. Each window is labelled from
    its ACTUAL length (5h / weekly / …) — NOT from its primary/secondary slot, since a pro
    account's primary window can itself be the weekly one (#103). Windows without a
    percentage are omitted (no more "weekly n/a"). `cwd` scopes to a specific live session's
    rollout; cwd=None uses the most recent rollout across all sessions — account-wide, for a
    global `/usage codex`. Returns None when no rollout / no rate-limit data is available."""
    try:
        u = codex_ctx.usage_for_cwd(cwd) if cwd else codex_ctx.usage_latest()
    except Exception as e:
        log(f"codex usage lookup failed for {cwd or 'latest'}: {e}")
        return None
    if not u:
        return None
    plan = u.get("plan_type") or "?"
    segments = []
    for pct_key, win_key, reset_key in (
        ("primary_pct", "primary_window_min", "primary_reset"),
        ("secondary_pct", "secondary_window_min", "secondary_reset"),
    ):
        pct = u.get(pct_key)
        if not isinstance(pct, (int, float)):
            continue  # window absent for this account/snapshot — omit, don't print "n/a"
        win_min = u.get(win_key)
        seg = f"{_codex_window_label(win_min) or '?'} {100 - pct:.0f}% left"
        reset = u.get(reset_key)
        if isinstance(reset, (int, float)):
            # short (≈5h) windows show just HH:MM; longer ones show the full datetime.
            if isinstance(win_min, (int, float)) and win_min <= 360:
                seg += f" (resets {local_hhmm(reset)} UTC+{TZ_OFFSET})"
            else:
                seg += f" (resets {local_datetime(reset)})"
        segments.append(seg)
    if not segments:
        return None
    return f"🤖 Codex usage ({plan}): " + " · ".join(segments)


def dashboard_body(orch_line):
    panes = fleet_panes()
    if panes is None:
        lines = ["(tmux not running)"]
    else:
        by_pane = registry_by_pane()
        lines = []
        for pane_id, session, title, engine in panes:
            tag = " [codex]" if engine == "codex" else ""
            if pane_id in by_pane:
                tid, info = by_pane[pane_id]
                ctx = read_context(pane_id)
                if ctx is None and engine == "codex":
                    ctx = _codex_ctx_dict(pane_id)
                pct = f"{int(float(ctx['pct']))}%" if ctx else "?%"
                cost = f" ${ctx['cost']:.0f}" if ctx and "cost" in ctx else ""
                unread = unread_count(int(tid))
                flag = f" 📨{unread}" if unread else ""
                icon = info.get("icon", "•")
                name = info.get("name", session)
                lines.append(f"{icon} {name} — {title}{tag} | t{tid} {pct}{cost}{flag}")
            else:
                lines.append(f"• {session} — {title}{tag} | not connected")
        if not lines:
            lines = ["(no sessions in tmux)"]
    tail = [line for line in (orch_line, account_usage_line()) if line]
    if tail:
        lines.append("")  # blank line: separate the fleet from the issue-queue/account block
        lines.extend(tail)
    return "\n".join(lines)


def load_dash_state():
    try:
        with open(state_path("dashboard.json")) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_dash_state(dash):
    path = state_path("dashboard.json")
    with open(path + ".tmp", "w") as f:
        json.dump(dash, f)
    os.replace(path + ".tmp", path)


def ensure_dashboard_message(cfg, dash):
    if dash.get("message_id"):
        return dash
    msg = api(cfg["bot_token"], "sendMessage", {
        "chat_id": cfg["chat_id"], "text": "📟 Claude fleet — initializing…",
    })
    dash = {"message_id": msg["message_id"]}
    save_dash_state(dash)
    try:
        api(cfg["bot_token"], "pinChatMessage", {
            "chat_id": cfg["chat_id"], "message_id": msg["message_id"],
            "disable_notification": True,
        })
    except Exception as e:
        log(f"dashboard pin failed (needs pin rights): {e}")
    log(f"dashboard message created: {msg['message_id']}")
    return dash


def dashboard_loop(cfg):
    """Maintain one pinned, auto-edited fleet overview message in General."""
    dash = load_dash_state()
    last_body, last_edit, orch_counts, tick = None, 0.0, None, 0
    while True:
        try:
            if tick % DASH_ORCH_EVERY == 0:
                fresh = issue_queue_counts()
                if any(v is not None for v in fresh.values()):
                    orch_counts = fresh
            body = dashboard_body(issue_queue_line(orch_counts))
            now = time.time()
            if body != last_body or now - last_edit >= DASH_TS_REFRESH:
                dash = ensure_dashboard_message(cfg, dash)
                text = f"📟 Claude fleet — updated {local_hhmm()} (UTC+{TZ_OFFSET})\n{body}"
                try:
                    api(cfg["bot_token"], "editMessageText", {
                        "chat_id": cfg["chat_id"],
                        "message_id": dash["message_id"], "text": text,
                    })
                    last_body, last_edit = body, now
                except RuntimeError as e:
                    if "message is not modified" in str(e):
                        last_body, last_edit = body, now
                    elif "message to edit not found" in str(e):
                        dash = {}  # deleted by hand; recreate next tick
                        save_dash_state(dash)
                    else:
                        raise
        except Exception as e:
            log(f"dashboard_loop error: {e}")
        tick += 1
        time.sleep(DASH_POLL)


HELP_TEXT = """Bridge commands (work in any session topic):
/help — this list
/sessions — all Claude sessions running in tmux (topic, context, unread)
/kill — terminate this topic's session (asks for a 'yes' confirmation)
/claude <name> [@~/path][: <task>] — start a new Claude session; it opens its own topic. With a task it echoes it and waits for your go (inline 'go' skips); without one it just waits for instructions there
/codex <name> [@~/path][: <task>] — same, but a Codex (OpenAI) session
/ctx — session's context-window usage
/model [name] — switch this session's model live (opus/sonnet/haiku, or a full id), keeping its conversation; no arg lists the options
/carryforward (or /cf) — bridge-driven: the session writes its carry-forward (state + next-steps) to a GitHub issue (creating one if none), then I run /compact and auto-resume it from those next-steps. Send any message to halt.
/usage [claude|codex] — account limits (5h window %, weekly %, reset time); bare /usage uses this topic's engine, `/usage claude` / `/usage codex` force either from any topic
/stop — interrupt the session's current turn
/peek — last ~25 lines of the session's terminal
!<text> — interrupt + hand <text> to the session as its next instruction
voice message starting with "stop"/"стоп" — same as !<text>
any other /command (e.g. /compact, /cost) — typed into the session's terminal

Convention: sessions echo their understanding of a request and wait for your "go" \
(да/давай/ok also work). Put "go" inside the request itself to skip the wait."""


SPAWN_RE = re.compile(
    r"^/(?P<cmd>claude|spawn|codex)\s+(?P<name>[^:@\n]+?)\s*(?:@(?P<path>\S+))?\s*(?::\s*(?P<task>.+))?$",
    re.S,
)
SPAWN_VERIFY_DELAY = 20  # seconds before checking the spawned TUI actually runs

SPAWN_USAGE = """Couldn't parse that. Examples:
/claude research-helper — session opens its own topic and waits for instructions there
/claude fix-bot @~/openclaw: investigate yesterday's token spike — with a working dir and a task
/codex port-script @~/tools: rewrite fetch.sh in python — same shape, Codex session"""

SPAWN_BOOTSTRAP = """You were spawned from Telegram by the owner via the claude-telegram-bridge /claude command. \
Set up your channel to them before anything else (the tg-channel skill has the full protocol):
1. Run: tg-bridge register --name {name_q} — note the printed topic_id N.
2. Echo your understanding of the task in 1-3 sentences: tg-bridge send --topic N "...".
3. Wait for their go: tg-bridge recv --topic N --wait 900. If the task below already contains an explicit \
go-ahead (e.g. "go"), start immediately after echoing instead of waiting.
Keep communicating through the topic (updates at milestones, questions via ask). If a send exits 3 it \
means the owner wrote again while you were working — run `tg-bridge recv --topic N` and compose what you say \
with their latest messages taken into account, rather than answering the earlier ones as if they were the \
whole picture. When idle, stay SILENT: \
arm one long background wait (recv --wait 86400, run_in_background), end your turn, and never send \
greetings/recaps/"still waiting" on timeouts — a timeout is not news. The wait MUST be a harness-tracked \
run_in_background task and NEVER a bare shell `&` (e.g. `recv ... &`) — a detached recv drains the reply \
and exits without ever waking you. Your task:
{task}"""

SPAWN_BOOTSTRAP_NO_TASK = """You were spawned from Telegram by the owner via the claude-telegram-bridge /claude \
command, without a task yet — they will give it in your Telegram topic. Set up your channel to them now \
(the tg-channel skill has the full protocol):
1. Run: tg-bridge register --name {name_q} — note the printed topic_id N.
2. Send a one-line greeting saying you're ready for instructions: tg-bridge send --topic N "...".
3. Wait for their message with ONE long background wait: tg-bridge recv --topic N --wait 86400 \
(run_in_background), then end your turn. A timeout (exit 2) is not news — re-arm it silently. The wait MUST \
be a harness-tracked run_in_background task and NEVER a bare shell `&` (e.g. `recv ... &`) — a detached recv \
drains the reply and exits without ever waking you. Never send \
greetings, recaps, or "still waiting" messages while idle; speak only when they write or assigned work \
produces something. When the task arrives, echo your understanding and follow the tg-channel discipline."""

# Codex can't arm background waits, so its idle protocol is nudge-driven: the daemon
# types a [tg-bridge] line into the pane whenever a new message lands.
CODEX_BOOTSTRAP = """You were spawned from Telegram by the owner via the claude-telegram-bridge /codex command. \
Set up your channel to them before anything else:
1. Run: tg-bridge register --name {name_q} — note the printed topic_id N.
2. Echo your understanding of the task in 1-3 sentences: tg-bridge send --topic N "...".
3. End your turn and wait for their go. When they write, a "[tg-bridge]" line is typed into this terminal — \
that is your cue to run tg-bridge recv --topic N and act on what it returns. If the task below already \
contains an explicit go-ahead (e.g. "go"), start immediately after echoing instead of waiting.
Keep communicating through the topic: updates at milestones via tg-bridge send --topic N "...", \
and if a send exits 3, the owner wrote again while you worked — recv, then compose what you say with their \
latest messages taken into account. \
questions via tg-bridge ask. When idle, end your turn and stay SILENT — never send greetings or recaps. \
READING their messages: only with a FOREGROUND tg-bridge recv --topic N when a "[tg-bridge]" nudge appears — \
NEVER run recv --wait in the background: a background wait drains their messages before you act on them, and \
the session goes silent to them. SENDING a multi-line message: write the text to a file (with real line \
breaks), then send that file on stdin — tg-bridge send --topic N - < /tmp/reply.txt. NEVER put a literal \
\\n inside a quoted argument (tg-bridge send --topic N "a\\nb"): the shell won't interpret it and Telegram \
shows a literal \\n. \
Your task:
{task}"""

CODEX_BOOTSTRAP_NO_TASK = """You were spawned from Telegram by the owner via the claude-telegram-bridge /codex \
command, without a task yet — they will give it in your Telegram topic. Set up your channel to them now:
1. Run: tg-bridge register --name {name_q} — note the printed topic_id N.
2. Send a one-line greeting saying you're ready for instructions: tg-bridge send --topic N "...".
3. End your turn. When they write, a "[tg-bridge]" line is typed into this terminal — that is your cue to \
run a FOREGROUND tg-bridge recv --topic N and act on what it returns; NEVER run recv --wait in the \
background (a background wait drains their messages before you act, and the session goes silent). While idle \
stay SILENT — never send greetings, recaps, or "still waiting" messages. SENDING a multi-line message: write \
it to a file (with real line breaks), then send that file on stdin — tg-bridge send --topic N - < \
/tmp/reply.txt. NEVER put a literal \\n in a quoted argument (the shell won't interpret it; Telegram shows a \
literal \\n). When the \
task arrives, echo your understanding, wait for their go \
(да/давай/ok also work; an inline "go" skips the wait), and report progress at milestones via tg-bridge send."""

# Pin spawned claude sessions to an explicit model. Spawning with no --model rides
# the account's implicit default — which is exactly how the Fable-5 disablement broke
# the whole fleet at once. shlex.quote protects the [1m] brackets from shell globbing.
SPAWN_MODEL = os.environ.get("TG_BRIDGE_SPAWN_MODEL", "claude-opus-4-8[1m]")

ENGINES = {
    "claude": {
        "bootstrap": SPAWN_BOOTSTRAP,
        "bootstrap_no_task": SPAWN_BOOTSTRAP_NO_TASK,
    },
    "codex": {
        "bootstrap": CODEX_BOOTSTRAP,
        "bootstrap_no_task": CODEX_BOOTSTRAP_NO_TASK,
    },
}


def spawn_flags(engine):
    """Extra launch flags for a spawned or revived session, from `spawn_flags` in config.

    **The default is nothing, and that is the point (#204 D9).** A message that reaches this
    daemon starts a program with access to the operator's files; whether that program still
    asks before editing or running anything is the second half of the threat model, and it
    must be a decision somebody made rather than a constant compiled in. On a fresh install
    the spawned session keeps its ordinary approval prompts, so a wrong `owner_id` or a bot
    in the wrong group is a nuisance and not a compromised machine.

    Opt in per engine, knowing what it means:

        "spawn_flags": {"claude": "--dangerously-skip-permissions",
                        "codex":  "--dangerously-bypass-approvals-and-sandbox"}

    That is the right setting for the use this was built for — driving sessions from a phone,
    where nobody is at the keyboard to approve anything — and it means the Telegram account
    is effectively a shell on the host. SECURITY.md says so in those words.

    Read per call rather than cached: spawns are rare, and a stale permission setting is a
    worse failure than a JSON parse. A malformed or missing value denies the flags rather
    than guessing, because the safe direction here is fewer permissions, not more.
    """
    try:
        cfg = load_config()
    # TypeError belongs here and its absence was a real defect: a config file whose whole
    # content is `true`, `7` or `null` is valid JSON, so `json.load` succeeds and `load_config`
    # then raises TypeError on `key not in cfg` — which propagated out of here and made every
    # spawn and every revive fail, from a typo in a file nothing else validates at that moment.
    # Denying the flags is the same answer as for any other malformed value (#204 review, F1).
    # RecursionError is not a ValueError: json.load raises it on a deeply nested document, and
    # it would otherwise escape here exactly as TypeError did. Same rule for the same reason —
    # a config the loader cannot handle means no flags, never an exception out of a launch
    # builder (#204 review r2, F4).
    except (OSError, ValueError, TypeError, RecursionError, SystemExit):
        return ""
    configured = cfg.get("spawn_flags") if isinstance(cfg, dict) else None
    if not isinstance(configured, dict):
        return ""
    value = configured.get(engine)
    return value if isinstance(value, str) else ""


def _command(*parts):
    """Join command fragments with exactly one space, dropping the ones that hold nothing.

    Both the emptiness check and the strip live HERE and nowhere else. Normalising in
    `spawn_flags` as well would mean two owners of one rule, and — measured — neither could
    then be mutation-tested, because reverting either left the other quietly covering for it.
    A configured `"  --flag  "` is normalised at the single point where fragments become a
    command line (#204 review, F4).
    """
    return " ".join(part for part in (p.strip() for p in parts if p) if part)


def engine_launch(engine):
    if engine == "codex":
        return _command("codex", spawn_flags("codex"))
    return _command("claude", spawn_flags("claude"), "--model", shlex.quote(SPAWN_MODEL))

CODEX_CONFIG = os.path.expanduser("~/.codex/config.toml")


def ensure_codex_trust(cwd):
    """The codex TUI blocks on a per-directory trust prompt (no flag skips it, and trust
    does not propagate from $HOME). Pre-trust the spawn cwd the same way accepting the
    prompt would: persist it in ~/.codex/config.toml."""
    header = f'[projects."{cwd}"]'
    try:
        with open(CODEX_CONFIG) as f:
            if header in f.read():
                return
    except OSError:
        pass
    with open(CODEX_CONFIG, "a") as f:
        f.write(f'\n{header}\ntrust_level = "trusted"\n')
    log(f"codex trust added for {cwd}")


def spawn_session(cfg, thread_id, text):
    m = SPAWN_RE.match(text.strip())
    if not m:
        reply(cfg, thread_id, SPAWN_USAGE)
        return
    engine = "codex" if m.group("cmd") == "codex" else "claude"  # /spawn = legacy alias for /claude
    name, path, task = m.group("name").strip(), m.group("path"), (m.group("task") or "").strip()
    tmux_name = re.sub(r"[^\w-]+", "-", name).strip("-") or "spawned"
    cwd = os.path.expanduser(path) if path else os.path.expanduser("~")
    if not os.path.isdir(cwd):
        reply(cfg, thread_id, f"Can't spawn: directory {cwd} doesn't exist.")
        return
    if _tmux(["tmux", "has-session", "-t", "=" + tmux_name], capture_output=True).returncode == 0:
        reply(cfg, thread_id, f"Can't spawn: tmux session '{tmux_name}' already exists. Pick another name.")
        return
    template = ENGINES[engine]["bootstrap" if task else "bootstrap_no_task"]
    bootstrap = template.format(name_q=shlex.quote(name), task=task)
    if engine == "codex":
        try:
            ensure_codex_trust(cwd)
        except OSError as e:
            log(f"codex trust write failed for {cwd}: {e}")  # spawn anyway; worst case it prompts
    pane, err = launch_pane(tmux_name, cwd, engine_launch(engine), bootstrap)
    if not pane:
        reply(cfg, thread_id, f"Spawn failed: {err}")
        return
    followup = "echo the task" if task else "wait for your instructions there"
    reply(cfg, thread_id, (
        f"🚀 Spawning {engine} session '{name}' (tmux: {tmux_name}, cwd: {cwd}). "
        f"It will open its own topic and {followup} shortly."
    ))
    log(f"spawned {engine} tmux session {tmux_name} (topic {thread_id} asked)")
    threading.Timer(SPAWN_VERIFY_DELAY, verify_spawn, args=(cfg, tmux_name, engine)).start()


def verify_spawn(cfg, tmux_name, engine):
    """Report to General if the spawned TUI didn't survive startup."""
    out = _tmux(
        ["tmux", "list-panes", "-t", "=" + tmux_name, "-F", "#{pane_current_command}\t#{pane_pid}"],
        capture_output=True, text=True,
    )
    if out.returncode == 0:
        for row in out.stdout.splitlines():
            command, pane_pid = (row.split("\t") + [""])[:2]
            if pane_engine(command, pane_pid) == engine:
                return
    tail = ""
    if out.returncode == 0:  # session exists but the TUI isn't running in it
        cap = _tmux(["tmux", "capture-pane", "-t", "=" + tmux_name, "-p"],
                             capture_output=True, text=True)
        tail = "\n" + "\n".join(line for line in cap.stdout.splitlines() if line.strip())[-500:]
    api(cfg["bot_token"], "sendMessage", {"chat_id": cfg["chat_id"], "text": (
        f"⚠️ Spawn '{tmux_name}' failed — the {engine} TUI isn't running "
        f"{SPAWN_VERIFY_DELAY}s after start.{tail}"
    )})
    log(f"spawn verify failed for {tmux_name}")


KILL_CONFIRM_WINDOW = 60  # seconds to confirm /kill with 'yes'
KILL_WORDS = ("yes", "да")
pending_kills = {}  # thread_id -> {pane, name, deadline}; in-memory, lost on restart by design


def check_pending_kill(cfg, thread_id, text):
    """Returns True if the message was consumed as a /kill confirmation."""
    pending = pending_kills.pop(thread_id, None)
    if not pending:
        return False
    if time.time() > pending["deadline"]:
        return False  # expired; message flows to the inbox as usual
    if text.strip().lower() not in KILL_WORDS:
        reply(cfg, thread_id, "Kill cancelled — message passed to the session as usual.")
        return False
    _tmux(["tmux", "kill-pane", "-t", pending["pane"]], capture_output=True)
    reply(cfg, thread_id, f"💀 Killed session '{pending['name']}' (pane {pending['pane']}).")
    log(f"killed pane {pending['pane']} (topic {thread_id}) on confirmation")
    return True


# Read-only status commands the daemon answers itself (from cache/registry/pane-capture)
# — they never type into the pane and never redirect the session. Because of that they
# must NOT trip the carry-forward kill-switch (#87): querying status is not interrupting
# work. Anything not listed here (plain text, /stop, /model, /kill, a new task, …) is a
# real redirect and still halts an active carry-forward.
_PASSIVE_STATUS_COMMANDS = ("/ctx", "/help", "/sessions", "/usage", "/peek")


def is_passive_status_command(text):
    """True for read-only status queries the daemon answers without touching the session
    (no pane keystrokes, no redirect), so they don't halt an active carry-forward (#87).

    Mirrors the command-dispatch gate in handle_message EXACTLY: routing to handle_command
    requires `text.startswith("/")` with no leading-whitespace tolerance, so a leading-space
    "  /ctx" is NOT a command there — and must not be classified passive here either, or it
    would bypass the kill-switch AND miss command dispatch, landing in the inbox (#88 review)."""
    t = text or ""
    if not t.startswith("/"):
        return False
    return t.split()[0] in _PASSIVE_STATUS_COMMANDS


def handle_command(cfg, thread_id, text):
    """Control messages from the owner: /help, /ctx, /stop, /peek answered here, anything else typed into the pane."""
    cmd = text.strip()
    info = read_registry().get(str(thread_id), {})
    pane = info.get("pane")
    if cmd.split()[0] == "/kill":
        if not pane or not pane_alive(pane):
            reply(cfg, thread_id, "Can't kill: no live terminal bound to this topic.")
            return
        # Arm ONLY once the prompt is in their hands. Arming first left a destructive
        # confirmation live after an undelivered prompt: on a closed-then-reopened topic a
        # "yes" inside the window would kill the pane though they never saw the question
        # (Codex round 2 of PR #162).
        if not reply(cfg, thread_id, (
            f"⚠️ Kill session '{info.get('name', '?')}' (pane {pane})? "
            f"Reply 'yes' within {KILL_CONFIRM_WINDOW}s to confirm — anything else cancels."
        )):
            log(f"kill prompt undeliverable for topic {thread_id} — not arming the confirmation")
            return
        pending_kills[thread_id] = {
            "pane": pane, "name": info.get("name", "?"),
            "deadline": time.time() + KILL_CONFIRM_WINDOW,
        }
        return
    if cmd.split()[0] == "/help":
        reply(cfg, thread_id, HELP_TEXT)
        return
    if cmd.split()[0] == "/sessions":
        reply(cfg, thread_id, sessions_overview())
        return
    if cmd.split()[0] == "/usage":
        parts = cmd.split()
        arg = parts[1].lower() if len(parts) > 1 else ""
        if len(parts) > 2 or arg not in ("", "claude", "codex"):
            reply(cfg, thread_id, "Usage: /usage [claude|codex] — bare /usage uses this topic's engine.")
            return
        # Explicit `/usage codex` works from ANY topic (account-wide, most-recent rollout);
        # bare /usage in a Codex topic scopes to that session's cwd. `/usage claude` and
        # bare /usage elsewhere report the Claude account.
        want_codex = arg == "codex" or (arg == "" and pane and engine_of_pane(pane) == "codex")
        if want_codex:
            cwd = pane_cwd(pane) if (arg == "" and pane) else None
            usage = codex_usage_line(cwd)
            reply(cfg, thread_id, usage or (
                "No Codex usage data yet — it appears once a Codex session has logged a "
                "rate-limit snapshot (start a Codex session and run some work first)."
            ))
        else:
            usage = account_usage_line()
            reply(cfg, thread_id, usage or (
                "No usage data yet — it appears once any session has rendered "
                "its statusline recently."
            ))
        return
    if cmd.split()[0] in ("/claude", "/codex", "/spawn"):
        spawn_session(cfg, thread_id, cmd)
        return
    if cmd.split()[0] == "/stop":
        interrupt_session(cfg, thread_id)
        return
    if cmd.split()[0] == "/peek":
        if pane and pane_alive(pane):
            reply(cfg, thread_id, f"Terminal ({info.get('name', '?')}):\n{peek_pane(pane)}")
        else:
            reply(cfg, thread_id, "No live terminal bound to this topic.")
        return
    if cmd.split()[0] == "/ctx":
        ctx = context_for(pane) if pane else None
        if ctx:
            cost = f", cost ${ctx['cost']:.2f}" if "cost" in ctx else ""
            reply(cfg, thread_id, f"Context window: {ctx['pct']}% used{cost} ({info.get('name', '?')})")
        else:
            reply(cfg, thread_id, "No context data — no statusline on the pane and nothing "
                                  "on disk. The session is closed, or its statusline hasn't "
                                  "rendered once yet.")
        return
    if cmd.split()[0] == "/model":
        # Intercept BEFORE the generic fallback: a bare /model would open and strand the picker.
        handle_model(cfg, thread_id, cmd, info, pane)
        return
    if is_carry_forward_command(cmd):
        # Intercept BEFORE the generic fallback: the daemon drives carry-forward → compact →
        # auto-resume itself (the model can't /compact itself).
        handle_carry_forward(cfg, thread_id, cmd, info, pane)
        return
    if not pane or not pane_alive(pane):
        reply(cfg, thread_id, f"Can't deliver {cmd}: no live terminal bound to this topic.")
        return
    try:
        with _pane_lock(pane):  # #165: don't write into another injection's verify window
            _tmux(["tmux", "send-keys", "-t", pane, "-l", cmd], check=True, capture_output=True)
            time.sleep(0.5)  # let the TUI's slash-command menu settle on the exact match
            _tmux(["tmux", "send-keys", "-t", pane, "Enter"], check=True, capture_output=True)
    except PaneLockUnavailable as e:
        log(f"command {cmd}: {e} (topic {thread_id})")
        reply(cfg, thread_id, f"Couldn't deliver {cmd} — the terminal is locked by another "
                              f"delivery that hasn't finished. Try again in a moment.")
        return
    reply(cfg, thread_id, f"→ typed {cmd} into the session terminal")
    log(f"command {cmd} -> pane {pane} (topic {thread_id})")


def load_autocf_exempt():
    """Topic ids (strings) exempted from auto carry-forward. Read fresh each poll so an operator
    can add/remove one WITHOUT a daemon restart. `state_path('autocf_exempt.json')` is a JSON list
    of topic ids that the daemon only READS (no write race with the registry). Missing/malformed
    -> empty set (feature simply inactive)."""
    try:
        with open(state_path("autocf_exempt.json")) as f:
            data = json.load(f)
        if isinstance(data, list):
            return {str(x) for x in data}
    except (OSError, ValueError):
        pass
    return set()


# Every mutation of an unfinished-run record goes through this. Read-then-unlink is not one
# filesystem operation, so without it a worker closing its own record could read its token,
# lose the race to a newer run installing a record between the read and the unlink, and delete
# that newer record — leaving a failed run with no retry, which is #239 back again (#240
# review r3, finding 1). One daemon owns this directory (Telegram permits a single consumer
# per token, and the CLI never touches it), so an in-process lock is the whole of the mutual
# exclusion needed; a second writer would need a second daemon, which cannot run.
_cf_record_lock = threading.Lock()


def _cf_unfinished_path(tid):
    """Where a carry-forward records that it has started and not yet compacted (#239).

    On disk, because the whole point is to outlive the process: a worker is a daemon thread,
    so a daemon that exits mid-run never gets to say how it ended, while `autocf.json` keeps
    the armed flag that only a post-compaction context drop can clear. One file per topic,
    so two topics cannot lose each other's record."""
    return state_path("cf-unfinished", str(tid))


def _cf_mark_unfinished(tid, token):
    """Open a record for the run `token` is about to make: `<epoch> <token>`. True if it took.

    Written when the run STARTS, not when it fails. A daemon that dies mid-run therefore
    leaves the record behind by default, which is the honest reading — nobody observed that
    run finish — and it removes the need to infer anything at startup from an armed flag
    that cannot distinguish a dead run from a completed one (#240 review r2, findings 1/3/5).
    Temp file + os.replace so a kill mid-write cannot leave a half-line that reads as
    'no record'. The caller must not start a run when this fails: a run with no record is one
    whose failure cannot re-arm the topic (#240 review r3, finding 2)."""
    path = _cf_unfinished_path(tid)
    try:
        with _cf_record_lock:
            with open(path + ".tmp", "w") as f:
                f.write(f"{time.time():.0f} {token}\n")
            os.replace(path + ".tmp", path)
        return True
    except OSError as e:
        log(f"carry-forward: could not open the unfinished-run record for topic {tid}: {e}")
        return False


def _cf_clear_unfinished(tid, token=None):
    """Close the record. With `token`, only if that run is the one that opened it.

    A halt frees the topic while the halted worker is still unwinding, so a later run can
    open its own record first, and the earlier worker must not close it — in either
    direction, since a stale success erases a retry that is genuinely owed (#240 review r2,
    finding 2). `token=None` is the unconditional close used by the warning loop, which is
    not a run and has no token to match. A record too malformed to attribute is removed
    rather than left: it can never be closed by the run that owns it, so leaving it would
    grant an unearned retry forever, and the next run rewrites it anyway."""
    path = _cf_unfinished_path(tid)
    try:
        with _cf_record_lock:
            if token is not None:
                try:
                    with open(path) as f:
                        fields = f.read().strip().split(" ", 1)
                    if len(fields) == 2 and fields[1] != token:
                        return
                except OSError:
                    return
            os.unlink(path)
    except OSError:
        pass


def _cf_unfinished_since(tid):
    """Epoch at which the topic's open carry-forward record was written, or None."""
    try:
        with _cf_record_lock, open(_cf_unfinished_path(tid)) as f:
            return float(f.read().strip().split(" ", 1)[0])
    except (OSError, ValueError, IndexError):
        return None


def _autocf_decide(pct, engine, armed, active, threshold=None, rearm=None):
    """Pure decision for auto carry-forward (#92). Returns (fire, armed_next).

    fire=True  -> trigger the carry-forward procedure now (write → issue → /compact →
                  resume). armed_next -> the armed flag to persist for this session.

    Fires ONCE when context first crosses `threshold` on a Claude session with no
    carry-forward already running, then stays armed (no repeat) until context falls
    below `rearm` — i.e. after the compaction — which re-arms it for the next climb.
    The rearm hysteresis (rearm < threshold) stops it flapping at the boundary."""
    if threshold is None:
        threshold = AUTOCF_PCT
    if rearm is None:
        rearm = AUTOCF_REARM_PCT
    if not threshold:
        return False, armed                  # feature disabled
    if pct < rearm:
        return False, False                  # dropped after a compact -> re-arm (engine-independent)
    if engine != "claude":
        return False, armed                  # Codex/unknown engine: never fire (no /compact flow)
    if pct >= threshold and not armed:
        if active:
            return False, armed              # a carry-forward already runs; retry next cycle
        return True, True                    # fire, and mark armed so it can't repeat
    return False, armed                      # armed & still high, or between rearm and threshold


def _process_autocf(cfg, thread_id, info, pane, pct, engine, exempt, autocf_fired):
    """Per-topic auto carry-forward step (extracted from warning_loop for testability).
    Returns True iff a carry-forward fired. Topics in `exempt` (#119) never fire and have any
    stale armed flag cleared, so un-exempting later re-enables auto-CF cleanly. Mutates
    `autocf_fired` in place."""
    if str(thread_id) in exempt:
        # Operator-set exemption (#119): never auto-CF this topic, regardless of %.
        autocf_fired.pop(thread_id, None)
        # The unfinished-run record goes with it. Dropping `armed` alone would let an
        # un-exemption later find a stale record and fire immediately, cooldown and all, on
        # the next tick (#240 review r1, finding 1). Whatever the exemption interrupted is
        # not owed a retry: #119 means "not here".
        _cf_clear_unfinished(thread_id)
        return False
    active = carry_forward_active(thread_id)
    if not active:
        # A carry-forward that never compacted left the context where it was, so the drop
        # below `rearm` that clears the armed flag is never coming and auto-CF is finished
        # for the life of the session (#239). Clear it here instead — once per cooldown, so
        # a session that is legitimately busy is retried occasionally rather than on every
        # warning tick. `not active` is what keeps this off a LIVE run's record: that record
        # is open by design until the run compacts, and consuming it would fire a second run
        # behind the first.
        started = _cf_unfinished_since(thread_id)
        if started is None:
            pass
        elif time.time() - started >= AUTOCF_RETRY_COOLDOWN:
            _cf_clear_unfinished(thread_id)
            autocf_fired[thread_id] = False
            log(f"auto carry-forward: re-arming topic {thread_id} — its last carry-forward "
                f"never reached compaction")
        else:
            # A run started recently and has not compacted. Hold the topic armed on the
            # strength of the RECORD rather than the flag: `autocf.json` is persisted at the
            # end of a poll, so a daemon killed between firing a run and that write comes back
            # with the flag missing, and without this the very first tick would fire again —
            # once per crash, cooldown bypassed (#240 review r3, finding 4).
            autocf_fired[thread_id] = True
    fire, armed_next = _autocf_decide(
        pct, engine, bool(autocf_fired.get(thread_id)), active)
    autocf_fired[thread_id] = armed_next
    if fire:
        log(f"auto carry-forward: context {pct}% >= {AUTOCF_PCT}% -> topic {thread_id}")
        # The start notice carries the kill-switch instructions, so a carry-forward that ran
        # without it would compact and resume a session with its owner told nothing and given
        # no way to halt it. If the topic can't receive the notice, don't start (#161).
        if not reply(cfg, int(thread_id),
                     f"🔄 Auto carry-forward at {pct}% context — writing the carry-forward, "
                     f"compacting, then resuming from its next-steps."):
            autocf_fired[thread_id] = False        # re-arm: nothing ran
            return False
        # handle_carry_forward posts a second notice with the halt instruction and aborts if
        # THAT one can't be delivered. Without its answer we would persist armed=True for a
        # run that never started, and a reopen before the next closed-topic poll would strand
        # the retry (Codex round 2 of PR #162).
        if not handle_carry_forward(cfg, int(thread_id), "/carryforward", info, pane):
            autocf_fired[thread_id] = False
            return False
    return fire


def warning_loop(cfg):
    warn_path = state_path("warnings.json")
    autocf_path = state_path("autocf.json")
    # `isinstance` as well as the exception: malformed JSON raises and is handled, but a file
    # holding VALID json of the wrong shape — `[]`, say — loads fine and then raises on every
    # `.get` for the life of the daemon, inside the loop's own exception handler, which logs
    # it and repairs nothing. The loop stays alive doing nothing, which is worse than dying
    # (#240 review r3, finding 5). Both files, because it is the same line twice.
    try:
        with open(warn_path) as f:
            warned = json.load(f)
    except (OSError, ValueError):
        warned = {}
    if not isinstance(warned, dict):
        warned = {}
    try:
        with open(autocf_path) as f:
            autocf_fired = json.load(f)  # thread_id -> bool: auto-cf armed(fired) this episode
    except (OSError, ValueError):
        autocf_fired = {}
    if not isinstance(autocf_fired, dict):
        log(f"warning_loop: {autocf_path} is not an object; starting from empty")
        autocf_fired = {}
    while True:
        time.sleep(CTX_POLL)
        try:
            registry = read_registry()
            exempt = load_autocf_exempt()  # per-poll so exemptions hot-reload without a restart
            # one warning target per pane: its most recent (highest-id) non-feed topic —
            # a session that re-registers must not double-warn through its older topics
            latest = {}
            for tid, info in registry.items():
                pane = info.get("pane")
                if pane and not info.get("ended") and not info.get("feed"):
                    if int(tid) > int(latest.get(pane, "-1")):
                        latest[pane] = tid
            for thread_id, info in registry.items():
                pane = info.get("pane")
                if not pane or info.get("ended") or info.get("feed"):
                    continue
                if info.get("closed"):
                    # Closed in Telegram: a context warning can't be delivered and an auto
                    # carry-forward must not run unannounced (#161). Drop the per-topic state
                    # too, so reopening it re-arms cleanly rather than resuming mid-ladder.
                    warned.pop(thread_id, None)
                    autocf_fired.pop(thread_id, None)
                    _cf_clear_unfinished(thread_id)  # same per-topic state (#239)
                    continue
                if latest.get(pane) != thread_id:
                    continue
                # Codex self-manages context (excellent native compaction, low context-window
                # usage), so the bridge's context machinery leaves it alone entirely: no context
                # warnings AND no auto-carry-forward. Resolve engine from the live fleet, not
                # the registry field — a freshly registered pane may not have
                # 'engine' set yet. engine_of_pane returns None for unknown, which stays on the
                # claude path (warns) as before; only a confirmed "codex" is skipped.
                # Do this BEFORE context_for(): a codex pane's context lookup can legitimately
                # return None, and the stale-state cleanup below must still run so a later Claude
                # reuse of the topic re-arms cleanly rather than staying suppressed by leftover
                # warn/auto-cf state. (Codex review, PR #97)
                engine = engine_of_pane(pane)
                if engine == "codex":
                    warned.pop(thread_id, None)
                    autocf_fired.pop(thread_id, None)
                    _cf_clear_unfinished(thread_id)  # same per-topic state (#239)
                    continue
                # `engine` is already resolved just above — hand it over so context_for
                # doesn't re-walk the whole fleet once per topic per poll (#158).
                ctx = context_for(pane, engine)
                if not ctx:
                    continue
                pct = int(float(ctx["pct"]))
                threshold = (pct // WARN_STEP) * WARN_STEP
                last = warned.get(thread_id, 0)
                if pct >= WARN_START and threshold > last:
                    # Only bank the rung if they actually got it. Advancing unconditionally
                    # lost the warning outright when the send failed and the topic was
                    # reopened before the next poll could clear the state (Codex round 2).
                    if reply(cfg, int(thread_id),
                             f"⚠️ Context window {pct}% used ({info.get('name', '?')})"):
                        warned[thread_id] = threshold
                        log(f"context warning {pct}% -> topic {thread_id}")
                elif threshold < last:  # compact/clear happened; re-arm
                    warned[thread_id] = threshold

                # Auto carry-forward (#92): at AUTOCF_PCT, drive the full carry-forward so
                # the session self-manages context. handle_carry_forward waits for idle
                # (never mid-turn), gates Claude-only, and guards against double-fire; the
                # armed flag persists so we don't re-call it every poll while it runs.
                # `engine` is resolved above (codex already skipped); a None here stays on the
                # claude gate inside _autocf_decide, which only fires for engine == "claude".
                _process_autocf(cfg, thread_id, info, pane, pct, engine, exempt, autocf_fired)
            with open(warn_path + ".tmp", "w") as f:
                json.dump(warned, f)
            os.replace(warn_path + ".tmp", warn_path)
            with open(autocf_path + ".tmp", "w") as f:
                json.dump(autocf_fired, f)
            os.replace(autocf_path + ".tmp", autocf_path)
        except Exception as e:
            log(f"warning_loop error: {e}")


# Self-heal sweep state (module-level, in-memory; lost on restart by design):
#   _dark_since[tid]       -> epoch when the topic first looked dark (sustained-dark gate)
#   _last_sweep_nudge[tid] -> epoch of the last sweep nudge we sent (cooldown gate)
_dark_since = {}
_last_sweep_nudge = {}


def has_live_recv(tid):
    """Is a `tg-bridge recv --topic <tid>` process actually running? Boundary-matched so
    topic 6 doesn't match 606. Returns None on error so callers can err toward 'not dark'."""
    try:
        out = subprocess.run(
            ["pgrep", "-af", f"tg-bridge recv --topic {tid}"],
            capture_output=True, text=True,
        )
        pat = re.compile(rf"tg-bridge recv --topic {tid}(?:\D|$)")
        for line in out.stdout.splitlines():
            if pat.search(line):
                return True
        return False
    except Exception:
        return None


def pane_is_idle(pane):
    """Idle iff the pane shows no active-turn / compaction signal — the live spinner
    timer, a compaction bar, or the legacy 'esc to interrupt' footer (see _cf_busy).
    Current Claude Code no longer renders 'esc to interrupt', so relying on it
    alone read an active turn as idle (broke this /model gate too); the spinner timer is
    the real signal. Errs toward NOT idle (returns False) on any capture error, since
    _cf_busy returns True on a bad capture."""
    return not _cf_busy(pane)


def idle_sweep_loop(cfg):
    """Periodic self-heal: nudge sessions that went dark — visible unread backlog (both
    engines), or (claude only) an empty inbox with no live recv listener while the pane is
    idle, which means a dying/detached recv drained the cursor without waking the session.
    A bug here must never crash the daemon: broad try/except per iteration and per topic."""
    while True:
        try:
            now = time.time()
            fleet = fleet_panes() or []
            engine_by_pane = {pid: eng for pid, _s, _t, eng in fleet}
            candidates = set()
            for tid, info in read_registry().items():
                try:
                    pane = info.get("pane")
                    if not pane or info.get("ended") or info.get("feed"):
                        continue
                    if not pane_alive(pane):
                        continue
                    engine = engine_by_pane.get(pane)
                    if engine not in ("claude", "codex"):
                        continue  # undeterminable engine / pane not in fleet: skip
                    unread = unread_count(tid)

                    flavor = None
                    if unread > 0:
                        flavor = "unread"  # visible backlog; applies to claude AND codex
                    elif engine == "claude":
                        # dead-listener case (claude only — codex never has a recv listener)
                        recv = has_live_recv(tid)
                        if recv is False and pane_is_idle(pane):
                            flavor = "dead-listener"
                    if not flavor:
                        continue

                    candidates.add(str(tid))
                    first = _dark_since.get(str(tid))
                    if first is None:
                        _dark_since[str(tid)] = now
                        continue  # first observation: record, take no action this round
                    if now - first < SWEEP_GRACE:
                        continue  # not yet sustained-dark
                    if now - _last_sweep_nudge.get(str(tid), 0) < SWEEP_COOLDOWN:
                        continue  # rate-limited

                    # #105: "unread" = messages sit undrained; else the recv listener died and
                    # we re-deliver a fresh dropped message. Dispatch factored into
                    # sweep_nudge_text so the call site is exercised by unit tests.
                    text = sweep_nudge_text(tid, flavor, now)
                    try:
                        status = type_line(pane, text)
                        undelivered = (f"{unread} undrained message(s)" if unread > 0
                                       else "a dropped message")
                        if status == "sent":
                            _last_sweep_nudge[str(tid)] = now
                            _unreadable_streak.pop(str(tid), None)
                            log(f"self-heal nudged pane {pane} (topic {tid}, {flavor}, unread={unread})")
                        else:
                            # Deliberately NOT stamping _last_sweep_nudge: nothing was
                            # delivered, so the next sweep must retry rather than sit out
                            # the cooldown (#133). report_blocked_pane has its own.
                            log(f"self-heal nudge {status} for topic {tid} (pane {pane})")
                            if status == "swallowed":
                                _unreadable_streak.pop(str(tid), None)
                                if pane_is_persistently_swallowing(pane):
                                    report_blocked_pane(tid, pane, undelivered)
                            elif status == "failed":
                                # "failed" means the pane could not be read or locked, which
                                # is not always transient. Retrying forever with no exit is
                                # the silence #133 exists to close — and it is quieter than
                                # the modal case, which IS reported. Escalate once per
                                # streak, past the cooldown, the way deliver_briefing does
                                # on its final attempt (#188).
                                streak = _unreadable_streak.get(str(tid), 0) + 1
                                _unreadable_streak[str(tid)] = streak
                                if streak == UNREADABLE_ESCALATE_AFTER:
                                    _blocked_reported.pop(str(tid), None)
                                    report_blocked_pane(tid, pane, undelivered,
                                                        lead=UNREADABLE_LEAD)
                    except Exception as e:
                        log(f"self-heal nudge failed for topic {tid}: {e}")
                except Exception as e:
                    log(f"idle_sweep_loop topic {tid} error: {e}")
            # topics no longer dark: clear their sustained-dark clock (cooldown expires naturally)
            for tid in list(_dark_since):
                if tid not in candidates:
                    del _dark_since[tid]
            _prune_pane_tables()
        except Exception as e:
            log(f"idle_sweep_loop error: {e}")
        time.sleep(SWEEP_POLL)


def extract_audio(msg):
    """Return (file_id, kind) for voice-like content, else (None, None)."""
    for kind in ("voice", "audio", "video_note"):
        if kind in msg:
            return msg[kind]["file_id"], kind
    return None, None


def extract_image(msg):
    """Return (file_id, ext, kind) for image content, else (None, None, None).
    Telegram sends screenshots either as a `photo` (compressed) or, if 'send as
    file' was used, as a `document` with an image mime type."""
    if msg.get("photo"):
        return msg["photo"][-1]["file_id"], "jpg", "photo"  # last entry = highest resolution
    doc = msg.get("document")
    if doc and (doc.get("mime_type") or "").startswith("image/"):
        ext = (doc["mime_type"].split("/", 1)[1] or "img").split("+")[0].split(";")[0]
        return doc["file_id"], ext, "image"
    return None, None, None


def safe_media_name(message_id, raw_name, fallback_ext="bin"):
    """A filesystem-safe basename for an attachment, prefixed with the message id.

    `document.file_name` is arbitrary text from the Telegram payload — it can carry `/`,
    `..`, a leading dot, or a null byte. Everything outside a conservative allowlist becomes
    `_`, and only the basename survives, so an attachment can never be written outside the
    topic's media directory. The message-id prefix also keeps two files of the same name
    from overwriting each other.
    """
    base = os.path.basename((raw_name or "").replace("\\", "/").strip())
    base = re.sub(r"[^A-Za-z0-9._-]", "_", base).lstrip(".")[:120]
    if not base or base in {"_", "."}:
        base = f"file.{fallback_ext}"
    return f"{message_id}-{base}"


def extract_file(msg):
    """(file_id, file_name) for a non-image document, else (None, None).

    Images are deliberately excluded: `extract_image` already handles `photo` and
    image-mime documents, and their note tells the agent to VIEW the file. Everything else
    used to reach `if not text: return` and vanish without a record (#150).
    """
    doc = msg.get("document")
    if not doc:
        return None, None
    if (doc.get("mime_type") or "").startswith("image/"):
        return None, None
    return doc.get("file_id"), doc.get("file_name")


# Bot API: an id "has at most 52 significant bits". Anything wider is not an id Telegram
# can have issued, so accepting it would only ever admit something we did not parse correctly.
_MAX_TELEGRAM_ID = 1 << 52
_ASCII_DIGITS = re.compile(r"\A[0-9]{1,19}\Z")


def bot_user_id(cfg):
    """This bridge's own bot id, read from the token's prefix (`<bot_id>:<secret>`).

    Same value `getMe` returns, without a network call on a path that runs for every update.
    Returns None for anything that is not a plausible id rather than raising: a bad token
    already fails at startup, and this must never be the thing that crashes update handling.

    `str.isdigit()` is NOT the test. It is True for Unicode decimal forms — `"٧".isdigit()`
    is True and `int("٧")` is 7 — so a token whose prefix is written in Arabic-Indic digits
    would parse to a real id. ASCII only.
    """
    token = cfg.get("bot_token")
    if not isinstance(token, str):
        return None
    head = token.split(":", 1)[0]
    if not _ASCII_DIGITS.match(head):
        return None
    value = int(head)
    return value if 0 < value < _MAX_TELEGRAM_ID else None


def _telegram_id(value):
    """`value` as a Telegram id, or None. One predicate for sender, owner, and bot.

    `bool` is an `int` subclass and `True == 1`, and `7.0 == 7` — so an identity check that
    compares whatever arrived against a configured id can be satisfied by something that is
    not an id at all. Both are rejected here rather than at each comparison, because an
    authorization boundary that is only correct at its call sites is one call site from wrong.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if 0 < value < _MAX_TELEGRAM_ID else None


def service_event_is_trusted(cfg, msg):
    """May this forum service message drive state changes and session lifecycle? (#206)

    Two senders qualify, and nothing else:

    * The owner. Same pin the message path uses — one concrete positive user id, never a
      fallback to "anyone in the group".
    * This bot. `revive_one` reopens a topic itself, and `closeForumTopic` on session end
      likewise, and Telegram echoes both back as service messages. Rejecting our own echo
      would be harmless today (both paths call `set_topic_closed` directly, so the echo is
      redundant) but it would fill the log with false alarms and hide a real one.

    Anonymous admins post with `sender_chat` and no `from`; so does a channel. Both are
    untrusted here — the Bot API gives no way to tell WHICH admin acted behind an anonymous
    post, and an authorization decision cannot be made on an unidentifiable actor.

    FAILS CLOSED on every malformed shape, and never raises. Telegram's documented types make
    most of these unreachable from the real wire, but this is the only thing standing between
    a service message and a bypass-permission agent launch — it should not depend on the
    remote being well-behaved (#208 review).
    """
    if not isinstance(msg, dict) or not isinstance(cfg, dict):
        return False
    sender = msg.get("from")
    if not isinstance(sender, dict):
        return False
    sender_id = _telegram_id(sender.get("id"))
    if sender_id is None:
        return False
    owner = _telegram_id(cfg.get("owner_id"))
    if owner is not None and sender_id == owner:
        return True
    bot = bot_user_id(cfg)
    return bot is not None and sender_id == bot


def handle_message(cfg, msg):
    chat = msg.get("chat", {})
    if chat.get("id") != cfg["chat_id"]:
        return
    # Record forum open/closed BEFORE the service-message drop (#161): these events are the
    # only source of truth the Bot API gives, and the digest uses them to list open topics only.
    if "forum_topic_closed" in msg or "forum_topic_reopened" in msg:
        if not service_event_is_trusted(cfg, msg):
            # #206. Everything below this point writes: it stamps `closed` on the registry,
            # posts to the topic, and can START A SESSION with privileged launch flags. The owner
            # pin at the bottom of this function never guarded any of it, because service
            # messages return above it — so "owner-pinned ingress" was true of message
            # content and false of the lifecycle. Group membership is not authorization.
            # `x or {}` is not the same guard as `isinstance(x, dict)`, and the difference is
            # the whole bug: a TRUTHY non-dict — `"from": "spoofed"`, `"from": [1]` — passes
            # `or {}` unchanged and then raises AttributeError on `.get`. The gate above has
            # already denied the event by then, so nothing unauthorized happens; what happens
            # is that the line explaining the denial throws instead of being written, and the
            # rejection reaches the operator as a traceback from main's catch-all rather than
            # as a log entry naming the sender (#208 round-2 review, X1).
            def _shown_id(value):
                return value.get("id") if isinstance(value, dict) else None

            log(f"ignored forum {'close' if 'forum_topic_closed' in msg else 'reopen'} of "
                f"topic {msg.get('message_thread_id')} from untrusted sender "
                f"{_shown_id(msg.get('from'))} "
                f"(sender_chat={_shown_id(msg.get('sender_chat'))})")
            return
        closed = "forum_topic_closed" in msg
        set_topic_closed(msg.get("message_thread_id"), closed)
        log(f"topic {msg.get('message_thread_id')} "
            f"{'closed' if closed else 'reopened'} in Telegram")
        if not closed:
            # #195: ask before resuming. A full resume re-reads the whole context, which on a
            # large session is the single most expensive thing the bridge can do
            # unprompted — so state the cost and let them choose carry-forward-first instead.
            # Falls back to nothing (not to a silent revive) when the question cannot be
            # delivered: acting unasked is exactly what this replaces.
            #
            # #192: reopening the topic is what #115 actually asked for; #116 shipped only
            # the on-message trigger, so reopening was inert and the session stayed dead
            # while the one-keystroke-away path (write anything) worked. State recording
            # above stays unconditional — the revive is an addition, never a substitute.
            #
            # This cannot loop even though revive_one reopens the topic itself (it calls
            # reopen_topic BEFORE clearing `ended`, so the service message it provokes comes
            # back here with `ended` still set): maybe_auto_revive holds `tid` in
            # _auto_reviving for the whole revive, and `ended` is cleared before that guard
            # is released. The manual path (revive_topics -> revive_one) never sets
            # _auto_reviving, so there it is `ended` alone that stops the echo.
            _reopened = read_registry().get(str(msg.get("message_thread_id")))
            if unrevivable_reason(_reopened):
                # #198: never fall silent here. The topic is open and the session is gone;
                # whatever the reason, saying nothing leaves them watching a topic that will
                # never answer — which is exactly what they reported.
                offer_fresh_restart(cfg, msg.get("message_thread_id"), _reopened)
            elif should_auto_revive(_reopened):
                if ((_reopened.get("engine") or "claude") == "claude"
                        and _reopen_needs_asking(_reopened)):
                    offer_reopen_choice(cfg, msg.get("message_thread_id"), _reopened)
                else:
                    # Codex has no such picker, and a claude session under Claude's own
                    # thresholds will not be offered one either — so there is nothing to
                    # relay: reopen just revives, as #192 shipped.
                    #
                    # cause="reopen", not the default: no message arrived here either. This
                    # route reaches the same revive by a different door, and telling a codex
                    # session its terminal "was found dead when a message arrived" is the same
                    # false trigger the choice path was just fixed for (#236 review r1, C3/Q2).
                    maybe_auto_revive(cfg, msg.get("message_thread_id"), cause="reopen")
    if any(key in msg for key in SERVICE_KEYS):
        return
    sender = msg.get("from", {})
    if sender.get("is_bot"):
        return
    owner = cfg.get("owner_id")
    if not valid_owner_id(owner) or sender.get("id") != owner:
        log(f"ignored message {msg.get('message_id')} from non-owner "
            f"{sender.get('id')} ({sender.get('first_name') or sender.get('username')})")
        return

    thread_id = msg.get("message_thread_id", 0)
    text = msg.get("text") or msg.get("caption") or ""
    kind = "text"

    # Kill-switch (#85): while a bridge-driven carry-forward is auto-driving this topic
    # (write → compact → auto-resume), an inbound message from the owner halts it — the one
    # message that stops a runaway continuation. It is consumed here, not delivered.
    # EXCEPTION (#87): read-only status queries (/ctx, /help, /sessions, /usage, /peek)
    # never halt — the daemon answers them from cache without touching the session, so
    # they are not a redirect. Without this, a passive /ctx during the trailing monitor
    # window fired a spurious "carry-forward halted" notice after the flow was done.
    # Round 3, finding 7: a carry-forward whose pane died leaves `_pending_cf` set until its
    # worker's `finally` runs. If the topic is reopened and asked about in that window, the
    # kill-switch would consume their answer — halting a flow that is already over — and C4's
    # re-ask and delivery would never happen.
    #
    # `delivered` ONLY, and this is the whole point (round 4). Exempting on any pending record
    # was a regression worse than the bug: `reviving` outlives the moment `revive_one` clears
    # `ended`, so the pane is live again while the record is still set, a real carry-forward
    # can start in that window, and every ordinary stop message would then skip the halt AND
    # be rejected as an answer — an unstoppable runaway, which is the one thing #85 exists to
    # prevent. `delivered` is the only state that means the session is genuinely dead and
    # waiting on them, so it is the only state where there is no continuation left to stop.
    if (thread_id and carry_forward_active(thread_id)
            and not is_passive_status_command(text)
            and not reopen_question_still_current(thread_id)):
        try:
            if halt_carry_forward(cfg, thread_id, "user message during carry-forward"):
                return
        except Exception as e:
            log(f"carry-forward halt error (topic {thread_id}): {e}")

    # Ahead of command/interrupt routing (Codex review, finding 8). Both of those branches
    # return early, so a `/status` or `!stop` typed while the reopen question was open used
    # to slip past C4 entirely: the question was neither answered nor re-asked, and it sat
    # invisible with the session still dead. For a non-answer this deliberately falls
    # THROUGH — the command still runs, they just also gets the question back.
    if text and str(thread_id) in pending_reopens:
        try:
            if check_pending_reopen(cfg, thread_id, text):
                return
        except Exception as e:
            log(f"reopen choice error (topic {thread_id}): {e}")
        if text.startswith("!"):
            # There is nothing to interrupt — the session is dead — so this is ordinary
            # content, and C4 promises it reaches the session once it revives. Routing it to
            # interrupt_session would consume it against a corpse and lose it for good
            # (round 2, finding 3). A `/command` is different: it is addressed to the daemon,
            # not to the session, so it runs now and is not queued for later replay.
            text = text[1:].strip()
    if text.startswith("/"):
        try:
            handle_command(cfg, thread_id, text)
        except Exception as e:
            log(f"command error (topic {thread_id}): {e}")
        return
    if text.startswith("!"):
        try:
            interrupt_session(cfg, thread_id, text[1:].strip())
        except Exception as e:
            log(f"interrupt error (topic {thread_id}): {e}")
        return
    if thread_id == 0:
        # No session reads General — answer immediately instead of dead-lettering
        # (and skip voice transcription: there's no reader to transcribe for).
        try:
            reply(cfg, 0, (
                "Nobody reads General — sessions only see their own topics. "
                "Write in a session's topic, or start one with /claude <name> or /codex <name>. "
                "Commands that DO work here: /help /usage /sessions /claude /codex."
            ))
        except Exception as e:
            log(f"general hint failed: {e}")
        return
    if text and thread_id in pending_kills:
        try:
            if check_pending_kill(cfg, thread_id, text):
                return
        except Exception as e:
            log(f"kill confirmation error (topic {thread_id}): {e}")

    file_id, audio_kind = extract_audio(msg)
    if file_id:
        kind = audio_kind
        tmp = state_path("tmp", f"{msg['message_id']}.ogg")
        try:
            download_file(cfg["bot_token"], file_id, tmp)
            transcript = transcribe(tmp, openai_api_key())
            log(f"transcribed {audio_kind} message {msg['message_id']} ({len(transcript)} chars)")
            if is_voice_interrupt(transcript):
                try:
                    interrupt_session(cfg, thread_id, transcript)
                    return
                except Exception as e:
                    log(f"voice interrupt error (topic {thread_id}): {e}")
            text = (text + "\n" if text else "") + transcript
        except Exception as e:
            # Redact before this reaches the inbox (which is mirrored to Telegram) or the
            # journal: a key with an embedded newline makes http.client raise
            # `Invalid header value b'Bearer sk-…'`, carrying the whole key in `e`.
            reason = redact_secrets(e)
            text = (text + "\n" if text else "") + f"[{audio_kind} message — transcription failed: {reason}]"
            log(f"transcription failed for message {msg['message_id']}: {reason}")
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)

    if not file_id:  # not audio — it may be an image (screenshot)
        img_id, img_ext, img_kind = extract_image(msg)
        if img_id:
            kind = img_kind
            path = state_path("topics", str(thread_id), "media", f"{msg['message_id']}.{img_ext}")
            try:
                download_file(cfg["bot_token"], img_id, path)
                log(f"saved {img_kind} message {msg['message_id']} -> {path}")
                note = f"[Image attached — view it before replying (Read tool / open the file): {path}]"
                text = (text + "\n" if text else "") + note
            except Exception as e:
                text = (text + "\n" if text else "") + f"[image received — download failed: {e}]"
                log(f"image download failed for message {msg['message_id']}: {e}")
        else:
            doc_id, doc_name = extract_file(msg)
            if doc_id:
                kind = "file"
                name = safe_media_name(msg["message_id"], doc_name)
                path = state_path("topics", str(thread_id), "media", name)
                try:
                    download_file(cfg["bot_token"], doc_id, path)
                    log(f"saved file message {msg['message_id']} -> {path}")
                    note = f"[File attached — read it before replying: {path}]"
                except Exception as e:
                    # Never silent: a failed download still produces a record, because the
                    # sender can see their file in Telegram and will assume it arrived.
                    note = f"[file received — download failed: {e}]"
                    log(f"file download failed for message {msg['message_id']}: {e}")
                text = (text + "\n" if text else "") + note

    if not text:
        return

    record = {
        "ts": now_iso(),
        "message_id": msg["message_id"],
        "thread_id": thread_id,
        "from": sender.get("first_name") or sender.get("username") or str(sender.get("id")),
        "kind": kind,
        "text": text,
    }
    try:
        reply_to = msg.get("reply_to_message")
        if (reply_to and reply_to.get("message_id") != thread_id
                and "forum_topic_created" not in reply_to):
            record["reply_to_message_id"] = int(reply_to["message_id"])
    except Exception as e:
        # Malformed optional reply metadata must never discard the inbound record, but
        # the correlation consumer reads a missing key as "no reply" — so log the skip.
        log(f"reply_to extraction failed for message {msg.get('message_id')}: {e}")
    wake_claim = append_jsonl(
        state_path("topics", str(thread_id), "inbox.jsonl"), record,
    )
    log(f"inbox <- topic {thread_id} {kind} message {msg['message_id']}")
    maybe_auto_revive(cfg, thread_id)
    schedule_nudge(thread_id, wake_claim)


# ===========================================================================
# Reboot auto-restore + live-session snapshotting + /model command (#74)
#
# A reboot kills every tmux pane, so every session dies and lifecycle_loop closes the
# topics. restore_on_boot() (run before any loop starts, ONLY when the kernel boot_id has
# changed) revives each live-at-reboot session: resume its exact conversation, reopen the
# topic, rebind the new pane. After a reboot the tmux server is brand new, so every
# pre-reboot pane id is dead by definition — we never trust pane_alive() on the old id.
#
# Restore needs each session's engine + session_id. We do NOT infer these at reboot time
# (unreliable — ~/ has hundreds of codex rollouts). snapshot_loop() records them into the
# registry WHILE the pane is alive, the only moment a codex pane maps to its rollout exactly.
# ===========================================================================

SNAPSHOT_POLL = int(os.environ.get("TG_BRIDGE_SNAPSHOT_POLL", "60"))
RESTORE_TMUX_PREFIX = "tgr"  # deterministic revive tmux session name = tgr-<topic>
RESTORE_SETTLE = 20          # max seconds to wait for a resumed TUI to be up+idle before briefing
COMPACT_SETTLE = 600         # same wait when we just answered the picker `compact`: Claude
                             # compacts before it goes idle, and that is minutes, not seconds
COMPACT_START_GRACE = 20     # how long to wait for that compaction to START before giving up
                             # on seeing it at all (#200: the pane is idle for ~1s first)


def session_slug(name, limit=24):
    """Clean, tmux-safe session name from a topic name: lowercase ascii, hyphen-joined,
    word-boundary-truncated to ~24 chars. Mirrors the one-time live rename so live and
    revived session names match."""
    s = re.sub(r"[^\w-]+", "-", name.lower(), flags=re.ASCII)
    s = re.sub(r"-+", "-", s).strip("-")
    if len(s) <= limit:
        return s or "session"
    out = ""
    for word in s.split("-"):
        cand = word if not out else out + "-" + word
        if len(cand) > limit:
            break
        out = cand
    return (out or s[:limit]).strip("-") or "session"


def _name_taken_by_other(sess, my_pane):
    """True if a live tmux session `sess` exists whose panes don't include my_pane."""
    if _tmux(["tmux", "has-session", "-t", "=" + sess],
                      capture_output=True).returncode != 0:
        return False
    out = _tmux(["tmux", "list-panes", "-t", "=" + sess, "-F", "#{pane_id}"],
                         capture_output=True, text=True)
    panes = [ln.strip() for ln in out.stdout.splitlines() if ln.strip()]
    return my_pane not in panes


def _revive_tmux_name(entry, tid, taken):
    """tgr-<tid> when the topic has no name; otherwise its slug, with -<tid> appended only
    on collision (another topic in this restore pass took the same slug, or a live session
    with that name isn't this topic's own pane). `taken` is a set carried across the pass."""
    name = entry.get("name")
    base = session_slug(name) if name else f"{RESTORE_TMUX_PREFIX}-{tid}"
    cand = base
    if cand in taken or _name_taken_by_other(cand, entry.get("pane")):
        cand = f"{base}-{tid}"
    taken.add(cand)
    return cand


def _session_path():
    """The hardened PATH a spawned pane needs — under systemd the client env is minimal
    (no claude/tg-bridge); linuxbrew supplies the node the codex launcher needs."""
    home = os.path.expanduser("~")
    return (f"{home}/.local/bin:{home}/bin:/usr/local/bin:/usr/bin:/bin"
            ":/home/linuxbrew/.linuxbrew/bin")


# The client attaches a `memoryPressure` handler to eligible ROOT background shell (#178)
# tasks (agent-owned ones are excluded) and kills one when that fires — but only if the
# task is still running and unnotified, the session has been human-idle past its gate
# window, the main loop is not busy, and no other active background task blocks reaping.
# A kill is reported as `killed` / "was stopped" with an EMPTY output file, which is what
# distinguishes it from a real `recv --wait` timeout: that prints "(no reply within
# timeout)" and exits 2.
#
# On Linux "pressure" is not a pressure signal at all: `Bun.ant.memoryPressureLevel()` is
# macOS-only, so the check degrades to `os.freemem() < tengu_bg_low_mem_mb` (default
# 1024 MB). When aggregate resident processes put a host near that line, session
# `recv --wait` listeners can be reaped. A reap wakes the session for a full turn that drains
# an empty inbox and re-arms, so idle sessions consume context while doing nothing. Retrying
# does not help because the freshly re-armed listener is eligible for the same reap.
#
# Two limits worth knowing before relying on this:
#   * The variable is read from the process environment at LAUNCH, so it protects only
#     panes created after it ships. revive_one REUSES a matching tmux session when one
#     exists (see its `existing` branch) and does not relaunch, so a revive onto a
#     pre-change pane leaves that engine reapable.
#   * The upstream switch is per-process, not per-task: it disables pressure reaping for
#     every eligible root background task in that session, not just the listener. On a
#     host whose free memory sits near the threshold, a memory-heavy background job in a
#     bridge-launched session now survives the event meant to reclaim it.
SPAWN_ENV = "CLAUDE_CODE_DISABLE_BG_SHELL_PRESSURE_REAP=1"


def launch_pane(tmux_name, cwd, launch, prompt=None):
    """Start a detached tmux session running `launch` (a full engine command) with the
    hardened spawn env, optionally passing `prompt` as the engine's initial-prompt arg.
    Returns (new_pane_id, "") on success or (None, error).

    Every spawn and every revive comes through here, which is why the reap opt-out below
    is set here and nowhere else."""
    shell_cmd = (f"PATH={shlex.quote(_session_path())} exec env -u CLAUDECODE "
                 f"{SPAWN_ENV} {launch}")
    if prompt:
        shell_cmd += f" {shlex.quote(prompt)}"
    run = _tmux(
        ["tmux", "new-session", "-d", "-P", "-F", "#{pane_id}", "-s", tmux_name, "-c", cwd, shell_cmd],
        capture_output=True, text=True,
    )
    if run.returncode != 0:
        return None, (run.stderr.strip() or "tmux error")
    return run.stdout.strip() or None, ""


# ---- boot id ----

def current_boot_id():
    try:
        with open("/proc/sys/kernel/random/boot_id") as f:
            return f.read().strip()
    except OSError:
        return None


def load_stored_boot_id():
    path = state_path("boot.json")
    if os.path.exists(path):
        try:
            with open(path) as f:
                return json.load(f).get("boot_id")
        except (ValueError, OSError):
            return None
    return None


def save_boot_id(bid):
    path = state_path("boot.json")
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"boot_id": bid}, f)
    os.replace(tmp, path)


# ---- session-id resolution ----

ROLLOUT_UUID_RE = re.compile(
    r"rollout-.*?-([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.jsonl$"
)


def rollout_uuid(path):
    m = ROLLOUT_UUID_RE.search(path)
    return m.group(1) if m else None


def codex_session_id_for_pane(pane):
    """EXACT codex session_id for a live codex pane, from the codex process's open fds
    (it holds its rollout-*.jsonl open) — unambiguous even when two codex panes share a
    cwd. Sub-agent rollouts, which a parent also holds open, are excluded; the cwd-newest
    rollout answers only when the process tree exposed nothing (see
    codex_ctx.rollout_for_pane)."""
    pid, cwd = _pane_locators(pane)
    try:
        path = codex_ctx.rollout_for_pane(pid, cwd)
    except Exception as e:
        log(f"codex sid lookup failed for pane {pane}: {e}")
        return None
    return rollout_uuid(path) if path else None


def context_session_id(pane):
    """session_id from a pane's statusline context file, IGNORING staleness. Used both by
    the live snapshot and by the one-time manual restore (where the file is necessarily old)."""
    if not pane:
        return None
    path = state_path("context", pane.lstrip("%") + ".json")
    try:
        with open(path) as f:
            return json.load(f).get("session_id") or None
    except (OSError, ValueError):
        return None


def session_id_for_pane(pane, engine):
    """For the LIVE snapshot: require FRESH evidence. A stale claude context file (a reused
    pane id left over from a prior session before the live one rewrites it) must NOT be
    persisted, so use the staleness-gated read_context() here — NOT context_session_id()."""
    if engine == "claude":
        return (read_context(pane) or {}).get("session_id") or None
    if engine == "codex":
        return codex_session_id_for_pane(pane)
    return None


# ---- snapshot loop: persist engine + session_id while the pane is live ----

def snapshot_once():
    """Record engine + session_id + current boot_id into each live, registered, non-ended,
    non-feed entry. Writes only on change (low registry churn). Never raises out."""
    boot = current_boot_id()
    for tid, info in list(read_registry().items()):
        try:
            pane = info.get("pane")
            if not pane or info.get("ended") or info.get("feed") or not pane_alive(pane):
                continue
            engine = engine_of_pane(pane)
            if engine not in ("claude", "codex"):
                continue
            sid = session_id_for_pane(pane, engine)
            if not sid:
                # #198: record the engine anyway. A codex session has NO session_id until it
                # completes its first turn — it does not open its rollout-*.jsonl before then,
                # and that open fd is where the id comes from (repeated samples before the
                # first turn returned None; an id appeared only after that turn completed).
                # Discarding the engine we already resolved leaves registry
                # entries reading `engine: null`, and it makes every later decision
                # about that topic guess "claude".
                #
                # ONLY for an entry that has no session id. A missing sid can also mean the
                # lookup was transient or stale, and writing the engine alone would then pair
                # a NEW engine with the OLD engine's id — review round 1 reproduced
                # `claude --resume <CODEX-SID>` from exactly that. Half of an (engine,
                # session_id) pair must never be replaced while the other half still stands.
                if not info.get("session_id") and info.get("engine") != engine:
                    def _stamp_engine(reg, _tid=str(tid), _e=engine):
                        # Re-check INSIDE update_registry's lock. `info` is a snapshot taken
                        # before the pane probes ran, and review round 2 reproduced a
                        # concurrent update adding a session id in that gap — the write then
                        # produced (claude, CODEX-SID) anyway. The gate above is an
                        # optimisation; this is the invariant.
                        cur = reg.get(_tid)
                        if cur is not None and not cur.get("session_id") and cur.get("engine") != _e:
                            cur["engine"] = _e
                    update_registry(_stamp_engine)
                    log(f"snapshot: topic {tid} engine={engine} (no session id yet)")
                continue
            if (info.get("engine") == engine and info.get("session_id") == sid
                    and info.get("boot_id") == boot):
                continue

            def _stamp(reg, _tid=str(tid), _e=engine, _s=sid, _b=boot):
                if _tid in reg:
                    reg[_tid].update({"engine": _e, "session_id": _s, "boot_id": _b})
            update_registry(_stamp)
            log(f"snapshot: topic {tid} engine={engine} sid={sid[:8]}")
        except Exception as e:
            log(f"snapshot error for topic {tid}: {e}")


def snapshot_loop(cfg):
    while True:
        try:
            snapshot_once()
        except Exception as e:
            log(f"snapshot_loop error: {e}")
        time.sleep(SNAPSHOT_POLL)


# ---- revive ----

RESTORE_BRIEFING_CLAUDE = (
    "[tg-bridge] {opening}; this is the SAME conversation, resumed. {persisted} Your Telegram "
    "topic is {tid} {topic_state}. Re-arm your listener now: run `tg-bridge recv "
    "--topic {tid} --wait 86400` as ONE harness run_in_background task (NEVER a detached `&` "
    "shell job), then end your turn. Do not send a greeting or recap — stay silent until the owner "
    "writes or your own work produces something."
)
RESTORE_BRIEFING_CODEX = (
    "[tg-bridge] {opening}; this is the SAME session, resumed. {persisted} Your Telegram "
    "topic is {tid} {topic_state}. You are nudge-driven: when the owner writes, a "
    "[tg-bridge] line appears here — run `tg-bridge recv --topic {tid}` then and act on it. "
    "Stay silent until then."
)
RESTORE_FRESH_CLAUDE = (
    "[tg-bridge] You are a FRESH session on Telegram topic {tid}: {opening_lc}, and "
    "{fresh_reason}, so you start clean. {persisted} Do NOT run `tg-bridge register`; your "
    "topic already exists as {tid} {topic_state}. Post a one-line note that you are starting "
    "without the previous context and are ready: `tg-bridge send --topic {tid} \"...\"`. "
    "Then arm ONE long `tg-bridge recv --topic {tid} --wait 86400` via run_in_background "
    "(never a detached `&`) and end your turn."
)
RESTORE_FRESH_CODEX = (
    "[tg-bridge] You are a FRESH codex session on Telegram topic {tid}: {opening_lc}, and "
    "{fresh_reason}. {persisted} Do NOT run `tg-bridge register`; topic {tid} already exists "
    "{topic_state}. Post a one-line ready note: `tg-bridge send --topic {tid} "
    "\"...\"`. You are nudge-driven: when the owner writes, a [tg-bridge] line appears here — run "
    "`tg-bridge recv --topic {tid}` and act on it."
)

# A session does not merely read the restore briefing, it REASONS from it (#167). So the
# briefing may state only what the bridge actually OBSERVED, and must say plainly what it
# did not determine. The original bug was asserting a reboot without evidence; asserting
# "no reboot" without evidence is the same defect pointed the other way, and would be worse
# on the restore_cli path, whose whole documented purpose is reviving reboot victims.
#
# Only `boot` has evidence for a reboot claim: restore_on_boot reached it by observing the
# kernel boot_id change. Every other cause knows that a pane is dead and nothing more —
# `should_auto_revive` records neither why nor when a pane died, and an operator running
# restore_cli may well be recovering a session a reboot killed.
RESTORE_CAUSES = ("boot", "recovery", "manual", "auto", "reopen")

# Rounds 1 and 2 both failed the same way: every cause-specific "helpful" detail I added
# turned out to be something the bridge cannot observe — "only this terminal changed" (false
# after a tmux kill), "background tasks from your previous terminal are gone" (false: a
# detached child reparents to init and outlives the pane), "every registered terminal was
# dead" (the check filters out feeds and ended entries), "why the previous terminal ended"
# (revive_topics never looks at the old pane). So the rule here is structural, not editorial:
# ONLY `boot` carries a cause-specific claim, because only `boot` has evidence — a changed
# kernel boot_id. Everything else gets the same blanket disclaimer. There is a test that
# enforces this by pattern, so the next tempting detail fails the suite instead of a review.
# TWO disclaimers, because one cannot serve both groups without lying in one direction.
# `recovery` and `auto` DID observe this topic's pane dead, so a blanket "did not determine
# the state of any previous terminal" contradicts their own opening sentence. `manual` and
# `unknown` never looked at the old pane at all, so for them "why it ended" presupposes an
# ending that was never established. Round 3 used one clause and hit both problems at once.
_UNDETERMINED_AFTER_DEATH = (
    "The bridge did NOT determine why it ended, whether the host rebooted, what happened to "
    "other sessions, or whether work you had running is still running — do not assume any of "
    "them, and do not report a cause you cannot check.")
_UNDETERMINED_UNOBSERVED = (
    "The bridge did NOT determine the state of any previous terminal for this topic, whether "
    "one ended, whether the host rebooted, what happened to other sessions, or whether work "
    "you had running is still running — do not assume any of them, and do not report a cause "
    "you cannot check.")
# Neither of the two above is true on a reopen, in opposite directions, which is why it gets a
# third. AFTER_DEATH attributes the observed death to the terminal just replaced, and #237 says
# it cannot: `mark_ended` stamps by topic id, so the pane that was seen dead may not be the one
# now bound. UNOBSERVED then denies the observation altogether — but a terminal for this topic
# really was seen dead, which is why `ended` was stamped at all. Round 2 of the #236 review
# caught that swap as the same error in the under-claiming direction, and it was: the comment
# calling the weaker clause "never false" was itself false.
_UNDETERMINED_AFTER_A_DEATH_NOT_NECESSARILY_THIS_PANE = (
    "A terminal for this topic was seen dead before this one was opened, but the bridge cannot "
    "guarantee it was the terminal this session was just resumed into. It did NOT determine why "
    "that one ended, whether the host rebooted, what happened to other sessions, or whether work "
    "you had running is still running — do not assume any of them, and do not report a cause "
    "you cannot check.")

# Each opening describes what happened to the TERMINAL and nothing about the session. The
# template says whether the session is the same one resumed or a fresh one, and an opening
# that also spoke for the session contradicted it: a manual FRESH launch was told both "You
# are a FRESH session" and "this session resumed into it".
_RESTORE_OPENING = {
    # boot_id changed — a genuine reboot, observed.
    "boot": "The host rebooted, so a new terminal was opened for this topic",
    # No stored boot_id and every pane SELECTED FOR RECOVERY was dead: a reboot or a killed
    # tmux server, and the bridge cannot tell which.
    "recovery": "Every terminal selected for recovery was found dead, including this topic's, "
                "and a new one was opened",
    "manual": "A terminal was opened for this topic on request",
    "auto": "This topic's terminal was found dead when a message arrived, so a new one was "
            "opened",
    # Distinct from `auto` because nothing arrived: the owner reopened the topic and answered
    # the resume question. Saying "when a message arrived" here told the session a trigger that
    # did not happen, and told the owner their own deliberate act was an incident (#235).
    "reopen": "The owner reopened this topic and asked for this session to be resumed, so a "
              "new terminal was opened",
    # Not selectable via RESTORE_CAUSES — the landing point for a caller passing a typo, so a
    # mistake degrades to the weakest claim rather than the strongest false one.
    "unknown": "A terminal was opened for this topic",
}
# What the session may take as fact. Only `boot` gets to say anything beyond the disclaimer.
_RESTORE_PERSISTED = {
    "boot": "Every session's terminal is new, and anything that did not survive the reboot "
            "is gone.",
    # These two saw the pane dead; they may say why they cannot explain it.
    "recovery": _UNDETERMINED_AFTER_DEATH,
    "auto": _UNDETERMINED_AFTER_DEATH,
    # Its own clause: a death WAS observed, but not provably this pane's (#237). See the
    # constant above for why neither neighbour fits.
    "reopen": _UNDETERMINED_AFTER_A_DEATH_NOT_NECESSARILY_THIS_PANE,
    # These two never inspected a previous pane at all.
    "manual": _UNDETERMINED_UNOBSERVED,
    "unknown": _UNDETERMINED_UNOBSERVED,
}
_RESTORE_NOTICE = {
    ("boot", False): "♻️ Restored after a reboot — resuming this conversation.",
    ("boot", True): "♻️ Reopened after a reboot — {fresh_short}, starting fresh.",
    ("recovery", False): "♻️ Terminal was found dead — relaunched and resuming this conversation.",
    ("recovery", True): "♻️ Terminal was found dead — {fresh_short}, starting fresh.",
    ("manual", False): "♻️ Restarted on request — resuming this conversation.",
    ("manual", True): "♻️ Reopened on request — {fresh_short}, starting fresh.",
    ("auto", False): "♻️ This session's terminal had died — relaunched and resumed.",
    ("auto", True): "♻️ This session's terminal had died — {fresh_short}, starting fresh.",
    # The owner did this on purpose and knows the topic was closed. Reporting it as a death
    # made them open an investigation into an incident that had not happened, in their words:
    # "Only makes an impression that something went wrong. This is why I actually asked you."
    # So it states what was DONE, and names which choice was applied — that is the part they
    # waited through a prolonged compaction for, and the picker can fail to apply it.
    ("reopen", False): "♻️ Resumed {resume_how}.",
    ("reopen", True): "♻️ Reopened — {fresh_short}, starting fresh.",
    ("unknown", False): "♻️ Restarted — resuming this conversation.",
    ("unknown", True): "♻️ Reopened — {fresh_short}, starting fresh.",
}

# How the owner's answer reads back to them. `None` covers a revive with no choice attached,
# which is every path except the reopen question.
_RESUME_HOW = {
    "compact": "from a summary",
    "full": "in full",
    None: "on request",
}

# "could not be recovered" implies an attempt. With an explicit --fresh there was none:
# `do_fresh = fresh or not sid`, and the operator asked for a clean session outright.
_FRESH_REASON = {
    True: ("a fresh session was explicitly requested, so no attempt was made to resume the "
           "previous one"),
    False: "no recoverable session id was stored for this topic",
}
_FRESH_SHORT = {True: "fresh was requested", False: "no session id stored"}

# reopen_topic can fail. Round 2 caught the briefing saying "has been reopened" regardless,
# while the Telegram notice carried the warning — so the session was told the opposite of
# what the owner was told.
_TOPIC_STATE = {
    True: "and has been reopened",
    False: ("and the bridge FAILED to reopen it — it may still be closed, so a send may not "
            "reach the owner until they reopen it"),
    # `None` = the bridge did not reopen the topic and makes no claim about its state. Used
    # by the reopen-choice path, where the owner's own Telegram event opened it and they may have
    # closed it again since; the daemon reads that flag but cannot read it atomically, so
    # asserting either state would be a claim it has not earned (round 3, finding 9).
    None: ("— the bridge did not change its open/closed state, so check before assuming a "
           "send will reach the owner"),
}


def _restore_wording(engine, cause, *, fresh=False, fresh_requested=False, reopened=True,
                     resume_choice=None):
    """Resolve a revive to a (briefing_tpl, topic_notice) pair.

    Every clause must be something the bridge OBSERVED. `fresh_requested` distinguishes "the
    operator asked for a clean session" from "there was no session id to resume", and
    `reopened` carries whether `reopen_topic` actually succeeded — the briefing must not tell
    the session its topic is open when the owner was just warned it may not be.

    The returned template still carries `{tid}` unresolved — deliver_briefing formats that,
    and it also re-enters itself on retry with the template as an argument, so everything else
    must be baked in HERE rather than threaded through that path. Substitution is by replace()
    precisely because `{tid}` has to survive it."""
    if cause not in _RESTORE_OPENING:
        # Degrade to the weakest claim, never to a reboot assertion: a typo in a caller must
        # not become the most confident false statement the bridge can make.
        log(f"revive: unknown cause {cause!r} — using neutral wording")
        cause = "unknown"
    if fresh:
        tpl = RESTORE_FRESH_CLAUDE if engine == "claude" else RESTORE_FRESH_CODEX
    else:
        tpl = RESTORE_BRIEFING_CLAUDE if engine == "claude" else RESTORE_BRIEFING_CODEX
    opening = _RESTORE_OPENING[cause]
    tpl = (tpl.replace("{opening_lc}", opening[0].lower() + opening[1:])
              .replace("{opening}", opening)
              .replace("{persisted}", _RESTORE_PERSISTED[cause])
              .replace("{fresh_reason}", _FRESH_REASON[bool(fresh_requested)])
              .replace("{topic_state}",
                       _TOPIC_STATE[None if reopened is None else bool(reopened)]))
    notice = (_RESTORE_NOTICE[(cause, bool(fresh))]
              .replace("{fresh_short}", _FRESH_SHORT[bool(fresh_requested)])
              .replace("{resume_how}", _RESUME_HOW.get(resume_choice, _RESUME_HOW[None])))
    return tpl, notice


def reopen_topic(cfg, tid):
    """Reopen a forum topic. Returns True on success (or if it was already open — Telegram
    answers reopen-of-open with TOPIC_NOT_MODIFIED). Any OTHER failure returns False so the
    caller can surface that the topic may still be closed instead of claiming success."""
    try:
        api(cfg["bot_token"], "reopenForumTopic",
            {"chat_id": cfg["chat_id"], "message_thread_id": int(tid)})
        set_topic_closed(tid, False)   # Telegram just confirmed it is open — don't wait for
        return True                    # the service message (restore_on_boot runs before
    except Exception as e:             # getUpdates, and a dropped update would strand it)
        if "NOT_MODIFIED" in str(e):
            set_topic_closed(tid, False)   # already open, equally definitive
            return True
        log(f"reopen topic {tid} failed: {e}")
        return False


def last_model_for_session(sid):
    try:
        with open(state_path("model-watchdog.json")) as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return None
        entry = data.get(sid)
        if not isinstance(entry, dict):
            return None
        return entry.get("last_model") or None
    except Exception:
        return None


# The reopen question has NO deadline (A1). /kill's 60s window exists because /kill is
# destructive and a stale confirmation could kill a pane they never meant to; this question
# destroys nothing, so a timer only creates a way to lose the choice. In measurement, a
# five-minute window expired unanswered and left a dead session behind an open topic with nothing
# to say so — a silent dead end, which is strictly worse than waiting.
REOPEN_FULL_WORDS = ("full", "fully", "as-is", "asis", "полностью")
REOPEN_COMPACT_WORDS = ("compact", "compacted", "cf", "сжать", "компакт")
REOPEN_FRESH_WORDS = ("fresh", "new", "заново", "новая", "новую")


def fresh_answer_engine(word, known):
    """The engine a fresh answer selects, or None when the text is not an answer at all.

    EXACT shapes only — `<fresh-word>` or `<fresh-word> <engine>`. Review round 2: matching a
    PREFIX meant every later token was ignored, so `fresh codex is not what I want` launched
    Codex. Launching the wrong engine rebinds the topic to it permanently, so this parser has
    to be the strict kind.

    One parser for both the live and the stale branch. They disagreed before: `fresh idea`
    was consumed and lost by one and rejected by the other, while `new codex` was an answer to
    one and ordinary text to the other."""
    parts = (word or "").split()
    if not parts or parts[0] not in REOPEN_FRESH_WORDS:
        return None
    if len(parts) == 1:
        # Bare `fresh` only works when the registry already knows what to launch. Nine of
        # nine ended id-less entries do not, so this is the normal case, not the edge one.
        return known if known in ("claude", "codex") else None
    if len(parts) == 2 and parts[1] in ("claude", "codex"):
        return parts[1]
    return None


def unrevivable_reason(entry):
    """Why a reopened topic cannot be resumed, or None when it can (or is none of our business).

    #198: reopening a topic whose session has no recorded `session_id` did nothing AND said
    nothing — the dead-session-behind-an-open-topic failure that #195 exists to remove,
    reached from a different direction. It occurs when a codex session is killed before its
    first turn, then its topic is reopened: there is no recorded session id, and silence is
    otherwise the only result."""
    if not isinstance(entry, dict) or entry.get("feed") or not entry.get("ended"):
        return None                      # live, or not a session topic: silence is correct
    if entry.get("session_id"):
        return None                      # revivable through the normal path
    return "no-session-id"


def fresh_restart_question(entry):
    """Explain why there is nothing to resume, and offer the only revive that is possible.

    A fresh session is NOT what they asked for by reopening, so it is offered rather than
    assumed — but it is cheap and unambiguous (no context to re-read), so A3's reasoning
    does not apply and there is no cost to quote.

    States only what the registry proves. Review round 1: asserting "killed before its first
    turn" is a cause the bridge has not established — an id can also be missing because the
    lookup failed, because the daemon was down, or because an older entry predates the
    snapshot. Name the pre-first-turn behaviour as the likely explanation, not as fact."""
    engine = entry.get("engine")
    why = "No session id is stored for this topic, so there is no conversation to resume."
    if engine == "codex":
        why += (" Most likely it never finished a first turn — Codex does not create a "
                "session id until it answers something.")
    choose = (f"Reply `fresh` to start a NEW {engine} session in this same topic"
              if engine in ("claude", "codex") else
              # Every ended id-less entry in the registry ALSO has no engine recorded, so this
              # is the normal case, not the edge one. Guessing here would launch the wrong TUI
              # and rebind the topic to it permanently (review round 1, finding 3).
              "I have no record of which engine it was, so I will not guess. "
              "Reply `fresh codex` or `fresh claude` to start a NEW session in this same topic")
    return (f"🔄 Reopened '{entry.get('name', '?')}', but there is nothing to resume. {why}\n\n"
            f"{choose}, or ignore this and the topic just stays as history.\n"
            f"Nothing starts until you answer.")
PENDING_REOPENS_PATH = "pending-reopens.json"


def _load_pending_reopens():
    """Questions still awaiting an answer, across daemon restarts (C5).

    Kept on disk because "waits indefinitely" is a promise the in-memory version cannot
    keep: a restart would drop the question silently and leave exactly the dead session /
    open topic pair this feature exists to prevent."""
    try:
        with open(state_path(PENDING_REOPENS_PATH)) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


# Empty at import, filled by main() AFTER the state tree is secured. Reading it here instead
# ran at import time — before main() had begun, let alone before the remediation — so the file
# a peer could still write was parsed first and its questions resent to the owner's topics.
# Populated with .update() rather than rebound, because every reference in this module mutates
# this dict in place (#245 review r2).
pending_reopens = {}
# Guards every transition of `pending_reopens` AND the write that publishes it. Round 3,
# finding 4: getUpdates and revive workers both mutate this dict now, so an unlocked
# read-modify-write could publish a file missing an entry that memory still holds.
_pending_reopen_lock = threading.RLock()


def _save_pending_reopens():
    """True iff the questions are durably on disk.

    C5 promises the question survives a restart, and that promise is only as good as this
    write. Without the fsync, os.replace makes the swap atomic but not durable: a crash
    before the kernel flushes leaves an empty or truncated file, which reads back as "no
    question pending" — precisely the dead session behind an open topic this feature exists
    to prevent. The return value is load-bearing too: the caller must be able to tell the owner
    the guarantee is reduced instead of silently asserting a C5 it cannot keep."""
    path = state_path(PENDING_REOPENS_PATH)
    # A per-write temp name, under the lock. Round 3, finding 4: revive workers now write
    # this dict too (_rearm_after_failed_revive), so two threads could open the SAME fixed
    # `.tmp` with "w", truncating one another's inode — one renames a half-written file into
    # place while the other is still writing through its now-orphaned descriptor, and
    # _load_pending_reopens turns the resulting corrupt JSON into {} on the next start. That
    # is C5 failing silently, which is the one way this feature loses a question for good.
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    try:
        # The WHOLE publish is serialized, not just the snapshot. Serializing under the lock
        # and renaming outside it still lets two threads publish out of order: A snapshots
        # {B}, B snapshots {A,B}, B renames first, A renames second, and the file on disk has
        # lost A while memory still has it.
        with _pending_reopen_lock:
            with open(tmp, "w") as f:
                json.dump(pending_reopens, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
            try:
                dirfd = os.open(os.path.dirname(path) or ".", os.O_RDONLY)
                try:
                    os.fsync(dirfd)  # the rename must survive the crash too, not just the bytes
                finally:
                    os.close(dirfd)
            except OSError:
                pass                 # some filesystems refuse directory fsync; bytes are safe
        return True
    except Exception as e:
        log(f"could not persist pending reopen questions: {e}")
        try:
            os.unlink(tmp)           # never leave a half-written .tmp to be mistaken for state
        except OSError:
            pass
        return False


def session_context_tokens(sid, cwd):
    """Tokens the session's last turn carried — i.e. what a full resume will re-read.

    Returns None when it cannot be established; the caller then says so rather than quoting
    a number it does not have. Read from the transcript tail: the last usage record is the
    live figure and these files reach tens of MB, so they are never parsed whole."""
    try:
        path = transcript.transcript_path(cwd, sid)
        total = None
        for record in transcript.read_tail_records(path):
            usage = (record.get("message") or {}).get("usage")
            if not isinstance(usage, dict):
                continue
            seen = (usage.get("input_tokens", 0)
                    + usage.get("cache_creation_input_tokens", 0)
                    + usage.get("cache_read_input_tokens", 0))
            if seen:
                total = seen          # keep the LAST, not the largest — it is the live size
        return total
    except Exception:
        return None


def session_age_minutes(sid, cwd):
    """Minutes since the session's last recorded activity, or None. Claude gates its resume
    picker on this, so we read the same quantity to predict whether it will appear."""
    try:
        path = transcript.transcript_path(cwd, sid)
        return max(0.0, (time.time() - os.path.getmtime(path)) / 60.0)
    except Exception:
        return None


def _reopen_needs_asking(entry):
    """Ask unless we can show the resume is cheap.

    Codex review, finding 1: gating purely on `resume_picker_expected` meant an unreadable
    or missing transcript — where the size is UNKNOWN — was revived silently, spending
    whatever it costs without a word. Unknown is precisely when to ask; C1 already says to
    admit the unknown rather than quote a number we do not have.

    A known-small session still reopens directly. A3's "nothing without an answer" exists to
    stop an unapproved 350k spend, not to put a prompt in front of a 20k one."""
    tokens, age = session_cost_sample(entry.get("session_id"),
                                      entry.get("cwd") or os.path.expanduser("~"))
    return _needs_asking_for(tokens, age)


def _needs_asking_for(tokens, age):
    """The gate itself, over an already-taken sample.

    Round 3, finding 8: reading the size and then calling resume_picker_expected read the
    transcript AGAIN, so the two halves could disagree. A stat that failed on the second pass
    made the prediction say "no picker" for a session the first pass had already measured at
    200k — a known-large resume with no question at all. One sample, one decision.

    An unknown AGE is not permission either. It only means we cannot predict Claude's own
    picker; the size is what A3 is about, and a large session is asked about regardless."""
    if tokens is None:
        return True                    # unknown cost: ask
    if age is None:
        return tokens > RESUME_MODAL_TOKENS
    return tokens > RESUME_MODAL_TOKENS and age > RESUME_MODAL_AGE_MINUTES


def session_cost_sample(sid, cwd):
    """(tokens, age_minutes) for a session, taken together so they cannot disagree."""
    return session_context_tokens(sid, cwd), session_age_minutes(sid, cwd)


def resume_picker_expected(sid, cwd):
    """Will Claude Code offer its own "Resume from summary / full session" picker?

    It appears only when the session is BOTH older than RESUME_MODAL_AGE_MINUTES and larger
    than RESUME_MODAL_TOKENS. Asking the owner to choose when no choice will be offered would
    leave the bridge waiting to answer a picker that never renders — so we predict it with
    Claude's own thresholds rather than always asking."""
    tokens = session_context_tokens(sid, cwd)
    age = session_age_minutes(sid, cwd)
    if tokens is None or age is None:
        return False
    return tokens > RESUME_MODAL_TOKENS and age > RESUME_MODAL_AGE_MINUTES


def _safe_peek(pane, lines=40):
    try:
        return peek_pane(pane, lines=lines) or ""
    except Exception:
        return ""


def _resume_picker_present(screen):
    """True only for the LIVE resume picker, not for prose that happens to quote it.

    Requires all three of its rows. Matching the single phrase "Resume from summary" would
    fire on ordinary output — including this daemon's own messages about the feature — and
    then send `1`/`2`+Enter into whatever UI is actually live."""
    if not screen:
        return False
    return all(marker in screen for marker in RESUME_MODAL_ROWS)


def answer_resume_picker(pane, choice, deadline=None):
    """Select `choice` ("compact" | "full") on Claude's resume picker once it renders.

    THIS is what the bridge was missing: a headless revive left that picker unanswered, so a
    large old session came back sitting on a modal, and the briefing typed into it was
    swallowed in repeated observations. Nothing else answers it — there is no operator
    at the terminal.

    Verified before acting, never blind: the picker's own text must be on screen. Pressing a
    key on an unverified pane is the #133 hazard, and here the wrong key is "Don't ask me
    again", which would silently disable the choice for every future resume."""
    key = RESUME_MODAL_CHOICE.get(choice)
    if not key:
        return "failed"
    deadline = deadline or (time.time() + RESUME_MODAL_WAIT)
    while time.time() < deadline:
        if _resume_picker_present(_safe_peek(pane)):
            # Re-verify INSIDE the lock. Checking before taking it is a TOCTOU: the picker
            # can be dismissed or replaced between the look and the keystroke, and then the
            # key lands in whatever is live now. This daemon already learned that in
            # _cf_modal_present/_cf_clear_modal; A2 is too expensive to relearn — the wrong
            # key here is "Don't ask me again", which disables Claude's picker machine-wide
            # and permanently (Codex review, finding 6).
            with _pane_lock(pane):
                if not _resume_picker_present(_safe_peek(pane)):
                    continue
                sent = _tmux(["tmux", "send-keys", "-t", pane, key], capture_output=True)
                if getattr(sent, "returncode", 1) != 0:
                    log(f"resume picker: choice key failed on pane {pane} — Enter withheld")
                    return "failed"      # never Enter on an unmoved cursor
                entered = _tmux(["tmux", "send-keys", "-t", pane, "Enter"], capture_output=True)
                if getattr(entered, "returncode", 1) != 0:
                    log(f"resume picker: Enter failed on pane {pane}")
                    return "failed"
            log(f"resume picker answered '{choice}' on pane {pane}")
            return "answered"
        time.sleep(2)
    log(f"resume picker never appeared on pane {pane} — nothing to answer")
    return "absent"


def reopen_question(entry, tokens):
    """The question, worded once so the first ask and every re-ask are identical.

    It must describe what the code ACTUALLY does — relay Claude Code's own picker, whose
    cheap option is "Resume from summary". An earlier build still promised a carry-forward
    here long after that implementation was removed, and the owner caught it: the message is the
    only part of this feature they can see, so a stale description misinforms the exact
    decision the feature exists to support (C2)."""
    if tokens:
        cost = f"re-read about {tokens:,} tokens ({tokens / 10_000:.0f}% of a 1M window)"
    else:
        cost = "re-read its whole history (size unknown — no usage record found)"
    return (
        f"🔄 Reopening '{entry.get('name', '?')}'. Resuming the full session will {cost}.\n\n"
        f"Reply `compact` to resume from a summary — Claude's own recommended option — or "
        f"`full` to resume the whole session as-is.\n"
        f"Nothing starts until you answer; the question waits and anything else re-asks it."
    )


def offer_reopen_choice(cfg, thread_id, entry):
    """Ask whether to resume the session as-is or carry it forward first, and state what a
    full resume costs. Returns True iff the question was delivered AND the answer armed.

    Arm only after delivery, exactly as /kill does: a live choice sitting behind a question
    they never received would act on an answer they did not know they were giving (PR #162 r2)."""
    tid = str(thread_id)
    if tid in pending_reopens or tid in _auto_reviving:
        return False                  # already asked, or a revive is already running
    tokens = session_context_tokens(
        entry.get("session_id"), entry.get("cwd") or os.path.expanduser("~"))
    # Persist BEFORE delivering, but as `prepared` — a record that is not yet an armed
    # choice. Round 2, finding 1: writing after the send leaves a window where the question
    # is in front of them and nothing is on disk, so a restart loses it (C5). Writing the
    # ARMED record first would resurrect the #162 r2 bug instead — a live choice behind a
    # question they never received. Two states settle both: `prepared` is only a note to ask,
    # and `delivered` is the sole state that accepts an answer.
    with _pending_reopen_lock:
        pending_reopens[tid] = {"entry": dict(entry), "tokens": tokens, "state": "prepared"}
    durable = _save_pending_reopens()
    try:
        delivered = reply(cfg, thread_id, reopen_question(entry, tokens))
    except Exception as e:
        # Round 3, finding 6: an exception here (PossiblyDelivered above all) left the record
        # stuck at `prepared` — unanswerable by design, and only re-asked at process start, so
        # the topic was wedged until the daemon restarted. Ambiguous delivery resolves toward
        # `delivered`: they may well be looking at the question, and an answer must work. If they
        # never saw it, nothing happens until they reopen the topic, which asks again.
        log(f"reopen question delivery for topic {tid} was ambiguous ({e}) — arming anyway")
        delivered = True
    if not delivered:
        with _pending_reopen_lock:
            pending_reopens.pop(tid, None)
        _save_pending_reopens()
        log(f"reopen question undeliverable for topic {tid} — not arming the choice")
        return False
    with _pending_reopen_lock:
        if tid not in pending_reopens:
            return False              # cleared underneath us while the send was in flight
        pending_reopens[tid]["state"] = "delivered"
    if not _save_pending_reopens() or not durable:
        # Stay armed anyway. The question is already in front of them, so disarming would turn
        # a rare durability failure into a certain dead end — their answer would do nothing at
        # all. What must not happen is claiming C5 while it does not hold, so say so.
        reply(cfg, thread_id,
              "(I could not write this question to disk. It still works — but if the bridge "
              "restarts before you answer, close and reopen the topic to get it back.)")
        log(f"reopen choice for topic {tid} armed in memory only — state write failed")
    log(f"reopen choice offered for topic {tid} ({tokens or 'unknown'} tokens)")
    return True


def resend_undelivered_reopen_questions(cfg):
    """Re-ask any question that was persisted but may never have reached them.

    A `prepared` record means the daemon died between writing the question and delivering it.
    That record blocks the auto-revive (A3) but cannot be answered, so left alone it is a
    dead end with no way out. Asking twice costs nothing; never asking strands the session."""
    for tid, pending in list(pending_reopens.items()):
        if pending.get("state") == "delivered":
            continue
        entry = read_registry().get(tid)
        # #198: validate against the question that was actually asked. A `fresh` question is
        # about an entry that should_auto_revive rejects BY DEFINITION, so validating every
        # record the resume way would silently drop exactly the ones this branch exists for.
        fresh = pending.get("kind") == "fresh"
        alive = (unrevivable_reason(entry) is not None) if fresh else should_auto_revive(entry)
        if not alive:
            pending_reopens.pop(tid, None)   # no longer the dead session we meant to ask about
            continue
        stored = pending.get("entry") or entry
        text = (fresh_restart_question(stored) if fresh
                else reopen_question(stored, pending.get("tokens")))
        if reply(cfg, int(tid), text):
            pending_reopens[tid]["state"] = "delivered"
            log(f"re-delivered the {'fresh-restart' if fresh else 'reopen'} question for "
                f"topic {tid} after a restart")
        else:
            pending_reopens.pop(tid, None)
            log(f"reopen question for topic {tid} is undeliverable — dropped")
    _save_pending_reopens()


def _rearm_after_failed_revive(cfg, tid, entry):
    """Make the question answerable again after a revive that did not survive its own worker.

    Round 2, finding 5: the answer was consumed the moment the thread STARTED, so a failure
    inside it left them with no question, no session, and a reply that said it was resuming.
    Round 3 sharpened it: bailing out because a key merely EXISTS is what made this a no-op
    against a `reviving` record. What matters is the state, not the presence."""
    tid = str(tid)
    with _pending_reopen_lock:
        pending = pending_reopens.get(tid)
        if pending and pending.get("state") == "delivered":
            return                       # already answerable — nothing to restore
        tokens = (pending or {}).get("tokens")
        if pending is None:
            tokens = session_context_tokens(entry.get("session_id"),
                                            entry.get("cwd") or os.path.expanduser("~"))
        restored = {"entry": dict(entry), "tokens": tokens, "state": "delivered"}
        # Carry the KIND across (review round 1, finding 1). Dropping it silently turned a
        # failed fresh restart into a resume record: their next `fresh` then entered the resume
        # branch, failed should_auto_revive — which rejects these entries by definition — and
        # was discarded as "the topic changed". Neither answerable by the word it advertised
        # nor revivable by the path it had switched to.
        kind = (pending or {}).get("kind")
        if kind:
            restored["kind"] = kind
        pending_reopens[tid] = restored
    _save_pending_reopens()


def offer_fresh_restart(cfg, thread_id, entry):
    """Ask whether to start a fresh session in a topic whose own session cannot be resumed.

    Deliberately reuses the pending-reopen record so it inherits everything that machinery
    was hardened for over four review rounds — persist-before-deliver, a `delivered` state
    that is the only answerable one, restart survival, and the re-ask on a non-answer. It is
    marked `kind: "fresh"` so it can never be mistaken for a resume choice."""
    tid = str(thread_id)
    if tid in pending_reopens or tid in _auto_reviving:
        return False
    with _pending_reopen_lock:
        pending_reopens[tid] = {"entry": dict(entry), "tokens": None,
                                "state": "prepared", "kind": "fresh"}
    durable = _save_pending_reopens()
    try:
        delivered = reply(cfg, thread_id, fresh_restart_question(entry))
    except Exception as e:
        log(f"fresh-restart question for topic {tid} was ambiguous ({e}) — arming anyway")
        delivered = True
    if not delivered:
        with _pending_reopen_lock:
            pending_reopens.pop(tid, None)
        _save_pending_reopens()
        log(f"fresh-restart question undeliverable for topic {tid}")
        return False
    with _pending_reopen_lock:
        if tid not in pending_reopens:
            return False
        pending_reopens[tid]["state"] = "delivered"
    if not _save_pending_reopens() or not durable:
        reply(cfg, thread_id,
              "(I could not write this question to disk. It still works — but if the bridge "
              "restarts before you answer, close and reopen the topic to get it back.)")
    log(f"fresh-restart offered for topic {tid} ({unrevivable_reason(entry)})")
    return True


def _check_fresh_restart(cfg, thread_id, pending, word):
    """Answer branch for a `kind: "fresh"` question. Separate from the resume path on purpose:
    that path validates with should_auto_revive, which is False for these entries by
    definition, so sharing it would drop every one of these as "the topic changed"."""
    tid = str(thread_id)
    # Validate BEFORE either branch (review round 1, finding 2). Checking only on the answer
    # left a stale `delivered` record alive indefinitely: every non-answer re-asked a question
    # about a topic that had since been rebound, and because `handle_message` exempts a
    # `delivered` record from the carry-forward kill-switch, a live carry-forward on that
    # topic became unhaltable — reproduced with halt_calls=0 and the stop text merely queued.
    current = read_registry().get(tid)
    stored = pending.get("entry") or {}
    if not fresh_record_matches(stored, current):
        _forget_pending_reopen(tid)
        reply(cfg, thread_id,
              "That topic changed since I asked — it is no longer the dead session I offered "
              "to restart, so I have done nothing.")
        log(f"fresh-restart for topic {tid} dropped: registry no longer matches")
        # The SAME parser decides this as decides the live branch (review round 2). They
        # disagreed before, so `fresh idea` was consumed and lost here while being rejected
        # there, and `new codex` was an answer there but ordinary text here.
        return fresh_answer_engine(word, stored.get("engine")) is not None
    engine = fresh_answer_engine(word, current.get("engine"))
    if engine is None:
        # Not an answer, or an answer that does not say which engine when nothing recorded it.
        # Re-ask and leave it armed; return False so the message still reaches the session's
        # inbox once one exists (C4).
        reply(cfg, thread_id, fresh_restart_question(current))
        log(f"fresh-restart re-asked for topic {tid} (no usable engine in {word!r})")
        return False
    current = dict(current, engine=engine)
    with _pending_reopen_lock:
        pending_reopens[tid]["state"] = "reviving"
    _save_pending_reopens()
    if not _start_fresh_session(cfg, thread_id, current):
        _rearm_after_failed_revive(cfg, tid, current)
        reply(cfg, thread_id,
              "I could not start it just now — the question is still open, answer again in "
              "a moment.")
        return True
    reply(cfg, thread_id, "Starting a fresh session in this topic.")
    return True


def _start_fresh_session(cfg, thread_id, entry):
    """Launch a fresh session bound to an existing topic. Mirrors _revive_with_choice's
    worker discipline: the WORKER owns the pending record, so a failure inside it gives the
    question back instead of leaving a dead topic and a message that said it was starting."""
    tid = str(thread_id)
    with _auto_revive_lock:
        if tid in _auto_reviving:
            return False
        _auto_reviving.add(tid)

    def _run():
        try:
            status, _task = revive_one(cfg, tid, entry, brief=True, cause="manual",
                                       fresh=True, fresh_requested=True, respect_close=True)
            log(f"fresh restart topic {tid}: {status}")
            if status == "failed":
                _rearm_after_failed_revive(cfg, tid, entry)
                reply(cfg, int(tid),
                      "⚠️ The fresh session failed to start. The question is open again: "
                      "reply `fresh` to retry.")
            else:
                _forget_pending_reopen(tid)
        except Exception as e:
            log(f"fresh restart failed for topic {tid}: {e}")
            _rearm_after_failed_revive(cfg, tid, entry)
            reply(cfg, int(tid),
                  "⚠️ The fresh session crashed on startup. The question is open again: "
                  "reply `fresh` to retry.")
        finally:
            with _auto_revive_lock:
                _auto_reviving.discard(tid)

    try:
        threading.Thread(target=_run, daemon=True).start()
        return True
    except Exception as e:
        with _auto_revive_lock:
            _auto_reviving.discard(tid)
        log(f"fresh restart thread failed for topic {tid}: {e}")
        return False


def _revive_with_choice(cfg, thread_id, entry, choice):
    """Revive, then answer Claude's own resume picker with `choice`.

    The bridge does not implement compaction here — Claude Code already offers "Resume from
    summary (recommended)" vs "Resume full session as-is" and states the age and token count
    itself. An earlier draft resumed and then drove a carry-forward (three reads of the
    context to save it once), and a second substituted `--autocompact`; both reimplemented a
    native flow badly. All the bridge has to do is relay the answer, because there is nobody
    at the terminal to give it.

    Held in `_auto_reviving` throughout: revive_one reopens the topic before clearing
    `ended`, and that service message would otherwise come back and offer the choice again."""
    tid = str(thread_id)
    with _auto_revive_lock:
        if tid in _auto_reviving:
            return False
        _auto_reviving.add(tid)

    def _run():
        try:
            status, _task = revive_one(cfg, tid, entry, brief=True, cause="reopen",
                                       resume_choice=choice, respect_close=True)
            log(f"reopen ({choice}) topic {tid}: {status}")
            if status == "failed":
                # Do not let a failed revive be a log line only: that is the silent dead end.
                _rearm_after_failed_revive(cfg, tid, entry)
                reply(cfg, int(tid),
                      "⚠️ The revive failed — the session is still down. The question is "
                      "open again: reply `compact` or `full` to retry.")
            else:
                # The worker owns this transition (round 3, finding 5). Only a revive that
                # actually came up may consume the question.
                _forget_pending_reopen(tid)
        except Exception as e:
            log(f"reopen ({choice}) failed for topic {tid}: {e}")
            # Same dead end by a different door: the answer was consumed when this thread
            # started, so without re-arming they are left with a "Resuming…" that never happened.
            _rearm_after_failed_revive(cfg, tid, entry)
            reply(cfg, int(tid),
                  "⚠️ The revive crashed — the session is still down. The question is open "
                  "again: reply `compact` or `full` to retry.")
        finally:
            with _auto_revive_lock:
                _auto_reviving.discard(tid)

    try:
        threading.Thread(target=_run, daemon=True).start()
        return True
    except Exception as e:
        with _auto_revive_lock:
            _auto_reviving.discard(tid)
        log(f"reopen thread failed for topic {tid}: {e}")
        return False


def _pending_reopen_state(thread_id):
    """The state of a topic's pending reopen question, or None. Read under the lock: workers
    transition these records, so an unsynchronized read can see a half-written dict."""
    with _pending_reopen_lock:
        return (pending_reopens.get(str(thread_id)) or {}).get("state")


def fresh_record_matches(stored, current):
    """True iff a stored fresh question still describes the topic in the registry.

    `unrevivable_reason` alone is too weak for identity: a DIFFERENT ended, id-less session
    in the same topic satisfies it just as well, so a replaced binding read as unchanged.
    `ended` is the discriminator the resume path gets from `session_id` — a new session that
    started and ended in this topic carries a different timestamp."""
    if unrevivable_reason(current) is None:
        return False
    return current.get("ended") == (stored or {}).get("ended")


def reopen_question_still_current(thread_id):
    """True iff a delivered question still describes a topic that cannot be reached anyway.

    This is what may disarm the carry-forward kill-switch — the one message that stops a
    runaway continuation — so it must be more than "a record exists". Review round 2: the
    exemption keyed on `state == "delivered"` alone, and validation did not happen until
    deep inside the answer path, so a STALE record left a live carry-forward unhaltable."""
    try:
        with _pending_reopen_lock:
            pending = pending_reopens.get(str(thread_id))
            if not pending or pending.get("state") != "delivered":
                return False
            kind, stored = pending.get("kind"), dict(pending.get("entry") or {})
        current = read_registry().get(str(thread_id))
        if kind == "fresh":
            return fresh_record_matches(stored, current)
        return (should_auto_revive(current)
                and current.get("session_id") == stored.get("session_id"))
    except Exception as e:
        # Fail toward HALTING (review round 3). This is evaluated BEFORE the kill-switch's own
        # try/except, so a raising registry read aborted handle_message outright — and main()
        # still advances and saves the Telegram offset, so the one message that stops a runaway
        # continuation was gone for good. Refusing the exemption merely halts a flow that had
        # already finished, which is recoverable; losing the halt is not.
        log(f"reopen-question validation failed for topic {thread_id}: {e}")
        return False


def _forget_pending_reopen(tid):
    with _pending_reopen_lock:
        pending_reopens.pop(str(tid), None)
    _save_pending_reopens()


def check_pending_reopen(cfg, thread_id, text):
    """True iff this message was consumed as an answer to the reopen question.

    Returning False here does NOT mean "ignore" — it means the message goes on to the inbox
    as usual, which is exactly what C4 wants for a non-answer: the question is asked again
    AND whatever they typed is still delivered to the session once it revives. What suppresses
    the premature revive is the pending-question guard in maybe_auto_revive, not swallowing
    their text."""
    tid = str(thread_id)
    pending = pending_reopens.get(tid)
    if not pending or pending.get("state") != "delivered":
        # A `prepared` record is a question they may never have seen, so it cannot be answered
        # — resend_undelivered_reopen_questions revives it at startup instead (finding 1).
        return False
    # rstrip, NOT strip. Round 3, finding 3: `.strip(".!?")` takes characters off BOTH ends,
    # so the previous "exact spellings only" fix did not hold at all — `!fully` still came
    # out as `fully` and authorised the expensive full resume, which is both a C4 miss and an
    # A3 breach. `!fully` is session-bound content and must be queued, not obeyed.
    word = (text or "").strip().lower().rstrip(".!?")
    if pending.get("kind") == "fresh":
        # #198: a different question with a different answer set, and its entry is
        # deliberately one that should_auto_revive rejects. Branch before any of the resume
        # path's validation touches it.
        return _check_fresh_restart(cfg, thread_id, pending, word)
    if word in ("/compact", "/full"):
        # Muscle memory: while the session is dead, `/compact` cannot mean Claude's own
        # command, because there is nothing running to compact. Matched EXACTLY rather than
        # by stripping a sigil character set — round 2, finding 7: `lstrip("/!")` also
        # swallowed `!fully` (a real interrupt) into an expensive full resume, and turned
        # `/cf` into `compact` even though `/cf` is the real carry-forward command.
        word = word[1:]
    choice = ("full" if word in REOPEN_FULL_WORDS
              else "compact" if word in REOPEN_COMPACT_WORDS else None)
    # Finding 5: the stored entry is a SNAPSHOT. Between question and answer the topic may
    # have been revived by another path, re-registered under a new session id, or unbound
    # entirely. Round 2, finding 8 extended this to the re-ask: a stale snapshot was being
    # quoted back at them for a session that no longer existed. Validate before either branch.
    current = read_registry().get(tid)
    if (not should_auto_revive(current)
            or current.get("session_id") != pending["entry"].get("session_id")):
        _forget_pending_reopen(tid)
        reply(cfg, thread_id,
              "That topic changed since I asked — it is no longer the dead session I "
              "offered to reopen, so I have done nothing. Reopen it again if you still "
              "want it back.")
        log(f"reopen question for topic {tid} dropped: registry no longer matches")
        # An answer is consumed here; anything else still belongs to the session.
        return bool(choice)
    if choice:
        # Finding 3: never consume the question before the revive exists. Round 3, finding 5:
        # "after Thread.start() returns" is not late enough either — a worker that ran and
        # FAILED before that return found the key still present, read it as "already armed",
        # did nothing, and then this handler popped it. Dead session, no question, and a
        # "Resuming…" they could not act on. So the record moves to `reviving` first, which is
        # neither answerable nor lost, and the WORKER owns every transition out of it.
        with _pending_reopen_lock:
            pending_reopens[tid]["state"] = "reviving"
        _save_pending_reopens()
        if not _revive_with_choice(cfg, thread_id, current, choice):
            _rearm_after_failed_revive(cfg, tid, current)
            reply(cfg, thread_id,
                  "I could not start the revive just now — the question is still open, "
                  "answer again in a moment.")
            log(f"reopen revive did not start for topic {tid}; question left armed")
            return True
        reply(cfg, thread_id, "Resuming the full session as-is." if choice == "full"
              else "Resuming from a summary.")
        return True
    # Not an answer: ask again and leave the question armed. There is no deadline, so this
    # can repeat indefinitely without ever stranding the session (A1).
    reply(cfg, thread_id, reopen_question(pending["entry"], pending.get("tokens")))
    log(f"reopen question re-asked for topic {tid} (answer was not full/compact)")
    return False


def last_effort_for_session(sid, cwd):
    """The reasoning effort a claude session was last running at, from its own transcript.

    `last_model_for_session` cannot answer this: it reads `model-watchdog.json`, and for
    claude sessions that file stores the bare model (`"claude-fable-5"`). Only codex entries
    embed the effort (`"gpt-5.6-sol medium"`), because `codex_label` joins the pair.

    The transcript records `effort` on every assistant record for models that take one, so
    the LAST such record is the live setting. Read from the tail — transcripts reach tens of
    MB and the answer is always at the end.

    Returns None when it cannot be established, and the caller then omits `--effort` rather
    than inventing a level: a model that takes no effort (Opus 4.8 records none) would reject
    a fabricated flag and the whole revive would fail to launch (#190)."""
    return last_model_and_effort_for_session(sid, cwd)[1]


def last_model_and_effort_for_session(sid, cwd):
    """The (model, effort) a claude session was last running at, read as ONE pair.

    They must come from the same record. Round 2, finding 4: taking the model from
    `model-watchdog.json` and the effort from the transcript reads two sources of different
    ages — the watchdog ticks every five minutes, so a session that switched model and died
    inside that window resumes on the model it no longer ran, carrying an effort measured
    against the model it did. Sampling one record makes that mismatch impossible.

    Returns (None, None) when the transcript cannot establish a model; the caller then falls
    back to the watchdog for the model alone and takes no effort with it."""
    try:
        path = transcript.transcript_path(cwd, sid)
        for record in reversed(transcript.read_tail_records(path)):
            # Stop at the NEWEST record that names a model, and take that record's effort —
            # do not keep scanning for any effort at all. A session that ran Fable at
            # `medium` and later switched to Opus 4.8 (which records none) would otherwise
            # hand Opus `--effort medium`, a flag it rejects: the revive dies, and because
            # the answer has already been consumed that lands in the dead-session state this
            # whole feature exists to remove (Codex review, finding 7).
            model = (record.get("message") or {}).get("model")
            if not model:
                continue
            effort = record.get("effort")
            return model, (effort if isinstance(effort, str) and effort else None)
        return None, None
    except Exception:
        return None, None


# Claude Code's own resume picker — "Resume from summary (recommended)" / "Resume full
# session as-is" / "Don't ask me again" — appears when a resumed session is BOTH older and
# larger than these. Mirrored from its defaults (CLAUDE_CODE_RESUME_THRESHOLD_MINUTES /
# CLAUDE_CODE_RESUME_TOKEN_THRESHOLD) and read from the same env vars, so we ask about a
# choice exactly when one will actually be offered.
RESUME_MODAL_AGE_MINUTES = int(os.environ.get("CLAUDE_CODE_RESUME_THRESHOLD_MINUTES", "70"))
RESUME_MODAL_TOKENS = int(os.environ.get("CLAUDE_CODE_RESUME_TOKEN_THRESHOLD", "100000"))
RESUME_MODAL_ROWS = ("Resume from summary", "Resume full session as-is",
                     "Don't ask me again")
RESUME_MODAL_CHOICE = {"compact": "1", "full": "2"}
RESUME_MODAL_WAIT = 90        # s to wait for the picker to render after launch


def _resume_launch(engine, sid, model=None, effort=None):
    if engine == "codex":
        # The bypass flag is a ROOT flag, not a `resume` subcommand option — it must precede
        # the subcommand (verified: clap accepts this form; `... resume <sid> --bypass` fails).
        # Codex carries its own model+effort in the rollout and takes neither as a flag.
        return _command("codex", spawn_flags("codex"), "resume", shlex.quote(sid))
    launch = _command("claude", "--resume", shlex.quote(sid),
                      "--model", shlex.quote(model or SPAWN_MODEL), spawn_flags("claude"))
    if effort:
        # Omitted when unknown: `--effort` on a model that does not take one is a launch
        # failure, which would turn a recoverable "wrong effort" into a dead revive.
        launch += f" --effort {shlex.quote(effort)}"
    return launch


def _briefing_still_ours(pane, tid, engine, phase):
    """True iff this briefing may still be typed into `pane` for `tid`.

    Trusting a captured pane would let one chain type topic A's briefing into a pane since
    rebound to topic B, and then mark topic A briefed at its new pane — which never received
    anything (#133 review r2). It also collapses duplicate chains from repeated revives:
    whichever lands first marks the topic and the rest abandon."""
    entry = read_registry().get(str(tid)) or {}
    if entry.get("pane") != pane:
        log(f"revive: topic {tid} no longer bound to pane {pane} — abandoning briefing ({phase})")
        return False
    # CODEX ONLY. This is the crash-retry guard, and `revive_one` already scopes it that way:
    # "re-brief an existing codex pane only if it wasn't already briefed this boot (crash-retry
    # — codex has no recv to detect)". Applying it to claude as well overrode that decision and
    # silently dropped the briefing on any host whose boot id had not changed since the last
    # one — prolonged uptime was enough, so every revive after the first lost its cause, and
    # the session's only account of the restart became Claude Code's stock "ran out of context",
    # which is exactly the false premise #167 exists to stop (#234). Claude keeps a real guard
    # below: has_live_recv, which detects the double-arm this was standing in for.
    if engine == "codex" and entry.get("briefed_boot") == current_boot_id():
        log(f"revive: topic {tid} already briefed this boot — abandoning briefing ({phase})")
        return False
    if engine == "claude" and has_live_recv(str(tid)):
        log(f"revive: topic {tid} already has a live recv — skip briefing ({phase})")
        return False
    return True


def _compaction_settled(pane, tid, window):
    """True when it is safe to type after the picker was answered `compact`.

    #200: "idle right now" is not "ready". The pane stays idle for about a second between the
    answer and compaction rendering, so the plain idle wait exited immediately and typed into
    a pane about to go busy. Measurement found the status line appeared 1.8 seconds after the
    picker answered, so the old one-second sleep typed the briefing into the startup gap and
    left it unsubmitted while the session came back dark.

    Two details this file already learned the hard way and the first version of this fix
    ignored:

    * `_cf_compacting`, not `pane_is_idle`. The latter is `not _cf_busy()`, which reports busy
      for ANY live turn and — deliberately — for every capture error, so one transient capture
      failure satisfied a generic busy gate and let the pre-compaction gap straight through.
      `_cf_compacting` is compaction-specific and defaults to False on a bad capture, which is
      the correct bias here (#101 made the same distinction).
    * A sustained-idle streak, not one sample. Carry-forward already requires
      CF_IDLE_SAMPLES consecutive not-busy captures because a status repaint reads as idle for
      a single capture; `idle, idle, compacting, idle, compacting` would otherwise deliver on
      the fourth sample, mid-compaction.

    Returns False only when compaction was seen and did not finish (or the pane died) — the
    caller must then retry, never inject."""
    started, deadline = False, time.time() + COMPACT_START_GRACE
    while time.time() < deadline:
        if not pane_alive(pane):
            log(f"revive: pane {pane} for topic {tid} died before briefing")
            return False
        if _cf_compacting(pane):
            started = True
            break
        time.sleep(0.5)
    if not started:
        # Compaction may have been declined, or finished before we first looked. Fall through
        # to the ordinary idle wait rather than stalling — the grace must not become a new
        # way to leave a session dark.
        log(f"revive: topic {tid} never started compacting after `compact` — briefing as usual")
        return True
    idle_streak, deadline = 0, time.time() + window
    while time.time() < deadline:
        if not pane_alive(pane):
            log(f"revive: pane {pane} for topic {tid} died during compaction")
            return False
        if _cf_compacting(pane) or _cf_busy(pane):
            idle_streak = 0
        else:
            idle_streak += 1
            if idle_streak >= CF_IDLE_SAMPLES:
                return True
        time.sleep(1)
    return False


def deliver_briefing(pane, tid, engine, briefing_tpl, attempt=1, settle=None, await_busy=False):
    """Type the briefing into a resumed pane once it is up and idle. Crash-retry safety:
    never create a second concurrent recv — if a claude recv already listens, skip. If the
    pane never becomes idle within the window, DO NOT send (a mid-turn inject could corrupt
    input); leave it for idle_sweep_loop to nudge as a dark session.

    A retry (attempt > 1) fires minutes after the pane was captured, so it re-reads the
    registry first and abandons unless the topic is STILL bound to this pane and STILL
    unbriefed for this boot. Trusting the captured pane would let a retry type topic A's
    briefing into a pane that has since been rebound to topic B, and then mark topic A
    briefed at its new pane — which never received anything (#133 review r2). The same check
    collapses duplicate chains from repeated revives: whichever one lands first marks the
    topic, and the rest abandon."""
    if attempt > 1 and not _briefing_still_ours(pane, tid, engine, "retry"):
        return
    if attempt == 1 and engine == "claude" and has_live_recv(str(tid)):
        log(f"revive: topic {tid} already has a live recv — skip briefing")
        return
    window = RESTORE_SETTLE if settle is None else settle
    if await_busy and engine == "claude" and not _compaction_settled(pane, tid, window):
        # Compaction was observed and did not finish. Do NOT fall through to the ordinary
        # idle wait: that returns without typing and schedules nothing (review finding 3),
        # which is precisely what left topic 4107 dark — its retry waited RESTORE_SETTLE,
        # gave up, and ended the chain while compaction was still running.
        if attempt < BRIEFING_MAX_ATTEMPTS and pane_alive(pane):
            threading.Timer(BRIEFING_RETRY_DELAY, deliver_briefing,
                            args=(pane, tid, engine, briefing_tpl, attempt + 1),
                            kwargs={"settle": settle, "await_busy": True}).start()
            log(f"revive: topic {tid} still compacting — retrying the briefing in "
                f"{BRIEFING_RETRY_DELAY}s")
        else:
            log(f"revive: topic {tid} still compacting after {attempt} attempts — "
                f"leaving for idle_sweep")
        return
    deadline, reached, waited = time.time() + window, False, False
    while time.time() < deadline:
        if not pane_alive(pane):
            log(f"revive: pane {pane} for topic {tid} died before briefing")
            return
        if engine != "claude" or pane_is_idle(pane):
            reached = True
            break
        waited = True          # any real wait can outlive the ownership we checked on entry
        time.sleep(1)
    if engine == "claude" and not reached:
        log(f"revive: topic {tid} not idle within {window}s — leaving for idle_sweep")
        return
    # Revalidate IMMEDIATELY before typing, but ONLY where a long wait really happened.
    # Review round 2: the checks above run before a wait that can now last COMPACT_SETTLE, and
    # in that window the topic can be rebound, another chain can brief it, or a live recv can
    # start. Reproduced — attempt 2 validated 4107 -> %15, the binding moved to %999
    # mid-wait, and it typed into %15 then stamped briefed_boot on %999, which had received
    # nothing: the #133 r2 failure reached through a longer wait.
    #
    # NOT on a plain first attempt. That runs inline immediately after revive_one bound the
    # pane, so re-reading there races the very binding write it was handed — which is why the
    # original check was gated on attempt > 1 at all.
    if ((attempt > 1 or await_busy or waited)
            and not _briefing_still_ours(pane, tid, engine, "delivery")):
        return
    text = briefing_tpl.format(tid=tid)
    try:
        status = type_line(pane, text, settle=0.4)
        if status != "sent":
            # Leave briefed_boot unset AND schedule the retry here. idle_sweep_loop cannot
            # stand in for this: for codex it only makes a candidate when unread > 0, and it
            # sends a generic re-arm nudge, not the briefing. A pane revived into a modal
            # with an empty inbox would otherwise stay unbriefed until the next inbound
            # message happened to arrive (#133 review).
            log(f"revive: briefing {status} for topic {tid} (pane {pane}, attempt {attempt})")
            if attempt < BRIEFING_MAX_ATTEMPTS:
                if status == "swallowed":
                    report_blocked_pane(tid, pane, "its session-revival briefing")
                # Retries do NOT inherit `settle`: they fire BRIEFING_RETRY_DELAY later, by
                # which time any compaction is long finished, and a 10-minute wait would pin
                # a Timer thread for no reason.
                threading.Timer(BRIEFING_RETRY_DELAY, deliver_briefing,
                                args=(pane, tid, engine, briefing_tpl, attempt + 1)).start()
            else:
                # Last attempt: the chain ends here, before type_line's own cap can release,
                # and for a codex pane with an empty inbox idle_sweep_loop is not a fallback
                # either (it only acts on unread). Force the escalation past its cooldown so
                # giving up is never silent — this is the one report that must get through.
                log(f"revive: giving up on briefing topic {tid} after {attempt} attempts")
                _blocked_reported.pop(str(tid), None)
                report_blocked_pane(tid, pane,
                                    "its session-revival briefing (final attempt — this "
                                    "session will stay unbriefed until you answer the prompt)")
            return
        boot = current_boot_id()

        def _mark(reg, _tid=str(tid), _b=boot, _p=pane):  # idempotency marker for crash-retry
            # Stamp only if the topic is STILL on the pane we actually typed into. Ownership
            # can change while type_line waits for its pane lock and types, and marking the
            # rebound pane briefed would tell the bridge a pane was briefed when it received
            # nothing — the #133 r2 failure at the other end of the function.
            if _tid in reg and reg[_tid].get("pane") == _p:
                reg[_tid]["briefed_boot"] = _b
        update_registry(_mark)
        log(f"revive: briefed topic {tid} (engine {engine})")
    except Exception as e:
        log(f"revive: briefing send failed for topic {tid}: {e}")


def revive_one(cfg, tid, entry, fresh=False, brief=True, taken=None, cause="boot",
               fresh_requested=None, resume_choice=None, respect_close=False):
    """Revive a single session for topic `tid`. Idempotent: if the deterministic revive
    tmux session already exists (crash-retry), verify its engine and rebind instead of
    duplicating. With brief=False, launch+rebind+notice run synchronously and the (slow,
    idle-waiting) briefing is left to the caller via the returned task — so a mass restore
    doesn't block the daemon's poll loop. `cause` is one of RESTORE_CAUSES and decides what
    the session is told about WHY it restarted and what survived; it must match reality,
    because the session reasons from it (#167). Returns (status, task) where status is
    'resumed'|'fresh'|'failed'."""
    tid = str(tid)
    if taken is None:
        taken = set()
    engine = entry.get("engine") or "claude"
    cwd = entry.get("cwd") or os.path.expanduser("~")
    if not os.path.isdir(cwd):
        cwd = os.path.expanduser("~")
    sid = entry.get("session_id")
    tmux_name = _revive_tmux_name(entry, tid, taken)
    do_fresh = fresh or not sid

    existing = _tmux(["tmux", "has-session", "-t", "=" + tmux_name],
                              capture_output=True).returncode == 0
    if existing:
        out = _tmux(["tmux", "list-panes", "-t", "=" + tmux_name, "-F", "#{pane_id}"],
                             capture_output=True, text=True)
        lines = [ln for ln in out.stdout.splitlines() if ln.strip()]
        pane = lines[0] if lines else None
        if not pane:
            return "failed", {"error": "existing revive session has no pane"}
        # Confirm the existing session runs the engine we expect before binding a topic to it.
        # engine_of_pane can transiently return None while the TUI is still coming up, so poll.
        actual = None
        for _ in range(5):
            actual = engine_of_pane(pane)
            if actual:
                break
            time.sleep(1)
        if actual != engine:  # wrong engine, or still undeterminable — do NOT bind blindly
            return "failed", {"error": f"{tmux_name} engine={actual!r}, expected {engine}"}
        newly_spawned = False
    else:
        if engine == "codex":
            try:
                ensure_codex_trust(cwd)
            except OSError as e:
                log(f"revive: codex trust write failed for {cwd}: {e}")
        if do_fresh:
            launch = engine_launch(engine)
        else:
            # #190: the model was already carried deliberately; effort was not, so a revived
            # session silently came back at the machine default. That is not cosmetic on a
            # rate-limited model, and correcting it afterwards costs a full context re-read.
            model, effort = (last_model_and_effort_for_session(sid, cwd)
                             if engine == "claude" else (None, None))
            if engine == "claude" and not model:
                # No transcript evidence. Fall back to the watchdog for the model and take NO
                # effort with it: pairing an effort from one source with a model from another
                # is exactly the mismatch this stopped reading two sources to avoid.
                model, effort = last_model_for_session(sid), None
            launch = _resume_launch(engine, sid, model, effort)
        pane, err = launch_pane(tmux_name, cwd, launch)
        if not pane:
            log(f"revive: launch failed for topic {tid}: {err}")
            return "failed", {"error": err}
        newly_spawned = True

    picker_outcome = None
    if newly_spawned and engine == "claude":
        # Answer Claude's own resume picker before anything else touches the pane. Until it
        # is answered the pane IS a modal: a briefing typed into it is swallowed and the
        # session comes back dark. Nothing else can answer it — no operator is at this
        # terminal.
        #
        # Run this even with no choice (Codex review, finding 2): the prediction can be
        # wrong, and an unanswered picker is the dark-session bug regardless of why we did
        # not expect one. With no choice we only DETECT it; we never guess on their behalf.
        try:
            picker_outcome = (answer_resume_picker(pane, resume_choice)
                              if resume_choice else
                              ("present" if _resume_picker_present(_safe_peek(pane)) else "absent"))
        except Exception as e:
            picker_outcome = "failed"
            log(f"resume picker handling failed for topic {tid}: {e}")
        if resume_choice and picker_outcome != "answered":
            # Fail LOUD. Silently proceeding hands them the full session they did not choose —
            # exactly the spend this feature exists to prevent.
            reply(cfg, tid, (
                f"⚠️ You chose `{resume_choice}`, but Claude's resume picker "
                f"{'never appeared' if picker_outcome == 'absent' else 'could not be answered'}. "
                f"The session is resuming with its FULL context. Nothing was lost — but the "
                f"choice was not applied."))
            log(f"resume picker '{resume_choice}' not applied for topic {tid}: {picker_outcome}")
        elif picker_outcome == "present":
            reply(cfg, tid, (
                "⚠️ Claude is showing its resume picker and I have no answer from you, so I "
                "have not pressed anything. The session cannot receive work until it is "
                "answered — reply `compact` or `full` and I will answer it."))
            log(f"unanswered resume picker on topic {tid} — reported, not guessed")

    # Findings 9 and (round 2) 6: a revive is not instantaneous — answering the picker alone
    # can hold it for RESUME_MODAL_WAIT — and they can close the topic again inside that
    # window. Reopening it would undo that close, which is the one thing a close must never
    # suffer. Re-reading `closed` first was still a TOCTOU: the close can land between the
    # read and the call. So on this path reopen_topic is not called AT ALL — the Telegram
    # reopen event is what started the revive, so the topic is already open, and there is
    # nothing to reopen and no window in which to reverse them. Boot restore still reopens:
    # there a closed topic is what a reboot left behind, not a decision they made.
    if respect_close:
        # Round 3, finding 9: this sample is not atomic and must not be dressed up as one. It
        # is safe for the ACTION (there is no reopen call left to reverse their close), but the
        # briefing must not tell the session "your topic has been reopened" on the strength of
        # a read that may already be stale. `reopened=None` means exactly that: unclaimed.
        closed_again = bool((read_registry().get(tid) or {}).get("closed"))
        reopened = None
    else:
        closed_again = False
        reopened = reopen_topic(cfg, tid)

    boot = current_boot_id()

    def _bind(reg):
        e = reg.get(tid)
        if e is None:
            return
        e["pane"] = pane
        e["boot_id"] = boot
        e["engine"] = engine
        if do_fresh:
            e.pop("session_id", None)  # clear a stale id so a re-reboot can't resume the old convo
        elif sid:
            e["session_id"] = sid  # persist now, don't wait for the next snapshot
        e.pop("ended", None)
    update_registry(_bind)

    # `fresh` and `not sid` are different reasons and only one of them means recovery was
    # attempted; `reopened` must reach the session too, not only the owner's notice (#167 r2).
    # `fresh_requested` defaults to this call's own `fresh`, which is right for every direct
    # caller; revive_topics passes it explicitly because it derives `fresh` internally.
    asked_for_fresh = bool(fresh) if fresh_requested is None else bool(fresh_requested)
    # The APPLIED choice, never the requested one. Keying the notice on the request meant that
    # when the picker did not appear, the bridge posted "resuming with its FULL context — the
    # choice was not applied" and then, immediately below it, "Resumed from a summary." Two
    # contradictory messages, the second false, in the very feature built because a misleading
    # notice sent the owner looking for an incident (#236 review r1, C1). `None` degrades to
    # "on request", which is all that is certain on that path — the warning above already
    # carries the detail, and adding a second claim about the context would be inventing one.
    applied_choice = resume_choice if picker_outcome == "answered" else None
    tpl, notice = _restore_wording(engine, cause, fresh=do_fresh,
                                   fresh_requested=asked_for_fresh, reopened=reopened,
                                   resume_choice=applied_choice)
    if reopened is False:
        # `is False` means reopen_topic was CALLED and failed. None means it was never called
        # (the reopen-choice path), which is not a failure to report: on that path the topic
        # is open because the owner opened it, or shut because they shut it again. Calling either
        # "topic reopen failed" would blame the bridge for obeying them.
        notice += " (⚠️ topic reopen failed — flag the owner if it stays closed.)"
    # Brief when freshly spawned; re-brief an existing claude pane (deliver_briefing self-guards
    # on has_live_recv so it won't double-arm); re-brief an existing codex pane only if it wasn't
    # already briefed this boot (crash-retry — codex has no recv to detect).
    already_briefed = entry.get("briefed_boot") == boot
    needs_brief = newly_spawned or engine == "claude" or (engine == "codex" and not already_briefed)
    task = {"pane": pane, "tid": tid, "engine": engine, "tpl": tpl,
            "needs_brief": needs_brief, "reopened": reopened}

    if closed_again:
        log(f"revive: topic {tid} was closed again during the revive — session is up, "
            f"topic left closed")
    else:
        try:
            reply(cfg, int(tid), notice)  # fast; always inline so the owner sees it immediately
        except Exception as e:
            log(f"revive: topic notice failed for {tid}: {e}")
    if brief and needs_brief:
        # A `compact` answer drops Claude straight into compaction, which runs for minutes.
        # RESTORE_SETTLE is sized for a plain resume and cannot cover it, so the briefing was
        # skipped and the session sat dark. Measurement found compaction took 110 seconds
        # against a 20-second window; the listener then required a manual nudge to arm.
        compacting = resume_choice == "compact" and picker_outcome == "answered"
        deliver_briefing(pane, tid, engine, tpl, await_busy=compacting,
                         settle=COMPACT_SETTLE if compacting else None)
    status = "resumed" if not do_fresh else "fresh"
    # Only a reopen that was attempted AND failed is a reopen failure. On the reopen-choice
    # path the session came up fine and the bridge never touched the topic's state.
    return ("reopen_failed" if reopened is False else status), task


def _restore_targets():
    return [(tid, info) for tid, info in read_registry().items()
            if info.get("pane") and not info.get("ended") and not info.get("feed")]


def _all_target_panes_dead(targets):
    """For the migration case where boot.json is absent, distinguish a normal first
    deploy with live panes from a post-reboot/post-kill state that needs restore."""
    return bool(targets) and all(not pane_alive(info.get("pane")) for _tid, info in targets)


def _restore_targets_now(cfg, boot, targets, cause="boot"):
    """`cause` must be "boot" only when a boot_id change actually proved a reboot. The
    unbaselined branch reaches here on "dead panes and no baseline", which is a reboot OR a
    killed tmux server — it passes "recovery" so nobody is told a reboot happened (#167)."""
    counts = {"resumed": [], "fresh": [], "reopen_failed": [], "failed": []}
    brief_tasks = []
    taken = set()
    for tid, info in targets:
        name = info.get("name", "?")
        try:
            status, task = revive_one(cfg, tid, info, brief=False, taken=taken,
                                      cause=cause)  # defer slow briefing
        except Exception as e:
            status, task = "failed", None
            log(f"restore: revive failed for topic {tid}: {e}")
        counts.setdefault(status, []).append(name)
        # reopen_failed still spawned a live pane, so it still needs briefing; only "failed" doesn't.
        if status != "failed" and task and task.get("needs_brief"):
            brief_tasks.append(task)
    save_boot_id(boot)
    # Brief in the background so the daemon can start polling immediately (each briefing can
    # wait up to RESTORE_SETTLE for the pane to go idle — synchronous would block getUpdates
    # for RESTORE_SETTLE * N sessions).
    for task in brief_tasks:
        threading.Thread(
            target=deliver_briefing,
            args=(task["pane"], task["tid"], task["engine"], task["tpl"]),
            daemon=True,
        ).start()
    headline = ("Reboot restore" if cause == "boot"
                else "Recovery restore (panes were dead; reboot NOT confirmed)")
    summary = (f"♻️ {headline}: {len(counts['resumed'])} resumed, "
               f"{len(counts['fresh'])} fresh, {len(counts['reopen_failed'])} topic-reopen-failed, "
               f"{len(counts['failed'])} failed.")
    detail = []
    for k, label in (("resumed", "resumed"), ("fresh", "fresh"),
                     ("reopen_failed", "REOPEN-FAILED (pane up, topic still closed)"),
                     ("failed", "FAILED")):
        if counts[k]:
            detail.append(f"{label}: " + ", ".join(counts[k]))
    try:
        api(cfg["bot_token"], "sendMessage", {"chat_id": cfg["chat_id"],
            "text": "⚙️ " + summary + ("\n" + "\n".join(detail) if detail else "")})
    except Exception as e:
        log(f"restore: summary post failed: {e}")
    log("restore: " + summary)


def restore_on_boot(cfg):
    """Run ONCE at daemon start, before any loop. Revives live-at-reboot sessions only when
    the kernel boot_id changed (a genuine reboot, not a mere daemon restart)."""
    boot = current_boot_id()
    stored = load_stored_boot_id()
    if boot is None:
        log("restore: no boot_id available — skipping")
        return
    if stored is None:
        targets = _restore_targets()
        if _all_target_panes_dead(targets):
            # Dead panes with no baseline is a reboot OR a killed tmux server. We revive
            # either way, but we do not tell the sessions which one it was (#167).
            log("restore: no prior boot_id, but registered panes are dead — unbaselined "
                "recovery (reboot not confirmed)")
            _restore_targets_now(cfg, boot, targets, cause="recovery")
            return
        save_boot_id(boot)
        log("restore: first run (no prior boot_id) — nothing to restore")
        return
    if stored == boot:
        log("restore: boot_id unchanged — daemon restart, not a reboot; no restore")
        return
    log(f"restore: boot_id changed ({stored[:8]}->{boot[:8]}) — reviving live sessions")
    # The one branch with actual evidence of a reboot: the kernel boot_id changed.
    _restore_targets_now(cfg, boot, _restore_targets(), cause="boot")


def revive_topics(cfg, specs, cause="manual"):
    """Manual one-time restore, independent of the boot gate and of `ended`. Each spec is a
    dict {tid, engine?, session_id?, cwd?, fresh?}. A claude topic missing engine/sid is
    resolved from its (old) statusline context file; still-missing => fresh reopen. `cwd`
    overrides the registry cwd (resume is cwd-scoped, so a session launched from a directory
    other than the one it registered in must be resumed from where its transcript lives).
    `cause` defaults to "manual", which says an operator opened a terminal and states
    explicitly that the bridge did NOT determine why the previous one ended. It must not
    claim the host did not reboot: this CLI's documented purpose is reviving already-`ended`
    reboot victims, so "no reboot" would be false exactly where it is used most (#167). An
    operator who knows the real cause can pass it."""
    reg = read_registry()
    results = {}
    for spec in specs:
        tid = str(spec["tid"])
        info = dict(reg.get(tid, {}))
        if spec.get("cwd"):
            info["cwd"] = spec["cwd"]
        # The operator ASKING for a fresh session and the bridge falling back to one because
        # no session id could be resolved are different facts, and the briefing states which
        # happened. Round 3 overwrote the same local for both and so told every derived-fresh
        # revive that fresh had been "explicitly requested".
        requested_fresh = bool(spec.get("fresh"))
        fresh = requested_fresh
        if not fresh:
            engine = spec.get("engine") or info.get("engine")
            sid = spec.get("session_id") or info.get("session_id")
            if not sid and info.get("pane"):
                sid = context_session_id(info["pane"])
                engine = engine or "claude"
            if sid:
                info["engine"], info["session_id"] = engine or "claude", sid
            else:
                fresh = True
        try:
            status, _ = revive_one(cfg, tid, info, fresh=fresh, cause=cause,
                                   fresh_requested=requested_fresh)
        except Exception as e:
            status = "failed"
            log(f"manual revive topic {tid} failed: {e}")
        results[tid] = status
        log(f"manual revive topic {tid}: {status}")
    return results


# ---- /model command ----

MODEL_ALIASES = ("opus", "sonnet", "haiku", "opus[1m]", "sonnet[1m]")
MODEL_ID_RE = re.compile(r"^claude-[a-z0-9.\-]+(\[1m\])?$")
# Codex live model-switch is verified at build time; flip to True once confirmed reliable.
CODEX_MODEL_SUPPORTED = os.environ.get("TG_BRIDGE_CODEX_MODEL", "0") == "1"
MODEL_RETRY_DELAY = 8       # seconds between idle-gate retries
MODEL_MAX_ATTEMPTS = 4
_pending_model = {}         # tid -> alias currently in flight (newest request supersedes)
_model_lock = threading.Lock()  # serializes the check/send/pop across handler + Timer threads


def valid_model(arg):
    a = arg.strip()
    return a in MODEL_ALIASES or bool(MODEL_ID_RE.match(a))


def handle_model(cfg, thread_id, text, info, pane):
    parts = text.split(maxsplit=1)
    valid = ", ".join(MODEL_ALIASES) + " (or a full claude-… id)"
    if len(parts) == 1:
        reply(cfg, thread_id, f"Usage: /model <name>. Valid: {valid}. Fable is disabled.")
        return
    alias = parts[1].strip()
    if not valid_model(alias):
        reply(cfg, thread_id, f"Unknown model '{alias}'. Valid: {valid}.")
        return
    if not pane or not pane_alive(pane):
        reply(cfg, thread_id, "Can't switch model: no live terminal bound to this topic.")
        return
    engine = engine_of_pane(pane)
    if engine == "codex" and not CODEX_MODEL_SUPPORTED:
        reply(cfg, thread_id, "Codex model-switching isn't supported via the bridge yet.")
        return
    with _model_lock:
        _pending_model[str(thread_id)] = alias  # newest request supersedes any older pending one
    _try_send_model(cfg, thread_id, alias, pane, engine, 1)


def _try_send_model(cfg, thread_id, alias, pane, engine, attempt):
    """Deliver `/model <alias>` when the pane is idle, retrying on a bounded timer. The
    whole decide-and-send is under _model_lock so a newer request can't slip an alias in
    between an older Timer's check and send (which would deliver a stale model)."""
    tid = str(thread_id)
    msg = retry = None
    with _model_lock:
        if _pending_model.get(tid) != alias:
            return  # superseded by a newer /model request
        info = read_registry().get(tid, {})
        if info.get("pane") != pane or not pane_alive(pane) or engine_of_pane(pane) != engine:
            _pending_model.pop(tid, None)
            msg = "Model switch cancelled — the session's terminal changed."
        elif engine == "claude" and not pane_is_idle(pane):
            if attempt >= MODEL_MAX_ATTEMPTS:
                _pending_model.pop(tid, None)
                msg = "Session stayed mid-turn — didn't switch. Try /model again when idle."
            else:
                retry = attempt + 1
                if attempt == 1:
                    msg = "Session is mid-turn — I'll switch the model once it settles."
        else:
            # ready: send under the lock so the pop can't race a newer request, then clear.
            # Also under the PANE lock (#165) — this writes keystrokes and presses Enter, so
            # running it inside another injection's capture→type→capture window both corrupts
            # that window and risks its Enter. Order is always _model_lock → _pane_lock;
            # nothing takes them the other way round.
            try:
                with _pane_lock(pane):
                    _tmux(["tmux", "send-keys", "-t", pane, "-l", f"/model {alias}"],
                                   check=True, capture_output=True)
                    time.sleep(0.5)  # let the slash-command menu settle on the exact match
                    _tmux(["tmux", "send-keys", "-t", pane, "Enter"], check=True, capture_output=True)
                    # Newer Claude Code shows a "Switch model?" confirmation dialog after
                    # /model; confirm it (default ❯ = option 1, Yes). Only send the extra Enter
                    # when the dialog is actually present, so on versions without it we don't
                    # emit a stray empty line. Without this, /model hangs the session on the
                    # modal (dark-session).
                    time.sleep(1.0)
                    cap = _tmux(["tmux", "capture-pane", "-p", "-t", pane, "-S", "-12"],
                                         capture_output=True, text=True)
                    if cap.returncode == 0 and ("Switch model" in cap.stdout or "Yes, switch" in cap.stdout):
                        _tmux(["tmux", "send-keys", "-t", pane, "Enter"], check=True, capture_output=True)
                msg = f"→ switched model to {alias}."
                log(f"/model {alias} -> pane {pane} (topic {thread_id})")
            except Exception as e:
                msg = f"Model switch failed: {e}"
            _pending_model.pop(tid, None)
    if msg:  # reply outside the lock — it's a network call
        reply(cfg, thread_id, msg)
    if retry:
        threading.Timer(MODEL_RETRY_DELAY, _try_send_model,
                        args=(cfg, thread_id, alias, pane, engine, retry)).start()


# ---- /carry forward command (#85) ----
#
# The model cannot trigger /compact on itself, so the daemon acts as its "hands":
# on `/carry forward` (or `/cf`) it drives, end-to-end, in one background worker
# per topic:
#   1. WRITE   — inject a NON-interactive prompt that makes the session write a
#                dense carry-forward (state + concrete next-steps) to a daemon-
#                chosen absolute file and record it to a GitHub issue, then as its
#                LAST action `touch` a daemon-chosen done-MARKER. The daemon waits
#                for that marker (not a pure idle gate) so a mid-task lull can't be
#                mistaken for completion (#85 bug 2) — deterministic WRITE→COMPACT.
#   2. COMPACT — type `/compact`+Enter, clear the resume/confirm modal (mirrors
#                handle_model's dialog-clear), then CONFIRM compaction actually
#                started (saw-busy-first: the ▰▱ bar appears) before waiting for it
#                to settle — so a not-yet-started compact isn't read as "done".
#   3. RESUME  — once compaction fully settles, inject a nudge to re-read the
#                carry-forward file and continue from its next-steps (always
#                auto-resume, issue #85 decision (a); safe-ish because it resumes
#                from the *written* next-steps, not open-ended). The daemon's job
#                ends HERE: it disarms the kill-switch the instant the resume nudge
#                is injected (#87) — the carry-forward is "complete" at resume.
#
# Kill-switch: it guards ONLY the daemon-driven WRITE→COMPACT→resume-inject stretch
# (the part the model cannot self-drive). While that runs, a REDIRECT message from
# The owner (plain text, /stop, /model, a new task) halts it (handle_message →
# halt_carry_forward): the flow state is popped and Escape interrupts the pane.
# Read-only status commands (/ctx /help /sessions /usage /peek) are EXEMPT — the
# daemon answers them from cache without touching the session, so they never halt
# (#87). Once the resume nudge is injected the flow is released, so any later
# message just flows to the session normally (never a spurious "halted"). Each
# phase re-checks it still owns the pane (unique token) before every action, so a
# halt/supersede aborts cleanly.
#
# Idle/turn detection: the WRITE→COMPACT hand-off is DETERMINISTIC (poll for the
# session-written done-marker), not idle-inferred. The remaining pane-idle waits
# (pre-inject settle, compaction-settle, post-resume monitor) use a SUSTAINED-idle
# gate (N consecutive not-busy samples) — a single check false-positives on idle
# scrollback. "Busy" is detected precisely, PER LINE, via _cf_line_is_busy — the live
# spinner elapsed-timer "…(Ns · …)" that current Claude Code shows during a turn, or the
# compaction bar (▰▱) — never a completed tool row's frozen "(Ns)" or idle scrollback.

CF_SETTLE = 5              # seconds to let a just-injected turn start before polling idle
CF_IDLE_SAMPLES = 3        # consecutive not-busy samples required (sustained-idle gate)
CF_IDLE_INTERVAL = 2       # seconds between idle samples
CF_SETTLE_WAIT = 90        # max seconds to wait for a pre-existing turn to settle before injecting
CF_WRITE_TIMEOUT = 300     # max seconds to wait for the carry-forward done-marker to appear
CF_COMPACT_START_WAIT = 25 # max seconds to confirm /compact actually started (saw-COMPACTING)
CF_COMPACT_TIMEOUT = 300   # max seconds to wait for compaction to finish
CF_MODAL_WAIT = 2.0        # seconds after /compact+Enter before checking for a modal
CF_COMPACT_TRIES = 3       # attempts to get /compact to actually start compacting (#101)
CF_COMPACT_IDLE_WAIT = 90  # max seconds to wait for the pane to go idle before injecting /compact (#101)

# Busy detection (#85 blocker 1). We must distinguish a LIVE active-turn/compaction
# status line from a COMPLETED command's frozen duration left in scrollback (e.g.
# "⎿  $ sleep 20 && echo hi (8s)"), which would otherwise pin an idle pane to "busy
# forever" (a bare "(\d+s" scan matched it). So busy is decided PER LINE, anchored to
# the live-status line:
#   esc to interrupt — the interrupt hint (older Claude Code + Codex still render it).
#   [▰▱]            — the /compact progress bar (first ~15s, before its "(Ns)" timer).
#   …(Ns            — the live spinner/compaction elapsed timer, but ONLY on the active
#                     status line: "✽ Mulling… (10s · …)" / "Compacting conversation… (8s)".
#                     The ellipsis-anchored "…(Ns" is the spinner signature; a completed
#                     tool-result row ("⎿ … (8s)") is a frozen duration, NOT busy, and is
#                     excluded outright by the ⎿ tool-result guard in _cf_line_is_busy.
_CF_INTERRUPT_RE = re.compile(r"esc to interrupt")
_CF_BAR_RE = re.compile(r"[▰▱]")
# Claude Code switches the elapsed timer to minutes past 60s ("…(4m 18s") and would
# presumably use hours beyond that, so a seconds-only pattern read every pane busy for
# a minute or more as IDLE — inverting every gate built on it exactly for the long turns
# worth not interrupting (#181). Anchored on the ellipsis and the first unit only, which
# is what keeps a wrapped tool-result continuation line ("… done (1m 36s" — no ellipsis
# before the paren) from matching.
_CF_SPINNER_TIMER_RE = re.compile(r"…[ \t]*\(\d+[hms]")  # live "…(Ns" / "…(4m 18s" timer
# Compaction-SPECIFIC signal (#101): the "Compacting conversation" status phrase (confirmed
# exact wording, see the busy-detection note above). _cf_line_is_compacting requires it to
# co-occur with a live-status signature (the "…(Ns" timer or the ▰▱ bar) on the same line, so
# an ordinary turn / a /compact QUEUED behind one (no phrase) AND idle prose that merely
# mentions the phrase (no live signature) are both excluded — that's how PHASE 2 tells a real
# compaction from a busy turn it was mistakenly reading as "compaction started/finished".
_CF_COMPACTING_RE = re.compile(r"Compacting conversation")
# A post-/compact selection/confirm modal (resume-from-summary or a "Switch model?"-style
# confirm). Anchored to the live selection cursor "❯ <n>." / "❯ Yes|…" so it NEVER matches
# prose in scrollback (docs/tests/review text) that merely says "recommended)" or
# "Resume from summary" (#85 should-fix 4) — those lack the ❯ live cursor.
_CF_MODAL_RE = re.compile(r"❯\s*(?:\d+\.|Yes\b|No\b|Resume\b|Keep\b|Continue\b|Compact\b)")
# A2 is absolute, and this is the one place that could break it by accident. _cf_clear_modal
# assumed the live cursor always rests on row 1 and pressed Enter on any ❯-row it saw; round
# 3 reproduced a capture with the cursor on row 3. Enter there is "Don't ask me again", which
# sets resumeReturnDismissed and disables Claude's resume picker on this machine PERMANENTLY
# — unrecoverable, and it would silently kill the feature this whole change is built on.
# Built from RESUME_MODAL_ROWS so the two cannot drift apart.
_CF_FORBIDDEN_ROW_RE = re.compile(r"❯\s*(?:\d+\.\s*)?" + re.escape(RESUME_MODAL_ROWS[2]),
                                  re.IGNORECASE)
# A PreCompact hook REFUSING /compact (#155). Claude Code answers a hook-blocked /compact in
# well under a second with "<local-command-stderr>Compaction blocked by PreCompact hook: …",
# so this is a DETERMINISTIC no — retrying it just burns CF_COMPACT_TRIES and ends in the
# generic "didn't start compacting" reply, which hides the hook's own reason from the owner.
_CF_HOOK_BLOCK_RE = re.compile(r"Compaction blocked by PreCompact hook")
# Confirmation applied to the JOINED window (tmux wraps, so the "[<command>]" can land on the
# next line): Claude Code always names the blocking command in brackets. Requiring it keeps
# ordinary scrollback that merely DISCUSSES a hook block — e.g. this repo's own docs, or an
# operator session reviewing a past failure — from aborting a healthy carry-forward.
_CF_HOOK_CONFIRM_RE = re.compile(r"Compaction blocked by PreCompact hook:\s*\[")
# Strip ONLY Claude Code's own wrapper around hook stderr. A general "<[a-z]…>" strip also ate
# legitimate reason text — `branch <main> must contain List<string>` was relayed as `branch
# must contain List`, deleting the actionable identifier while claiming to quote the hook
# verbatim. Arbitrary angle brackets are already safe: md_to_telegram_html escapes them.
_CF_HOOK_TAG_RE = re.compile(r"</?local-command-(?:stderr|stdout)>")
CF_HOOK_BLOCK_LINES = 4    # max capture lines per reason (tmux wraps); a boundary usually ends it first
CF_HOOK_BLOCK_CHARS = 400  # cap the quoted reason so a hook essay can't flood the topic
# A GitHub issue reference the session records for the carry-forward, verified by the
# daemon (#85 blocker 3): a full issue URL or the short owner/repo#N form.
_CF_ISSUE_REF_RE = re.compile(
    r"https?://github\.com/[\w.-]+/[\w.-]+/issues/\d+"
    r"|(?<![\w./])[\w.-]+/[\w.-]+#\d+"
)
def carry_forward_fallback_repo():
    """`owner/repo` for the daemon's fallback carry-forward issue, or None if unconfigured.

    The target used to be compiled in, so it named the author and pointed every fork at a
    repository they cannot write to (#204 D6). Unconfigured means no fallback issue is created;
    the caller already treats that as "gh could not do it" and the carry-forward FILE — which
    is what the auto-resume actually reads — is unaffected either way.

        "carry_forward_repo": "your-user/your-repo"
    """
    try:
        cfg = load_config()
    except (OSError, ValueError, TypeError, RecursionError, SystemExit):
        return None
    repo = cfg.get("carry_forward_repo") if isinstance(cfg, dict) else None
    # Same rule as above, and for the same reason the review found: `"  /  "` has one slash and
    # two non-empty halves, and is not a repository.
    if not _matches(_GH_REPO, repo):
        return None
    return repo.strip()

CF_WRITE_PROMPT = (
    "[tg-bridge carry-forward] the owner triggered /carryforward from Telegram; I (the bridge daemon) am "
    "driving carry-forward -> /compact -> auto-resume. Work FULLY AUTONOMOUSLY with NO interactive "
    "prompts: do NOT ask me to confirm anything, do NOT use AskUserQuestion or open any menu, do NOT "
    "load or invoke the /carryforward skill (THIS prompt is the protocol — the skill's confirm-first "
    "flow does NOT apply here), and do NOT run /compact yourself (I will). "
    "Step 1 — write a dense carry-forward to EXACTLY this absolute file: {path} — include, in order of "
    "forward-usefulness: session goal; state right now (what's done, what's running in the background "
    "with PIDs+log paths, the next concrete action); critical open items (findings/blockers/bugs) with "
    "file:line; open decisions needing the owner's input; the first 1-3 concrete next steps, specific enough "
    "to execute on resume; key file paths + line numbers; process learnings; anti-goals. Optimize for "
    "token-density of FORWARD utility so a fresh reader resumes from this file + repo state alone. "
    "Step 2 — ALWAYS record this carry-forward to a GitHub issue, deciding autonomously (NEVER asking): "
    "if this session already has a focus issue, post the carry-forward as a comment on it; otherwise "
    "CREATE one — in this session's working repo, or the `ops` repo for non-code/trivial work — with a "
    "concise title and the standard one-domain + one-priority + one-type labels, record it as the focus "
    "issue, and post the carry-forward as its first comment. If anything is ambiguous (no repo, trivial "
    "work, unsure which repo), DEFAULT to creating an issue in `ops` and proceed — do not wait for me. "
    "Step 3 — as your VERY LAST action, and ONLY after Steps 1-2 are fully complete, write the GitHub "
    "issue reference from Step 2 (the full issue URL like https://github.com/OWNER/REPO/issues/N, or the "
    "short OWNER/REPO#N form) into the marker file at EXACTLY this absolute path — run: "
    "printf '%s\\n' 'PASTE_THE_REAL_ISSUE_URL_HERE' > {marker}. Writing this file is how I know you are "
    "done AND records which issue holds the carry-forward, so write it ONLY now, and make sure it "
    "contains a REAL issue reference (never a placeholder) — if you somehow could not create/find an "
    "issue, still create the marker but leave it empty and I will create the issue myself. "
    "Step 4 — then STOP and end your turn: do not compact, do not start new work."
)
CF_RESUME_PROMPT = (
    "[tg-bridge carry-forward resume] Compaction is complete. Re-read your carry-forward at {path}, "
    "then resume by executing its concrete next steps now and continuing autonomously. If a next "
    "step needs the owner's decision, do the parts you can and ask them in this Telegram topic. The owner can "
    "steer or stop you at any time by messaging this topic — their message arrives in your normal inbox."
)

_pending_cf = {}                 # tid(str) -> {token, phase, pane, cf_file, marker, started}
_cf_lock = threading.Lock()      # guards _pending_cf across handler + worker threads


def is_carry_forward_command(cmd):
    """True for `/carryforward` (the primary, no-space form the owner uses), `/cf`, or
    `/carry [forward]` — the daemon-driven carry-forward. In a bridge topic this
    intercepts `/carryforward` on purpose, because the daemon has to run /compact
    (the model can't compact itself). The session-side interactive skill still
    works in non-bridge sessions, where the daemon isn't in the path."""
    parts = cmd.split()
    if not parts:
        return False
    if parts[0] in ("/carryforward", "/cf"):
        return True
    if parts[0] == "/carry":
        return len(parts) == 1 or parts[1].lower() == "forward"
    return False


def carry_forward_active(thread_id):
    with _cf_lock:
        return str(thread_id) in _pending_cf


def _cf_owns(tid, token):
    """True while this worker's run is still the active carry-forward for the topic
    (not killed, not superseded). Every pane action is gated on this."""
    with _cf_lock:
        st = _pending_cf.get(str(tid))
        return bool(st) and st.get("token") == token


def _cf_release(tid, token):
    """Pop the flow state iff `token` still owns it. Returns True if it did."""
    with _cf_lock:
        st = _pending_cf.get(str(tid))
        if st and st.get("token") == token:
            _pending_cf.pop(str(tid), None)
            return True
        return False


def _cf_set_phase(tid, token, phase):
    with _cf_lock:
        st = _pending_cf.get(str(tid))
        if st and st.get("token") == token:
            st["phase"] = phase


def _cf_line_is_busy(line):
    """True iff a SINGLE captured line is a live active-turn/compaction signal. A
    completed tool-result row ("⎿ … (8s)" frozen duration) is explicitly NOT busy —
    that scrollback residue is what used to pin an idle pane to busy (#85 blocker 1)."""
    if _CF_INTERRUPT_RE.search(line) or _CF_BAR_RE.search(line):
        return True
    if line.lstrip().startswith("⎿"):
        return False  # completed tool-result row: its "(Ns)" is a frozen duration, not live
    return bool(_CF_SPINNER_TIMER_RE.search(line))


def _cf_text_is_busy(text):
    """True iff ANY line of a pane capture reads as a live busy signal."""
    return any(_cf_line_is_busy(ln) for ln in text.splitlines())


def _cf_busy(pane):
    """Precise busy check. Err toward BUSY (True) on a bad capture so we never act
    (inject/compact) on unverified state."""
    try:
        out = _tmux(
            ["tmux", "capture-pane", "-p", "-t", pane, "-S", "-25"],
            capture_output=True, text=True,
        )
        if out.returncode != 0:
            return True
        return _cf_text_is_busy(out.stdout)
    except Exception:
        return True


def _cf_line_is_compacting(line):
    """True iff a line is the LIVE compaction status — NOT merely any busy turn (#101), and
    NOT idle scrollback that merely MENTIONS compaction. The live status is
    "Compacting conversation… (Ns)"; we require the phrase AND a live-status signature on the
    SAME line — the "…(Ns" elapsed timer or the ▰▱ progress bar — exactly as _cf_line_is_busy
    anchors its spinner. Otherwise prose / a prior run's residue / this very bug's discussion
    text ("…the Compacting conversation footer…") would read as a live compaction and re-open
    the #101 false success. A completed '⎿ … (Ns)' tool-result row is never live either."""
    if line.lstrip().startswith("⎿"):
        return False
    if not _CF_COMPACTING_RE.search(line):
        return False
    return bool(_CF_SPINNER_TIMER_RE.search(line) or _CF_BAR_RE.search(line))


def _cf_text_is_compacting(text):
    """True iff ANY line of a pane capture reads as a live compaction signal."""
    return any(_cf_line_is_compacting(ln) for ln in text.splitlines())


def _cf_compacting(pane):
    """Compaction-SPECIFIC check for PHASE 2 (#101). Note the INVERTED bad-capture default
    vs _cf_busy: _cf_busy errs toward BUSY (True) so we never act on unverified state, but
    a false 'compacting' would let a NON-compaction be read as success — the exact #101 bug
    — so here a bad capture returns False and the caller times out / retries."""
    try:
        out = _tmux(
            ["tmux", "capture-pane", "-p", "-t", pane, "-S", "-25"],
            capture_output=True, text=True,
        )
        if out.returncode != 0:
            return False
        return _cf_text_is_compacting(out.stdout)
    except Exception:
        return False


def _cf_hook_block_all(text):
    """Every PreCompact refusal visible in a capture, in pane order (newest last) (#155).

    Each refusal is read as a BOUNDED block, not a blind CF_HOOK_BLOCK_LINES join: the reason
    ends at the first blank line (our own hook prints one before its instructions), at the next
    command echo or prompt, at the next tool-result row, or at another refusal. Without that
    boundary a SHORT refusal absorbed the `/compact` we had just echoed, so the same displayed
    refusal read differently before and after the injection and passed the freshness check
    below (Codex round 2 of PR #156).

    Multi-line reasons are still joined and whitespace-collapsed, because tmux wraps them."""
    lines = text.splitlines()
    out = []
    for i, line in enumerate(lines):
        if not _CF_HOOK_BLOCK_RE.search(line):
            continue
        block = [line]
        for nxt in lines[i + 1:i + CF_HOOK_BLOCK_LINES]:
            stripped = nxt.strip()
            if not stripped or stripped[0] in ">❯⎿" or _CF_HOOK_BLOCK_RE.search(nxt):
                break
            block.append(nxt)
        joined = _CF_HOOK_TAG_RE.sub("", " ".join(block))
        joined = " ".join(joined.split())          # collapse the wrap whitespace
        if not _CF_HOOK_CONFIRM_RE.search(joined):
            continue                               # prose about a block, not a live one
        joined = joined[:CF_HOOK_BLOCK_CHARS].strip()
        if joined:
            out.append(joined)
    return out


def _cf_hook_block_text(text):
    """The NEWEST PreCompact refusal in a capture, as one readable line, or None (#155).
    Newest = lowest on the pane, so a stale refusal sitting above a fresh one can't mask it."""
    blocks = _cf_hook_block_all(text)
    return blocks[-1] if blocks else None


def _cf_capture_tail(pane):
    """The pane's recent lines, or None if it can't be read (#155). -J joins tmux-soft-wrapped
    lines, so a reason that ran past the pane width comes back whole, not split mid-word."""
    try:
        out = _tmux(
            ["tmux", "capture-pane", "-p", "-J", "-t", pane, "-S", "-25"],
            capture_output=True, text=True,
        )
        if out.returncode != 0:
            return None
        return out.stdout
    except Exception:
        return None


def _cf_hook_block_reason(pane, before_text):
    """The PreCompact hook's refusal, but ONLY if it is NEW since `before_text` (#155).

    Detection is textual, and render syntax alone cannot prove freshness: a pane can DISPLAY
    a perfectly formed past refusal — an operator reading this diff, a raw capture pasted
    into the scrollback — and matching it would abort a carry-forward that still had
    CF_COMPACT_TRIES-1 attempts left. That is a real loss, not a cosmetic one, which is why
    "both paths abort anyway" was the wrong call (Codex review of PR #156).

    Freshness therefore comes from the snapshot the caller takes immediately BEFORE injecting
    /compact: a refusal already on the pane then cannot be the answer to this injection. Under
    ordinary append-and-scroll output, content only moves up and out of the window, never into
    it — a row still visible now at distance d from the bottom sat at d-k when the snapshot was
    taken (k = lines added since), and d <= 25 gives d-k <= 25 — so a stale refusal may drop out
    of the after-capture (we miss and retry, as before) but a fresh one cannot be mistaken for
    something the snapshot already held. That is a property of append/scroll, NOT a tmux
    invariant: a pane clear or full-screen repaint can reintroduce rows (Codex round 3 of PR
    #156). Nothing on this path clears or repaints the pane, and an identical repaint cancels in
    the multiset below, so the residue is a weaker proof rather than a known false positive.

    Comparison is by OCCURRENCE, not by text prefix. Every refusal from one hook shares a long
    constant head ("Compaction blocked by PreCompact hook: [bash …/pre-compact-check.sh]"),
    so a prefix probe let any previously displayed refusal mask a genuinely fresh one with a
    DIFFERENT reason — which retried a deterministic refusal and hid its cause, the very thing
    this change exists to stop (Codex round 2 of PR #156). Matching each after-block against the
    snapshot's blocks as a multiset also reports a second, identical refusal correctly.

    Returns None whenever freshness is unprovable — a missing snapshot or an unreadable pane —
    so the caller falls through to the ordinary retry/timeout path rather than acting on
    unverified state (the same inverted default as _cf_compacting)."""
    if before_text is None:
        return None
    after = _cf_capture_tail(pane)
    if after is None:
        return None
    seen = _cf_hook_block_all(before_text)
    for block in _cf_hook_block_all(after):
        if block in seen:
            # Accounted for by the snapshot; a REPEAT still counts as new. If an old identical
            # occurrence scrolls out as a new one arrives the counts cancel and we miss — a
            # conservative false negative, never a wrong reason (Codex round 3).
            seen.remove(block)
            continue
        return block
    return None


def _cf_inject_owned(tid, token, pane, text, settle=0.5, release_after=False):
    """Ownership-gated inject (#85 blocker 2 — kill-switch TOCTOU). Re-checks ownership
    and performs the whole type-text→Enter ATOMICALLY under _cf_lock, so a halt that
    already popped the flow (consuming the user's message) can NEVER be followed by an
    inject that resurrects the session. The `settle` gap lets a slash-command menu land
    on the exact match before Enter. Returns True if injected, False if skipped because
    the flow was halted/superseded before the send.

    release_after=True also POPS the flow (disarms the kill-switch) in the SAME lock hold
    as the send — used for the final resume inject so no redirect can slip into the gap
    between 'resumed' and 'released' and halt an already-resumed session (#88 review r2).

    The send goes through type_line (#133): _cf_wait_idle defines idle as "no spinner and no
    compaction bar", which an approval / update / model-confirm modal satisfies, so this path
    could type a prompt into a picker and have its Enter select the default. Carry-forward is
    claude-only, but claude has modals too. Lock order is _cf_lock → the per-pane lock, and
    nothing acquires them the other way round. The Telegram escalation is deliberately sent
    AFTER the lock is dropped — a network call under _cf_lock would stall getUpdates."""
    blocked = False
    try:
        with _cf_lock:
            st = _pending_cf.get(str(tid))
            if not (st and st.get("token") == token):
                return False
            status = type_line(pane, text, settle=settle)
            if status != "sent":
                log(f"carry-forward inject {status} (pane {pane}, topic {tid})")
                blocked = status == "swallowed"
                return False
            if release_after:
                _pending_cf.pop(str(tid), None)
            return True
    finally:
        if blocked:
            report_blocked_pane(tid, pane, "a carry-forward step")


def _cf_modal_present(text):
    return bool(_CF_MODAL_RE.search(text))


def _cf_clear_modal(tid, token, pane):
    """After /compact, Claude Code may show a resume-from-summary / confirm modal whose
    default (❯ option 1) is selected by Enter. Only send Enter when a modal is actually
    detected (#85 should-fix 4 — narrowed to the live ❯-cursor block, not arbitrary tail
    prose), and only while THIS flow still owns the pane, sent atomically under _cf_lock so a
    halt can't be followed by a stray Enter (#85 blocker 2).

    Also under the PANE lock, with the modal RE-checked inside it (#165 review r2). Leaving
    this one Enter unlocked was defended as safe because it types nothing and confirms a
    detected modal — that was wrong, and Codex reproduced the failure: a concurrent type_line
    (in this process or a `tg-bridge notify` one, which has no view of _pending_cf) types its
    payload into the composer, this Enter SUBMITS someone else's text, and type_line then sees
    its own text in the transcript, reads that as a rise, and sends a SECOND Enter. Observed
    enters=2. Unlike the deliberately unlocked halt Escapes, Enter can commit another
    injector's content, so it has to be serialized with them.

    The capture drops `-S -12` on purpose: with history included, a ❯-cursor block that was
    live BEFORE the compaction can still sit in scrollback and authorise this Enter against
    today's idle composer. Only the visible screen is evidence about what is on screen now."""
    try:
        with _cf_lock:
            st = _pending_cf.get(str(tid))
            if not (st and st.get("token") == token):
                return False
            with _pane_lock(pane):
                cap = _tmux(["tmux", "capture-pane", "-p", "-t", pane],
                            capture_output=True, text=True)
                if cap.returncode != 0 or not _cf_modal_present(cap.stdout):
                    return False
                if _CF_FORBIDDEN_ROW_RE.search(cap.stdout):
                    # A2. Never press Enter here: the selected row is "Don't ask me again",
                    # and that choice is permanent and machine-wide. Leaving the modal up is
                    # recoverable — a human can answer it — so refusing is strictly safer
                    # than clearing it. Report rather than fail silently.
                    log(f"carry-forward: REFUSING Enter on pane {pane} — the resume picker's "
                        f"cursor is on 'Don't ask me again' (A2)")
                    report_blocked_pane(tid, pane,
                                        "its post-/compact modal, because the cursor is on "
                                        "\"Don't ask me again\" and pressing Enter there "
                                        "would disable Claude's resume picker permanently")
                    return False
                _tmux(["tmux", "send-keys", "-t", pane, "Enter"],
                      check=True, capture_output=True)
        log(f"carry-forward: cleared post-/compact modal on pane {pane}")
        return True
    except PaneLockUnavailable as e:
        log(f"carry-forward modal-clear skipped (pane {pane}): {e}")
    except Exception as e:
        log(f"carry-forward modal-clear error (pane {pane}): {e}")
    return False


def _cf_wait_idle(tid, token, pane, timeout):
    """Block until the pane is sustained-idle (CF_IDLE_SAMPLES consecutive not-busy
    samples), the flow is aborted, or timeout. Returns "idle" / "aborted" / "timeout"."""
    deadline = time.time() + timeout
    streak = 0
    while time.time() < deadline:
        if not _cf_owns(tid, token) or not pane_alive(pane):
            return "aborted"
        if _cf_busy(pane):
            streak = 0
        else:
            streak += 1
            if streak >= CF_IDLE_SAMPLES:
                return "idle"
        time.sleep(CF_IDLE_INTERVAL)
    return "timeout"


def _cf_wait_done(tid, token, pane, cf_file, marker, timeout):
    """Deterministic WRITE-completion gate (#85 bug 2). Block until the session's
    done-MARKER exists AND the carry-forward file is non-empty AND the pane is
    sustained-idle — or the flow aborts / times out. The marker is written by the
    model as its LAST action, so polling for it (instead of inferring completion
    from idle) removes the mid-task-lull false-positive that fired /compact
    mid-turn. Returns "done" / "aborted" / "timeout"."""
    deadline = time.time() + timeout
    streak = 0
    while time.time() < deadline:
        if not _cf_owns(tid, token) or not pane_alive(pane):
            return "aborted"
        done = (os.path.exists(marker)
                and os.path.exists(cf_file) and os.path.getsize(cf_file) > 0)
        if done and not _cf_busy(pane):
            streak += 1
            if streak >= CF_IDLE_SAMPLES:
                return "done"
        else:
            streak = 0
        time.sleep(CF_IDLE_INTERVAL)
    return "timeout"


def _cf_wait_busy(tid, token, pane, timeout):
    """Saw-busy-first gate (#85 bug 2): block until the pane shows a busy signal (a
    turn / compaction actually started), the flow aborts, or timeout. Used after
    /compact so a not-yet-started compaction is never mistaken for "done"/idle.
    Returns "busy" / "aborted" / "timeout"."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not _cf_owns(tid, token) or not pane_alive(pane):
            return "aborted"
        if _cf_busy(pane):
            return "busy"
        time.sleep(0.5)
    return "timeout"


def _cf_wait_compacting(tid, token, pane, timeout):
    """Compaction-specific saw-started gate (#101): block until the pane shows a REAL
    compaction (_cf_compacting), the flow aborts, or timeout. Replaces the generic
    _cf_wait_busy in PHASE 2 so a /compact queued behind an ordinary turn — which the
    generic busy check misread as "compaction started" — is no longer a false success.
    Returns "compacting" / "aborted" / "timeout"."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not _cf_owns(tid, token) or not pane_alive(pane):
            return "aborted"
        if _cf_compacting(pane):
            return "compacting"
        time.sleep(0.5)
    return "timeout"


def _cf_cleanup_marker(marker):
    """Best-effort remove of the done-marker (before a run, so a stale marker can't
    short-circuit the gate; and after, so markers don't accumulate)."""
    try:
        if marker and os.path.exists(marker):
            os.remove(marker)
    except OSError:
        pass


def _cf_read_issue_ref(marker):
    """Return the GitHub issue reference the session wrote into the marker (full URL or
    owner/repo#N), or None if the marker is absent/empty/holds no valid ref (#85 blocker 3)."""
    try:
        with open(marker, encoding="utf-8", errors="replace") as f:
            content = f.read()
    except OSError:
        return None
    m = _CF_ISSUE_REF_RE.search(content)
    return m.group(0) if m else None


def _cf_create_fallback_issue(cfg, thread_id, name, cf_file):
    """Daemon-side fallback (#85 blocker 3): when the session left no valid issue ref, the
    daemon itself records the carry-forward to a GitHub issue so the 'always a durable
    record' contract holds deterministically. Returns the issue URL, or None if gh failed."""
    repo = carry_forward_fallback_repo()
    if repo is None:
        log(f"carry-forward: no 'carry_forward_repo' configured, so no fallback issue "
            f"(topic {thread_id}); the carry-forward file is written and is what the resume reads")
        return None
    title = f"Carry-forward: {name} ({datetime.now():%Y-%m-%d %H:%M})"
    try:
        res = subprocess.run(
            [GH_BIN, "issue", "create", "--repo", repo,
             "--title", title, "--body-file", cf_file],
            capture_output=True, text=True, timeout=60,
        )
        out = (res.stdout or "").strip()
        url = out.splitlines()[-1].strip() if out else ""
        if res.returncode == 0 and _CF_ISSUE_REF_RE.search(url):
            log(f"carry-forward: daemon fallback issue created: {url} (topic {thread_id})")
            return url
        log(f"carry-forward: fallback gh issue create failed (topic {thread_id}) "
            f"rc={res.returncode}: {(res.stderr or '').strip()[:200]}")
    except Exception as e:
        log(f"carry-forward: fallback gh issue create error (topic {thread_id}): {e}")
    return None


def _cf_verify_or_create_issue(cfg, thread_id, name, cf_file, marker):
    """Enforce the 'always a durable GitHub record' contract (#85 blocker 3): prefer the
    issue reference the session wrote into the marker; if it's absent/invalid, the daemon
    creates the issue itself from the carry-forward file. Returns (ref_or_None, source)
    where source is 'session' | 'daemon' | None (None only if even the fallback failed)."""
    ref = _cf_read_issue_ref(marker)
    if ref:
        log(f"carry-forward: issue recorded by session: {ref} (topic {thread_id})")
        return ref, "session"
    log(f"carry-forward: no valid issue ref in marker (topic {thread_id}) — daemon creating fallback")
    ref = _cf_create_fallback_issue(cfg, thread_id, name, cf_file)
    return (ref, "daemon") if ref else (None, None)


def handle_carry_forward(cfg, thread_id, text, info, pane):
    """Start the daemon-driven carry-forward. Returns True iff a worker was started, so
    callers that recorded state on the assumption it ran can undo that (#161)."""
    if not pane or not pane_alive(pane):
        reply(cfg, thread_id, "Can't carry forward: no live terminal bound to this topic.")
        return False
    engine = engine_of_pane(pane)
    if engine != "claude":
        reply(cfg, thread_id, "Carry-forward is Claude-only (Codex has no /compact-driven flow).")
        return False
    tid = str(thread_id)
    with _cf_lock:
        if tid in _pending_cf:
            reply(cfg, thread_id, "A carry-forward is already running for this topic — "
                                  "send any message to halt it first.")
            return False
        token = f"{time.time():.6f}-{threading.get_ident()}"
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        cf_file = state_path("topics", tid, f"carry-forward-{ts}.md")  # ensures the dir exists
        marker = cf_file + ".done"  # session touches this as its last action; daemon polls for it
        _pending_cf[tid] = {"token": token, "phase": "settle", "pane": pane,
                            "cf_file": cf_file, "marker": marker, "started": time.time()}
    if not reply(cfg, thread_id, (
        f"🧭 Carry-forward ({info.get('name', '?')}): waiting for the session to settle, then "
        f"writing state → /compact → auto-resuming from its next-steps. "
        f"Send any message here to halt."
    )):
        # Same contract as the auto path: the halt instruction is IN this notice, so a
        # carry-forward the owner cannot see or stop must not begin (#161).
        _cf_release(tid, token)
        log(f"carry-forward aborted: topic {thread_id} can't receive the start notice")
        return False
    log(f"carry-forward started (topic {thread_id}, pane {pane}, file {cf_file})")
    # Open the unfinished-run record BEFORE the worker exists, so there is no window in which
    # a run is under way with nothing on disk saying so. Everything after this either closes
    # it by compacting or leaves it open — including a daemon that is killed outright, which
    # is the case no `finally` can cover and the one that kept #239 alive (#240 review r2).
    #
    # And do not run without it. A carry-forward whose record could not be written is one
    # whose failure can never re-arm the topic — it would compact-or-die silently and leave
    # auto-CF dead exactly as #239 described (#240 review r3, finding 2). The state directory
    # being unwritable also means the carry-forward file itself cannot be written, so this
    # refuses a run that was going to fail two steps later anyway, with a worse story.
    if not _cf_mark_unfinished(tid, token):
        _cf_release(tid, token)
        reply(cfg, thread_id, "⚠️ Can't carry forward: the bridge could not write to its own "
                              "state directory. Nothing was started.")
        return False
    name = info.get("name", "session")
    try:
        threading.Thread(target=_carry_forward_worker,
                         args=(cfg, thread_id, pane, cf_file, marker, token, name),
                         daemon=True).start()
    except RuntimeError as e:
        # Thread creation can fail outright under resource exhaustion. Without this the topic
        # keeps a pending flow no worker will ever release, plus a record for a run that never
        # happened (#240 review r3, finding 6).
        _cf_clear_unfinished(tid, token)
        _cf_release(tid, token)
        log(f"carry-forward: could not start the worker for topic {thread_id}: {e}")
        reply(cfg, thread_id, f"⚠️ Can't carry forward: the worker thread would not start ({e}).")
        return False
    return True


def _carry_forward_worker(cfg, thread_id, pane, cf_file, marker, token, name):
    tid = str(thread_id)
    try:
        # PHASE 1 — WRITE: let any in-flight turn settle, inject the CF prompt, then
        # wait DETERMINISTICALLY for the session's done-marker (#85 bug 2) — a pure
        # idle gate false-fired on mid-task lulls and ran /compact mid-turn.
        _cf_cleanup_marker(marker)  # clear any stale marker so it can't short-circuit the gate
        if _cf_wait_idle(tid, token, pane, CF_SETTLE_WAIT) != "idle":
            if _cf_owns(tid, token):
                _cf_release(tid, token)
                # Not "try /cf again once it's idle" any more: the daemon now retries this
                # itself (#239), and a notice that sends the owner to do by hand what is
                # already coming is the same defect as the one that prompted the fix — a
                # bridge message that misdescribes what the bridge will do. But only where
                # the retry can actually happen: a `/cf` typed by hand on an exempt topic, or
                # with auto-CF switched off entirely, gets no retry, and promising one there
                # would be the same defect pointing the other way (#240 review r1, finding 5).
                # Wrapped: this notice is required to reach the owner (C5), and working out
                # which wording to use must not be able to stop it. `load_autocf_exempt`
                # catches OSError and ValueError, but a pathological exemption file raises
                # RecursionError out of json.load, which the worker's outer handler would
                # turn into silence (#240 review r2, finding 4). Anything unexpected falls
                # back to the wording that promises nothing.
                try:
                    retried = bool(AUTOCF_PCT) and tid not in load_autocf_exempt()
                except Exception:
                    retried = False
                reply(cfg, thread_id, "⚠️ Session stayed mid-turn — carry-forward aborted. "
                      + ("The daemon will try again on its own; /cf forces one now."
                         if retried else "Try /cf again once it's idle."))
            return
        if not pane_alive(pane):
            return
        if not _cf_inject_owned(tid, token, pane, CF_WRITE_PROMPT.format(path=cf_file, marker=marker)):
            return  # halted between the settle gate and the inject
        time.sleep(CF_SETTLE)  # let the write turn actually start
        res = _cf_wait_done(tid, token, pane, cf_file, marker, CF_WRITE_TIMEOUT)
        if res == "aborted":
            return
        if res == "timeout":
            if _cf_release(tid, token):
                # the session may be stuck on an interactive prompt it shouldn't have
                # opened; Escape dismisses it so we don't leave the pane blocked.
                if pane_alive(pane):
                    _tmux(["tmux", "send-keys", "-t", pane, "Escape"], capture_output=True)
                reply(cfg, thread_id, "⚠️ Carry-forward didn't signal completion in time — aborted "
                                      "before /compact (dismissed any open prompt). Session left as-is.")
            return

        # WRITE done — enforce the "always a durable GitHub record" contract (#85 blocker 3):
        # prefer the issue ref the session wrote into the marker; if it's absent/invalid, the
        # daemon creates the issue itself from the CF file. Proceed to /compact either way (the
        # local CF file is a durable record); only warn if even the fallback couldn't record it.
        if not _cf_owns(tid, token):
            return
        issue_ref, _issue_src = _cf_verify_or_create_issue(cfg, thread_id, name, cf_file, marker)
        if issue_ref is None:
            reply(cfg, thread_id,
                  f"⚠️ Carry-forward saved LOCALLY only — could not create a GitHub issue "
                  f"(gh may be down/unauthenticated). Durable file: {cf_file}")

        # PHASE 2 — COMPACT (#101). The WRITE→issue-record step just above runs a gh call
        # (up to ~60s), a window in which a heavy session can pick up a QUEUED turn — so a
        # /compact injected blindly here would QUEUE behind that turn instead of compacting.
        # The old gate then watched for GENERIC busy (esc-to-interrupt / ✽ spinner) and
        # misread that ordinary turn as "compaction started" and its end as "done" → the
        # false success that left t4111 uncompacted. Fix: (1) wait for the pane to actually
        # go IDLE before injecting /compact (so it can't queue), (2) confirm a COMPACTION-
        # specific signal via _cf_wait_compacting (not any busy turn), (3) retry the whole
        # inject a few times. Only a confirmed compaction proceeds to auto-resume.
        _cf_set_phase(tid, token, "compact")
        started = "timeout"
        for _attempt in range(CF_COMPACT_TRIES):
            if not pane_alive(pane):
                return
            # /compact must land on an IDLE pane, else Claude Code queues it behind the turn.
            idle = _cf_wait_idle(tid, token, pane, CF_COMPACT_IDLE_WAIT)
            if idle == "aborted":
                return
            if idle != "idle":
                continue  # still busy after the wait — retry (or fall through to the abort)
            # Snapshot BEFORE the inject so a refusal found afterwards can be proved fresh (#155).
            pane_before = _cf_capture_tail(pane)
            if not _cf_inject_owned(tid, token, pane, "/compact", settle=0.7):
                return
            time.sleep(CF_MODAL_WAIT)
            _cf_clear_modal(tid, token, pane)
            started = _cf_wait_compacting(tid, token, pane, CF_COMPACT_START_WAIT)
            if started == "aborted":
                return
            if started == "compacting":
                break
            # A PreCompact hook refusal is deterministic (#155): the next two injections
            # would be refused identically, and the generic timeout reply below would hide
            # WHY. Report the hook's own words and stop. Note the session cannot fix this
            # itself — CF_WRITE_PROMPT step 4 told it to end its turn, so the hook's
            # "post a comment and re-run compact" instructions reach nobody.
            blocked = _cf_hook_block_reason(pane, pane_before)
            if blocked:
                log(f"carry-forward: /compact refused by a PreCompact hook "
                    f"(topic {thread_id}, pane {pane}): {blocked}")
                if _cf_release(tid, token):
                    reply(cfg, thread_id, "⚠️ /compact was refused by a PreCompact hook — NOT "
                                          f"auto-resuming. The hook said: {blocked}")
                return
            _cf_clear_modal(tid, token, pane)  # a beat-late modal may still block the start; clear before retry
        if started != "compacting":
            if _cf_release(tid, token):
                reply(cfg, thread_id, f"⚠️ /compact didn't start compacting (retried {CF_COMPACT_TRIES}×) "
                                      "— NOT auto-resuming. Check the session terminal.")
            return
        res = _cf_wait_idle(tid, token, pane, CF_COMPACT_TIMEOUT)
        if res == "aborted":
            return
        if res == "timeout":
            if _cf_release(tid, token):
                reply(cfg, thread_id, "⚠️ Compaction didn't settle in time — NOT auto-resuming. "
                                      "Check the session terminal.")
            return

        # A compaction started (a compaction-specific signal, not a busy pane) and then
        # settled — the same evidence this flow already requires before it dares auto-resume.
        # It is NOT a reading of the context: nothing here has looked at occupancy, so "the
        # compaction lowered it past the re-arm point" is not established and must not be
        # written down as if it were (#240 review r1, finding 4). What follows from it is
        # narrower and enough: this run did the thing it exists to do, so it is not the
        # never-compacted case #239 is about, and if occupancy did fall the ordinary
        # hysteresis clears the armed flag without any help from the record.
        #
        # So close the record HERE, on the line where that becomes true — not in a `finally`
        # three phases later, past a settle, an injection and a reply. A daemon killed in that
        # window would leave an open record for a run that demonstrably compacted, and fifteen
        # minutes later the record would buy an unearned carry-forward (#240 review r3,
        # finding 3). Writing the fact where the fact becomes true is the rule #241 is about,
        # and the `finally` was itself a violation of it.
        _cf_clear_unfinished(tid, token)

        # PHASE 3 — RESUME + DISARM: inject the auto-resume nudge (always, per #85
        # decision (a)); then the daemon's job is DONE, so disarm the kill-switch right
        # here (#87). The kill-switch exists ONLY to abort the write→compact stretch — the
        # part the model cannot self-drive. Once the session is resumed it is just working
        # normally and the owner steers/stops it the usual way (a queued message, `!` interrupt,
        # or /stop). Keeping it armed through a trailing "monitor" window made ANY later
        # message — even a passive /ctx — fire a spurious "carry-forward halted" after the
        # flow was already done. That was the bug.
        _cf_set_phase(tid, token, "resume")
        if not _cf_owns(tid, token) or not pane_alive(pane):
            return
        time.sleep(CF_SETTLE)  # let the SessionStart:compact hook / fresh prompt settle
        if not pane_alive(pane):
            return
        # Inject the resume nudge AND disarm the kill-switch atomically under one _cf_lock
        # hold (release_after=True): so a redirect arriving at this instant either wins the
        # lock BEFORE the send (halts cleanly — the session isn't resumed yet) or finds the
        # flow already gone AFTER (delivered normally). No in-between window can halt an
        # already-resumed session (#88 review r2).
        if not _cf_inject_owned(tid, token, pane, CF_RESUME_PROMPT.format(path=cf_file),
                                release_after=True):
            return
        log(f"carry-forward: auto-resume injected + kill-switch disarmed (topic {thread_id}, pane {pane})")
        reply(cfg, thread_id, "▶️ Compaction done — session resumed from its carry-forward "
                              "next-steps. Carry-forward complete.")
    except Exception as e:
        log(f"carry-forward worker error (topic {thread_id}): {e}")
    finally:
        _cf_release(tid, token)  # safety net: never leak an armed flow
        _cf_cleanup_marker(marker)  # don't leave the done-marker behind


def halt_carry_forward(cfg, thread_id, reason):
    """Kill-switch: pop the active flow and interrupt the pane. Returns True if a
    flow was actually halted (so the caller can consume the triggering message)."""
    tid = str(thread_id)
    with _cf_lock:
        st = _pending_cf.pop(tid, None)
    if not st:
        return False
    pane = st.get("pane")
    # Best-effort pane interrupt. The flow is already popped (the halt has functionally
    # succeeded), so a tmux timeout here must NOT skip the user reply or propagate into the
    # getUpdates loop (#110) — swallow it and still confirm the halt.
    try:
        if pane and pane_alive(pane):
            _tmux(["tmux", "send-keys", "-t", pane, "Escape"], capture_output=True)
    except subprocess.TimeoutExpired:
        log(f"carry-forward halt: tmux timed out interrupting pane {pane} (topic {thread_id}); "
            f"flow already popped, continuing")
    reply(cfg, thread_id, (
        "🛑 Carry-forward halted — session interrupted. Your message was NOT delivered; "
        "resend it if you want the session to act on it."
    ))
    log(f"carry-forward halted (topic {thread_id}, phase {st.get('phase')}): {reason}")
    return True


def main():
    # First line of the process: everything below creates files, and the umask decides
    # their modes before any mode argument this code passes can matter.
    secure_process_umask()
    cfg = load_config()
    # Before anything reads state. Two earlier versions of this comment were wrong about that
    # and were believed because they were comments: the first ran after `load_offset`, and the
    # second still ran after `_load_pending_reopens()` at module scope, which reads at IMPORT —
    # earlier than any line in this function can be. That read is now deferred to below (#241,
    # #233). A group-writable state tree means a peer account can append to an inbox and have
    # an agent read it as if it came from Telegram (#245). Say what was changed — silently
    # altering the owner's filesystem is not a fix they can audit.
    tightened = secure_state_tree()
    if tightened:
        changed, before, after = tightened
        # Two different facts, and one sentence cannot carry both honestly. When the root was
        # already private and only entries inside were not, saying "the directory was mode
        # 0o700, which let other local accounts write to it" is false about 0o700 — the first
        # version of this line said exactly that (#233's class, in the fix for #245).
        entries = f"{changed} entr" + ("y" if changed == 1 else "ies")
        if before != after:
            log(f"state directory was mode {before}, which let other local accounts write to "
                f"it — narrowed to {after}. {entries} in the tree were narrowed.")
        else:
            log(f"state directory was already {before}, but {entries} inside it were "
                f"writable by other local accounts — narrowed.")
        log("Anything that could append to a topic inbox could put text in front of an agent "
            "without passing the owner check.")
    for ancestor in unsafe_state_ancestors():
        log(f"WARNING: {ancestor} is writable by other local accounts. The state directory "
            f"below it is private, but write on a directory permits replacing any entry in "
            f"it, so a peer can swap the whole state directory for one they control. This is "
            f"not the daemon's to fix — chmod go-w it if nothing else needs to write there.")
    # The two state reads, both after the remediation above. `.update`, not a rebinding:
    # every other reference in this module mutates this dict in place.
    pending_reopens.update(_load_pending_reopens())
    offset = load_offset()
    restore_on_boot(cfg)  # BEFORE any loop or getUpdates: no thread may observe stale registry
    resend_undelivered_reopen_questions(cfg)  # a question persisted but never sent (C5)
    threading.Thread(target=warning_loop, args=(cfg,), daemon=True).start()
    threading.Thread(target=lifecycle_loop, args=(cfg,), daemon=True).start()
    threading.Thread(target=dashboard_loop, args=(cfg,), daemon=True).start()
    threading.Thread(target=idle_sweep_loop, args=(cfg,), daemon=True).start()
    threading.Thread(target=snapshot_loop, args=(cfg,), daemon=True).start()
    log(f"daemon started, chat_id={cfg['chat_id']}, offset={offset}")
    while True:
        try:
            updates = api(cfg["bot_token"], "getUpdates", {
                "offset": offset,
                "timeout": POLL_TIMEOUT,
                "allowed_updates": ALLOWED_UPDATES,
            }, timeout=POLL_TIMEOUT + 20)
        except Exception as e:
            log(f"getUpdates error: {e}")
            time.sleep(5)
            continue
        for update in updates:
            offset = update["update_id"] + 1
            msg = update.get("message")
            if msg:
                try:
                    handle_message(cfg, msg)
                except Exception as e:
                    log(f"handle_message error: {e}")
        if updates:
            save_offset(offset)


if __name__ == "__main__":
    main()
