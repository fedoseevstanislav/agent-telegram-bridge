"""Unit tests for Telegram HTML message formatting (#89): the Markdown->HTML converter,
the newline-aware splitter, and send_message's parse-error plain-text fallback."""

import pytest

from bridge import common

H = common.md_to_telegram_html


# ---- md_to_telegram_html -----------------------------------------------------

def test_bold_underline_strike():
    assert H("a **b** c") == "a <b>b</b> c"
    assert H("a __b__ c") == "a <u>b</u> c"
    assert H("a ~~b~~ c") == "a <s>b</s> c"


def test_inline_and_fenced_code():
    assert H("run `ls -l` now") == "run <code>ls -l</code> now"
    assert H("```python\nprint(1)\n```") == "<pre>print(1)\n</pre>"


def test_link():
    assert H("[docs](https://ex.com/p)") == '<a href="https://ex.com/p">docs</a>'


def test_angle_brackets_escaped_not_dropped():
    # The silent-truncation gotcha: literal < > must be escaped, never read as tags.
    assert H("show <thinking> and List<int>") == "show &lt;thinking&gt; and List&lt;int&gt;"
    assert H("a & b") == "a &amp; b"


def test_code_contents_not_reformatted():
    # markdown/angle-brackets inside code must be literal (escaped), not converted to tags
    assert H("`**x**`") == "<code>**x**</code>"
    assert H("`List<int>`") == "<code>List&lt;int&gt;</code>"


def test_single_char_emphasis_left_literal():
    # conservative: single * / _ are NOT italicised (avoid bullet + snake_case false hits)
    assert H("* item one") == "* item one"
    assert H("use my_var_name here") == "use my_var_name here"
    assert H("a *maybe* b") == "a *maybe* b"


def test_link_url_ampersand_escaped_once():
    out = H("[x](https://a.com?b=1&c=2)")
    assert out == '<a href="https://a.com?b=1&amp;c=2">x</a>'
    assert "&amp;amp;" not in out  # not double-escaped


def test_bold_around_inline_code_preserved():
    # bold wrapping normal text, code kept literal alongside it
    assert H("**do** `x<y`") == "<b>do</b> <code>x&lt;y</code>"


def test_plain_text_roundtrips():
    assert H("just a normal sentence.") == "just a normal sentence."


# ---- split_for_telegram ------------------------------------------------------

def test_split_short_is_single_chunk():
    assert common.split_for_telegram("hello") == ["hello"]


def test_split_empty_never_returns_empty_list():
    assert common.split_for_telegram("") == [""]


def test_split_respects_limit_on_newlines():
    text = "\n".join(["line"] * 100)
    chunks = common.split_for_telegram(text, limit=20)
    assert all(len(c) <= 20 for c in chunks)
    assert len(chunks) > 1


def test_split_hard_splits_overlong_single_line():
    chunks = common.split_for_telegram("x" * 45, limit=20)
    assert all(len(c) <= 20 for c in chunks)
    assert "".join(chunks) == "x" * 45


def test_split_is_lossless_roundtrip():
    # Concatenating chunks must reproduce the input exactly — boundary newlines are not
    # dropped (Codex #90 review) — and every chunk stays within the limit.
    cases = [
        ("a\nb\nc", 3),
        ("hello world", 3800),
        ("line\n" * 50, 17),
        ("x" * 45, 20),
        ("para1\n\npara2\n\npara3", 8),
        ("", 10),
        ("\n\n\n", 2),
        ("trailing newline\n", 5),
    ]
    for text, limit in cases:
        chunks = common.split_for_telegram(text, limit)
        assert "".join(chunks) == text, f"lossy for {text!r} @ {limit}"
        assert all(len(c) <= limit for c in chunks), f"over-limit chunk for {text!r} @ {limit}"


# ---- send_message ------------------------------------------------------------

class _Api:
    """Records sendMessage params; can force a parse error or a connection error."""

    def __init__(self, fail_parse=False, fail_conn=False):
        self.calls = []
        self.fail_parse = fail_parse
        self.fail_conn = fail_conn

    def __call__(self, token, method, params):
        self.calls.append(params)
        if self.fail_conn:
            raise RuntimeError("sendMessage: connection failed after 3 attempts")
        if self.fail_parse and "parse_mode" in params:
            raise RuntimeError("sendMessage: 400 Bad Request: can't parse entities")
        return {"message_id": len(self.calls)}


def test_send_message_happy_path_html(monkeypatch):
    rec = _Api()
    monkeypatch.setattr(common, "api", rec)
    common.send_message("t", 5, "hi **bold**", thread_id=33)
    assert len(rec.calls) == 1
    assert rec.calls[0]["parse_mode"] == "HTML"
    assert rec.calls[0]["text"] == "hi <b>bold</b>"
    assert rec.calls[0]["message_thread_id"] == 33
    assert rec.calls[0]["chat_id"] == 5


def test_send_message_falls_back_to_plain_on_parse_error(monkeypatch):
    rec = _Api(fail_parse=True)
    monkeypatch.setattr(common, "api", rec)
    common.send_message("t", 5, "bad <x", thread_id=33)
    assert len(rec.calls) == 2
    assert "parse_mode" in rec.calls[0]          # first HTML attempt
    assert "parse_mode" not in rec.calls[1]      # plain-text fallback
    assert rec.calls[1]["text"] == "bad <x"      # RAW chunk, unconverted — never dropped


def test_send_message_reraises_non_parse_error(monkeypatch):
    rec = _Api(fail_conn=True)
    monkeypatch.setattr(common, "api", rec)
    with pytest.raises(RuntimeError):
        common.send_message("t", 5, "hi", thread_id=33)
    assert len(rec.calls) == 1                   # no plain-text retry on connection failure


def test_send_message_no_thread_id_omits_field(monkeypatch):
    rec = _Api()
    monkeypatch.setattr(common, "api", rec)
    common.send_message("t", 5, "hi")
    assert "message_thread_id" not in rec.calls[0]


def test_send_message_splits_multichunk(monkeypatch):
    rec = _Api()
    monkeypatch.setattr(common, "api", rec)
    common.send_message("t", 5, "\n".join(["y" * 30] * 300), thread_id=1)
    assert len(rec.calls) > 1
    assert all(len(c["text"]) <= common._TG_HTML_LIMIT + 20 for c in rec.calls)  # +tags


def test_send_message_verbatim_preserves_disclosure_characters(monkeypatch):
    rec = _Api()
    monkeypatch.setattr(common, "api", rec)
    line = (
        "+ [install guide](https://example.com/real-target) `code` "
        "__init__.py <literal> ~~old~~ & exact\n"
    )
    disclosure = (line * (10528 // len(line) + 1))[:10528]

    deliveries = common.send_message("t", 5, disclosure, thread_id=33, verbatim=True)

    assert len(rec.calls) == 3
    assert "".join(call["text"] for call in rec.calls) == disclosure
    assert "".join(delivery["text"] for delivery in deliveries) == disclosure
    assert all("parse_mode" not in call for call in rec.calls)
    assert all(len(call["text"]) <= common._TG_HTML_LIMIT for call in rec.calls)


def test_send_message_adds_force_reply_only_to_a_single_prompt(monkeypatch):
    rec = _Api()
    monkeypatch.setattr(common, "api", rec)

    common.send_message(
        "t", 5, "Reply OK or go", thread_id=33,
        reply_markup={"force_reply": True, "input_field_placeholder": "OK to publish"},
    )

    assert rec.calls == [{
        "chat_id": 5,
        "message_thread_id": 33,
        "text": "Reply OK or go",
        "parse_mode": "HTML",
        "reply_markup": '{"force_reply":true,"input_field_placeholder":"OK to publish"}',
    }]
    with pytest.raises(ValueError, match="single Telegram message chunk"):
        common.send_message(
            "t", 5, "x" * (common._TG_HTML_LIMIT + 1), reply_markup={"force_reply": True}
        )


def test_verbatim_ambiguous_delivery_records_exact_inflight_chunk(monkeypatch):
    def ambiguous(_token, _method, _params):
        raise common.PossiblyDelivered("unknown send result")

    monkeypatch.setattr(common, "api", ambiguous)
    raw = "__init__.py [guide](https://example.com)"

    with pytest.raises(common.PossiblyDelivered) as caught:
        common.send_message("t", 5, raw, verbatim=True)

    assert caught.value.completed_sends == []
    assert caught.value.possibly_delivered_send == {
        "text": raw,
        "chunk_index": 0,
        "chunk_count": 1,
    }
