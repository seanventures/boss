"""POST /v1/templates/compose: several connectors in ONE build (BOSSAPP-526), on the real app.

Login + Email on a new app starts ONE governed build whose `input.template` is a hidden composite
with the merged manifest, records one application row, one receipt (unique dedupe key) and one
event per connector, and every row follows the shared build (building -> pr_open -> applied, or
failed). Stripe + PayPal are refused with every conflict named and nothing written; the
single-connector apply path is untouched.
"""
from __future__ import annotations

import base64
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from apps.api.app import main
from apps.api.tests.identity_fixtures import IdentityWorld
from boss_engine.identity import vault
from boss_engine.templates import apply as apply_service
from boss_engine.templates import compose
from boss_engine.templates.gate import probe_paths
from boss_engine.templates.manifest import validate_manifest


@pytest.fixture(scope="module")
def world(engine) -> IdentityWorld:
    return IdentityWorld.create(engine)


@pytest.fixture(scope="module")
def people(world) -> dict:
    out = {}
    for role in ("operator", "viewer"):
        p = world.person(f"cmp-{role}")
        world.bind(p, role)
        out[role] = p
    return out


@pytest.fixture(autouse=True)
def master_key(monkeypatch):
    monkeypatch.setenv("BOSS_MASTER_KEY", base64.b64encode(b"s" * 32).decode())
    monkeypatch.delenv("BOSS_MASTER_KEY_FILE", raising=False)
    vault.reset_master_key_cache()
    yield
    vault.reset_master_key_cache()


@pytest.fixture
def client():
    return TestClient(main.app, raise_server_exceptions=False)


@pytest.fixture
def started(monkeypatch):
    calls: list[dict] = []

    def fake_start(body, connection_in=None):
        calls.append(body.model_dump())
        return {"run": {"id": f"wfr_{uuid.uuid4().hex[:10]}"}}

    monkeypatch.setattr(main, "start_delivery_workflow", fake_start)
    return calls


def as_(world, person):
    return world.acting_as(world.ctx(person))


def _body(world, name, *templates, **over):
    return {"project_id": world.project_a, "new_app": {"name": name}, "stack": "node",
            "templates": list(templates), "accept_egress": True, **over}


def _count(engine, sql, **params) -> int:
    with engine.connect() as db:
        return int(db.execute(text(sql), params).scalar() or 0)


def test_compose_needs_workflow_run_start(world, people, client, started):
    with as_(world, people["viewer"]):
        r = client.post("/v1/templates/compose", json=_body(world, "Nope", "login-oauth", "transactional-email"))
    assert r.status_code == 403 and not started


def test_login_and_email_start_one_build_with_one_record_per_connector(world, people, client, engine, started):
    name = f"Shop {uuid.uuid4().hex[:6]}"
    with as_(world, people["operator"]):
        r = client.post("/v1/templates/compose",
                        json=_body(world, name, "transactional-email", "login-oauth", note="one login"))
    assert r.status_code == 201, r.text
    out = r.json()
    assert len(started) == 1                                     # ONE build
    assert out["order"] == ["transactional-email", "login-oauth"]
    inp = started[0]["input"]
    tpl = inp["template"]
    assert tpl["key"].startswith("compose-") and tpl["stack"] == "node"
    assert [c["key"] for c in tpl["connectors"]] == out["order"]
    assert set(tpl["egress"]) == {"api.resend.com", "smtp.resend.com", "accounts.google.com",
                                  "oauth2.googleapis.com", "openidconnect.googleapis.com",
                                  "www.googleapis.com", "github.com", "api.github.com"}
    assert "one login" in inp["directive"] and "COMPOSE, NEVER OVERWRITE" in inp["directive"]
    assert any("GET /auth/providers" in c for c in inp["acceptance_criteria"])
    assert any("GET /api/email/status" in c for c in inp["acceptance_criteria"])

    # The composite the build's gates read: hidden, merged, every connector's routes probed.
    with engine.connect() as db:
        row = db.execute(text("SELECT status, digest, manifest FROM app_templates WHERE id = :i"),
                         {"i": tpl["id"]}).mappings().one()
        paths = [p for (p,) in db.execute(text("SELECT path FROM app_template_files WHERE template_id = :i"),
                                          {"i": tpl["id"]}).all()]
    assert row["status"] == "hidden" and row["digest"] == tpl["digest"]
    merged = validate_manifest(dict(row["manifest"]))
    assert {"/auth/providers", "/api/email/status"} <= set(probe_paths(merged))
    assert "stacks/node/login-oauth/auth/routes.mjs" in paths
    # Never in the catalogue.
    with as_(world, people["operator"]):
        cards = client.get("/v1/templates").json()["templates"]
    assert not any(c["template_key"].startswith("compose-") for c in cards)

    # One application row, one receipt and one event per connector.
    wfr = out["workflow_run_id"]
    assert [c["template_key"] for c in out["connectors"]] == out["order"]
    keys = {c["dedupe_key"] for c in out["connectors"]}
    assert keys == {compose.receipt_dedupe_key(wfr, k) for k in out["order"]}
    assert _count(engine, "SELECT count(*) FROM receipts WHERE effect_kind = :k AND dedupe_key LIKE :d",
                  k=compose.RECEIPT_KIND, d=f"template.compose:{wfr}:%") == 2
    assert _count(engine, "SELECT count(*) FROM receipts WHERE dedupe_key LIKE :d AND status = 'succeeded'",
                  d=f"template.compose:{wfr}:%") == 2
    assert _count(engine, "SELECT count(*) FROM events WHERE event_type = :t AND data->>'workflow_run_id' = :w",
                  t=compose.EVENT_TYPE, w=wfr) == 2
    with as_(world, people["operator"]):
        applied = client.get(f"/v1/apps/{out['app_key']}/templates",
                             params={"project_id": world.project_a}).json()["templates"]
    assert sorted(a["template_key"] for a in applied) == ["login-oauth", "transactional-email"]
    assert {a["status"] for a in applied} == {"building"}
    assert {a["workflow_run_id"] for a in applied} == {wfr}

    # A replayed record of the same build writes nothing twice.
    with engine.begin() as db:
        plan = {"connectors": tpl["connectors"], "app": {"app_key": out["app_key"]}, "stack": "node",
                "framework": None, "run_input": {"template": tpl}}
        again = compose.record(db, plan, org_id=world.org, project_id=world.project_a,
                               workflow_run_id=wfr, actor_id=None)
    assert not any(c["created"] for c in again["connectors"])
    assert [c["application_id"] for c in again["connectors"]] == [c["application_id"] for c in out["connectors"]]
    assert _count(engine, "SELECT count(*) FROM app_template_applications WHERE workflow_run_id = :w", w=wfr) == 2

    # Each connector's own setup guide.
    with as_(world, people["operator"]):
        guides = client.get(f"/v1/apps/{out['app_key']}/setup-guide",
                            params={"project_id": world.project_a}).json()["guides"]
    assert sorted(g["template_key"] for g in guides) == ["login-oauth", "transactional-email"]


def _transition(engine, project_id, app_key, wfr, *, status, pr=None, decision=None) -> set[str]:
    with engine.begin() as db:
        run_id = db.execute(text("SELECT id FROM code_task_runs WHERE finalize_ctx->>'workflow_run_id' = :w"),
                            {"w": wfr}).scalar()
        if run_id is None:
            run_id = f"crun_{uuid.uuid4().hex[:10]}"
            db.execute(text(
                "INSERT INTO code_task_runs (id, project_id, status, phase, title, spec, state, finalize_ctx) "
                "VALUES (:id, :p, :s, 'build', 't', '{}'::jsonb, '{}'::jsonb, CAST(:f AS JSONB))"),
                {"id": run_id, "p": project_id, "s": status, "f": f'{{"workflow_run_id": "{wfr}"}}'})
        db.execute(text("UPDATE code_task_runs SET status = :s, pull_request_url = :u WHERE id = :id"),
                   {"s": status, "u": pr, "id": run_id})
        if decision:
            did = f"dec_{uuid.uuid4().hex[:10]}"
            db.execute(text("INSERT INTO decisions (id, project_id, question, status) "
                            "VALUES (:id, :p, 'merge?', :s)"), {"id": did, "p": project_id, "s": decision})
            db.execute(text("UPDATE code_task_runs SET decision_id = :d WHERE id = :id"), {"d": did, "id": run_id})
        apply_service.refresh_statuses(db, project_id, app_key)
        return {r[0] for r in db.execute(text(
            "SELECT status FROM app_template_applications WHERE workflow_run_id = :w"), {"w": wfr}).all()}


def test_every_connector_follows_the_shared_build(world, people, client, engine, started):
    with as_(world, people["operator"]):
        r = client.post("/v1/templates/compose",
                        json=_body(world, f"Club {uuid.uuid4().hex[:6]}", "login-oauth", "transactional-email"))
    assert r.status_code == 201, r.text
    out = r.json()
    p, a, w = world.project_a, out["app_key"], out["workflow_run_id"]
    assert _transition(engine, p, a, w, status="running") == {"building"}
    assert _transition(engine, p, a, w, status="running", pr="https://example.com/pr/1") == {"pr_open"}
    assert _transition(engine, p, a, w, status="completed", pr="https://example.com/pr/1",
                       decision="executed") == {"applied"}


def test_a_failed_build_fails_every_connector(world, people, client, engine, started):
    with as_(world, people["operator"]):
        r = client.post("/v1/templates/compose",
                        json=_body(world, f"Gym {uuid.uuid4().hex[:6]}", "login-oauth", "transactional-email"))
    assert r.status_code == 201, r.text
    out = r.json()
    assert _transition(engine, world.project_a, out["app_key"], out["workflow_run_id"],
                       status="failed") == {"failed"}


def test_the_merged_secrets_a_person_must_set_are_asked_for_once(world, people, client, started):
    """Login + Stripe: Stripe's test keys have no default, so ONE refusal names them, with the
    app's key to set them under; nothing starts."""
    with as_(world, people["operator"]):
        r = client.post("/v1/templates/compose",
                        json=_body(world, f"Gym {uuid.uuid4().hex[:6]}", "login-oauth", "stripe-checkout"))
    assert r.status_code == 409 and r.json()["error"] == "secrets_needed", r.text
    assert set(r.json()["secrets"]) == {"STRIPE_SECRET_KEY", "STRIPE_PUBLISHABLE_KEY"}
    assert r.json()["app_key"] and not started


def test_stripe_and_paypal_are_refused_with_every_conflict_and_nothing_written(world, people, client, engine, started):
    before = _count(engine, "SELECT count(*) FROM app_template_applications")
    receipts = _count(engine, "SELECT count(*) FROM receipts WHERE effect_kind = :k", k=compose.RECEIPT_KIND)
    with as_(world, people["operator"]):
        r = client.post("/v1/templates/compose",
                        json=_body(world, f"Pay {uuid.uuid4().hex[:6]}", "stripe-checkout", "paypal-checkout"))
    assert r.status_code == 409, r.text
    body = r.json()
    assert body["error"] == "connector_conflict"
    whats = {c["what"] for c in body["conflicts"]}
    assert "GET /api/orders/:id" in whats and "stacks/node/payments/store.js" in whats
    assert "GET /api/orders/:id" in body["message"]
    assert not started
    assert _count(engine, "SELECT count(*) FROM app_template_applications") == before
    assert _count(engine, "SELECT count(*) FROM receipts WHERE effect_kind = :k", k=compose.RECEIPT_KIND) == receipts


@pytest.mark.parametrize("templates,error", [
    (["login-oauth"], None),                                  # 422 from the body schema
    (["login-oauth", "login-oauth"], "compose_duplicate"),
    (["login-oauth", "catalog-cart"], "compose_connectors_only"),
    (["login-oauth", "transactional-email"], "stack_not_supported"),
])
def test_compose_refuses_what_it_must(world, people, client, started, templates, error):
    body = _body(world, "Refused", *templates)
    if error == "stack_not_supported":
        body["stack"] = "python"                              # the email connector is Node only
    with as_(world, people["operator"]):
        r = client.post("/v1/templates/compose", json=body)
    assert r.status_code in (409, 422), r.text
    if error:
        assert r.json()["error"] == error
    assert not started


def test_compose_needs_the_merged_egress_accepted(world, people, client, started):
    body = _body(world, "Egress", "login-oauth", "transactional-email")
    body["accept_egress"] = ["api.resend.com", "smtp.resend.com"]       # only the email hosts
    with as_(world, people["operator"]):
        r = client.post("/v1/templates/compose", json=body)
    assert r.status_code == 409 and r.json()["error"] == "egress_not_accepted"
    assert "accounts.google.com" in r.json()["egress"] and not started


def _app_run(engine, project_id: str, title: str) -> str:
    """A finished Node build of an app, so a composition has an existing app to build onto."""
    run_id = f"crun_{uuid.uuid4().hex[:10]}"
    with engine.begin() as db:
        db.execute(text(
            "INSERT INTO code_task_runs (id, project_id, status, phase, title, spec, state, finalize_ctx) "
            "VALUES (:id, :p, 'completed', 'done', :t, '{}'::jsonb, CAST(:s AS JSONB), '{}'::jsonb)"),
            {"id": run_id, "p": project_id, "t": title,
             "s": '{"impl_files": ["web/package.json", "web/server.js"], "test_files": []}'})
    return run_id


def test_an_existing_app_composes_onto_its_build_and_a_route_it_already_serves_conflicts(
        world, people, client, engine, started):
    run_id = _app_run(engine, world.project_a, f"Shop {uuid.uuid4().hex[:6]}")
    body = {"project_id": world.project_a, "extends_run_id": run_id, "stack": "node",
            "templates": ["login-oauth", "transactional-email"], "accept_egress": True}
    with as_(world, people["operator"]):
        r = client.post("/v1/templates/compose", json=body)
    assert r.status_code == 201, r.text
    out = r.json()
    assert started[-1]["input"]["extends_run_id"] == run_id
    # Stripe already on the app (applied by hand here): PayPal's GET /api/orders/:id is taken.
    with engine.begin() as db:
        tid = db.execute(text("SELECT id FROM app_templates WHERE org_id = :o AND template_key = 'stripe-checkout'"),
                         {"o": world.org}).scalar()
        db.execute(text("UPDATE app_template_applications SET status = 'applied' WHERE workflow_run_id = :w"),
                   {"w": out["workflow_run_id"]})
        db.execute(text(
            "INSERT INTO app_template_applications (id, project_id, app_key, template_id, version, stack, status) "
            "VALUES (:id, :p, :a, :t, '1.0.0', 'node', 'applied')"),
            {"id": f"tap_{uuid.uuid4().hex[:10]}", "p": world.project_a, "a": out["app_key"], "t": tid})
    with as_(world, people["operator"]):
        r = client.post("/v1/templates/compose",
                        json={**body, "templates": ["paypal-checkout", "transactional-email"]})
    assert r.status_code == 409 and r.json()["error"] == "connector_conflict", r.text
    whats = " | ".join(c["what"] for c in r.json()["conflicts"])
    assert "already served by stripe-checkout" in whats
    # The email connector already applied is not a conflict with itself.
    assert "/api/email/" not in whats
