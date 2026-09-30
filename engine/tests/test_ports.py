from __future__ import annotations

import dataclasses
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


#: A named integration may say its own name, the way an AWS adapter says "AWS". Anything else
#: mentioning the operator's platform is a leak of where BOSS happens to run.
_MAY_NAME_THE_PLATFORM = {
    "services/goldeneye_paas.py",      # the adapter itself; the name IS the integration
    "services/api_catalog.py",         # catalogues that integration's API surface
    "services/agent_kms.py",           # speaks that integration's ops-agent secret protocol
    "services/console_v2.py",          # matches BOSS's own run titles, which contain the word
    "mcp/curated_request_bodies.json", # names public Goldeneye adapter routes in API schemas
}


def _engine_sources():
    package_root = ROOT / "engine" / "boss_engine"
    for path in package_root.rglob("*"):
        rel = path.relative_to(package_root)
        if (
            not path.is_file()
            or "__pycache__" in path.parts
            or any(part.startswith(".") for part in rel.parts)
        ):
            continue
        try:
            yield rel.as_posix(), path.read_text(encoding="utf-8").lower()
        except UnicodeDecodeError:
            continue


# scripts/private/public_export_host_denylist.txt is private and must stay out of the Community export.
_PRIVATE_HOST_DENYLIST = ROOT / "scripts" / "private" / "public_export_host_denylist.txt"


def _private_host_denylist() -> tuple[str, ...]:
    """Names to forbid in engine source. Absent file: skip them and still pass."""
    if not _PRIVATE_HOST_DENYLIST.is_file():
        return ()
    hosts: list[str] = []
    for line in _PRIVATE_HOST_DENYLIST.read_text(encoding="utf-8").splitlines():
        host = line.split("#", 1)[0].strip().lower()
        if host:
            hosts.append(host)
    return tuple(hosts)


def test_engine_shipped_files_do_not_contain_internal_hostnames():
    """A private hostname must never ship. No exceptions, ever.

    The names live in a private denylist, not in this file. Private CI still fails if
    engine source grows one of them. A Community tree has no denylist, skips those
    names, and passes.
    """
    for rel, source in _engine_sources():
        for host in _private_host_denylist():
            assert host not in source, f"{rel} ships the hostname {host!r}"


def test_only_the_platform_integration_names_the_platform():
    """The other half, which was NOT unambiguous and had this test permanently red.

    The old rule forbade the string "goldeneye" anywhere in the engine — including in
    goldeneye_paas.py, a module whose entire purpose is integrating with it and which says the
    word 68 times. That is not a leak; it is the module's identity, exactly as an AWS adapter
    says "AWS".

    So the rule is split. Hostnames are forbidden outright, above. The platform's NAME is
    allowed only where naming it is the point, and the allowlist is small and explicit so a new
    mention has to be argued for rather than absorbed.
    """
    offenders = sorted(
        rel for rel, source in _engine_sources()
        if "goldeneye" in source and rel not in _MAY_NAME_THE_PLATFORM
    )
    assert not offenders, (
        "these name the operator's platform without being an integration with it: "
        f"{offenders}"
    )


def test_the_platform_allowlist_has_no_dead_entries():
    """An allowlist that outlives its reason silently widens the rule."""
    present = {rel for rel, source in _engine_sources() if "goldeneye" in source}
    stale = sorted(_MAY_NAME_THE_PLATFORM - present)
    assert not stale, f"allowlisted but no longer mention it: {stale}"


def test_default_classes_are_the_documented_oss_implementations():
    ports = EnginePorts.oss_defaults(_settings())
    assert isinstance(ports.budget, NoopBudgetPort)
    assert isinstance(ports.entitlement, AllowAllEntitlementPort)
    assert isinstance(ports.persistence, InMemoryPersistence)
    assert isinstance(ports.events, LoggingEventSink)
    assert isinstance(ports.decisions, InMemoryDecisionSink)


def test_duplicate_detection_survives_a_store_that_cannot_run_sql(restore_ports):
    """The duplicate CHECK must not take down the work it protects.

    The OSS in-memory adapter defines execute() and raises from it, so a `hasattr` guard let
    the call through and every retry died with "raw SQL is unavailable". Both guards now fall
    back to the persistence port, which keeps the protection working there rather than
    switching it off — without it the OSS path would start the same pipeline twice, and each
    one is a full budget.
    """
    from boss_engine.services.code_task_service import active_duplicate, active_same_app

    ports = EnginePorts.oss_defaults(_settings())
    configure_engine_ports(ports)
    ports.persistence.save_run("run-live", {
        "id": "run-live",
        "project_id": "project",
        "status": "running",
        "title": "Shorty — self-hosted URL shortener",
        "spec": {"work_fingerprint": "fp-1", "repo": "repo"},
        "created_at": "2026-01-01T00:00:00Z",
    })

    class _NoSql:
        def execute(self, *_a, **_k):
            raise RuntimeError("raw SQL is unavailable in the OSS in-memory persistence adapter")

    # By fingerprint, and by app name — both paths, both stores.
    for connection in (_NoSql(), None):
        assert active_duplicate(connection, "project", "fp-1") == "run-live"
        assert active_duplicate(connection, "project", "fp-other") is None
        assert active_same_app(connection, "project", "repo",
                               "Shorty — self-hosted URL shortener · retry 2") == "run-live"
        assert active_same_app(connection, "project", "other-repo",
                               "Shorty — self-hosted URL shortener") is None


def test_a_finished_run_is_not_a_duplicate(restore_ports):
    """Running the same task again after it finished is legitimate — that is what the Launch
    button on a finished card is for. Only IN-FLIGHT work blocks."""
    from boss_engine.services.code_task_service import active_duplicate

    ports = EnginePorts.oss_defaults(_settings())
    configure_engine_ports(ports)
    ports.persistence.save_run("run-done", {
        "id": "run-done",
        "project_id": "project",
        "status": "completed",
        "title": "Shorty",
        "spec": {"work_fingerprint": "fp-1", "repo": "repo"},
        "created_at": "2026-01-01T00:00:00Z",
    })
    assert active_duplicate(None, "project", "fp-1") is None


def test_an_unanswerable_duplicate_check_does_not_block_work(restore_ports):
    """When the store cannot answer at all, a second run is a lesser harm than no runs."""
    from boss_engine.services import code_task_service

    class _Broken:
        def list_runs(self, *_a, **_k):
            raise RuntimeError("store is down")

    ports = dataclasses.replace(EnginePorts.oss_defaults(_settings()), persistence=_Broken())
    configure_engine_ports(ports)
    assert code_task_service.active_duplicate(None, "project", "fp-1") is None
    assert code_task_service.active_same_app(None, "project", "repo", "Shorty") is None
