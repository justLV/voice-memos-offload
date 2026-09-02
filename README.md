# Voice Memos Offload

Run this on an always on Mac device. Any voice memos you record from any iCloud sync'd Apple device will be transcribed, and routed as either a request to your agent or to your notes database.

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
  (.m4a)         on-device ASR    request or       Grok Bot routine
                                     note?          or any webhook
```

Transcription runs on your Mac with
[parakeet-mlx](https://github.com/senstella/parakeet-mlx), so no audio is
uploaded. It takes about a second for a ten second memo once the model is loaded.

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

## Where requests go

The default sink wakes a **Grok Bot routine** through Cursor's automation
webhook. Set it up once:

1. In the Grok Bot desktop app, create a routine. Give it instructions along the
   lines of *"the payload holds one voice-note transcription in `context`. It was
   dictated, so expect filler words and transcription errors — infer intent.
   Answer it directly and don't ask clarifying questions, nobody is watching."*
2. Add a trigger, choose **webhook**, and copy the **POST URL** and **sender key**
   off the trigger card. They are shown on desktop only, not on iOS.
3. Put both in `.env`:

```bash
MURMUR_WEBHOOK_URL=https://api2.cursor.sh/automations/webhook/<routine-id>
MURMUR_WEBHOOK_TOKEN=crsr_<sender-key>
```

Then `uv run murmur.py`. Each request is POSTed as `{"context": "..."}` and the
routine answers in its own thread, so turn on notifications for that bot or the
answers just sit there.

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
asr=local  classifier=groq:llama-3.1-8b-instant  sink=cursor/routine webhook @ api2.cursor.sh
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
| `--sink` | `cursor` | `cursor`, `webhook` or `grokbot` |
| `--agent` | *active one* | `--sink grokbot` only: agent to send to, by name |
| `--agents` | | `--sink grokbot` only: list your agents and exit |
| `--webhook-url` | | where to POST: the routine's URL, or your own |
| `--webhook-token` | | bearer token; the sender key for `--sink cursor` |
| `--notes-dir` | *discard* | keep non-requests as markdown |
| `--asr` | `local` | `local`, `groq`, `openai` |
| `--classifier` | `auto` | `auto`, `llm`, `heuristic` |
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
and the webhook and note paths against a local server.

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
