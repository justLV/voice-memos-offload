"""Sanity-check the no-API classifier against realistic memos.

Run: uv run test_classify.py
"""
# /// script
# requires-python = ">=3.11"
# dependencies = ["httpx>=0.27"]
# ///
import importlib.util
import sys
from pathlib import Path

spec = importlib.util.spec_from_file_location("murmur", Path(__file__).parent / "murmur.py")
murmur = importlib.util.module_from_spec(spec)
sys.modules["murmur"] = murmur  # @dataclass resolves the module by name
spec.loader.exec_module(murmur)

REQUESTS = [
    "Can you check for me um what spots are available for Pilates on Sunday",
    "Can you look up the train times to Brighton tomorrow morning",
    "Hey, find me a decent ramen place near the office",
    "What's the best way to remove a stripped screw?",
    "Could you email Sarah and tell her I'll be ten minutes late",
    "Look up how much a replacement battery costs for the M2 Air",
    "Remind me to renew the car insurance before the 30th",
    "How do I convert an m4a to wav on the command line?",
    "Book me a table for four on Friday at eight",
]

NOTES = [
    "I should really start going to bed earlier",
    "Note to self, the blue cable is the one that goes to the amp",
    "I keep thinking about that conversation with Tom",
    "Today was actually pretty good, got through the whole backlog",
    "I need to remember that the bins go out on Tuesday",
    "Maybe I could rewrite the whole thing in Rust over the holidays",
    "I've been feeling weirdly tired all week",
    "My knee is still sore from the run on Saturday",
    "Thinking about whether the ESP32 board is worth respinning",
]

fails = []
print("expecting REQUEST:")
for t in REQUESTS:
    got = murmur.classify_heuristic(t)
    print(f"  {'ok  ' if got else 'MISS'} {t[:64]}")
    if not got:
        fails.append(("request", t))

print("\nexpecting NOTE:")
for t in NOTES:
    got = murmur.classify_heuristic(t)
    print(f"  {'ok  ' if not got else 'MISS'} {t[:64]}")
    if got:
        fails.append(("note", t))

total = len(REQUESTS) + len(NOTES)
print(f"\n{total - len(fails)}/{total} correct")
if fails:
    print("\nmisclassified:")
    for kind, t in fails:
        print(f"  expected {kind}: {t}")
    sys.exit(1)
print("all correct")
