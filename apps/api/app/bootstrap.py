"""Phase-2 compatibility shim.

`bootstrap` now lives in the OSS engine at `boss_engine.bootstrap`. This shim aliases it
back under `apps.api.app.bootstrap` so the API + worker keep importing it unchanged,
with a single module identity (Pydantic model / class identity preserved). New
code should import from `boss_engine.bootstrap` directly.
"""
import sys as _sys

from boss_engine import bootstrap as _real

_sys.modules[__name__] = _real
