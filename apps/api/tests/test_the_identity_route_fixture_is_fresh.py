"""The identity console's contract test pins every call to the API's real route table, read from
apps/web/tests/fixtures/identity-api-routes.json. A route added, renamed or removed here without
regenerating that file would let the console drift again; this refuses a stale fixture.

Regenerate with: PYTHONPATH=.:engine python3 scripts/dump-identity-api-routes.py
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def _dump():
    spec = importlib.util.spec_from_file_location("dump_identity_api_routes", ROOT / "scripts/dump-identity-api-routes.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_the_identity_route_fixture_matches_the_api():
    dump = _dump()
    assert dump.FIXTURE.exists(), "run scripts/dump-identity-api-routes.py"
    assert dump.FIXTURE.read_text() == dump.rendered(), (
        "apps/web/tests/fixtures/identity-api-routes.json is stale: "
        "run PYTHONPATH=.:engine python3 scripts/dump-identity-api-routes.py")
