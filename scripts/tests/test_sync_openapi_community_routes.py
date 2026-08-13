"""Generator sees isolated Community routers (no network, no spend)."""
from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "scripts" / "sync_openapi_from_routes.py"


def _load():
    spec = importlib.util.spec_from_file_location("sync_openapi_from_routes", MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_collect_impl_includes_community_ga_routes():
    module = _load()
    impl = module.collect_impl()
    expected = {
        "/v1/previews/fixture": "post",
        "/v1/providers/easy-connect": "post",
        "/v1/comms/outbox/{message_id}/dry-run": "post",
        "/v1/meetings/standup": "post",
        "/v1/meetings/simulated-run": "post",
        "/v1/meetings/{meeting_id}/evidence": "get",
        "/v1/assurance/seed-demo": "post",
        "/v1/entitlements/sources/{source_id}/openai-compatible": "post",
    }
    missing = [
        f"{method.upper()} {path}"
        for path, method in expected.items()
        if method not in impl.get(path, set())
    ]
    assert missing == [], missing
