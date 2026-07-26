"""Engine module resolution.

After Phase 2 the engine is self-contained: its config/db/schemas/bootstrap and
all service modules live under ``boss_engine`` itself, so nothing here reaches
into the monorepo. (Pre-Phase-2 this shim redirected to
``apps.api.app.services``; that indirection is gone.)

Everything imports service/config modules through ``services_module()`` /
``app_module()`` so there remains a single, obvious switch point.
"""
from __future__ import annotations

import importlib
from types import ModuleType

_APP_PKG = "boss_engine"
SERVICES_PKG = "boss_engine.services"


def ensure_monorepo_on_path() -> None:
    """No-op post-Phase-2: the engine no longer depends on the monorepo layout.

    Kept as a stable hook so callers that used to invoke it still work.
    """
    return None


def app_module(name: str) -> ModuleType:
    """Import a top-level engine module, e.g. app_module('config')."""
    return importlib.import_module(f"{_APP_PKG}.{name}")


def services_module(name: str) -> ModuleType:
    """Import a service module by bare name, e.g. services_module('code_task_service')."""
    return importlib.import_module(f"{SERVICES_PKG}.{name}")
