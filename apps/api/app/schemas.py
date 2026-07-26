"""Phase-2 compatibility shim.

`schemas` now lives in the OSS engine at `boss_engine.schemas`. This shim aliases it
back under `apps.api.app.schemas` so the API + worker keep importing it unchanged,
with a single module identity (Pydantic model / class identity preserved). New
code should import from `boss_engine.schemas` directly.
"""
import sys as _sys

from boss_engine import schemas as _real

_sys.modules[__name__] = _real
