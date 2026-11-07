"""The onboarding agent readies a new project by itself and asks only for what it cannot do
(BOSSAPP-913, contract §8).

A project is created the way a person creates one; onboarding starts by itself as a queued
`project.onboarding` job. Its passes run here inline against the real database with a stub for
the outside world (GitHub's access probe, CI, issues; no model is ever called): it checks repo
access, waits for the code graph, writes the dossier, reads CI, declares the secrets by name,
finds the demo users, ranks a backlog with the Chief of Staff's scoring and suggests first work.
It works with the ambient preview off.
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from apps.api.app import main
from apps.api.tests.identity_fixtures import IdentityWorld
from boss_engine.config import get_settings
from boss_engine.services.project_experience import onboarding

REPO = "acme/helpdesk-ai-demo"


@pytest.fixture(autouse=True)
def _run_record_is_the_model_answer(monkeypatch):
    """These runs answer the model check through StubProbes; the view's LIVE read of Repo and
    models (round 3, 10-20) is this install's, so here it defers to the run's own record."""
    monkeypatch.setattr(onboarding, "_live_models", lambda connection, project_id: None)


class StubProbes(onboarding.Probes):
    """GitHub and Repo-and-models, as answers; everything else is BOSS's own code."""

    def __init__(self, root: Path, *, access=(True, True, True), ci="failed", models=True,
                 boom: bool = False, kept: bool = True, fetched: bool = False,
                 pick: str | None = None):
        self.root, self.access, self.ci_state, self.models, self.boom = root, access, ci, models, boom
        self.kept, self.fetched, self.pick = kept, fetched, pick
        self.index_requested = 0
        self.asked: list[dict] = []

    def check_repository(self, connection, settings, project_id, binding_id):
        if self.boom:
            raise RuntimeError("GitHub answered 502")
        ok = lambda v: {"ok": v, "state": "working" if v else "forbidden"}  # noqa: E731
        return {"state": "working", "checks": {"read": ok(self.access[0]), "write": ok(self.access[1]),
                                               "pull_requests": ok(self.access[2])}}

    def models_ready(self, connection, settings, project_id):
        return (True, "Claude connected") if self.models else (False, "No model connected yet")

    def reader(self, connection, settings, project_id, repo):
        return object()

    def ci(self, reader, branch):
        if self.ci_state == "failed":
            return {"state": "failed", "words": "CI failed", "checks": 6, "passing": 5,
                    "failing": ["test"], "url": "https://ci.example/run/1", "has_ci": True}
        if self.ci_state == "passed":
            return {"state": "passed", "words": "CI passed", "checks": 6, "passing": 6,
                    "failing": [], "url": None, "has_ci": True}
        return {"state": "no_ci", "words": "No CI", "checks": 0, "passing": 0, "failing": [],
                "url": None, "has_ci": False}

    def issues(self, reader):
        return [{"number": 7, "title": "Login fails for agents", "labels": [{"name": "bug"}],
                 "html_url": f"https://github.com/{REPO}/issues/7", "body": "500 on POST /login"},
                {"number": 9, "title": "Dark mode", "labels": [],
                 "html_url": f"https://github.com/{REPO}/issues/9"}]

    def pulls(self, reader):
        return [{"number": 3, "title": "Visual refresh finish",
                 "html_url": f"https://github.com/{REPO}/pull/3"}]

    def tree_paths(self, connection, project_id, repo, branch):
        return ["package.json", "server/index.mjs", "tests/a.test.mjs"]

    def request_index(self, connection, settings, project_id):
        self.index_requested += 1

    def checkout(self, connection, settings, project_id, repo):
        if not self.kept:          # the index's checkout is gone (an API restart empties /tmp)
            return None, ["server/index.mjs"]
        files = sorted(str(p.relative_to(self.root)) for p in self.root.rglob("*") if p.is_file())
        return self.root, files

    def fetch(self, engine, settings, project_id, repo, branch, binding_id):
        if not self.fetched:
            raise RuntimeError("could not fetch main: network is unreachable")
        return self.root

    def marshal(self, engine, settings, project_id, run_id, *, message, choices, context, hints):
        self.asked.append({"choices": choices, "context": context, "hints": hints})
        if self.pick is None:
            return {"decided": False, "action": None, "reason": "no model is connected"}
        chosen = next((c["key"] for c in choices if self.pick in c["label"]), None)
        return {"decided": chosen is not None, "action": chosen,
                "reason": "A red suite blocks every later change. Then the rest."}


@pytest.fixture(scope="module")
def world(engine) -> IdentityWorld:
    w = IdentityWorld.create(engine)
    w.owner = w.person("onb-owner")
    w.bind(w.owner, "owner")
    w.viewer = w.person("onb-viewer")
    w.bind(w.viewer, "viewer")
    return w


@pytest.fixture
def client():
    return TestClient(main.app, raise_server_exceptions=False)


@pytest.fixture
def checkout(tmp_path) -> Path:
    root = tmp_path / "helpdesk"
    (root / "server").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "package.json").write_text(json.dumps(
        {"scripts": {"test": "node --test", "build": "vite build", "start": "node server"}}))
    (root / "README.md").write_text("# HelpDesk AI\n")
    (root / "server" / "index.mjs").write_text("// TODO: rate limit\nexport const app = 1;\n")
    (root / "server" / "seed.mjs").write_text(
        "export const users = ['customer@demo.test', 'agent@demo.test', 'admin@demo.test'];\n")
    (root / "tests" / "sla.test.mjs").write_text("import test from 'node:test';\n")
    (root / "boss.deploy.yaml").write_text(
        "version: 1\nname: helpdesk\nkind: commands\ninstall: [{name: up, run: npm start}]\n"
        "env:\n  DEMO_PASSWORD: {generate: password}\n  STRIPE_KEY: {secret: stripe-key}\n")
    return root


def _project(world, client, engine, *, repo: bool = True) -> str:
    slug = f"onb-{uuid.uuid4().hex[:8]}"
    with world.acting_as(world.ctx(world.owner)):
        made = client.post("/v1/projects", json={"name": "HelpDesk AI Demo", "slug": slug})
    assert made.status_code == 201, made.text
    if repo:
        with engine.begin() as db:
            db.execute(text(
                "INSERT INTO github_connections (id, project_id, connection_label, auth_method, "
                "credential_ref, status, created_by) VALUES (:c, :p, 'test', 'personal_access_token', "
                "'secret:none', 'active', 'test')"), {"c": f"ghc_{slug}", "p": slug})
            db.execute(text(
                "INSERT INTO github_repository_bindings (id, project_id, connection_id, owner, name, "
                "full_name, role, default_branch, created_by) VALUES (:b, :p, :c, 'acme', "
                "'helpdesk-ai-demo', :r, 'primary', 'main', 'test')"),
                {"b": f"ghb_{slug}", "p": slug, "c": f"ghc_{slug}", "r": REPO})
    return slug


def _mapped(engine, project_id: str) -> None:
    with engine.begin() as db:
        db.execute(text("UPDATE kg_repo_index SET status = 'indexed', files = 7, parsed = 7, "
                        "nodes = 40, indexed_at = now(), due = false WHERE project_id = :p"),
                   {"p": project_id})


def _run_id(engine, project_id: str) -> str:
    with engine.connect() as db:
        return db.execute(text("SELECT id FROM project_onboarding_runs WHERE project_id = :p "
                               "ORDER BY started_at DESC LIMIT 1"), {"p": project_id}).scalar()


def _view(engine, project_id: str) -> dict:
    with engine.connect() as db:
        return onboarding.view(db, project_id)


def test_creating_a_project_starts_onboarding_and_queues_its_job(world, client, engine):
    pid = _project(world, client, engine, repo=False)
    run = _view(engine, pid)
    assert run["state"] == "running" and run["trigger"] == "created"
    with engine.connect() as db:
        job = db.execute(text("SELECT kind, payload, max_attempts FROM jobs WHERE project_id = :p "
                              "AND kind = 'project.onboarding'"), {"p": pid}).mappings().one()
        captain = db.execute(text("SELECT role_key FROM project_captains WHERE project_id = :p"),
                             {"p": pid}).scalar()
    assert job["payload"]["run_id"] == run["run_id"] and job["max_attempts"] == 1
    assert captain == "engineering_captain"


def test_without_a_repository_it_asks_only_for_the_repository(world, client, engine, checkout):
    pid = _project(world, client, engine, repo=False)
    out = onboarding.advance(engine, get_settings(), _run_id(engine, pid),
                             probes=StubProbes(checkout), requeue=False)
    assert out["state"] == "needs_you"
    run = _view(engine, pid)
    assert [i["key"] for i in run["only_you"]] == ["repo_access", "deploy_target"]
    repo = run["only_you"][0]
    assert repo["owner"] == "you" and repo["action"]["label"] == "Add repository"
    assert "onboarding continues by itself" in repo["detail"]


def test_it_readies_the_project_and_proposes_the_backlog_and_first_work(world, client, engine,
                                                                       checkout):
    settings = get_settings()
    assert str(getattr(settings, "ambient_cos_mode", "off")) in ("", "off")   # the preview is off
    pid = _project(world, client, engine)
    _mapped(engine, pid)
    probes = StubProbes(checkout)
    out = onboarding.advance(engine, settings, _run_id(engine, pid), probes=probes, requeue=False)
    assert out == {"status": "succeeded", "state": "needs_you", "again": False}
    run = _view(engine, pid)
    items = {i["key"]: i for i in run["checklist"]}
    assert items["repo_access"]["state"] == "done" and items["repo_access"]["sub"] == "Read · Write · PR"
    assert items["models"]["state"] == "done"
    assert items["knowledge"]["state"] == "done" and "7 files" in items["knowledge"]["detail"]
    # Tests RAN (in CI): the check is done and its failures go to the backlog (screen 10
    # "6 failing → backlog"); the red default branch is the Baseline build's failure.
    assert items["tests"]["state"] == "done" and items["tests"]["sub"] == "1 failing → backlog"
    assert items["baseline_build"]["state"] == "failed"
    assert items["deploy_target"]["owner"] == "you"
    assert items["secrets"]["owner"] == "you" and "STRIPE_KEY" in items["secrets"]["detail"]
    assert all(len(i["sub"].split()) <= 6 for i in run["checklist"])
    assert {i["key"] for i in run["only_you"]} == {"deploy_target", "secrets"}
    # The backlog: failing CI first (Chief of Staff scoring), then the bug, the issues, the PR.
    backlog = run["backlog"]
    assert backlog[0]["source"] == "ci" and backlog[0]["label"] == "Fix 1 failing check"
    assert any(b["source"] == "bug" and "Login fails" in b["label"] for b in backlog)
    assert any(b["label"] == "Finish PR #3" for b in backlog)
    assert [b["score"] for b in backlog] == sorted((b["score"] for b in backlog), reverse=True)
    first = run["first_work"]
    assert first["label"] == "Fix 1 failing check" and first["source"] == "ci"
    assert first["start"]["href"] == f"/v1/projects/{pid}/work"
    assert first["start"]["body"]["kind"] == "bug"
    stages = {s["key"]: s["state"] for s in run["stages"]}
    # Discover is done (it read everything); the red default branch fails READY, not Discover.
    assert stages["discover"] == "done" and stages["ready"] == "failed"
    # ONE STATE MACHINE (10-08): Ready is current (failed), so everything after it is ahead —
    # the drafted backlog keeps its words ("N proposed") but is not ticked before Ready.
    assert stages["backlog"] == "todo" and run["stages"][2]["sub"].endswith(" proposed")
    assert all(b["why"] for b in backlog), "every item says why in a few words"
    assert first["first_by"] == "score" and first["why_first"] == "highest Chief-of-Staff score"
    assert stages["first_work"] == "todo" and stages["deploy"] == "todo"
    assert not run["live"] and run["title"] == "Helm read your repo"
    # The Now line names whose move it is once the read is done: only you, here.
    assert run["now"].startswith("Waiting on you") and run["now_live"] is False
    assert run["log"][0]["label"] == "Started" and run["log"][-1]["label"] == "Finished"
    # Screen 10's Discovered map: each node's label IS the finding; screen 00b's live diagram
    # names the steps (Stack found · Mapping code · Tests run · Ready check).
    nodes = run["discover"]["nodes"]
    assert [n["key"] for n in nodes] == ["files", "stack", "tests", "hotspots", "docs"]
    assert nodes[0]["label"].endswith(" files") and nodes[2]["label"] == "5/6 tests"
    assert all(len(n["label"].split()) <= 4 for n in nodes)
    assert [n["label"] for n in run["discover"]["live"]] == ["Stack found", "Mapping code",
                                                             "Tests run", "Ready check"]
    with engine.connect() as db:
        facts = db.execute(text("SELECT facts FROM project_onboarding_runs WHERE project_id = :p"),
                           {"p": pid}).scalar()
        dossier = db.execute(text("SELECT read_from FROM project_dossiers WHERE project_id = :p"),
                             {"p": pid}).scalar()
    assert [u["user"] for u in facts["demo_users"]] == ["customer@demo.test", "agent@demo.test",
                                                        "admin@demo.test"]
    assert facts["secrets"]["yours"] == ["STRIPE_KEY"] and "DEMO_PASSWORD" in facts["secrets"]["demo"]
    assert dossier == "checkout"


def test_green_ci_and_a_verified_shared_host_leave_only_the_deploy_confirmation(world, client,
                                                                               engine, checkout):
    pid = _project(world, client, engine)
    _mapped(engine, pid)
    (checkout / "boss.deploy.yaml").write_text(
        "version: 1\nname: helpdesk\nkind: commands\ninstall: [{name: up, run: npm start}]\n")
    with engine.begin() as db:
        db.execute(text("INSERT INTO deployment_hosts (id, project_id, name, address, ssh_user, status, "
                        "created_by) VALUES (:i, :p, 'shared', '203.0.113.20', 'deploy', 'verified', "
                        "'test')"), {"i": f"dhost_{uuid.uuid4().hex[:8]}", "p": world.project_a})
    onboarding.advance(engine, get_settings(), _run_id(engine, pid),
                       probes=StubProbes(checkout, ci="passed"), requeue=False)
    run = _view(engine, pid)
    items = {i["key"]: i for i in run["checklist"]}
    assert items["tests"]["state"] == "done" and items["baseline_build"]["sub"] == "CI green"
    assert items["secrets"]["state"] == "done"
    deploy = items["deploy_target"]
    assert deploy["owner"] == "you" and "203.0.113.20" in deploy["detail"] and "shared" in deploy["detail"]
    assert deploy["action"]["label"] == "Use for staging"
    assert run["state"] == "needs_you" and [i["key"] for i in run["only_you"]] == ["deploy_target"]


def test_it_waits_for_the_code_graph_then_carries_on_from_githubs_file_list(world, client,
                                                                          engine, checkout,
                                                                          monkeypatch):
    pid = _project(world, client, engine)
    run_id = _run_id(engine, pid)
    probes = StubProbes(checkout, ci="none", kept=False)
    first = onboarding.advance(engine, get_settings(), run_id, probes=probes, requeue=False)
    assert first["again"] is True and first["state"] == "running"
    knowledge = {i["key"]: i for i in _view(engine, pid)["checklist"]}["knowledge"]
    assert knowledge["state"] == "now" and knowledge["sub"] == "Queued to map"
    assert probes.index_requested == 1
    monkeypatch.setattr(onboarding, "MAX_WAIT_S", 0)
    second = onboarding.advance(engine, get_settings(), run_id, probes=probes, requeue=False)
    assert second["again"] is False
    items = {i["key"]: i for i in _view(engine, pid)["checklist"]}
    assert "GitHub's list of 3 files only" in items["knowledge"]["detail"]
    assert "network is unreachable" in items["knowledge"]["detail"]
    # The secrets item gives the SAME reason, never a contradicting one.
    assert items["secrets"]["state"] == "skipped"
    assert "network is unreachable" in items["secrets"]["detail"]
    assert items["baseline_build"]["state"] == "skipped"
    assert "own sandbox" in items["baseline_build"]["detail"]


def test_a_pass_that_waits_queues_its_next_pass_instead_of_holding_the_worker(world, client,
                                                                              engine, checkout):
    pid = _project(world, client, engine)
    run_id = _run_id(engine, pid)
    out = onboarding.advance(engine, get_settings(), run_id, probes=StubProbes(checkout))
    assert out["again"] is True
    with engine.connect() as db:
        passes = db.execute(text("SELECT payload->>'pass' FROM jobs WHERE project_id = :p AND "
                                 "kind = 'project.onboarding' ORDER BY created_at"),
                            {"p": pid}).scalars().all()
        after = db.execute(text("SELECT run_after > now() FROM jobs WHERE project_id = :p AND "
                                "payload->>'pass' = '2'"), {"p": pid}).scalar()
    assert passes == ["1", "2"] and after is True


def test_access_it_cannot_get_is_asked_of_a_person_in_one_line(world, client, engine, checkout):
    pid = _project(world, client, engine)
    onboarding.advance(engine, get_settings(), _run_id(engine, pid),
                       probes=StubProbes(checkout, access=(True, False, False)), requeue=False)
    item = {i["key"]: i for i in _view(engine, pid)["checklist"]}["repo_access"]
    assert item["state"] == "failed" and item["owner"] == "you"
    assert item["detail"].startswith(f"BOSS cannot push branches, open pull requests on {REPO}")
    assert "\n" not in item["detail"] and item["action"]["label"] == "Fix access"


def test_a_failing_probe_or_a_crash_is_recorded_never_raised(world, client, engine, checkout,
                                                             monkeypatch):
    pid = _project(world, client, engine)
    onboarding.advance(engine, get_settings(), _run_id(engine, pid),
                       probes=StubProbes(checkout, boom=True), requeue=False)
    item = {i["key"]: i for i in _view(engine, pid)["checklist"]}["repo_access"]
    assert item["state"] == "failed" and "GitHub answered 502" in item["detail"]

    other = _project(world, client, engine)
    _mapped(engine, other)

    def broken(*_a, **_k):
        raise ValueError("the dossier store is read-only\nsecond line")

    monkeypatch.setattr(onboarding, "_knowledge", broken)
    out = onboarding.advance(engine, get_settings(), _run_id(engine, other),
                             probes=StubProbes(checkout), requeue=False)
    assert out["status"] == "failed"
    run = _view(engine, other)
    assert run["state"] == "failed"
    assert run["error"] == "Onboarding stopped: the dossier store is read-only"


def test_start_is_idempotent_a_viewer_cannot_start_and_only_the_worker_executes(world, client,
                                                                                engine):
    pid = _project(world, client, engine, repo=False)
    with world.acting_as(world.ctx(world.owner)):
        one = client.post(f"/v1/projects/{pid}/onboarding/start")
        two = client.post(f"/v1/projects/{pid}/onboarding/start")
        read = client.get(f"/v1/projects/{pid}/onboarding")
        not_worker = client.post("/v1/projects/onboarding/execute",
                                 json={"job_id": "j", "worker_id": "w", "lease_token": "t"})
    assert one.status_code == 200, one.text
    assert one.json()["run_id"] == two.json()["run_id"] == read.json()["run_id"]
    assert read.json()["start"]["href"] == f"/v1/projects/{pid}/onboarding/start"
    assert not_worker.status_code == 403
    with world.acting_as(world.ctx(world.viewer)):
        refused = client.post(f"/v1/projects/{pid}/onboarding/start")
        seen = client.get(f"/v1/projects/{pid}/onboarding")
    assert refused.status_code == 403 and seen.status_code == 200


def test_the_worker_leases_onboarding_jobs_under_a_declared_permission():
    from apps.worker.boss_worker.runner import capabilities
    from boss_engine.identity.job_permissions import JOB_KIND_PERMISSIONS

    assert "project.onboarding" in capabilities()
    assert JOB_KIND_PERMISSIONS["project.onboarding"] == "workflow_run:start"


# ── 10-08: the stage strip is one state machine ───────────────────────────────────────────────

def _strip(*states):
    keys = ("discover", "ready", "backlog", "first_work", "deploy")
    return [{"key": k, "label": k, "state": st, "sub": ""} for k, st in zip(keys, states)]


def test_the_stage_strip_has_one_current_stage_and_nothing_after_it_is_done():
    """Tester 10-08: Ready current while later stages were ticked (Propose backlog done while
    Ready was 4 of 6). Round 3 (10-07): Deploy is a fact — staging live stays done."""
    for run_state in ("running", "ready", "needs_you", "failed"):
        for raw in (("done", "now", "done", "now", "done"), ("done", "done", "done", "now", "done"),
                    ("done", "failed", "done", "todo", "done"), ("now", "todo", "todo", "todo", "done"),
                    ("done", "done", "done", "done", "todo")):
            out = [s["state"] for s in onboarding.linear_stages(_strip(*raw), run_state)]
            current = [i for i, st in enumerate(out) if st not in ("done", "skipped")]
            if current:
                first = current[0]
                assert out[first] in ("now", "failed", "waiting"), (run_state, raw, out)
                # Nothing after the current stage is live; a later stage whose own fact is done
                # stays done (round 3, 10-07: Deploy after four deploys), the rest is todo.
                assert all(st == ("done" if r == "done" and i == 4 else "todo")
                           for i, (st, r) in enumerate(zip(out, raw)) if i > first), (run_state, raw, out)
                assert all(st in ("done", "skipped") for st in out[:first])
    assert [s["state"] for s in onboarding.linear_stages(
        _strip("done", "done", "done", "done", "done"), "not_started")] == ["todo"] * 5
    assert [s["state"] for s in onboarding.linear_stages(
        _strip("done", "now", "todo", "todo", "todo"), "failed")][1] == "failed"


def test_the_now_line_names_whose_move_it_is():
    items = [{"key": "deploy_target", "state": "now", "owner": "agent", "sub": "port 8004"},
             {"key": "secrets", "state": "todo", "owner": "you", "sub": "OPENAI_API_KEY"}]
    live = onboarding.now_of(items, running=True)
    assert live == {"agent": "sre", "text": "Beacon is checking the staging host · port 8004",
                    "live": True}
    waiting = onboarding.now_of(items, running=False, run_state="needs_you")
    assert waiting["text"] == "Waiting on you · App secrets: OPENAI_API_KEY" and not waiting["live"]
    stages = [{"key": "discover", "state": "done"}, {"key": "ready", "state": "done"},
              {"key": "backlog", "state": "done"}, {"key": "first_work", "state": "now"}]
    pick = onboarding.now_of([], running=False, run_state="ready", stages=stages,
                             first={"label": "Fix 6 failing tests"})
    assert pick["agent"] == "chief_of_staff" and "Fix 6 failing tests" in pick["text"]
    assert onboarding.now_of([], running=False, run_state="not_started")["text"] == ""
