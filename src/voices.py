"""Creating and auditioning Fish Audio voices.

Three ways to make a voice, all ending up as a real model on the user's Fish
Audio account so every program that reads that account can use it:

    clone_from_audio()  an uploaded or recorded sample  -> POST /model
    design()            a text description              -> POST /v1/voice-design
    save_candidate()    keep a designed candidate        -> POST /model

Notes that matter, from the API contract:

* `visibility` defaults to **public** on POST /model, and a public model
  requires a `cover_image`. Everything here is created `private` -- these are
  the user's own voice, not something to publish by accident.
* Models are tagged so this app and the browser avatar app can both recognise
  what they created. Same tag as the older app on purpose.
* Training is asynchronous: POST /model returns `state: "created"` and becomes
  `trained` later, so cloning polls. `texts` is omitted deliberately -- Fish
  runs its own ASR on the sample, which is better than a guessed transcript.
* Voice Design returns candidates as base64 WAV in the response. They are held
  in memory here so the browser can audition them by URL and then save the one
  it liked, rather than shipping the audio back and forth.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import secrets
import threading
import time
import urllib.error
import urllib.request

logger = logging.getLogger("zoom-avatar.voices")

API = "https://api.fish.audio"

# Shared with the browser avatar app so both can tell which voices they made.
APP_VOICE_TAG = "bdm-avatar-app"

# Matches the browser app's limits, which were tuned against Fish's behaviour.
MIN_AUDIO_BYTES = 2 * 1024
MAX_AUDIO_BYTES = 15 * 1024 * 1024
MAX_SAMPLE_SECONDS = 30
MIN_SAMPLE_SECONDS = 3

TRAIN_POLL_SECONDS = 2
TRAIN_TIMEOUT_SECONDS = 90

DESIGN_MODEL = "voice-design-1"
TTS_MODEL = "s2-pro"

# Designed candidates awaiting a decision: token -> {"at", "candidates": [wav bytes]}
_designs: dict[str, dict] = {}
_designs_lock = threading.Lock()
DESIGN_TTL_SECONDS = 30 * 60


class VoiceError(RuntimeError):
    """Something the user needs to read, not a stack trace."""


def _key() -> str:
    key = os.getenv("FISH_API_KEY", "").strip()
    if not key:
        raise VoiceError("FISH_API_KEY is not set in .env.local.")
    return key


def _explain_http(exc: urllib.error.HTTPError) -> str:
    """Turn a Fish error response into something readable."""
    try:
        body = exc.read().decode("utf-8", "replace")[:400]
    except Exception:  # noqa: BLE001
        body = ""
    if exc.code == 401:
        return "Fish Audio rejected the API key (401). Check FISH_API_KEY in .env.local."
    if exc.code == 402:
        return "Fish Audio says the account is out of credit (402)."
    if exc.code == 422:
        return f"Fish Audio rejected the request (422): {body}"
    return f"Fish Audio returned {exc.code}: {body}" if body else f"Fish Audio returned {exc.code}."


def _request(method: str, path: str, *, data=None, headers=None, timeout=120):
    req = urllib.request.Request(
        f"{API}{path}",
        data=data,
        method=method,
        headers={"Authorization": f"Bearer {_key()}", **(headers or {})},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - fixed https host
            return resp.read()
    except urllib.error.HTTPError as exc:
        raise VoiceError(_explain_http(exc)) from exc
    except urllib.error.URLError as exc:
        raise VoiceError(f"Could not reach Fish Audio: {exc.reason}") from exc


def _multipart(fields: list[tuple[str, str]], files: list[tuple[str, str, bytes]]) -> tuple[bytes, str]:
    """Build a multipart/form-data body.

    Hand-rolled because POST /model needs repeated field names (`tags`, and
    `voices` for multi-sample uploads), which the stdlib has no helper for.
    """
    boundary = f"----zoomavatar{secrets.token_hex(12)}"
    out = bytearray()
    for name, value in fields:
        out += f"--{boundary}\r\n".encode()
        out += f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode()
        out += f"{value}\r\n".encode()
    for name, filename, blob in files:
        out += f"--{boundary}\r\n".encode()
        out += (
            f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
            "Content-Type: application/octet-stream\r\n\r\n"
        ).encode()
        out += blob + b"\r\n"
    out += f"--{boundary}--\r\n".encode()
    return bytes(out), f"multipart/form-data; boundary={boundary}"


# --------------------------------------------------------------------------- #
# Cloning from a sample
# --------------------------------------------------------------------------- #


def _create_model(title: str, filename: str, audio: bytes, description: str = "") -> dict:
    fields = [
        ("type", "tts"),
        ("train_mode", "fast"),
        ("title", title),
        # Never public: a public model would also require a cover_image, and
        # this is the user's own voice.
        ("visibility", "private"),
        ("tags", APP_VOICE_TAG),
    ]
    if description:
        fields.append(("description", description))
    body, content_type = _multipart(fields, [("voices", filename, audio)])
    raw = _request("POST", "/model", data=body, headers={"Content-Type": content_type}, timeout=180)
    return json.loads(raw)


def _model_state(model_id: str) -> str:
    try:
        return json.loads(_request("GET", f"/model/{model_id}", timeout=30)).get("state", "")
    except VoiceError:
        return ""  # transient; the caller keeps polling until its deadline


def clone_from_audio(
    title: str,
    audio: bytes,
    filename: str = "sample.wav",
    description: str = "",
    on_progress=None,
) -> dict:
    """Create a voice from a speech sample and wait for training.

    Returns {"id", "title", "state"}. `state` is "trained" when it finished,
    or "pending" when training is still running past the timeout -- which is
    not an error, the voice just isn't usable yet.
    """
    title = (title or "").strip()
    if not title:
        raise VoiceError("Give the voice a name.")
    if len(audio) < MIN_AUDIO_BYTES:
        raise VoiceError("That audio is empty or far too short.")
    if len(audio) > MAX_AUDIO_BYTES:
        raise VoiceError(
            f"That audio is larger than {MAX_AUDIO_BYTES // 1024 // 1024} MB. "
            f"Trim it to about {MAX_SAMPLE_SECONDS} seconds."
        )

    if on_progress:
        on_progress("Uploading the sample to Fish Audio")
    created = _create_model(title, filename, audio, description)
    model_id = created.get("_id") or created.get("id")
    if not model_id:
        raise VoiceError("Fish Audio accepted the upload but returned no voice id.")

    state = created.get("state", "created")
    deadline = time.time() + TRAIN_TIMEOUT_SECONDS
    while state not in {"trained", "failed"} and time.time() < deadline:
        if on_progress:
            left = int(deadline - time.time())
            on_progress(f"Training the voice ({left}s left before we stop waiting)")
        time.sleep(TRAIN_POLL_SECONDS)
        state = _model_state(model_id) or state

    if state == "failed":
        raise VoiceError(
            "Fish Audio could not train a voice from that sample. Try a longer, "
            "cleaner recording of continuous speech."
        )
    logger.info("Voice %r created: %s (%s)", title, model_id, state)
    return {"id": model_id, "title": title, "state": "trained" if state == "trained" else "pending"}


# --------------------------------------------------------------------------- #
# Designing from a description
# --------------------------------------------------------------------------- #


def _prune_designs() -> None:
    cutoff = time.time() - DESIGN_TTL_SECONDS
    with _designs_lock:
        for token in [t for t, d in _designs.items() if d["at"] < cutoff]:
            _designs.pop(token, None)


def design(instruction: str, reference_text: str = "", language: str = "en", n: int = 2) -> dict:
    """Generate voice candidates from a text description.

    Returns {"token", "count"}. The audio stays here; fetch each candidate with
    `candidate_audio(token, index)` and keep one with `save_candidate()`.
    """
    instruction = (instruction or "").strip()
    if not instruction:
        raise VoiceError("Describe the voice you want.")
    if len(instruction) > 2000:
        raise VoiceError("That description is too long (2000 characters maximum).")

    payload: dict = {"instruction": instruction, "n": max(1, min(int(n), 4))}
    # Optional preview line, capped by the API at 150 characters.
    if reference_text.strip():
        payload["reference_text"] = reference_text.strip()[:150]
    if language.strip():
        payload["language"] = language.strip()

    raw = _request(
        "POST",
        "/v1/voice-design",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "model": DESIGN_MODEL},
        timeout=180,
    )
    candidates = json.loads(raw).get("candidates") or []
    audio = []
    for candidate in candidates:
        blob = candidate.get("audio_base64")
        if blob:
            audio.append(base64.b64decode(blob))
    if not audio:
        raise VoiceError("Fish Audio returned no voice candidates for that description.")

    _prune_designs()
    token = secrets.token_hex(8)
    with _designs_lock:
        _designs[token] = {"at": time.time(), "candidates": audio, "instruction": instruction}
    logger.info("Designed %d candidate(s) for %r", len(audio), instruction[:60])
    return {"token": token, "count": len(audio)}


def candidate_audio(token: str, index: int) -> bytes:
    with _designs_lock:
        entry = _designs.get(token)
    if entry is None:
        raise VoiceError("Those candidates have expired. Generate them again.")
    try:
        return entry["candidates"][index]
    except (IndexError, TypeError) as exc:
        raise VoiceError("No such candidate.") from exc


def save_candidate(token: str, index: int, title: str, on_progress=None) -> dict:
    """Turn a designed candidate into a permanent voice on the account."""
    wav = candidate_audio(token, index)
    with _designs_lock:
        instruction = (_designs.get(token) or {}).get("instruction", "")
    return clone_from_audio(
        title,
        wav,
        filename="designed.wav",
        description=f"Designed from: {instruction}"[:500],
        on_progress=on_progress,
    )


# --------------------------------------------------------------------------- #
# Auditioning an existing voice
# --------------------------------------------------------------------------- #


def preview(voice_id: str, text: str = "") -> bytes:
    """Synthesize a short line in an existing voice. Returns mp3 bytes."""
    voice_id = (voice_id or "").strip()
    if not voice_id:
        raise VoiceError("Pick a voice first.")
    line = (text or "").strip() or (
        "Hi, this is how I'll sound in your meetings. I can take notes and pass "
        "messages along."
    )
    payload = {
        "text": line[:300],
        "reference_id": voice_id,
        "format": "mp3",
        "mp3_bitrate": 128,
        "latency": "normal",
    }
    return _request(
        "POST",
        "/v1/tts",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "model": TTS_MODEL},
        timeout=120,
    )


def delete_voice(voice_id: str) -> None:
    _request("DELETE", f"/model/{voice_id}", timeout=30)
    logger.info("Deleted voice %s", voice_id)
