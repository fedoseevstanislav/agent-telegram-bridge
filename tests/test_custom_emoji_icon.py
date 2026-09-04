"""Per-topic custom-emoji message icon (#292).

The registry's optional `icon_custom_emoji_id` turns send_text's plain icon prefix into a
Telegram `<tg-emoji>` entity on the first chunk. The tag is spliced in AFTER the Markdown to
HTML conversion, because that conversion escapes `< >` in its input.
"""

import pytest

from bridge import cli, common


CFG = {"bot_token": "token", "chat_id": -100}
EMOJI_ID = "1234567890"          # made-up id; the real one lives only in the registry
ICON = "🦊"
# One body exercising every part of the conversion the prefix must not disturb.
BODY = "a < b & **bold** `x<y`"
BODY_HTML = "a &lt; b &amp; <b>bold</b> <code>x&lt;y</code>"


class _Api:
    """Records sendMessage params; can force one parse error per HTML attempt."""

    def __init__(self, fail_parse=False):
        self.calls = []
        self.fail_parse = fail_parse

    def __call__(self, _token, method, params):
        assert method == "sendMessage"
        self.calls.append(params)
        if self.fail_parse and "parse_mode" in params:
            raise RuntimeError("sendMessage: 400 Bad Request: can't parse entities")
        return {"message_id": 9000 + len(self.calls)}


def _env(tmp_path, monkeypatch, entry, fail_parse=False):
    monkeypatch.setattr(common, "STATE_DIR", str(tmp_path))
    monkeypatch.setattr(cli, "read_registry", lambda: {"55": entry})
    rec = _Api(fail_parse=fail_parse)
    monkeypatch.setattr(common, "api", rec)
    return rec


# ---- C1: the entity is spliced after conversion ------------------------------

def test_custom_emoji_id_wraps_the_icon_on_chunk_one(tmp_path, monkeypatch):
    rec = _env(tmp_path, monkeypatch,
               {"icon": ICON, "icon_custom_emoji_id": EMOJI_ID})

    cli.send_text(CFG, 55, BODY)

    assert len(rec.calls) == 1
    assert rec.calls[0]["parse_mode"] == "HTML"
    assert rec.calls[0]["text"] == (
        f'<tg-emoji emoji-id="{EMOJI_ID}">{ICON}</tg-emoji> {BODY_HTML}'
    )


# ---- C2: no field, byte-identical to the pre-#292 payload --------------------

def test_without_the_field_the_payload_is_unchanged(tmp_path, monkeypatch):
    rec = _env(tmp_path, monkeypatch, {"icon": ICON})

    cli.send_text(CFG, 55, BODY)

    assert rec.calls[0]["text"] == f"{ICON} {BODY_HTML}"
    assert rec.calls[0]["parse_mode"] == "HTML"


@pytest.mark.parametrize("raw", ["", "12a", "<tg-emoji>", " 123", 1234567890, None])
def test_non_digit_ids_are_ignored_and_logged(tmp_path, monkeypatch, capsys, raw):
    entry = {"icon": ICON}
    if raw is not None:
        entry["icon_custom_emoji_id"] = raw
    rec = _env(tmp_path, monkeypatch, entry)

    cli.send_text(CFG, 55, BODY)

    assert rec.calls[0]["text"] == f"{ICON} {BODY_HTML}"
    logged = capsys.readouterr().err
    assert ("icon_custom_emoji_id" in logged) is (raw is not None)


# ---- C3: fallback and later chunks -------------------------------------------

def test_plain_text_fallback_keeps_the_plain_icon(tmp_path, monkeypatch):
    rec = _env(tmp_path, monkeypatch,
               {"icon": ICON, "icon_custom_emoji_id": EMOJI_ID}, fail_parse=True)

    cli.send_text(CFG, 55, BODY)

    assert len(rec.calls) == 2
    assert "parse_mode" not in rec.calls[1]
    assert rec.calls[1]["text"] == f"{ICON} {BODY}"


def test_later_chunks_carry_no_prefix(tmp_path, monkeypatch):
    rec = _env(tmp_path, monkeypatch,
               {"icon": ICON, "icon_custom_emoji_id": EMOJI_ID})

    cli.send_text(CFG, 55, "x" * 4000)

    assert len(rec.calls) == 2
    assert rec.calls[0]["text"].startswith(f'<tg-emoji emoji-id="{EMOJI_ID}">{ICON}</tg-emoji> ')
    assert "tg-emoji" not in rec.calls[1]["text"]
    assert ICON not in rec.calls[1]["text"]


def test_verbatim_sends_are_untouched(monkeypatch):
    rec = _Api()
    monkeypatch.setattr(common, "api", rec)

    common.send_message("t", 5, f"{ICON} {BODY}", thread_id=55, verbatim=True,
                        icon_custom_emoji=(ICON, EMOJI_ID))

    assert rec.calls[0]["text"] == f"{ICON} {BODY}"
    assert "parse_mode" not in rec.calls[0]


# ---- C4: file captions use the same entity helper ----------------------------

def test_file_caption_wraps_the_first_icon_after_html_conversion(tmp_path, monkeypatch):
    rec = _env(tmp_path, monkeypatch,
               {"icon": ICON, "icon_custom_emoji_id": EMOJI_ID})
    monkeypatch.setattr(cli, "file_send_plan", lambda *_a, **_k: None)
    monkeypatch.setattr(common, "read_file_for_upload",
                        lambda *_a, **_k: ("sendDocument", "document", b"file", "digest"))
    uploads = []
    monkeypatch.setattr(common, "api_upload",
                        lambda _token, _method, params, *_a: uploads.append(params) or {"message_id": 1})

    cli.send_files(CFG, 55, ["first.txt", "second.txt"], caption=BODY)

    assert uploads[0]["caption"] == (
        f'<tg-emoji emoji-id="{EMOJI_ID}">{ICON}</tg-emoji> {BODY_HTML}'
    )
    assert uploads[0]["parse_mode"] == "HTML"
    assert "caption" not in uploads[1]


def test_file_caption_without_custom_emoji_is_unchanged(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch, {"icon": ICON})
    monkeypatch.setattr(cli, "file_send_plan", lambda *_a, **_k: None)
    monkeypatch.setattr(common, "read_file_for_upload",
                        lambda *_a, **_k: ("sendDocument", "document", b"file", "digest"))
    uploads = []
    monkeypatch.setattr(common, "api_upload",
                        lambda _token, _method, params, *_a: uploads.append(params) or {"message_id": 1})

    cli.send_files(CFG, 55, ["first.txt"], caption=BODY)

    assert uploads[0]["caption"] == f"{ICON} {BODY_HTML}"


def test_over_limit_file_caption_keeps_the_text_message_fallback(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch,
         {"icon": ICON, "icon_custom_emoji_id": EMOJI_ID})
    monkeypatch.setattr(cli, "file_send_plan", lambda *_a, **_k: None)
    sent_text = []
    monkeypatch.setattr(cli, "send_text",
                        lambda _cfg, topic_id, text: sent_text.append((topic_id, text)))
    uploads = []
    monkeypatch.setattr(cli, "send_file",
                        lambda *_a, **_k: uploads.append(_k) or {
                            "result": {"message_id": 1}, "path": "first.txt",
                            "size": 1, "content_sha256": "digest"})
    caption = "x" * (cli.CAPTION_LIMIT + 1)

    cli.send_files(CFG, 55, ["first.txt"], caption=caption)

    assert sent_text == [(55, caption)]
    assert uploads[0]["caption"] is None
