"""Regenerate a recap from a transcript that's already on disk.

    uv run python src/recap.py meetings/<file>.jsonl

Useful when the recap failed, was overwritten, or you want to re-summarize an
older meeting. The .jsonl is the source of truth; the .md is derived from it and
can always be rebuilt. Writes alongside the transcript, replacing the .md.
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import sys

from dotenv import load_dotenv

_ROOT = pathlib.Path(__file__).resolve().parents[1]
load_dotenv(_ROOT / ".env.local")
load_dotenv(_ROOT / ".env")

import delivery  # noqa: E402
import notes as notes_mod  # noqa: E402


def _load(path: pathlib.Path) -> notes_mod.MeetingNotes:
    """Rebuild a MeetingNotes from a transcript file, pointed at the same .md."""
    meta: dict = {}
    turns: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        # A file written before filenames carried a session id can hold more than
        # one meta record, from two sessions that collided. Keep the first.
        if record.get("type") == "meta":
            meta = meta or record
        elif record.get("type") == "turn":
            turns.append(record)

    n = notes_mod.MeetingNotes(
        path.parent,
        meta.get("meeting_url", "(unknown)"),
        meta.get("bot_name", "(unknown)"),
        session_id="rebuilt",
    )
    # Point at the existing pair rather than the new timestamped names.
    n.transcript_path = path
    n.recap_path = path.with_suffix(".md")
    n._turns = turns  # noqa: SLF001 - rebuilding our own state
    return n


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        transcripts = sorted(
            (_ROOT / "meetings").glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True
        )
        if not transcripts:
            print("No transcripts in meetings/.", file=sys.stderr)
            return 1
        print("usage: recap.py <transcript.jsonl>\n\nMost recent:")
        for p in transcripts[:10]:
            print(f"  {p.name}")
        return 1

    email = "--no-email" not in argv
    resend_only = "--resend" in argv
    argv = [a for a in argv if not a.startswith("--")]
    if not argv:
        print("usage: recap.py <transcript.jsonl> [--no-email] [--resend]", file=sys.stderr)
        return 1

    path = pathlib.Path(argv[0])
    if not path.is_file():
        candidate = _ROOT / "meetings" / path.name
        if candidate.is_file():
            path = candidate
        else:
            print(f"Not found: {argv[0]}", file=sys.stderr)
            return 1

    n = _load(path)
    print(f"transcript : {path.name}")
    print(f"turns      : {n.turn_count}")
    if n.turn_count == 0:
        print("Nothing to summarize.", file=sys.stderr)
        return 1

    if resend_only:
        # Re-render and re-send the recap that already exists, without paying
        # for another summarization.
        out = n.recap_path
        if not out.is_file():
            print(f"No existing recap at {out.name}; drop --resend to generate one.", file=sys.stderr)
            return 1
    else:
        from livekit.agents import inference

        out = asyncio.run(n.write_recap(inference.LLM(model="openai/gpt-4o-mini")))
    print(f"recap      : {out}")

    got = delivery.deliver(
        out,
        meeting_url=n._meeting_url,  # noqa: SLF001 - our own object
        bot_name=n._bot_name,  # noqa: SLF001
        turn_count=n.turn_count,
        email=email,
    )
    for f in got["files"]:
        print(f"wrote      : {f.name}")
    if got["emailed_to"]:
        print(f"emailed    : {', '.join(got['emailed_to'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
