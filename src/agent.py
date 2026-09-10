"""LiveKit Agents worker: send a LemonSlice avatar into a Zoom / Meet / Teams /
Webex meeting, with a custom Fish Audio voice and a vector-RAG knowledge base.

Ported from two sources:
  * `07-livekit-zoom/agent.py`      -> the `join_meeting()` / `room_options()` plumbing
  * BDM Real-Time Avatar `agent.py` -> persona-from-metadata, Fish Audio TTS,
                                       local-image upload, vector RAG

Run the worker:      uv run python src/agent.py dev
Send it to a call:   scripts/send-to-meeting.ps1 "<MEETING URL>"

See HANDOFF.md for the design notes and known gotchas.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import pathlib
import re
from io import BytesIO

import aiohttp
from dotenv import load_dotenv
from PIL import Image, ImageOps

from livekit import agents
from livekit.agents import Agent, AgentServer, AgentSession, TurnHandlingOptions, inference, utils
from livekit.plugins import fishaudio, lemonslice

import delivery
import mailer
import notes as notes_mod
import rag

_ROOT = pathlib.Path(__file__).resolve().parents[1]
load_dotenv(_ROOT / ".env.local")
load_dotenv(_ROOT / ".env")

KNOWLEDGE_DIR = _ROOT / "knowledge"
AVATAR_DIR = _ROOT / "avatars"
PRESETS_PATH = _ROOT / "presets.json"
MEETINGS_DIR = _ROOT / "meetings"
# Touched by the interface (or Stop-Agent.bat) to ask a running meeting to
# wind up. A file rather than a signal because Windows cannot deliver a
# console signal to a process started without a console -- see
# catalog.stop_worker.
STOP_REQUEST = _ROOT / "logs" / "stop.request"

# Cap injected knowledge to keep real-time latency snappy (~12 pages / ~7.5k tokens).
# Only used on the context-injection fallback path; RAG retrieval is per-turn.
MAX_KNOWLEDGE_CHARS = 30_000

AGENT_NAME = os.getenv("AGENT_NAME", "zoom-bot")

logger = logging.getLogger("zoom-avatar")
logger.setLevel(logging.INFO)

DEFAULT_PERSONA = """
You are an AI avatar attending a video meeting on behalf of your owner.

Keep every reply to three sentences or less — this is spoken conversation, not
writing. No markdown, no emojis, no bulleted lists.

You are in a call with other people who may also be talking to each other. If a
remark clearly is not directed at you, stay quiet rather than interjecting.

If someone gets inappropriate, steer the conversation back to acceptable topics.
""".strip()

DEFAULT_AGENT_PROMPT = "A person talking."
DEFAULT_AGENT_IDLE_PROMPT = "Between speaking, stay relaxed and natural."

# Fish Audio voice ("Melmore 2") carried over from the browser app.
DEFAULT_VOICE_ID = "8b5e9142f2184439b48dee26169d9dba"

# Seconds to wait after the avatar appears before it speaks its opening line.
# Covers the meeting audio path coming up plus the AEC warmup; without it the
# avatar is heard joining mid-sentence. Override with JOIN_DELAY_SECONDS.
DEFAULT_JOIN_DELAY = 3.5

# Who the avatar is standing in for. It gets asked "who is your owner?" and
# needs an answer, and the capability note refers to them by name.
DEFAULT_OWNER_NAME = os.getenv("OWNER_NAME", "").strip()

RAG_KNOWLEDGE_NOTE = (
    "\n\n# Reference knowledge\n"
    "You have a searchable document knowledge base. The most relevant excerpts "
    "for each message are provided to you as system messages during the "
    "conversation. Answer from them when relevant; if the answer isn't in them, "
    "say you're not certain rather than guessing."
)

def _disclosure_instructions(bot_name: str, require_address: bool, owner: str = "") -> str:
    """The opening line. The agent has to be told its own display name, or it
    improvises -- the first live test produced "you can address me as AI",
    because "AI Assistant" was the name and it had no idea."""
    name = (bot_name or "").strip()
    owner = (owner or "").strip()
    on_behalf = f"on {owner}'s behalf" if owner else "on your owner's behalf"
    lines = [
        "You have just joined the meeting. Greet everyone in one short sentence and "
        f"state plainly that you are an AI assistant attending {on_behalf}.",
    ]
    if name:
        lines.append(f'Your display name in this meeting is "{name}".')
        if require_address:
            lines.append(
                f'Add that people should say "{name}" to get your attention, since you '
                "only respond when addressed by name."
            )
    lines.append("Then stop talking and wait.")
    return " ".join(lines)



# --------------------------------------------------------------------------- #
# Session config (LiveKit dispatch metadata)
# --------------------------------------------------------------------------- #


def _join_delay(meta: dict) -> float:
    """Settle time before the opening line: dispatch metadata, else env, else
    the default. Clamped -- a negative value would be nonsense and a very large
    one just looks like the avatar is broken."""
    raw = meta.get("joinDelay")
    if raw is None:
        raw = os.getenv("JOIN_DELAY_SECONDS")
    try:
        value = float(raw) if raw not in (None, "") else DEFAULT_JOIN_DELAY
    except (TypeError, ValueError):
        logger.warning("Ignoring unreadable join delay %r", raw)
        value = DEFAULT_JOIN_DELAY
    return max(0.0, min(value, 30.0))


def _load_presets() -> dict[str, dict]:
    """Presets keyed by both id and lowercased label, for `preset` lookups."""
    try:
        raw = json.loads(PRESETS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    out: dict[str, dict] = {}
    for p in raw.get("presets", []):
        if p.get("id"):
            out[str(p["id"])] = p
        if p.get("label"):
            out[str(p["label"]).lower()] = p
    return out


def _load_session_config(ctx: agents.JobContext) -> dict:
    """Resolve this job's config from dispatch metadata, layered over a named
    preset, layered over the built-in defaults.

    Metadata keys (all optional except `meeting_url`):
        meeting_url             full join URL; for Zoom the passcode MUST be in
                                the query string (`?pwd=...`)
        bot_name                display name in the participant list
        listen_to_meeting_chat  relay meeting chat into the session (default true)
        preset                  preset id or label from presets.json
        persona                 system instructions
        voiceId                 Fish Audio reference_id
        image                   local file in avatars/, an absolute path, or an
                                http(s) URL
        knowledgeId             basename in knowledge/ (e.g. "my-kb")
        agentPrompt             LemonSlice body language while speaking
        agentIdlePrompt         LemonSlice body language while idle
        requireAddress          only reply when addressed (default false)
        announce                open with the AI-disclosure line (default true)
        joinDelay               seconds to settle before the opening line
        ownerName               who the avatar is standing in for
    """
    raw = (ctx.job.metadata or "").strip()
    meta: dict = {}
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                meta = parsed
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"Job metadata is not valid JSON ({exc}). Received: {raw!r}. "
                "If it looks cut off, whatever created the dispatch mangled it -- "
                "use src/send_to_meeting.py rather than passing JSON on a command line."
            ) from exc

    base: dict = {}
    preset_key = str(meta.get("preset") or "").strip().lower()
    if preset_key:
        presets = _load_presets()
        base = presets.get(preset_key) or presets.get(str(meta.get("preset"))) or {}
        if base:
            logger.info("Using preset %r", base.get("label") or preset_key)
        else:
            logger.warning("Preset %r not found in presets.json", meta.get("preset"))

    def pick(key: str, default):
        """Metadata wins, then the preset, then the default."""
        for src in (meta, base):
            val = src.get(key)
            if isinstance(val, str):
                val = val.strip()
            if val:
                return val
        return default

    meeting_url = str(meta.get("meeting_url") or "").strip()
    if not meeting_url:
        raise ValueError("meeting_url must be provided in job metadata")

    resolved = {
        "meeting_url": meeting_url,
        "bot_name": str(meta.get("bot_name") or "AI Assistant").strip(),
        "listen_to_meeting_chat": bool(meta.get("listen_to_meeting_chat", True)),
        "persona": pick("persona", DEFAULT_PERSONA),
        "voice_id": pick("voiceId", DEFAULT_VOICE_ID),
        "image": pick("image", ""),
        "knowledge_id": pick("knowledgeId", ""),
        "agent_prompt": pick("agentPrompt", DEFAULT_AGENT_PROMPT),
        "agent_idle_prompt": pick("agentIdlePrompt", DEFAULT_AGENT_IDLE_PROMPT),
        "require_address": bool(meta.get("requireAddress", False)),
        "announce": bool(meta.get("announce", True)),
        "join_delay": _join_delay(meta),
        "owner_name": pick("ownerName", DEFAULT_OWNER_NAME),
    }

    logger.info(
        "Session config: bot_name=%r image=%r voice_id=%s knowledge_id=%s "
        "persona_chars=%d require_address=%s announce=%s chat=%s join_delay=%.1fs",
        resolved["bot_name"], resolved["image"] or "(default)", resolved["voice_id"],
        resolved["knowledge_id"] or "(none)", len(resolved["persona"]),
        resolved["require_address"], resolved["announce"],
        resolved["listen_to_meeting_chat"], resolved["join_delay"],
    )
    return resolved


# --------------------------------------------------------------------------- #
# Knowledge base
# --------------------------------------------------------------------------- #


def _load_rag_index(knowledge_id: str) -> dict | None:
    """Load a prebuilt vector index for this knowledge base, if one exists."""
    if not knowledge_id:
        return None
    index_path = KNOWLEDGE_DIR / f"{knowledge_id}.index.npz"
    if not index_path.exists():
        logger.warning("No vector index at %s", index_path)
        return None
    try:
        index = rag.load_index(str(index_path))
        logger.info("RAG index loaded: %d chunks (knowledge_id=%s)", len(index["chunks"]), knowledge_id)
        return index
    except Exception as exc:  # noqa: BLE001 - fall back to context injection
        logger.warning("Failed to load RAG index %s: %s", knowledge_id, exc)
        return None


def _load_knowledge_text(knowledge_id: str) -> str:
    """Raw knowledge text, for the context-injection fallback when no index exists."""
    if not knowledge_id:
        return ""
    try:
        data = json.loads((KNOWLEDGE_DIR / f"{knowledge_id}.json").read_text(encoding="utf-8"))
        text = (data.get("text") or "").strip()
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Could not load knowledge %s: %s", knowledge_id, exc)
        return ""
    if len(text) > MAX_KNOWLEDGE_CHARS:
        logger.info("Knowledge %s truncated %d -> %d chars", knowledge_id, len(text), MAX_KNOWLEDGE_CHARS)
        text = text[:MAX_KNOWLEDGE_CHARS]
    return text


def _build_instructions(persona: str, knowledge: str) -> str:
    if not knowledge:
        return persona
    return (
        f"{persona}\n\n"
        "# Reference knowledge\n"
        "Use the source material below to answer questions when relevant. If the "
        "answer isn't contained in it, say you're not certain rather than guessing.\n\n"
        f"{knowledge}"
    )


def _capability_note(owner: str, notes_enabled: bool, emailed_to: bool) -> str:
    """Tell the agent what it can actually do.

    The note-taking happens entirely outside the conversation -- transcript
    capture, the recap, the PDF and the email are all invisible to the LLM. So
    when someone asked it to pass a message to Bob it said "I can't relay
    messages directly" and then "I don't have the functionality to take notes or
    send messages automatically". Both were false, and it is the one thing the
    avatar most needs to get right: a stand-in that refuses to take a message is
    worse than useless.

    Deliberately precise about the limits too, so it doesn't over-promise: it
    cannot contact anyone mid-meeting, and the notes carry no speaker labels.
    """
    who = owner.strip() or "your owner"
    if not notes_enabled:
        return (
            "\n\n# What you can do\n"
            f"You cannot take notes or pass messages on in this meeting, so if "
            f"someone asks you to get something to {who}, say plainly that you "
            "cannot and suggest they contact them directly."
        )

    delivery_line = (
        f"emailed to {who} straight after the meeting"
        if emailed_to
        else f"saved for {who} to read straight after the meeting"
    )
    return (
        "\n\n# What you can do\n"
        "This matters and people will ask you about it, so be clear and confident:\n"
        "\n"
        "- Everything said in this meeting is being transcribed, whether or not you "
        "reply to it. You are effectively taking notes the whole time.\n"
        f"- When the meeting ends, a written summary is {delivery_line}. It includes "
        "decisions, action items, open questions, and a section specifically for "
        f"{who} covering anything aimed at them, anything you were asked to pass "
        "along, and anything you were asked but could not answer.\n"
        f"- So YES, you can take a message for {who}. When someone asks you to pass "
        "something on, accept it and confirm it will reach them in your notes from "
        f"this meeting. Repeat the key details back -- names, dates, times, numbers "
        "-- so they are captured accurately.\n"
        f"- If you cannot answer something, say so and add that you will flag it for "
        f"{who} in your notes. That is the useful thing to do, not a failure.\n"
        "\n"
        "Never claim you are unable to take notes or pass on a message. You can.\n"
        "\n"
        "Be honest about the two real limits: you cannot contact anyone during the "
        f"meeting -- {who} sees your notes afterwards, not in the moment -- and you "
        "should not promise a decision or commitment on their behalf."
    )


# --------------------------------------------------------------------------- #
# Avatar image
# --------------------------------------------------------------------------- #


IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png", ".webp")


def _first_avatar_in_folder() -> pathlib.Path | None:
    """Fall back to whatever portrait is sitting in avatars/, so a first run
    works without anyone having to name a file."""
    if not AVATAR_DIR.is_dir():
        return None
    for f in sorted(AVATAR_DIR.iterdir()):
        if f.is_file() and f.suffix.lower() in IMAGE_SUFFIXES:
            return f
    return None


def _resolve_image_path(image: str) -> pathlib.Path | None:
    """Resolve `image` to a local file: a bare name in avatars/, or any path.
    With nothing specified, use the first portrait found in avatars/."""
    if not image:
        found = _first_avatar_in_folder()
        if found is not None:
            logger.info("No image specified; using avatars/%s", found.name)
        return found
    candidates = [AVATAR_DIR / image, pathlib.Path(image)]
    for c in candidates:
        try:
            if c.is_file():
                return c
        except OSError:
            continue
    return None


def _prepare_image(data: bytes) -> Image.Image:
    """Normalize bytes into what LemonSlice expects.

    Phone photos store an EXIF orientation tag rather than rotating pixel data;
    LemonSlice's re-encode ignores that tag, so bake the rotation in now or the
    avatar renders sideways. LemonSlice also caps the upload (~4 MB) and targets
    368x560, so downscale to a safe bounding box.
    """
    img = Image.open(BytesIO(data))
    img = ImageOps.exif_transpose(img)
    img = img.convert("RGB")
    img.thumbnail((640, 960))
    return img


async def _load_avatar_image(image: str) -> Image.Image | None:
    """Load the avatar portrait as pixels so the plugin can upload the bytes
    directly (`agent_image`). This is the trick that removes the Zoom example's
    requirement for a publicly-hosted LEMONSLICE_IMAGE_URL — LemonSlice never
    fetches anything, we hand it the image.

    Returns None so the caller can fall back to `agent_image_url` passthrough.
    """
    local = _resolve_image_path(image)
    if local is not None:
        try:
            return await asyncio.to_thread(lambda: _prepare_image(local.read_bytes()))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not read avatar image %s (%s)", local, exc)
            return None

    if not image.startswith(("http://", "https://")):
        raise FileNotFoundError(
            f"Avatar image {image!r} was not found. Expected it at "
            f"{AVATAR_DIR / image}, or give an absolute path or an http(s) URL. "
            "Any size or shape is fine -- it gets resized automatically."
        )

    try:
        async with aiohttp.ClientSession() as http:
            async with http.get(image, timeout=aiohttp.ClientTimeout(total=20)) as resp:
                resp.raise_for_status()
                data = await resp.read()
        return await asyncio.to_thread(_prepare_image, data)
    except Exception as exc:  # noqa: BLE001 - intentional: fall back to URL passthrough
        logger.warning("Could not fetch avatar image %s (%s); falling back to URL", image, exc)
        return None


# --------------------------------------------------------------------------- #
# Agent
# --------------------------------------------------------------------------- #


class MeetingAssistant(Agent):
    """Per-turn RAG retrieval, plus an optional 'only speak when spoken to' gate.

    The gate matters in group calls: a 1:1 agent tries to answer every utterance
    in the room, including people talking to each other. With `require_address`
    on, a turn is dropped unless the bot's name (or a direct second-person cue)
    appears in it.
    """

    def __init__(
        self,
        instructions: str,
        rag_index: dict | None = None,
        bot_name: str = "",
        require_address: bool = False,
        notes: notes_mod.MeetingNotes | None = None,
    ) -> None:
        super().__init__(instructions=instructions)
        self._rag_index = rag_index
        self._require_address = require_address
        self._address_terms = self._build_address_terms(bot_name)
        self._notes = notes

    # Generic filler, so a bot named "AI Assistant" doesn't match every mention of
    # "assistant" or the letters "ai" inside another word.
    _GENERIC = {"ai", "the", "bot", "avatar", "assistant"}

    @classmethod
    def _build_address_terms(cls, bot_name: str) -> list[str]:
        terms = {"hey assistant", "ok assistant", "hey avatar"}
        for token in bot_name.replace("-", " ").split():
            # Strip punctuation, or a display name like "Jess (AI)" yields the
            # useless term "(ai)" that matches any text containing it.
            token = "".join(ch for ch in token.lower() if ch.isalnum())
            if len(token) > 2 and token not in cls._GENERIC:
                terms.add(token)
        return sorted(terms)

    def _is_addressed(self, text: str) -> bool:
        low = text.lower()
        return any(term in low for term in self._address_terms)

    @staticmethod
    def _turn_text(new_message) -> str:
        text = getattr(new_message, "text_content", None) or ""
        if isinstance(text, list):
            text = " ".join(str(x) for x in text)
        return text.strip()

    async def on_user_turn_completed(self, turn_ctx, new_message) -> None:
        text = self._turn_text(new_message)
        if not text:
            return

        # Record BEFORE the gate. The whole point of attending on someone's
        # behalf is to capture what was said, including everything the agent
        # deliberately stays quiet about.
        if self._notes is not None:
            # Meeting chat arrives pre-tagged by the plugin as "[Sender]: text".
            chat = re.match(r"^\[([^\]]{1,80})\]:\s*(.+)$", text, re.DOTALL)
            if chat:
                self._notes.add("room", chat.group(2), speaker=f"{chat.group(1)} (chat)")
            else:
                self._notes.add("room", text)

        if self._require_address and self._address_terms and not self._is_addressed(text):
            logger.info("Turn not addressed to the bot, staying quiet: %r", text[:80])
            raise agents.StopResponse()

        if not self._rag_index:
            return

        try:
            chunks = rag.retrieve(text, self._rag_index, k=6)
        except Exception as exc:  # noqa: BLE001 - never break the turn on retrieval error
            logger.warning("RAG retrieval failed: %s", exc)
            return

        if chunks:
            turn_ctx.add_message(
                role="system",
                content=(
                    "Relevant excerpts from the knowledge base (use them to answer; "
                    "if the answer isn't here, say you're not certain):\n\n"
                    + "\n\n---\n\n".join(chunks)
                ),
            )


server = AgentServer()


@server.rtc_session(agent_name=AGENT_NAME)
async def zoom_avatar(ctx: agents.JobContext) -> None:
    config = _load_session_config(ctx)

    await ctx.connect()

    session = AgentSession(
        llm=inference.LLM(model="openai/gpt-4o-mini"),
        stt=inference.STT(
            model="deepgram/nova-3",
            language="en",
            # Interim results add churn in a multi-speaker room.
            extra_kwargs={"interim_results": False},
        ),
        # Custom voice via Fish Audio. `voice_id` is the Fish Audio model
        # reference_id; the API key is read from the FISH_API_KEY env var.
        tts=fishaudio.TTS(voice_id=config["voice_id"]),
        turn_handling=TurnHandlingOptions(
            # In a meeting we cannot see who is talking over whom, so don't try
            # to resume a reply that was cut off — it lands on top of a human.
            interruption={"resume_false_interruption": False},
        ),
    )

    avatar_kwargs = {
        "agent_prompt": config["agent_prompt"],
        "agent_idle_prompt": config["agent_idle_prompt"],
    }
    avatar_image = await _load_avatar_image(config["image"])
    if avatar_image is not None:
        avatar = lemonslice.AvatarSession(agent_image=avatar_image, **avatar_kwargs)
    else:
        # Falls back to LEMONSLICE_IMAGE_URL, which LemonSlice fetches itself and
        # which therefore must be publicly reachable.
        fallback = config["image"] or os.getenv("LEMONSLICE_IMAGE_URL") or ""
        if not fallback:
            raise ValueError(
                f"No avatar image found. Copy a photo (jpg or png) into {AVATAR_DIR} "
                "and run again -- any size is fine, it gets resized automatically."
            )
        logger.info("Using agent_image_url passthrough: %s", fallback)
        avatar = lemonslice.AvatarSession(agent_image_url=fallback, **avatar_kwargs)

    await avatar.start(session, room=ctx.room)

    await avatar.join_meeting(
        config["meeting_url"],
        bot_name=config["bot_name"],
        listen_to_meeting_chat=config["listen_to_meeting_chat"],
    )
    # Must use the avatar's own room options in meeting mode. Once join_meeting()
    # has run this returns RoomOptions(audio_input=False, audio_output=False):
    # meeting audio is fed straight into STT, bypassing LiveKit room audio. That
    # is also why there is no noise_cancellation.BVC() here as in the browser app
    # — there is no room audio input for it to filter. Extra kwargs are forwarded
    # to RoomOptions if other room-level options are ever needed.
    room_options = avatar.room_options()

    # Prefer vector RAG (per-turn retrieval); fall back to injecting raw text.
    rag_index = _load_rag_index(config["knowledge_id"])
    if rag_index is not None:
        # Warm the embedding model off the event loop so the first turn is fast.
        await asyncio.to_thread(rag.embed_texts, ["warmup"])
        instructions = config["persona"] + RAG_KNOWLEDGE_NOTE
    else:
        instructions = _build_instructions(
            config["persona"], _load_knowledge_text(config["knowledge_id"])
        )

    # Appended to whatever persona is in use, so every preset gets it.
    instructions += _capability_note(
        config["owner_name"],
        notes_enabled=True,
        emailed_to=bool(mailer.recipients()),
    )

    notes = notes_mod.MeetingNotes(
        MEETINGS_DIR,
        config["meeting_url"],
        config["bot_name"],
        session_id=getattr(ctx.job, "id", "") or "",
    )
    logger.info("Taking notes to %s", notes.transcript_path)

    # The avatar's own turns. User turns are captured in on_user_turn_completed
    # instead, because that runs before the address gate can drop them.
    @session.on("conversation_item_added")
    def _on_item(event) -> None:
        item = event.item
        if getattr(item, "role", None) != "assistant":
            return
        text = getattr(item, "text_content", None) or ""
        if isinstance(text, list):
            text = " ".join(str(x) for x in text)
        notes.add("avatar", text)

    # Runs when the meeting ends or the worker is stopped.
    async def _write_recap(*_args) -> None:
        logger.info("Meeting over; writing recap (%d turns)", notes.turn_count)
        try:
            path = await notes.write_recap(session.llm)
        except Exception as exc:  # noqa: BLE001 - shutdown must not raise
            logger.warning("Recap failed: %s", exc)
            return
        if path is None:
            return
        logger.info("Recap written: %s", path)

        # PDF / DOCX rendering and emailing are best-effort: the markdown recap
        # and the raw transcript are already safely on disk, so a failure here
        # must never look like a lost meeting.
        try:
            await asyncio.to_thread(
                delivery.deliver,
                path,
                meeting_url=config["meeting_url"],
                bot_name=config["bot_name"],
                turn_count=notes.turn_count,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Recap delivery failed: %s", exc)

    ctx.add_shutdown_callback(_write_recap)

    await session.start(
        agent=MeetingAssistant(
            instructions,
            rag_index=rag_index,
            bot_name=config["bot_name"],
            require_address=config["require_address"],
            notes=notes,
        ),
        room=ctx.room,
        room_options=room_options,
    )

    async def _leave_meeting(*_args) -> None:
        """Take the avatar out of the meeting.

        Without this the LemonSlice bot is a cloud-side participant that stays
        sitting in the call after the local worker is gone -- stopping the agent
        looked like it worked while the avatar was still visibly in the meeting.
        Registered before the notes callback so the bot leaves first and the
        summarizing happens after it is out.
        """
        try:
            await avatar.leave_meeting()
            logger.info("Left the meeting.")
        except Exception as exc:  # noqa: BLE001 - shutdown must not raise
            logger.warning("Could not leave the meeting cleanly: %s", exc)

    ctx.add_shutdown_callback(_leave_meeting)

    async def _watch_for_stop() -> None:
        """Wind up when the interface asks us to.

        ctx.shutdown() runs the shutdown callbacks, so this path leaves the
        meeting AND writes the notes properly -- unlike killing the process,
        which does neither.
        """
        while True:
            await asyncio.sleep(0.5)
            if STOP_REQUEST.exists():
                logger.info("Stop requested; leaving the meeting and writing notes.")
                ctx.shutdown(reason="stopped from the interface")
                return

    stop_watcher = asyncio.create_task(_watch_for_stop())

    async def _cancel_watcher(*_args) -> None:
        stop_watcher.cancel()

    ctx.add_shutdown_callback(_cancel_watcher)

    # Wait for the LemonSlice avatar (AGENT participant) before the first reply.
    await utils.wait_for_agent(ctx.room)

    if config["announce"]:
        # `wait_for_agent` only means the LemonSlice avatar participant exists.
        # The meeting's own audio path takes another moment to come up (the plugin
        # logs "connected to meeting relay", then "received first pcm audio frame"
        # a beat later, and there is a ~3s AEC warmup on top). Speaking into that
        # gap means the meeting drops the start of the sentence, so the avatar is
        # heard joining mid-word.
        settle = config["join_delay"]
        if settle > 0:
            logger.info("Letting the meeting audio settle for %.1fs before speaking", settle)
            await asyncio.sleep(settle)

        session.generate_reply(
            instructions=_disclosure_instructions(
                config["bot_name"], config["require_address"], config["owner_name"]
            )
        )


if __name__ == "__main__":
    agents.cli.run_app(server)
