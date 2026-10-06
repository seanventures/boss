"""Three holes the authorization review found, each closed as a class (identity int-b, items 3-5).

  * POST /v1/mutation-gate never runs a caller-chosen program: the test command is one of an
    allow-list, file paths stay inside the gate's temporary tree, and the tests run with none of
    the API's environment.
  * POST /v1/factory/quality-gate is a gate WAIVER: it needs code_task:override, its run must be
    in the request's project, and it never replaces a recorded verdict silently — a reason is
    required, the prior verdict is kept, and a pass over a fail is recorded as `waived`.
  * POST /v1/orchestration/next-move/execute is the worker's, with a LIVE lease on the very job.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from apps.api.app.main import app
from apps.api.tests.identity_fixtures import IdentityWorld
from boss_engine.identity.principal import AuthContext, AuthMethod, Principal, PrincipalKind, PrincipalStatus
from boss_engine.services import mutation_gate as mg
from boss_engine.services import owned_process


@pytest.fixture
def world(engine) -> IdentityWorld:
    return IdentityWorld.create(engine)


# ---- 3. the mutation gate ---------------------------------------------------------------------------

@pytest.fixture
def no_process(monkeypatch):
    ran: list = []
    monkeypatch.setattr(owned_process, "run", lambda *a, **k: ran.append(a) or (_ for _ in ()).throw(
        AssertionError("the gate ran a process")))
    return ran


@pytest.mark.parametrize("command", [
    ["sh", "-c", "id > /tmp/pwned"], "bash -c 'curl evil'", ["python3", "-c", "print(1)"],
    ["python3", "-m", "pytest", "-q", "--rootdir=/"], ["/usr/bin/python3", "-m", "unittest"],
])
def test_an_arbitrary_test_command_is_refused_before_anything_runs(client, no_process, command):
    out = client.post("/v1/mutation-gate", json={
        "files": {"solution.py": "def f():\n    return 1\n", "test_solution.py": "x = 1\n"},
        "target_file": "solution.py", "test_command": command})
    assert out.status_code == 422 and "test_command" in out.text, out.text
    assert no_process == []


@pytest.mark.parametrize("path", ["../escape.py", "/etc/cron.d/boss", "a/../../b.py", "~/x.py", "C:/x.py"])
def test_a_file_path_outside_the_gate_tree_is_refused(client, no_process, path):
    out = client.post("/v1/mutation-gate", json={
        "files": {"solution.py": "def f():\n    return 1\n", path: "x = 1\n"},
        "target_file": "solution.py"})
    assert out.status_code == 422, out.text
    assert no_process == []


def test_the_allowed_commands_run_this_interpreter_with_none_of_the_apis_environment(monkeypatch):
    import sys

    assert mg.allowed_test_command(None)[0] == sys.executable
    assert mg.allowed_test_command(["python3", "-m", "pytest", "-q"])[1:] == ["-m", "pytest", "-q"]
    monkeypatch.setenv("DATABASE_URL", "postgresql://secret")
    env = mg._scrubbed_env("/tmp/x")
    assert "DATABASE_URL" not in env and not any("KEY" in k or "TOKEN" in k for k in env)


# ---- 4. the quality gate -----------------------------------------------------------------------------

ALL_PASS = {"tests_passed": True, "ci_green": True, "lint_passed": True, "reviewer_independent": True,
            "test_author_independent": True, "adversarial_verdict": "survived"}
FAIL = {**ALL_PASS, "tests_passed": False}


def test_the_quality_gate_is_a_waiver_and_never_overwrites_silently(world):
    client = TestClient(app, raise_server_exceptions=False)
    admin, operator = world.person("admin"), world.person("operator")
    world.bind(admin, "admin", project=world.project_a)
    world.bind(operator, "operator", project=world.project_a)
    run = "crun_gate_x"
    body = {"project_id": world.project_a, "code_task_run_id": run}
    with world.acting_as(world.ctx(operator)):
        assert client.post("/v1/factory/quality-gate", json={**body, "checks": ALL_PASS}).status_code == 403
    with world.acting_as(world.ctx(admin)):
        first = client.post("/v1/factory/quality-gate", json={**body, "checks": FAIL})
        silent = client.post("/v1/factory/quality-gate", json={**body, "checks": ALL_PASS})
        stated = client.post("/v1/factory/quality-gate",
                             json={**body, "checks": ALL_PASS, "override_reason": "flaky CI, rerun green"})
    assert first.status_code == 201 and first.json()["status"] == "fail", first.text
    assert silent.status_code == 409 and "override_reason" in silent.text, silent.text
    assert stated.status_code == 201 and stated.json()["status"] == "waived", stated.text
    with world.engine.connect() as db:
        status, evidence = db.execute(text(
            "SELECT status, evidence FROM quality_gates WHERE project_id = :p AND code_task_run_id = :r"),
            {"p": world.project_a, "r": run}).one()
        events = db.execute(text("SELECT count(*) FROM events WHERE project_id = :p "
                                 "AND event_type = 'quality_gate.waived'"), {"p": world.project_a}).scalar()
    assert status == "waived" and evidence["override"]["prior_status"] == "fail"
    assert evidence["override"]["by"] == admin.id and events == 1


def test_the_quality_gates_run_must_be_this_projects(world):
    client = TestClient(app, raise_server_exceptions=False)
    admin = world.person("admin")
    world.bind(admin, "admin", project=world.project_a)
    with world.engine.begin() as db:
        db.execute(text("INSERT INTO code_task_runs (id, project_id, title) VALUES ('crun_in_b', :p, 't')"),
                   {"p": world.project_b})
    with world.acting_as(world.ctx(admin)):
        out = client.post("/v1/factory/quality-gate", json={
            "project_id": world.project_a, "code_task_run_id": "crun_in_b", "checks": ALL_PASS})
    assert out.status_code == 404 and out.json() == {"error": "not_found"}, out.text


# ---- 5. next-move execute ----------------------------------------------------------------------------

def _worker(world) -> AuthContext:
    return AuthContext(Principal("sys_worker", PrincipalKind.SYSTEM, world.org, PrincipalStatus.ACTIVE,
                                 "worker"), AuthMethod.SYSTEM)


def test_next_move_execute_needs_the_worker_and_a_live_lease_on_that_job(world):
    from apps.api.app.main import jobs

    client = TestClient(app, raise_server_exceptions=False)
    with world.engine.begin() as db:
        job = jobs.create_job(db, world.project_a, "agent.orchestrator.next_move", {})
        other = jobs.create_job(db, world.project_a, "agent.brief", {})
    with world.engine.begin() as db:
        leased = jobs.claim_job(db, "w-1", ["agent.orchestrator.next_move"], 120)
    assert leased and leased["id"] == job["id"], leased
    base = {"project_id": world.project_a, "event_data": {}}
    lease = {"worker_id": "w-1", "created_by_job_id": job["id"], "lease_token": leased["lease_token"]}
    owner = world.person("owner")
    world.bind(owner, "owner")
    with world.acting_as(world.ctx(owner)):
        person = client.post("/v1/orchestration/next-move/execute", json={**base, **lease})
    with world.acting_as(_worker(world)):
        no_lease = client.post("/v1/orchestration/next-move/execute", json=base)
        bad_token = client.post("/v1/orchestration/next-move/execute",
                                json={**base, **lease, "lease_token": "lease_forged"})
        wrong_job = client.post("/v1/orchestration/next-move/execute",
                                json={**base, **lease, "created_by_job_id": other["id"]})
        wrong_project = client.post("/v1/orchestration/next-move/execute",
                                    json={**base, **lease, "project_id": world.project_b})
        good = client.post("/v1/orchestration/next-move/execute", json={**base, **lease})
    assert person.status_code == 403, person.text
    assert no_lease.status_code == 422, no_lease.text
    assert bad_token.status_code == 409 and wrong_job.status_code == 409, (bad_token.text, wrong_job.text)
    assert wrong_project.status_code == 409, wrong_project.text
    assert good.status_code == 200, good.text
