"""A voice note with no audible speech must not vanish (2026-09-27).

The owner's phone mic went quiet; four notes in ten minutes transcribed to "" and were dropped
by the empty-text guard, with no word back. He kept talking to nobody. Now the topic hears it.
"""

import pytest

from bridge import daemon

CFG = {"chat_id": 1, "owner_id": 5, "bot_token": "T"}


def _voice(caption=None):
    msg = {"chat": {"id": 1}, "from": {"id": 5}, "message_id": 42,
           "message_thread_id": 7033, "voice": {"file_id": "F"}}
    if caption:
        msg["caption"] = caption
    return msg


@pytest.fixture
def harness(monkeypatch, tmp_path):
    out = {"replies": [], "inbox": []}
    monkeypatch.setattr(daemon, "log", lambda *a, **k: None)
    monkeypatch.setattr(daemon, "carry_forward_active", lambda tid: False)
    monkeypatch.setattr(daemon, "state_path", lambda *p: str(tmp_path.joinpath(*p)))
    monkeypatch.setattr(daemon, "download_file", lambda token, fid, path: None)
    monkeypatch.setattr(daemon, "openai_api_key", lambda: "K")
    monkeypatch.setattr(daemon, "reply", lambda cfg, tid, text: out["replies"].append(text) or True)
    monkeypatch.setattr(daemon, "append_jsonl",
                        lambda path, rec, *a, **k: out["inbox"].append(rec) or True)
    return out


@pytest.mark.parametrize("heard", ["", "   \n"])
def test_a_silent_note_is_answered_not_dropped(harness, monkeypatch, heard):
    monkeypatch.setattr(daemon, "transcribe", lambda path, key: heard)
    daemon.handle_message(CFG, _voice())
    assert len(harness["replies"]) == 1 and "no speech" in harness["replies"][0]
    assert harness["inbox"] == []


def test_a_note_with_speech_is_unchanged(harness, monkeypatch):
    monkeypatch.setattr(daemon, "transcribe", lambda path, key: "please ship it")
    daemon.handle_message(CFG, _voice())
    assert harness["replies"] == []
    assert [r.get("text") for r in harness["inbox"]] == ["please ship it"]


def test_a_captioned_silent_note_still_delivers_the_caption(harness, monkeypatch):
    monkeypatch.setattr(daemon, "transcribe", lambda path, key: "")
    daemon.handle_message(CFG, _voice(caption="see attached"))
    assert len(harness["replies"]) == 1
    assert [r.get("text", "").strip() for r in harness["inbox"]] == ["see attached"]


def test_a_failed_notice_is_not_filed_as_a_transcription_failure(harness, monkeypatch):
    """reply() re-raises anything but a closed topic. Inside the transcription try, that
    would have queued "[voice message — transcription failed: …]" for the session."""
    monkeypatch.setattr(daemon, "transcribe", lambda path, key: "")

    def _down(cfg, tid, text):
        raise OSError("network down")
    monkeypatch.setattr(daemon, "reply", _down)

    daemon.handle_message(CFG, _voice(caption="see attached"))

    assert [r.get("text", "").strip() for r in harness["inbox"]] == ["see attached"]


@pytest.mark.parametrize("heard", ["", "  ", None])
def test_a_long_silent_note_is_not_dressed_up_as_a_truncated_transcript(heard):
    """Review r1, C1: at >= 60 s the incomplete-transcript marker made "" non-empty, so the
    no-speech notice never fired and the marker alone was queued as a message."""
    from bridge import transcribe
    assert transcribe._mark_if_short(heard, 95) == ""


def test_a_real_short_transcript_is_still_marked():
    from bridge import transcribe
    assert "may be incomplete" in transcribe._mark_if_short("two words", 95)
