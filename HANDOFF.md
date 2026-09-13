# Handoff: what carried over from the browser avatar app

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

| | Browser avatar app | This project |
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
`https://192.168.x.x:3000/uploads/...`. Those are dead outside that LAN. If you
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
name. Voices are private to the account that made them, so the default comes
from `DEFAULT_VOICE_ID`, or the plugin's own public voice when that is unset.

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

Knowledge bases between roughly 4k and 180k characters were used in testing;
both ends worked, and retrieval quality mattered more than size.

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
said "I can't relay messages directly to individuals like your owner", and then "I
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
owner, and correctly refuses to "text them right now".

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
render and email), then terminates. It only waits when a meeting is actually in
progress; an idle worker has nothing polling and is stopped at once, and is not
reported as "forced" because that is unremarkable.

**"In a meeting" is a question about state, not about the log's contents.** The
first version asked whether the last 600 log lines *mentioned* a meeting
(`"Taking notes to" in tail`), which stayed true for as long as that line sat in
the window. A meeting that had ended ten minutes earlier therefore still counted,
so every stop burned the full 75 seconds while nothing was polling the request
file -- the button looked broken, then worked. `_meeting_in_progress()` now
compares the newest start marker against the newest end marker. "Meeting over;
writing recap" is the end marker because it is logged unconditionally at the top
of the recap shutdown callback; "Left the meeting." is skipped whenever leaving
raises. Measured after the fix: 0.1s to stop an idle worker, down from 88s.

The other half of that bug was feedback in the wrong place again -- the Stop
button is in the header, its status message rendered in the send card several
sections down, and the status poll kept repainting "agent ready" over the top.
The pill beside the button now says "stopping..." and the poll leaves it alone.

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

Voices are tagged `zoom-avatar-agent`, so the app can tell which voices it created.

Trimming and recording happen in the browser: `decodeAudioData` into an
AudioBuffer, a canvas waveform with the selection highlighted, and a hand-rolled
16-bit PCM WAV writer for the selected span (WAV is a 44-byte header plus raw
samples, so no library and no re-encode). Recording uses `MediaRecorder` with a
hard stop at 30s, and an over-long upload is pre-trimmed to 30s on load rather
than shown as an error to fix by hand.

## Delivery tone: the direction has to be on every sentence

A trained Fish voice reads flat. `s2.1-pro` takes a free-form direction in
square brackets at the head of the text -- `[upbeat, bright, smiling while
speaking, lively pace]` -- and **consumes it rather than speaking it**. Verified
rather than assumed: each tagged line was synthesized and the audio fed back
through Fish's own `/v1/asr`, which returned the line with no trace of the tag.
`(parenthesis)` tags work too; those are the S1 syntax.

**The trap.** `livekit-plugins-fishaudio` does not send a turn to Fish as one
piece. It runs the text through a sentence tokenizer and sends **each sentence
as its own synthesis unit** -- `{"event":"text"}` then `{"event":"flush"}`, per
sentence -- so that audio arrives at sentence boundaries instead of waiting for
`chunk_length` characters. A direction placed once at the head of the turn
therefore reaches only the first sentence:

```
flush 1: '[upbeat, ...] Hi, thanks for having me.'
flush 2: "I'm standing in for Alex today."         <- undirected
flush 3: "What's first?"                            <- undirected
```

First sentences are usually short ("Hi there."), so in practice almost all of
the speech came out flat and the feature looked like it did nothing at all.
This cost an afternoon, because the obvious first implementation -- overriding
`Agent.tts_node` to prepend the tag to the turn's text stream -- is the wrong
seam and fails silently.

**The fix** is `TonedSentenceTokenizer` in `agent.py`, handed to the plugin
through its supported `tokenizer=` constructor argument. It wraps the default
blingfire tokenizer and re-attaches the direction to every sentence *after*
tokenization, so each unit that actually reaches the socket carries it. It skips
any sentence already starting with `[`, so nothing gets double-tagged.

**The audition path and the speaking path must agree on the model, or the
audition lies.** This cost far more time than the tokenizer did.
`voices.py` sent `model: s2-pro` while the plugin speaks with `s2.1-pro`, and
**s2-pro does not act on the bracketed direction at all**. So "Hear this voice"
came back flat while the identical tone in a real meeting sounded right, and
every hand-written test script sounded right too because those all named
`s2.1-pro` explicitly. `TTS_MODEL` in `voices.py` now carries a comment saying it
has to track the plugin's `DEFAULT_MODEL`; if you ever change one, change both.

The lesson underneath it: when two paths differ, diff the requests before
theorising about behaviour. Three wrong explanations were built and shipped --
tokenization, then per-sentence tagging in preview, then process staleness --
while the actual difference was one constant, one line apart from the code being
edited, never compared.

Related: a script that drives `fishaudio.TTS` outside a job has to be wrapped in
`async with livekit.agents.utils.http_context.open():` or the plugin refuses to
open an HTTP session. `voices.preview` also repeats the direction per sentence,
which is not what fixed it but does keep the audition shaped like the call.

Tones live in one `TONES` table in `agent.py`; the interface builds its dropdown
from it via `/api/state`, so the list and the defaults cannot drift apart.
`AVATAR_TONE` sets the default, an unknown value falls back rather than failing
the join, and `prosody.speed` is exposed by the plugin but not yet wired up.

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

1. **Follow-ups die.** ~~"and Q4?" is dropped.~~ **Fixed.** For
   `FOLLOW_UP_SECONDS` (15) after the avatar *finishes* speaking, an unaddressed
   turn is treated as still aimed at it, up to `FOLLOW_UP_MAX` (2) in a row
   before the name is required again. Timed from when it stops talking, not
   starts, or a long answer spends its own window; monotonic, so a clock
   correction cannot open or close it.

   The cap is the important half. Every windowed answer restarts the clock, so
   without one the avatar could chain replies through a conversation it is not
   part of. The accepted cost is the reverse case: if two people turn to each
   other within fifteen seconds of the avatar speaking, it will interject, at
   most twice. Nothing in the audio can distinguish that from a follow-up --
   one mixed stream, no speaker labels, and the meeting's attendees are not
   participants the agent can see. Set either value to 0 to disable.

   **Closing the window early matters as much as opening it.** Two exchanges and
   you are done with it, but the window keeps it listening for another fifteen
   seconds while the humans carry on -- so "thanks, Carl" (or the Stop listening
   button) sets a `_dismissed` flag and it goes quiet until named again. A flag
   and not a cleared clock, because the avatar answers the dismissal and that
   reply would otherwise reopen the window it was just told to close. Dismissal
   phrases are only matched on a turn already being answered and only when the
   turn is short and not a question, so "thanks, and what about the budget?"
   stays a question rather than a goodbye.
2. **STT mangles names.** ~~Needs fuzzy matching.~~ **Partly fixed.** A real
   call proved it: a bot named "Krendall" was transcribed "Krendel", "Crindle"
   and "Crindle" in three consecutive turns, the literal check missed every
   one, and the avatar sat silent while being addressed by name -- having just
   announced "please say Krendall to get my attention". `_is_addressed` now
   falls back to a consonant skeleton (`_phonetic_key`: digraphs folded, c/q->k,
   vowels dropped, doubles collapsed), under which all three spellings become
   `krndl`. Guarded to names of five letters or more with three or more
   consonants, because short skeletons collide with ordinary words -- "Sam" and
   "some" are both `sm`. Still misses: names with under three consonants
   ("Louie" -> `l`, rejected as too collision-prone) and voiced/unvoiced swaps
   ("Grendel" -> `grndl`).
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
