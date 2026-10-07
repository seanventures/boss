"""Every mounted route can describe its own parameters.

The Activity log's shared filters were a class dependency in a module with postponed
annotations. FastAPI reads a class dependency's annotations without the module's globals, so
`Optional[str]` stayed an unresolved forward reference: every GET /v1/activity* answered 500 and
/openapi.json could not be built. Checking routes one at a time names the route that breaks,
instead of one whole-app failure that other tests report as something else.
"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[2]

CODE = """
from fastapi.openapi.utils import get_openapi
from fastapi.routing import APIRoute
from app.main import app

broken = []
for route in app.routes:
    if not isinstance(route, APIRoute):
        continue
    try:
        get_openapi(title="probe", version="0", routes=[route])
    except Exception as error:  # noqa: BLE001 - every failure is a finding
        broken.append(f"{sorted(route.methods)} {route.path}: {type(error).__name__}")
assert not broken, "\\n".join(broken)
"""


def test_every_route_builds_its_openapi_schema() -> None:
    environment = os.environ.copy()
    environment["BOSS_EDITION"] = "community"
    environment["BOSS_REQUIRE_API_AUTH"] = "false"
    environment["PYTHONPATH"] = os.pathsep.join(
        (str(ROOT), str(ROOT / "apps/api"), str(ROOT / "engine"))
    )
    result = subprocess.run(
        [sys.executable, "-W", "ignore", "-c", CODE], cwd=ROOT, env=environment,
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr[-4000:]
