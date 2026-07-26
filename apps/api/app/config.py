"""Phase-2 compatibility shim.

`config` now lives in the OSS engine at `boss_engine.config`. This shim aliases it
back under `apps.api.app.config` so the API + worker keep importing it unchanged,
with a single module identity (Pydantic model / class identity preserved). New
code should import from `boss_engine.config` directly.
"""
import sys as _sys

from boss_engine import config as _real

_sys.modules[__name__] = _real
