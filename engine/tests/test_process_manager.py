"""Project Manager steward — operate, test, validate, propose. Never spend."""

from __future__ import annotations

import json
import os
import uuid

import pytest
from sqlalchemy import create_engine, text

from boss_engine.config import Settings
from boss_engine.services.agent_process import AgentProcessService
from boss_engine.services.process_manager import ProcessManagerService, validate_action
from boss_engine.services.receipts import ReceiptService


def _engine():
    url = os.environ.get(
        "DATABASE_URL",
        "postgresql+psycopg://boss:boss@localhost:5432/boss",
    )
    return create_engine(url, future=True)


@pytest.fixture
def db():
    engine = _engine()
    try:
        with engine.connect() as probe:
            probe.execute(text("SELECT 1"))
    except Exception:
        pytest.skip("Postgres is not available for process manager tests.")
    with engine.connect() as connection:
        trans = connection.begin()
        try:
            yield connection
        finally:
            trans.rollback()


@pytest.fixture
def project_id(db):
    suffix = uuid.uuid4().hex[:12]
    org_id = f"org_pm_{suffix}"
    workspace_id = f"ws_pm_{suffix}"
    proj_id = f"proj_pm_{suffix}"
    db.execute(text("INSERT INTO orgs (id, name) VALUES (:id, :n)"), {"id": org_id, "n": "PM test"})
    db.execute(
        text("INSERT INTO workspaces (id, org_id, name) VALUES (:id, :o, :n)"),
        {"id": workspace_id, "o": org_id, "n": "PM test"},
    )
    db.execute(
        text("INSERT INTO projects (id, workspace_id, name, slug) VALUES (:id, :w, :n, :s)"),
        {"id": proj_id, "w": workspace_id, "n": "PM test", "s": f"pm-{suffix}"},
    )
    return proj_id


def _svc() -> ProcessManagerService:
    return ProcessManagerService(Settings())


def _pm_receipts(connection, project_id: str) -> list[dict]:
    rows = ReceiptService().list_by_project(connection, project_id, 50)
    return [
        row for row in rows
        if str(row.get("dedupe_key") or "").startswith(f"process_manager:{project_id}:")
    ]


def test_validate_action_fail_closed_unknown_auto_build_and_human_only():
    unknown = validate_action("explode")
    assert unknown["ok"] is False
    assert "known_kind" in unknown["failed"]

    auto = validate_action("auto_build")
    assert auto["ok"] is False

    canary_spend = validate_action(
        "start_simulated_canary",
        {"simulated": True, "auto_build": True},
    )
    assert canary_spend["ok"] is False

    decision = validate_action("escalate_decision")
    assert decision["ok"] is False
    dlq = validate_action("resolve_deadletter")
    assert dlq["ok"] is False
    rewrite = validate_action("rewrite_template")
    assert rewrite["ok"] is False

    ok = validate_action("ensure_default_template")
    assert ok["ok"] is True
    ok_canary = validate_action(
        "start_simulated_canary",
        {"simulated": True, "auto_build": False},
    )
    assert ok_canary["ok"] is True


def test_tick_empty_project_creates_template_canary_and_metrics(db, project_id):
    svc = _svc()
    processes = AgentProcessService(Settings())
    assert processes.get_template_by_key(db, project_id, "prd_to_accepted_work") is None

    result = svc.tick(db, project_id=project_id, actor="pm")

    template = processes.get_template_by_key(db, project_id, "prd_to_accepted_work")
    assert template is not None
    runs = processes.list_runs(db, project_id)
    assert len(runs) == 1
    inp = runs[0].get("input") or {}
    if isinstance(inp, str):
        inp = json.loads(inp)
    assert inp.get("simulated") is True
    assert inp.get("auto_build") is False
    assert runs[0]["status"] == "completed"

    metrics = result["metrics"]
    assert metrics["run_count"] == 1
    assert metrics["completed"] == 1
    assert metrics["failed"] == 0
    assert metrics["accept_rate"] == 1.0
    assert "panel_certified_rate" in metrics
    assert "revise_findings_count" in metrics
    assert metrics.get("avg_score") is None
    assert "score" not in metrics

    steward = svc.ensure_for_project(db, project_id, "pm")
    assert steward["last_tick_at"]
    assert steward["last_metrics"]
    assert steward["last_evidence"]
    assert result["receipt_id"]


def test_second_tick_same_minute_does_not_duplicate_receipt(db, project_id):
    svc = _svc()
    first = svc.tick(db, project_id=project_id, actor="pm")
    first_receipts = _pm_receipts(db, project_id)
    assert first_receipts

    second = svc.tick(db, project_id=project_id, actor="pm")
    second_receipts = _pm_receipts(db, project_id)

    assert first["receipt_id"] == second["receipt_id"]
    assert len(second_receipts) == len(first_receipts)
    tick_keys = [row["dedupe_key"] for row in second_receipts if str(row["dedupe_key"]).endswith(":tick")]
    assert len(tick_keys) == 1


def test_gap_creates_proposal_not_silent_template_mutate(db, project_id):
    svc = _svc()
    processes = AgentProcessService(Settings())
    processes.ensure_default_template(db, project_id=project_id, actor="pm")
    before = processes.get_template_by_key(db, project_id, "prd_to_accepted_work")
    spec_before = json.dumps(before["spec"], sort_keys=True)

    result = svc.tick(db, project_id=project_id, actor="pm")

    after = processes.get_template_by_key(db, project_id, "prd_to_accepted_work")
    assert json.dumps(after["spec"], sort_keys=True) == spec_before
    proposals = svc.list_proposals(db, project_id)
    assert proposals, "a real review gap must store a proposal, not rewrite the template"
    assert proposals[0]["status"] == "proposed"
    assert proposals[0]["spec_diff"]
    assert proposals[0]["reason"]
    applied_kinds = {item.get("kind") for item in (result.get("applied") or [])}
    assert "rewrite_template" not in applied_kinds
    assert "mutate_template" not in applied_kinds


def test_auto_build_never_called(monkeypatch, db, project_id):
    factory_calls = []

    def boom(*_args, **_kwargs):
        factory_calls.append(True)
        raise AssertionError("PM tick must never auto_build or spend")

    monkeypatch.setattr(
        "boss_engine.services.code_task_service.start_code_task",
        boom,
    )
    svc = _svc()
    result = svc.tick(db, project_id=project_id, actor="pm")
    assert factory_calls == []
    assert all(item.get("kind") != "auto_build" for item in (result.get("applied") or []))

    runs = AgentProcessService(Settings()).list_runs(db, project_id)
    assert runs
    for run in runs:
        inp = run.get("input") or {}
        if isinstance(inp, str):
            inp = json.loads(inp)
        assert inp.get("auto_build") is False
        assert inp.get("simulated") is True
