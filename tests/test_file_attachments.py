"""Non-image attachments reach the session instead of vanishing (#150).

The owner sent a `.md` file to a session; the session never received it and nothing told either
of them. `extract_image` accepts a `document` only with an `image/` mime type, so anything
else fell through to `if not text: return` — no inbox record, no nudge, no trace. Silent
loss is the worst shape for this: the sender sees their file posted in Telegram and assumes
it landed.

The filename is the security-relevant part of the fix. `document.file_name` is arbitrary
text from the payload, so it is reduced to a safe basename before it touches the filesystem.
"""

import json
import os

import pytest

from bridge import daemon


OWNER = 4242001


def _msg(message_id=7001, thread_id=4242, **extra):
    return {"message_id": message_id, "message_thread_id": thread_id,
            "chat": {"id": -100},
            "from": {"id": OWNER, "first_name": "Владелец"}, **extra}


def _doc(name="notes.md", mime="text/markdown", file_id="FILE-1"):
    return {"document": {"file_id": file_id, "file_name": name, "mime_type": mime}}


@pytest.fixture
def bridge(tmp_path, monkeypatch):
    """Daemon wired to a tmp state dir, with Telegram download stubbed."""
    monkeypatch.setattr(daemon, "state_path",
                        lambda *parts: _mkpath(tmp_path, parts))
    monkeypatch.setattr(daemon, "read_registry",
                        lambda: {"4242": {"name": "target", "pane": "%1"}})
    monkeypatch.setattr(daemon, "maybe_auto_revive", lambda *a, **k: None)
    monkeypatch.setattr(daemon, "schedule_nudge", lambda *a, **k: None)
    monkeypatch.setattr(daemon, "log", lambda *a, **k: None)
    downloaded = []

    def download(_token, file_id, dest):
        downloaded.append((file_id, dest))
        with open(dest, "wb") as f:
            f.write(b"# contents\n")
        return dest

    monkeypatch.setattr(daemon, "download_file", download)
    monkeypatch.setattr(daemon, "carry_forward_active", lambda *a, **k: False)
    return {"cfg": {"bot_token": "t", "chat_id": -100, "owner_id": OWNER},
            "tmp": tmp_path, "downloaded": downloaded}


def _mkpath(tmp_path, parts):
    path = tmp_path.joinpath(*parts)
    path.parent.mkdir(parents=True, exist_ok=True)
    return str(path)


def _records(tmp_path, thread_id=4242):
    inbox = tmp_path / "topics" / str(thread_id) / "inbox.jsonl"
    if not inbox.exists():
        return []
    return [json.loads(line) for line in inbox.read_text().splitlines() if line.strip()]


def test_a_markdown_file_reaches_the_session(bridge):
    daemon.handle_message(bridge["cfg"], _msg(**_doc()))

    record, = _records(bridge["tmp"])
    assert record["kind"] == "file"
    assert "[File attached" in record["text"]
    # The path must be in the record — it is the only way the agent can open the file.
    path = record["text"].split(": ", 1)[1].rstrip("]")
    assert os.path.exists(path)
    assert path.endswith("7001-notes.md")


def test_the_caption_survives_alongside_the_path(bridge):
    daemon.handle_message(bridge["cfg"], _msg(caption="review this", **_doc()))

    record, = _records(bridge["tmp"])
    assert "review this" in record["text"]
    assert "[File attached" in record["text"]


@pytest.mark.parametrize("evil,reason", [
    ("../../../.ssh/authorized_keys", "traversal"),
    ("/etc/passwd", "absolute path"),
    ("..", "parent ref"),
    ("....//....//x.md", "doubled traversal"),
    ("sub/dir/notes.md", "nested path"),
    ("..\\..\\windows.md", "backslash traversal"),
])
def test_a_hostile_filename_cannot_escape_the_media_directory(bridge, evil, reason):
    """`document.file_name` is arbitrary text from the payload, not a trusted name."""
    daemon.handle_message(bridge["cfg"], _msg(**_doc(name=evil)))

    _file_id, dest = bridge["downloaded"][-1]
    media = str(bridge["tmp"] / "topics" / "4242" / "media")
    assert os.path.dirname(os.path.realpath(dest)) == os.path.realpath(media), reason
    assert ".." not in os.path.basename(dest)


def test_a_symlink_at_the_destination_is_replaced_not_followed(bridge, tmp_path, monkeypatch):
    """A link planted at the destination must not redirect the write through it.

    The containment check below resolves the media directory itself, so it would pass even
    if the write escaped this way — hence a separate test that asserts the outside file is
    untouched, byte for byte.
    """
    from bridge import common
    monkeypatch.setattr(daemon, "download_file", common.download_file)
    monkeypatch.setattr(common, "api", lambda *a, **k: {"file_path": "x"})

    class FakeResp:
        def read(self): return b"ATTACKER CONTENT"
        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setattr(common.urllib.request, "urlopen", lambda *a, **k: FakeResp())

    outside = tmp_path / "outside.txt"
    outside.write_text("PRECIOUS")
    media = tmp_path / "topics" / "4242" / "media"
    media.mkdir(parents=True)
    os.symlink(outside, media / "7001-notes.md")

    daemon.handle_message(bridge["cfg"], _msg(**_doc()))

    assert outside.read_text() == "PRECIOUS"          # the real target is untouched
    dest = media / "7001-notes.md"
    assert not os.path.islink(dest)                    # the link was replaced, not followed
    assert dest.read_bytes() == b"ATTACKER CONTENT"
    assert not list(media.glob("*.part-*"))            # no partial left behind


def test_a_symlinked_media_directory_is_refused(bridge, tmp_path, monkeypatch):
    """Containment cannot be reasoned about once the directory itself is a link."""
    from bridge import common
    monkeypatch.setattr(daemon, "download_file", common.download_file)

    outside_dir = tmp_path / "elsewhere"
    outside_dir.mkdir()
    topic = tmp_path / "topics" / "4242"
    topic.mkdir(parents=True)
    os.symlink(outside_dir, topic / "media")

    daemon.handle_message(bridge["cfg"], _msg(**_doc()))

    assert list(outside_dir.iterdir()) == []           # nothing was written through the link
    record, = _records(bridge["tmp"])
    assert "download failed" in record["text"]         # refused loudly, not silently


@pytest.mark.parametrize("name", ["", None, ".", "...", "\x00", "   "])
def test_a_missing_or_degenerate_filename_still_lands_somewhere_safe(bridge, name):
    daemon.handle_message(bridge["cfg"], _msg(**_doc(name=name)))

    _file_id, dest = bridge["downloaded"][-1]
    assert os.path.basename(dest).startswith("7001-")
    assert os.path.exists(dest)


def test_two_files_with_the_same_name_do_not_overwrite_each_other(bridge):
    daemon.handle_message(bridge["cfg"], _msg(message_id=1, **_doc(name="report.md")))
    daemon.handle_message(bridge["cfg"], _msg(message_id=2, **_doc(name="report.md")))

    destinations = {dest for _id, dest in bridge["downloaded"]}
    assert len(destinations) == 2


def test_a_failed_download_is_reported_not_dropped(bridge, monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("file is too big")

    monkeypatch.setattr(daemon, "download_file", boom)

    daemon.handle_message(bridge["cfg"], _msg(**_doc()))

    # The whole point of #150: the sender sees their file in Telegram either way, so a
    # failure must still produce a record rather than vanishing.
    record, = _records(bridge["tmp"])
    assert "download failed" in record["text"]
    assert "file is too big" in record["text"]


def test_images_are_untouched_by_this_path(bridge):
    daemon.handle_message(
        bridge["cfg"], _msg(**_doc(name="shot.png", mime="image/png")))

    record, = _records(bridge["tmp"])
    # Still the image note ("view it"), not the file note ("read it") — images already
    # worked and their wording tells the agent to look at it.
    assert record["kind"] == "image"
    assert "[Image attached" in record["text"]


def test_a_plain_text_message_is_unaffected(bridge):
    daemon.handle_message(bridge["cfg"], _msg(text="just words"))

    record, = _records(bridge["tmp"])
    assert record["text"] == "just words"
    assert bridge["downloaded"] == []


def test_an_empty_message_still_writes_nothing(bridge):
    daemon.handle_message(bridge["cfg"], _msg())

    assert _records(bridge["tmp"]) == []


@pytest.mark.parametrize("message_id,raw,expected", [
    (5, "notes.md", "5-notes.md"),
    (5, "../../etc/passwd", "5-passwd"),
    (5, "a b;c&d.md", "5-a_b_c_d.md"),
    (5, ".hidden", "5-hidden"),
    (5, "x" * 300 + ".md", "5-" + "x" * 120),
])
def test_safe_media_name(message_id, raw, expected):
    assert daemon.safe_media_name(message_id, raw) == expected


def test_a_failed_download_leaks_no_file_descriptor(tmp_path, monkeypatch):
    """`os.open` hands back a raw fd; if urlopen raised before it was wrapped, it leaked.

    One descriptor per failed download, and failures are exactly what happens when Telegram
    is unreachable — so the leak compounds precisely when the daemon can least afford it.
    """
    from bridge import common
    monkeypatch.setattr(common, "api", lambda *a, **k: {"file_path": "x"})

    def boom(*_a, **_k):
        raise RuntimeError("telegram unreachable")

    monkeypatch.setattr(common.urllib.request, "urlopen", boom)
    dest = tmp_path / "media" / "f.md"
    dest.parent.mkdir(parents=True)

    before = len(os.listdir("/proc/self/fd"))
    for _ in range(20):
        with pytest.raises(RuntimeError):
            common.download_file("t", "FILE-1", str(dest))
    after = len(os.listdir("/proc/self/fd"))

    assert after <= before + 1               # +1 tolerance for the listdir handle itself
    assert not list(dest.parent.glob("*.part-*"))
