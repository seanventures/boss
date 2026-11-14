"""AP-P0 (BOSSAPP-1051): the internal Redis-Streams bus is `services/agent_bus.py`, so the name
`a2a` is free for Google's Agent2Agent protocol (AGENT_PLATFORM.md Phase 5). The old import
path is a shim to the same module object, and no BOSS code imports through it any more."""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_the_old_path_is_the_same_module():
    from boss_engine.services import a2a, agent_bus

    assert a2a is agent_bus
    assert a2a.A2AService is agent_bus.A2AService
    from apps.api.app.services import agent_bus as via_api

    assert via_api is agent_bus


def test_no_code_imports_the_bus_through_the_shim():
    pattern = re.compile(r"(services\.a2a\b|from \.a2a import|from \. import a2a\b)")
    offenders = []
    for base in (ROOT / "engine", ROOT / "apps"):
        for path in base.rglob("*.py"):
            if "node_modules" in path.parts or path.name in ("a2a.py", Path(__file__).name):
                continue
            if pattern.search(path.read_text(encoding="utf-8", errors="ignore")):
                offenders.append(str(path.relative_to(ROOT)))
    assert offenders == []
