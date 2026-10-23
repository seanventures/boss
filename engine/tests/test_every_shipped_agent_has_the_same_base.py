"""EVERY SHIPPED AGENT HAS THE SAME BASE (operator, 2026-10-23).

"ALL BOSS default (shipped) agents have the SAME expertise and the SAME full action and tool
access through the one pipeline. They differ only by skills and MCP servers an operator assigns
in the console (role_skills / role_mcp_servers), or when a user customizes an agent."

So nothing in the agent pipeline's CODE may name a shipped role to widen or narrow what it can
do: no role list (the six-role `kg_propose_fact` grant was one), no `role == "qa"` branch. A
difference between agents is data a person can see and change, never a code branch.
Governance still applies to every agent the same way: permissions, consent, step-up, budget,
receipts, Passport ceilings."""
from __future__ import annotations

import ast
import inspect
from pathlib import Path

import yaml

from boss_engine.services import agent_baseline as ab

ROOT = Path(__file__).resolve().parents[2]
ROLES = ROOT / "packages" / "specpack" / "roles"
SHIPPED = {p.stem for p in ROLES.glob("*.yaml")}

#: The agent pipeline: where an agent's actions and tools are chosen, offered or granted.
PIPELINE = [
    "engine/boss_engine/services/agent_service.py",
    "engine/boss_engine/services/intent_router.py",
    "engine/boss_engine/services/agent_baseline.py",
    "engine/boss_engine/services/agent_loop.py",
    "engine/boss_engine/services/agent_steps.py",
    "engine/boss_engine/services/skill_runtime.py",
    "engine/boss_engine/services/skill_definitions.py",
    *sorted(str(p.relative_to(ROOT)) for p in (ROOT / "engine/boss_engine/tools").glob("*.py")),
]


def _role_literals(path: str) -> list[str]:
    """Where the code names shipped roles to decide reach: a collection literal of two or more
    shipped role keys, or a comparison against one."""
    tree = ast.parse((ROOT / path).read_text())
    hits: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
            keys = [e.value for e in node.elts
                    if isinstance(e, ast.Constant) and isinstance(e.value, str) and e.value in SHIPPED]
            if len(keys) >= 2:
                hits.append(f"{path}:{node.lineno} {keys}")
        elif isinstance(node, ast.Compare):
            for side in (node.left, *node.comparators):
                if isinstance(side, ast.Constant) and isinstance(side.value, str) \
                        and side.value in SHIPPED:
                    hits.append(f"{path}:{node.lineno} compares with {side.value!r}")
    return hits


def test_the_pipeline_never_branches_on_a_shipped_role():
    assert SHIPPED, "no shipped roles found"
    found = [hit for path in PIPELINE for hit in _role_literals(path)]
    assert not found, found


def test_every_shipped_agent_resolves_the_same_base_skills_and_tools():
    # The base is one constant, not a function of the role.
    assert not [n for n in dir(ab) if n.endswith("_ROLES")]
    assert "role" not in inspect.signature(ab.bundles).parameters
    base_tools = {t for skill in ab.BASELINE_SKILLS for t in ab.bundles().get(skill, ())}
    assert {"kg_propose_fact", "propose_action", "read_logs", "kg_search"} <= base_tools
    # No role spec forbids a baseline skill: the base is the same for every shipped agent (a
    # role may still list one it holds anyway, which changes nothing).
    for path in sorted(ROLES.glob("*.yaml")):
        spec = yaml.safe_load(path.read_text()) or {}
        perms = spec.get("permissions") or (spec.get("role") or {}).get("permissions") or {}
        forbidden = set(perms.get("forbidden_skills") or [])
        assert not forbidden & set(ab.BASELINE_SKILLS), (path.stem, forbidden & set(ab.BASELINE_SKILLS))


def test_the_chat_agent_has_one_tool_set_whoever_is_asked():
    """Ask BOSS addressed to any agent (`role=`) answers with the same registry: the role changes
    the lens and the history, never the reach."""
    from boss_engine.tools import build_discuss_read_registry

    assert "role" not in inspect.signature(build_discuss_read_registry).parameters
    source = (ROOT / "engine/boss_engine/services/intent_router.py").read_text()
    for call in [n for n in ast.walk(ast.parse(source)) if isinstance(n, ast.Call)
                 and getattr(n.func, "id", getattr(n.func, "attr", "")) == "build_discuss_read_registry"]:
        assert all(k.arg in {"edition"} for k in call.keywords), ast.dump(call)
