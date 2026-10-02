"""The BOSS end-to-end harness: stubs, a real stack, the workflow driver and the console walk.

See tests/e2e/README.md. Entry point: scripts/e2e.sh (or `make e2e`).
"""
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
E2E_ROOT = Path(__file__).resolve().parents[1]
