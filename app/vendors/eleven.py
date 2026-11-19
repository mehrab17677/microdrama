"""Voice, music and sound (guide 6.3), ElevenLabs. Auth header: xi-api-key. Base https://api.elevenlabs.io

Speech:        POST /v1/text-to-dialogue/with-timestamps, one input per request, model eleven_v3
Music:         POST /v1/music  (model_id always sent; force_instrumental=true)
Voice design:  POST /v1/text-to-voice/design  then  POST /v1/text-to-voice
Sound effects: POST /v1/sound-generation  (model_id always sent: the default is deprecated)
Speech to text: POST /v1/speech-to-text  (multipart)
"""
from __future__ import annotations

import base64
import hashlib
import os

import httpx

from app.config import dry_run, get_config
from app.jobs.errors import PermanentError
from app.vendors import fixtures
from app.vendors.errors import check_response, map_exception

def _local() -> bool:
    """VOICE_BACKEND=local swaps in the open-source test backend (English only; see local_voice.py). Free, so it
    also runs under DRY_RUN=1."""
    return os.environ.get("VOICE_BACKEND") == "local"


BASE = "https://api.elevenlabs.io"
MAX_SPEECH_CHARS = 2000  # the docs call this a reliability limit
MUSIC_MS = (3000, 600000)
OUTPUT_FORMAT = "mp3_44100_128"


def _headers() -> dict:
    return {"xi-api-key": os.environ.get("ELEVENLABS_API_KEY", "")}


def _request(method: str, path: str, **kw) -> httpx.Response:
    try:
        resp = httpx.request(method, BASE + path, headers=_headers(), timeout=kw.pop("timeout", 300), **kw)
    except Exception as exc:  # noqa: BLE001
        raise map_exception(exc) from exc
    check_response(resp)
    return resp


# ---------- pure helpers ----------

def stable_seed(line_id: str, version: int) -> int:
    """Stable integer in 0..4294967295 derived from the line id and version."""
    return int(hashlib.sha256(f"{line_id}:{version}".encode()).hexdigest()[:8], 16)


def apply_pronunciations(text: str, pairs: list[tuple[str, str]]) -> str:
    """Replace each term with its respelling (longest terms first). Pronunciation dictionaries are
    not used: their support on eleven_v3 is unverified."""
    for term, spoken_as in sorted(pairs, key=lambda p: -len(p[0])):
        text = text.replace(term, spoken_as)
    return text


def word_timings(alignment: dict | None) -> list[dict]:
    """[{w, start, end}] from the character alignment, skipping everything inside square brackets
    (audio tags). A null alignment gives an empty list: captions then fall back to even spacing."""
    if not alignment:
        return []
    chars = alignment["characters"]
    starts = alignment["character_start_times_seconds"]
    ends = alignment["character_end_times_seconds"]
    words: list[dict] = []
    depth, cur, w_start, w_end = 0, [], None, None

    def flush():
        nonlocal cur, w_start, w_end
        if cur:
            words.append({"w": "".join(cur), "start": w_start, "end": w_end})
        cur, w_start, w_end = [], None, None

    for ch, st, en in zip(chars, starts, ends):
        if ch == "[":
            depth += 1
            flush()
            continue
        if ch == "]":
            depth = max(0, depth - 1)
            continue
        if depth:
            continue
        if ch.isspace():
            flush()
            continue
        if not cur:
            w_start = st
        cur.append(ch)
        w_end = en
    flush()
    return words


def speech_cost(chars: int) -> float:
    return chars / 1000 * get_config("price_book")["speech_per_1k_chars"]


def music_cost(length_ms: int) -> float:
    return length_ms / 60000 * get_config("price_book")["music_per_minute"]


def sfx_cost(duration_seconds: float) -> float:
    return duration_seconds / 60 * get_config("price_book")["sfx_per_minute"]


# ---------- speech ----------

def speech(text: str, voice_id: str, language_code: str, seed: int) -> tuple[dict, dict]:
    """One dialogue line. Returns ({audio: mp3 bytes, words, alignment}, usage).
    The caller measures duration with ffprobe on the saved file, not from the alignment."""
    if len(text) > MAX_SPEECH_CHARS:
        raise PermanentError(f"line is {len(text)} characters; the limit is {MAX_SPEECH_CHARS}")
    model = get_config("tts_model")
    usage = {"model": model, "chars": len(text), "cost_usd": speech_cost(len(text))}
    if _local():
        from app.vendors import local_voice

        audio = local_voice.speech(text, voice_id, language_code, seed)
        return {"audio": audio, "alignment": None, "words": []}, {"model": "local:" + local_voice.MODEL_ID, "chars": len(text), "cost_usd": 0.0}
    if dry_run():
        n = len(text)
        alignment = {
            "characters": list(text),
            "character_start_times_seconds": [2.0 * i / max(n, 1) for i in range(n)],
            "character_end_times_seconds": [2.0 * (i + 1) / max(n, 1) for i in range(n)],
        }
        return {"audio": fixtures.fixture_bytes("speech_2s.mp3"), "alignment": alignment, "words": word_timings(alignment)}, usage
    resp = _request(
        "POST", "/v1/text-to-dialogue/with-timestamps",
        params={"output_format": OUTPUT_FORMAT},
        json={"inputs": [{"text": text, "voice_id": voice_id}], "model_id": model, "language_code": language_code, "seed": seed},
    )
    body = resp.json()
    alignment = body.get("alignment")
    return {"audio": base64.b64decode(body["audio_base64"]), "alignment": alignment, "words": word_timings(alignment)}, usage


# ---------- music ----------

def music(prompt: str, length_ms: int) -> tuple[bytes, dict]:
    if not MUSIC_MS[0] <= length_ms <= MUSIC_MS[1]:
        raise PermanentError(f"music length must be {MUSIC_MS[0]} to {MUSIC_MS[1]} ms, got {length_ms}")
    model = get_config("music_model")
    usage = {"model": model, "length_ms": length_ms, "cost_usd": music_cost(length_ms)}
    if _local():
        from app.vendors import local_voice

        return local_voice.music(prompt, length_ms), {"model": "local:procedural", "length_ms": length_ms, "cost_usd": 0.0}
    if dry_run():
        return fixtures.fixture_bytes("music_30s.mp3"), usage
    resp = _request(
        "POST", "/v1/music", params={"output_format": OUTPUT_FORMAT},
        json={"prompt": prompt, "music_length_ms": length_ms, "model_id": model, "force_instrumental": True},
    )
    return resp.content, usage


# ---------- voice design ----------

def design_voice(description: str, sample_text: str | None = None) -> tuple[list[dict], dict]:
    """Returns (previews, usage). Each preview: {generated_voice_id, audio, duration_secs}.
    The API wants preview text of 100 to 1000 characters; otherwise ask it to write one."""
    body: dict = {"voice_description": description, "model_id": get_config("voice_design_model")}
    if sample_text and 100 <= len(sample_text) <= 1000:
        body["text"] = sample_text
    else:
        body["auto_generate_text"] = True
    usage = {"model": body["model_id"], "cost_usd": 0.0}  # no price found in the docs read
    if _local():
        from app.vendors import local_voice

        return local_voice.design(description, sample_text), {"model": "local:" + local_voice.MODEL_ID, "cost_usd": 0.0}
    if dry_run():
        vid = "dry-gen-" + hashlib.sha256(description.encode()).hexdigest()[:10]
        return [{"generated_voice_id": vid, "audio": fixtures.fixture_bytes("speech_2s.mp3"), "duration_secs": 2.0}], usage
    data = _request("POST", "/v1/text-to-voice/design", params={"output_format": OUTPUT_FORMAT}, json=body).json()
    previews = [
        {"generated_voice_id": p["generated_voice_id"], "audio": base64.b64decode(p["audio_base_64"]), "duration_secs": p["duration_secs"]}
        for p in data["previews"]  # note the field name audio_base_64 (extra underscore)
    ]
    return previews, usage


def save_voice(generated_voice_id: str, name: str, description: str) -> str:
    """Save a chosen preview as a permanent voice. Returns the voice_id."""
    if _local():
        return generated_voice_id  # a local voice id is self-contained: it carries its own pitch and tempo
    if dry_run():
        return "dry-voice-" + hashlib.sha256(generated_voice_id.encode()).hexdigest()[:10]
    data = _request(
        "POST", "/v1/text-to-voice",
        json={"voice_name": name, "voice_description": description, "generated_voice_id": generated_voice_id},
    ).json()
    return data["voice_id"]


# ---------- sound effects ----------

def sound_effect(text: str, duration_seconds: float | None = None, loop: bool = False) -> tuple[bytes, dict]:
    if duration_seconds is not None and not 0.5 <= duration_seconds <= 30:
        raise PermanentError("sound effect duration must be 0.5 to 30 seconds")
    model = get_config("sfx_model")
    usage = {"model": model, "cost_usd": sfx_cost(duration_seconds or 30)}
    if _local():
        from app.vendors import local_voice

        return local_voice.ambience(text, duration_seconds or 30, loop), {"model": "local:procedural", "cost_usd": 0.0}
    if dry_run():
        return fixtures.fixture_bytes("ambience_30s.mp3"), usage
    body: dict = {"text": text, "model_id": model, "loop": loop}
    if duration_seconds is not None:
        body["duration_seconds"] = duration_seconds
    resp = _request("POST", "/v1/sound-generation", params={"output_format": OUTPUT_FORMAT}, json=body)
    return resp.content, usage


# ---------- speech to text ----------

def transcribe(audio: bytes, language_code: str | None = None, duration_seconds: float = 0) -> tuple[str, dict]:
    model = get_config("stt_model")
    usage = {"model": model, "cost_usd": duration_seconds / 3600 * get_config("price_book")["stt_per_hour"]}
    if dry_run():
        return "fixture transcript", usage
    data = {"model_id": model}
    if language_code:
        data["language_code"] = language_code
    resp = _request("POST", "/v1/speech-to-text", data=data, files={"file": ("audio.mp3", audio, "audio/mpeg")})
    return resp.json()["text"], usage
