"""Exercise the Sesame and webhook sinks against a real local server, and the note writer.

Run: uv run test_sinks.py
"""
# /// script
# requires-python = ">=3.11"
# dependencies = ["httpx>=0.27"]
# ///
import importlib.util
import json
import sys
import tempfile
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

spec = importlib.util.spec_from_file_location("murmur", Path(__file__).parent / "murmur.py")
murmur = importlib.util.module_from_spec(spec)
sys.modules["murmur"] = murmur
spec.loader.exec_module(murmur)

received = {}


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get("content-length", 0))
        raw = self.rfile.read(n)
        received["raw"] = raw
        received["headers"] = {k.lower(): v for k, v in self.headers.items()}
        received["body"] = json.loads(raw) if received["headers"].get(
            "content-type") == "application/json" else None
        received["auth"] = self.headers.get("authorization")
        if self.path == "/deny":
            self.send_response(401)
            self.end_headers()
            self.wfile.write(b'{"code":"error","message":"Invalid API key"}')
            return
        if self.path == "/too-large":
            self.send_response(413)
            self.end_headers()
            self.wfile.write(b"The recording is too large. Sesame accepts up to 10 MB.")
            return
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *a):
        pass


srv = HTTPServer(("127.0.0.1", 0), Handler)
port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()

tmp = Path(tempfile.mkdtemp(prefix="murmur-test-"))
cfg = murmur.Config(
    memos_dir=tmp, sink="webhook", webhook_url=f"http://127.0.0.1:{port}/hook",
    webhook_token="test-token-123", notes_dir=tmp / "notes", asr="local",
    classifier="heuristic", provider="groq", prefix="[from voice memo] ",
    poll=1.0, dry_run=False,
)

print("--- webhook sink ---")
ok = murmur.send_webhook("can you check the train times to brighton", cfg)
print("returned:", ok)
print("received body:", json.dumps(received.get("body"), indent=2))
print("auth header:", received.get("auth"))

assert ok
b = received["body"]
assert b["text"] == "can you check the train times to brighton"
assert b["source"] == "voice memo" and b["kind"] == "request"
assert b.get("ts")
assert received["auth"] == "Bearer test-token-123"
print("PASS: webhook delivered correct payload + bearer token")

print("\n--- cursor sink ---")
received.clear()
cfg_cursor = murmur.Config(**{**cfg.__dict__, "sink": "cursor"})
ok = murmur.send_cursor("[from voice memo] what is the tide doing tomorrow", cfg_cursor)
print("returned:", ok)
print("received body:", json.dumps(received.get("body"), indent=2))
print("auth header:", received.get("auth"))

assert ok
b = received["body"]
# A routine reads exactly one field, `context`. Anything else we invent would be
# silently dropped, so assert the payload is only that.
assert list(b) == ["context"], f"cursor payload should be just context, got {list(b)}"
assert b["context"] == "[from voice memo] what is the tide doing tomorrow"
assert received["auth"] == "Bearer test-token-123"
print("PASS: cursor sink posted {context} with the sender key as a bearer token")

print("\n--- cursor sink rejects bad credentials loudly ---")
# 401/403/404 mean the URL or key is wrong, which will fail the same way on every
# future memo. It must stop rather than retry forever.
cfg_401 = murmur.Config(**{**cfg.__dict__, "sink": "cursor",
                           "webhook_url": f"http://127.0.0.1:{port}/deny"})
try:
    murmur.send_cursor("this should not be retried", cfg_401)
except SystemExit as e:
    print("exited with:", str(e).splitlines()[0])
    assert "401" in str(e)
    print("PASS: bad credentials stop the run instead of looping")
else:
    raise AssertionError("expected SystemExit on 401")

print("\n--- sesame sink ---")
received.clear()
memo = tmp / "20261015 101112-ABCD1234.m4a"
memo.write_bytes(b"\x00\x00\x00\x18ftypM4A not really audio")
cfg_sesame = murmur.Config(**{**cfg.__dict__, "sink": "sesame",
                              "webhook_url": f"http://127.0.0.1:{port}/v1/devices/abc"})
ok = murmur.send_sesame(memo, cfg_sesame)
h = received["headers"]
print("returned:", ok)
print("headers:", {k: h.get(k) for k in ("content-type", "idempotency-key", "x-recorded-at")})

assert ok
# Voice Capture takes recordings only, so the body must be the memo's bytes.
assert received["raw"] == memo.read_bytes()
assert h["content-type"] == "audio/mp4"
assert received["auth"] == "Bearer test-token-123"
assert h["x-recorded-at"] == str(int(memo.stat().st_mtime * 1000))
key = h["idempotency-key"]
murmur.send_sesame(memo, cfg_sesame)
assert received["headers"]["idempotency-key"] == key, "a retry must reuse the key"
print("PASS: sesame sink uploaded the raw audio with a stable idempotency key")

print("\n--- sesame sink skips a memo it can never accept ---")
cfg_413 = murmur.Config(**{**cfg_sesame.__dict__,
                           "webhook_url": f"http://127.0.0.1:{port}/too-large"})
try:
    murmur.send_sesame(memo, cfg_413)
except murmur.Rejected as e:
    print("rejected with:", e)
    assert "too large" in str(e)
    print("PASS: a 413 is a rejection, not a retry")
else:
    raise AssertionError("expected Rejected on 413")

print("\n--- sesame sink stops on a bad device URL or secret ---")
cfg_401s = murmur.Config(**{**cfg_sesame.__dict__,
                            "webhook_url": f"http://127.0.0.1:{port}/deny"})
try:
    murmur.send_sesame(memo, cfg_401s)
except SystemExit as e:
    print("exited with:", str(e).splitlines()[0])
    assert "401" in str(e)
    print("PASS: bad device credentials stop the run")
else:
    raise AssertionError("expected SystemExit on 401")

print("\n--- sesame device is checked at startup ---")
received.clear()
murmur.connect_sesame(cfg_sesame)
assert received["headers"]["x-voice-capture-test"] == "true" and not received["raw"]
assert received["auth"] == "Bearer test-token-123"
try:
    murmur.connect_sesame(cfg_401s)
except SystemExit as e:
    print("exited with:", str(e).splitlines()[0])
    assert "401" in str(e)
else:
    raise AssertionError("expected SystemExit on 401 at startup")
print("PASS: startup sends an empty test event, and a bad device stops the run")

print("\n--- classifier off sends every memo to sesame without transcribing ---")
# transcribe() would import parakeet-mlx, which this test does not install, so
# reaching the upload proves the local step was skipped.
received.clear()
orig_state, orig_seen = murmur.STATE_DIR, murmur.SEEN_FILE
murmur.STATE_DIR, murmur.SEEN_FILE = tmp / "state", tmp / "state" / "seen.txt"
try:
    murmur.handle(memo, murmur.Config(**{**cfg_sesame.__dict__, "classifier": "off"}))
    assert received["raw"] == memo.read_bytes()
    assert murmur.load_seen() == {memo.name}
finally:
    murmur.STATE_DIR, murmur.SEEN_FILE = orig_state, orig_seen
print("PASS: memo uploaded and marked seen with no local transcription")

print("\n--- sealed grokbot credentials degrade gracefully ---")
# Desktop app 0.36+ encrypts this file. Reading it used to raise KeyError on
# conn["baseUrl"]; it must explain itself and return None instead.
sealed = tmp / "sealed.json"
sealed.write_text(json.dumps({"sandSealedFile": 1, "data": "encrypted-blob"}))
orig = murmur.GROKBOT_CONN
murmur.GROKBOT_CONN = sealed
try:
    assert murmur.grokbot_conn() is None
finally:
    murmur.GROKBOT_CONN = orig
print("PASS: sealed credential file returns None, no KeyError")

print("\n--- note writer ---")
when = datetime(2026, 8, 14, 19, 51, 54)
murmur.save_note("I should really start going to bed earlier", when, cfg)
note = cfg.notes_dir / "2026-08-14 19.51.54.md"
print(note.read_text())
assert note.exists() and "bed earlier" in note.read_text()
print("PASS: note written as dated markdown")

print("\n--- notes disabled by default ---")
cfg2 = murmur.Config(**{**cfg.__dict__, "notes_dir": None})
murmur.save_note("should not be written anywhere", when, cfg2)
print("PASS: no notes_dir -> silently discarded")

srv.shutdown()
print("\nall sink tests passed")
