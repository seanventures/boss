"""Shared support for the autonomous-agents tests (services/ambient, BOSSAPP-902..912).

Nothing here is collected (no test_ names). Every test gets its OWN project, so rows never leak
between tests, and `purge` removes everything the ambient tables hold for it — the ledger is
append-only, so the purge says so (`SET LOCAL boss.ledger_purge = 'on'`), exactly as a
retention job would.
"""
from __future__ import annotations

import os
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text

PG_URL = os.environ.get("BOSS_TEST_DATABASE_URL", "")

AMBIENT_TABLES = ("work_item_messages", "work_item_links", "ambient_autonomy",
                  "ambient_autonomy_changes", "ambient_settings")


def pg_engine():
    if not PG_URL.startswith("postgres"):
        pytest.skip("needs BOSS_TEST_DATABASE_URL")
    from boss_engine.db import ensure_schema

    eng = create_engine(PG_URL, future=True)
    ensure_schema(eng)
    return eng


def make_project(eng) -> str:
    pid = f"p_amb_{uuid4().hex[:10]}"
    with eng.begin() as c:
        c.execute(text("INSERT INTO orgs (id, name) VALUES ('org_amb', 'ambient') "
                       "ON CONFLICT DO NOTHING"))
        c.execute(text("INSERT INTO workspaces (id, org_id, name) VALUES ('ws_amb', 'org_amb', "
                       "'ambient') ON CONFLICT DO NOTHING"))
        c.execute(text("INSERT INTO projects (id, workspace_id, name, slug) VALUES "
                       "(:p, 'ws_amb', :p, :p)"), {"p": pid})
    return pid


def purge(eng, pid: str) -> None:
    with eng.begin() as c:
        c.execute(text("SET LOCAL boss.ledger_purge = 'on'"))
        c.execute(text("DELETE FROM agent_act_decisions WHERE project_id = :p"), {"p": pid})
        c.execute(text("DELETE FROM agent_acts WHERE project_id = :p"), {"p": pid})
        for table in AMBIENT_TABLES:
            c.execute(text(f"DELETE FROM {table} WHERE project_id = :p"), {"p": pid})
        c.execute(text("DELETE FROM signals WHERE project_id = :p"), {"p": pid})
        try:
            with c.begin_nested():   # the security scan's findings hang off the scan, not a project
                c.execute(text("DELETE FROM security_findings WHERE scan_id IN (SELECT id FROM "
                               "security_scans WHERE project_id = :p)"), {"p": pid})
        except Exception:  # noqa: BLE001
            pass
        for table in ("project_dossiers", "cost_anomalies", "security_scans", "ci_waits",
                      "agent_messages", "agent_threads", "agent_session_ledger", "kg_usage",
                      "kg_edges", "kg_nodes", "meeting_runs", "roles",
                      "work_items", "cost_receipts", "budget_reservations", "budget_refusals",
                      "policy_evaluations", "decisions", "events", "workflow_step_runs",
                      "workflow_runs", "workflow_versions", "workflow_definitions",
                      "code_task_runs", "app_alerts", "shepherd_escalations", "budget_policies"):
            try:
                with c.begin_nested():
                    c.execute(text(f"DELETE FROM {table} WHERE project_id = :p"), {"p": pid})
            except Exception:  # noqa: BLE001 — a table some installs lack
                pass
        c.execute(text("DELETE FROM projects WHERE id = :p"), {"p": pid})


@pytest.fixture(scope="module")
def eng():
    return pg_engine()


@pytest.fixture()
def project(eng):
    pid = make_project(eng)
    yield pid
    purge(eng, pid)


def signal(c, pid: str, **kw: Any) -> dict[str, Any]:
    """Record one signal with sensible defaults; returns the signal row."""
    from boss_engine.services.ambient import signals

    args = {"project_id": pid, "source": "alert", "kind": "alert.firing",
            "title": "5xx above 5% on order-api", "subject_kind": "app", "subject_id": "order-api",
            "app": "order-api", "severity": "high", **kw}
    return signals.record_signal(c, **args)["signal"]


def open_item(c, pid: str, *, fingerprint: str = "fp:order-api-5xx",
              task_class: str = "incident_response", **kw: Any) -> dict[str, Any]:
    from boss_engine.config import get_settings
    from boss_engine.services.ambient import board

    sig = kw.pop("signal_ids", None)
    if sig is None:
        sig = [signal(c, pid, fingerprint_value=fingerprint)["id"]]
    result = board.open_item(c, get_settings(), project_id=pid, fingerprint=fingerprint,
                             title=kw.pop("title", "order-api 5xx at 6.6%"),
                             task_class=task_class, subject_ref=kw.pop("subject_ref",
                                                                      "app:order-api"),
                             signal_ids=sig, actor=kw.pop("actor", "agent:chief_of_staff"), **kw)
    return result
