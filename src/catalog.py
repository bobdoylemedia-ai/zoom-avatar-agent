"""What the interface needs to know about: avatars, voices, knowledge bases,
presets, and the worker process.

Kept separate from the HTTP layer in `webui.py` so it can be exercised without
starting a server.
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
import subprocess
import sys
import time

import psutil

logger = logging.getLogger("zoom-avatar.catalog")

_ROOT = pathlib.Path(__file__).resolve().parents[1]
AVATAR_DIR = _ROOT / "avatars"
KNOWLEDGE_DIR = _ROOT / "knowledge"
MEETINGS_DIR = _ROOT / "meetings"
LOG_DIR = _ROOT / "logs"
PRESETS_PATH = _ROOT / "presets.json"
VOICE_CACHE = _ROOT / "logs" / "voices.cache.json"
WORKER_LOG = LOG_DIR / "worker.log"
STOP_REQUEST = LOG_DIR / "stop.request"

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}
DOC_SUFFIXES = {".pdf", ".docx", ".txt", ".md", ".markdown"}
VOICE_CACHE_TTL = 6 * 60 * 60  # 6 hours; the voice list rarely changes


# --------------------------------------------------------------------------- #
# Avatars
# --------------------------------------------------------------------------- #


def list_avatars() -> list[dict]:
    if not AVATAR_DIR.is_dir():
        return []
    out = []
    for f in sorted(AVATAR_DIR.iterdir()):
        if f.is_file() and f.suffix.lower() in IMAGE_SUFFIXES:
            out.append({"name": f.name, "size": f.stat().st_size})
    return out


# --------------------------------------------------------------------------- #
# Knowledge bases
# --------------------------------------------------------------------------- #


def _backfill_chunk_count(record_path: pathlib.Path, data: dict) -> int:
    """Chunk count for a base carried over from the browser app, whose records
    predate the `chunks` field. Read it from the index once and write it back, so
    this costs nothing on later listings."""
    index_path = record_path.parent / f"{record_path.stem}.index.npz"
    if not index_path.exists():
        return 0
    try:
        import numpy as np

        with np.load(index_path, allow_pickle=True) as blob:
            count = int(blob["vectors"].shape[0])
    except Exception as exc:  # noqa: BLE001 - a missing count is cosmetic
        logger.warning("Could not read chunk count for %s: %s", record_path.stem, exc)
        return 0
    try:
        data["chunks"] = count
        record_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError:
        pass
    return count


def list_knowledge() -> list[dict]:
    if not KNOWLEDGE_DIR.is_dir():
        return []
    out = []
    for j in sorted(KNOWLEDGE_DIR.glob("*.json")):
        try:
            data = json.loads(j.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        docs = data.get("docs") or []
        out.append({
            "id": j.stem,
            "chars": data.get("chars") or len(data.get("text", "")),
            "chunks": data.get("chunks") or _backfill_chunk_count(j, data),
            # A base with no vector index still works (the agent falls back to
            # injecting the raw text), but retrieval is better with one.
            "indexed": (KNOWLEDGE_DIR / f"{j.stem}.index.npz").exists(),
            "docs": [d.get("name", "?") for d in docs if isinstance(d, dict)],
        })
    return out


# --------------------------------------------------------------------------- #
# Presets
# --------------------------------------------------------------------------- #


def list_presets() -> list[dict]:
    try:
        data = json.loads(PRESETS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return [p for p in data.get("presets", []) if isinstance(p, dict)]


def save_preset(preset: dict) -> list[dict]:
    """Insert or replace a preset by id. Returns the full list."""
    presets = list_presets()
    pid = str(preset.get("id") or "").strip()
    if not pid:
        raise ValueError("preset needs an id")
    presets = [p for p in presets if str(p.get("id")) != pid]
    presets.insert(0, preset)
    PRESETS_PATH.write_text(json.dumps({"presets": presets}, indent=2), encoding="utf-8")
    return presets


def delete_preset(pid: str) -> list[dict]:
    presets = [p for p in list_presets() if str(p.get("id")) != str(pid)]
    PRESETS_PATH.write_text(json.dumps({"presets": presets}, indent=2), encoding="utf-8")
    return presets


# --------------------------------------------------------------------------- #
# Fish Audio voices
# --------------------------------------------------------------------------- #


def _read_voice_cache() -> list[dict] | None:
    try:
        blob = json.loads(VOICE_CACHE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if time.time() - float(blob.get("at", 0)) > VOICE_CACHE_TTL:
        return None
    return blob.get("voices") or None


def _write_voice_cache(voices: list[dict]) -> None:
    try:
        VOICE_CACHE.parent.mkdir(parents=True, exist_ok=True)
        VOICE_CACHE.write_text(
            json.dumps({"at": time.time(), "voices": voices}), encoding="utf-8"
        )
    except OSError:
        pass


def list_voices(refresh: bool = False) -> tuple[list[dict], str | None]:
    """Your Fish Audio voice models. Returns (voices, error).

    Cached for a few hours so opening the interface doesn't wait on the network,
    and so a Fish outage leaves the dropdown populated. On a cache miss with no
    network the error is returned for the page to show.
    """
    if not refresh:
        cached = _read_voice_cache()
        if cached is not None:
            return cached, None

    key = os.getenv("FISH_API_KEY", "").strip()
    if not key:
        return _read_voice_cache() or [], "FISH_API_KEY is not set in .env.local"

    try:
        import urllib.error
        import urllib.request

        req = urllib.request.Request(
            "https://api.fish.audio/model?self=true&page_size=100",
            headers={"Authorization": f"Bearer {key}"},
        )
        with urllib.request.urlopen(req, timeout=20) as resp:  # noqa: S310 - fixed https host
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 - fall back to whatever we have
        logger.warning("Could not list Fish Audio voices: %s", exc)
        return _read_voice_cache() or [], f"{type(exc).__name__}: {exc}"

    items = payload.get("items") if isinstance(payload, dict) else payload
    voices = []
    for m in items or []:
        vid = m.get("_id") or m.get("id")
        if not vid:
            continue
        voices.append({"id": vid, "title": m.get("title") or "(untitled)"})
    voices.sort(key=lambda v: v["title"].lower())
    if voices:
        _write_voice_cache(voices)
    return voices, None


# --------------------------------------------------------------------------- #
# Meeting notes
# --------------------------------------------------------------------------- #


def list_meetings(limit: int = 25) -> list[dict]:
    if not MEETINGS_DIR.is_dir():
        return []
    rows = []
    for j in sorted(
        MEETINGS_DIR.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True
    )[:limit]:
        stem = j.stem
        turns = 0
        bot = ""
        try:
            for line in j.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                rec = json.loads(line)
                if rec.get("type") == "turn":
                    turns += 1
                elif rec.get("type") == "meta" and not bot:
                    bot = rec.get("bot_name", "")
        except (OSError, json.JSONDecodeError):
            pass
        rows.append({
            "stem": stem,
            "bot_name": bot,
            "turns": turns,
            "at": j.stat().st_mtime,
            "formats": [
                ext for ext in ("md", "pdf", "docx")
                if (MEETINGS_DIR / f"{stem}.{ext}").exists()
            ],
        })
    return rows


# --------------------------------------------------------------------------- #
# The worker process
# --------------------------------------------------------------------------- #


def _worker_procs() -> list[psutil.Process]:
    """Every running agent worker for THIS project.

    Matched on the command line rather than a pid file, so a worker started from
    1-Start-Agent.bat is found too -- and so two workers racing for the same
    dispatch can be detected.
    """
    found = []
    for proc in psutil.process_iter(["pid", "name", "cmdline", "create_time"]):
        try:
            cmdline = proc.info.get("cmdline") or []
            if not cmdline:
                continue
            joined = " ".join(cmdline).replace("\\", "/")
            if "src/agent.py" in joined and (proc.info.get("name") or "").lower().startswith(
                ("python", "uv")
            ):
                found.append(proc)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return found


def worker_status() -> dict:
    procs = _worker_procs()
    # `uv run` spawns a child python, so one logical worker shows as two
    # processes. Report the oldest as the start time and count distinctly.
    started = min((p.info.get("create_time") or 0) for p in procs) if procs else None
    registered = False
    log_tail = tail_worker_log(400)
    if log_tail:
        # The worker prints this once it has registered with LiveKit Cloud;
        # "running" and "ready to take a meeting" are not the same thing.
        last_registered = log_tail.rfind("registered worker")
        last_exit = max(log_tail.rfind("Worker stopped"), log_tail.rfind("worker exiting"))
        registered = last_registered > -1 and last_registered > last_exit
    return {
        "running": bool(procs),
        "registered": bool(procs) and registered,
        "pids": [p.info["pid"] for p in procs],
        "started_at": started,
    }


def tail_worker_log(lines: int = 120) -> str:
    try:
        text = WORKER_LOG.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return "\n".join(text.splitlines()[-lines:])


def start_worker() -> dict:
    """Start the worker, logging to logs/worker.log.

    Logging to a file rather than a console window is deliberate: the interface
    can then show the log, so a failure is visible where the user already is
    instead of in a black window behind the browser.
    """
    if _worker_procs():
        return {"started": False, "reason": "already running", **worker_status()}

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    # Clear any leftover stop request, or the new worker would wind up the
    # moment it picks up a job.
    STOP_REQUEST.unlink(missing_ok=True)
    # Truncate per start so the log shown in the UI is this run, not history.
    log = WORKER_LOG.open("w", encoding="utf-8", errors="replace")
    # No console window. That does mean the worker cannot be asked to shut down
    # cleanly (see stop_worker), so the notes are guaranteed by reconcile_notes()
    # instead of by a shutdown handler.
    creation = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    # Run the agent on THIS interpreter, not via `uv run`. The interface is
    # already running inside the project venv, which has every dependency the
    # agent needs -- and that venv has no `uv` module of its own, so going back
    # through uv fails with "No module named uv".
    subprocess.Popen(  # noqa: S603 - fixed command, no user input
        [sys.executable, "src/agent.py", "dev"],
        cwd=str(_ROOT),
        stdout=log,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        creationflags=creation,
    )
    # Give it a moment so the caller's immediate status read isn't a false "off".
    for _ in range(20):
        time.sleep(0.25)
        if _worker_procs():
            break
    return {"started": True, **worker_status()}


def stop_worker(grace_seconds: int = 75) -> dict:
    """Stop the agent: take the avatar out of the meeting, let it write its
    notes, then end the process.

    Asks first, via a `logs/stop.request` file that a running meeting polls for.
    That path calls `ctx.shutdown()` inside the worker, which leaves the meeting
    through the LemonSlice API and runs the notes callbacks -- neither of which a
    killed process does.

    A file rather than a signal because Windows can only ask a process to exit
    via a console control event, and the worker is started without a console
    (measured: CTRL_BREAK was ignored and it had to be killed after 40s). The
    grace period has to cover leaving the meeting, an LLM summary, a PDF render
    and an SMTP send.

    `reconcile_notes()` remains the backstop for anything that still slips
    through -- a crash, a closed window, a power cut.
    """
    procs = _worker_procs()
    if not procs:
        STOP_REQUEST.unlink(missing_ok=True)
        return {"stopped": 0, "forced": False, "asked": False, **worker_status()}

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    STOP_REQUEST.write_text("stop", encoding="utf-8")

    # Only a worker that is actually in a meeting has the watcher polling that
    # file, so an idle one is stopped straight away rather than waited on.
    in_meeting = "Taking notes to" in tail_worker_log(600)
    wait = grace_seconds if in_meeting else 0

    gone, alive = psutil.wait_procs(procs, timeout=wait) if wait else ([], procs)
    for proc in alive:
        logger.info("Worker %s still up after the wind-up window; stopping it.", proc.pid)
        try:
            proc.terminate()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    gone2, alive2 = psutil.wait_procs(alive, timeout=8)
    for proc in alive2:
        try:
            proc.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    psutil.wait_procs(alive2, timeout=5)

    STOP_REQUEST.unlink(missing_ok=True)
    return {
        "stopped": len(gone) + len(alive),
        # True only when it was in a meeting and still did not wind up on its
        # own; the notes then come from reconcile_notes() rather than the agent.
        # An idle worker is always "forced" and that is unremarkable, so it is
        # reported as False.
        "forced": bool(alive) and in_meeting,
        "asked": True,
        "wasInMeeting": in_meeting,
        **worker_status(),
    }


# --------------------------------------------------------------------------- #
# Making sure notes exist
# --------------------------------------------------------------------------- #


def transcripts_without_notes() -> list[pathlib.Path]:
    """Transcripts that hold turns but never got a recap written."""
    if not MEETINGS_DIR.is_dir():
        return []
    out = []
    for j in sorted(MEETINGS_DIR.glob("*.jsonl")):
        if (MEETINGS_DIR / f"{j.stem}.md").exists():
            continue
        try:
            has_turns = any(
                '"type": "turn"' in line or '"type":"turn"' in line
                for line in j.read_text(encoding="utf-8").splitlines()
            )
        except OSError:
            continue
        if has_turns:
            out.append(j)
    return out


def reconcile_notes() -> list[dict]:
    """Write notes for every transcript that is missing them, and deliver them.

    This is the safety net that makes the notes unconditional. A hard kill, a
    crash, a closed console window or a power cut all leave the transcript on
    disk; this turns it into a recap, a PDF and an email after the fact.
    """
    import recap

    results = []
    for path in transcripts_without_notes():
        logger.info("Backfilling notes for %s", path.name)
        try:
            code = recap.main([str(path)])
            results.append({"stem": path.stem, "ok": code == 0})
        except Exception as exc:  # noqa: BLE001 - report per transcript
            logger.warning("Could not backfill %s: %s", path.name, exc)
            results.append({"stem": path.stem, "ok": False, "error": str(exc)})
    return results
