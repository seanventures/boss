"""BOSS HAS NO CONNECTION TO THE PRODUCT ITS IDENTITY SCREENS WERE MODELLED ON (operator rule).

That product was a design reference for BOSS Identity and its sign-in layout, nothing more: no
code, comment, fixture, test account, asset or document in the tracked tree names it. Fixtures
use a neutral fake domain (example.com, example.test, boss.test). The word is assembled below so
the scan never finds this file itself (this docstring used to spell it, and the test failed on
its own text). The web suite has a twin: apps/web/tests/the-tree-names-no-design-reference.test.ts.
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
