"""Community Cloud API composition and OpenAPI contract."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


def test_community_cloud_routes_boot_without_private_modules() -> None:
    """A fresh Community process imports no private Cloud implementation."""

    script = """
import builtins
import json
import sys

blocked = (
    'boss_engine.services.commercial',
    'boss_engine.services.cloud_product',
    'apps.api.app.services.commercial',
    'apps.api.app.enterprise_extensions.cloud_routes',
)
original_import = builtins.__import__
def community_import(name, *args, **kwargs):
    if name in blocked or any(name.startswith(prefix + '.') for prefix in blocked):
        raise AssertionError(f'Community imported private module: {name}')
    return original_import(name, *args, **kwargs)
builtins.__import__ = community_import

from apps.api.app.main import app, cloud_product, commercial

paths = app.openapi()['paths']
methods = {
    path: sorted(method for method in operations if method in {'get', 'post', 'put'})
    for path, operations in paths.items()
    if path.startswith('/v1/cloud/')
}
print(json.dumps({
    'services': [type(commercial).__name__, type(cloud_product).__name__],
    'methods': methods,
    'private_loaded': sorted(name for name in sys.modules if name in blocked),
}))
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=ROOT,
        env={
            **os.environ,
            "BOSS_EDITION": "community",
            "PYTHONPATH": f"{ROOT}:{ROOT / 'engine'}",
        },
        capture_output=True,
        text=True,
        timeout=45,
    )
    assert result.returncode == 0, result.stderr
    output = json.loads(result.stdout)
    assert output["services"] == ["CommunityEditionService", "CommunityCloudService"]
    assert not output["private_loaded"]
    methods = output["methods"]
    assert methods["/v1/cloud/catalog"] == ["get"]
    assert methods["/v1/cloud/status"] == ["get"]
    assert methods["/v1/cloud/workers"] == ["get"]
    assert methods["/v1/cloud/retention"] == ["get"]
    assert methods["/v1/cloud/easy-connect"] == ["post"]
    assert methods["/v1/cloud/easy-connect/{session_id}"] == ["get"]
    assert methods["/v1/cloud/easy-connect/{session_id}/complete"] == ["post"]
    assert methods["/v1/cloud/easy-connect/complete"] == ["post"]
    assert methods["/v1/cloud/unlink"] == ["post"]
    for hosted_path in (
        "/v1/cloud/workers/enroll",
        "/v1/cloud/sync",
        "/v1/cloud/entitlements",
        "/v1/cloud/team-invites",
        "/v1/cloud/marketplace",
    ):
        assert hosted_path not in methods
