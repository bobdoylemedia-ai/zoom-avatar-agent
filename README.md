# Zoom Avatar Agent

Send an AI avatar into a Zoom, Google Meet, Microsoft Teams, or Webex meeting to
act as your agent — your face, your cloned voice, and your knowledge base.

Built on [LiveKit Agents](https://docs.livekit.io/agents/) with a
[LemonSlice](https://lemonslice.com) avatar and a [Fish Audio](https://fish.audio)
voice. Carried over from an earlier browser-based 1:1 avatar app — see
[HANDOFF.md](HANDOFF.md) for what transferred, what was left behind, and why.

## How it works

```
  src/send_to_meeting.py                src/agent.py (worker)
  ──────────────────────                ─────────────────────
  meeting URL + avatar config  ──dispatch──▶  join as a bot participant
                                              listen  → STT
                                              think   → LLM + RAG over knowledge/
                                              speak   → Fish Audio voice
                                              appear  → LemonSlice avatar video
```

Two processes: a long-running **worker** that waits for jobs, and a **dispatch**
command that sends it into a specific meeting.

## Prerequisites

- Python 3.10–3.12 and [uv](https://docs.astral.sh/uv/)
- Accounts / keys: LiveKit, LemonSlice, Fish Audio

## Setup

```bash
cp .env.example .env.local    # then fill in the keys
uv sync
```

Copy a photo of the face you want into `avatars/`. **Any size or shape works** —
it gets resized and re-oriented automatically. A clear, front-facing headshot
gives the best result; a tall crop suits the avatar frame slightly better than a
wide one, but that's a preference, not a requirement.

If you don't name a file when dispatching, the agent just uses the first image
it finds in `avatars/`.

## Run (the interface)

Double-click **`0-Open-Interface.bat`**. Your browser opens on the control panel,
and everything happens there:

1. **Start agent** in the top right, and wait for the dot to go green.
2. Pick a **preset**, or set the face, voice and knowledge base yourself.
3. Check **Attending on behalf of** — the avatar says this when it joins, uses
   it to answer "who is your owner?", and promises messages will reach that
   person by name. It defaults to `OWNER_NAME` from `.env.local` and is saved
   with the preset.
4. Paste the **meeting link** and hit **Send avatar to meeting**.

The panel also uploads photos and builds knowledge bases from documents, lists
recent meeting notes for download, and shows the agent's log when something goes
wrong. It serves on `127.0.0.1` only — never the network — because it can start
processes and send an avatar into a meeting.

The `.bat` files for the individual steps (`1-Start-Agent`, `2-Send-To-Meeting`,
`3-Meeting-Notes`, `4-Email-Test`, `Stop-Agent`) all still work if you prefer
them.

## Run (command line)

Terminal 1 — start the worker and leave it running:

```bash
uv run python src/agent.py dev
```

Terminal 2 — send it into a meeting:

```bash
uv run python src/send_to_meeting.py "https://us05web.zoom.us/j/1234567890?pwd=abc123"
```

**Zoom passcodes must be inside the URL** as `?pwd=...`. For Meet / Teams /
Webex, use the link from the calendar invite. The bot may sit in the waiting room
until the host admits it.

A fuller run:

```bash
uv run python src/send_to_meeting.py "https://us05web.zoom.us/j/1234567890?pwd=abc123" --bot-name "Jess (AI)" --image "jess.jpg" --knowledge "my-kb" --require-address
```

`--require-address` makes the avatar stay quiet unless someone says its name.
Recommended for any call with more than one other person in it — see the
turn-taking section of [HANDOFF.md](HANDOFF.md).

Add `--dry-run` to see the dispatch command without sending anything.

## Meeting notes

The agent hears everything said in the meeting, whether or not it replies -- the
"only speak when spoken to" gate suppresses the reply, not the listening. Every
meeting therefore leaves two files in `meetings/`:

| File | What |
| --- | --- |
| `<date>-<name>.jsonl` | Every turn, appended as it happens |
| `<date>-<name>.md` | The recap, written when the meeting ends |
| `<date>-<name>.pdf` | The same recap, formatted for reading |

The recap has Summary, Key points, Decisions, Action items, **For you** (questions
aimed at you, things the avatar was asked to pass along, things it couldn't
answer) and Open questions.

**Notes cannot be lost.** The `.jsonl` is written turn by turn while the meeting
happens. If the agent doesn't get to write the recap -- you stopped it mid-call,
it crashed, the window got closed -- the interface notices a transcript with no
write-up, says so, and **Write any missing notes** produces the recap, PDF and
email from it. Stopping the agent does that automatically.

Double-click `3-Meeting-Notes.bat` to open the folder, newest first.

The `.jsonl` is the source of truth and the `.md` is derived from it, so a
recap can always be rebuilt:

```bash
uv run python src/recap.py meetings/<file>.jsonl
```

Run it with no arguments to list recent transcripts. Add `--no-email` to render
without sending, or `--resend` to re-render and re-send the existing recap
without paying for another summarization.

### PDF and Word

`NOTES_FORMATS` in `.env.local` controls what gets rendered next to the markdown:
`pdf` (the default), `docx`, `both`, or `none`. Both are produced by pure-Python
libraries, so there's no Node or GTK toolchain to install.

### Emailing the notes to yourself

Set `NOTES_EMAIL_TO` in `.env.local` and the notes are emailed automatically when
each meeting ends, with the PDF attached. Leave it empty and nothing is sent.

Gmail needs a **16-character App Password**, not your account password, and
2-Step Verification has to be on: <https://myaccount.google.com/apppasswords>.
Paste it into `SMTP_PASSWORD` in `.env.local` yourself -- that file is gitignored
and the password is never logged.

Prove it works before trusting it:

```bash
uv run python src/mailer.py
```

or double-click `4-Email-Test.bat`. Emailing is best-effort by design: the
markdown recap and the raw transcript are on disk before any of this runs, so a
bad password can never cost you a meeting.

**One real limitation:** meeting audio arrives as a single mixed stream with no
per-speaker labels, so the transcript records *what was said*, not *who said it*.
The recap is instructed never to invent an attribution -- names appear only where
someone was named out loud, or where a line came from meeting chat (those do
carry a sender). If you need reliable per-speaker attribution, that has to come
from the meeting platform's own transcript, not from here.

## Give it a knowledge base

```bash
uv run python src/ingest.py my-kb ./docs
```

Accepts PDF, DOCX, TXT, and MD — a single file, several files, or a folder.
Writes `knowledge/my-kb.json` plus a vector index next to it, then pass
`--knowledge my-kb` when dispatching. Embeddings run on-device via fastembed, so
documents never leave the machine.

Indexes copied over from the browser app are already in `knowledge/` and work
as-is.

## Avatar presets

Drop a `presets.json` in the project root (same shape as the browser app's
`app/data/presets.json`) and dispatch with `--preset "<id or label>"`. Explicit
flags override preset values.

Re-point any copied preset's `image` at a local filename — the old app's URLs are
LAN addresses that LemonSlice cannot reach.

## Layout

| Path | What |
| --- | --- |
| `src/agent.py` | The worker: joins the meeting, runs the conversation loop |
| `src/rag.py` | On-device embeddings, chunking, vector retrieval |
| `src/ingest.py` | Build a knowledge base from local documents |
| `src/send_to_meeting.py` | Dispatch the avatar into a meeting |
| `avatars/` | Portrait images (gitignored) |
| `knowledge/` | Knowledge bases and their vector indexes (gitignored) |
| `src/notes.py` | Transcript capture and recap generation |
| `src/recap.py` | Rebuild a recap from a saved transcript |
| `src/export.py` | Render a recap to PDF / DOCX |
| `src/mailer.py` | Email the notes (also the SMTP self-test) |
| `src/delivery.py` | Render + email, shared by both paths |
| `src/webui.py` | The interface's web server |
| `src/webui.html` | The interface itself (one page, no build step) |
| `src/catalog.py` | Lists avatars/voices/knowledge, controls the worker |
| `src/voices.py` | Clone, design and audition Fish Audio voices |
| `brand/` | Your own logo, if you add one (gitignored) |
| `src/stop_agent.py` | Stops the agent and backfills missing notes |
| `meetings/` | Transcripts and recaps (gitignored) |
| `HANDOFF.md` | Design notes, gotchas, and open problems |

## Making it yours

The interface header is unbranded out of the box. Set `BRAND_NAME` in
`.env.local` to put your own name in it, and drop a square `logo-small.png` in
`brand/` to sit beside it — without one, no logo is shown at all. `brand/` is
gitignored, so your mark stays yours.

`OWNER_NAME` is the person the avatar stands in for. The bot's display name
follows from it (`Jane Smith (AI)`) unless you set `BOT_NAME` yourself.

## A note on disclosure

The avatar introduces itself as an AI on joining, by default. Leave that on.
Recording and participation consent laws vary by jurisdiction, and an undisclosed
AI participant is a problem in several of them.
