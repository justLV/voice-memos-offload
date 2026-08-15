# murmur

**Leave yourself a voice memo. Your agent picks it up.**

Record a memo on your iPhone or Mac. murmur notices it, transcribes it locally,
works out whether you were talking *to* someone or just thinking out loud, and
forwards the requests to your agent. Notes stay yours.

```
"Can you check what spots are free for Pilates on Sunday?"   → sent to your agent
"I should really start going to bed earlier"                 → left alone
```

One file. One command. No server, no signup, no API key.

```bash
uv run murmur.py
```

That's the whole install. [uv](https://docs.astral.sh/uv/) reads the dependencies
declared inside `murmur.py` and builds a throwaway environment on the spot.

---

## How it works

```
Voice Memos  →  parakeet-mlx  →  classifier  →  your agent
  (.m4a)        on-device ASR    request or       Grok Bot
                                    note?         or webhook
```

Transcription runs **on your Mac** via
[parakeet-mlx](https://github.com/senstella/parakeet-mlx) — no audio leaves the
machine, and it's fast (about a second for a ten-second memo once the model is
warm).

Classification defaults to a local rule that needs no API key and gets the
common cases right. Add a key and it upgrades itself to a hosted model.

## Requirements

- **Apple Silicon Mac** — parakeet-mlx is MLX-based. (Use `--asr groq` or
  `--asr openai` on Intel.)
- **[uv](https://docs.astral.sh/uv/getting-started/installation/)**
- No ffmpeg needed. murmur transcodes with `afconvert`, which ships with macOS.

## First run

```bash
git clone <this repo> && cd murmur
uv run murmur.py --dry-run
```

`--dry-run` prints what it *would* send without sending anything. Good way to
watch it think before you point it at a live agent.

murmur marks every memo you already have as seen on first launch, so it will
never dump your back catalogue at your agent. To deliberately process recent
ones:

```bash
uv run murmur.py --backfill 3
```

## Sending somewhere

**Grok Bot** (default) needs no configuration at all. If the desktop app is
installed and signed in, murmur reads its local gateway credentials and sends
straight into your chat thread. Credentials rotate on restart, so they're
re-read every time.

**Anything else** — Hermes, openclaw, n8n, Home Assistant, a Shortcut, your own
server:

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

## Keeping your notes

By default, memos classified as notes are logged and discarded — murmur is a
pipe to your agent, not a note app. If you'd rather keep them, point it at a
folder and they'll be written as dated markdown with front matter:

```bash
uv run murmur.py --notes-dir ~/Library/Mobile\ Documents/iCloud~md~obsidian/Documents/voice
```

## Better classification (optional)

The built-in rule is deliberately conservative — when in doubt, it treats
something as a note rather than pestering your agent. For sharper results, drop
a key in `.env`:

```bash
cp .env.example .env
# GROQ_API_KEY=gsk_...
```

murmur picks it up automatically. Groq is the default because it's fast and has
a usable free tier; `--provider openai` and `--provider gemini` also work. The
same key can do transcription too if you'd rather not run the local model:

```bash
uv run murmur.py --asr groq
```

## Options

| Flag | Default | |
|---|---|---|
| `--sink` | `grokbot` | `grokbot` or `webhook` |
| `--webhook-url` | — | where to POST |
| `--notes-dir` | *discard* | keep non-requests as markdown |
| `--asr` | `local` | `local`, `groq`, `openai` |
| `--classifier` | `auto` | `auto`, `llm`, `heuristic` |
| `--provider` | `groq` | which API to use when a key exists |
| `--memos-dir` | *auto-detected* | override the Voice Memos folder |
| `--prefix` | `[from voice memo] ` | prepended for Grok Bot |
| `--backfill N` | `0` | process N recent memos on first run |
| `--dry-run` | off | print, don't send |
| `--once` | off | single pass, then exit |

Every flag also reads from an env var (`MURMUR_SINK`, `MURMUR_NOTES_DIR`, …),
via `.env` or your shell.

## Running it in the background

```bash
nohup uv run murmur.py > ~/murmur.log 2>&1 &
```

For something that survives reboots, wrap it in a LaunchAgent — but try it in
the foreground first.

## Troubleshooting

**"Could not find your Voice Memos folder"** — record one memo first. If it's
still not found, your terminal may need Full Disk Access in System Settings ›
Privacy & Security, or you can pass `--memos-dir` directly.

**"no Grok Bot credentials"** — the desktop app isn't installed or signed in on
this machine. That file only exists where the app runs.

**Transcription is wrong** — parakeet is verbatim, so it keeps your "um"s and
occasionally mangles proper nouns. Try a bigger model with
`MURMUR_ASR_MODEL=mlx-community/parakeet-tdt-1.1b`.

## Testing the classifier

```bash
uv run test_classify.py
```

18 realistic memos, requests and notes, checked against the no-API rule.

## Notes

The Grok Bot transport is an internal gateway with no official API. It works,
but it may change between app versions. The webhook sink is the stable option.

MIT.
