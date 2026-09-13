"""Send the avatar into a meeting.

    uv run python src/send_to_meeting.py "<MEETING URL>" [options]

Creates a LiveKit agent dispatch whose metadata carries the meeting URL and the
avatar's configuration. The worker (src/agent.py) must already be running.

Why Python rather than the `lk` CLI: the metadata is JSON, and handing JSON to a
native executable through Windows PowerShell mangles it — the argument gets split
at the first space, so a bot name like "Jane Smith (AI)" silently truncated the
JSON mid-string and the worker crashed with "meeting_url must be provided".
Building the request in-process removes the quoting problem, and removes the CLI
as a prerequisite entirely.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import secrets
import sys
import time

from dotenv import load_dotenv
from livekit import api

_ROOT = pathlib.Path(__file__).resolve().parents[1]
load_dotenv(_ROOT / ".env.local")
load_dotenv(_ROOT / ".env")

import os  # noqa: E402 - after load_dotenv so os.getenv sees the file values


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="send_to_meeting",
        description="Send the avatar into a Zoom / Meet / Teams / Webex meeting.",
    )
    p.add_argument("meeting_url", help="Full meeting join URL. For Zoom the passcode must be in the URL (?pwd=...).")
    p.add_argument("--bot-name", default="AI Assistant", help="Display name in the participant list.")
    p.add_argument(
        "--owner-name",
        help="Who the avatar is attending for. It says this on joining and gets asked directly.",
    )
    p.add_argument("--preset", help="Preset id or label from presets.json.")
    p.add_argument("--image", help="File in avatars/, an absolute path, or an http(s) URL.")
    p.add_argument("--voice-id", help="Fish Audio reference_id.")
    p.add_argument("--knowledge", help="Knowledge base name in knowledge/ (e.g. my-kb).")
    p.add_argument("--persona", help="Inline persona override.")
    p.add_argument(
        "--tone",
        help="How the avatar should sound: off, warm, upbeat, excited, professional, calm.",
    )
    p.add_argument(
        "--require-address",
        action="store_true",
        help="Only reply when addressed by name. Recommended for calls with several people.",
    )
    p.add_argument(
        "--no-announce",
        action="store_true",
        help="Skip the 'I am an AI assistant' opening line.",
    )
    p.add_argument("--no-meeting-chat", action="store_true", help="Don't relay meeting chat to the agent.")
    p.add_argument("--dry-run", action="store_true", help="Print what would be sent and exit.")
    p.add_argument(
        "--force",
        action="store_true",
        help="Send a second avatar into a meeting one was just sent to (see _recent_dispatch).",
    )
    return p.parse_args(argv)


# Sending twice puts two avatars in one meeting: they both hear everything, both
# decide to answer, and talk over each other. It also produced two note-takers
# racing for the same recap file. One dispatch is almost always what was meant,
# so a repeat within this window is refused unless --force is given.
_REDISPATCH_WINDOW_SECONDS = 120
_STATE_PATH = _ROOT / "meetings" / ".last-dispatch.json"


def _recent_dispatch(meeting_url: str) -> float | None:
    """Seconds since this same meeting was last dispatched to, if within the
    window. None otherwise."""
    try:
        state = json.loads(_STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if state.get("meeting_url") != meeting_url:
        return None
    age = time.time() - float(state.get("at", 0))
    return age if 0 <= age < _REDISPATCH_WINDOW_SECONDS else None


def _record_dispatch(meeting_url: str) -> None:
    try:
        _STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _STATE_PATH.write_text(
            json.dumps({"meeting_url": meeting_url, "at": time.time()}), encoding="utf-8"
        )
    except OSError:
        pass  # a missed note here only costs the duplicate warning


def _build_metadata(args: argparse.Namespace) -> dict:
    meta: dict = {"meeting_url": args.meeting_url, "bot_name": args.bot_name}
    if args.owner_name:
        meta["ownerName"] = args.owner_name
    if args.preset:
        meta["preset"] = args.preset
    if args.image:
        meta["image"] = args.image
    if args.voice_id:
        meta["voiceId"] = args.voice_id
    if args.knowledge:
        meta["knowledgeId"] = args.knowledge
    if args.persona:
        meta["persona"] = args.persona
    if args.tone:
        meta["tone"] = args.tone
    if args.require_address:
        meta["requireAddress"] = True
    if args.no_announce:
        meta["announce"] = False
    if args.no_meeting_chat:
        meta["listen_to_meeting_chat"] = False
    return meta


async def _dispatch(agent_name: str, room: str, metadata: str) -> None:
    lkapi = api.LiveKitAPI()  # reads LIVEKIT_URL / _API_KEY / _API_SECRET
    try:
        result = await lkapi.agent_dispatch.create_dispatch(
            api.CreateAgentDispatchRequest(
                agent_name=agent_name,
                room=room,
                metadata=metadata,
            )
        )
    finally:
        await lkapi.aclose()
    print(f"Dispatch created: {result.id}")


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    missing = [k for k in ("LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET") if not os.getenv(k)]
    if missing:
        print(f"Missing LiveKit credentials: {', '.join(missing)}", file=sys.stderr)
        print(f"Set them in {_ROOT / '.env.local'} (see .env.example).", file=sys.stderr)
        return 1

    agent_name = os.getenv("AGENT_NAME", "zoom-bot")
    room = f"meeting-{secrets.token_hex(6)}"
    metadata = json.dumps(_build_metadata(args))

    if "zoom.us" in args.meeting_url and "pwd=" not in args.meeting_url:
        print(
            "Note: this Zoom link has no ?pwd= passcode in it. If the meeting has a\n"
            "      passcode, the bot will not be able to get in.\n",
            file=sys.stderr,
        )

    print(f"livekit  : {os.getenv('LIVEKIT_URL')}")
    print(f"agent    : {agent_name}")
    print(f"room     : {room}")
    print(f"meeting  : {args.meeting_url}")
    print(f"metadata : {metadata}")
    print()

    if args.dry_run:
        print("(dry run - nothing sent)")
        return 0

    age = None if args.force else _recent_dispatch(args.meeting_url)
    if age is not None:
        print(
            f"An avatar was already sent to this meeting {int(age)} seconds ago.\n"
            "Sending another would put two avatars in the call, talking over each other.\n"
            "\n"
            "If the first one never appeared, check the worker window for the error\n"
            "instead of sending again. To send a second on purpose, add --force.",
            file=sys.stderr,
        )
        return 1

    try:
        asyncio.run(_dispatch(agent_name, room, metadata))
    except Exception as exc:  # noqa: BLE001 - surface a readable reason, not a traceback
        print(f"\nDispatch failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        print("Is the worker running (1-Start-Agent.bat)?", file=sys.stderr)
        return 1

    _record_dispatch(args.meeting_url)

    print()
    print("Sent. Watch the worker window for the join.")
    print("If nothing appears in the meeting, check that window for errors.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
