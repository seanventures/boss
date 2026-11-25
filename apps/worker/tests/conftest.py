"""Some worker tests import the package as `boss_worker` (how the worker image runs it), others as
`apps.worker.boss_worker` (from the repository root). Put apps/worker on the path so both work
under the same PYTHONPATH=.:engine every other suite uses (scripts/test-tier.sh, CI)."""
from __future__ import annotations

import sys
from pathlib import Path

WORKER = str(Path(__file__).resolve().parents[1])
if WORKER not in sys.path:
    sys.path.insert(0, WORKER)
