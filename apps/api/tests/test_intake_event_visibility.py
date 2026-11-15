"""BOSSAPP-1196: every intake read that can tie a chat user to a BOSS person withholds it from a
caller without user:read. The routes are integration:read (Operator and Viewer hold it), but who
the chat user is in BOSS (the event's principal_id, each outcome subject's principal_id, the
envelope's bossactor email, any email in the raw body) is directory data, the same rule as the
People list (test_intake_people_visibility). One redaction (event_intake.redact_people) serves
the events list, one event, dead letters and the route preview; a `q` search and a preview
filter are no oracle for what the answer withholds."""
from __future__ import annotations

import json
import os
import uuid
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text

from apps.api.app.routers.agent_platform import build_agent_platform_router
from apps.api.app.routers.intake import build_intake_router
from boss_engine.identity import authz
from boss_engine.identity.permissions_gen import ROLE_PERMISSIONS
from boss_engine.services import event_intake

os.environ.setdefault("BOSS_COMMUNITY_SECRET_STORE", "database")

SEES = {"owner": True, "admin": True, "auditor": True, "operator": False, "viewer": False}
EMAIL = "priya.raman@northpeak.test"
PERSON = "usr_priya_1196"


@pytest.fixture()
def world(engine):
    from boss_engine.config import get_settings

    pid = f"p_iev_{uuid.uuid4().hex[:10]}"
    with engine.begin() as c:
        c.execute(text("INSERT INTO orgs (id, name) VALUES ('org_iev', 'iev') ON CONFLICT DO NOTHING"))
        c.execute(text("INSERT INTO workspaces (id, org_id, name) VALUES ('ws_iev', 'org_iev', 'iev') "
                       "ON CONFLICT DO NOTHING"))
        c.execute(text("INSERT INTO projects (id, workspace_id, name, slug) VALUES (:p, 'ws_iev', :p, :p)"),
                  {"p": pid})
        conn = event_intake.create_connection(c, project_id=pid, kind="webhook", actor="ceo",
                                              name="hooks", config={}, secrets={}, limits={})
        envelope = {"specversion": "1.0", "id": "e1", "source": "webhook://x", "type": "com.acme.ping",
                    "data": {"text": "deploy failed", "reporter": {"email": EMAIL}},
                    "bossconnection": conn["id"],
                    "bossactor": {"kind": "user", "external_id": "U0PRIYA", "display": "priya",
                                  "email": EMAIL}}
        outcome = [{"trigger_id": "atrg_x", "fire_outcome": "admitted", "run_id": "arun_x",
                    "subject": {"mode": "person", "kind": "person", "principal_id": PERSON,
                                "link_id": "chl_1", "method": "sso_email", "external_id": "U0PRIYA",
                                "display": "priya", "reason": "linked"}}]
        raw = json.dumps({"actor": {"id": "U0PRIYA", "email": EMAIL}, "text": "deploy failed"})
        ids = {}
        for status in ("routed", "dead"):
            ids[status] = f"iev_{uuid.uuid4().hex[:12]}"
            c.execute(text("""
                INSERT INTO intake_events (id, connection_id, project_id, dedupe_key, type, subject,
                       status, raw, envelope, actor_kind, actor_external_id, principal_id, outcome,
                       processed_at)
                VALUES (:id, :c, :p, :k, 'com.acme.ping', 'svc', :s, :raw, CAST(:env AS JSONB),
                        'user', 'U0PRIYA', :pr, CAST(:out AS JSONB), now())"""),
                {"id": ids[status], "c": conn["id"], "p": pid, "k": f"k-{status}", "s": status,
                 "raw": raw.encode(), "env": json.dumps(envelope), "pr": PERSON,
                 "out": json.dumps(outcome)})
    app = FastAPI()
    app.include_router(build_intake_router(engine=engine, settings=get_settings,
                                           require_write_actor=lambda *a, **k: "ceo",
                                           require_reader=lambda *a, **k: None))
    app.include_router(build_agent_platform_router(engine=engine,
                                                   require_write_actor=lambda *a, **k: "ceo",
                                                   require_reader=lambda *a, **k: None))
    yield SimpleNamespace(client=TestClient(app), pid=pid, conn=conn["id"], ids=ids)
    with engine.begin() as c:
        c.execute(text("DELETE FROM intake_events WHERE project_id = :p"), {"p": pid})
        c.execute(text("DELETE FROM intake_connections WHERE project_id = :p"), {"p": pid})


def _as(monkeypatch, role: str) -> None:
    from apps.api.app import identity_guard

    ctx = SimpleNamespace(principal=SimpleNamespace(org_id="org_iev", id=f"usr_{role}"))
    monkeypatch.setattr(identity_guard, "request_auth", lambda request: ctx)
    monkeypatch.setattr(authz, "effective_permissions",
                        lambda c, scope, db=None: SimpleNamespace(
                            permissions=frozenset(ROLE_PERMISSIONS[role])))
    assert "integration:read" in ROLE_PERMISSIONS[role], "every built-in role reads the routes"


def _leaks(body) -> bool:
    blob = json.dumps(body)
    return EMAIL in blob or PERSON in blob


@pytest.mark.parametrize("role,sees", sorted(SEES.items()))
def test_event_detail_names_the_person_only_with_user_read(world, monkeypatch, role, sees):
    _as(monkeypatch, role)
    r = world.client.get(f"/v1/intake/events/{world.ids['routed']}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["people_redacted"] is (not sees)
    assert _leaks(body) is sees
    if sees:
        assert body["principal_id"] == PERSON
        assert body["outcome"][0]["subject"]["principal_id"] == PERSON
        assert body["envelope"]["bossactor"]["email"] == EMAIL
    else:
        assert body["principal_id"] is None
        subject = body["outcome"][0]["subject"]
        assert subject["principal_id"] is None
        # The chat side and the link's method stay: the console can say "a BOSS person".
        assert subject["kind"] == "person" and subject["method"] == "sso_email"
        assert body["envelope"]["bossactor"] == {"kind": "user", "external_id": "U0PRIYA",
                                                 "display": "priya", "email": None}
        assert body["raw"]["actor"] == {"id": "U0PRIYA", "email": None}
        assert body["envelope"]["data"]["text"] == "deploy failed"


@pytest.mark.parametrize("role,sees", sorted(SEES.items()))
def test_event_list_and_its_search_withhold_the_person(world, monkeypatch, role, sees):
    _as(monkeypatch, role)
    r = world.client.get("/v1/intake/events", params={"project_id": world.pid})
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["events"]) == 2
    assert body["people_redacted"] is (not sees)
    assert not _leaks(body)               # the summary never carried it; the flag still says so
    # A search must not confirm an email the answer withholds.
    found = world.client.get("/v1/intake/events", params={"project_id": world.pid, "q": EMAIL}).json()
    assert len(found["events"]) == (2 if sees else 0)
    # Ordinary text still searches.
    assert len(world.client.get("/v1/intake/events",
                                params={"project_id": world.pid, "q": "deploy"}).json()["events"]) == 2


@pytest.mark.parametrize("role,sees", sorted(SEES.items()))
def test_dead_letters_withhold_the_person(world, monkeypatch, role, sees):
    _as(monkeypatch, role)
    r = world.client.get("/v1/intake/dead-letters", params={"project_id": world.pid})
    assert r.status_code == 200, r.text
    body = r.json()
    assert [d["id"] for d in body["dead_letters"]] == [world.ids["dead"]]
    assert body["people_redacted"] is (not sees)
    assert not _leaks(body)


@pytest.mark.parametrize("role,sees", sorted(SEES.items()))
def test_route_preview_is_no_oracle_for_an_email(world, monkeypatch, role, sees):
    _as(monkeypatch, role)
    r = world.client.post("/v1/agents/support/triggers/preview-match", json={
        "project_id": world.pid, "connection_id": world.conn,
        "match": {"field": "bossactor.email", "op": "eq", "value": EMAIL}})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["people_redacted"] is (not sees)
    assert body["matched"] == (2 if sees else 0)
    # A leaf echoes the caller's own `value`; its `actual` is the email only with user:read.
    actuals = [leaf["actual"] for e in body["events"] for leaf in e["leaves"]]
    assert actuals == ([EMAIL, EMAIL] if sees else [None, None])


def test_no_identity_fails_closed(world, monkeypatch):
    from apps.api.app import identity_guard

    monkeypatch.setattr(identity_guard, "request_auth", lambda request: None)
    body = world.client.get(f"/v1/intake/events/{world.ids['routed']}").json()
    assert body["people_redacted"] is True and not _leaks(body)


def test_the_engine_redacts_by_default(world, engine):
    """A caller that forgets `reveal` gets the redacted answer, never the person."""
    with engine.begin() as c:
        one = event_intake.get_event(c, world.ids["routed"])
        listed = event_intake.list_events(c, world.pid, q=EMAIL)
        dead = event_intake.dead_letters(c, world.pid)
        shown = event_intake.get_event(c, world.ids["routed"], reveal=True)
    assert one["people_redacted"] and not _leaks(one)
    assert listed["events"] == [] and dead["people_redacted"]
    assert shown["principal_id"] == PERSON and shown["people_redacted"] is False
    # A body that is not JSON cannot be scrubbed: withheld whole.
    assert event_intake._redacted({"raw_base64": "AAAA"}, False)["raw_base64"] is None
