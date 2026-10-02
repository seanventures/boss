"""Every API route that writes who an agent is, or what it may reach, is held like POST /v1/roles.

THE INCIDENT (review of milestone/agents, round 1). POST /v1/roles has always needed a person's
confirmation when a non-human (an MCP client, a model calling the API directly) calls it. The
agent resource arrived as new paths that nothing classified: POST /v1/agents writes a roles row,
PATCH /v1/agents/{role_key} can rewrite a bundled agent's mission and must_not boundaries, and
POST /v1/role-mcp-servers hands an agent an MCP server and its credentials — and
needs_operator_confirmation answered False for all three. An external MCP client could strip an
agent's safety boundaries with no person confirming.

ENFORCED BY ENUMERATION. This parses every route in the API and finds each one whose handler
calls a roster writer. Each must be in api_surface.ROSTER_AUTHORITY_WRITES, held for a non-human
caller, and confirmation-eligible (so a reviewed call still goes through) — the same answers
POST /v1/roles gets. A new route that writes an agent fails here until it is classified.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

from boss_engine.mcp.api_surface import (
    ROSTER_AUTHORITY_WRITES,
    confirmation_eligible,
    needs_operator_confirmation,
)

API = Path(__file__).resolve().parents[2] / "apps" / "api" / "app"

#: The functions that change an agent's identity, mission, boundaries or MCP reach.
#:
#: A SKILL GRANT IS ONE OF THEM (review of milestone/integration, blocker). This list once left
#: out `assign_skill_to_role` and `sync_role_skills` on the grounds that "a skill's own risk tier
#: decides when a use of it waits for a person" — but an `mcp_tool` skill reaches an MCP server,
#: and POST /v1/role-skills then let a non-human caller do through a skill what POST
#: /v1/role-mcp-servers holds for a person.
ROSTER_WRITERS = re.compile(
    r"\b(?:specpack\.register_role|agents\.(?:create_agent|update_agent|delete_agent|grant_mcp"
    r"|revoke_mcp)|assign_skill_to_role|sync_role_skills)\s*\(")

#: Routes that call a roster writer and are deliberately classified otherwise, with why.
DIFFERENTLY_CLASSIFIED: dict[tuple[str, str], str] = {}


def _routes() -> dict[tuple[str, str], str]:
    found: dict[tuple[str, str], str] = {}
    for path in API.rglob("*.py"):
        if "__pycache__" in str(path):
            continue
        source = path.read_text(encoding="utf-8")
        for node in ast.walk(ast.parse(source)):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for deco in node.decorator_list:
                if (isinstance(deco, ast.Call) and isinstance(deco.func, ast.Attribute)
                        and deco.func.attr in {"post", "put", "patch", "delete"}
                        and deco.args and isinstance(deco.args[0], ast.Constant)):
                    body = ast.get_source_segment(source, node) or ""
                    if ROSTER_WRITERS.search(body):
                        found[(deco.func.attr.upper(), str(deco.args[0].value))] = str(
                            path.relative_to(API))
    return found


def test_the_scan_finds_the_routes_it_exists_for():
    routes = _routes()
    for route in [("POST", "/v1/roles"), ("POST", "/v1/agents"),
                  ("PATCH", "/v1/agents/{role_key}"), ("POST", "/v1/role-mcp-servers"),
                  ("POST", "/v1/role-skills"), ("POST", "/v1/role-skills/sync")]:
        assert route in routes, f"the scan no longer finds {route}; it would pass vacuously"


def test_every_route_that_writes_an_agent_is_classified_like_post_roles():
    unclassified = sorted(
        f"{method} {path} ({where})" for (method, path), where in _routes().items()
        if (method, path) not in ROSTER_AUTHORITY_WRITES
        and (method, path) not in DIFFERENTLY_CLASSIFIED)
    assert not unclassified, (
        f"{unclassified} write an agent's identity or reach but are not in "
        "api_surface.ROSTER_AUTHORITY_WRITES, so a non-human caller could do it with no person "
        "confirming. Add them, or list them in DIFFERENTLY_CLASSIFIED with the reason.")


def test_each_is_held_and_confirmable_exactly_as_post_roles_is():
    reference = (needs_operator_confirmation("POST", "/v1/roles"),
                 confirmation_eligible("POST", "/v1/roles"))
    assert reference == (True, True)
    for method, path in sorted(ROSTER_AUTHORITY_WRITES):
        assert (needs_operator_confirmation(method, path),
                confirmation_eligible(method, path)) == reference, f"{method} {path}"
