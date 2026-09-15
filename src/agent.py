"""LiveKit Agents worker: send a LemonSlice avatar into a Zoom / Meet / Teams /
Webex meeting, with a custom Fish Audio voice and a vector-RAG knowledge base.

Ported from two sources:
  * `07-livekit-zoom/agent.py`      -> the `join_meeting()` / `room_options()` plumbing
  * The browser avatar app's `agent.py` -> persona-from-metadata, Fish Audio TTS,
                                       local-image upload, vector RAG

Run the worker:      uv run python src/agent.py dev
Send it to a call:   uv run python src/send_to_meeting.py "<MEETING URL>"

See HANDOFF.md for the design notes and known gotchas.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import pathlib
import re
import time
from io import BytesIO

import aiohttp
from dotenv import load_dotenv
from PIL import Image, ImageOps

from livekit import agents
from livekit.agents import (
    Agent,
    AgentServer,
    AgentSession,
    TurnHandlingOptions,
    inference,
    tokenize,
    utils,
)
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
# Lines the owner sends into a meeting that is already running -- a correction,
# a fact that just changed, an instruction. A queue rather than a single file so
# two sent seconds apart cannot overwrite each other.
WHISPER_QUEUE = _ROOT / "logs" / "whisper.jsonl"

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

# The voice used when none is chosen. Voices are private to a Fish Audio
# account, so a hard-coded id only works for whoever owns it. Leave
# DEFAULT_VOICE_ID unset and the Fish plugin's own public default is used.
DEFAULT_VOICE_ID = os.getenv("DEFAULT_VOICE_ID", "").strip()

# Seconds to wait after the avatar appears before it speaks its opening line.
# Covers the meeting audio path coming up plus the AEC warmup; without it the
# avatar is heard joining mid-sentence. Override with JOIN_DELAY_SECONDS.
# How the avatar should sound. Fish Audio's s2.1-pro takes a free-form
# natural-language direction in square brackets at the head of the text and
# consumes it rather than reading it out -- verified by synthesizing each of
# these and running the audio back through Fish's own ASR, which returned the
# line without the tag. `temperature` is the plugin's expressiveness dial
# (0.7 default); the livelier tones sit above it.
TONES: dict[str, dict] = {
    "off": {
        "label": "Off (voice as trained)",
        "tag": "",
        "temperature": 0.7,
    },
    "warm": {
        "label": "Warm and engaged",
        "tag": "[warm, friendly, genuinely engaged, natural conversational energy]",
        "temperature": 0.9,
    },
    "upbeat": {
        "label": "Upbeat and bright",
        "tag": "[upbeat, bright, smiling while speaking, lively pace]",
        "temperature": 0.9,
    },
    "excited": {
        "label": "Excited and high energy",
        "tag": "[excited, high energy, enthusiastic, animated delivery]",
        "temperature": 0.95,
    },
    "professional": {
        "label": "Confident and professional",
        "tag": "[confident, clear, professional, attentive and interested]",
        "temperature": 0.85,
    },
    "calm": {
        "label": "Calm and measured",
        "tag": "[calm, measured, reassuring, unhurried]",
        "temperature": 0.8,
    },
}

# A flat, un-directed read is the thing people notice first and like least, so
# the default is a lively one rather than "off".
DEFAULT_TONE = os.getenv("AVATAR_TONE", "upbeat").strip().lower()
if DEFAULT_TONE not in TONES:
    DEFAULT_TONE = "upbeat"


class _TonedSentenceStream:
    """Prefixes each tokenized sentence with the delivery direction."""

    def __init__(self, tag: str, inner) -> None:
        self._tag = tag
        self._inner = inner

    def push_text(self, text: str) -> None:
        self._inner.push_text(text)

    def flush(self) -> None:
        self._inner.flush()

    def end_input(self) -> None:
        self._inner.end_input()

    async def aclose(self) -> None:
        await self._inner.aclose()

    def __aiter__(self):
        return self

    async def __anext__(self):
        ev = await self._inner.__anext__()
        if self._tag and ev.token and not ev.token.lstrip().startswith("["):
            ev.token = f"{self._tag} {ev.token}"
        return ev


class TonedSentenceTokenizer(tokenize.SentenceTokenizer):
    """Repeat the delivery direction on every sentence.

    The Fish plugin flushes each tokenized sentence to the websocket as its own
    synthesis unit. A direction placed once at the head of a turn therefore only
    reaches the first sentence; every sentence after it is synthesized with no
    direction at all and comes back flat -- which is exactly what a multi-
    sentence answer sounded like. Wrapping the tokenizer puts the direction on
    each unit that actually gets sent.
    """

    def __init__(self, tag: str, inner: tokenize.SentenceTokenizer | None = None) -> None:
        self._tag = (tag or "").strip()
        self._inner = inner or tokenize.blingfire.SentenceTokenizer(min_sentence_len=1)

    def tokenize(self, text: str, *, language: str | None = None) -> list[str]:
        out = self._inner.tokenize(text, language=language)
        if not self._tag:
            return out
        return [t if t.lstrip().startswith("[") else f"{self._tag} {t}" for t in out]

    def stream(self, *, language: str | None = None):
        return _TonedSentenceStream(self._tag, self._inner.stream(language=language))


def resolve_tone(name: str | None) -> tuple[str, dict]:
    """Look up a tone by id, falling back to the default rather than failing."""
    key = (name or "").strip().lower()
    if key not in TONES:
        key = DEFAULT_TONE
    return key, TONES[key]


# After the avatar speaks, a short window in which an unaddressed turn is still
# treated as meant for it. Without this the gate kills every follow-up: "Travis,
# what did we decide?" is answered, "and who's owning it?" is not, and nobody
# says the name twice in a row.
#
# The window cannot be smarter than this. Meeting audio arrives as one mixed
# stream with no speaker labels, and the Zoom attendees are not participants the
# agent can see, so there is no way to tell "they are still talking to me" from
# "they have turned to each other". It is time and a count, nothing else.
#
# FOLLOW_UP_MAX is what stops a runaway: every windowed answer restarts the
# clock, so without a cap the avatar could chain replies into a conversation it
# is not part of. Set either to 0 to switch the whole thing off.
def _int_env(name: str, default: int) -> int:
    try:
        return max(0, int(float(os.getenv(name, "").strip() or default)))
    except ValueError:
        return default


# What the avatar says when told "thanks" -- instantly, without the model.
# Set DISMISS_REPLY= (empty) in .env.local to have it stand down silently.
DISMISS_REPLY = (
    "Anytime." if os.environ.get("DISMISS_REPLY") is None
    else os.environ["DISMISS_REPLY"].strip()
)

FOLLOW_UP_SECONDS = _int_env("FOLLOW_UP_SECONDS", 15)
FOLLOW_UP_MAX = _int_env("FOLLOW_UP_MAX", 2)

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
        "tone": resolve_tone(pick("tone", DEFAULT_TONE))[0],
    }

    logger.info(
        "Session config: bot_name=%r image=%r voice_id=%s knowledge_id=%s "
        "persona_chars=%d require_address=%s announce=%s chat=%s join_delay=%.1fs "
        "tone=%s follow_up=%ss/%s",
        resolved["bot_name"], resolved["image"] or "(default)", resolved["voice_id"],
        resolved["knowledge_id"] or "(none)", len(resolved["persona"]),
        resolved["require_address"], resolved["announce"],
        resolved["listen_to_meeting_chat"], resolved["join_delay"],
        resolved["tone"], FOLLOW_UP_SECONDS, FOLLOW_UP_MAX,
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
    when someone asked it to pass a message to its owner it said "I can't relay
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
        self._address_keys = self._build_address_keys(bot_name)
        self._notes = notes
        # Monotonic, not wall clock: this measures an elapsed gap, and a clock
        # correction mid-meeting must not open or close the window by accident.
        self._last_spoke_at: float | None = None
        self._follow_ups_used = 0
        # Everything the owner has sent in during this meeting. Injected whole
        # on every turn rather than only the next one: a fact given at 10:05 is
        # still true at 10:40, and unlike the knowledge base these are never
        # subject to retrieval missing them.
        self._whispers: list[str] = []
        # Set by "thanks, Carl" or the Stop listening button; cleared the moment
        # the avatar is named again.
        self._dismissed = False

    # Generic filler, so a bot named "AI Assistant" doesn't match every mention of
    # "assistant" or the letters "ai" inside another word.
    _GENERIC = {"ai", "the", "bot", "avatar", "assistant"}

    # Speech-to-text spells an unusual name however it likes. In one call
    # "Krendall" came back as "Krendel", "Crindle" and "Crindle" again, so the
    # literal check never fired and the agent sat silent while being addressed
    # by name. All three reduce to the same consonant skeleton, which is what
    # the fallback compares.
    _DIGRAPHS = (("ph", "f"), ("ck", "k"), ("ch", "k"), ("sh", "s"),
                 ("th", "t"), ("wh", "w"), ("gh", "g"))
    _LETTERS = str.maketrans({"c": "k", "q": "k", "z": "s", "y": "i"})
    _VOWELS = "aeiou"
    # Short names produce skeletons that collide with ordinary words -- "Sam"
    # and "some" are both "sm" -- so the fallback only applies above this.
    _MIN_PHONETIC_LEN = 5

    @classmethod
    def _phonetic_key(cls, word: str) -> str:
        """A spelling-independent skeleton: digraphs folded, vowels dropped."""
        w = "".join(ch for ch in word.lower() if ch.isalpha())
        for pair, single in cls._DIGRAPHS:
            w = w.replace(pair, single)
        w = w.translate(cls._LETTERS)
        w = "".join(ch for ch in w if ch not in cls._VOWELS)
        out: list[str] = []
        for ch in w:
            if not out or out[-1] != ch:
                out.append(ch)
        return "".join(out)

    @classmethod
    def _build_address_keys(cls, bot_name: str) -> set[str]:
        """Phonetic keys for the name tokens long enough to be distinctive."""
        keys: set[str] = set()
        for token in bot_name.replace("-", " ").split():
            token = "".join(ch for ch in token.lower() if ch.isalnum())
            if len(token) < cls._MIN_PHONETIC_LEN or token in cls._GENERIC:
                continue
            key = cls._phonetic_key(token)
            if len(key) >= 3:
                keys.add(key)
        return keys

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
        if any(term in low for term in self._address_terms):
            return True
        if not self._address_keys:
            return False
        # The name was not spelled the way we expect it. Compare skeletons.
        for word in re.findall(r"[a-z']+", low):
            if (len(word) >= self._MIN_PHONETIC_LEN - 1
                    and self._phonetic_key(word) in self._address_keys):
                logger.info("Addressed as %r, matched the bot name phonetically", word)
                return True
        return False

    # Enough for a running meeting's worth of corrections without letting the
    # prompt grow without limit.
    MAX_WHISPERS = 25
    MAX_WHISPER_CHARS = 4000

    def add_whisper(self, text: str) -> None:
        """Something the owner has told the avatar mid-meeting."""
        text = (text or "").strip()
        if not text:
            return
        self._whispers.append(text)
        while len(self._whispers) > self.MAX_WHISPERS or (
            sum(len(w) for w in self._whispers) > self.MAX_WHISPER_CHARS
            and len(self._whispers) > 1
        ):
            self._whispers.pop(0)

    # Ways of saying "we're done with you". Matched only on a turn the avatar
    # was going to answer anyway, so these never wake it up -- they only send it
    # back to sleep.
    # Matched against the WHOLE remark, not searched for inside it. Searching
    # was too loose: "we're good on budget but what about staffing" contains
    # "we're good" and was being read as a goodbye, silencing the avatar in the
    # middle of a question.
    _ONE_DISMISSAL = (
        r"(?:"
        r"thanks?(?:\s+(?:so\s+much|a\s+lot|very\s+much))?"
        r"|thank\s+you(?:\s+(?:so\s+much|a\s+lot|very\s+much))?"
        r"|that\s+is\s+(?:all|it|everything)"
        r"|that\s+will\s+be\s+all"
        r"|we\s+are\s+(?:good|all\s+set|done|finished)"
        r"|nothing\s+else"
        r"|no(?:thing)?\s+more\s+questions"
        r"|you\s+can\s+go"
        r"|stand\s+down"
        r")"
    )
    # Acknowledgements that ride along in front of a goodbye. On their own they
    # are not a goodbye -- "yes" just means yes -- so they may only come before
    # one. Real transcripts from a live call that the pair-only version missed:
    #   "No. That's it. Thanks, Carl."      "No, that's good. Thanks, Carl."
    _ONE_ACK = (
        r"(?:"
        r"no|nope|yes|yeah|yep|sure|fine|good|great|perfect|cool|nice|awesome"
        r"|got\s+it|sounds\s+good|all\s+good"
        r"|that\s+is\s+(?:good|great|perfect|fine)"
        r")"
    )
    # Any run of acknowledgements and goodbyes, as long as it ENDS on a goodbye
    # and contains nothing else. Matched against the whole remark, so a real
    # question that happens to start politely never qualifies.
    _DISMISSALS = re.compile(
        rf"(?:(?:{_ONE_ACK}|{_ONE_DISMISSAL})\s+)*{_ONE_DISMISSAL}", re.I
    )

    # Contractions expanded word by word. Substring replacement would also rewrite
    # the inside of longer words.
    _EXPAND = {
        "that's": "that is", "thats": "that is", "that'll": "that will",
        "we're": "we are", "it's": "it is",
    }

    # Politeness and filler that can sit around a dismissal without changing it.
    # "much" and "so" are deliberately absent: either would eat the tail of
    _FILLER = re.compile(
        r"\b(ok|okay|alright|all\s+right|well|then|now|for\s+now|please|"
        r"everyone|everybody|guys|folks|mate|again|appreciate\s+it)\b",
        re.I,
    )

    def _is_dismissal(self, text: str) -> bool:
        """"Thanks, Carl" means stop listening. "Thanks, and what about the
        budget?" does not.

        The remark is stripped down to its bones -- contractions expanded, the
        avatar's own name removed, politeness and filler dropped -- and what is
        left has to be a dismissal and nothing else. Anything with content still
        attached is a real turn that happened to begin politely, and silencing
        the avatar on those would be worse than the problem being solved.
        """
        if "?" in text:
            return False
        words: list[str] = []
        for w in re.findall(r"[a-z']+", text.lower().replace("\u2019", "'")):
            # Drop the avatar's own name, however it was spelled.
            if w in self._address_terms or self._phonetic_key(w) in self._address_keys:
                continue
            words.extend(self._EXPAND.get(w, w).split())
        low = self._FILLER.sub(" ", " ".join(words))
        low = re.sub(r"\s+", " ", low).strip()
        if not low:
            return False
        return bool(self._DISMISSALS.fullmatch(low))

    def dismiss(self) -> None:
        """Close the follow-up window until the avatar is named again.

        A flag rather than clearing the clock, because the avatar usually
        answers the dismissal ("anytime") and that reply would otherwise
        reopen the very window it was just told to close.
        """
        self._dismissed = True
        self._follow_ups_used = 0

    def note_spoke(self) -> None:
        """The avatar has finished a turn, so the follow-up window opens now.

        Timed from when it *stops* talking, not when it starts: a fifteen-second
        answer would otherwise spend most of the window before the other person
        has had a chance to reply.
        """
        self._last_spoke_at = time.monotonic()

    def _within_follow_up_window(self) -> bool:
        """Is an unaddressed turn still plausibly aimed at the avatar?"""
        if self._dismissed:
            return False
        if not FOLLOW_UP_SECONDS or not FOLLOW_UP_MAX:
            return False
        if self._last_spoke_at is None:
            return False
        if time.monotonic() - self._last_spoke_at > FOLLOW_UP_SECONDS:
            # Lapsed. Hand the next conversation a fresh allowance.
            self._follow_ups_used = 0
            return False
        return self._follow_ups_used < FOLLOW_UP_MAX

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

        if self._require_address and self._address_terms:
            if self._is_addressed(text):
                # Named outright. Back on duty, and the allowance starts over.
                self._dismissed = False
                self._follow_ups_used = 0
            elif self._within_follow_up_window():
                self._follow_ups_used += 1
                logger.info(
                    "No name, but within %ss of speaking (follow-up %d of %d), "
                    "treating as addressed: %r",
                    FOLLOW_UP_SECONDS, self._follow_ups_used, FOLLOW_UP_MAX, text[:80],
                )
            else:
                logger.info("Turn not addressed to the bot, staying quiet: %r", text[:80])
                raise agents.StopResponse()

        if self._is_dismissal(text):
            # Stand down WITHOUT asking the model to reply. Letting "thanks"
            # through to the model, with the knowledge base attached, produced
            # a speech about what the avatar can do -- or a random fact from the
            # documents -- and took up to 13 seconds to arrive. By then the room
            # had moved on, so it sounded like the avatar waking up by itself.
            # A fixed line is instant and can't wander off-topic.
            self.dismiss()
            logger.info("Dismissed by %r; listening again only when named.", text[:60])
            if DISMISS_REPLY:
                try:
                    self.session.say(DISMISS_REPLY, allow_interruptions=True)
                except Exception as exc:  # noqa: BLE001 - never break the turn
                    logger.warning("Could not acknowledge the dismissal: %s", exc)
            raise agents.StopResponse()

        if self._whispers:
            # Ahead of the knowledge base on purpose: this is the owner
            # correcting or updating things live, so it outranks the documents.
            turn_ctx.add_message(
                role="system",
                content=(
                    "Your owner has sent you these directly during this meeting. "
                    "They are current and they override anything in your knowledge "
                    "base that disagrees. Do not read them out as a list or mention "
                    "being sent them; just use them:\n\n- "
                    + "\n- ".join(self._whispers)
                ),
            )

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


def _build_tts(config: dict):
    """Fish Audio TTS carrying the session's delivery direction."""
    _, tone = resolve_tone(config["tone"])
    opts: dict = {
        # Expressiveness. The direction shapes *how* it reads; this decides how
        # far the model is willing to move from a flat one.
        "temperature": tone["temperature"],
    }
    # No voice chosen and no DEFAULT_VOICE_ID: let the plugin use its own.
    if config["voice_id"]:
        opts["voice_id"] = config["voice_id"]
    if tone["tag"]:
        opts["tokenizer"] = TonedSentenceTokenizer(tone["tag"])
    return fishaudio.TTS(**opts)


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
        tts=_build_tts(config),
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

    assistant = MeetingAssistant(
        instructions,
        rag_index=rag_index,
        bot_name=config["bot_name"],
        require_address=config["require_address"],
        notes=notes,
    )

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
        # The avatar has just spoken, so the follow-up window opens here.
        assistant.note_spoke()

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
        agent=assistant,
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
            await _drain_whispers()
            if STOP_REQUEST.exists():
                logger.info("Stop requested; leaving the meeting and writing notes.")
                ctx.shutdown(reason="stopped from the interface")
                return

    async def _drain_whispers() -> None:
        """Take anything the owner has sent in and act on it.

        The file is read and truncated in one go, so a line cannot be handled
        twice, and a write landing during the read is picked up on the next pass
        rather than lost.
        """
        if not WHISPER_QUEUE.exists():
            return
        try:
            raw = WHISPER_QUEUE.read_text(encoding="utf-8")
            WHISPER_QUEUE.unlink()
        except OSError as exc:  # noqa: BLE001 - never let this end the meeting
            logger.warning("Could not read whispers: %s", exc)
            return

        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            # Mode first: "dismiss" is a control and carries no text, so
            # checking for text before this dropped every one of them silently.
            mode = str(item.get("mode") or "tell")
            if mode == "dismiss":
                assistant.dismiss()
                logger.info("Told to stop listening from the interface.")
                continue
            text = str(item.get("text") or "").strip()
            if not text:
                continue
            say_now = mode == "say"

            # In the transcript either way, so the recap shows what was steered.
            notes.add("room", text, speaker="sent in by the owner")

            if say_now:
                logger.info("Whisper (say now): %r", text[:120])
                session.generate_reply(
                    instructions=(
                        "Your owner has just sent you this to pass on to the "
                        "meeting. Say it now, in your own words and in character, "
                        "without mentioning that you were sent it:\n\n" + text
                    )
                )
            else:
                logger.info("Whisper (context): %r", text[:120])
                assistant.add_whisper(text)

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
