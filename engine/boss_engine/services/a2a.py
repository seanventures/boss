"""Compatibility shim: the internal Redis-Streams agent bus is `services/agent_bus.py`.

Renamed in AP-P0 (BOSSAPP-1051, docs/design/AGENT_PLATFORM.md §2.10) because "A2A" names
Google's Agent2Agent protocol, which this bus is not (a real A2A endpoint is Phase 5). The old
import path keeps working and resolves to the SAME module object, so classes, singletons and
isinstance checks are unchanged. New code imports `boss_engine.services.agent_bus`.
"""
import sys as _sys

from . import agent_bus as _real

_sys.modules[__name__] = _real
