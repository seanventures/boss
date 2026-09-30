"""Phase-2 compatibility shim for the service layer.

The service modules now live in the OSS engine at ``boss_engine.services``. To
avoid touching the ~90 inline ``from .services.X import Y`` imports in the API (a
huge, hotfix-conflicting diff), this package installs a lazy meta-path finder
that maps every ``apps.api.app.services.X`` onto the *same* module object as
``boss_engine.services.X``.

Key properties:
  * Lazy — only the submodules actually imported are aliased (no eager import of
    all 127, so a broken optional service doesn't block the whole app).
  * Single identity — ``apps.api.app.services.X is boss_engine.services.X`` is
    True, so class/singleton/isinstance semantics are preserved.

New code should import from ``boss_engine.services`` directly; this shim exists
only so the internal API/worker keep working until they're repointed.
"""
from __future__ import annotations

import importlib
import importlib.abc
import importlib.util
import sys

_PREFIX = __name__ + "."  # "apps.api.app.services."
_TARGET = "boss_engine.services."


class _AliasFinder(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    def find_spec(self, name, path=None, target=None):
        if not name.startswith(_PREFIX):
            return None
        # Confirm the real module exists before claiming the spec.
        real_name = _TARGET + name[len(_PREFIX):]
        if importlib.util.find_spec(real_name) is None:
            return None
        return importlib.util.spec_from_loader(name, self)

    def create_module(self, spec):
        real_name = _TARGET + spec.name[len(_PREFIX):]
        real = importlib.import_module(real_name)
        sys.modules[spec.name] = real  # alias: one object under both names
        return real

    def exec_module(self, module):  # already executed as boss_engine.services.X
        return None


# Point this package's search path at the engine's services dir too, and install
# the alias finder ahead of the default finders.
try:  # pragma: no cover - defensive
    __path__ = list(importlib.import_module("boss_engine.services").__path__)
except Exception:
    pass

if not any(isinstance(f, _AliasFinder) for f in sys.meta_path):
    sys.meta_path.insert(0, _AliasFinder())
