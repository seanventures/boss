"""Cross-app links on the real API under enforce (APP_TEMPLATES_AND_SECURITY_LOOP.md §5.6, §5.9 API).

Per role; same project only (D7); one port per link (D8); the audited, step-up reveal (D9);
test users generated, vaulted and rotated per preview of the provider; revocation kills the
link's passes at once; and no test-user password or link pass ever reaches a log, a list or an
audit row.
"""
from __future__ import annotations

import base64
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from apps.api.app import main
from apps.api.tests.identity_fixtures import IdentityWorld
from boss_engine.app_links import service as links
from boss_engine.identity import vault


@pytest.fixture(scope="module", autouse=True)
def master_key():
    import os

    saved = os.environ.get("BOSS_MASTER_KEY")
    os.environ["BOSS_MASTER_KEY"] = base64.b64encode(b"s5-links-test-only-master-key-32").decode()
    vault.reset_master_key_cache()
    yield
    if saved is None:
        os.environ.pop("BOSS_MASTER_KEY", None)
    else:
        os.environ["BOSS_MASTER_KEY"] = saved
    vault.reset_master_key_cache()


def _run(engine, project: str, title: str) -> str:
    return f"ctr_{uuid.uuid4().hex[:12]}"


@pytest.fixture(autouse=True)
def apps_exist(world, engine):
    """The four apps' builds. Per test: the suite's clean_tables empties code_task_runs."""
    with engine.begin() as db:
        for rid, project, title in ((world.bank, world.project_a, "Banking"),
                                    (world.attacker, world.project_a, "Bank attacker"),
                                    (world.third, world.project_a, "Ledger viewer"),
                                    (world.elsewhere, world.project_b, "Other banking")):
            db.execute(text("INSERT INTO code_task_runs (id, project_id, title, status) "
                            "VALUES (:i, :p, :t, 'completed') ON CONFLICT (id) DO NOTHING"),
                       {"i": rid, "p": project, "t": title})


@pytest.fixture(scope="module")
def world(engine) -> IdentityWorld:
    w = IdentityWorld.create(engine)
    w.bank = _run(engine, w.project_a, "Banking")
    w.attacker = _run(engine, w.project_a, "Bank attacker")
    w.third = _run(engine, w.project_a, "Ledger viewer")
    w.elsewhere = _run(engine, w.project_b, "Other banking")
    w.people = {}
    for role in ("owner", "admin", "operator", "viewer", "auditor"):
        p = w.person(f"links-{role}")
        w.bind(p, role, project=None if role in ("owner", "admin", "auditor") else w.project_a)
        w.people[role] = p
    outsider = w.person("links-outsider")
    w.bind(outsider, "operator", project=w.project_b)
    w.people["outsider"] = outsider
    return w


@pytest.fixture
def client():
    return TestClient(main.app, raise_server_exceptions=False)


def _create(client, world, who="operator", *, app=None, mfa=True, **body):
    payload = {"project_id": world.project_a, "provider_app_key": "Banking", "alias": "BANK",
               "env": "preview", "credentials": "test_users", **body}
    with world.acting_as(world.ctx(world.people[who], mfa=mfa)):
        return client.post(f"/v1/apps/{app or world.attacker}/links", json=payload)


def _revoke(client, world, link_id, who="operator"):
    with world.acting_as(world.ctx(world.people[who])):
        return client.delete(f"/v1/apps/{world.attacker}/links/{link_id}", params={"project_id": world.project_a})


@pytest.mark.parametrize("role, status", [("owner", 201), ("admin", 201), ("operator", 201),
                                          ("viewer", 403), ("auditor", 403), ("outsider", 403)])
def test_who_may_link_apps(client, world, role, status):
    r = _create(client, world, role, alias=f"BANK_{role.upper()}")
    assert r.status_code == status, r.text
    if status == 201:
        assert _revoke(client, world, r.json()["id"], role).status_code == 200


@pytest.mark.parametrize("role, status", [("viewer", 200), ("auditor", 200), ("outsider", 403)])
def test_who_may_see_links(client, world, role, status):
    with world.acting_as(world.ctx(world.people[role])):
        r = client.get(f"/v1/apps/{world.attacker}/links", params={"project_id": world.project_a})
    assert r.status_code == status


def test_a_viewer_cannot_revoke(client, world, engine):
    link = _create(client, world, alias="BANK_RV").json()
    with world.acting_as(world.ctx(world.people["viewer"])):
        r = client.delete(f"/v1/apps/{world.attacker}/links/{link['id']}", params={"project_id": world.project_a})
    # The guard refuses an object-scoped write as not_found (no existence leak): refused either way.
    assert r.status_code in (403, 404)
    with engine.begin() as db:
        assert links._row(db, link["id"])["status"] == "active"
    assert _revoke(client, world, link["id"]).status_code == 200


@pytest.mark.parametrize("body, status, error", [
    ({"provider_app_key": "Other banking"}, 422, "provider_not_in_project"),     # D7: another project's app
    ({"provider_app_key": "no such app"}, 422, "provider_not_in_project"),
    ({"provider_app_key": "Bank attacker"}, 422, "self_link"),
    ({"alias": "bank"}, 422, "bad_alias"),
    ({"alias": "B"}, 422, "bad_alias"),
    ({"alias": "BANK-1"}, 422, "bad_alias"),
    ({"env": "prod"}, 422, "bad_env"),
    ({"credentials": "client"}, 422, "client_credentials_not_available"),
])
def test_bad_links_are_refused(client, world, body, status, error):
    r = _create(client, world, **body)
    assert (r.status_code, r.json().get("error")) == (status, error)


def test_cross_project_by_any_door_is_refused(client, world):
    # The consumer named in the path must be an app of the body's project.
    r = _create(client, world, app=world.elsewhere, alias="XPROJ")
    assert (r.status_code, r.json().get("error")) == (404, "unknown_app")
    # A link of project A cannot be revoked by naming project B.
    link = _create(client, world, alias="BANK_XP").json()
    with world.acting_as(world.ctx(world.people["owner"])):
        r = client.delete(f"/v1/apps/{world.attacker}/links/{link['id']}", params={"project_id": world.project_b})
    assert r.status_code in (403, 404)
    assert _revoke(client, world, link["id"]).status_code == 200


def test_one_port_per_link_and_an_alias_is_free_again_after_revoke(client, world, engine):
    a = _create(client, world, alias="PORTS_A").json()
    b = _create(client, world, alias="PORTS_B", credentials="none").json()
    assert a["gateway_port"] != b["gateway_port"]
    assert a["url"] == f"http://egress:{a['gateway_port']}"
    assert _create(client, world, alias="PORTS_A").json()["error"] == "alias_taken"
    assert _revoke(client, world, a["id"]).status_code == 200
    again = _create(client, world, alias="PORTS_A")
    assert again.status_code == 201
    for link in (b, again.json()):
        _revoke(client, world, link["id"])


def test_revocation_kills_every_pass_and_is_audited(client, world, engine):
    link = _create(client, world, alias="REVOKE").json()
    with engine.begin() as db:
        row = links._row(db, link["id"])
        raw = links.mint_pass(db, row, consumer_kind="run", consumer_id=world.attacker,
                              expires_at=datetime.now(timezone.utc) + timedelta(hours=1))
    with engine.begin() as db:
        assert links.resolve_pass(db, raw)["link_id"] == link["id"]
    assert _revoke(client, world, link["id"]).status_code == 200
    with engine.begin() as db:
        assert links.resolve_pass(db, raw) is None
        assert links.link_on_port(db, link["gateway_port"]) is None
        events = [r[0] for r in db.execute(text(
            "SELECT event_type FROM identity_security_events WHERE target_id = :i ORDER BY seq"),
            {"i": link["id"]}).all()]
    assert events == ["app_link.created", "app_link.revoked"]


def test_test_users_flow_to_both_apps_rotate_per_preview_and_never_leak(client, world, engine, caplog):
    caplog.set_level(logging.DEBUG)
    created = _create(client, world, alias="LEAK")
    link = created.json()
    with engine.begin() as db:
        provider = links.workload_environment(db, project_id=world.project_a, run_id=world.bank, env="preview",
                                              consumer_kind="preview", consumer_id="prev_a", rotate=True)
        consumer = links.workload_environment(db, project_id=world.project_a, run_id=world.attacker,
                                              env="preview", consumer_kind="run", consumer_id=world.attacker)
        item_1 = db.execute(text("SELECT vault_item_id FROM app_test_users WHERE project_id = :p AND env = 'preview'"),
                            {"p": world.project_a}).scalar()
    users = json.loads(provider["BOSS_TEST_USERS"])
    assert [u["role"] for u in users] == ["user", "user", "admin"]
    assert json.loads(consumer["BOSS_LINK_LEAK_USERS"]) == users            # the same users, both sides
    assert consumer["BOSS_LINK_LEAK_URL"] == link["url"]
    assert consumer["BOSS_LINK_LEAK_TOKEN"].startswith("boss_lnk_")
    assert "BOSS_TEST_USERS" not in consumer                                 # B is not a provider
    # A third app, not linked, gets nothing.
    with engine.begin() as db:
        assert links.workload_environment(db, project_id=world.project_a, run_id=world.third, env="preview",
                                          consumer_kind="run", consumer_id=world.third) == {}
    # A new preview of the provider rotates them; the old vault item is gone.
    with engine.begin() as db:
        rotated = json.loads(links.provider_environment(db, project_id=world.project_a, app_key="banking",
                                                        env="preview", rotate=True)["BOSS_TEST_USERS"])
        assert db.execute(text("SELECT 1 FROM identity_vault_items WHERE id = :v"), {"v": item_1}).first() is None
    assert {u["password"] for u in rotated}.isdisjoint({u["password"] for u in users})
    # Presence only, on every surface but the reveal.
    with world.acting_as(world.ctx(world.people["viewer"])):
        listed = client.get(f"/v1/apps/{world.attacker}/links", params={"project_id": world.project_a}).text
        presence = client.get(f"/v1/apps/{world.bank}/test-users", params={"project_id": world.project_a})
    assert presence.json()["envs"]["preview"]["count"] == 3
    # D9: the reveal needs app_secret:write WITH step-up, and is audited.
    with world.acting_as(world.ctx(world.people["viewer"])):
        assert client.post(f"/v1/apps/{world.bank}/test-users/reveal",
                           json={"project_id": world.project_a}).status_code == 403
    with world.acting_as(world.ctx(world.people["operator"], mfa=False)):
        assert client.post(f"/v1/apps/{world.bank}/test-users/reveal",
                           json={"project_id": world.project_a}).status_code in (401, 403)
    with world.acting_as(world.ctx(world.people["operator"])):
        revealed = client.post(f"/v1/apps/{world.bank}/test-users/reveal", json={"project_id": world.project_a})
    assert revealed.status_code == 200 and revealed.json()["users"] == rotated
    assert revealed.headers["cache-control"] == "no-store"
    with engine.begin() as db:
        audit_rows = json.dumps([dict(r) for r in db.execute(text(
            "SELECT event_type, details FROM identity_security_events WHERE scope = :s"),
            {"s": f"project:{world.project_a}"}).mappings().all()], default=str)
        stored = db.execute(text("SELECT secret_enc FROM identity_vault_items WHERE provider = :p"),
                            {"p": links.TEST_USERS_PROVIDER}).all()
    assert "app_link.test_users_revealed" in audit_rows
    secrets_ = [u["password"] for u in users + rotated] + [consumer["BOSS_LINK_LEAK_TOKEN"]]
    for secret in secrets_:
        assert secret not in caplog.text, "a test secret reached a log"
        assert secret not in listed and secret not in presence.text and secret not in created.text
        assert secret not in audit_rows
        assert all(secret.encode() not in bytes(r[0]) for r in stored)       # encrypted at rest
    # And the redaction filter scrubs them should anything ever log one.
    logging.getLogger("boss.test").warning("leaked %s", rotated[0]["password"])
    assert rotated[0]["password"] not in caplog.text
    _revoke(client, world, link["id"])


def test_a_pass_dies_with_its_workload(world, engine):
    with engine.begin() as db:
        row = {"id": "lnk_unused", "project_id": world.project_a}
        live_run = {"consumer_kind": "run", "consumer_id": world.attacker, "project_id": world.project_a}
        assert links.consumer_live(db, live_run) is False                       # completed: not live
        db.execute(text("UPDATE code_task_runs SET status = 'running' WHERE id = :i"), {"i": world.attacker})
        assert links.consumer_live(db, live_run) is True
        db.execute(text("UPDATE code_task_runs SET status = 'completed' WHERE id = :i"), {"i": world.attacker})
        prev = {"consumer_kind": "preview", "consumer_id": "prev_gone", "project_id": world.project_a}
        assert links.consumer_live(db, prev) is False
        assert row  # the pass rows themselves are covered by the revocation test


def test_the_build_and_the_preview_get_the_link_environment_and_nothing_else_does(client, world, engine):
    """At build: every process of THAT build (agent_environment, after its credential filter);
    at preview: the launched app (app_environment). Another build, or no build, gets nothing."""
    from pathlib import Path

    from boss_engine.app_links import run_env
    from boss_engine.services import owned_process, preview_service
    from boss_engine.services.agent_env import agent_environment

    link = _create(client, world, alias="WIRED").json()
    with engine.begin() as db:
        db.execute(text("UPDATE code_task_runs SET status = 'running' WHERE id = :i"), {"i": world.attacker})
    with owned_process.owner_scope("code_run", world.attacker, fresh=True), \
            run_env.for_run(engine, world.attacker, world.project_a) as env:
        inside = agent_environment({"PATH": "/bin"})
    with owned_process.owner_scope("code_run", world.third, fresh=True):
        other = agent_environment({"PATH": "/bin"})
    assert inside["BOSS_LINK_WIRED_URL"] == link["url"] and inside["BOSS_LINK_WIRED_TOKEN"] == env["BOSS_LINK_WIRED_TOKEN"]
    assert not any(k.startswith("BOSS_LINK_") for k in other)
    assert not any(k.startswith("BOSS_LINK_") for k in agent_environment({"PATH": "/bin"}))
    with engine.begin() as db:                         # the build ended: its pass is revoked
        assert links.resolve_pass(db, env["BOSS_LINK_WIRED_TOKEN"]) is None
        db.execute(text("UPDATE code_task_runs SET status = 'completed' WHERE id = :i"), {"i": world.attacker})
    extra = preview_service.preview_link_environment(
        engine, project_id=world.project_a, run_id=world.attacker, preview_id="prev_wired",
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5), rotate=True)
    app_env = preview_service.app_environment(Path("/tmp"), 19001, extra)
    assert app_env["BOSS_LINK_WIRED_URL"] == link["url"] and app_env["PORT"] == "19001"
    provider = preview_service.preview_link_environment(
        engine, project_id=world.project_a, run_id=world.bank, preview_id="prev_bank",
        expires_at=None, rotate=True)
    assert "BOSS_TEST_USERS" in provider and not any(k.startswith("BOSS_LINK_") for k in provider)
    assert 19000 <= preview_service.app_preview_port() <= 19999
    _revoke(client, world, link["id"])


def test_a_new_app_is_linked_under_the_title_its_start_sends(client, world, engine):
    """The composer's "Talks to" on a NEW app (§5.5): the link is made once the start is accepted,
    before the app's first build exists, so its build finds the link. Filed under the title's key
    — the key that first build will have — and only when the console says it is a new app."""
    from boss_engine.services.agent_assignment import app_key

    refused = _create(client, world, app="Bank attacker v2", alias="BANKNEW")
    assert refused.status_code == 404 and refused.json()["error"] == "unknown_app"
    made = _create(client, world, app="Bank attacker v2", alias="BANKNEW", new_app=True)
    assert made.status_code == 201, made.text
    with engine.connect() as db:
        consumer = db.execute(text("SELECT consumer_app_key FROM app_links WHERE id = :i"),
                              {"i": made.json()["id"]}).scalar()
    assert consumer == app_key("Bank attacker v2")
    # It names an app of THIS project as the provider all the same (D7).
    other = _create(client, world, app="Bank attacker v3", alias="ELSE", new_app=True,
                    provider_app_key="Other banking")
    assert other.status_code == 422 and other.json()["error"] == "provider_not_in_project"
