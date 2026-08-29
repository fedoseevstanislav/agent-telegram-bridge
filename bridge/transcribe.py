"""Voice transcription via the OpenAI audio API. Stdlib multipart upload, ffmpeg fallback."""

import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
import uuid

OPENAI_URL = "https://api.openai.com/v1/audio/transcriptions"
PRIMARY_MODEL = "gpt-4o-mini-transcribe"
FALLBACK_MODEL = "whisper-1"
# At/above this, skip gpt-4o-mini-transcribe (which silently truncates near its ~2k output-token
# cap) and use whisper-1 directly. Set below the observed cap: an eight-minute recording lost
# roughly its final minute, so five minutes leaves about two minutes of measured margin.
LONG_AUDIO_THRESHOLD_S = 300
# Marker threshold, deliberately below the slowest complete sample that prompted #227. A gate
# close to natural slow speech marked a correct transcript even though its closing sentence was
# present.
# A false alarm on a good transcript is worse than no alarm, because it teaches the reader to
# ignore the one that matters. The threshold therefore catches only extreme degeneration.
#
# It cannot separate every bad result from every good one — a failed result can still have a
# higher word rate than a complete slow recording. That is why chunking is the fix and this is
# only a backstop: no word-rate gate can tell a slow speaker from a lost half of a note.
MIN_WORDS_PER_SEC = 0.5
# Above this, transcribe in pieces rather than in one request. whisper-1 has no output cap the
# way gpt-4o-mini-transcribe does, but it DEGENERATES on long audio: it falls into repeating a
# phrase, and pads the tail with stock hallucinations ("İzlediğiniz için teşekkür ederim" —
# "thanks for watching" — is the notorious one), losing most of what was actually said.
#
# In measured long recordings (#227), chunking recovered 27% more words in one case and nearly three
# times as many in another; only the chunked output retained the speaker's closing sentence.
# Those ratios, rather than the identity or provenance of the recordings, justify the split.
#
# 240 s pieces: comfortably inside where the degeneration starts, and few enough requests that a
# long note still finishes in seconds.
CHUNK_ABOVE_S = 240
CHUNK_SECONDS = 240

CONTENT_TYPES = {".ogg": "audio/ogg", ".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".mp4": "audio/mp4"}


def _multipart(fields, file_field, filename, file_bytes, content_type):
    boundary = "----tgbridge" + uuid.uuid4().hex
    body = b""
    for name, value in fields.items():
        body += (
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n"
        ).encode()
    body += (
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"{file_field}\"; "
        f"filename=\"{filename}\"\r\nContent-Type: {content_type}\r\n\r\n"
    ).encode()
    body += file_bytes + f"\r\n--{boundary}--\r\n".encode()
    return body, f"multipart/form-data; boundary={boundary}"


def _request(api_key, path, model):
    ext = os.path.splitext(path)[1].lower()
    with open(path, "rb") as f:
        file_bytes = f.read()
    body, content_type = _multipart(
        {"model": model}, "file", "voice" + ext, file_bytes, CONTENT_TYPES.get(ext, "audio/ogg")
    )
    req = urllib.request.Request(
        OPENAI_URL,
        data=body,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": content_type},
    )
    with urllib.request.urlopen(req, timeout=300) as resp:
        return json.load(resp)["text"]


def _audio_duration(path):
    try:
        ffprobe = shutil.which("ffprobe") or "/home/linuxbrew/.linuxbrew/bin/ffprobe"
        result = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                path,
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        return float(result.stdout.strip())
    except Exception:
        return None


def _split(path, seconds=CHUNK_SECONDS):
    """Cut `path` into ~`seconds` pieces beside it, returning them in order.

    Stream-copied, so nothing is re-encoded and no audio is lost — a piece boundary can land
    mid-word, which costs at most one word per boundary and is a bargain against losing half the
    note. Returns [] if ffmpeg cannot do it, and the caller falls back to one request.
    """
    ffmpeg = shutil.which("ffmpeg") or "/home/linuxbrew/.linuxbrew/bin/ffmpeg"
    stem = f"{path}.part-{uuid.uuid4().hex[:8]}"
    ext = os.path.splitext(path)[1].lower() or ".ogg"
    try:
        subprocess.run(
            [ffmpeg, "-y", "-loglevel", "error", "-i", path,
             "-f", "segment", "-segment_time", str(seconds), "-c", "copy",
             # Each piece starts at zero. Without this they keep the ORIGINAL timestamps, so
             # piece 3 of a 10-minute note declares itself as running 08:00-09:48 — a file whose
             # metadata claims eight minutes of nothing at the front, laid as a trap for
             # whatever reads it next. Measured as making no difference to the transcript
             # itself (447 words either way); it is here for the metadata, not the text.
             "-reset_timestamps", "1",
             f"{stem}-%03d{ext}"],
            check=True, capture_output=True, timeout=300,
        )
    except Exception:
        return []
    import glob
    return sorted(glob.glob(f"{stem}-*{ext}"))


def _transcribe_in_pieces(api_key, path, model):
    """One request per piece, joined. Returns None if splitting was not possible."""
    pieces = _split(path)
    if len(pieces) < 2:
        for piece in pieces:                      # a single piece is the whole file again
            os.remove(piece)
        return None
    try:
        return " ".join((_request(api_key, piece, model) or "").strip() for piece in pieces)
    finally:
        for piece in pieces:
            if os.path.exists(piece):
                os.remove(piece)


def _to_mp3(path):
    ffmpeg = shutil.which("ffmpeg") or "/home/linuxbrew/.linuxbrew/bin/ffmpeg"
    mp3_path = path + ".mp3"
    subprocess.run(
        [ffmpeg, "-y", "-loglevel", "error", "-i", path, mp3_path],
        check=True, capture_output=True,
    )
    return mp3_path


def _looks_truncated(transcript, duration):
    try:
        return (
            isinstance(duration, (int, float))
            and duration >= 60
            and len((transcript or "").split()) < duration * MIN_WORDS_PER_SEC
        )
    except Exception:
        return False


def _mark_if_short(text, duration):
    """Append the honest caveat when the result is too short for the audio it came from.

    Kept after the piecewise path too: chunking removes the failure this was written for, and
    the marker is what would say so if it ever came back.
    """
    if _looks_truncated(text, duration):
        return (text or "") + (
            "\n\n[transcript may be incomplete — audio %.0fs, %d words]"
            % (duration, len((text or "").split()))
        )
    return text


def transcribe(path, api_key):
    """Transcribe an audio file, routing long or unknown audio through whisper-1."""
    duration = _audio_duration(path)
    if duration is None or duration >= LONG_AUDIO_THRESHOLD_S:
        primary, secondary = FALLBACK_MODEL, PRIMARY_MODEL
    else:
        primary, secondary = PRIMARY_MODEL, FALLBACK_MODEL

    # Long audio goes in pieces. Not an optimisation — one request over ~4 minutes loses most
    # of the content to repetition and hallucinated filler (see CHUNK_ABOVE_S above).
    if isinstance(duration, (int, float)) and duration > CHUNK_ABOVE_S:
        try:
            pieced = _transcribe_in_pieces(api_key, path, FALLBACK_MODEL)
        except Exception:
            pieced = None
        if pieced and pieced.strip():
            return _mark_if_short(pieced, duration)

    errors = []
    text = None
    model_used = None
    for model in [primary, secondary]:
        try:
            text = _request(api_key, path, model)
            model_used = model
            break
        except urllib.error.HTTPError as e:
            errors.append(f"{model}: HTTP {e.code} {e.read()[:200]!r}")

    if model_used is None:
        try:
            mp3_path = _to_mp3(path)
            try:
                text = _request(api_key, mp3_path, FALLBACK_MODEL)
                model_used = f"{FALLBACK_MODEL}(mp3)"
            finally:
                if os.path.exists(mp3_path):
                    os.remove(mp3_path)
        except Exception as e:
            errors.append(f"mp3 fallback: {e}")
        if model_used is None:
            raise RuntimeError("transcription failed: " + " | ".join(errors))

    if model_used == PRIMARY_MODEL and _looks_truncated(text, duration):
        try:
            try:
                retry_text = _request(api_key, path, FALLBACK_MODEL)
            except Exception:
                mp3_path = _to_mp3(path)
                try:
                    retry_text = _request(api_key, mp3_path, FALLBACK_MODEL)
                finally:
                    if os.path.exists(mp3_path):
                        os.remove(mp3_path)
            if len((retry_text or "").split()) > len((text or "").split()):
                text = retry_text
        except Exception:
            pass

    return _mark_if_short(text, duration)


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from bridge.common import openai_api_key

    print(transcribe(sys.argv[1], openai_api_key()))
