"""A project to test the API-onboarding gaps in (BOSSAPP-1139 / 1141 / 1142 / 1143), on ANY test database.

The shared `project` fixture of test_workflow_custom_steps skips when the database has no
workspace — which is the bench database's state, so the GitHub and failure-path tests skipped
there and proved nothing. This one seeds an org and a workspace of its own when none exists,
so the tests run wherever a schema does.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from .test_workflow_custom_steps import eng  # noqa: F401 — re-exported fixture

ORG = "org_onboarding_tests"
WORKSPACE = "ws_onboarding_tests"

_CLEANUP = ("workflow_step_runs", "workflow_runs", "workflow_custom_steps", "workflow_mappings",
            "workflow_versions", "workflow_definitions", "project_skills", "skill_versions",
            "skill_definitions", "skill_invocations", "mcp_servers", "decisions", "events",
            "receipts", "role_skills", "roles", "deployment_hosts", "deployment_targets",
            "deadletters")


def _workspace(c) -> str:
    found = c.execute(text("SELECT id FROM workspaces ORDER BY created_at LIMIT 1")).scalar()
    if found:
        return str(found)
    c.execute(text("INSERT INTO orgs (id, name) VALUES (:o, 'onboarding tests') "
                   "ON CONFLICT (id) DO NOTHING"), {"o": ORG})
    c.execute(text("INSERT INTO workspaces (id, org_id, name) VALUES (:w, :o, 'onboarding tests') "
                   "ON CONFLICT (id) DO NOTHING"), {"w": WORKSPACE, "o": ORG})
    return WORKSPACE


@pytest.fixture()
def project(eng):  # noqa: F811
    pid = f"p_{uuid.uuid4().hex[:10]}"
    with eng.begin() as c:
        c.execute(text("INSERT INTO projects (id, workspace_id, name, slug) "
                       "VALUES (:i, :w, 'onboarding gaps test', :s)"),
                  {"i": pid, "w": _workspace(c), "s": pid})
    yield pid
    with eng.begin() as c:
        for table in _CLEANUP:
            try:
                with c.begin_nested():
                    c.execute(text(f"DELETE FROM {table} WHERE project_id=:p"), {"p": pid})
            except Exception:  # noqa: BLE001 — a table this schema lacks
                pass
        c.execute(text("DELETE FROM projects WHERE id=:p"), {"p": pid})


def add_deployment_host(c, project_id: str, address: str) -> None:
    """Make `address` one of this project's own deployment hosts (contract_test may reach it)."""
    c.execute(text("INSERT INTO deployment_hosts (id, project_id, name, address) "
                   "VALUES (:i, :p, :n, :a)"),
              {"i": f"dh_{uuid.uuid4().hex[:10]}", "p": project_id,
               "n": f"staging-{uuid.uuid4().hex[:4]}", "a": address})
