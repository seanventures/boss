from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from boss_engine.defaults import (
    AllowAllEntitlementPort,
    InMemoryDecisionSink,
    InMemoryPersistence,
    LoggingEventSink,
    NoopBudgetPort,
)
from boss_engine.ports import (
    BudgetPort,
    EnginePorts,
    EntitlementPort,
    PersistencePort,
    configure_engine_ports,
    get_engine_ports,
)
from boss_engine.services import code_task_service
from boss_engine.services.core import create_blocking_decision, emit_event


ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def restore_ports():
    previous = get_engine_ports()
    try:
        yield
    finally:
        configure_engine_ports(previous)


def _settings(**overrides):
    values = {
        "openai_model": "user-model",
        "target_github_owner": "owner",
        "target_github_repo": "repo",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_oss_ports_satisfy_protocols_and_never_need_a_database():
    ports = EnginePorts.oss_defaults(_settings())

    assert isinstance(ports.budget, BudgetPort)
    assert isinstance(ports.entitlement, EntitlementPort)
    assert isinstance(ports.persistence, PersistencePort)

    reservation = ports.budget.reserve(
        None, "project", "code_task", 500_000, "test", 15
    )
    assert reservation["status"] == "reserved"
    assert reservation["budget_state"] == "unmetered"
    assert ports.budget.assert_active_reservation(
        None, "project", reservation["reservation_id"]
    )
    ports.budget.settle(
        None,
        "project",
        reservation["reservation_id"],
        "receipt",
        "model",
        "user-model",
        500_000,
        1,
    )
    assert (
        ports.budget.assert_active_reservation(
            None, "project", reservation["reservation_id"]
        )
        is None
    )

    route = ports.entitlement.preview_route(
        None,
        "project",
        skill_id="model.generate_text",
        task_kind="coding",
        workload_class="owner_sandbox",
        risk_tier=1,
    )
    assert route["source_kind"] == "user_supplied"
    assert route["budget_state"] == "unmetered"


def test_oss_event_and_decision_sinks_are_connection_free(restore_ports):
    ports = EnginePorts.oss_defaults(_settings())
    configure_engine_ports(ports)

    event_id = emit_event(
        None, "project", "test.event", "tester", "test", "one", {"ok": True}
    )
    decision_id = create_blocking_decision(
        None,
        "project",
        "task",
        "Continue?",
        {"action": "hold"},
        {},
        "test",
    )

    assert event_id.startswith("oss_evt_")
    assert decision_id.startswith("oss_dec_")
    assert ports.events.events[0]["event_type"] == "test.event"
    assert ports.decisions.decisions[0]["status"] == "pending"


class _Jobs:
    def __init__(self):
        self.created: list[dict] = []

    def create_job(self, _connection, **payload):
        self.created.append(payload)


def test_in_memory_persistence_preserves_retry_lineage(restore_ports):
    ports = EnginePorts.oss_defaults(_settings())
    configure_engine_ports(ports)
    ports.persistence.save_run(
        "run-parent",
        {
            "project_id": "project",
            "task_id": "task",
            "status": "failed",
            "phase": "done",
            "title": "Fix a bug",
            "spec": {
                "instructions": "Fix it",
                "acceptance_criteria": ["works"],
                "test_commands": ["pytest"],
                "owner": "owner",
                "repo": "repo",
                "base_branch": "main",
            },
            "state": {"review_summary": "Address the edge case."},
            "usage": {},
            "finalize_ctx": {"attempt": 1},
            "reservation_id": None,
            "pull_request_url": None,
            "pull_request_number": None,
            "decision_id": None,
            "error": "edge case failed",
        },
    )
    jobs = _Jobs()

    result = code_task_service.retry_code_task(
        None, _settings(), jobs, "run-parent", fix_note="Handle empty input."
    )
    child = ports.persistence.get_run(result["run_id"])

    assert result["attempt"] == 2
    assert child is not None
    assert child["finalize_ctx"]["parent_run_id"] == "run-parent"
    assert child["finalize_ctx"]["root_run_id"] == "run-parent"
    assert child["finalize_ctx"]["attempt"] == 2
    assert "Handle empty input." in child["spec"]["instructions"]
    assert jobs.created[0]["payload"]["run_id"] == result["run_id"]


def test_in_memory_persistence_supports_gate_override(restore_ports, monkeypatch):
    ports = EnginePorts.oss_defaults(_settings())
    configure_engine_ports(ports)
    ports.persistence.save_run(
        "run-blocked",
        {
            "project_id": "project",
            "task_id": "task",
            "status": "blocked",
            "phase": "awaiting_gate_override",
            "title": "Fix a bug",
            "spec": {},
            "state": {},
            "usage": {},
            "finalize_ctx": {
                "failing_gates": ["review"],
                "pending_diff": [{"path": "fix.py", "content": "fixed = True\n"}],
                "quality_gate_id": "qg-one",
            },
            "reservation_id": None,
            "pull_request_url": None,
            "pull_request_number": None,
            "decision_id": None,
            "error": None,
        },
    )

    class DummyFactory:
        def __init__(self, _settings, **_kwargs):
            self.budget = NoopBudgetPort()

        def propose_pull_request(self, *_args, **_kwargs):
            return {
                "status": "created",
                "pull_request_url": "https://example.test/pull/1",
                "pull_request_number": 1,
                "decision_id": "decision-one",
            }

    monkeypatch.setattr(code_task_service, "SoftwareFactoryService", DummyFactory)
    result = code_task_service.override_code_task(
        None,
        _settings(),
        _Jobs(),
        "run-blocked",
        waive_gates=["review"],
        reason="verified manually",
    )
    run = ports.persistence.get_run("run-blocked")

    assert result["status"] == "completed"
    assert run is not None
    assert run["status"] == "completed"
    assert run["pull_request_number"] == 1


def test_engine_port_import_does_not_import_sqlalchemy():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import boss_engine.services.core; "
            "raise SystemExit(1 if 'sqlalchemy' in sys.modules else 0)",
        ],
        cwd=ROOT,
        env={"PYTHONPATH": str(ROOT / "engine")},
        check=False,
    )
    assert result.returncode == 0


def test_pipeline_hot_paths_have_no_direct_database_imports_or_text_calls():
    for relative in (
        "engine/boss_engine/services/code_task_service.py",
        "engine/boss_engine/services/governed_code_task_service.py",
        "engine/boss_engine/services/software_factory.py",
    ):
        source = (ROOT / relative).read_text(encoding="utf-8")
        assert "from ..db import" not in source
        assert "from sqlalchemy" not in source
        assert re.search(r"\btext\(", source) is None
        assert "sf.budget" not in source


def test_engine_shipped_files_do_not_contain_internal_hostnames():
    forbidden = ("goldeneye", "sean.network")
    package_root = ROOT / "engine" / "boss_engine"
    for path in package_root.rglob("*"):
        if (
            not path.is_file()
            or "__pycache__" in path.parts
            or any(part.startswith(".") for part in path.relative_to(package_root).parts)
        ):
            continue
        try:
            source = path.read_text(encoding="utf-8").lower()
        except UnicodeDecodeError:
            continue
        assert not any(value in source for value in forbidden), path


def test_default_classes_are_the_documented_oss_implementations():
    ports = EnginePorts.oss_defaults(_settings())
    assert isinstance(ports.budget, NoopBudgetPort)
    assert isinstance(ports.entitlement, AllowAllEntitlementPort)
    assert isinstance(ports.persistence, InMemoryPersistence)
    assert isinstance(ports.events, LoggingEventSink)
    assert isinstance(ports.decisions, InMemoryDecisionSink)
