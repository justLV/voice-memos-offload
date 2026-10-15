# Voice Memos Offload

Run this on an always on Mac device. Any voice memos you record from any iCloud sync'd Apple device will be transcribed, and routed as either a request to your agent (Sesame by default) or to your notes database.

```bash
uv run murmur.py
```

There is no install step. uv reads the dependencies declared at the top of
`murmur.py` and sets up an environment for you.

```
"Can you check what spots are free for Pilates on Sunday?"   sent to your agent
"I should really start going to bed earlier"                 kept as a note
```

## How it works

```
Voice Memos  ->  parakeet-mlx  ->  classifier  ->  your agent
  (.m4a)         on-device ASR    request or       Sesame, a Grok Bot
                                     note?          routine or any webhook
```

Transcription runs on your Mac with
[parakeet-mlx](https://github.com/senstella/parakeet-mlx). It takes about a
second for a ten second memo once the model is loaded. Sesame takes the
recording of each request and transcribes it itself; the other sinks get only
the local transcript.

Classification uses a local rule by default, which needs no API key. Add a key
and it uses a hosted model instead.

## Requirements

- Apple Silicon Mac, since parakeet-mlx uses MLX. On Intel, use `--asr groq` or
  `--asr openai`.
- [uv](https://docs.astral.sh/uv/getting-started/installation/)
- No ffmpeg. Audio is converted with `afconvert`, which comes with macOS.

## First run

```bash
git clone https://github.com/justLV/voice-memos-offload
cd voice-memos-offload
uv run murmur.py --dry-run
```

`--dry-run` prints what it would send, without sending it.

On the first run it marks your existing memos as already seen, so it will not
send your back catalogue anywhere. To process recent ones anyway:

```bash
uv run murmur.py --backfill 3
```

## Sending requests to Sesame

The default sink sends each request to Sesame through its **Voice Capture** app.
Voice Capture takes recordings only, never text, so murmur uploads the memo's
audio and Sesame transcribes it. Set it up once:

1. In the Sesame app, open the apps list and choose **Voice Capture**. Tap
   **Install**, then **Connect** to open its settings page.

2. Under **Add a device**, find **Other device or script**. Give it a name and
   tap **Add**. Sesame uses the name when it replies, so `Voice Memos app` or
   `iPhone` reads well.

3. Copy the **Device URL** and the **Secret** off the new device.

4. Check the connection, before involving murmur at all:

   ```bash
   curl -X POST https://capture.sesameai.app/v1/devices/<device-id> \
     -H "Authorization: Bearer sesame_whsec_<secret>" \
     -H "X-Voice-Capture-Test: true"
   ```

   It answers `Connected to Sesame.`, and the settings page shows the device as
   connected. Any other answer says what to fix.

5. Put both values in `.env`:

```bash
MURMUR_WEBHOOK_URL=https://capture.sesameai.app/v1/devices/<device-id>
MURMUR_WEBHOOK_TOKEN=sesame_whsec_<secret>
```

Then `uv run murmur.py`. Sesame answers each request in your chat with it, and
its reply starts with what it heard from which device.

To open the settings again later, ask Sesame to "open Voice Capture settings".
Do not tap **Disconnect** on the app, since that removes every device.

Sesame accepts up to 10 MB and about four minutes of speech per memo, and 60
memos an hour from each device. A memo it cannot take is skipped with Sesame's
reason, and stays in Voice Memos.

### Sending every memo

Since Sesame transcribes the recording itself, you can skip the local step and
send every memo, notes included:

```bash
uv run murmur.py --classifier off
```

parakeet then never loads, and Sesame answers every memo.

## Sending requests to a Grok Bot routine

`--sink cursor` wakes a **Grok Bot routine** through Cursor's automation
webhook. Set it up once:

1. In the Grok Bot desktop app, create a routine and give it these instructions:

   > You handle voice note requests. The webhook payload contains a single
   > voice-note transcription in `context`, prefixed with the capture device it
   > came from. It was dictated, so expect filler words, false starts and
   > transcription errors — infer intent rather than reading literally. Answer
   > the request directly: research it, then reply with a short, concrete
   > answer. If the request is ambiguous, answer the most likely reading and
   > state what you assumed. Do not ask clarifying questions — nobody is
   > watching this thread live.

   That last line matters. A webhook run has no one in the loop, so a routine
   that asks a question just stalls.

2. Add a trigger, choose **webhook**, and copy the **POST URL** and **sender key**
   off the trigger card. They are shown on desktop only, not on iOS.

3. Check the routine wakes, before involving murmur at all:

   ```bash
   curl -X POST https://api2.cursor.sh/automations/webhook/<routine-id> \
     -H "Authorization: Bearer crsr_<sender-key>" \
     -H "Content-Type: application/json" \
     -d '{"context":"What is the tallest mountain in Japan? Reply with just the name."}'
   ```

   If the routine answers *Mount Fuji*, everything downstream is wiring. Doing
   this first separates "is the webhook set up right" from "is the classifier
   deciding correctly", which are much easier to debug apart than together.

4. Put both values in `.env`:

```bash
MURMUR_SINK=cursor
MURMUR_WEBHOOK_URL=https://api2.cursor.sh/automations/webhook/<routine-id>
MURMUR_WEBHOOK_TOKEN=crsr_<sender-key>
```

Then `uv run murmur.py`. Each request is POSTed as `{"context": "..."}` and the
routine answers in its own thread, so turn on notifications for that bot or the
answers just sit there.

You do not need a template variable for this. What you POST as `context` reaches
the routine's prompt on its own — there is no `{{context}}` to write anywhere.
The curl above is the quickest way to confirm that for yourself.

One caveat worth knowing before you lean on it: a webhook wake is not treated as
you typing, so a routine that tries to *act* on the outside world — send mail,
buy something — can still stop at an approval card. Questions and research come
back unattended; outbound actions may not.

For anything else, such as Hermes, openclaw, n8n, Home Assistant, a Shortcut or
your own server:

```bash
uv run murmur.py --sink webhook --webhook-url https://example.com/hook
```

It POSTs:

```json
{
  "text": "can you check the train times to brighton",
  "source": "voice memo",
  "kind": "request",
  "ts": "2026-08-14T19:51:54+01:00"
}
```

Set `MURMUR_WEBHOOK_TOKEN` to have it sent as a bearer token.

### The old Grok Bot sink

`--sink grokbot` pushed straight into the desktop app's own gateway, using
credentials the app left in `~/.grokbot/`. Desktop app 0.36 and later encrypt
that file, so those credentials can no longer be read and the sink only works on
much older versions. It is kept for anyone still on one; everyone else wants
`--sink cursor` above.

## Keeping your notes

Notes are logged and then discarded, since this is a pipe to your agent rather
than a note app. To keep them as dated markdown instead:

```bash
uv run murmur.py --notes-dir ~/Documents/voice-notes
```

## Using an API key

The built-in rule is conservative and treats anything ambiguous as a note. For
better accuracy, add a key to `.env`:

```bash
cp .env.example .env
# GROQ_API_KEY=gsk_...
```

Whichever key you set gets picked up. The model in use is printed at startup:

```
asr=local  classifier=groq:llama-3.1-8b-instant  sink=sesame/voice capture @ capture.sesameai.app
```

| Provider | Classifier | Transcription |
|---|---|---|
| `groq` | `llama-3.1-8b-instant` | `whisper-large-v3-turbo` |
| `openai` | `gpt-5.4-nano` | `gpt-transcribe` |
| `gemini` | `gemini-3.5-flash-lite` | use local or another provider |

Groq is preferred if you have several keys set. Use `--provider` to force one,
and `MURMUR_CHAT_MODEL` to pin a different model.

The same key can do transcription too, which is how to run this on an Intel Mac:

```bash
uv run murmur.py --asr groq
```

Note that `gpt-transcribe` is OpenAI's model for completed audio files.
`gpt-live-transcribe` is for streaming sessions and will not work here.

## Options

| Flag | Default | |
|---|---|---|
| `--sink` | `sesame` | `sesame`, `cursor`, `webhook` or `grokbot` |
| `--agent` | *active one* | `--sink grokbot` only: agent to send to, by name |
| `--agents` | | `--sink grokbot` only: list your agents and exit |
| `--webhook-url` | | where to POST: the Sesame device URL, the routine's URL, or your own |
| `--webhook-token` | | bearer token: the Sesame device secret, or the routine's sender key |
| `--notes-dir` | *discard* | keep non-requests as markdown |
| `--asr` | `local` | `local`, `groq`, `openai` |
| `--classifier` | `auto` | `auto`, `llm`, `heuristic`, or `off` to send every memo |
| `--provider` | *auto* | force `groq`, `openai` or `gemini` |
| `--memos-dir` | *auto-detected* | override the Voice Memos folder |
| `--prefix` | `[from voice memo] ` | prepended for `cursor` and `grokbot` |
| `--backfill N` | `0` | process N recent memos on first run |
| `--dry-run` | off | print, don't send |
| `--once` | off | single pass, then exit |

Every flag also reads from an env var (`MURMUR_SINK`, `MURMUR_NOTES_DIR` and so
on), via `.env` or your shell.

## Running in the background

```bash
nohup uv run murmur.py > ~/murmur.log 2>&1 &
```

For something that survives reboots, wrap it in a LaunchAgent. Try it in the
foreground first.

## Troubleshooting

**"Could not find your Voice Memos folder"**. Record one memo first. If it is
still not found, your terminal may need Full Disk Access in System Settings >
Privacy & Security, or you can pass `--memos-dir` directly.

**"Sesame rejected this device (HTTP 401/404)"**. The device URL or secret is
wrong, or the device was removed. Copy both from Voice Capture settings again.
**New URL and secret** there replaces both.

**"skipped; the memo is still in Voice Memos"**. Sesame cannot take that
recording, and the line above it says why. Usually the memo is too long. If
Voice Memos records in Lossless, Compressed files are about a quarter of the
size.

**"the webhook rejected this (HTTP 401/403/404)"**. The POST URL or sender key
is wrong, or the routine was deleted. Recheck both on the routine's trigger card
in the desktop app — the key starts `crsr_`, and is passed without the `Bearer `
prefix.

**"Grok Bot's local gateway is no longer reachable"**. You are on `--sink
grokbot` with a current desktop app, which encrypts the credentials that sink
needs. Switch to `--sink cursor`.

**"no Grok Bot credentials"**. `--sink grokbot` only: the desktop app is not
installed or not signed in on this machine. That file only exists where the app
runs.

**Transcription is wrong**. parakeet is verbatim, so it keeps your "um"s and
sometimes mangles proper nouns. Try a bigger model with
`MURMUR_ASR_MODEL=mlx-community/parakeet-tdt-1.1b`.

## Tests

```bash
uv run test_classify.py
uv run test_sinks.py
```

18 memos checked against the no-API rule, 9 cases for reading a model's answer,
and the Sesame, webhook and note paths against a local server.

## About the Grok Bot connection

The `cursor` sink posts to the same automation webhook endpoint the desktop app
gives you on a routine's trigger card. The URL and sender key are stable, so
there is nothing to re-read or refresh between sends.

The retired `grokbot` sink worked differently, and the difference is why it
stopped working. It called undocumented endpoints the desktop app uses for
itself, `POST /api/sendPrompt` and `listAgents`, with credentials the app left in
`~/.grokbot/` and rotated on every restart of your agent's VM. Despite appearances
that was never a localhost endpoint: the call went over HTTPS to the VM, and only
the credentials were local. Desktop app 0.36 encrypts that file, and the sink has
no way in.

Treat the webhook as the supported path and that one as history.

MIT.
