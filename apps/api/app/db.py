"""Phase-2 compatibility shim.

`db` now lives in the OSS engine at `boss_engine.db`. This shim aliases it
back under `apps.api.app.db` so the API + worker keep importing it unchanged,
with a single module identity (Pydantic model / class identity preserved). New
code should import from `boss_engine.db` directly.
"""
import sys as _sys

from boss_engine import db as _real

_sys.modules[__name__] = _real
