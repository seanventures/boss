"""Contract: Community GA routes are documented in api/openapi.yaml.

File-only — does not exercise runtime handlers or spend.
"""
from __future__ import annotations

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[3]
OPENAPI_PATH = ROOT / "api" / "openapi.yaml"

REQUIRED_OPERATIONS = (
    ("post", "/v1/previews/fixture"),
    ("post", "/v1/providers/easy-connect"),
)

COMMUNITY_OPERATIONS = REQUIRED_OPERATIONS + (
    ("post", "/v1/comms/outbox/{message_id}/dry-run"),
    ("post", "/v1/meetings/standup"),
    ("post", "/v1/meetings/simulated-run"),
    ("get", "/v1/meetings/{meeting_id}/evidence"),
    ("post", "/v1/assurance/seed-demo"),
    ("post", "/v1/entitlements/sources/{source_id}/openai-compatible"),
)


def _openapi_paths() -> dict:
    document = yaml.safe_load(OPENAPI_PATH.read_text(encoding="utf-8"))
    paths = document.get("paths") or {}
    assert isinstance(paths, dict)
    return paths


def test_openapi_contains_preview_fixture_and_provider_easy_connect():
    paths = _openapi_paths()
    assert "/v1/previews/fixture" in paths
    assert "post" in paths["/v1/previews/fixture"]
    assert "/v1/providers/easy-connect" in paths
    assert "post" in paths["/v1/providers/easy-connect"]


def test_openapi_contains_community_ga_operations():
    paths = _openapi_paths()
    missing = [
        f"{method.upper()} {path}"
        for method, path in COMMUNITY_OPERATIONS
        if method not in (paths.get(path) or {})
    ]
    assert missing == [], f"OpenAPI missing Community routes: {missing}"
