"""`tg-bridge send --file` posts a file into the topic AS THE BOT (#210).

Before this, a session with a file deliverable had two bad options: `tg send-file me`, which
lands in Saved Messages rather than the topic, or a user-account Telethon call, which posts
as the owner themselves, echoes straight back into the session's own inbox, and never reaches the
outbox ledger.
"""

import hashlib
import json
import os
import sys

import pytest

from bridge import cli, common

CFG = {"bot_token": "123:AA", "chat_id": -100}
TOPIC = 33


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setattr(common, "STATE_DIR", str(tmp_path))
    monkeypatch.setattr(cli, "read_registry", lambda: {str(TOPIC): {"icon": "🦊"}})
    return tmp_path


@pytest.fixture
def sent(monkeypatch):
    """Capture uploads instead of performing them."""
    calls = []

    def fake_upload(token, method, params, field, filename, payload, timeout=300):
        calls.append({"method": method, "params": dict(params), "field": field,
                      "path": filename, "payload": payload})
        return {"message_id": 1000 + len(calls), "chat": {"id": -100}, "document": {}}

    monkeypatch.setattr(common, "api_upload", fake_upload)
    return calls


def _outbox(state):
    path = state / "topics" / str(TOPIC) / "outbox.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def _file(tmp_path, name="report.html", body=b"<h1>hi</h1>"):
    path = tmp_path / name
    path.write_bytes(body)
    return str(path)


# ---- C1: it posts as the bot, into the topic --------------------------------------------

def test_a_file_goes_to_the_topic_as_a_document(state, sent, tmp_path):
    cli.send_files(CFG, TOPIC, [_file(tmp_path)])
    assert len(sent) == 1
    assert sent[0]["method"] == "sendDocument"
    assert sent[0]["params"]["message_thread_id"] == TOPIC
    assert sent[0]["params"]["chat_id"] == -100


def test_an_image_goes_as_an_inline_photo(state, sent, tmp_path):
    cli.send_files(CFG, TOPIC, [_file(tmp_path, "shot.png", b"\x89PNG\r\n")])
    assert sent[0]["method"] == "sendPhoto"
    assert sent[0]["field"] == "photo"


def test_as_document_overrides_the_photo_default(state, sent, tmp_path):
    cli.send_files(CFG, TOPIC, [_file(tmp_path, "shot.png", b"\x89PNG\r\n")], as_document=True)
    assert sent[0]["method"] == "sendDocument"


def test_a_large_image_falls_back_to_a_document_instead_of_failing(state, sent, tmp_path, monkeypatch):
    # Telegram rejects as a photo what it accepts as a document. A big screenshot arriving as
    # a file beats a confusing error.
    monkeypatch.setattr(common, "PHOTO_LIMIT", 4)
    cli.send_files(CFG, TOPIC, [_file(tmp_path, "big.png", b"\x89PNG\r\n" * 10)])
    assert sent[0]["method"] == "sendDocument"


def test_the_caption_carries_the_topic_icon_and_rides_only_the_first_file(state, sent, tmp_path):
    paths = [_file(tmp_path, "a.txt", b"a"), _file(tmp_path, "b.txt", b"b")]
    cli.send_files(CFG, TOPIC, paths, caption="here you go")
    assert "🦊" in sent[0]["params"]["caption"]
    assert "caption" not in sent[1]["params"]


def test_an_overlong_caption_is_sent_as_its_own_message_not_truncated(state, sent, tmp_path,
                                                                     monkeypatch):
    texts = []
    monkeypatch.setattr(cli, "send_text", lambda cfg, tid, text: texts.append(text))
    long_caption = "x" * (common.CAPTION_LIMIT + 1)
    cli.send_files(CFG, TOPIC, [_file(tmp_path)], caption=long_caption)
    assert texts == [long_caption]                     # whole thing, nothing lost
    assert "caption" not in sent[0]["params"]


# ---- C2: the outbox ledger ---------------------------------------------------------------

def test_each_file_gets_one_outbox_record_with_its_hash(state, sent, tmp_path):
    body = b"<h1>hi</h1>"
    path = _file(tmp_path, "report.html", body)
    cli.send_files(CFG, TOPIC, [path])
    records = _outbox(state)
    assert len(records) == 1
    assert records[0]["kind"] == "file"
    assert records[0]["message_id"] == 1001
    assert records[0]["path"] == path
    assert records[0]["size"] == len(body)
    assert records[0]["content_sha256"] == hashlib.sha256(body).hexdigest()


def test_a_journal_failure_does_not_look_like_a_send_failure(state, sent, tmp_path, monkeypatch):
    # The file is already in the topic by then; raising here would tell the caller to resend.
    monkeypatch.setattr(cli, "state_path", lambda *p: "/proc/nonexistent/outbox.jsonl")
    cli.send_files(CFG, TOPIC, [_file(tmp_path)])       # must not raise
    assert len(sent) == 1


# ---- validation: nothing is posted unless everything can be ------------------------------

def test_one_bad_path_in_a_batch_posts_nothing(state, sent, tmp_path):
    good = _file(tmp_path, "good.txt", b"ok")
    with pytest.raises(RuntimeError):
        cli.send_files(CFG, TOPIC, [good, "/nonexistent/typo.txt"])
    assert sent == []


@pytest.mark.parametrize("bad,reason", [
    ("relative/path.txt", "absolute"),
    ("/nonexistent/nope.txt", "no such file"),
])
def test_unusable_paths_are_refused_with_a_reason(bad, reason, tmp_path):
    with pytest.raises(RuntimeError, match=reason):
        common.file_send_plan(bad)


def test_a_directory_is_not_a_file(tmp_path):
    with pytest.raises(RuntimeError, match="not a regular file"):
        common.file_send_plan(str(tmp_path))


def test_an_empty_file_is_refused(tmp_path):
    path = tmp_path / "empty.txt"
    path.write_bytes(b"")
    with pytest.raises(RuntimeError, match="empty"):
        common.file_send_plan(str(path))


# ---- C5: the size cap is enforced before any network call --------------------------------

def test_an_oversized_file_fails_before_it_is_read(tmp_path, monkeypatch):
    path = tmp_path / "huge.bin"
    path.write_bytes(b"0" * 64)
    monkeypatch.setattr(common, "DOCUMENT_LIMIT", 8)
    opened = []
    real_open = open
    monkeypatch.setattr("builtins.open", lambda *a, **k: opened.append(a) or real_open(*a, **k))
    with pytest.raises(RuntimeError, match="exceeds the Bot API limit"):
        common.send_file("t", -100, str(path))
    assert not any(str(path) in str(call) for call in opened)   # never read into memory


# ---- multipart encoding ------------------------------------------------------------------

def test_the_payload_survives_encoding_byte_for_byte(tmp_path):
    payload = bytes(range(256))                    # every byte value, including CR, LF, NUL
    content_type, body = common._multipart({"chat_id": 1}, "document", "b.bin", payload)
    assert "boundary=" in content_type
    boundary = content_type.split("boundary=")[1]
    assert payload in body
    assert body.endswith(f"\r\n--{boundary}--\r\n".encode())


def test_a_filename_cannot_inject_headers(tmp_path):
    _type, body = common._multipart({}, "document", 'ev"il\r\nX-Injected: 1.txt', b"x")
    # The property is that no NEW header line is created — the text may survive as literal
    # characters inside the quoted filename, which is harmless.
    assert b"\r\nX-Injected" not in body
    assert b'filename="ev_il__X-Injected: 1.txt"' in body
    assert body.count(b"Content-Disposition") == 1


def test_the_boundary_is_not_reused_between_calls():
    first, _ = common._multipart({}, "document", "a", b"a")
    second, _ = common._multipart({}, "document", "a", b"a")
    assert first != second


# ---- A1 / C3: the bot sends, so nothing echoes back --------------------------------------

def test_the_upload_uses_the_bot_token_only(state, sent, tmp_path, monkeypatch):
    captured = {}
    monkeypatch.setattr(common, "api_upload",
                        lambda token, *a, **k: captured.setdefault("token", token) or
                        {"message_id": 1, "chat": {"id": -100}})
    cli.send_files(CFG, TOPIC, [_file(tmp_path)])
    assert captured["token"] == CFG["bot_token"]


def test_a_bot_document_is_never_written_to_the_inbox(state, monkeypatch):
    # The user-account workaround was echoed back BECAUSE it came from the owner. handle_message
    # drops anything from a bot before it can reach the inbox.
    from bridge import daemon

    written = []
    monkeypatch.setattr(daemon, "append_jsonl", lambda *a, **k: written.append(a))
    daemon.handle_message({"chat_id": -100, "owner_id": 7, "bot_token": "1:x"},
                          {"chat": {"id": -100}, "message_thread_id": TOPIC,
                           "from": {"id": 999, "is_bot": True},
                           "document": {"file_id": "x", "file_name": "report.html"}})
    assert written == []


# ---- #211 review: the three blocking failures, each with a regression test ---------------

def test_a_swapped_symlink_cannot_change_what_is_uploaded(state, sent, tmp_path,
                                                          monkeypatch):
    """Validation, upload and hashing must share ONE file identity.

    Reproduced by the reviewer: `file_send_plan` approved an eight-byte harmless file, the
    symlink was repointed, and the upload sent the replacement bytes while the ledger recorded
    the pre-swap size beside the post-swap hash.
    """
    real = tmp_path / "approved.txt"
    real.write_bytes(b"approved")
    evil = tmp_path / "evil.txt"
    evil.write_bytes(b"replaced-with-something-longer")
    link = tmp_path / "link.txt"
    link.symlink_to(real)

    # Swap INSIDE the window — between the descriptor being inspected and the bytes being
    # read. Repointing it afterwards would prove nothing, which is how the first version of
    # this test passed against the very defect it was named for.
    real_fstat = os.fstat

    def swap_then_stat(fd):
        info = real_fstat(fd)
        if link.is_symlink() and link.resolve() == real.resolve():
            link.unlink()
            link.symlink_to(evil)
        return info

    monkeypatch.setattr(os, "fstat", swap_then_stat)
    method, field, payload, digest = common.read_file_for_upload(str(link))

    assert payload == b"approved"               # the bytes come from the open fd, not the name
    assert digest == hashlib.sha256(b"approved").hexdigest()


def test_the_hash_describes_the_bytes_that_were_uploaded(state, sent, tmp_path):
    path = _file(tmp_path, "report.html", b"original")
    delivery = common.send_file("t", -100, path)
    with open(path, "wb") as handle:            # changed after the upload
        handle.write(b"something else entirely")
    cli.append_outbox_file_record(TOPIC, delivery)
    record = _outbox(state)[-1]
    assert record["content_sha256"] == hashlib.sha256(b"original").hexdigest()
    assert record["size"] == len(b"original")


def test_a_fifo_is_refused_rather_than_hanging(tmp_path):
    """The regression here is a HANG, so the test needs its own deadline.

    Without O_NONBLOCK, `os.open` on a FIFO with no writer blocks forever. A test that detects
    that by never returning turns a regression into a stuck CI job rather than a red one — the
    round-2 review had to impose an external 3-second timeout to get a verdict out of it. The
    alarm below makes the failure bounded and self-describing.
    """
    import signal

    fifo = tmp_path / "pipe"
    os.mkfifo(fifo)

    def _too_slow(_signum, _frame):
        raise AssertionError("read_file_for_upload blocked on a FIFO — O_NONBLOCK is missing")

    previous = signal.signal(signal.SIGALRM, _too_slow)
    signal.setitimer(signal.ITIMER_REAL, 3)
    try:
        with pytest.raises(RuntimeError, match="not a regular file"):
            common.read_file_for_upload(str(fifo))
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def test_a_payload_containing_the_delimiter_still_round_trips(monkeypatch):
    """A crafted payload used to split the message into two parts, so the upload silently
    became something other than the file."""
    # Force the FIRST boundary to be one the payload already contains. A fixed fake delimiter
    # can never collide with a random one, so the earlier version of this test never
    # exercised the loop at all — it passed with the loop deleted.
    colliding, safe = b"\x11" * 16, b"\x22" * 16
    draws = iter([colliding, safe, safe, safe])
    monkeypatch.setattr(os, "urandom", lambda n: next(draws))

    payload = b"before--" + b"----tg-bridge-" + colliding.hex().encode() + b"\r\nafter"
    content_type, body = common._multipart({"chat_id": 1}, "document", "b.bin", payload)

    assert safe.hex() in content_type           # it regenerated past the collision
    parsed = _parse_multipart(content_type, bytes(body))
    assert len(parsed) == 2                     # chat_id + the file, not three
    assert parsed[-1] == payload


def _parse_multipart(content_type, body):
    """Parse with the stdlib email parser rather than our own assumptions."""
    import email

    message = email.message_from_bytes(
        b"MIME-Version: 1.0\r\nContent-Type: " + content_type.encode() + b"\r\n\r\n" + body)
    return [part.get_payload(decode=True) for part in message.get_payload()]


def test_every_byte_value_round_trips_through_a_real_parser():
    """General corruption coverage — NOT proof that boundary collisions are handled.

    Said explicitly because the round-2 review measured it: this test still passes with the
    whole boundary-regeneration change reverted, because a random payload does not contain the
    randomly chosen delimiter. It catches encoder corruption (a one-byte truncation mutant does
    fail it). Collision avoidance is proved by
    `test_a_payload_containing_the_delimiter_still_round_trips`, which constructs the collision.
    """
    payload = bytes(range(256)) * 8
    content_type, body = common._multipart({"chat_id": 1}, "document", "b.bin", payload)
    assert _parse_multipart(content_type, bytes(body))[-1] == payload


def test_a_bad_path_does_not_consume_a_piped_caption(tmp_path, monkeypatch, capsys):
    """A typo used to return NOT SENT having already eaten the caller's only copy of stdin."""
    import argparse
    import io

    monkeypatch.setattr(cli, "resolve_topic", lambda args: TOPIC)
    monkeypatch.setattr(cli, "require_own_topic", lambda *a: None)
    stdin = io.StringIO("the caption nobody wants to lose")
    monkeypatch.setattr(sys, "stdin", stdin)
    args = argparse.Namespace(text="-", topic=TOPIC, files=["/nonexistent/typo.txt"],
                              as_document=False, force=False, json=False)
    with pytest.raises(SystemExit, match="NOT SENT"):
        cli.cmd_send(CFG, args)
    assert stdin.read() == "the caption nobody wants to lose"   # still there


def test_an_ambiguous_upload_is_journaled_as_possibly_delivered(state, tmp_path, monkeypatch):
    """Text mode journals a lost ACK; a file must too, or there is no evidence to resend from."""
    def timeout(*a, **k):
        raise common.PossiblyDelivered("sendDocument timed out after connecting")

    monkeypatch.setattr(common, "api_upload", timeout)
    with pytest.raises(common.PossiblyDelivered):
        cli.send_files(CFG, TOPIC, [_file(tmp_path, "report.html", b"body")])
    # ONE record, not "the last of however many". Written as an unpacking assignment because
    # the round-2 review's duplicate-append mutant survived `[-1]` with every assertion below
    # still passing: a doubly-journaled ambiguous upload reads as two separate lost ACKs and
    # invites two resends of the same file.
    record, = _outbox(state)
    assert record["kind"] == "file"
    assert record["delivery"] == "possibly_delivered"
    assert record["message_id"] is None
    assert record["content_sha256"] == hashlib.sha256(b"body").hexdigest()
