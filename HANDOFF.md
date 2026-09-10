# Handoff: what carried over from BDM Real-Time Avatar

Written 2026-09-10, at the start of this project. The source project was a
browser-based 1:1 avatar call — a Next.js frontend plus a Python LiveKit agent.

This file exists so we don't re-learn what that build already taught us. Read it
before changing the agent.

---

## What this project is

The same avatar pipeline (LemonSlice video + Fish Audio voice + LiveKit
orchestration + local vector RAG), but instead of a browser calling the avatar,
the avatar is **dispatched into a third-party meeting** as a bot participant.

The other end changes; the middle does not.

| | BDM Real-Time Avatar | This project |
|---|---|---|
| Who calls whom | Browser joins a LiveKit room | Agent joins Zoom/Meet/Teams/Webex |
| Trigger | Web form → `/api/token` | `lk dispatch create` with metadata |
| Audio out | Browser plays the avatar track | The meeting plays it |
| Control surface | Next.js panel | `scripts/send-to-meeting.ps1` |

---

## Provenance of the code here

| File | Came from |
|---|---|
| `src/agent.py` | Merge of `lemonslice-examples/07-livekit-zoom/agent.py` (the `join_meeting` plumbing) and the browser app's `app/agent/src/agent.py` (persona-from-metadata, Fish TTS, local-image upload, RAG hook) |
| `src/rag.py` | Copied unchanged from `app/agent/src/rag.py` |
| `src/ingest.py` | Python rewrite of the app's `/api/knowledge` Node route (which used unpdf + mammoth) plus `rag_ingest.py` |
| `knowledge/*` | Copied from `app/knowledge/` — prebuilt indexes, usable immediately |

The three lines that make it a meeting bot rather than a room bot:

```python
await avatar.join_meeting(meeting_url, bot_name=..., listen_to_meeting_chat=...)
room_options = avatar.room_options()
await session.start(agent=..., room=ctx.room, room_options=room_options)
```

---

## Hard-won details worth not rediscovering

### 1. The avatar image must reach LemonSlice's servers

`agent_image_url` is fetched **by LemonSlice**, not by us. So a site path
(`/avatar.png`), a local file path, or anything on localhost / a LAN IP will
silently fail.

The browser app worked around this by downloading the image and handing the
plugin raw pixels via `agent_image=`. That workaround is now the *primary* path
here (`_load_avatar_image`) — drop a file in `avatars/` and pass its name. It
removes the Zoom example's `LEMONSLICE_IMAGE_URL` requirement entirely.

Related: the old app's `presets.json` has image URLs like
`https://192.168.0.48:3000/uploads/...`. Those are dead outside that LAN. If you
copy presets over, re-point `image` at a local filename.

### 2. EXIF rotation and the upload size cap

Phone photos carry an orientation *tag* instead of rotated pixels. LemonSlice's
re-encode ignores the tag, so the avatar renders sideways. `ImageOps.exif_transpose`
bakes it in. LemonSlice also caps the upload around 4 MB and targets 368×560, so
a full-res photo gets rejected — hence the `thumbnail((640, 960))`.

Both are in `_prepare_image`. Don't remove them.

### 3. `FISH_API_KEY` was never in a .env file

On the original build machine it lives as a **Windows user-level environment
variable**. That means the browser app appears to work with no Fish key in
`.env.local`, and will silently fail on any other machine. This project's
`.env.example` lists it properly — put it in `.env.local`.

### 4. Fish Audio voices are addressed by `reference_id`

`fishaudio.TTS(voice_id=...)` wants the Fish Audio *model reference_id*, not a
name. Carried-over default is `8b5e9142f2184439b48dee26169d9dba` ("Melmore 2").

### 5. Don't reuse the browser app's `RoomOptions` — and drop noise cancellation

The app passed `audio_output=False` because the browser rendered the avatar's
track itself. In meeting mode that is wrong — use `avatar.room_options()`, which
the plugin builds for the meeting transport.

Verified against the installed 1.8.0 source: once `join_meeting()` has been
called, `room_options()` returns `RoomOptions(audio_input=False, audio_output=False)`
— **meeting audio is fed directly into STT and bypasses LiveKit room audio
entirely.**

That settles what looked like an open item. The app's `noise_cancellation.BVC()`
audio-input option is not "not yet wired up" here — it is *inapplicable*, because
there is no LiveKit room audio input for it to filter. The dependency is
deliberately absent from `pyproject.toml`. If meeting audio turns out to be
noisy, the fix has to live upstream (Zoom's own suppression) or in an STT-side
option, not in BVC.

`room_options(**kwargs)` does forward extra kwargs to `RoomOptions`, so other
room-level options can still be layered in if needed.

### 6. `resume_false_interruption` should be off

The browser app sets it `True` (1:1, so resuming a cut-off reply is right). In a
meeting you can't tell who talked over whom, and resuming lands the avatar's
voice on top of a human. Set to `False` here — same as the Zoom example.

### 7. `livekit-plugins-lemonslice` no longer needs a git pin

The `07-livekit-zoom` README says `join_meeting()` was only on `livekit/agents`
main (as of 2026-07-02) and pins the plugin to git, with `override-dependencies`
gymnastics and `GIT_LFS_SKIP_SMUDGE=1` to make `uv sync` work.

That's stale: **1.8.0 shipped 2026-09-05** from PyPI. `pyproject.toml` here pins
`livekit-agents[lemonslice]>=1.8` normally. If a future install ever regresses,
that README has the git-pin recipe.

---

## Status (2026-09-10)

The avatar joins a real Zoom meeting, LemonSlice renders it, meeting audio
arrives, it delivers its disclosure line, and a **two-way conversation works** --
confirmed by the user with the default portrait and default Fish voice.

The original goal is met: a preset combining a real portrait, a cloned Fish
Audio voice and a personal knowledge base joins a Zoom call and converses,
confirmed against a live meeting.

Knowledge bases were sized between roughly 4k and 180k characters during
testing; both ends worked, and retrieval quality mattered far more than size.

Confirmed working, in the order it was built and tested:
join a Zoom call -> avatar + cloned voice -> two-way conversation ->
address gate -> transcript capture -> recap -> PDF -> email received.

**Not yet exercised: the automatic chain end to end in one real meeting.**
The email was proven with `src/mailer.py`'s self-test, and rendering was proven
against a saved recap, but no single live meeting has yet run
shutdown -> recap -> render -> send by itself.

The address gate has now been tested with two participants and behaved correctly:
silent on unaddressed speech, answering when named. Its two known blind spots
(follow-ups without the name, and being *mentioned* rather than *addressed*)
are still unfixed -- see the turn-taking section.

Two things that test taught us, both now fixed:

1. The dispatch metadata was arriving truncated (see the JSON/argv note below).
2. The agent didn't know its own display name, so its disclosure improvised
   "you can address me as AI". `_disclosure_instructions()` now passes the name in.

## Note-taking: what it can and cannot know

`src/notes.py` writes every turn to `meetings/<stamp>.jsonl` as it happens (not
buffered, so a crash still leaves a record) and generates a markdown recap on
`ctx.add_shutdown_callback`.

Capture happens in `on_user_turn_completed` **before** the address gate, on
purpose: the point of attending on someone's behalf is to record what was said,
including everything the agent deliberately stays silent about. The avatar's own
turns come from the `conversation_item_added` session event instead, filtered to
`role == "assistant"`, which avoids double-counting.

**No speaker attribution for speech.** Verified by reading the plugin:
`meeting/audio.py` receives one mixed PCM stream with no participant labels.
`UserInputTranscribedEvent` does carry a `speaker_id` field, but there is nothing
upstream to populate it meaningfully in meeting mode. `meeting/chat.py` is the
exception -- it formats chat as `[Sender]: text`, which `MeetingAssistant` parses
back out into a real speaker name.

Two bugs from the first live notes test, both fixed:

1. **Filenames must carry a session id.** Two agent sessions started 4 seconds
   apart in the same meeting, both computed `<date>_<HHMM>-<slug>` as their
   filename, interleaved their turns into one `.jsonl`, and the session that
   recorded nothing wrote its "Nothing was said" recap last -- destroying the
   real notes. Filenames now include the LiveKit job id and seconds.
2. **A zero-turn session writes nothing.** Files are created lazily on the first
   recorded turn, and `write_recap` returns None rather than emitting a vacuous
   recap.

`src/send_to_meeting.py` also now refuses a second dispatch to the same meeting
URL within 120 seconds unless `--force` is passed. Two avatars in one call don't
just race for the recap file -- they both answer and talk over each other.

`src/recap.py` rebuilds a `.md` from any `.jsonl`. The transcript is the source
of truth; the recap is derived and disposable.

So the recap prompt explicitly forbids inventing attributions. If per-speaker
attribution ever becomes a requirement, it has to come from the meeting
platform's own transcript (Zoom cloud recording / transcript API), not from here.

## Delivering the notes: PDF, DOCX, email

`export.py` renders the recap markdown to PDF (reportlab) and DOCX
(python-docx). Both are pure Python **on purpose** -- Puppeteer would drag in a
Node toolchain and WeasyPrint needs GTK system libraries on Windows, and neither
is worth it for a document this simple. The renderer handles only the subset the
recap prompt emits; anything else degrades to plain text.

Two rendering details that needed fixing by looking at the output rather than
trusting the API:

* reportlab's named bullet starts (`start="circle"`) render as **nothing**. The
  first PDF had indented text with no bullet glyphs at all. Use an explicit
  `start="•"`.
* The recap header (Attended by / Started / Meeting / ...) is markdown bullets
  but reads as a field list. `_blocks()` relabels bullets appearing before the
  first `---` as `meta` and renders them without glyphs.

`mailer.py` sends over plain SMTP, configured entirely from `.env.local`, and is
a no-op unless `NOTES_EMAIL_TO` is set. `uv run python src/mailer.py` is a
self-test that sends one message and explains an auth failure in Gmail terms.
The app password lives only in `.env.local` (gitignored) and is never logged.

`delivery.py` is the one shared path for render-then-email, used by both the
automatic shutdown hook and `recap.py`. Everything in it is best-effort: the
markdown and the transcript are already on disk when it runs, so a missing
library or a wrong password must never look like a lost meeting.

## The interface

`webui.py` (FastAPI) serves `webui.html` (one page, inline CSS/JS, no build step
and no CDN) on `127.0.0.1:8765`. `catalog.py` holds everything it needs to know
about -- avatars, Fish voices, knowledge bases, presets, meeting notes, and the
worker process -- kept separate from the HTTP layer so it can be tested without a
server.

Deliberate choices:

* **Python, not Node.** The browser app it descends from was Next.js. Serving one
  page from the venv that already exists means `uv sync` remains the only setup
  step.
* **Bound to localhost, no auth.** The page starts processes, writes files and
  sends an avatar into a meeting. It has no authentication *because* it is not
  reachable from the network. Do not "just" bind it to 0.0.0.0.
* **The worker runs on `sys.executable`, not `uv run`.** The interface already
  runs inside the project venv, which has every dependency -- and that venv has
  no `uv` module, so going back through uv fails with "No module named uv". This
  was the first bug in the interface.
* **The worker logs to `logs/worker.log`, not a console window.** The page can
  then show the log, so a failure is visible where the user already is. That is
  how the uv bug above was diagnosed.
* **"running" and "ready" are different.** `worker_status()` reports `registered`
  separately, by looking for `registered worker` in the log after the last exit
  line. Send is disabled until then, because a started-but-unregistered worker
  silently drops dispatches.
* **Voice filtering keeps a hidden selection.** Typing in the filter box must not
  silently change which voice is about to be used, so a selected-but-filtered-out
  voice is re-appended to the list marked "(selected)".
* **Uploaded documents keep their real names.** Staged uploads carry a random
  filename prefix to avoid collisions; that prefix must be stripped before it
  reaches the knowledge record. It leaked once.

## Notes are guaranteed by reconciliation, not by shutdown

A real meeting's notes were lost this way: the user stopped the worker with
`Stop-Agent.bat`, which force-killed it, so `ctx.add_shutdown_callback` never
ran. The transcript was intact (11 turns) but no recap, PDF or email happened.

**There is no graceful stop available on Windows here, and that is measured, not
assumed.** The only way to ask a process to exit cleanly is a console control
event, and a process started with `CREATE_NO_WINDOW` has no console to receive
one. Starting it with `CREATE_NEW_PROCESS_GROUP` and sending `CTRL_BREAK` was
tried: the worker ignored it and had to be killed after a full 40 second wait,
with no meeting even in progress. Giving it a real console instead would mean a
stray black window on every start.

So the guarantee moved somewhere it can actually hold:

* `catalog.transcripts_without_notes()` finds any `.jsonl` with turns and no `.md`.
* `catalog.reconcile_notes()` runs `recap.py` over each -- summary, PDF, email.
* It runs automatically after every stop, is offered as a button in the
  interface, and is reported on page load as a banner.

This covers more than a shutdown handler ever could: force-kills, crashes,
closed windows, power cuts. The `.jsonl` is written turn by turn as the meeting
happens, so it is always there to reconcile from.

Consequence for design: **never make the notes depend on clean shutdown.** The
transcript is the source of truth; everything else is derived and rebuildable.

## Joining mid-sentence

`utils.wait_for_agent()` only means the LemonSlice avatar participant exists.
The meeting's own audio path comes up a beat later -- the plugin logs "connected
to meeting relay", then "received first pcm audio frame", and there is a ~3s AEC
warmup after that. Speaking into that gap means the meeting drops the start of
the sentence, and the avatar is heard joining mid-word.

`DEFAULT_JOIN_DELAY` (3.5s) sits between the two. Overridable per dispatch with
`joinDelay`, or globally with `JOIN_DELAY_SECONDS`; clamped to 0-30s.

## A dead server must not look like a broken feature

An upload failed with "Upload failed: Failed to fetch". Nothing was wrong with
the upload -- the interface's own server had been stopped, so the page in the
browser could not reach anything. The raw fetch error reads like a bug in
whatever button was pressed, which sent the diagnosis in the wrong direction.

`api()` now distinguishes a connection failure from an HTTP error. On connection
failure the page flips to an offline state: the status pill says "interface
offline", every action button is disabled, and the banner says to close the tab
and run `0-Open-Interface.bat` again. The poll keeps running, so the page
re-enables itself when the interface comes back -- verified by killing and
restarting the server with the tab open.

General point for this project: the interface is a page pointing at a local
process that can disappear at any time (window closed, machine slept, crash).
Any new fetch should go through `api()` rather than calling `fetch` directly, so
that case stays legible.

## The agent must be told what it can do

The note-taking pipeline is entirely invisible to the LLM. Transcript capture,
the recap, the PDF and the email all happen outside the conversation, so the
model has no way to know they exist. Asked to pass a message to its owner, it
said "I can't relay messages directly to individuals like Bob", and then "I
don't have the functionality to take notes or send messages automatically".

Both were false. The notes for that very meeting were written correctly, with
the request captured under **For you**. Only the agent's belief about itself was
wrong -- and for a stand-in, refusing to take a message is the worst possible
failure: it destroys the entire premise while everything underneath works.

`_capability_note()` is appended to every persona, whichever preset is in use. It
states that everything is transcribed, that a summary reaches the owner
afterwards with a section for them, that it should therefore accept messages and
read the details back for accuracy, and that flagging an unanswerable question
for the owner is the useful move rather than a failure. It is also explicit about
the two real limits, so the model doesn't over-promise: no contact during the
meeting, and no commitments on the owner's behalf.

Verified by replaying the exact failed exchange against the new instructions --
it now accepts the message, repeats back "Thursday at around nine AM", names its
owner, and correctly refuses to "text Bob right now".

`OWNER_NAME` (env, or `ownerName` per dispatch) exists because it was also asked
"who is your owner?" and did not know.

**Rule for this project: any capability implemented outside the conversation has
to be described inside the instructions, or the agent will deny having it.**

## Stopping must take the avatar out of the meeting

Killing the worker does not remove the avatar from the call. The LemonSlice
meeting bot is a **cloud-side participant**, so the local process dying leaves it
sitting in the Zoom meeting -- stopping the agent looked like it worked while the
avatar was still visibly there.

`AvatarSession.leave_meeting()` is the fix, and it has to run inside the worker,
which a killed process cannot do. Since Windows has no usable signal for that
(see stop_worker), the worker polls for `logs/stop.request` while it is in a
meeting; on seeing it, it calls `ctx.shutdown()`, which runs the shutdown
callbacks in order: leave the meeting, then write the notes.

`stop_worker()` writes that file, waits (75s -- long enough to leave, summarize,
render and email), then terminates. It only waits when the log shows a meeting is
actually in progress; an idle worker has nothing polling and is stopped at once,
and is not reported as "forced" because that is unremarkable.

## Voice creation

`voices.py` wraps three Fish Audio paths, all landing as real models on the
user's account so any program reading that account can use them:

* **clone_from_audio** -- `POST /model` with `train_mode=fast`. Note
  `visibility` defaults to **public** and a public model then requires a
  `cover_image`; everything here is created `private`. `texts` is omitted on
  purpose so Fish runs its own ASR rather than trusting a guessed transcript.
  Training is async, so it polls `GET /model/{id}` until `trained`.
* **design** -- `POST /v1/voice-design` with the `model: voice-design-1` header.
  Candidates come back as base64 WAV and are held in memory keyed by a token, so
  the browser auditions them by URL and only the chosen one is turned into a
  model. Billing is per request, not per candidate, so asking for 2 costs the
  same as 1.
* **preview** -- `POST /v1/tts`, for auditioning any of the 54 existing voices
  before committing to one.

Voices are tagged `bdm-avatar-app`, the same tag the browser avatar app uses, so
both can tell which voices they created.

Trimming and recording happen in the browser: `decodeAudioData` into an
AudioBuffer, a canvas waveform with the selection highlighted, and a hand-rolled
16-bit PCM WAV writer for the selected span (WAV is a 44-byte header plus raw
samples, so no library and no re-encode). Recording uses `MediaRecorder` with a
hard stop at 30s, and an over-long upload is pre-trimmed to 30s on load rather
than shown as an error to fix by hand.

## Theming

Every colour is a token on `:root`. Dark values are defined once and applied
either by an explicit `data-theme="dark"` or, absent a stored choice, by
`prefers-color-scheme`. The toggle stores the choice in `localStorage` (wrapped
in try/catch for private windows) and the button icon reflects the effective
theme, not the stored one.

## "Nothing happens" is usually a message in the wrong place

Recording reported as doing nothing. The handler was running fine and the mic
error *was* being shown -- in the panel's shared message box, several elements
below the button, easily off-screen. Feedback has to appear where the user is
looking, so `#recMsg` sits directly under the record controls.

The same click now also says "Asking for microphone access…" immediately, because
waiting on a permission prompt with no feedback is indistinguishable from a dead
button. `micProblem()` names the actual cause -- blocked (with the Chrome steps
to unblock), no device, device busy in another app, or no getUserMedia at all --
rather than printing a DOMException name.

Verified by substituting a real `MediaStream` from an oscillator for the
microphone, which exercises MediaRecorder, the timer, onstop, decode and the trim
box without needing a device: recording, stopping, and the 30s auto-stop
(including releasing the mic) all work. Worth reusing -- device permissions
cannot be granted in the test browser.

## The real open problem: turn-taking in a group call

Everything above is plumbing. This is the actual design work.

The browser app is 1:1 — every utterance is aimed at the avatar, so "respond to
what you just heard" is a correct policy. Drop that same agent into a six-person
Zoom and it tries to answer people talking to *each other*. It will interrupt,
and it will be exhausting within a minute.

`MeetingAssistant` ships a first cut: `requireAddress` drops any turn whose text
doesn't contain the bot's name, by raising `StopResponse` from
`on_user_turn_completed`. It is deliberately crude and **off by default** so the
first join test isn't fighting the gate.

Known weaknesses of the crude version, in the order they'll bite:

1. **Follow-ups die.** "Jess, what's our Q3 number?" works; "and Q4?" is dropped.
   Needs a short window after the bot speaks where unaddressed turns still count.
2. **STT mangles names.** Deepgram may render "Jess" as "Jeff"/"yes". Needs fuzzy
   matching or a phrase hint.
3. **No sense of being asked implicitly.** A direct question in a 1:1 call needs
   no name. Probably wants a cheap LLM classifier ("is this addressed to the
   assistant?") gating the expensive generation, rather than substring matching.

Do not build a nice UI before this feels right in a real call.

---

## Other things that will bite

- **Admittance.** Waiting rooms and "host must admit" leave the bot hanging with
  no error. Zoom passcodes must be *in the URL query string*.
- **Disclosure and consent.** The bot announces itself on join by default
  (`DISCLOSURE_INSTRUCTIONS`). Keep that. An undisclosed AI participant in a
  call is a consent problem in two-party-consent jurisdictions, and rude
  everywhere else.
- **Latency budget.** STT → LLM → TTS → avatar render, and now a meeting
  transport hop on top. `gpt-4o-mini` is the carried-over LLM; the Zoom example
  chose Groq `llama-3.3-70b-versatile` specifically for speed. Worth A/B-ing.
- **Never hand JSON to a native exe on Windows.** This cost the first live test.
  `lk dispatch create --metadata '<json>'` from PowerShell arrives *truncated at
  the first space*: a bot name of "Jane Smith (AI)" cut the JSON mid-string, so
  the worker saw no `meeting_url` and crashed. Escaping the quotes is not enough
  — the argv parser still breaks at the space. Dispatch is now built in-process
  with the LiveKit Python SDK (`src/send_to_meeting.py`), which also drops the
  `lk` CLI as a prerequisite. Don't reintroduce a shell-quoted JSON path.

---

## Deliberately left behind

- The whole Next.js app (upload endpoints, voice cloning UI, preset editor,
  HTTPS certs). v1 is CLI-only, on purpose — the unknowns are in joining and
  turn-taking, not in the UI.
- `videos/`, `docs/user-guide/`, the HyperFrames motion-graphics pipeline, and
  the marketing PDFs. Unrelated to this project.
- `faceTimeMode`. Its whole point was simulating picking up a phone call; a
  meeting join has its own opening (the disclosure line).
