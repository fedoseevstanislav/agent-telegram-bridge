"""Long voice notes are transcribed in pieces, because one request loses most of them (#227).

whisper-1 has no output cap the way gpt-4o-mini-transcribe does, so the earlier fix (#117) —
route anything long to whisper-1 — looked sufficient. It is not. On long audio whisper-1
DEGENERATES: it repeats a phrase, and pads the tail with a stock hallucination. Measured on two
real notes:

    588 s   352 words in one request   →  447 in pieces, and only the pieced version contains
                                          the speaker's actual closing sentence
    944 s   218 words in one request   →  620 in pieces, nearly three times the content; the
                                          single-request version was mostly one phrase repeated

Nothing failed, nothing raised, and the daemon logged a successful transcription both times.
"""

import os

import pytest

from bridge import transcribe


class _Recorder:
    """Stands in for the OpenAI call: records what it was asked to transcribe.

    Returns enough words that the short-transcript marker does not fire — these tests are about
    which requests are made, and a marker appended to the result would obscure that.
    """

    WORDS = 400

    def __init__(self, per_file="piece"):
        self.paths = []
        self.per_file = per_file

    def __call__(self, api_key, path, model):
        self.paths.append((path, model))
        label = f"{self.per_file}-{len(self.paths)}"
        return " ".join([label] + ["w"] * self.WORDS)

    def joined(self, count):
        return " ".join(" ".join([f"{self.per_file}-{i}"] + ["w"] * self.WORDS)
                        for i in range(1, count + 1))


def _fake_pieces(monkeypatch, tmp_path, count):
    made = []
    for i in range(count):
        piece = tmp_path / f"piece-{i}.ogg"
        piece.write_bytes(b"x")
        made.append(str(piece))
    monkeypatch.setattr(transcribe, "_split", lambda path, seconds=None: list(made))
    return made


def test_long_audio_is_transcribed_in_pieces_and_joined(monkeypatch, tmp_path):
    audio = tmp_path / "voice.oga"
    audio.write_bytes(b"x")
    pieces = _fake_pieces(monkeypatch, tmp_path, 3)
    recorder = _Recorder()
    monkeypatch.setattr(transcribe, "_request", recorder)
    monkeypatch.setattr(transcribe, "_audio_duration", lambda path: 600.0)

    text = transcribe.transcribe(str(audio), "key")

    assert text == recorder.joined(3)
    assert [p for p, _ in recorder.paths] == pieces
    assert {m for _, m in recorder.paths} == {transcribe.FALLBACK_MODEL}


def test_the_pieces_are_deleted_afterwards(monkeypatch, tmp_path):
    audio = tmp_path / "voice.oga"
    audio.write_bytes(b"x")
    pieces = _fake_pieces(monkeypatch, tmp_path, 2)
    monkeypatch.setattr(transcribe, "_request", _Recorder())
    monkeypatch.setattr(transcribe, "_audio_duration", lambda path: 600.0)

    transcribe.transcribe(str(audio), "key")

    assert not [p for p in pieces if os.path.exists(p)]


def test_short_audio_is_still_one_request(monkeypatch, tmp_path):
    """The common case must not pay for this. Nothing under the threshold is split."""
    audio = tmp_path / "voice.oga"
    audio.write_bytes(b"x")
    monkeypatch.setattr(transcribe, "_split",
                        lambda *a, **k: pytest.fail("short audio must not be split"))
    recorder = _Recorder(per_file="whole")
    monkeypatch.setattr(transcribe, "_request", recorder)
    monkeypatch.setattr(transcribe, "_audio_duration", lambda path: 30.0)

    assert transcribe.transcribe(str(audio), "key") == recorder.joined(1)
    assert recorder.paths == [(str(audio), transcribe.PRIMARY_MODEL)]


def test_a_file_that_cannot_be_split_falls_back_to_one_request(monkeypatch, tmp_path):
    """No ffmpeg, or a container it cannot segment: still transcribe, just the old way."""
    audio = tmp_path / "voice.oga"
    audio.write_bytes(b"x")
    monkeypatch.setattr(transcribe, "_split", lambda *a, **k: [])
    recorder = _Recorder(per_file="whole")
    monkeypatch.setattr(transcribe, "_request", recorder)
    monkeypatch.setattr(transcribe, "_audio_duration", lambda path: 600.0)

    assert transcribe.transcribe(str(audio), "key") == recorder.joined(1)
    assert recorder.paths == [(str(audio), transcribe.FALLBACK_MODEL)]


def test_a_single_piece_is_not_treated_as_chunked(monkeypatch, tmp_path):
    """One piece IS the whole file; taking that path would just rename the same request."""
    audio = tmp_path / "voice.oga"
    audio.write_bytes(b"x")
    _fake_pieces(monkeypatch, tmp_path, 1)
    recorder = _Recorder(per_file="whole")
    monkeypatch.setattr(transcribe, "_request", recorder)
    monkeypatch.setattr(transcribe, "_audio_duration", lambda path: 600.0)

    assert transcribe.transcribe(str(audio), "key") == recorder.joined(1)
    assert recorder.paths == [(str(audio), transcribe.FALLBACK_MODEL)]


def test_unknown_duration_is_not_chunked_but_still_avoids_the_capped_model(monkeypatch, tmp_path):
    """ffprobe missing is not evidence of length — but it is a reason not to risk the cap."""
    audio = tmp_path / "voice.oga"
    audio.write_bytes(b"x")
    monkeypatch.setattr(transcribe, "_split",
                        lambda *a, **k: pytest.fail("unknown duration must not be split"))
    recorder = _Recorder(per_file="whole")
    monkeypatch.setattr(transcribe, "_request", recorder)
    monkeypatch.setattr(transcribe, "_audio_duration", lambda path: None)

    assert transcribe.transcribe(str(audio), "key") == recorder.joined(1)
    assert recorder.paths == [(str(audio), transcribe.FALLBACK_MODEL)]


@pytest.mark.parametrize("words,duration,marked", [
    (447, 588, False),      # measured, complete — must NOT be marked
    (620, 944, False),      # measured, complete — the 0.7 gate marked this one
    (218, 944, True),       # measured, lost two thirds of the note
    (10, 600, True),
])
def test_the_incomplete_marker_is_calibrated_against_measured_notes(words, duration, marked):
    assert transcribe._looks_truncated(" ".join(["w"] * words), duration) is marked
