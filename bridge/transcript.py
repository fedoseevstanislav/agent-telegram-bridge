"""Read a Claude session's own transcript — the daemon's ground truth (#157).

The daemon has three times inferred what a session *just did* by reading rendered text out
of a tmux pane, and three times that premise broke: #101 (a busy turn misread as compaction),
#133/#163 (a pane that took the text read as having swallowed it, and vice versa), #155 (a
*displayed* refusal read as the answer to the `/compact` we just injected). Rendered text
cannot establish causation. Claude Code, however, already writes an append-only JSONL record
of exactly these events.

Three measured facts shape this module, all from real transcripts rather than assumption:

1. **A record is written at SUBMIT, not at paste.** Text that sat unsubmitted in a composer
   for two minutes produced nothing at all; the `user` record appeared within a second of the
   Enter. So a transcript read can CONFIRM an injection landed, but can never authorize the
   Enter — at the moment of that decision the transcript contains nothing about our text.
2. **Only 29 of 45 registered topics have a transcript on disk.** Claude Code deletes them
   after `cleanupPeriodDays` (default 30). Every caller must handle the unknown case.
3. **Not every `user` record is something a user submitted.** Scanning 63,474 real `user`
   records found 943 `isMeta`, 135 `isCompactSummary`, and 135 `isVisibleInTranscriptOnly` —
   all synthetic context written by the client. A compaction summary is prose ABOUT the
   session, so it quotes payloads that were never re-submitted: 94 of 136 summary records
   satisfy a naive `/compact` search. Treating those as receipts turns this module into the
   pane-scraping bug in a new medium, so every predicate here filters them out.

The reading API is deliberately three-valued. `[]` and "unknown" are different answers, and
collapsing them is how the pane helpers failed: absence of evidence was read as evidence of
absence. Callers may treat absence as proof ONLY when the status is OK.
"""

import hashlib
import json
import os
import re

PROJECTS_DIR = os.path.expanduser("~/.claude/projects")
TAIL_BYTES = 2 * 1024 * 1024
# Hashed to detect a file that was replaced or rewritten under the same inode. Inode reuse
# after unlink+create is not theoretical — a reviewer reproduced it on the first attempt.
PREFIX_BYTES = 64 * 1024
# Head and tail windows leave the whole middle of a long transcript unguarded. These sample
# it at a constant cost — 8 probes, not a function of file size.
INTERIOR_SAMPLES = 8
INTERIOR_WINDOW = 4 * 1024
# Ceiling on how much may be pulled in for one "did this land?" question. A transcript in this
# corpus reaches 72 MB; an unbounded read of the gap is a memory hazard, not a feature.
MAX_SINCE_BYTES = 16 * 1024 * 1024

OK = "ok"            # everything after the cursor was read and parsed; absence IS proof
PENDING = "pending"  # a record is mid-write; what parsed is real, but absence proves nothing
UNKNOWN = "unknown"  # the cursor cannot be trusted at all; fall back to another signal


class Since:
    """Result of `records_since`: records plus how much they can be trusted."""

    __slots__ = ("records", "status")

    def __init__(self, records, status):
        self.records = tuple(records)
        self.status = status

    @property
    def trusted_absence(self):
        """True only when NOT finding something proves it did not happen.

        PENDING is excluded on purpose: a half-written line may be the very record being
        looked for. UNKNOWN is excluded because nothing here is trustworthy.
        """
        return self.status == OK

    def __repr__(self):
        return f"Since(records={len(self.records)}, status={self.status!r})"

    def __eq__(self, other):
        return (isinstance(other, Since) and self.records == other.records
                and self.status == other.status)


def transcript_path(cwd, session_id):
    """Claude's project dir flattens the absolute launch cwd: EVERY non-alphanumeric char -> '-'.

    Verified against real ~/.claude/projects/ dirs: /home/user/.worktrees/12-feature-branch
    is stored as -home-user--worktrees-12-feature-branch (dot becomes '-', hence the double dash).
    """
    absolute_cwd = os.path.abspath(os.path.expanduser(cwd))
    flattened_cwd = re.sub(r"[^A-Za-z0-9]", "-", absolute_cwd)
    return os.path.join(PROJECTS_DIR, flattened_cwd, f"{session_id}.jsonl")


def read_tail_records(path, max_bytes=TAIL_BYTES):
    """Return valid JSON objects from at most the final ``max_bytes`` of a JSONL file.

    Deliberately permissive — it starts mid-file, so a broken first line is expected. This is
    the model_watchdog's "what model is this session on" reader, NOT the causal API below;
    do not route cursor reads through it.
    """
    if max_bytes <= 0:
        return []
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        start = max(0, f.tell() - max_bytes)
        f.seek(start)
        if start:
            f.readline()  # discard the first partial JSONL record
        data = f.read(max_bytes)
    return _parse(data)


def _parse(data):
    records = []
    for raw_line in data.decode("utf-8", errors="replace").splitlines():
        try:
            record = json.loads(raw_line)
        except ValueError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def _digest_window(fileno, offset, length, digest):
    """Fold `length` bytes at `offset` into `digest`. False if they could not all be read."""
    read = 0
    while read < length:
        try:
            chunk = os.pread(fileno, min(65536, length - read), offset + read)
        except OSError:
            return False
        if not chunk:
            return False  # shrank under us — the caller treats that as untrustworthy
        digest.update(chunk)
        read += len(chunk)
    return True


def _generation_digest(fileno, size):
    """SHA-256 over a bounded sample of the cursor's range, or None if unreadable.

    WHAT THIS DETECTS: the file replaced, truncated, rotated, or rewritten anywhere in the
    head window, the tail window, or any sampled interior window — and any change of length.

    WHAT IT DOES NOT: an in-place rewrite that preserves the total size, both end windows,
    AND every sampled interior window. That is a real residual and it is stated here rather
    than papered over. It is out of the threat model because Claude Code's transcripts are
    append-only — the client writes complete JSONL lines at the end and never seeks back to
    overwrite the middle. A caller that ever points this at a file some process DOES rewrite
    in place must not rely on the digest alone.

    Hashing the whole range would remove the residual and was measured before being rejected:
    822 ms for the 85 MB transcript in this corpus, ~18 s across the fleet for one poll each.
    The sample is constant-cost at any file size.
    """
    if size <= 0:
        return None
    # `size` is deliberately NOT folded in: both calls pass the cursor's size, so the
    # contribution is identical by construction and cannot discriminate anything. Size is
    # already compared directly by the caller, and the window offsets derive from it.
    digest = hashlib.sha256()
    for offset, length in _sample_windows(size):
        if not _digest_window(fileno, offset, length, digest):
            return None
    return digest.hexdigest()


def _sample_windows(size):
    """(offset, length) pairs covering the head, the tail, and INTERIOR_SAMPLES points between.

    The interior points are what stop a size-preserving rewrite of the middle — with only the
    two end windows, everything between them is unguarded, which is the widest part of a long
    transcript. Offsets are derived from `size` alone so the cursor and the later check agree
    without storing them.
    """
    head = min(size, PREFIX_BYTES)
    windows = [(0, head)]
    interior_start, interior_end = head, max(head, size - PREFIX_BYTES)
    span = interior_end - interior_start
    if span > 0:
        for i in range(1, INTERIOR_SAMPLES + 1):
            offset = interior_start + (span * i) // (INTERIOR_SAMPLES + 1)
            length = min(INTERIOR_WINDOW, size - offset)
            if length > 0:
                windows.append((offset, length))
    tail = min(size, PREFIX_BYTES)
    windows.append((size - tail, tail))
    return windows


def cursor(path):
    """An opaque generation token for `path`, or None if it can't be read.

    Taken BEFORE an injection so that everything after it is known-new. An offset is what
    makes freshness a fact instead of a text comparison: the pane-scraping versions of this
    check had to distinguish a fresh refusal from an identical one already on screen, and got
    it wrong twice (#155 rounds 1 and 2). Bytes that did not exist yet cannot be stale.

    `(st_dev, st_ino, size)` alone is NOT enough to name one file generation. A reviewer
    reproduced all three of: inode reused immediately after unlink+create, truncate-then-
    regrow past the old size, and the path being swapped between a `stat()` and a later
    `open()`. So the token also carries a digest of the file's first bytes, and every check
    below runs against a single open descriptor rather than re-resolving the path.
    """
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        return (st.st_dev, st.st_ino, st.st_size, _generation_digest(fd, st.st_size))
    except OSError:
        return None
    finally:
        os.close(fd)


def records_since(path, cur):
    """`Since` describing what was appended to `path` after `cur`.

    UNKNOWN — never an empty OK — when the cursor cannot be trusted: no cursor, an unreadable
    file, a different device/inode (the session was replaced), a file shorter than the cursor
    (truncated or rotated), a changed prefix (rewritten in place), or a complete line that
    does not parse (something happened that this module cannot interpret, and silently
    dropping it would hide the event forever).

    A zero-length cursor is also UNKNOWN once the file grows: there is no prefix to verify
    against, so a replacement that reused the inode would be indistinguishable from an append.
    Falling back costs a poll; misattributing another session's records does not announce
    itself.

    PENDING when the tail ends mid-line. That is ordinary — the session may be writing right
    now — so the parsed records are real, but absence proves nothing until the line lands.
    """
    if not cur or len(cur) != 4:
        return Since((), UNKNOWN)
    dev, ino, size, digest = cur
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return Since((), UNKNOWN)
    try:
        try:
            st = os.fstat(fd)
        except OSError:
            return Since((), UNKNOWN)
        # Identity and generation are both checked against THIS descriptor, so nothing can be
        # swapped in between the check and the read.
        if (st.st_dev, st.st_ino) != (dev, ino) or st.st_size < size:
            return Since((), UNKNOWN)
        if _generation_digest(fd, size) != digest:
            return Since((), UNKNOWN)
        if st.st_size == size:
            return Since((), OK)
        if size == 0:
            return Since((), UNKNOWN)  # nothing was verifiable; see docstring
        length = st.st_size - size
        # A gap this large means the daemon lost track for a long time; reading it would pull
        # tens of megabytes into memory for a question about one injection. Refusing is the
        # honest answer and the caller already has to handle UNKNOWN.
        if length > MAX_SINCE_BYTES:
            return Since((), UNKNOWN)
        data = _pread_all(fd, size, length)
        if data is None:
            return Since((), UNKNOWN)
    finally:
        os.close(fd)
    return _parse_strict(data)


def _pread_all(fd, offset, length):
    """Exactly `length` bytes at `offset`, or None.

    Returning what it managed to read would be worse than failing: `_parse_strict` would
    classify a truncated read as OK, and the caller would take "the record isn't there" as
    proof when the bytes were simply never handed over.
    """
    chunks, read = [], 0
    while read < length:
        try:
            chunk = os.pread(fd, min(1 << 20, length - read), offset + read)
        except OSError:
            return None
        if not chunk:
            return None
        chunks.append(chunk)
        read += len(chunk)
    return b"".join(chunks)


def _parse_strict(data):
    """Parse appended bytes, distinguishing "nothing new" from "something I can't read".

    The permissive `_parse` is wrong here. It drops a malformed line silently, so a real
    transcript's NUL-bearing invalid line becomes an empty OK — the caller then concludes the
    injection did not land when in fact the file recorded something unreadable. A COMPLETE
    line that will not parse is UNKNOWN; only an unterminated trailing line is PENDING.
    """
    pending = not data.endswith(b"\n")
    text = data.decode("utf-8", errors="replace")
    lines = text.split("\n")
    trailing = lines.pop() if pending else (lines.pop() if lines and lines[-1] == "" else "")
    records = []
    for line in lines:
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except ValueError:
            return Since((), UNKNOWN)
        if not isinstance(record, dict):
            return Since((), UNKNOWN)
        records.append(record)
    if pending and trailing.strip():
        return Since(records, PENDING)
    return Since(records, OK)


# ---- record shapes ----

# Written BY the client, not submitted by anyone. `isCompactSummary` is prose about the
# session and freely quotes payloads that were never re-sent; `isMeta` covers injected
# reminders and caveats. Counted on real data: 135, 135 and 943 respectively.
_SYNTHETIC_FLAGS = ("isCompactSummary", "isVisibleInTranscriptOnly", "isMeta")


def _is_synthetic(record):
    return any(record.get(flag) is True for flag in _SYNTHETIC_FLAGS)


def _text_of(record):
    """The record's content flattened to a string, '' if it has none.

    TWO shapes, and missing the second one silently loses every hook refusal. `user` and
    `assistant` records nest content under `message`; `system` records — including
    `subtype: "local_command"`, which is exactly how a PreCompact refusal arrives — put it at
    the TOP LEVEL instead. Verified on a real session transcript, whose three real
    refusals this function returned nothing for until the top-level branch existed.
    """
    content = (record.get("message") or {}).get("content")
    if content is None:
        content = record.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    return ""


def _is_hook_refusal(record):
    """A PreCompact refusal, by STRUCTURE — `system` + `local_command` + top-level content.

    Every one of the 19 genuine refusals in the real corpus has exactly this shape and none
    is user-shaped. Accepting a `user` record here would let ordinary prose forge the event:
    a queued message reading "Earlier I saw: Compaction blocked by PreCompact hook: ..." would
    make the daemon abort a compaction that was in fact proceeding normally — the same
    false-positive this module exists to remove.
    """
    if record.get("type") != "system" or record.get("subtype") != "local_command":
        return False
    content = record.get("content")
    return isinstance(content, str) and "Compaction blocked by PreCompact hook" in content


def compact_events(records):
    """Classify the compaction-relevant records in `records`.

    All from record STRUCTURE rather than from matching prose. Shapes verified against a live
    corpus carrying 19 refusals and 135 compaction summaries:

    - `submitted` — a NON-synthetic `user` record holding `<command-name>/compact</command-name>`
    - `completed` — `isCompactSummary: true`, or the client's own stdout confirmation
    - `refusal`   — `system`/`local_command` carrying the hook's stderr, as its reason text
    """
    submitted = completed = False
    refusal = None
    for record in records:
        if record.get("isCompactSummary"):
            completed = True          # structural flag; trustworthy on any record type
            continue
        if _is_hook_refusal(record):
            refusal = refusal or _refusal_reason(_text_of(record))
            continue
        # ONLY genuine user/system records carry commands and their local output. An
        # `assistant` record with the same words is the model TALKING about a refusal, not one
        # happening — searching transcripts for "Compaction blocked by PreCompact hook"
        # returned 184 hits in one session, nearly all of it this very work discussing the
        # marker. Synthetic records are excluded for the same reason in a different costume.
        if record.get("type") not in ("user", "system") or _is_synthetic(record):
            continue
        text = _text_of(record)
        if not text:
            continue
        if "<command-name>/compact</command-name>" in text:
            submitted = True
        # Measured across 448 real transcripts: 135 occurrences of the client's completion
        # line, ALL of them inside a <local-command-stdout> wrapper, none bare. A separate
        # `"Compacted (ctrl+o" in text` alternative was carried here until a reviewer showed
        # no test could turn it red — there is no input in the corpus that reaches it. The
        # structural `isCompactSummary` flag above is the primary completion signal anyway;
        # this is the secondary one.
        elif "<local-command-stdout>" in text and "Compacted" in text:
            completed = True
    return {"submitted": submitted, "completed": completed, "refusal": refusal}


_TAG_RE = re.compile(r"</?[a-z][\w-]*>")  # <local-command-stderr> and friends
_BLOCK_RE = re.compile(r"Compaction blocked by PreCompact hook:.*", re.S)


def _refusal_reason(text):
    """The hook's refusal as one readable line — its own words, not a generic timeout."""
    match = _BLOCK_RE.search(text)
    if not match:
        return None
    return " ".join(_TAG_RE.sub("", match.group()).split())


def payload_landed(records, payload):
    """True if `payload` was SUBMITTED as a user message in `records`.

    This is the confirmation half of #157 and the only causal receipt available: the record is
    written when the message is submitted, so its presence proves the injection took.

    Synthetic `user` records are rejected first, and that is not a refinement — it is the
    difference between a receipt and a rumour. A compaction summary is prose about the
    session and quotes payloads verbatim: across the real corpus, 94 of 136 summaries satisfy
    a `/compact` probe and 15 satisfy the carry-forward prefix, none of them submissions. One
    such record landing after the cursor while the real injection was swallowed would forge
    exactly the receipt this function exists to provide.

    Compared on squashed text because the transcript stores what was sent while the pane wraps
    what was drawn; only the sent form is authoritative here, but squashing both keeps this
    agnostic to which side gained whitespace.
    """
    needle = _squash(payload)
    if not needle:
        return False
    return any(needle in _squash(_text_of(r)) for r in records
               if r.get("type") == "user" and not _is_synthetic(r))


def _squash(text):
    return re.sub(r"\s+", "", text or "")
