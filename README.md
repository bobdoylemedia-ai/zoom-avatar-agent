# Zoom Avatar Agent

Send an AI avatar into a Zoom, Google Meet, Microsoft Teams or Webex meeting to
act as your stand-in — your face, your cloned voice, and your knowledge base —
and steer it while it's there.

Built on [LiveKit Agents](https://docs.livekit.io/agents/), with voices from
[Fish Audio](https://fish.audio) and the animated face from
[LemonSlice](https://lemonslice.com). [HANDOFF.md](HANDOFF.md) has the design
notes and every gotcha found along the way.

## What it does

- **Joins real meetings** as a participant, announces itself as an AI, and holds
  a two-way conversation.
- **Speaks in any voice on your Fish Audio account**, with a **tone** you choose —
  warm, upbeat, excited, professional or calm.
- **Creates new voices from the control panel**: describe one in words, record 30
  seconds, or upload a clip and trim it.
- **Only speaks when spoken to**, keeps listening briefly for follow-up
  questions, and goes quiet again when someone says "thanks".
- **Lets you send it lines mid-meeting** — facts that just changed, or things to
  say out loud — without anyone in the call seeing you do it.
- **Takes notes** on everything said, and emails you a recap with a "For you"
  section when the meeting ends.
- **Answers from your documents**, searched on your own machine.

## How it works

```
  control panel / src/send_to_meeting.py        src/agent.py (worker)
  ──────────────────────────────────────        ─────────────────────
  meeting link + avatar settings  ──dispatch──▶  joins as a participant
                                                 listens → speech-to-text
                                                 thinks  → LLM + your documents
                                                 speaks  → Fish Audio voice + tone
                                                 appears → LemonSlice avatar video
```

Two processes: a long-running **worker** that waits for jobs, and a **dispatch**
that sends it into a specific meeting. The control panel starts and stops the
worker for you.

## Before you start

- **Python 3.10–3.12** and **[uv](https://docs.astral.sh/uv/)**. Built and tested on Windows;
  the Python should run elsewhere, but only Windows has been tried.
- Accounts and API keys for:
  - **[LiveKit Cloud](https://cloud.livekit.io)** — transport, plus the speech-to-text
    and language model, billed through LiveKit
  - **[Fish Audio](https://fish.audio/app/api-keys)** — the voice
  - **[LemonSlice](https://lemonslice.com)** — the avatar video
- Optional: a Gmail app password, if you want notes emailed to you

The `.bat` launchers are for Windows. On other systems, use the command-line
equivalents below.

## Setup

1. Copy `.env.example` to `.env.local` and fill in your keys. **Never commit
   `.env.local`** — it's already in `.gitignore`.
2. Install everything:

   ```bash
   uv sync
   ```

3. Put a photo of the face you want in `avatars/`. Any size or shape works; it's
   resized and rotated automatically. A clear, front-facing headshot looks best.

## Run it

Double-click **`0-Open-Interface.bat`**, or run `uv run python src/webui.py`.
Your browser opens on the control panel.

1. **Start agent** (top right) and wait for the dot to turn green. Sending before
   it's green is refused — a worker that is running but not yet registered would
   silently drop the job.
2. Choose a **face**, a **voice**, a **tone** and a **knowledge base** — or load a
   **preset**, and save your own with **Save preset**.
3. Set **Attending on behalf of** — the avatar says who it stands in for, and
   promises messages will reach that person by name.
4. Paste the **meeting link** and click **Send avatar to meeting**.

**Zoom passcodes must be inside the link** (`?pwd=...`). The avatar may wait in the
waiting room until the host admits it.

**Stop** takes the avatar out of the call, waits for it to write its notes, then
shuts the worker down.

## The voice

### Choosing a voice and a tone

The **Voice** list is every voice on your Fish Audio account. **Hear this voice**
plays a short sample using the tone you've selected.

**Tone** changes how the voice delivers its lines. Fish Audio's S2.1 Pro model
accepts a written direction in square brackets — `[upbeat, bright, smiling while
speaking]` — and performs it without reading it out loud. The app adds that
direction to every sentence the avatar speaks, so the energy holds through a
whole answer. Set the default with `AVATAR_TONE` in `.env.local`.

### Creating a voice

Click **Create a new voice…** and pick one of three ways:

| Tab | How |
| --- | --- |
| **Describe it** | Write a description, click **Generate voices**, listen to the options and keep one |
| **Record now** | Record straight from your microphone; it stops at 30 seconds |
| **Upload audio** | Choose a file and drag the handles to trim it |

New voices are saved **privately** to your Fish Audio account, so they also
appear in anything else connected to it. If one doesn't show up in the list,
click **Refresh from Fish Audio**.

## In the meeting

### Only speak when spoken to

Tick **Only speak when spoken to** for any call with more than one other person.
The avatar then answers only when it hears its display name. Speech-to-text
often misspells unusual names, so it also matches names that *sound* right.

After it speaks, it keeps listening for `FOLLOW_UP_SECONDS` (15), so a follow-up
like "and who owns that?" still gets an answer — up to `FOLLOW_UP_MAX` (2) in a
row before the name is needed again. Set either to `0` to always require the name.

To end the exchange early, say one of these — the name is optional:

> thanks · thank you · that's all · that's it · we're good · we're all set ·
> we're done · nothing else · no more questions · you can go · stand down

It replies, then stays quiet until it hears its name again.

### Sending it lines mid-meeting

While the avatar is in a call, the **Send a line into the meeting** card has
three buttons:

| Button | What happens |
| --- | --- |
| **Tell it this** | Nothing is said. The avatar now knows it for the rest of the call, and it takes priority over the knowledge base |
| **Say it out loud now** | The avatar says it in its own words and in character, without mentioning it was sent a line |
| **Stop listening** | Same as saying "thanks" — quiet until someone uses its name |

Everything you send is recorded in the transcript as **sent in by the owner**.

## Meeting notes

The avatar hears everything, including what it doesn't answer. Each meeting
leaves these in `meetings/`:

| File | What |
| --- | --- |
| `<date>-<name>.jsonl` | Every turn, written as it happens |
| `<date>-<name>.md` | The recap, written when the meeting ends |
| `<date>-<name>.pdf` | The recap, formatted for reading |

The recap has Summary, Key points, Decisions, Action items, **For you** and Open
questions.

**Notes can't be lost.** The transcript is saved turn by turn, so if the recap
never got written — a crash, a closed window — the panel notices, and **Write any
missing notes** rebuilds the recap, PDF and email from the transcript.

- `NOTES_FORMATS`: `pdf` (default), `docx`, `both` or `none`
- `NOTES_EMAIL_TO`: set it to have the notes emailed when each meeting ends.
  Gmail needs a 16-character **app password**
  (<https://myaccount.google.com/apppasswords>) in `SMTP_PASSWORD`. Test it with
  `4-Email-Test.bat` or `uv run python src/mailer.py`.
- Rebuild any recap: `uv run python src/recap.py meetings/<file>.jsonl`

**A real limitation:** meeting audio arrives as one mixed stream with no speaker
labels, so notes record *what* was said, not *who* said it. Names appear only
where someone was named out loud, or where a line came from meeting chat.

## Knowledge bases

In the panel, type a name under **Knowledge base** and click **Add documents…**.
PDF, Word, text and Markdown all work, and several files become one knowledge
base. From the command line:

```bash
uv run python src/ingest.py my-kb ./docs
```

Embeddings run on your machine with fastembed, so documents never leave it.

### Spreadsheets

Language models are unreliable at arithmetic, and document search cuts tables
apart. `sheet_facts.py` calculates the figures in Python instead and writes them
as plain question-and-answer facts the avatar can quote:

```bash
uv run python src/sheet_facts.py data.csv --out knowledge/my-sheet.md
```

- `--since 2020-01-01` / `--until …` keep only rows in a date range
- `--drop revenue` leaves a column out entirely — use it for anything the avatar
  shouldn't be able to say out loud

Then add the `.md` file as a knowledge base. It reads CSV — export Excel or Google
Sheets to CSV first. Works well for already-summarised data (analytics exports,
budgets, KPIs); it isn't a replacement for querying thousands of raw
transactions.

## Command line

Start the worker and leave it running:

```bash
uv run python src/agent.py dev
```

Send it into a meeting:

```bash
uv run python src/send_to_meeting.py "https://us05web.zoom.us/j/1234567890?pwd=abc123" --bot-name "Jess (AI)" --image "jess.jpg" --knowledge "my-kb" --tone upbeat --require-address
```

Add `--dry-run` to see what would be sent without sending it.

## Reaching the panel from another device (experimental)

The panel only answers on this machine by default, because it can start programs
and send an avatar into meetings. Set `ALLOW_LAN=1` to also serve it on your local
network. The startup window prints the address to use.

- If [mkcert](https://github.com/FiloSottile/mkcert) is installed, the panel serves
  HTTPS with a certificate for your network address — browsers only allow
  microphone recording over HTTPS. Open `/rootCA.crt` on the other device to
  install the certificate authority.
- On Windows you may need a firewall rule for port 8765, limited to your local
  network.
- **Known issue:** iOS Safari refuses mkcert's certificates because they're valid
  for longer than Apple allows, and fails with "the network connection was lost".
- With `ALLOW_LAN=1`, anyone on your network can use the panel.

## Settings

All in `.env.local`. See `.env.example` for descriptions.

| Setting | Default | What it does |
| --- | --- | --- |
| `OWNER_NAME` | — | Who the avatar stands in for |
| `BOT_NAME` | `<OWNER_NAME> (AI)` | The avatar's display name |
| `DEFAULT_VOICE_ID` | Fish plugin default | Voice used when none is chosen |
| `AVATAR_TONE` | `upbeat` | `off`, `warm`, `upbeat`, `excited`, `professional`, `calm` |
| `FOLLOW_UP_SECONDS` | `15` | How long it keeps listening after speaking |
| `FOLLOW_UP_MAX` | `2` | Unaddressed follow-ups before its name is needed |
| `JOIN_DELAY_SECONDS` | `3.5` | Pause before its opening line |
| `NOTES_FORMATS` | `pdf` | `pdf`, `docx`, `both`, `none` |
| `NOTES_EMAIL_TO` | — | Where to email the notes |
| `BRAND_NAME` | — | Your name in the panel header |
| `ALLOW_LAN` | off | Serve the panel on your local network |

## Making it yours

Set `BRAND_NAME` to put your name in the header, and drop a square
`logo-small.png` in `brand/` to show your logo beside it. `brand/` is gitignored,
so your logo stays out of version control.

## Layout

| Path | What |
| --- | --- |
| `src/agent.py` | The worker: joins the meeting, runs the conversation |
| `src/send_to_meeting.py` | Sends the avatar into a meeting |
| `src/webui.py`, `src/webui.html` | The control panel |
| `src/catalog.py` | Lists faces, voices and knowledge; starts and stops the worker |
| `src/voices.py` | Creates and previews Fish Audio voices |
| `src/rag.py`, `src/ingest.py` | Document search and knowledge-base building |
| `src/sheet_facts.py` | Turns a spreadsheet into quotable facts |
| `src/notes.py`, `src/recap.py` | Transcripts and recaps |
| `src/export.py`, `src/mailer.py`, `src/delivery.py` | PDF/Word output and email |
| `src/stop_agent.py` | Stops the agent and writes any missing notes |
| `avatars/`, `knowledge/`, `meetings/`, `brand/` | Your files — all gitignored |
| `HANDOFF.md` | Design notes, gotchas and open problems |

## A note on disclosure

The avatar introduces itself as an AI when it joins. Leave that on. Recording and
consent laws vary by place, and an undisclosed AI in a meeting is a problem in
many of them.

## License

MIT — see [LICENSE](LICENSE).
