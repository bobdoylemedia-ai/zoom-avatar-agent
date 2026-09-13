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
import socket
import shutil
import subprocess
import sys
import threading
import time
import webbrowser

import psutil
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

# Loopback by default, deliberately: this page can start processes and send an
# avatar into a meeting, so anything that can reach it can do those things. Set
# ALLOW_LAN=1 in .env.local to also answer on the local network -- useful for
# driving it from a phone, and still not reachable from outside the router.
ALLOW_LAN = os.getenv("ALLOW_LAN", "").strip().lower() in {"1", "true", "yes", "on"}
HOST = "0.0.0.0" if ALLOW_LAN else "127.0.0.1"  # noqa: S104 - opt-in, see above
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
    import agent as agent_mod

    voices, voice_error = await asyncio.to_thread(catalog.list_voices)
    return JSONResponse({
        "avatars": catalog.list_avatars(),
        "voices": voices,
        "voiceError": voice_error,
        "knowledge": catalog.list_knowledge(),
        "presets": catalog.list_presets(),
        "tones": [{"id": k, "label": v["label"]} for k, v in agent_mod.TONES.items()],
        # Match the background refresh, or the list jumps from 10 to 25
        # twenty seconds after the page loads.
        "meetings": catalog.list_meetings(25),
        "worker": catalog.worker_status(),
        "pendingNotes": [p.stem for p in catalog.transcripts_without_notes()],
        "defaults": {
            # Follows the owner's name, so a fresh install reads "Jane Smith (AI)"
            # rather than somebody else's. BOT_NAME overrides it outright.
            "botName": _default_bot_name(),
            # Optional: your own name in the interface header.
            "brandName": os.getenv("BRAND_NAME", "").strip(),
            # Falls back to OWNER_NAME so the field is pre-filled rather than
            # left blank for the agent to guess at.
            "ownerName": os.getenv("OWNER_NAME", "").strip(),
            "tone": agent_mod.DEFAULT_TONE,
        },
    })


@app.get("/api/voices/refresh")
async def voices_refresh() -> JSONResponse:
    voices, err = await asyncio.to_thread(catalog.list_voices, True)
    return JSONResponse({"voices": voices, "voiceError": err})


@app.get("/rootCA.crt")
async def root_ca() -> FileResponse:
    """Hand the phone the CA that signed this site's certificate.

    Only the public certificate is ever served -- rootCA-key.pem sits next to it
    and must never leave the machine. Off unless ALLOW_LAN is set, because on
    loopback there is nothing to install it for.
    """
    if not ALLOW_LAN:
        raise HTTPException(404, "not found")
    root = _root_ca()
    if root is None:
        raise HTTPException(404, "no local certificate authority was found")
    # Served inline, deliberately. With Content-Disposition: attachment, iOS
    # Safari files it away in Downloads and nothing ever appears under Device
    # Management; served inline with this content type, Safari offers to install
    # it as a profile. FileResponse always sets a disposition, hence Response.
    from fastapi.responses import Response

    return Response(
        content=root.read_bytes(),
        media_type="application/x-x509-ca-cert",
    )


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


@app.post("/api/whisper")
async def whisper(payload: dict) -> JSONResponse:
    """Send a line into a meeting that is already running.

    Appended to a queue the worker drains twice a second while it is in a
    meeting. Appending rather than overwriting, so two sent in quick succession
    both arrive.
    """
    mode = str(payload.get("mode") or "tell")
    if mode not in ("say", "tell", "dismiss"):
        mode = "tell"
    text = str(payload.get("text") or "").strip()
    # "dismiss" is a control, not a message, so it carries no text.
    if mode != "dismiss":
        if not text:
            raise HTTPException(400, "type something to send first")
        if len(text) > 1000:
            raise HTTPException(400, "that is too long to send mid-meeting")

    worker = catalog.worker_status()
    if not worker["running"]:
        raise HTTPException(409, "The agent isn't running, so there's nothing to send to.")
    if not catalog._meeting_in_progress(catalog.tail_worker_log(600)):
        raise HTTPException(409, "The avatar isn't in a meeting right now.")

    line = json.dumps({"text": text, "mode": mode}, ensure_ascii=False)
    path = _ROOT / "logs" / "whisper.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")
    return JSONResponse({"sent": True, "mode": mode})


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

    import agent as agent_mod

    _, tone = agent_mod.resolve_tone(payload.get("tone"))
    try:
        mp3 = await asyncio.to_thread(
            voices_mod.preview,
            str(payload.get("voiceId") or ""),
            str(payload.get("text") or ""),
            tone["tag"],
            tone["temperature"],
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
        "tone": payload.get("tone") or "",
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
    if not payload.get("dryRun"):
        worker = catalog.worker_status()
        if not worker["running"]:
            raise HTTPException(409, "The agent isn't running. Start it first.")
        # Registration lags the process by a second or two. Dispatching inside
        # that gap targets an agent name LiveKit has no worker for, so the job
        # is never delivered: no avatar appears and nothing is logged.
        if not worker["registered"]:
            raise HTTPException(
                409,
                "The agent is still starting up. Wait for the dot to turn green, "
                "then send again.",
            )

    argv = [url, "--bot-name", str(payload.get("botName") or "AI Assistant")]
    for flag, key in (
        ("--image", "image"),
        ("--voice-id", "voiceId"),
        ("--knowledge", "knowledgeId"),
        ("--persona", "persona"),
        ("--owner-name", "ownerName"),
        ("--preset", "preset"),
        ("--tone", "tone"),
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


CERT_DIR = _ROOT / "certs"
CERT_FILE = CERT_DIR / "lan-cert.pem"
KEY_FILE = CERT_DIR / "lan-key.pem"
NAMES_FILE = CERT_DIR / "lan-names.json"

_MKCERT_HOME = (
    pathlib.Path(os.environ.get("LOCALAPPDATA", "")) / "mkcert" if os.name == "nt" else None
)


def _mkcert():
    """The mkcert binary and the environment it needs, if it is available."""
    if _MKCERT_HOME and _MKCERT_HOME.is_dir():
        for exe in sorted(_MKCERT_HOME.glob("mkcert*.exe")):
            # CAROOT must point at the same root CA the other apps used, or
            # mkcert makes a second one that nothing already trusts.
            return str(exe), {**os.environ, "CAROOT": str(_MKCERT_HOME)}
    found = shutil.which("mkcert")
    return (found, dict(os.environ)) if found else None


def _root_ca() -> pathlib.Path | None:
    """The mkcert root CA *certificate*. Public half only, never the key."""
    tool = _mkcert()
    if not tool:
        return None
    root = pathlib.Path(tool[1].get("CAROOT", "")) / "rootCA.pem"
    return root if root.is_file() else None


def _ensure_cert(names):
    """A certificate covering `names`, generated on demand.

    Browsers only hand over the microphone in a secure context, so recording a
    voice from a phone needs HTTPS: localhost is exempt, a LAN address is not.
    A certificate's names are fixed when it is generated, unlike the address it
    covers, so the names used are recorded beside it and the certificate is
    regenerated by itself whenever this machine's address changes.
    """
    want = sorted(names)
    if CERT_FILE.is_file() and KEY_FILE.is_file() and NAMES_FILE.is_file():
        try:
            if json.loads(NAMES_FILE.read_text(encoding="utf-8")) == want:
                return str(CERT_FILE), str(KEY_FILE)
        except (OSError, json.JSONDecodeError):
            pass  # unreadable, so regenerate

    tool = _mkcert()
    if not tool:
        return None
    exe, env = tool
    CERT_DIR.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(  # noqa: S603 - fixed binary, names come from our own interfaces
            [exe, "-cert-file", str(CERT_FILE), "-key-file", str(KEY_FILE), *want],
            cwd=str(_ROOT), env=env, check=True,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=120,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        logger.warning("Could not generate a certificate: %s", exc)
        return None
    NAMES_FILE.write_text(json.dumps(want), encoding="utf-8")
    return str(CERT_FILE), str(KEY_FILE)


def _lan_ips() -> list[str]:
    """This machine's addresses on the local network."""
    found = set()
    for addrs in psutil.net_if_addrs().values():
        for addr in addrs:
            if addr.family == socket.AF_INET and not addr.address.startswith(
                ("127.", "169.254.")
            ):
                found.add(addr.address)
    return sorted(found)


class _IgnoreClientDisconnect(logging.Filter):
    """Drop the traceback Windows prints when a client vanishes mid-connection.

    On the proactor event loop, a browser closing a TLS connection abruptly --
    a probe, a closed tab, tapping through the certificate warning -- leaves
    asyncio shutting down a socket that is already gone, and it logs the whole
    stack at ERROR. Nothing is wrong and no request is lost, but a stack trace
    in the window the user watches for real failures is worse than useless.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        exc = record.exc_info[1] if record.exc_info else None
        return not (
            isinstance(exc, (ConnectionResetError, ConnectionAbortedError))
            and getattr(exc, "winerror", None) in (10053, 10054)
        )


def main() -> int:
    import uvicorn

    logging.getLogger("asyncio").addFilter(_IgnoreClientDisconnect())

    if not PAGE.is_file():
        print(f"Missing {PAGE}", file=sys.stderr)
        return 1

    ips = _lan_ips() if ALLOW_LAN else []
    ssl_args = {}
    scheme = "http"
    if ALLOW_LAN:
        # localhost is a secure context on its own; a LAN address is not, and
        # without one the browser silently refuses the microphone, so recording
        # a voice from a phone would not work.
        cert = _ensure_cert(["localhost", "127.0.0.1", "::1", *ips])
        if cert:
            ssl_args = {"ssl_certfile": cert[0], "ssl_keyfile": cert[1]}
            scheme = "https"

    local = f"{scheme}://127.0.0.1:{PORT}"
    print(f"Interface running at {local}")
    if ALLOW_LAN:
        # Printed rather than guessed at: the address changes with the network,
        # and the one Windows reports first is often a VPN adapter.
        for ip in ips:
            print(f"  on this network:   {scheme}://{ip}:{PORT}")
        if scheme == "https":
            print("  Recording a voice from a phone needs this https:// address.")
            if _root_ca() and ips:
                print(f"  To stop the certificate warning, open this on the phone")
                print(f"  once and install it:  https://{ips[0]}:{PORT}/rootCA.crt")
        else:
            print("  WARNING: no certificate, serving plain http. A phone will")
            print("  refuse microphone access, so voice recording will not work.")
            print("  Install mkcert to fix that.")
        print("  ALLOW_LAN is on, so anyone on your network can drive this.")
    print("Close this window to shut it down.")
    threading.Timer(1.0, lambda: webbrowser.open(local)).start()
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning", **ssl_args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
