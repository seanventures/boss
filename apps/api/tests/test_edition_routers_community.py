"""Edition route boundary that runs from a Community-only source tree."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.api.app.routers import mount_edition_routers
from apps.api.app.routers.route_ownership import is_enterprise_path


ROOT = Path(__file__).resolve().parents[3]


def test_community_mount_keeps_shared_routes_and_omits_paid_routes() -> None:
    app = FastAPI()
    app.get("/v1/code-tasks")(lambda: {"surface": "community"})
    app.get("/v1/account-connect/link")(lambda: {"surface": "paid"})
    mount_edition_routers(app, "community")

    client = TestClient(app)
    assert client.get("/v1/code-tasks").status_code == 200
    assert client.get("/v1/account-connect/link").status_code == 404


@pytest.mark.parametrize(
    "path",
    [
        "/v1/code-tasks",
        "/v1/provider-definitions",
        "/v1/providers/easy-connect",
        "/v1/changesets",
        "/v1/worker-pools",
        "/v1/deployment-targets",
        "/saml/v2/project/acs",
        "/scim/v2/project/Users",
        "/v1/enterprise/identity-providers",
    ],
)
def test_shared_workflow_paths_remain_community(path: str) -> None:
    assert not is_enterprise_path(path)


@pytest.mark.parametrize(
    "path",
    [
        "/v1/account-connect/link",
        "/v1/marketplace/listings",
        "/v1/enterprise/clusters",
        "/v1/self-improvement/proposals",
    ],
)
def test_paid_only_paths_are_not_mounted(path: str) -> None:
    assert is_enterprise_path(path)


def test_community_api_starts_without_paid_route_module() -> None:
    script = """
import builtins
import sys
original = builtins.__import__
def no_paid_router(name, *args, **kwargs):
    if name == 'apps.api.app.routers.enterprise':
        raise AssertionError('private route module imported')
    return original(name, *args, **kwargs)
builtins.__import__ = no_paid_router
from apps.api.app.main import app
paths = {getattr(route, 'path', '') for route in app.routes}
assert '/v1/code-tasks' in paths
assert '/v1/providers' in paths
assert '/v1/changesets' in paths
assert '/v1/account-connect/link' not in paths
assert 'apps.api.app.routers.enterprise' not in sys.modules
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=ROOT,
        env={**os.environ, "BOSS_EDITION": "community", "PYTHONPATH": f"{ROOT}:{ROOT / 'engine'}"},
        capture_output=True,
        text=True,
        timeout=45,
    )
    assert result.returncode == 0, result.stderr
