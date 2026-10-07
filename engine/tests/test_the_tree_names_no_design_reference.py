"""BOSS HAS NO CONNECTION TO KAAPPU (operator rule, round 3).

Kaappu was a design reference for BOSS Identity, nothing more: no code, comment, fixture, test
account or document in the tracked tree names it. Fixtures use a neutral fake domain
(example.com, example.test, boss.test). This test is the one place the word is spelled, and it
assembles it so the scan below never finds itself.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORD = "ka" + "appu"


def test_no_tracked_file_mentions_it():
    tracked = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-z"], capture_output=True, check=True).stdout
    hits = []
    for rel in (p.decode() for p in tracked.split(b"\0") if p):
        if WORD in rel.lower():
            hits.append(f"{rel} (path)")
            continue
        path = ROOT / rel
        try:
            data = path.read_bytes()
        except OSError:
            continue
        if WORD.encode() in data.lower():
            hits.append(rel)
    assert not hits, f"the tree mentions {WORD!r}: {hits}"
