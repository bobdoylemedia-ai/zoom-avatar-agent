"""Local web interface: pick an avatar, a voice and a knowledge base, paste a
meeting link, hit Go.

    uv run python src/webui.py          # then open http://127.0.0.1:8765

Bound to 127.0.0.1 deliberately. The page can start processes, upload files and
send an avatar into a meeting, so it must not be reachable from the network.
There is no authentication precisely because it never leaves this machine.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import pathlib
import re
import secrets
import sys
import threading
import time
import webbrowser

from dotenv import load_dotenv

_ROOT = pathlib.Path(__file__).resolve().parents[1]
load_dotenv(_ROOT / ".env.local")
load_dotenv(_ROOT / ".env")

from fastapi import FastAPI, HTTPException, UploadFile  # noqa: E402
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse  # noqa: E402

import catalog  # noqa: E402
import rag  # noqa: E402
import voices as voices_mod  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("zoom-avatar.webui")

HOST = "127.0.0.1"
PORT = 8765
PAGE = pathlib.Path(__file__).with_name("webui.html")
BRAND_DIR = _ROOT / "brand"

MAX_IMAGE_BYTES = 25 * 1024 * 1024
MAX_DOC_BYTES = 50 * 1024 * 1024

app = FastAPI(title="Zoom Avatar Generator")

# Knowledge-base indexing takes tens of seconds, so it runs in a thread and the
# page polls. Keyed by job id.
_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()


def _default_bot_name() -> str:
    """What the avatar is called in the participant list, by default."""
    explicit = os.getenv("BOT_NAME", "").strip()
    if explicit:
        return explicit
    owner = os.getenv("OWNER_NAME", "").strip()
    return f"{owner} (AI)" if owner else "Avatar (AI)"


def _safe_name(name: str) -> str:
    """A filename that cannot escape its directory or surprise the shell."""
    base = pathlib.PurePath(name).name
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", base).strip("-._")
    return cleaned or f"file-{secrets.token_hex(4)}"


# --------------------------------------------------------------------------- #
# Page
# --------------------------------------------------------------------------- #


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    return HTMLResponse(PAGE.read_text(encoding="utf-8"))


@app.get("/api/state")
async def state() -> JSONResponse:
    voices, voice_error = await asyncio.to_thread(catalog.list_voices)
    return JSONResponse({
        "avatars": catalog.list_avatars(),
        "voices": voices,
        "voiceError": voice_error,
        "knowledge": catalog.list_knowledge(),
        "presets": catalog.list_presets(),
        "meetings": catalog.list_meetings(10),
        "worker": catalog.worker_status(),
        "pendingNotes": [p.stem for p in catalog.transcripts_without_notes()],
        "defaults": {
            # The bot's display name follows the owner's, so a fresh install
            # reads "Jane Smith (AI)" rather than someone else's name. BOT_NAME
            # overrides it outright.
            "botName": _default_bot_name(),
            # Falls back to OWNER_NAME so the field is pre-filled rather than
            # left blank for the agent to guess at.
            "ownerName": os.getenv("OWNER_NAME", "").strip(),
            # Optional. Set BRAND_NAME to put your own name in the header.
            "brandName": os.getenv("BRAND_NAME", "").strip(),
        },
    })


@app.get("/api/voices/refresh")
async def voices_refresh() -> JSONResponse:
    voices, err = await asyncio.to_thread(catalog.list_voices, True)
    return JSONResponse({"voices": voices, "voiceError": err})


@app.get("/brand/{name}")
async def brand_asset(name: str) -> FileResponse:
    path = BRAND_DIR / _safe_name(name)
    if not path.is_file():
        raise HTTPException(404, "no such asset")
    return FileResponse(path, headers={"Cache-Control": "public, max-age=86400"})


@app.get("/avatars/{name}")
async def avatar_image(name: str) -> FileResponse:
    path = catalog.AVATAR_DIR / _safe_name(name)
    if not path.is_file():
        raise HTTPException(404, "no such image")
    return FileResponse(path)


# --------------------------------------------------------------------------- #
# Worker
# --------------------------------------------------------------------------- #


@app.get("/api/worker")
async def worker_get() -> JSONResponse:
    return JSONResponse({
        **catalog.worker_status(),
        "log": catalog.tail_worker_log(200),
    })


@app.post("/api/worker/start")
async def worker_start() -> JSONResponse:
    return JSONResponse(await asyncio.to_thread(catalog.start_worker))


@app.post("/api/worker/stop")
async def worker_stop() -> JSONResponse:
    """Stop the worker, then make sure the notes exist.

    There is no graceful stop available on Windows (see catalog.stop_worker), so
    the reconcile afterwards is what actually guarantees the notes -- and it also
    covers crashes and closed windows.
    """
    result = await asyncio.to_thread(catalog.stop_worker)
    result["backfilled"] = await asyncio.to_thread(catalog.reconcile_notes)
    result["meetings"] = catalog.list_meetings(10)
    return JSONResponse(result)


@app.get("/api/notes/pending")
async def notes_pending() -> JSONResponse:
    return JSONResponse({
        "pending": [p.stem for p in catalog.transcripts_without_notes()]
    })


@app.post("/api/notes/reconcile")
async def notes_reconcile() -> JSONResponse:
    done = await asyncio.to_thread(catalog.reconcile_notes)
    return JSONResponse({"backfilled": done, "meetings": catalog.list_meetings(10)})


# --------------------------------------------------------------------------- #
# Uploads
# --------------------------------------------------------------------------- #


@app.post("/api/avatars")
async def upload_avatar(file: UploadFile) -> JSONResponse:
    name = _safe_name(file.filename or "avatar.jpg")
    suffix = pathlib.Path(name).suffix.lower()
    if suffix not in catalog.IMAGE_SUFFIXES:
        raise HTTPException(
            400, f"{suffix or 'that'} is not an image. Use jpg, png or webp."
        )
    data = await file.read()
    if not data:
        raise HTTPException(400, "the file was empty")
    if len(data) > MAX_IMAGE_BYTES:
        raise HTTPException(400, f"image is larger than {MAX_IMAGE_BYTES // 1024 // 1024} MB")

    # Verify it decodes before accepting it, so a broken file fails here rather
    # than mid-meeting when LemonSlice rejects it.
    try:
        from io import BytesIO

        from PIL import Image

        Image.open(BytesIO(data)).verify()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"that image could not be read ({type(exc).__name__})") from exc

    catalog.AVATAR_DIR.mkdir(parents=True, exist_ok=True)
    path = catalog.AVATAR_DIR / name
    stem, ext = path.stem, path.suffix
    n = 2
    while path.exists():
        path = catalog.AVATAR_DIR / f"{stem}-{n}{ext}"
        n += 1
    path.write_bytes(data)
    logger.info("Uploaded avatar %s (%d bytes)", path.name, len(data))
    return JSONResponse({"name": path.name, "avatars": catalog.list_avatars()})


def _ingest_job(job_id: str, kb_id: str, saved: list[tuple[pathlib.Path, str]]) -> None:
    """Extract text, build the vector index, write the knowledge record.

    `saved` pairs each staged file with the name the user actually uploaded --
    the staged filename carries a random prefix to avoid collisions, and that
    prefix must not end up in the stored document list.
    """
    import ingest

    def note(**kw) -> None:
        with _jobs_lock:
            _jobs[job_id].update(kw)

    try:
        parts, sources = [], []
        for i, (path, original) in enumerate(saved, 1):
            note(stage=f"Reading {original} ({i} of {len(saved)})")
            text = ingest._extract(path).strip()  # noqa: SLF001 - same project
            if not text:
                note(warning=f"No text found in {original} - a scanned image needs OCR.")
                continue
            parts.append(f"# {pathlib.Path(original).stem}\n\n{text}")
            sources.append({"name": original, "chars": len(text)})

        combined = "\n\n".join(parts).strip()
        if not combined:
            note(state="error", error="No readable text in those files.")
            return

        note(stage="Building the search index (this is the slow part)")
        index = rag.build_index(combined)
        rag.save_index(str(catalog.KNOWLEDGE_DIR / f"{kb_id}.index.npz"), index)
        (catalog.KNOWLEDGE_DIR / f"{kb_id}.json").write_text(
            json.dumps({
                "id": kb_id, "text": combined, "docs": sources,
                "chars": len(combined), "chunks": len(index["chunks"]),
            }, indent=2),
            encoding="utf-8",
        )
        note(
            state="done",
            stage="Ready",
            chunks=len(index["chunks"]),
            chars=len(combined),
            docs=[s["name"] for s in sources],
        )
        logger.info("Knowledge base %s built: %d chunks", kb_id, len(index["chunks"]))
    except Exception as exc:  # noqa: BLE001 - report to the page, don't crash the server
        logger.exception("Ingest failed for %s", kb_id)
        note(state="error", error=f"{type(exc).__name__}: {exc}")
    finally:
        for path, _ in saved:
            try:
                path.unlink()
            except OSError:
                pass


@app.post("/api/knowledge")
async def upload_knowledge(name: str = "", files: list[UploadFile] = None) -> JSONResponse:  # noqa: B006
    files = files or []
    if not files:
        raise HTTPException(400, "no files were sent")

    kb_id = _safe_name(name).lower() or f"kb-{secrets.token_hex(3)}"
    if (catalog.KNOWLEDGE_DIR / f"{kb_id}.json").exists():
        raise HTTPException(400, f"a knowledge base called '{kb_id}' already exists")

    staging = catalog.KNOWLEDGE_DIR / "_uploads"
    staging.mkdir(parents=True, exist_ok=True)
    saved: list[tuple[pathlib.Path, str]] = []
    total = 0
    for upload in files:
        fname = _safe_name(upload.filename or "doc.txt")
        if pathlib.Path(fname).suffix.lower() not in catalog.DOC_SUFFIXES:
            raise HTTPException(400, f"{fname}: only PDF, DOCX, TXT and MD are supported")
        data = await upload.read()
        total += len(data)
        if total > MAX_DOC_BYTES:
            raise HTTPException(400, f"those files total more than {MAX_DOC_BYTES // 1024 // 1024} MB")
        path = staging / f"{secrets.token_hex(4)}-{fname}"
        path.write_bytes(data)
        saved.append((path, fname))

    job_id = secrets.token_hex(6)
    with _jobs_lock:
        _jobs[job_id] = {
            "state": "running", "stage": "Starting", "id": kb_id,
            "files": [original for _, original in saved], "at": time.time(),
        }
    threading.Thread(target=_ingest_job, args=(job_id, kb_id, saved), daemon=True).start()
    return JSONResponse({"job": job_id, "id": kb_id})


@app.get("/api/jobs/{job_id}")
async def job_status(job_id: str) -> JSONResponse:
    with _jobs_lock:
        job = _jobs.get(job_id)
    if job is None:
        raise HTTPException(404, "no such job")
    payload = dict(job)
    if payload.get("state") == "done":
        payload["knowledge"] = catalog.list_knowledge()
    return JSONResponse(payload)


# --------------------------------------------------------------------------- #
# Making voices
# --------------------------------------------------------------------------- #


def _voice_job(job_id: str, run) -> None:
    """Run a voice operation in a thread, reporting progress to the page.

    Cloning uploads a sample and then polls Fish Audio until training finishes,
    which can take the better part of a minute -- too long to hold a request
    open, hence the job + poll shape used for knowledge indexing too.
    """
    def note(**kw) -> None:
        with _jobs_lock:
            _jobs[job_id].update(kw)

    try:
        result = run(lambda stage: note(stage=stage))
        note(state="done", stage="Ready", voice=result, voices=catalog.list_voices(True)[0])
    except voices_mod.VoiceError as exc:
        note(state="error", error=str(exc))
    except Exception as exc:  # noqa: BLE001 - never take the server down
        logger.exception("Voice job failed")
        note(state="error", error=f"{type(exc).__name__}: {exc}")


def _start_voice_job(run) -> str:
    job_id = secrets.token_hex(6)
    with _jobs_lock:
        _jobs[job_id] = {"state": "running", "stage": "Starting", "at": time.time()}
    threading.Thread(target=_voice_job, args=(job_id, run), daemon=True).start()
    return job_id


@app.post("/api/voices/clone")
async def voices_clone(name: str = "", file: UploadFile = None) -> JSONResponse:
    if file is None:
        raise HTTPException(400, "no audio was sent")
    if not name.strip():
        raise HTTPException(400, "give the voice a name")
    audio = await file.read()
    if not audio:
        raise HTTPException(400, "the audio was empty")
    if len(audio) > voices_mod.MAX_AUDIO_BYTES:
        raise HTTPException(400, "that audio is too large; trim it to about 30 seconds")

    filename = _safe_name(file.filename or "sample.wav")
    title = name.strip()
    return JSONResponse({
        "job": _start_voice_job(
            lambda progress: voices_mod.clone_from_audio(
                title, audio, filename, on_progress=progress
            )
        )
    })


@app.post("/api/voices/design")
async def voices_design(payload: dict) -> JSONResponse:
    try:
        result = await asyncio.to_thread(
            voices_mod.design,
            str(payload.get("instruction") or ""),
            str(payload.get("referenceText") or ""),
            str(payload.get("language") or "en"),
            int(payload.get("n") or 2),
        )
    except voices_mod.VoiceError as exc:
        raise HTTPException(400, str(exc)) from exc
    return JSONResponse(result)


@app.get("/api/voices/design/{token}/{index}.wav")
async def voices_design_audio(token: str, index: int):
    from fastapi.responses import Response

    try:
        wav = voices_mod.candidate_audio(token, index)
    except voices_mod.VoiceError as exc:
        raise HTTPException(404, str(exc)) from exc
    return Response(content=wav, media_type="audio/wav")


@app.post("/api/voices/design/save")
async def voices_design_save(payload: dict) -> JSONResponse:
    token = str(payload.get("token") or "")
    index = int(payload.get("index") or 0)
    title = str(payload.get("name") or "").strip()
    if not title:
        raise HTTPException(400, "give the voice a name")
    return JSONResponse({
        "job": _start_voice_job(
            lambda progress: voices_mod.save_candidate(token, index, title, on_progress=progress)
        )
    })


@app.post("/api/voices/preview")
async def voices_preview(payload: dict):
    from fastapi.responses import Response

    try:
        mp3 = await asyncio.to_thread(
            voices_mod.preview,
            str(payload.get("voiceId") or ""),
            str(payload.get("text") or ""),
        )
    except voices_mod.VoiceError as exc:
        raise HTTPException(400, str(exc)) from exc
    return Response(content=mp3, media_type="audio/mpeg")


# --------------------------------------------------------------------------- #
# Presets
# --------------------------------------------------------------------------- #


@app.post("/api/presets")
async def preset_save(payload: dict) -> JSONResponse:
    label = str(payload.get("label") or "").strip()
    if not label:
        raise HTTPException(400, "give the preset a name")
    pid = _safe_name(payload.get("id") or label).lower()
    preset = {
        "id": pid,
        "label": label,
        "image": payload.get("image") or "",
        "voiceId": payload.get("voiceId") or "",
        "voiceName": payload.get("voiceName") or "",
        "persona": payload.get("persona") or "",
        "ownerName": payload.get("ownerName") or "",
        "knowledgeId": payload.get("knowledgeId") or "",
        "agentPrompt": payload.get("agentPrompt") or "",
        "agentIdlePrompt": payload.get("agentIdlePrompt") or "",
    }
    try:
        presets = catalog.save_preset(preset)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return JSONResponse({"presets": presets, "saved": pid})


@app.delete("/api/presets/{pid}")
async def preset_delete(pid: str) -> JSONResponse:
    return JSONResponse({"presets": catalog.delete_preset(pid)})


# --------------------------------------------------------------------------- #
# Send to a meeting
# --------------------------------------------------------------------------- #


@app.post("/api/send")
async def send(payload: dict) -> JSONResponse:
    import send_to_meeting

    url = str(payload.get("meetingUrl") or "").strip()
    if not url:
        raise HTTPException(400, "paste a meeting link first")
    if not url.startswith(("http://", "https://")):
        raise HTTPException(400, "that doesn't look like a meeting link")

    # A dry run sends nothing, so don't make it depend on the agent being up.
    if not payload.get("dryRun") and not catalog.worker_status()["running"]:
        raise HTTPException(409, "The agent isn't running. Start it first.")

    argv = [url, "--bot-name", str(payload.get("botName") or "AI Assistant")]
    for flag, key in (
        ("--image", "image"),
        ("--voice-id", "voiceId"),
        ("--knowledge", "knowledgeId"),
        ("--persona", "persona"),
        ("--owner-name", "ownerName"),
        ("--preset", "preset"),
    ):
        value = str(payload.get(key) or "").strip()
        if value:
            argv += [flag, value]
    if payload.get("requireAddress"):
        argv.append("--require-address")
    if payload.get("noAnnounce"):
        argv.append("--no-announce")
    if payload.get("force"):
        argv.append("--force")
    if payload.get("dryRun"):
        # Assemble and validate everything but send nothing. Used to check the
        # wiring without spending a LemonSlice session.
        argv.append("--dry-run")

    # send_to_meeting.main prints and returns a code; capture stdout/stderr so
    # the page can show exactly what it said.
    import contextlib
    import io

    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = await asyncio.to_thread(send_to_meeting.main, argv)

    message = (out.getvalue() + err.getvalue()).strip()
    if code != 0:
        return JSONResponse({"ok": False, "message": message}, status_code=400)
    return JSONResponse({"ok": True, "message": message})


# --------------------------------------------------------------------------- #
# Meeting notes
# --------------------------------------------------------------------------- #


@app.get("/api/meetings")
async def meetings() -> JSONResponse:
    return JSONResponse({"meetings": catalog.list_meetings(25)})


@app.get("/notes/{stem}.{ext}")
async def note_file(stem: str, ext: str) -> FileResponse:
    if ext not in {"md", "pdf", "docx", "jsonl"}:
        raise HTTPException(404, "no such format")
    path = catalog.MEETINGS_DIR / f"{_safe_name(stem)}.{ext}"
    if not path.is_file():
        raise HTTPException(404, "not found")
    return FileResponse(path, filename=path.name)


def main() -> int:
    import uvicorn

    if not PAGE.is_file():
        print(f"Missing {PAGE}", file=sys.stderr)
        return 1

    url = f"http://{HOST}:{PORT}"
    print(f"Interface running at {url}")
    print("Close this window to shut it down.")
    threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
