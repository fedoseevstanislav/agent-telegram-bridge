"""#157: the session's own transcript, not the rendered pane, is the daemon's ground truth.

Every shape asserted here was verified against transcripts containing compactions before it
was written down, so these are representations of Claude Code's actual output rather
than a guess at it. That distinction is the whole point of the issue: #133 and #163 were both
built against a synthetic model of the pane that could not contain the dominant production
case, and both shipped broken.
"""

import json
import os

import pytest

from bridge import transcript


def _write(path, records):
    with open(path, "a", encoding="utf-8") as f:
        for record in records:
            f.write(json.dumps(record) + "\n")


def _user(text, **extra):
    return {"type": "user", "message": {"content": text}, **extra}


# ---- the cursor: freshness as a fact, not a text comparison -------------------

def _texts(since):
    return [transcript._text_of(r) for r in since.records]


def test_records_since_returns_only_what_was_appended(tmp_path):
    p = str(tmp_path / "s.jsonl")
    _write(p, [_user("old one"), _user("old two")])
    cur = transcript.cursor(p)
    _write(p, [_user("new one")])
    got = transcript.records_since(p, cur)
    assert _texts(got) == ["new one"]
    assert got.status == transcript.OK
    assert got.trusted_absence is True


def test_nothing_appended_is_empty_and_TRUSTED(tmp_path):
    """Empty-and-OK and unknown are different answers and callers branch on that. Collapsing
    them is how a pane check ends up acting on stale state."""
    p = str(tmp_path / "s.jsonl")
    _write(p, [_user("only")])
    got = transcript.records_since(p, transcript.cursor(p))
    assert got.records == ()
    assert got.status == transcript.OK
    assert got.trusted_absence is True


@pytest.mark.parametrize("break_it,label", [
    ("missing", "the file is gone"),
    ("truncated", "the file is shorter than the cursor"),
    ("replaced", "a different inode — the session was replaced"),
])
def test_an_untrustworthy_cursor_answers_unknown(tmp_path, break_it, label):
    p = str(tmp_path / "s.jsonl")
    _write(p, [_user("a"), _user("b")])
    cur = transcript.cursor(p)
    if break_it == "missing":
        os.remove(p)
    elif break_it == "truncated":
        with open(p, "w"):
            pass
    else:
        # rename-over, not remove-then-create: deleting and recreating can REUSE the inode,
        # which would make this assertion pass or fail by filesystem luck. The reuse case is
        # covered deterministically by its own test below.
        other = str(tmp_path / "replacement.jsonl")
        _write(other, [_user("a"), _user("b"), _user("c")])
        os.rename(other, p)
    got = transcript.records_since(p, cur)
    assert got.status == transcript.UNKNOWN, label
    assert got.trusted_absence is False, label


def test_inode_reuse_after_unlink_is_still_unknown(tmp_path):
    """A reviewer reproduced inode reuse on the FIRST attempt: unlink + create handed back the
    same inode, and an (inode, size) cursor happily returned the replacement's records as
    ours. The prefix digest is what closes it — the new file's first bytes differ."""
    p = str(tmp_path / "s.jsonl")
    _write(p, [_user("original a"), _user("original b")])
    cur = transcript.cursor(p)
    os.remove(p)
    _write(p, [_user("IMPOSTOR a"), _user("IMPOSTOR b"), _user("IMPOSTOR c")])
    if os.stat(p).st_ino != cur[1]:
        pytest.skip("this filesystem did not reuse the inode; nothing to prove here")
    got = transcript.records_since(p, cur)
    assert got.status == transcript.UNKNOWN
    assert "IMPOSTOR c" not in _texts(got)


def test_truncate_then_regrow_past_the_old_size_is_unknown(tmp_path):
    """Defeats a size check outright: the file ends up LONGER than the cursor, on the same
    inode, so every (inode, size) comparison says 'appended' about a different generation."""
    p = str(tmp_path / "s.jsonl")
    _write(p, [_user("original")])
    cur = transcript.cursor(p)
    original_size = os.stat(p).st_size
    with open(p, "w", encoding="utf-8") as f:      # same inode, truncated
        pass
    # The FIRST rewritten record is padded to exactly the old size, so byte `original_size`
    # lands on a clean record boundary. Without this the strict parser returns UNKNOWN for
    # malformed JSON at a mid-record offset and the test passes with the generation guard
    # deleted — which is what it did on round 2.
    filler = _user("x")
    pad = original_size - (len(json.dumps(filler)) + 1)
    assert pad >= 0, "fixture needs the padded record to fit the old size"
    _write(p, [_user("x" * (pad + 1))])
    assert os.stat(p).st_size == original_size, "the rewritten head must align to the cursor"
    _write(p, [_user("rewritten one"), _user("rewritten two"), _user("rewritten three")])
    assert os.stat(p).st_size > cur[2], "fixture must regrow PAST the cursor to be meaningful"
    got = transcript.records_since(p, cur)
    assert got.status == transcript.UNKNOWN, (
        "only the generation digest can catch this: identity matches, the file is longer, "
        "and the cursor offset parses cleanly")
    assert got.records == ()


def test_rewritten_prefix_at_the_same_size_is_unknown(tmp_path):
    """The stat-then-open race in miniature: identity matches and the file has grown, but the
    bytes the cursor was taken over are no longer the bytes on disk."""
    p = str(tmp_path / "s.jsonl")
    _write(p, [_user("aaaa")])
    cur = transcript.cursor(p)
    head = os.stat(p).st_size
    with open(p, "r+b") as f:
        f.seek(0)
        f.write(b"{\"type\":\"user\",\"message\":{\"content\":\"zzzz\"}}".ljust(head - 1) + b"\n")
    _write(p, [_user("appended after the rewrite")])
    got = transcript.records_since(p, cur)
    assert got.status == transcript.UNKNOWN


def test_no_cursor_is_unknown(tmp_path):
    p = str(tmp_path / "s.jsonl")
    _write(p, [_user("a")])
    got = transcript.records_since(p, None)
    assert got.status == transcript.UNKNOWN
    assert got.trusted_absence is False


def test_a_zero_length_cursor_cannot_vouch_for_what_follows(tmp_path):
    """There is no prefix to verify, so an inode-reusing replacement is indistinguishable from
    an append. Falling back costs one poll; misattributing another session's records does not
    announce itself."""
    p = str(tmp_path / "s.jsonl")
    open(p, "w").close()
    cur = transcript.cursor(p)
    _write(p, [_user("whose record is this?")])
    assert transcript.records_since(p, cur).status == transcript.UNKNOWN


def test_a_partial_trailing_line_is_PENDING_not_trusted_absence(tmp_path):
    """The session may be mid-write. What parsed is real, but absence proves nothing yet — the
    half-written line may BE the record we are waiting for. Marking this OK would let a caller
    conclude 'the injection did not land' one millisecond before it did."""
    p = str(tmp_path / "s.jsonl")
    _write(p, [_user("first")])
    cur = transcript.cursor(p)
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(_user("complete")) + "\n")
        f.write('{"type":"user","message":{"content":"half')       # no newline, truncated
    got = transcript.records_since(p, cur)
    assert _texts(got) == ["complete"]
    assert got.status == transcript.PENDING
    assert got.trusted_absence is False
    with open(p, "a", encoding="utf-8") as f:
        f.write('written"}}\n')
    again = transcript.records_since(p, cur)
    assert _texts(again) == ["complete", "halfwritten"]
    assert again.status == transcript.OK


def test_a_malformed_COMPLETE_line_is_unknown_not_trusted_absence(tmp_path):
    """A large transcript contained a newline-terminated, NUL-bearing invalid line. The
    permissive tail parser drops it silently, so the caller sees 'nothing new' and concludes
    the injection failed — when in truth the file recorded something unreadable. A complete
    line that will not parse is the definition of untrustworthy."""
    p = str(tmp_path / "s.jsonl")
    _write(p, [_user("first")])
    cur = transcript.cursor(p)
    with open(p, "ab") as f:
        f.write(b"{definitely-not-json\x00}\n")
    got = transcript.records_since(p, cur)
    assert got.status == transcript.UNKNOWN
    assert got.trusted_absence is False


def test_a_malformed_line_does_not_hide_behind_a_later_valid_one(tmp_path):
    """Returning only the later record would bury the unknown event permanently."""
    p = str(tmp_path / "s.jsonl")
    _write(p, [_user("first")])
    cur = transcript.cursor(p)
    with open(p, "ab") as f:
        f.write(b"{definitely-not-json}\n")
    _write(p, [_user("perfectly fine")])
    got = transcript.records_since(p, cur)
    assert got.status == transcript.UNKNOWN
    assert _texts(got) == []


# ---- compaction, classified by record STRUCTURE ------------------------------

def test_a_submitted_compact_is_recognised():
    records = [_user("<command-name>/compact</command-name>\n<command-message>compact</command-message>")]
    assert transcript.compact_events(records)["submitted"] is True


@pytest.mark.parametrize("record,label", [
    ({"type": "user", "isCompactSummary": True, "message": {"content": "…summary…"}},
     "the isCompactSummary flag"),
    # The real shape, copied from a transcript: the client's line inside its stdout wrapper.
    (_user("<local-command-stdout>Compacted (ctrl+o to see full summary)</local-command-stdout>"),
     "the client's completion line in its wrapper"),
    (_user("<local-command-stdout>Compacted the conversation.</local-command-stdout>"),
     "the wrapper without the ctrl+o phrasing"),
])
def test_completion_is_recognised_from_either_signal(record, label):
    assert transcript.compact_events([record])["completed"] is True, label


def _refusal(reason):
    """The ONLY refusal shape that exists in real data: system / local_command, content at the
    top level. A corpus scan found 19 genuine refusals and all 19 look like this. Round 1 of
    this module invented a `user`-shaped variant and wrote tests around it, which is how the
    forgery hole below survived review."""
    return {"type": "system", "subtype": "local_command", "level": "info",
            "content": f"<local-command-stderr>Compaction blocked by PreCompact hook: "
                       f"{reason}</local-command-stderr>"}


def test_a_hook_refusal_is_returned_with_its_own_reason():
    """#155's whole point: report what the hook said, not a generic timeout. The reason is the
    part that differs between refusals — the constant head is what made a pane-side prefix
    probe collide across different refusals."""
    events = transcript.compact_events([_refusal(
        "[bash /opt/example/hooks/pre-compact-check.sh]: PreCompact blocked: "
        "no new carry-forward comment on #42")])
    assert events["refusal"] is not None
    assert "no new carry-forward comment on #42" in events["refusal"]
    assert "<local-command-stderr>" not in events["refusal"]   # tags stripped
    assert events["completed"] is False


@pytest.mark.parametrize("forgery,label", [
    (_user("Earlier I saw: Compaction blocked by PreCompact hook: [bash /x.sh]: nope"),
     "ordinary prose in a user message"),
    # Content at the TOP level, like a real system record, but typed `user`. This is the one
    # that pins the type check itself: a classifier that keys only on where the content lives
    # would accept it.
    # `subtype` is deliberately the REAL one: without it the subtype check rejects this
    # before record type is ever consulted, so the type clause would be untested — which is
    # exactly what round 2 of this test did.
    ({"type": "user", "subtype": "local_command",
      "content": "<local-command-stderr>Compaction blocked by PreCompact "
                 "hook: [bash /x.sh]: nope</local-command-stderr>"},
     "a user record wearing the system record's shape, subtype and all"),
    # Right type, wrong subtype — `local_command` is what carries hook output.
    ({"type": "system", "subtype": "info",
      "content": "Compaction blocked by PreCompact hook: [bash /x.sh]: nope"},
     "a system record of another subtype"),
])
def test_only_the_measured_refusal_shape_counts(forgery, label):
    """A queued message that merely MENTIONS a past refusal must not abort a compaction that
    is proceeding normally. Substring matching across record types is exactly the
    pane-scraping false positive this module exists to remove, and round 1 reintroduced it.
    Every genuine refusal inspected is system/local_command; none is user-shaped."""
    assert transcript.compact_events([forgery])["refusal"] is None, label


def test_submitted_but_refused_is_distinguishable_from_submitted_and_done():
    """A measured submissions/summaries gap in a transcript can be a refusal. Record
    STRUCTURE separates them — no timing window, no prose matching."""
    submitted = _user("<command-name>/compact</command-name>")
    refused = transcript.compact_events([submitted, _refusal("[bash /x.sh]: nope")])
    done = transcript.compact_events([submitted,
                                      {"type": "user", "isCompactSummary": True,
                                       "message": {"content": "…"}}])
    assert (refused["submitted"], refused["completed"], bool(refused["refusal"])) == (True, False, True)
    assert (done["submitted"], done["completed"], bool(done["refusal"])) == (True, True, False)


def test_the_system_record_shape_is_recognised():
    """A representative record with the exact structure Claude Code writes.

    A `system` record puts its content at the TOP LEVEL, not under `message` — unlike `user`
    and `assistant`. The first version of this module only looked under `message`, so it
    returned zero refusals for a transcript containing refusals, and the fixtures
    (which used the `user` shape) all passed. Caught only by running the classifier over real
    data. That is the same failure mode as #133 and #163: a synthetic model of the input that
    could not contain the production case.
    """
    record = {
        "type": "system",
        "subtype": "local_command",
        "level": "info",
        "content": "<local-command-stderr>Compaction blocked by PreCompact hook: "
                   "[bash /opt/example/hooks/pre-compact-check.sh]: PreCompact blocked: "
                   "no new carry-forward comment on #42 (example-org/ops) since "
                   "session start on this issue.</local-command-stderr>",
    }
    events = transcript.compact_events([record])
    assert events["refusal"] is not None
    assert "no new carry-forward comment on #42" in events["refusal"]
    assert events["completed"] is False


def test_a_displayed_refusal_cannot_forge_one():
    """The failure #155 spent three rounds on: a pane can *show* an old refusal, so the daemon
    had to prove freshness by diffing captures. Here an assistant merely quoting the refusal is
    not a refusal, because it is not a user/system command record."""
    quoted = {"type": "assistant", "message": {"content": [
        {"type": "text", "text": "Earlier I saw: Compaction blocked by PreCompact hook: [bash /x.sh]: nope"}]}}
    assert transcript.compact_events([quoted])["refusal"] is None


# ---- the confirmation half ---------------------------------------------------

def test_a_submitted_payload_is_confirmed():
    payload = "[tg-bridge carry-forward] write your state to /tmp/cf.md " * 30
    assert transcript.payload_landed([_user(payload)], payload) is True


def test_confirmation_survives_the_wrapping_the_pane_adds():
    payload = "please drain topic 55 and act on it"
    wrapped = _user("please drain topic 55\n  and act on it")
    assert transcript.payload_landed([wrapped], payload) is True


def test_an_unsubmitted_payload_is_not_confirmed():
    """The measured fact this whole design rests on: text sitting in a composer writes NOTHING.
    In the observed failure a briefing sat unsubmitted and the transcript gained no record until
    Enter. So absence is meaningful, and it is what the #163 outage lacked: for an extended
    period nothing could distinguish a correctly withheld Enter from a wrongly withheld one."""
    assert transcript.payload_landed([_user("something else entirely")], "our payload") is False


def test_only_user_records_confirm_a_submission():
    """An assistant echoing the text back is not evidence that the user record was written."""
    echoed = {"type": "assistant", "message": {"content": [
        {"type": "text", "text": "You asked me to drain topic 55"}]}}
    assert transcript.payload_landed([echoed], "drain topic 55") is False


# ---- path resolution ---------------------------------------------------------

def test_transcript_path_flattens_every_non_alphanumeric():
    got = transcript.transcript_path("/home/user/claude-telegram-bridge/.worktrees/126-tg", "abc")
    assert got.endswith("-home-user-claude-telegram-bridge--worktrees-126-tg/abc.jsonl")


# ---- synthetic records are not receipts --------------------------------------
#
# The hole Codex found with my own transcript. Claude writes compaction summaries as
# `type: "user", isCompactSummary: true` — prose ABOUT the session, quoting payloads verbatim.
# Many sampled summaries satisfy a `/compact` probe and some satisfy the carry-forward prefix;
# none of them is a submission. One landing after the cursor while the
# real injection was swallowed forges precisely the receipt this module exists to provide.

# Shape copied from a real compaction summary that discusses /compact and the
# carry-forward marker at length — i.e. the worst case for a naive text probe.
_REAL_SUMMARY = {
    "type": "user",
    "isCompactSummary": True,
    "isVisibleInTranscriptOnly": True,
    "message": {"content":
        "This session is being continued from a previous conversation... The user "
        "submitted <command-name>/compact</command-name> and the daemon then wrote the "
        "[tg-bridge carry-forward] marker file before compaction completed."},
}


@pytest.mark.parametrize("payload", [
    "/compact",
    "<command-name>/compact</command-name>",
    "[tg-bridge carry-forward]",
])
def test_a_compaction_summary_is_never_a_receipt(payload):
    assert transcript.payload_landed([_REAL_SUMMARY], payload) is False


@pytest.mark.parametrize("flag", ["isCompactSummary", "isVisibleInTranscriptOnly", "isMeta"])
def test_every_synthetic_user_shape_is_rejected(flag):
    """All three counted in real data: 135, 135 and 943 occurrences. They are written BY the
    client, so none of them proves anything was submitted."""
    synthetic = _user("please drain topic 55 and act on it", **{flag: True})
    assert transcript.payload_landed([synthetic], "drain topic 55") is False


@pytest.mark.parametrize("flag", ["isMeta", "isVisibleInTranscriptOnly"])
def test_a_synthetic_record_cannot_forge_a_compact_submission(flag):
    """The same hole on the other predicate: injected context quoting the command must not
    count as the command being run again.

    `isCompactSummary` is deliberately NOT one of the parameters — that flag is consumed by
    the completion branch before the filter is reached, so using it here would test nothing.
    That is precisely how the first version of this test passed against a source with the
    filter deleted.
    """
    quoting = _user("I ran <command-name>/compact</command-name> earlier", **{flag: True})
    assert transcript.compact_events([quoting])["submitted"] is False


def test_a_genuine_submission_next_to_a_summary_still_counts():
    """The filter must not swallow the real thing — the whole point is telling them apart."""
    real = _user("<command-name>/compact</command-name>")
    events = transcript.compact_events([_REAL_SUMMARY, real])
    assert events["submitted"] is True
    assert events["completed"] is True   # the summary itself proves the compaction happened


# ---- round-2 findings: reads that lie ----------------------------------------

def test_a_short_read_is_unknown_not_trusted_absence(tmp_path, monkeypatch):
    """The worst possible failure of this module: hand back FEWER bytes than were asked for,
    parse them cleanly, and report OK. The caller then reads "the record isn't there" as proof
    when the bytes were simply never delivered. Returning partial data is strictly worse than
    failing."""
    p = str(tmp_path / "s.jsonl")
    _write(p, [_user("first")])
    cur = transcript.cursor(p)
    _write(p, [_user("the record we are waiting for")])

    real_pread = os.pread
    cursor_offset = cur[2]

    def truncating(fd, n, off):
        # The generation windows all sit at or before the cursor; let those through. The read
        # of the appended bytes hits EOF one byte in, which is exactly what a file shrinking
        # under a concurrent writer looks like.
        if off < cursor_offset:
            return real_pread(fd, n, off)
        return real_pread(fd, 1, off) if off == cursor_offset else b""

    monkeypatch.setattr(os, "pread", truncating)
    got = transcript.records_since(p, cur)
    assert got.status == transcript.UNKNOWN
    assert got.trusted_absence is False


def test_an_unreadable_generation_window_is_unknown_not_an_exception(tmp_path, monkeypatch):
    """`records_since` must answer, not raise. An OSError escaping it would propagate into the
    daemon's poll loop from a routine 'did that land?' check."""
    p = str(tmp_path / "s.jsonl")
    _write(p, [_user("first")])
    cur = transcript.cursor(p)
    _write(p, [_user("second")])

    def boom(fd, n, off):
        raise OSError(5, "I/O error")

    monkeypatch.setattr(os, "pread", boom)
    got = transcript.records_since(p, cur)          # must not raise
    assert got.status == transcript.UNKNOWN


def test_an_enormous_gap_is_refused_rather_than_read(tmp_path, monkeypatch):
    """A transcript can be large. Pulling an unbounded gap into memory to answer a question
    about one injection is a hazard, and UNKNOWN is already a state every caller must
    handle."""
    monkeypatch.setattr(transcript, "MAX_SINCE_BYTES", 200)
    p = str(tmp_path / "s.jsonl")
    _write(p, [_user("first")])
    cur = transcript.cursor(p)
    _write(p, [_user("x" * 500)])
    got = transcript.records_since(p, cur)
    assert got.status == transcript.UNKNOWN
    assert got.records == ()


def test_a_gap_under_the_ceiling_is_still_read(tmp_path, monkeypatch):
    """The ceiling must not swallow ordinary use."""
    monkeypatch.setattr(transcript, "MAX_SINCE_BYTES", 4096)
    p = str(tmp_path / "s.jsonl")
    _write(p, [_user("first")])
    cur = transcript.cursor(p)
    _write(p, [_user("ordinary")])
    got = transcript.records_since(p, cur)
    assert got.status == transcript.OK
    assert _texts(got) == ["ordinary"]


def test_the_generation_digest_covers_the_tail_not_only_the_head(tmp_path, monkeypatch):
    """Hashing only the head lets a rewrite that reproduces the opening bytes slip through —
    and a transcript's first records are the most predictable bytes in the file. The window
    ending exactly at the cursor is what makes that forgery need to match at both ends.

    PREFIX_BYTES is shrunk here on purpose. With the real 64 KiB value the head window already
    spans any test-sized file, so the tail window could be deleted and this test would still
    pass — which is precisely what it did before this fixture existed. The gap only opens on
    files LARGER than the prefix, i.e. every real transcript.
    """
    monkeypatch.setattr(transcript, "PREFIX_BYTES", 64)
    # Interior sampling is disabled here on purpose. With it on, the 4 KB interior windows
    # span this whole small fixture and catch the change even with the tail window deleted —
    # so the test passed for the wrong reason and proved nothing about the tail. Interior
    # sampling has its own tests; this one must isolate the tail.
    monkeypatch.setattr(transcript, "INTERIOR_SAMPLES", 0)
    p = str(tmp_path / "s.jsonl")
    _write(p, [_user("identical opening record " + "h" * 200)])
    head_size = os.stat(p).st_size
    assert head_size > 64, "the head must exceed the shrunk prefix for this to prove anything"
    _write(p, [_user("tail record ALPHA")])
    cur = transcript.cursor(p)
    size = os.stat(p).st_size
    # Same inode, same opening bytes, same total size — only the bytes just before the cursor
    # differ. A head-only digest cannot tell this from an append.
    with open(p, "r+b") as f:
        f.seek(head_size)
        f.truncate()
    _write(p, [_user("tail record OMEGA")])
    assert os.stat(p).st_size == size, "fixture must keep the size identical"
    _write(p, [_user("appended after the swap")])
    got = transcript.records_since(p, cur)
    assert got.status == transcript.UNKNOWN


def _big_file(path, rows=24, width=20000):
    recs = [_user(f"row {i} " + "f" * width) for i in range(rows)]
    _write(path, recs)
    return open(path, "rb").read()


def test_a_size_preserving_middle_rewrite_inside_a_sampled_window_is_caught(tmp_path):
    """Codex was building exactly this when its run was cut off: keep the size identical, keep
    the head and tail windows byte-identical, and rewrite only the middle. Head+tail alone
    cannot see it, and the middle is the largest part of a long transcript."""
    p = str(tmp_path / "s.jsonl")
    original = _big_file(p)
    cur = transcript.cursor(p)
    ino = os.stat(p).st_ino
    size = len(original)

    # Land the change inside a window the sampler actually reads.
    windows = transcript._sample_windows(size)
    interior = [w for w in windows if w[0] > transcript.PREFIX_BYTES
                and w[0] + w[1] < size - transcript.PREFIX_BYTES]
    assert interior, "fixture needs a genuine interior window"
    offset = interior[len(interior) // 2][0] + 16
    with open(p, "r+b") as f:
        f.seek(offset)
        f.write(b"IMPOSTOR")                      # same length, same everything else

    rewritten = open(p, "rb").read()
    assert len(rewritten) == size, "fixture must preserve the size"
    assert rewritten[:transcript.PREFIX_BYTES] == original[:transcript.PREFIX_BYTES]
    assert rewritten[-transcript.PREFIX_BYTES:] == original[-transcript.PREFIX_BYTES:]
    assert rewritten != original
    assert os.stat(p).st_ino == ino, "fixture must keep the same inode"

    _write(p, [_user("appended after the middle rewrite")])
    got = transcript.records_since(p, cur)
    assert got.status == transcript.UNKNOWN
    assert got.records == ()


def test_the_documented_residual_is_real_and_is_not_pretended_away(tmp_path):
    """The limit stated in `_generation_digest`, pinned so it cannot be quietly forgotten or
    quietly claimed fixed: a rewrite that preserves the size, both end windows AND misses
    every sampled interior window is NOT detected.

    This is deliberate, not an oversight. Closing it means hashing the full range, which was
    measured on the order of a second for one large transcript and exceeded the useful poll
    budget across a fleet. It is out of the threat model because Claude Code appends complete JSONL lines
    and never seeks back to overwrite the middle. If that ever stops being true, this test is
    the one that should start failing on purpose.
    """
    p = str(tmp_path / "s.jsonl")
    original = _big_file(p)
    cur = transcript.cursor(p)
    size = len(original)

    covered = set()
    for off, length in transcript._sample_windows(size):
        covered.update(range(off, off + length))
    gap = next(i for i in range(transcript.PREFIX_BYTES, size - transcript.PREFIX_BYTES)
               if not covered & set(range(i, i + 8)))
    with open(p, "r+b") as f:
        f.seek(gap)
        f.write(b"IMPOSTOR")
    assert len(open(p, "rb").read()) == size

    _write(p, [_user("appended after an unsampled rewrite")])
    got = transcript.records_since(p, cur)
    assert got.status == transcript.OK, (
        "if this now returns UNKNOWN the guarantee got STRONGER — update the docstring in "
        "_generation_digest and this test together, do not just delete the assertion")


def test_the_sample_is_constant_cost_not_proportional_to_size():
    """The whole reason the residual exists: hashing a large transcript costs on the order of
    a second. If the window count grew with size that trade would be lost."""
    small = transcript._sample_windows(10 * 1024)
    huge = transcript._sample_windows(85 * 1024 * 1024)
    assert len(huge) <= transcript.INTERIOR_SAMPLES + 2
    assert len(small) <= transcript.INTERIOR_SAMPLES + 2
    assert sum(length for _o, length in huge) <= (
        2 * transcript.PREFIX_BYTES + transcript.INTERIOR_SAMPLES * transcript.INTERIOR_WINDOW)


def test_sample_windows_never_read_past_the_cursor(tmp_path):
    """Every window must lie inside [0, size); one that ran past it would hash bytes written
    AFTER the cursor and so change on an ordinary append, making every check UNKNOWN."""
    for size in (1, 100, 4096, 64 * 1024, 200 * 1024, 85 * 1024 * 1024):
        for offset, length in transcript._sample_windows(size):
            assert offset >= 0
            assert length > 0
            assert offset + length <= size, f"window {offset}+{length} exceeds size {size}"
