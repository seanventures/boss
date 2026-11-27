"""BOSSAPP-1460: Community and self-hosted BOSS run one behavioral code path.

Two guards:
1. Static/AST guard: `boss_edition` / `BOSS_EDITION` / edition helper checks must NOT gate
   capabilities, routes, tools, agents, spend, or workflows anywhere in `engine/boss_engine`,
   `apps/api/app`, or `apps/web/{app,lib}` — only read-only edition display/telemetry and
   pass-through `edition=` arguments remain.
2. Runtime parity guard: `"community"`, `"oss"`, `"local"`, and `"enterprise_self_hosted"`
   produce identical tool exposure, profile defaults, fleet checks, research job routing,
   API route mounting, and entitlement skill bundles.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

from fastapi import FastAPI
import pytest

from apps.api.app.routers import mount_edition_routers
from boss_engine.config import Settings
from boss_engine.services import (
    community_edition,
    deployment_fleet,
    discovery_service,
    entitlements,
    pipeline_profile_service,
)
from boss_engine.tools.builtin import build_default_registry, build_discuss_read_registry

ROOT = Path(__file__).resolve().parents[2]

PY_SCAN_DIRS = (
    ROOT / "engine" / "boss_engine",
    ROOT / "apps" / "api" / "app",
)

# Files that legitimately hold the Settings field, GET /v1/edition display, telemetry labels, or
# pass `edition=` through to registry builders that return identical tools across editions.
PY_EDITION_ALLOWLIST = {
    "engine/boss_engine/config.py",
    "engine/boss_engine/services/community_edition.py",
    "engine/boss_engine/services/improve_boss.py",
    "engine/boss_engine/services/self_telemetry.py",
    "engine/boss_engine/services/intent_router.py",
    "engine/boss_engine/services/agent_baseline.py",
    "engine/boss_engine/services/ambient/squad_turns.py",
    "engine/boss_engine/services/skill_executors.py",
    "engine/boss_engine/services/workflow_precheck_agent.py",
    "engine/boss_engine/agent/engine_agent.py",
    "engine/boss_engine/__main__.py",
    "apps/api/app/main.py",
}

# Only display/telemetry helpers may inspect the edition string in Python.
PY_CONDITION_ALLOWLIST = {
    "engine/boss_engine/services/community_edition.py",
    "engine/boss_engine/services/improve_boss.py",
    "engine/boss_engine/services/self_telemetry.py",
}

WEB_SCAN_DIRS = (
    ROOT / "apps" / "web" / "app",
    ROOT / "apps" / "web" / "lib",
)

WEB_EDITION_ALLOWLIST = {
    "apps/web/lib/edition.ts",
    "apps/web/app/edition-ui.tsx",
    "apps/web/app/ConsoleShell.tsx",
    "apps/web/app/page.tsx",
}

REFUSAL_PHRASES = (
    "unavailable in community",
    "paused in community",
    "disabled in community",
    "not available in community",
    "not mounted in community",
    "community edition does not",
)

EDITION_TOKEN_RE = re.compile(
    r"\b(?:boss_edition|BOSS_EDITION|is_community_edition|is_paid_edition|"
    r"isCommunityEdition|isPaidEdition|shouldShowPaidNavBadge)\b"
)


def _attr_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _expr_mentions_boss_edition(node: ast.AST) -> bool:
    for child in ast.walk(node):
        if isinstance(child, ast.Name) and child.id in {"boss_edition", "BOSS_EDITION"}:
            return True
        if isinstance(child, ast.Attribute) and child.attr == "boss_edition":
            return True
    return False


def test_no_edition_gates_remain_in_python_source():
    token_violations: list[str] = []
    branch_violations: list[str] = []
    refusal_violations: list[str] = []

    for base in PY_SCAN_DIRS:
        for path in sorted(base.rglob("*.py")):
            rel = path.relative_to(ROOT).as_posix()
            text = path.read_text(encoding="utf-8", errors="ignore")
            lower = text.lower()
            for phrase in REFUSAL_PHRASES:
                if phrase in lower:
                    refusal_violations.append(f"{rel}: contains {phrase!r}")

            if rel not in PY_EDITION_ALLOWLIST and EDITION_TOKEN_RE.search(text):
                token_violations.append(rel)

            if rel not in PY_CONDITION_ALLOWLIST:
                tree = ast.parse(text, filename=rel)
                for node in ast.walk(tree):
                    if isinstance(node, (ast.If, ast.IfExp)) and _expr_mentions_boss_edition(node.test):
                        branch_violations.append(f"{rel}:{node.lineno}")
                    elif isinstance(node, ast.Compare) and _expr_mentions_boss_edition(node):
                        branch_violations.append(f"{rel}:{node.lineno}")

    assert not refusal_violations, f"Community refusal wording remains: {refusal_violations}"
    assert not token_violations, f"Unexpected edition references outside allowlist: {token_violations}"
    assert not branch_violations, f"Conditional branch on boss_edition found: {branch_violations}"


def test_no_edition_gates_remain_in_web_source():
    violations: list[str] = []
    for base in WEB_SCAN_DIRS:
        for path in sorted(base.rglob("*.ts*")):
            rel = path.relative_to(ROOT).as_posix()
            text = path.read_text(encoding="utf-8", errors="ignore")
            if rel not in WEB_EDITION_ALLOWLIST and EDITION_TOKEN_RE.search(text):
                violations.append(rel)
    assert not violations, f"Unexpected web edition references outside allowlist: {violations}"

    edition_ts = (ROOT / "apps" / "web" / "lib" / "edition.ts").read_text(encoding="utf-8")
    assert "export function isCommunityEdition(_editionId?: string | null): boolean {\n  return false;" in edition_ts
    assert "export function isPaidEdition(_editionId?: string | null): boolean {\n  return true;" in edition_ts
    assert "export function shouldShowPaidNavBadge(_areaId: string, _editionId?: string | null): boolean {\n  return false;" in edition_ts


@pytest.mark.parametrize(
    "edition", ["community", "oss", "local", "enterprise_self_hosted"],
)
def test_runtime_behavior_is_identical_across_editions(edition):
    baseline = build_discuss_read_registry(edition="enterprise_self_hosted")
    current = build_discuss_read_registry(edition=edition)
    assert current.names() == baseline.names()
    assert "http_probe" in current.names()
    assert "contract_test" in current.names()
    assert "generate_image" in current.names()

    default_reg = build_default_registry()
    assert "query_db" in default_reg.names()
    assert "chat" in default_reg.get("http_probe").exposure.surfaces

    settings = Settings(boss_edition=edition).model_copy(update={"ssh_host_deploy_allowed": True})
    enterprise_settings = Settings(boss_edition="enterprise_self_hosted").model_copy(
        update={"ssh_host_deploy_allowed": True},
    )

    assert deployment_fleet.direct_host_writes_allowed(settings) is True
    assert discovery_service._research_runs_as_a_job(settings) is True
    assert pipeline_profile_service.usable_profile("standard", settings=settings) == (
        pipeline_profile_service.usable_profile("standard", settings=enterprise_settings)
    )

    assert entitlements.EntitlementBrokerService(settings)._edition_skill_bundles(None, "p1") == (
        entitlements.EntitlementBrokerService(enterprise_settings)._edition_skill_bundles(None, "p1")
    )

    svc = community_edition.CommunityEditionService(settings)
    assert svc._account_connect_required(edition) is False
    assert isinstance(svc._commercial_capabilities(edition, {}), list)

    from fastapi.testclient import TestClient

    app = FastAPI()
    app.get("/v1/code-tasks")(lambda: {"ok": True})
    app.get("/v1/account-connect/link")(lambda: {"ok": True})
    mount_edition_routers(app, edition)
    client = TestClient(app)
    assert client.get("/v1/code-tasks").status_code == 200
    assert client.get("/v1/account-connect/link").status_code == 200
