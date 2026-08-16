#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "parakeet-mlx>=0.2",
#   "httpx>=0.27",
# ]
# ///
"""
murmur — talk to your agent by leaving yourself a voice memo.

Watches Apple Voice Memos. Transcribes locally on your Mac. Decides whether what
you said was a request for an agent or just a note to yourself. Sends the
requests on; leaves the notes alone.

    uv run murmur.py

Nothing to install, no server, no API key required.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import httpx

# ---------------------------------------------------------------------------
# Where things live
# ---------------------------------------------------------------------------

# Checked in order; first one that exists and holds recordings wins.
MEMO_DIRS = [
    "~/Library/Group Containers/group.com.apple.VoiceMemos.shared/Recordings",
    "~/Library/Application Support/com.apple.voicememos/Recordings",
    "~/Library/Mobile Documents/com~apple~VoiceMemos/Documents",
]

STATE_DIR = Path(
    os.environ.get("XDG_STATE_HOME", "~/.local/state")
).expanduser() / "murmur"
SEEN_FILE = STATE_DIR / "seen.txt"

GROKBOT_CONN = Path("~/.grokbot/local-exec-daemon-connection.json").expanduser()

ASR_MODEL = os.environ.get("MURMUR_ASR_MODEL", "mlx-community/parakeet-tdt-0.6b-v3")

# The answer we want is one token ("YES" or "NO"), but many current models spend
# reasoning tokens before emitting any content — and a model that hits its cap
# mid-thought returns a message with no content at all. So the cap is only a
# runaway guard, deliberately far above what the reply needs. Being generous
# costs nothing when the model is not a thinking one: it still emits one token.
REPLY_BUDGET = 2048

# Every provider below speaks the OpenAI chat-completions dialect, so one code
# path covers all three. Models are pinned here and overridable by env, since
# hosted model names get retired.
PROVIDERS = {
    "groq": {
        "base": "https://api.groq.com/openai/v1",
        "key": "GROQ_API_KEY",
        # Smallest/fastest instruct model (~560 tok/s). This is a YES/NO call,
        # so a 70B would be wasted latency.
        "chat": "llama-3.1-8b-instant",
        "asr": "whisper-large-v3-turbo",
        "params": {"temperature": 0, "max_tokens": REPLY_BUDGET},
    },
    "openai": {
        "base": "https://api.openai.com/v1",
        "key": "OPENAI_API_KEY",
        "chat": "gpt-5.4-nano",
        # gpt-transcribe is the async/batch model. Its sibling
        # gpt-live-transcribe is for streaming Realtime sessions, not files.
        "asr": "gpt-transcribe",
        # GPT-5-family rejects max_tokens outright; it wants
        # max_completion_tokens, and pins its own temperature.
        "params": {"max_completion_tokens": REPLY_BUDGET},
    },
    "gemini": {
        "base": "https://generativelanguage.googleapis.com/v1beta/openai",
        "key": "GEMINI_API_KEY",
        "chat": "gemini-3.5-flash-lite",
        "asr": None,  # dedicated ASR beats it; use local or another provider
        "params": {"temperature": 0, "max_tokens": REPLY_BUDGET},
    },
}

CLASSIFY_PROMPT = """\
You classify voice transcriptions. Is this a 2nd-person request directed at an \
AI assistant — i.e. the speaker is talking TO someone, asking "you" to do something?

Say YES only if the speaker is addressing an assistant directly, e.g. \
"Can you look up...", "Hey, find me...", "What's the best way to...".

Say NO for everything else: notes-to-self ("I should really..."), journaling, \
reminders, stream-of-consciousness, 1st-person plans, or anything where the \
speaker is talking to themselves.

When in doubt, say NO.

Respond with exactly: YES or NO"""


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

@dataclass
class Config:
    memos_dir: Path
    sink: str
    webhook_url: str | None
    webhook_token: str | None
    notes_dir: Path | None
    asr: str
    classifier: str
    provider: str | None
    prefix: str
    poll: float
    dry_run: bool
    agent: str | None = None


def load_dotenv(path: Path = Path(".env")) -> None:
    """Minimal .env reader — no dependency needed for six lines of parsing."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip().strip("'\""))


def find_memos_dir(override: str | None) -> Path:
    """Locate Voice Memos: explicit override, known locations, then Spotlight."""
    if override:
        p = Path(override).expanduser()
        if not p.is_dir():
            sys.exit(f"! --memos-dir does not exist: {p}")
        return p

    candidates = [Path(d).expanduser() for d in MEMO_DIRS]
    found = [p for p in candidates if p.is_dir() and any(p.glob("*.m4a"))]
    if found:
        # If several exist, trust whichever was written to most recently.
        return max(found, key=lambda p: max(f.stat().st_mtime for f in p.glob("*.m4a")))

    # Nothing in the usual places — ask Spotlight where the recordings went.
    try:
        out = subprocess.run(
            ["mdfind", "-name", ".m4a", "-onlyin", str(Path.home() / "Library")],
            capture_output=True, text=True, timeout=10,
        ).stdout
        hits = [Path(l).parent for l in out.splitlines() if "VoiceMemos" in l]
        if hits:
            return max(set(hits), key=hits.count)
    except Exception:
        pass

    sys.exit(
        "! Could not find your Voice Memos folder.\n"
        "  Record one memo, then pass the folder explicitly:\n"
        "      uv run murmur.py --memos-dir '<path>'\n"
        "  If the folder exists but looks empty, grant your terminal\n"
        "  Full Disk Access in System Settings > Privacy & Security."
    )


# ---------------------------------------------------------------------------
# Transcription
# ---------------------------------------------------------------------------

_model = None


def to_wav(src: Path, dst: Path) -> Path:
    """Transcode to 16 kHz mono WAV using afconvert, which ships with macOS.

    Doing this ourselves means no ffmpeg install just to read an .m4a.
    """
    subprocess.run(
        ["afconvert", "-f", "WAVE", "-d", "LEI16@16000", "-c", "1", str(src), str(dst)],
        check=True, capture_output=True,
    )
    return dst


def transcribe_local(audio: Path) -> str:
    global _model
    if _model is None:
        from parakeet_mlx import from_pretrained  # imported lazily: it's a big import

        print(f"  loading {ASR_MODEL} (first run downloads it)...", flush=True)
        _model = from_pretrained(ASR_MODEL)
    wav = to_wav(audio, audio.parent / f".murmur-{audio.stem}.wav")
    try:
        return _model.transcribe(str(wav)).text.strip()
    finally:
        wav.unlink(missing_ok=True)


def transcribe_remote(audio: Path, provider: str) -> str:
    """Whisper-style multipart upload — same shape for Groq and OpenAI."""
    p = PROVIDERS[provider]
    if not p["asr"]:
        sys.exit(f"! {provider} has no transcription endpoint; use --asr local")
    key = os.environ.get(p["key"])
    if not key:
        sys.exit(f"! --asr {provider} needs {p['key']}")
    wav = to_wav(audio, audio.parent / f".murmur-{audio.stem}.wav")
    try:
        with wav.open("rb") as fh:
            r = httpx.post(
                f"{p['base']}/audio/transcriptions",
                headers={"Authorization": f"Bearer {key}"},
                files={"file": (wav.name, fh, "audio/wav")},
                data={"model": os.environ.get("MURMUR_ASR_REMOTE_MODEL", p["asr"]),
                      "response_format": "text"},
                timeout=120,
            )
        r.raise_for_status()
        return r.text.strip()
    finally:
        wav.unlink(missing_ok=True)


def transcribe(audio: Path, cfg: Config) -> str:
    if cfg.asr == "local":
        return transcribe_local(audio)
    return transcribe_remote(audio, cfg.asr)


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

ADDRESSED = re.compile(r"\b(can|could|would|will)\s+(you|u)\b", re.I)
IMPERATIVE = re.compile(
    # optional greeting, optionally with a name: "hey", "hey siri,", "ok claude "
    r"^\s*((?:hey|hi|yo|okay|ok)\b[\s,]*(?:\w+[\s,]+)?)?"
    r"(look up|find|search|check|tell me|remind me|add|send|"
    r"email|text|book|schedule|order|play|set|make|draft|summarize|summarise|"
    r"what'?s|what is|when is|where is|how do i|how much|who is)\b", re.I)
SELF_TALK = re.compile(
    r"^\s*(note to self|i\s|i'?m|i'?ve|i'?ll|my |today |remember that|thinking about|"
    r"maybe i|i should|i need|i want|i have to|i keep)", re.I)


def classify_heuristic(text: str) -> bool:
    """No-API fallback: is the speaker addressing someone, or thinking aloud?"""
    score = 0
    if ADDRESSED.search(text):
        score += 2
    if IMPERATIVE.search(text):
        score += 2
    if text.rstrip().endswith("?"):
        score += 1
    if SELF_TALK.match(text):
        score -= 2
    return score >= 2


def parse_verdict(answer: str) -> bool | None:
    """Pull YES/NO out of a model reply. None if it never committed.

    Takes the *last* standalone verdict rather than the first word, since a
    model that thinks out loud may lead with preamble ("The speaker is asking
    someone... YES") before answering.
    """
    verdicts = re.findall(r"\b(YES|NO)\b", (answer or "").upper())
    return None if not verdicts else verdicts[-1] == "YES"


def pick_provider(preferred: str | None) -> str | None:
    """Use whichever provider the user actually has a key for.

    Without this, defaulting to one provider would silently fall back to the
    heuristic for someone who set a different provider's key.
    """
    if preferred:
        return preferred
    for name in ("groq", "openai", "gemini"):  # fastest first
        if os.environ.get(PROVIDERS[name]["key"]):
            return name
    return None


def classify_llm(text: str, provider: str) -> bool | None:
    """Ask a fast hosted model. Returns None if unavailable, so we can fall back."""
    p = PROVIDERS[provider]
    key = os.environ.get(p["key"])
    if not key:
        return None
    try:
        r = httpx.post(
            f"{p['base']}/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json={
                "model": os.environ.get("MURMUR_CHAT_MODEL", p["chat"]),
                "messages": [
                    {"role": "system", "content": CLASSIFY_PROMPT},
                    {"role": "user", "content": text},
                ],
                **p["params"],
            },
            timeout=20,
        )
        r.raise_for_status()
        # A reply truncated mid-thought can omit "content" entirely, so don't
        # index blindly.
        answer = (r.json()["choices"][0]["message"].get("content") or "").strip()
        verdict = parse_verdict(answer)
        if verdict is None:
            print(f"  ! classifier gave no verdict ({answer[:60]!r}), using heuristic")
        return verdict
    except Exception as e:
        print(f"  ! classifier unavailable ({type(e).__name__}), using heuristic")
        return None


def classify(text: str, cfg: Config) -> bool:
    if cfg.classifier in ("auto", "llm"):
        if cfg.provider:
            verdict = classify_llm(text, cfg.provider)
            if verdict is not None:
                return verdict
        if cfg.classifier == "llm":
            keys = ", ".join(p["key"] for p in PROVIDERS.values())
            sys.exit(f"! --classifier llm needs one of: {keys}")
    return classify_heuristic(text)


# ---------------------------------------------------------------------------
# Sinks
# ---------------------------------------------------------------------------

def grokbot_conn() -> tuple[str, dict] | None:
    """Base URL + headers. Credentials rotate when the box restarts, so this is
    re-read on every use rather than cached."""
    if not GROKBOT_CONN.exists():
        print(f"  ! no Grok Bot credentials at {GROKBOT_CONN}")
        print("    Is the Grok Bot desktop app installed and signed in?")
        return None
    conn = json.loads(GROKBOT_CONN.read_text())
    return conn["baseUrl"].rstrip("/"), {
        "authorization": f"Bearer {conn['token']}",
        "content-type": "application/json",
        **(conn.get("headers") or {}),
    }


def grokbot_agents(base: str, headers: dict) -> list[dict]:
    r = httpx.post(f"{base}/api/listAgents", headers=headers, json={}, timeout=20)
    r.raise_for_status()
    return r.json()


def resolve_agent(base: str, headers: dict, cfg: Config) -> tuple[str | None, str]:
    """Find the agent to send to. Returns (agent_id, human label).

    With no agent configured this falls back to whichever one is frontmost in
    the app, which is rarely what you want.
    """
    want = cfg.agent
    if not want:
        active = httpx.get(f"{base}/health", headers=headers, timeout=20).json().get(
            "activeAgentId")
        return active, "whichever agent is active"

    agents = grokbot_agents(base, headers)
    lowered = want.lower()
    for match in (lambda a: a["id"] == want,
                  lambda a: (a.get("name") or "").lower() == lowered,
                  lambda a: lowered in (a.get("name") or "").lower()):
        hits = [a for a in agents if match(a)]
        if len(hits) == 1:
            return hits[0]["id"], hits[0].get("name") or hits[0]["id"]
        if len(hits) > 1:
            names = ", ".join(repr(a.get("name")) for a in hits)
            print(f"  ! {want!r} matches several agents: {names}")
            return None, want

    print(f"  ! no agent named {want!r}. Available: "
          + ", ".join(repr(a.get('name')) for a in agents))
    return None, want


def send_grokbot(text: str, cfg: Config) -> bool:
    """Push into the Grok Bot desktop app via its local gateway."""
    got = grokbot_conn()
    if got is None:
        return False
    base, headers = got

    agent_id, label = resolve_agent(base, headers, cfg)
    if not agent_id:
        return False
    print(f"  → {label}")
    now = int(time.time() * 1000)
    r = httpx.post(
        f"{base}/api/sendPrompt",
        headers=headers,
        json={
            "prompt": text,
            "agentId": agent_id,
            "clientNonce": str(uuid.uuid4()),
            "directAddressedAcceptance": True,
            "attachmentPaths": [],
            "attachmentNames": [],
            "composedAtMs": now,
            "enterEpochMs": now,
        },
        timeout=30,
    )
    r.raise_for_status()
    return bool(r.json().get("accepted"))


def send_webhook(text: str, cfg: Config) -> bool:
    """POST to anything: Hermes, openclaw, n8n, Shortcuts, your own server."""
    if not cfg.webhook_url:
        sys.exit("! --sink webhook needs --webhook-url (or MURMUR_WEBHOOK_URL)")
    headers = {"content-type": "application/json"}
    if cfg.webhook_token:
        headers["authorization"] = f"Bearer {cfg.webhook_token}"
    r = httpx.post(
        cfg.webhook_url,
        headers=headers,
        json={
            "text": text,
            "source": "voice memo",
            "kind": "request",
            "ts": datetime.now().astimezone().isoformat(),
        },
        timeout=30,
    )
    r.raise_for_status()
    return True


SINKS = {"grokbot": send_grokbot, "webhook": send_webhook}


def save_note(text: str, when: datetime, cfg: Config) -> None:
    """Notes aren't sent anywhere. Optionally keep them as dated markdown."""
    if not cfg.notes_dir:
        return
    cfg.notes_dir.mkdir(parents=True, exist_ok=True)
    path = cfg.notes_dir / f"{when.strftime('%Y-%m-%d %H.%M.%S')}.md"
    path.write_text(
        f"---\ncreated: {when.isoformat(timespec='seconds')}\n"
        f"source: voice memo\ntags: [voice]\n---\n{text}\n"
    )
    print(f"  saved note -> {path}")


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

def load_seen() -> set[str]:
    if not SEEN_FILE.exists():
        return set()
    return {l.strip() for l in SEEN_FILE.read_text().splitlines() if l.strip()}


def mark_seen(name: str) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with SEEN_FILE.open("a") as f:
        f.write(name + "\n")


def baseline(memos: Path, keep_last: int) -> set[str]:
    """First run: mark existing memos as seen so we don't replay your history.

    Without this, installing murmur would fire every memo you have ever recorded
    at your agent. `--backfill N` opts the N most recent ones back in.
    """
    files = sorted(memos.glob("*.m4a"), key=lambda f: f.stat().st_mtime)
    skip = files[: len(files) - keep_last] if keep_last else files
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    SEEN_FILE.write_text("".join(f"{f.name}\n" for f in skip))
    print(f"  first run: ignoring {len(skip)} existing memo(s)"
          + (f", processing the {keep_last} most recent" if keep_last else ""))
    return {f.name for f in skip}


def settled(path: Path, tries: int = 20) -> bool:
    """Wait for the file to stop growing — Voice Memos writes progressively."""
    last = -1
    for _ in range(tries):
        if not path.exists():
            return False
        size = path.stat().st_size
        if size > 0 and size == last:
            return True
        last = size
        time.sleep(0.4)
    return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def handle(path: Path, cfg: Config) -> None:
    print(f"\n▸ {path.name}")
    if not settled(path):
        print("  still being written, will retry next pass")
        return

    started = time.time()
    text = transcribe(path, cfg)
    if not text:
        print("  (silence)")
        mark_seen(path.name)
        return
    print(f'  "{text}"  [{time.time() - started:.1f}s]')

    if not classify(text, cfg):
        print("  note — not sent")
        save_note(text, datetime.fromtimestamp(path.stat().st_mtime), cfg)
        mark_seen(path.name)
        return

    message = f"{cfg.prefix}{text}" if cfg.sink == "grokbot" else text
    if cfg.dry_run:
        target = f"grokbot/{cfg.agent or 'active agent'}" if cfg.sink == "grokbot" else cfg.sink
        print(f"  REQUEST → would send to {target}: {message}")
        mark_seen(path.name)
        return

    try:
        ok = SINKS[cfg.sink](message, cfg)
        print(f"  REQUEST → sent to {cfg.sink}" if ok else "  ! send rejected")
    except Exception as e:
        print(f"  ! send failed ({type(e).__name__}: {e}) — will retry next pass")
        return  # deliberately not marked seen, so it retries
    mark_seen(path.name)


def main() -> None:
    load_dotenv()
    ap = argparse.ArgumentParser(
        description="Voice memos → your agent.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="examples:\n"
               "  uv run murmur.py --dry-run\n"
               "  uv run murmur.py --sink webhook --webhook-url https://...\n"
               "  uv run murmur.py --notes-dir ~/Obsidian/voice\n",
    )
    ap.add_argument("--memos-dir", default=os.environ.get("MURMUR_MEMOS_DIR"))
    ap.add_argument("--sink", default=os.environ.get("MURMUR_SINK", "grokbot"),
                    choices=sorted(SINKS))
    ap.add_argument("--webhook-url", default=os.environ.get("MURMUR_WEBHOOK_URL"))
    ap.add_argument("--webhook-token", default=os.environ.get("MURMUR_WEBHOOK_TOKEN"))
    ap.add_argument("--notes-dir", default=os.environ.get("MURMUR_NOTES_DIR"),
                    help="keep non-requests as markdown here (default: discard)")
    ap.add_argument("--asr", default=os.environ.get("MURMUR_ASR", "local"),
                    choices=["local", "groq", "openai"])
    ap.add_argument("--classifier", default=os.environ.get("MURMUR_CLASSIFIER", "auto"),
                    choices=["auto", "llm", "heuristic"],
                    help="auto: use an API key if present, else a local rule")
    ap.add_argument("--provider", default=os.environ.get("MURMUR_PROVIDER"),
                    choices=sorted(PROVIDERS),
                    help="default: whichever provider you have a key for")
    ap.add_argument("--agent", default=os.environ.get("MURMUR_AGENT"),
                    help="Grok Bot agent to send to, by name or id "
                         "(default: whichever is active in the app)")
    ap.add_argument("--agents", action="store_true",
                    help="list your Grok Bot agents and exit")
    ap.add_argument("--prefix", default=os.environ.get("MURMUR_PREFIX", "[from voice memo] "))
    ap.add_argument("--poll", type=float, default=3.0)
    ap.add_argument("--backfill", type=int, default=0, metavar="N",
                    help="on first run, also process the N most recent memos")
    ap.add_argument("--once", action="store_true", help="one pass, then exit")
    ap.add_argument("--dry-run", action="store_true",
                    help="print what would be sent, send nothing")
    a = ap.parse_args()

    if a.agents:
        got = grokbot_conn()
        if got is None:
            sys.exit(1)
        base, headers = got
        active = httpx.get(f"{base}/health", headers=headers, timeout=20).json().get(
            "activeAgentId")
        for ag in grokbot_agents(base, headers):
            mark = "*" if ag["id"] == active else " "
            desc = (ag.get("description") or "").replace("\n", " ")[:58]
            print(f" {mark} {ag.get('name', '?'):<26} {desc}")
        print("\n * = currently active in the app")
        print("   pin one with --agent NAME or MURMUR_AGENT=NAME")
        return

    if not shutil.which("afconvert"):
        sys.exit("! afconvert not found — murmur needs macOS.")

    memos = find_memos_dir(a.memos_dir)
    cfg = Config(
        memos_dir=memos, sink=a.sink, webhook_url=a.webhook_url,
        webhook_token=a.webhook_token,
        notes_dir=Path(a.notes_dir).expanduser() if a.notes_dir else None,
        asr=a.asr, classifier=a.classifier, provider=pick_provider(a.provider),
        prefix=a.prefix, poll=a.poll, dry_run=a.dry_run,
        agent=a.agent,
    )

    first_run = not SEEN_FILE.exists()
    # Say plainly which classifier is actually in play, so a missing key is
    # visible up front rather than a silent downgrade.
    if cfg.classifier != "heuristic" and cfg.provider:
        how = f"{cfg.provider}:{os.environ.get('MURMUR_CHAT_MODEL', PROVIDERS[cfg.provider]['chat'])}"
    else:
        how = "heuristic (no API key — set one for sharper results)"
    where = cfg.sink
    if cfg.sink == "grokbot":
        where += f"/{cfg.agent}" if cfg.agent else "/active agent (set --agent to pin one)"
    print(f"murmur — watching {memos}")
    print(f"  asr={cfg.asr}  classifier={how}  sink={where}"
          + ("  [dry run]" if cfg.dry_run else ""))
    seen = baseline(memos, a.backfill) if first_run else load_seen()

    try:
        while True:
            for f in sorted(memos.glob("*.m4a"), key=lambda f: f.stat().st_mtime):
                if f.name in seen:
                    continue
                handle(f, cfg)
                seen = load_seen()
            if a.once:
                break
            time.sleep(cfg.poll)
    except KeyboardInterrupt:
        print("\nbye")


if __name__ == "__main__":
    main()
