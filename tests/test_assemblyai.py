"""Voice transcription on the OpenAI path first, AssemblyAI as the fallback.

2026-10-28: the OpenAI account ran out of credits, and every voice note in every topic came
through as "[voice message — transcription failed: … HTTP 429 …]". The owner asked for
AssemblyAI. Measured on one of those notes: completed in 3.7 s, English detected.
2026-10-29: the owner topped up the OpenAI account and 15 real notes went through on it; OpenAI goes first again and AssemblyAI catches its failures.
"""

import json
import os

import pytest

from bridge import common, daemon, transcribe

KEY = "assemblyai-placeholder-not-real"


def _config(tmp_path, monkeypatch, body, mode=0o600):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(body))
    os.chmod(path, mode)
    monkeypatch.setattr(common, "CONFIG_PATH", str(path))


def test_the_key_comes_from_the_bridges_own_config(tmp_path, monkeypatch):
    _config(tmp_path, monkeypatch, {"assemblyai_api_key": KEY})
    assert common.assemblyai_api_key() == KEY


@pytest.mark.parametrize("body", [{}, {"assemblyai_api_key": ""}, {"assemblyai_api_key": 7}])
def test_no_usable_key_means_none(tmp_path, monkeypatch, body):
    _config(tmp_path, monkeypatch, body)
    assert common.assemblyai_api_key() is None


def test_a_world_readable_config_yields_no_key(tmp_path, monkeypatch):
    _config(tmp_path, monkeypatch, {"assemblyai_api_key": KEY}, mode=0o644)
    assert common.assemblyai_api_key() is None


def _fake_api(monkeypatch, statuses):
    calls = []

    def _aai(api_key, url, data=None, content_type=None, timeout=120):
        calls.append(url)
        assert 0 < timeout <= 120
        if url.endswith("/upload"):
            return {"upload_url": "https://cdn.example/u1"}
        if url.endswith("/transcript"):
            body = json.loads(data)
            assert body["audio_url"] == "https://cdn.example/u1"
            assert body["language_detection"] is True
            return {"id": "t1"}
        return statuses.pop(0)

    monkeypatch.setattr(transcribe, "_aai", _aai)
    return calls


def test_it_polls_until_completed(tmp_path, monkeypatch):
    audio = tmp_path / "v.ogg"
    audio.write_bytes(b"OggS")
    calls = _fake_api(monkeypatch, [{"status": "queued"}, {"status": "processing"},
                                    {"status": "completed", "text": "Reader notes?"}])
    assert transcribe.transcribe_assemblyai(str(audio), KEY, poll=0) == "Reader notes?"
    assert sum(u.endswith("/transcript/t1") for u in calls) == 3


def test_no_speech_is_the_empty_string(tmp_path, monkeypatch):
    audio = tmp_path / "v.ogg"
    audio.write_bytes(b"OggS")
    _fake_api(monkeypatch, [{"status": "completed", "text": None}])
    assert transcribe.transcribe_assemblyai(str(audio), KEY, poll=0) == ""


def test_an_error_status_raises(tmp_path, monkeypatch):
    audio = tmp_path / "v.ogg"
    audio.write_bytes(b"OggS")
    _fake_api(monkeypatch, [{"status": "error", "error": "bad audio"}])
    with pytest.raises(RuntimeError, match="bad audio"):
        transcribe.transcribe_assemblyai(str(audio), KEY, poll=0)


def test_it_gives_up_after_the_timeout(tmp_path, monkeypatch):
    audio = tmp_path / "v.ogg"
    audio.write_bytes(b"OggS")
    _fake_api(monkeypatch, [{"status": "processing"}] * 5)
    monkeypatch.setattr(transcribe, "ASSEMBLYAI_TIMEOUT_S", 0)
    with pytest.raises(RuntimeError, match="did not finish"):
        transcribe.transcribe_assemblyai(str(audio), KEY, poll=0)


@pytest.fixture
def routes(monkeypatch):
    seen = []
    monkeypatch.setattr(daemon, "log", lambda *a, **k: None)
    monkeypatch.setattr(daemon, "openai_api_key", lambda: "sk-x")
    monkeypatch.setattr(daemon, "transcribe",
                        lambda path, key: (seen.append("openai"), "from openai")[1])
    monkeypatch.setattr(daemon, "transcribe_assemblyai",
                        lambda path, key: (seen.append("aai"), "from aai")[1])
    return seen


def test_without_a_key_it_is_the_openai_path_as_before(routes, monkeypatch):
    monkeypatch.setattr(daemon, "assemblyai_api_key", lambda: None)
    assert daemon.transcribe_voice("/v.ogg") == "from openai" and routes == ["openai"]


def test_with_a_key_openai_still_goes_first(routes, monkeypatch):
    monkeypatch.setattr(daemon, "assemblyai_api_key", lambda: KEY)
    assert daemon.transcribe_voice("/v.ogg") == "from openai" and routes == ["openai"]


def test_a_failed_openai_falls_back_to_assemblyai(routes, monkeypatch):
    monkeypatch.setattr(daemon, "assemblyai_api_key", lambda: KEY)

    def _boom(path, key):
        raise RuntimeError("HTTP 429 no credits")

    monkeypatch.setattr(daemon, "transcribe", _boom)
    assert daemon.transcribe_voice("/v.ogg") == "from aai"


def test_when_both_fail_the_error_names_both(monkeypatch):
    monkeypatch.setattr(daemon, "log", lambda *a, **k: None)
    monkeypatch.setattr(daemon, "assemblyai_api_key", lambda: KEY)
    monkeypatch.setattr(daemon, "openai_api_key", lambda: "sk-x")

    def _aai(path, key):
        raise RuntimeError("AssemblyAI error: bad audio")

    def _openai(path, key):
        raise RuntimeError("HTTP 429 no credits")

    monkeypatch.setattr(daemon, "transcribe_assemblyai", _aai)
    monkeypatch.setattr(daemon, "transcribe", _openai)
    with pytest.raises(RuntimeError, match="OpenAI: .*429.* \\| AssemblyAI: .*bad audio"):
        daemon.transcribe_voice("/v.ogg")


def test_a_key_in_an_error_never_leaves_transcribe_voice(monkeypatch):
    logged = []
    monkeypatch.setattr(daemon, "log", logged.append)
    monkeypatch.setattr(daemon, "assemblyai_api_key", lambda: KEY)
    monkeypatch.setattr(daemon, "openai_api_key", lambda: "sk-x")

    def _aai(path, key):
        raise RuntimeError(f"Invalid header value {key}")

    def _openai(path, key):
        raise RuntimeError(f"also saw {KEY}")

    monkeypatch.setattr(daemon, "transcribe_assemblyai", _aai)
    monkeypatch.setattr(daemon, "transcribe", _openai)
    with pytest.raises(RuntimeError) as err:
        daemon.transcribe_voice("/v.ogg")
    assert KEY not in str(err.value)
    assert err.value.__cause__ is None and err.value.__context__ is None
    assert all(KEY not in line for line in logged)


def test_a_redirect_is_refused_not_followed():
    import urllib.request
    handler = transcribe._NoRedirect()
    req = urllib.request.Request("https://api.assemblyai.com/v2/transcript/t1",
                                 headers={"authorization": KEY})
    assert handler.redirect_request(req, None, 302, "Found", {}, "https://evil.example/") is None


def test_each_request_gets_only_the_time_left(tmp_path, monkeypatch):
    audio = tmp_path / "v.ogg"
    audio.write_bytes(b"OggS")
    timeouts = []
    clock = {"t": 1000.0}
    monkeypatch.setattr("time.time", lambda: clock["t"])
    monkeypatch.setattr("time.sleep", lambda s: None)

    def _aai(api_key, url, data=None, content_type=None, timeout=120):
        timeouts.append(timeout)
        clock["t"] += 100  # every request takes 100 s
        if url.endswith("/upload"):
            return {"upload_url": "u"}
        if url.endswith("/transcript"):
            return {"id": "t1"}
        return {"status": "processing"}

    monkeypatch.setattr(transcribe, "_aai", _aai)
    with pytest.raises(RuntimeError, match="did not finish"):
        transcribe.transcribe_assemblyai(str(audio), KEY, poll=0)
    assert timeouts == [120, 120, 100]
