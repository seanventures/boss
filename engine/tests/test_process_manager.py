"""Project Manager steward — operate, test, validate, propose. Never spend."""

from __future__ import annotations

import json
import os
import uuid

import pytest
from sqlalchemy import create_engine, text

from boss_engine.config import Settings
from boss_engine.services.agent_process import AgentProcessService
from boss_engine.services.console_v2_workforce import org_chart, workforce
from boss_engine.services import org_assignments
from boss_engine.services.process_manager import (
    CANARY_ORIGIN,
    LEGACY_CANARY_TITLE,
    ProcessManagerService,
    validate_action,
)
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


def _start_test_canary(service, connection, project_id):
    """Explicit QA fixture; the ordinary steward must never create it."""
    move = {
        "kind": "start_simulated_canary",
        "simulated": True,
        "auto_build": False,
        "title": LEGACY_CANARY_TITLE,
    }
    assert service.validate_action(move["kind"], move)["ok"]
    return service._apply(connection, project_id=project_id, actor="system", move=move)


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


@pytest.mark.parametrize("template", [None, {"id": "existing-template"}])
@pytest.mark.parametrize("active_ids", [[], ["existing-run"]])
def test_plan_never_starts_a_simulation(template, active_ids):
    planned = _svc()._plan(
        {"template": template, "active_ids": active_ids, "recent_canary": None},
        {},
    )
    assert [move["kind"] for move in planned] == (
        ([] if template else ["ensure_default_template"])
        + (["advance_runs"] if active_ids else [])
    )
    if active_ids:
        assert planned[-1]["run_ids"] == active_ids


def test_tick_empty_project_creates_template_without_simulated_work(db, project_id):
    svc = _svc()
    processes = AgentProcessService(Settings())
    assert processes.get_template_by_key(db, project_id, "prd_to_accepted_work") is None

    result = svc.tick(db, project_id=project_id, actor="pm")

    template = processes.get_template_by_key(db, project_id, "prd_to_accepted_work")
    assert template is not None
    assert processes.list_runs(db, project_id) == []
    assert db.execute(text("SELECT count(*) FROM agent_process_steps WHERE project_id=:p"),
                      {"p": project_id}).scalar_one() == 0
    assert db.execute(text("SELECT count(*) FROM events WHERE project_id=:p "
                           "AND event_type IN ('agent_process.started', "
                           "'agent_process.handoff', 'process_manager.canary_started')"),
                      {"p": project_id}).scalar_one() == 0
    assert all(not row["effect_kind"].startswith("agent_process.")
               for row in ReceiptService().list_by_project(db, project_id, 50))

    metrics = result["metrics"]
    assert metrics["run_count"] == 0
    assert metrics["completed"] == 0
    assert metrics["failed"] == 0
    assert metrics["accept_rate"] == 0.0
    assert "panel_certified_rate" in metrics
    assert "revise_findings_count" in metrics
    assert metrics.get("avg_score") is None
    assert "score" not in metrics

    steward = svc.ensure_for_project(db, project_id, "pm")
    assert steward["last_tick_at"]
    assert steward["last_metrics"]
    assert steward["last_evidence"]
    assert result["receipt_id"]
    assert {move["kind"] for move in result["applied"]} == {"ensure_default_template"}
    second = svc.tick(db, project_id=project_id, actor="system")
    assert second["applied"] == []
    assert second["metrics"]["run_count"] == 0
    assert processes.list_runs(db, project_id) == []


def test_explicit_qa_canary_stays_labeled_and_audited(db, project_id):
    # An explicit QA fixture remains distinct from registered customer roles.
    for key in ("cto", "qa"):
        db.execute(text("INSERT INTO roles (id, project_id, role_key, display_name) "
                        "VALUES (:id, :p, :key, :name)"),
                   {"id": f"role_{project_id}_{key}", "p": project_id,
                    "key": key, "name": key.upper()})
    svc = _svc()
    _start_test_canary(svc, db, project_id)
    result = svc.tick(db, project_id=project_id, actor="system")
    processes = AgentProcessService(Settings())
    run = processes.list_runs(db, project_id)[0]
    steps = processes.list_steps(db, run["id"])

    assert run["status"] == "completed"
    assert run["title"] == LEGACY_CANARY_TITLE
    assert run["input"]["simulated"] is True
    assert run["input"]["auto_build"] is False
    assert run["input"]["origin"] == CANARY_ORIGIN
    assert len(steps) == 11
    build = next(step for step in steps if step["action"] == "build")
    assert build["output"]["verdict"] == "planned"
    certification = next(step for step in steps if step["action"] == "certify_spec")
    assert certification["output"]["verdict"] == "certified_planned"
    assert run["receipt_id"] and result["receipt_id"]
    assert _pm_receipts(db, project_id)
    assert db.execute(text("SELECT count(*) FROM roles WHERE project_id = :p"),
                      {"p": project_id}).scalar_one() == 2

    roster = workforce(db, project_id)
    chart = org_chart(db, project_id)
    assert roster["agents"] == []
    assert roster["handoffs"] == []
    assert roster["unregistered"] == []
    assert chart["nodes"] == []
    assert chart["edges"] == []

    # A conscious team placement still appears, even though its role has not worked.
    org_assignments.set_one(db, project_id, "qa", actor="ceo")
    planned = org_assignments.apply_to(org_chart(db, project_id),
                                       org_assignments.get_all(db, project_id))
    assert [node["id"] for node in planned["tree"]] == ["qa"]
    assert [node["id"] for node in planned["nodes"]] == ["qa"]

    # If a system check stalls midway, it still must not look like customer work.
    db.execute(text("UPDATE agent_process_runs SET status = 'running' WHERE id = :id"),
               {"id": run["id"]})
    db.execute(text("UPDATE agent_process_steps SET status = 'running' WHERE id = :id"),
               {"id": steps[0]["id"]})
    assert workforce(db, project_id)["agents"] == []


def test_customer_simulation_remains_visible_without_starting_system_canary(db, project_id):
    processes = AgentProcessService(Settings())
    preview = processes.start_run(
        db, project_id=project_id, actor="ceo",
        prd="# Customer preview\nCheck an internal draft.",
        title="Customer preview", simulated=True, auto_build=False,
    )
    assert preview["status"] == "completed"
    before = workforce(db, project_id)
    assert "product_manager" in before["unregistered"]
    assert before["handoffs"]
    assert sum(agent.get("steps") or 0 for agent in before["agents"]) == len(preview["steps"])

    _svc().tick(db, project_id=project_id, actor="system")
    runs = processes.list_runs(db, project_id)
    assert len(runs) == 1
    assert runs[0]["id"] == preview["id"]
    assert all(run["input"].get("origin") != CANARY_ORIGIN for run in runs)

    after = workforce(db, project_id)
    assert after["unregistered"] == before["unregistered"]
    assert after["handoffs"] == before["handoffs"]
    assert [(agent["id"], agent["steps"]) for agent in after["agents"]] == [
        (agent["id"], agent["steps"]) for agent in before["agents"]
    ]


def test_legacy_system_canary_is_excluded_without_hiding_simulated_user_run(db, project_id):
    processes = AgentProcessService(Settings())
    legacy = processes.start_run(
        db, project_id=project_id, actor="system",
        prd="# Old canary\nCheck the process.",
        title=LEGACY_CANARY_TITLE, simulated=True, auto_build=False,
    )
    assert legacy["status"] == "completed"
    assert workforce(db, project_id)["agents"] == []

    preview = processes.start_run(
        db, project_id=project_id, actor="ceo",
        prd="# Customer preview\nCheck the process.",
        title=LEGACY_CANARY_TITLE, simulated=True, auto_build=False,
    )
    assert preview["status"] == "completed"
    assert workforce(db, project_id)["unregistered"]
    _svc().tick(db, project_id=project_id, actor="system")
    # Existing historical records remain intact; the steward creates no new simulation.
    assert len(processes.list_runs(db, project_id)) == 2


def test_tick_advances_an_existing_durable_run_without_creating_another(monkeypatch, db, project_id):
    svc = _svc()
    # Pause only the fixture's initial synchronous advance, leaving a durable queued run.
    with monkeypatch.context() as patch:
        patch.setattr(svc.processes, "advance", lambda connection, *, project_id, actor, run_id:
                      svc.processes.get_run(connection, project_id, run_id))
        queued = _start_test_canary(svc, db, project_id)
    assert queued["run_status"] == "queued"
    assert svc.processes.list_steps(db, queued["run_id"]) == []

    result = svc.tick(db, project_id=project_id, actor="system")

    applied = {move["kind"]: move for move in result["applied"]}
    assert "start_simulated_canary" not in applied
    assert applied["advance_runs"]["runs"] == [queued["run_id"]]
    runs = svc.processes.list_runs(db, project_id)
    assert len(runs) == 1
    assert runs[0]["id"] == queued["run_id"]
    assert runs[0]["status"] == "completed"
    assert len(svc.processes.list_steps(db, queued["run_id"])) == 11


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

    # Seed the review gap explicitly instead of relying on an automatic fake delivery.
    _start_test_canary(svc, db, project_id)
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
    _start_test_canary(svc, db, project_id)
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
