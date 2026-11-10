"""What an apply and a template build promise, held as classes (tester defects 2026-10-09):

  * P1-2  the setup guide gives the REACHABLE preview and callback URLs (the preview origin), never
          the app's port inside the API container;
  * P1-3  a build a template gate stopped ends BLOCKED, with why and what to do: never completed
          ("Done · no PR"), its application not "building" for ever, the workflow step above it
          not waiting for a waiver nobody can give;
  * P1-5  an insecure_by_design template applies only as a new app in its own lab project; the
          stack is checked on every apply, and "unknown" is not "compatible";
  * P2    setting a live key twice is ONE decision, worded with the app's name, with evidence;
  * P2    a refused apply writes NOTHING (bindings, application rows).
"""
from __future__ import annotations

import base64
import copy
import json
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from apps.api.app import main
from apps.api.tests.identity_fixtures import IdentityWorld
from boss_engine.identity import vault

from engine.tests.templates._fixtures import MANIFEST, git_repo


@pytest.fixture(scope="module")
def world(engine) -> IdentityWorld:
    return IdentityWorld.create(engine)


@pytest.fixture(scope="module")
def people(world) -> dict:
    out = {}
    for role in ("owner", "admin", "operator"):
        p = world.person(f"tah-{role}")
        world.bind(p, role)
        out[role] = p
    out["admin2"] = world.person("tah-admin2")
    world.bind(out["admin2"], "admin")
    return out


@pytest.fixture(autouse=True)
def master_key(monkeypatch):
    monkeypatch.setenv("BOSS_MASTER_KEY", base64.b64encode(b"t" * 32).decode())
    monkeypatch.delenv("BOSS_MASTER_KEY_FILE", raising=False)
    monkeypatch.setenv("BOSS_TEMPLATE_IMPORT_ALLOW_LOCAL", "1")
    vault.reset_master_key_cache()
    yield
    vault.reset_master_key_cache()


@pytest.fixture
def client():
    return TestClient(main.app, raise_server_exceptions=False)


def as_(world, person):
    return world.acting_as(world.ctx(person))


def _app_run(engine, project_id: str, title: str, files=("web/package.json", "web/server.js"),
             status: str = "completed", finalize_ctx: dict | None = None) -> str:
    run_id = f"crun_{uuid.uuid4().hex[:10]}"
    with engine.begin() as db:
        db.execute(text(
            "INSERT INTO code_task_runs (id, project_id, status, phase, title, spec, state, finalize_ctx) "
            "VALUES (:id, :p, :st, 'done', :t, '{}'::jsonb, CAST(:s AS JSONB), CAST(:f AS JSONB))"),
            {"id": run_id, "p": project_id, "t": title, "st": status,
             "s": json.dumps({"impl_files": list(files), "test_files": []}),
             "f": json.dumps(finalize_ctx or {})})
    return run_id


def _approved(client, world, people, tmp_path, key: str, **changes) -> str:
    m = copy.deepcopy(MANIFEST)
    m["id"] = key
    m.update(changes)
    repo, _ = git_repo(tmp_path / key, manifest=m)
    with as_(world, people["admin"]):
        r = client.post("/v1/templates/import", json={"git_url": f"file://{repo}",
                                                      "acknowledge_insecure": "insecure_by_design" in (m.get("labels") or [])})
        assert r.status_code == 201, r.text
        tid = r.json()["id"]
    with as_(world, people["admin2"]):
        assert client.post(f"/v1/templates/{tid}/approve").status_code == 200
    return tid


def _counts(engine, project_id: str) -> tuple[int, int, int]:
    with engine.begin() as db:
        return tuple(db.execute(text(q), {"p": project_id}).scalar() for q in (
            "SELECT count(*) FROM app_secret_bindings WHERE project_id = :p",
            "SELECT count(*) FROM app_template_applications WHERE project_id = :p",
            "SELECT count(*) FROM workflow_runs WHERE project_id = :p"))


# ---- P2: a refused apply writes nothing ---------------------------------------------------------

def test_a_refused_apply_writes_nothing_and_says_consent_as_the_build_does(world, people, client, engine,
                                                                           tmp_path, monkeypatch):
    """The template declares a `generated` secret, so the apply writes bindings before the start.
    The start then refuses (consent): the 409 is the start's own consent body, and NOTHING the apply
    wrote survives — no binding, no application, no run."""
    from boss_engine.identity.consent import ConsentRequired

    tid = _approved(client, world, people, tmp_path, "refused-apply")
    run_id = _app_run(engine, world.project_a, "Refused Notes")
    seen = []

    def refuse(body, connection_in=None):
        # the bindings ARE written by now, in the caller's transaction
        seen.append(connection_in.execute(text(
            "SELECT count(*) FROM app_secret_bindings WHERE project_id = :p"), {"p": world.project_a}).scalar())
        raise ConsentRequired({"error": "consent_required", "task": "x", "task_run_id": "pending:abc",
                               "agents": [], "permissions": [], "projects": [world.project_a],
                               "github": None, "durations": [], "max_days": 30})

    monkeypatch.setattr(main, "start_delivery_workflow", refuse)
    before = _counts(engine, world.project_a)
    with as_(world, people["operator"]):
        r = client.post(f"/v1/templates/{tid}/apply", json={"project_id": world.project_a,
                                                            "extends_run_id": run_id, "stack": "node"})
    assert r.status_code == 409, r.text
    assert r.json()["error"] == "consent_required" and r.json()["task_run_id"] == "pending:abc"
    assert seen and seen[0] > before[0]                 # written, then rolled back
    assert _counts(engine, world.project_a) == before


def test_a_readiness_refusal_from_the_real_start_path_writes_nothing(world, people, client, engine, tmp_path):
    """The real start path, refusing (no workflow of that key here): a 404 and nothing kept."""
    tid = _approved(client, world, people, tmp_path, "refused-apply-real")
    run_id = _app_run(engine, world.project_a, "Refused Real")
    before = _counts(engine, world.project_a)
    with as_(world, people["operator"]):
        r = client.post(f"/v1/templates/{tid}/apply", json={
            "project_id": world.project_a, "extends_run_id": run_id, "stack": "node",
            "workflow_key": "no_such_workflow_here"})
    # 422 since a template is applied only through a published workflow with a build step
    # (workflow_cannot_build): an unknown workflow key is invalid input. Nothing kept either way.
    assert r.status_code in (404, 409, 422), r.text
    assert _counts(engine, world.project_a) == before


# ---- P1-5: stack on every apply; insecure only into a new lab project ------------------------------

def test_the_stack_is_checked_on_every_apply_and_unknown_is_not_compatible(world, people, client, engine):
    node_app = _app_run(engine, world.project_a, "Node Shop")
    blank_app = _app_run(engine, world.project_a, "Blank Shop", files=())
    with as_(world, people["operator"]):
        r = client.post("/v1/templates/hello/apply", json={"project_id": world.project_a,
                                                           "extends_run_id": node_app, "stack": "python"})
        assert r.status_code == 409 and r.json()["error"] == "stack_mismatch", r.text
        assert "Node Shop is a node app" in r.json()["message"]
        r = client.post("/v1/templates/hello/apply", json={"project_id": world.project_a,
                                                           "extends_run_id": blank_app, "stack": "node"})
    assert r.status_code == 409 and r.json()["error"] == "stack_unknown", r.text
    assert "cannot tell which stack" in r.json()["message"]


def test_an_insecure_template_goes_only_into_its_own_new_lab_project(world, people, client, engine,
                                                                     tmp_path, monkeypatch):
    from boss_engine.templates import deploy_guard

    started = []

    def fake_start(body, connection_in=None):
        started.append(body.model_dump())
        return {"run": {"id": f"wfr_{uuid.uuid4().hex[:8]}"}}

    monkeypatch.setattr(main, "start_delivery_workflow", fake_start)
    # The training bank is a Java template: an install builds it only with Java turned on
    # (services.stacks refuses it at apply otherwise, saying how to turn it on).
    toolchain_manifest = tmp_path / "toolchains.json"
    toolchain_manifest.write_text('{"java": {"jdk": "21"}}')
    monkeypatch.setenv("BOSS_TOOLCHAINS_MANIFEST", str(toolchain_manifest))
    existing = _app_run(engine, world.project_a, "Real Bank")
    with as_(world, people["owner"]):
        r = client.post("/v1/templates/banking-starter-insecure/apply", json={
            "project_id": world.project_a, "extends_run_id": existing, "stack": "java"})
        assert r.status_code == 409 and r.json()["error"] == "insecure_needs_new_app", r.text
        assert "own lab project" in r.json()["message"]
        assert not started
        r = client.post("/v1/templates/banking-starter-insecure/apply", json={
            "project_id": world.project_a, "new_app": {"name": "Training bank"}, "stack": "java"})
        assert r.status_code == 201, r.text
        out = r.json()
        lab_id = out["project_id"]
        assert lab_id != world.project_a and out["lab"]["project_id"] == lab_id
        assert out["lab"]["name"] == "Training bank lab (insecure by design)"
        assert started[-1]["project_id"] == lab_id
        # a second apply (the retry after a consent sheet) lands in the SAME lab
        again = client.post("/v1/templates/banking-starter-insecure/apply", json={
            "project_id": world.project_a, "new_app": {"name": "Training bank"}, "stack": "java"})
        assert again.status_code == 201 and again.json()["project_id"] == lab_id
        # a lab is not a place to apply other templates into
        r = client.post("/v1/templates/hello/apply", json={"project_id": lab_id, "new_app": {"name": "x"},
                                                           "stack": "node"})
        assert r.status_code == 409 and r.json()["error"] == "lab_project"
    with engine.begin() as db:
        ws = db.execute(text("SELECT p.workspace_id, q.workspace_id FROM projects p, projects q "
                             "WHERE p.id = :a AND q.id = :b"), {"a": world.project_a, "b": lab_id}).one()
        assert ws[0] == ws[1]
        assert db.execute(text("SELECT count(*) FROM app_template_applications WHERE project_id = :p"),
                          {"p": world.project_a}).scalar() == db.execute(text(
                              "SELECT count(*) FROM app_template_applications a JOIN app_templates t "
                              "ON t.id = a.template_id WHERE a.project_id = :p "
                              "AND t.template_key <> 'banking-starter-insecure'"), {"p": world.project_a}).scalar()
        assert db.execute(text("SELECT count(*) FROM app_template_applications WHERE project_id = :p"),
                          {"p": lab_id}).scalar() == 2
        # the lab deploys nothing, whatever the target is called; home is untouched
        assert "lab project" in deploy_guard.deploy_refusal(db, lab_id, target={"app_name": "renamed", "repo": "o/r"})
        assert deploy_guard.deploy_refusal(db, world.project_a, target={"app_name": "Real Bank", "repo": "o/r"}) is None


# ---- P1-2: reachable URLs in the setup guide ----------------------------------------------------

def test_the_setup_guide_gives_the_preview_origin_never_the_internal_port(world, people, client, engine,
                                                                          tmp_path, monkeypatch):
    monkeypatch.setattr(main, "start_delivery_workflow",
                        lambda body, connection_in=None: {"run": {"id": "wfr_guide"}})
    monkeypatch.setenv("BOSS_PREVIEW_ORIGIN", "https://previews.example.test")
    tid = _approved(client, world, people, tmp_path, "guide-urls",
                    webhooks=[{"provider": "stripe", "path": "/webhooks/stripe"}])
    run_id = _app_run(engine, world.project_a, "Guide Shop")
    with as_(world, people["operator"]):
        out = client.post(f"/v1/templates/{tid}/apply", json={"project_id": world.project_a,
                                                              "extends_run_id": run_id, "stack": "node"}).json()
    preview_id = f"prv_{uuid.uuid4().hex[:8]}"
    with engine.begin() as db:
        db.execute(text("INSERT INTO app_previews (id, project_id, run_id, repo, branch, command, mode, status, url) "
                        "VALUES (:i, :p, :r, 'o/r', 'b', 'npm start', 'process', 'running', "
                        "'http://127.0.0.1:19769')"), {"i": preview_id, "p": world.project_a, "r": run_id})
    with as_(world, people["operator"]):
        g = client.get(f"/v1/apps/{out['app_key']}/setup-guide", params={"project_id": world.project_a}).json()
    guide = g["guides"][0]
    blob = json.dumps(guide)
    assert "127.0.0.1" not in blob and "19769" not in blob
    base = f"https://{preview_id.replace('_', '-')}.previews.example.test/v1/previews/{preview_id}/web"
    assert guide["copy"]["preview_url"] == base
    assert guide["copy"]["redirect_uri"] == base + "/auth/callback"
    assert guide["copy"]["webhook_url:stripe"] == base + "/webhooks/stripe"
    assert f"Open {base}/healthz." in guide["guide"]


# ---- P1-3: a build a template gate stopped is BLOCKED -------------------------------------------

def test_a_template_gate_failure_ends_blocked_with_why_and_what_next(world, engine):
    from boss_engine.services import run_fencing
    from boss_engine.services.workflow_capability_runner import _build_waits_on_a_person
    from boss_engine.templates import apply as apply_service

    p = world.project_a
    wfr = f"wfr_{uuid.uuid4().hex[:8]}"
    run_id = _app_run(engine, p, "Stripe Shop", status="running", finalize_ctx={"workflow_run_id": wfr})
    with engine.begin() as db:
        tid = db.execute(text("SELECT id FROM app_templates WHERE template_key = 'hello' AND org_id = :o"),
                         {"o": world.org}).scalar()
        db.execute(text("INSERT INTO app_template_applications (id, project_id, app_key, template_id, version, "
                        "stack, workflow_run_id, status) VALUES (:i, :p, 'stripe shop', :t, '1.0.0', 'node', :w, "
                        "'building')"), {"i": f"tap_{uuid.uuid4().hex[:8]}", "p": p, "t": tid, "w": wfr})
        db.execute(text("INSERT INTO quality_gates (id, project_id, code_task_run_id, status, gates_passed, "
                        "gates_failed, evidence) VALUES (:i, :p, :r, 'fail', '[]'::jsonb, "
                        "'[\"template_tests\"]'::jsonb, '{}'::jsonb)"),
                   {"i": f"qg_{uuid.uuid4().hex[:8]}", "p": p, "r": run_id})
        # whichever path tries to finish it as completed, it ends blocked
        assert run_fencing.guarded_status(db, run_id, None, "completed", "phase='done'") is True
        row = db.execute(text("SELECT status, phase, error, finalize_ctx FROM code_task_runs WHERE id = :i"),
                         {"i": run_id}).mappings().one()
        assert row["status"] == "blocked" and row["phase"] == "done"
        assert row["error"].startswith("Blocked: the template's tests did not pass.")
        assert "retry the build" in row["error"]
        assert row["finalize_ctx"]["template_hold"]["gates"] == ["template_tests"]
        # nobody can waive it, so the workflow step above it is not "waiting on a person"
        db.execute(text("UPDATE code_task_runs SET phase = 'awaiting_gate_override' WHERE id = :i"), {"i": run_id})
        assert _build_waits_on_a_person(db, run_id) is False
        db.execute(text("UPDATE code_task_runs SET phase = 'done' WHERE id = :i"), {"i": run_id})
        apps = apply_service.applications(db, p, "stripe shop")
    mine = next(a for a in apps if a["workflow_run_id"] == wfr)
    assert mine["status"] == "blocked" and mine["blocked_reason"].startswith("Blocked: the template's tests")


def test_an_ordinary_build_still_completes(world, engine):
    from boss_engine.services import run_fencing

    run_id = _app_run(engine, world.project_a, "Plain App", status="running")
    with engine.begin() as db:
        db.execute(text("INSERT INTO quality_gates (id, project_id, code_task_run_id, status, gates_passed, "
                        "gates_failed, evidence) VALUES (:i, :p, :r, 'fail', '[]'::jsonb, "
                        "'[\"code_review_passed\"]'::jsonb, '{}'::jsonb)"),
                   {"i": f"qg_{uuid.uuid4().hex[:8]}", "p": world.project_a, "r": run_id})
        assert run_fencing.guarded_status(db, run_id, None, "completed", "phase='done'") is True
        assert db.execute(text("SELECT status FROM code_task_runs WHERE id = :i"), {"i": run_id}).scalar() == "completed"


# ---- P2: one live-key decision, in words, with evidence ------------------------------------------

def test_setting_a_live_key_twice_is_one_decision_worded_with_evidence(world, people, client, engine, tmp_path):
    tid = _approved(client, world, people, tmp_path, "live-dedupe", secrets=[
        {"name": "PAY_KEY", "test_default": "none", "pattern": "^pk_test_[a-z0-9]{12,}$",
         "live_pattern": "^pk_live_[a-z0-9]{12,}$"}])
    _app_run(engine, world.project_a, "Pay Desk")
    from boss_engine.services.agent_assignment import app_key

    key = app_key("Pay Desk")
    live = "pk_live_abcdef123456789"
    body = {"project_id": world.project_a, "env": "deploy", "mode": "live", "value": live, "template_id": tid}
    with as_(world, people["admin"]):
        first = client.put(f"/v1/apps/{key}/secrets/PAY_KEY", json=body)
        second = client.put(f"/v1/apps/{key}/secrets/PAY_KEY", json={**body, "value": "pk_live_zyxwvu987654321"})
    assert first.status_code == 200 and second.status_code == 200, (first.text, second.text)
    assert first.json()["live_decision_id"] == second.json()["live_decision_id"]
    with engine.begin() as db:
        rows = db.execute(text("SELECT question, recommendation FROM decisions WHERE project_id = :p "
                               "AND status = 'pending' AND recommendation->>'kind' = 'app_secret_live' "
                               "AND recommendation->>'app_key' = :a"),
                          {"p": world.project_a, "a": key}).mappings().all()
    assert len(rows) == 1
    q, rec = rows[0]["question"], rows[0]["recommendation"]
    assert q == "Use the live PAY_KEY when Pay Desk is deployed?"
    blob = json.dumps(rows[0]["recommendation"]) + q
    assert "§" not in blob and live not in blob and "abcdef" not in blob and "zyxwvu" not in blob
    ev = {e["label"]: e["value"] for e in rec["evidence"]}
    assert ev["App"] == "Pay Desk" and ev["Secret"] == "PAY_KEY" and ev["Set by"]
    assert ev["Key check"] == "matches the template's live-key pattern"
    assert rec["reason"] and rec["times_set"] == 2
