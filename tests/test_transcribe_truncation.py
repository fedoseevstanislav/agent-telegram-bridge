import types

from bridge import transcribe as T


def _words(count):
    return " ".join(["word"] * count)


def test_audio_duration_uses_ffprobe(monkeypatch):
    seen = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        seen["kwargs"] = kwargs
        return types.SimpleNamespace(stdout="123.5\n")

    monkeypatch.setattr(T.shutil, "which", lambda name: "/usr/bin/ffprobe")
    monkeypatch.setattr(T.subprocess, "run", fake_run)

    assert T._audio_duration("/tmp/voice.ogg") == 123.5
    assert seen["argv"] == [
        "/usr/bin/ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        "/tmp/voice.ogg",
    ]
    assert seen["kwargs"] == {"check": True, "capture_output": True, "text": True}


def test_audio_duration_returns_none_on_failure(monkeypatch):
    monkeypatch.setattr(T.shutil, "which", lambda name: "/usr/bin/ffprobe")
    monkeypatch.setattr(T.subprocess, "run", lambda *args, **kwargs: 1 / 0)

    assert T._audio_duration("/tmp/voice.ogg") is None


def test_looks_truncated():
    assert T._looks_truncated("word " * 10, 600)
    assert not T._looks_truncated("word " * 500, 600)
    assert not T._looks_truncated("anything", None)
    assert not T._looks_truncated("x", 30)


def test_short_audio_uses_gpt_first(monkeypatch):
    calls = []
    monkeypatch.setattr(T, "_audio_duration", lambda path: 30)
    monkeypatch.setattr(
        T, "_request", lambda api_key, path, model: calls.append(model) or "transcript"
    )

    T.transcribe("/tmp/voice.ogg", "key")

    assert calls[0] == T.PRIMARY_MODEL


def test_long_audio_uses_whisper_first(monkeypatch):
    calls = []
    monkeypatch.setattr(T, "_audio_duration", lambda path: 600)
    monkeypatch.setattr(
        T, "_request", lambda api_key, path, model: calls.append(model) or _words(500)
    )

    T.transcribe("/tmp/voice.ogg", "key")

    assert calls[0] == T.FALLBACK_MODEL


def test_unknown_duration_uses_whisper_first(monkeypatch):
    calls = []
    monkeypatch.setattr(T, "_audio_duration", lambda path: None)
    monkeypatch.setattr(
        T, "_request", lambda api_key, path, model: calls.append(model) or "transcript"
    )

    T.transcribe("/tmp/voice.ogg", "key")

    assert calls[0] == T.FALLBACK_MODEL


def test_truncated_gpt_transcript_retries_with_whisper(monkeypatch):
    calls = []
    whisper_text = _words(220)

    def fake_request(api_key, path, model):
        calls.append(model)
        if model == T.PRIMARY_MODEL:
            return _words(5)
        return whisper_text

    monkeypatch.setattr(T, "_audio_duration", lambda path: 240)  # below threshold -> gpt-4o-mini first
    monkeypatch.setattr(T, "_request", fake_request)

    assert T.transcribe("/tmp/voice.ogg", "key") == whisper_text
    assert T.FALLBACK_MODEL in calls


def test_short_transcript_gets_the_incomplete_marker(monkeypatch):
    """Wording changed with #227: the failure is rarely a clean truncation.

    whisper-1 on long audio repeats a phrase and pads with hallucinated filler — the result is
    short AND wrong, not short because it stopped early. "Incomplete" says the true thing.
    """
    monkeypatch.setattr(T, "_audio_duration", lambda path: 600)
    monkeypatch.setattr(T, "_split", lambda *a, **k: [])   # unsplittable: one request, as before
    monkeypatch.setattr(T, "_request", lambda api_key, path, model: _words(5))

    result = T.transcribe("/tmp/voice.ogg", "key")

    assert result.endswith(
        "[transcript may be incomplete — audio 600s, 5 words]"
    )


def test_adequate_gpt_transcript_is_returned_without_whisper_retry(monkeypatch):
    calls = []
    transcript = _words(100)
    monkeypatch.setattr(T, "_audio_duration", lambda path: 120)
    monkeypatch.setattr(
        T, "_request", lambda api_key, path, model: calls.append(model) or transcript
    )

    assert T.transcribe("/tmp/voice.ogg", "key") == transcript
    assert calls == [T.PRIMARY_MODEL]
