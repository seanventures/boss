"""Self-tests for the end-to-end harness. Fast, no database, no Docker, no browser.

They prove the harness's own parts — the scripted model, the fake `claude`, the fakes, the stack's
environment contract and the readers that discover what to cover — so that when the harness says
a template or a screen failed, the harness is not the thing that is broken.
"""
from __future__ import annotations

import sys
from pathlib import Path

TESTS = Path(__file__).resolve().parents[2]
REPO = TESTS.parent
for path in (TESTS, REPO, REPO / "engine", TESTS / "e2e" / "stubs"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))
