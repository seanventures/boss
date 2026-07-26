"""Phase-0 import shim.

During Phase 0 the engine still *imports* the service modules from their current
home, `apps.api.app.services`, rather than physically relocating them (that's
Phase 2). To do that from an installed/standalone `boss_engine`, the monorepo
root must be importable so `apps.api.app...` resolves.

`ensure_monorepo_on_path()` walks up from this file to find the repo root (the
directory that contains `apps/api/app`) and puts it on `sys.path`. Once Phase 2
moves the modules under `boss_engine/`, this shim's `SERVICES_PKG` flips to
`boss_engine.services` and nothing else in the engine changes.

Callers should always import service modules through `services_module(name)` /
the re-exports in `boss_engine.services_facade`, never hard-code
`apps.api.app.services`, so the Phase-2 move is a one-line change here.
"""
from __future__ import annotations

import importlib
import os
import sys
from functools import lru_cache
from types import ModuleType

# Phase 0: services live under the API app. Phase 2: change to "boss_engine".
_APP_PKG = "apps.api.app"
SERVICES_PKG = f"{_APP_PKG}.services"


@lru_cache(maxsize=1)
def ensure_monorepo_on_path() -> str:
    """Locate the monorepo root (contains apps/api/app) and add it to sys.path.

    Honors $BOSS_MONOREPO_ROOT for out-of-tree installs. Returns the root path.
    """
    override = os.environ.get("BOSS_MONOREPO_ROOT")
    if override and os.path.isdir(os.path.join(override, "apps", "api", "app")):
        root = os.path.abspath(override)
    else:
        here = os.path.dirname(os.path.abspath(__file__))
        root = None
        cur = here
        for _ in range(8):  # engine/boss_engine/_compat.py -> ... -> repo root
            cur = os.path.dirname(cur)
            if os.path.isdir(os.path.join(cur, "apps", "api", "app")):
                root = cur
                break
        if root is None:
            raise RuntimeError(
                "Could not locate the BOSS monorepo root (a directory containing "
                "apps/api/app). Set BOSS_MONOREPO_ROOT to point at it."
            )
    if root not in sys.path:
        sys.path.insert(0, root)
    return root


def app_module(name: str) -> ModuleType:
    """Import a module from the API app package, e.g. app_module('config')."""
    ensure_monorepo_on_path()
    return importlib.import_module(f"{_APP_PKG}.{name}")


def services_module(name: str) -> ModuleType:
    """Import a service module by bare name, e.g. services_module('code_task_service')."""
    ensure_monorepo_on_path()
    return importlib.import_module(f"{SERVICES_PKG}.{name}")
