"""Meeting transcript capture and end-of-meeting recap.

The agent hears everything in the meeting regardless of whether it answers --
speech-to-text runs on all meeting audio, and the "only speak when addressed"
gate suppresses the *reply*, not the listening. This module writes that down and
turns it into something worth reading afterwards.

Two files per meeting, in meetings/:
    <stamp>-<slug>-<session>.jsonl   every turn, appended as it happens
    <stamp>-<slug>-<session>.md      the recap, written when the meeting ends

The .jsonl is appended line by line rather than held in memory and dumped at the
end, so a crashed or force-closed session still leaves a usable record.

Two details here are load-bearing, both learned the hard way:

* The filename carries a per-session id. Without it, two sessions starting in
  the same minute shared a filename, interleaved their turns into one file, and
  the session that recorded nothing overwrote the real recap with "Nothing was
  said while the avatar was in the meeting."
* Files are created lazily, on the first recorded turn, so a session that hears
  nothing leaves nothing behind instead of littering meetings/ with empty
  transcripts and vacuous recaps.

A real limitation to know about: spoken audio arrives from the meeting as a
single mixed stream with no per-speaker labels, so transcript lines are "what was
said", not "who said it". Meeting chat messages are the exception -- those arrive
already tagged as "[Sender]: text".
"""

from __future__ import annotations

import json
import logging
import pathlib
import re
import secrets
from datetime import datetime, timezone

logger = logging.getLogger("zoom-avatar.notes")

RECAP_SYSTEM_PROMPT = """
You are writing up notes from a meeting that an AI avatar attended on behalf of
its owner, who was not present. The owner will read only your notes, so they must
stand alone.

You are given the transcript as alternating turns. "room" is speech heard in the
meeting; "avatar" is what the AI avatar itself said.

Important: the room audio has no speaker labels. You genuinely do not know who
said what, unless a line is a chat message tagged "[Name]: ...", or someone is
named out loud in the conversation. NEVER invent an attribution. Write "someone
asked" or "it was suggested" rather than guessing a name.

Write GitHub-flavoured markdown with these sections, and omit any section that
would be empty rather than writing "none":

## Summary
Two to four sentences. What was this meeting actually about, and what came of it.

## Key points
Bullets. Substance only -- skip pleasantries and small talk.

## Decisions
Bullets. Only things actually settled. If something was discussed but left open,
it belongs under Open questions, not here.

## Action items
Bullets. Each one: what needs doing, and who it fell to if that was stated.

## For you
Bullets. Things the owner specifically needs to see: questions aimed at them,
commitments the avatar declined to make on their behalf, anything the avatar was
asked to pass along, and anything it was asked but could not answer.

## Open questions
Bullets. Raised but unresolved.

Be concise and factual. Do not pad. If the transcript is too short or too garbled
to support notes, say exactly that in one line under ## Summary and stop.
""".strip()


def _slug(text: str, limit: int = 40) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s[:limit] or "meeting"


class MeetingNotes:
    """Append-only transcript for one meeting, plus recap generation."""

    def __init__(
        self,
        out_dir: pathlib.Path,
        meeting_url: str,
        bot_name: str,
        session_id: str = "",
    ) -> None:
        # `session_id` must be unique per agent session -- pass the LiveKit job
        # id. Without it, two sessions starting in the same minute produced the
        # same filename, interleaved their turns into one file, and the one that
        # recorded nothing overwrote the good recap.
        suffix = _slug(session_id, 12) if session_id else secrets.token_hex(3)
        stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        base = f"{stamp}-{_slug(bot_name)}-{suffix}"
        self._out_dir = out_dir
        self.transcript_path = out_dir / f"{base}.jsonl"
        self.recap_path = out_dir / f"{base}.md"
        self._meeting_url = meeting_url
        self._bot_name = bot_name
        self._turns: list[dict] = []
        self._started = datetime.now(timezone.utc)
        self._file_started = False

    def _append(self, record: dict) -> None:
        try:
            # Create the directory and write the meta header only once there is
            # actually something to record, so a session that hears nothing
            # leaves no files behind at all.
            if not self._file_started:
                self._out_dir.mkdir(parents=True, exist_ok=True)
                meta = {
                    "type": "meta",
                    "meeting_url": self._meeting_url,
                    "bot_name": self._bot_name,
                    "started_at": self._started.isoformat(),
                }
                with self.transcript_path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(meta, ensure_ascii=False) + "\n")
                self._file_started = True
            with self.transcript_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError as exc:  # noqa: BLE001 - never let note-taking break the call
            logger.warning("Could not write transcript line: %s", exc)

    def add(self, role: str, text: str, speaker: str | None = None) -> None:
        """Record one turn. `role` is "room" (heard in the meeting) or "avatar"."""
        text = (text or "").strip()
        if not text:
            return
        turn = {
            "type": "turn",
            "role": role,
            "text": text,
            "at": datetime.now(timezone.utc).isoformat(),
        }
        if speaker:
            turn["speaker"] = speaker
        self._turns.append(turn)
        self._append(turn)

    @property
    def turn_count(self) -> int:
        return len(self._turns)

    def as_prompt(self, max_chars: int = 120_000) -> str:
        """The transcript as text for the summarizer. Keeps the tail if oversized,
        since the end of a meeting usually carries the decisions."""
        lines = []
        for t in self._turns:
            who = t.get("speaker") or ("avatar" if t["role"] == "avatar" else "room")
            lines.append(f"{who}: {t['text']}")
        body = "\n".join(lines)
        if len(body) > max_chars:
            body = "[...earlier conversation omitted...]\n" + body[-max_chars:]
        return body

    async def write_recap(self, llm) -> pathlib.Path | None:
        """Summarize the transcript and write the .md recap. Returns its path, or
        None if there was nothing worth writing up."""
        from livekit.agents import llm as llm_mod

        header = (
            f"# Meeting notes\n\n"
            f"- **Attended by:** {self._bot_name} (AI avatar)\n"
            f"- **Started:** {self._started.astimezone().strftime('%Y-%m-%d %H:%M %Z')}\n"
            f"- **Meeting:** {self._meeting_url}\n"
            f"- **Turns recorded:** {self.turn_count}\n"
            f"- **Transcript:** `{self.transcript_path.name}`\n\n"
            "> Spoken audio from the meeting has no speaker labels, so this write-up\n"
            "> generally cannot say who said what. Names appear only where someone was\n"
            "> named out loud or a line came from meeting chat.\n\n"
            "---\n\n"
        )

        if self.turn_count == 0:
            # Write nothing at all. A session that heard nothing used to emit a
            # "Nothing was said" recap which -- before filenames carried a
            # session id -- overwrote the real notes from a concurrent session.
            logger.info("No turns recorded; not writing a recap.")
            return None

        chat_ctx = llm_mod.ChatContext.empty()
        chat_ctx.add_message(role="system", content=RECAP_SYSTEM_PROMPT)
        chat_ctx.add_message(role="user", content=f"Transcript:\n\n{self.as_prompt()}")

        chunks: list[str] = []
        try:
            stream = llm.chat(chat_ctx=chat_ctx)
            async for chunk in stream:
                delta = getattr(chunk, "delta", None)
                if delta is not None and delta.content:
                    chunks.append(delta.content)
            await stream.aclose()
        except Exception as exc:  # noqa: BLE001 - still leave the raw transcript behind
            logger.warning("Recap generation failed: %s", exc)
            self.recap_path.write_text(
                header
                + f"## Summary\n\nThe recap could not be generated ({type(exc).__name__}: {exc}).\n"
                f"The full transcript is still in `{self.transcript_path.name}`.\n",
                encoding="utf-8",
            )
            return self.recap_path

        body = "".join(chunks).strip()
        if not body:
            body = "## Summary\n\nThe summarizer returned nothing usable. See the raw transcript."
        self.recap_path.write_text(header + body + "\n", encoding="utf-8")
        return self.recap_path
