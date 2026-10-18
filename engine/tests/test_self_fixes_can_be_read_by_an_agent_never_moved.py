"""MCP TOOLS FOR SELF-FIXES ARE READ-ONLY (BOSSAPP-571 spec §11.4): an agent or a client can list
self-fixes, read one with its lineage and card, and read the autonomy metric — under
`improvement:read`. No tool starts, approves, installs, removes or sends one: those are a
person's acts (approval is the decisions API), never an agent's."""
from __future__ import annotations

from boss_engine.tools import build_default_registry

READS = ("list_self_fixes", "get_self_fix", "autonomy_report")


def _registry():
    return build_default_registry()


def test_the_three_reads_exist_and_nothing_writes():
    reg = _registry()
    names = set(reg.names())
    for name in READS:
        assert name in names, name
        spec = reg.get(name)
        assert getattr(spec, "permission", None) == "improvement:read"
    movers = [n for n in names if "self_fix" in n and n not in READS]
    assert movers == [], f"no tool moves a self-fix: {movers}"


def test_they_are_reads():
    reg = _registry()
    reads = {s.name for s in reg.specs(kind="read")}
    assert set(READS) <= reads
