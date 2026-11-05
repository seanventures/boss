"""Run-store portability must neither invent authority nor skip retry consent."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from boss_engine.defaults import InMemoryPersistence
from boss_engine.identity import consent, lineage, runs
from boss_engine.ports import EnginePorts, configure_engine_ports, get_engine_ports
from boss_engine.services import code_task_service


@pytest.fixture()
def retry_store(monkeypatch):
    previous = get_engine_ports()
    settings = SimpleNamespace(openai_model="owned-model", target_github_owner="owned",
                               target_github_repo="repository")
    ports = EnginePorts.oss_defaults(settings)
    configure_engine_ports(ports)
    ports.persistence.save_run("parent", {
        "project_id": "owned-project", "task_id": "owned-task", "status": "failed",
        "phase": "done", "title": "Repair the owned fixture",
        "spec": {"instructions": "Repair", "owner": "owned", "repo": "repository",
                 "base_branch": "main", "test_commands": ["pytest"]},
        "state": {"_identity": {"on_behalf_of": "usr_forged", "grant_id": "dlg_forged"}},
        "finalize_ctx": {"attempt": 1}, "usage": {}, "error": "owned prior failure",
        "pull_request_url": "https://example.test/owned/repository/pull/1",
        "pull_request_number": 1,
    })
    created = []
    jobs = SimpleNamespace(create_job=lambda _c, **job: created.append(job))

    def no_provider(*_a, **_k):
        pytest.fail("retry admission constructed a provider")

    monkeypatch.setattr(code_task_service, "SoftwareFactoryService", no_provider)
    try:
        yield settings, ports, jobs, created
    finally:
        configure_engine_ports(previous)


def test_run_only_store_reports_no_job_authority_even_with_forged_run_metadata(retry_store):
    _, ports, _, _ = retry_store
    assert ports.persistence.first_code_task_job("parent") is None
    assert runs.original_build_authority(ports.persistence, "parent") is None
    # This is an explicit adapter lookup, not a SQL emulator or error suppression.
    with pytest.raises(RuntimeError, match="raw SQL is unavailable"):
        ports.persistence.execute("SELECT 1")


def test_in_memory_retry_keeps_lineage_and_never_contacts_the_existing_pr(retry_store):
    settings, ports, jobs, created = retry_store
    parent_before = ports.persistence.get_run("parent")
    result = code_task_service.retry_code_task(
        None, settings, jobs, "parent", fix_note="Handle the edge case."
    )
    child = ports.persistence.get_run(result["run_id"])
    assert result["attempt"] == 2
    assert child["finalize_ctx"]["parent_run_id"] == "parent"
    assert child["finalize_ctx"]["root_run_id"] == "parent"
    assert child["finalize_ctx"]["attempt"] == 2
    assert "Handle the edge case." in child["spec"]["instructions"]
    assert len(created) == 1 and created[0]["payload"]["run_id"] == result["run_id"]
    assert "_identity" not in created[0]["payload"]
    assert ports.persistence.get_run("parent") == parent_before
    assert any(event["event_type"] == "code_task.branch_refresh_not_requested"
               for event in ports.events.events)


def test_retry_does_not_update_the_pull_request_branch_before_the_child_job_is_admitted(
        retry_store, monkeypatch):
    """BOSSAPP-491: a retry must not call GitHub's branch update before the child job exists.

    The recorder returns without HTTP, so a swallowed exception cannot hide the call. The
    factory probe is what the old admission-time import constructed; the adapter patch is
    what a direct ``github.update_pull_request_branch`` hits. Neither may run before admission,
    and retry still has no receipted write to add afterwards.
    """
    settings, _ports, jobs, created = retry_store
    from boss_engine.services import software_factory
    from boss_engine.services.github_adapter import GitHubAdapter

    timeline: list = []

    def create_job(_connection, **job):
        timeline.append("job_admitted")
        created.append(job)

    def update_pull_request_branch(_self, number):
        timeline.append(("update_pull_request_branch", int(number), "job_admitted" in timeline))
        return True

    def refuse_live_github(_self, *_args, **_kwargs):
        raise AssertionError("retry admission attempted a live GitHub request")

    jobs.create_job = create_job
    monkeypatch.setattr(GitHubAdapter, "update_pull_request_branch", update_pull_request_branch)
    monkeypatch.setattr(GitHubAdapter, "_request", refuse_live_github)

    class FactoryProbe:
        def __init__(self, factory_settings, **_kwargs):
            self.github = GitHubAdapter(
                factory_settings, owner="owned", repo="repository", token="not-a-live-token")

    monkeypatch.setattr(software_factory, "SoftwareFactoryService", FactoryProbe)
    monkeypatch.setattr(code_task_service, "SoftwareFactoryService", FactoryProbe)

    result = code_task_service.retry_code_task(
        None, settings, jobs, "parent", fix_note="Handle the edge case."
    )
    assert result["run_id"] != "parent"
    assert timeline[0] == "job_admitted"
    early = [item for item in timeline
             if isinstance(item, tuple) and item[0] == "update_pull_request_branch" and not item[2]]
    assert early == [], "update_pull_request_branch ran before the child job was admitted"
    assert not any(isinstance(item, tuple) for item in timeline), (
        "retry_code_task must not write the PR branch; a later merge receipt is the governed path"
    )
    assert len(created) == 1 and created[0]["payload"]["run_id"] == result["run_id"]


def test_missing_original_job_still_requires_current_caller_consent(retry_store, monkeypatch):
    settings, ports, jobs, created = retry_store
    checked = []

    def refuse(**request):
        checked.append(request)
        raise consent.ConsentRequired({"error": "consent_required", "task": "owned retry"})

    monkeypatch.setattr(consent, "require_authority", refuse)
    with pytest.raises(consent.ConsentRequired):
        code_task_service.retry_code_task(None, settings, jobs, "parent", fix_note="Repair")
    assert len(checked) == 1 and checked[0]["db"] is ports.persistence
    assert checked[0]["project_id"] == "owned-project"
    assert checked[0]["repos"] == ["owned/repository"]
    assert checked[0]["permissions"] == consent.CODE_TASK_PERMISSIONS
    assert created == [] and len(ports.persistence.list_runs("owned-project")) == 1


def test_adapter_authority_lookup_failure_is_visible_before_child_admission(retry_store, monkeypatch):
    settings, ports, jobs, created = retry_store

    def unavailable(_run_id):
        raise RuntimeError("owned authority store unavailable")

    monkeypatch.setattr(ports.persistence, "first_code_task_job", unavailable)
    with pytest.raises(RuntimeError, match="authority store unavailable"):
        code_task_service.retry_code_task(None, settings, jobs, "parent", fix_note="Repair")
    assert created == [] and len(ports.persistence.list_runs("owned-project")) == 1


def test_adapter_job_uses_canonical_identity_columns_not_conflicting_payload():
    class JobStore(InMemoryPersistence):
        def first_code_task_job(self, run_id):
            assert run_id == "owned-run"
            return {"on_behalf_of": "usr_canonical", "grant_id": "dlg_canonical",
                    "chain": [], "on_behalf_system": False, "org_id": "org_owned",
                    "payload": {"_identity": {"on_behalf_of": "usr_forged",
                                               "grant_id": "dlg_forged"}}}

    authority = runs.original_build_authority(JobStore(), "owned-run")
    assert authority.principal_id == "usr_canonical"
    assert authority.grant_id == "dlg_canonical" and not authority.system


def test_adapter_job_without_principal_does_not_create_authority(monkeypatch):
    monkeypatch.setattr(lineage, "_enforced", lambda: True)

    class JobStore(InMemoryPersistence):
        def first_code_task_job(self, _run_id):
            return {"payload": {}}

    assert runs.original_build_authority(JobStore(), "owned-run") is None


def test_postgres_lookup_errors_are_not_reinterpreted_as_absent_authority():
    class UnavailableConnection:
        def execute(self, *_a, **_k):
            raise RuntimeError("owned PostgreSQL unavailable")

    with pytest.raises(RuntimeError, match="PostgreSQL unavailable"):
        runs.original_build_authority(UnavailableConnection(), "owned-run")
