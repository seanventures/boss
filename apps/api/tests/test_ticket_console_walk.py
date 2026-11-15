"""What the console walk of the ticket screens needed from the API (BOSSAPP-1047, 2026-11-14).

* Settings › Client ID / Client secret: a connection's own OAuth app for "Connect with Atlassian"
  (PATCH `oauth_app`). The secret goes to the secret backend and is never answered back; the
  connection shows the client id, whether a secret is stored, and the callback URL to paste into
  the Atlassian app — the same URL the sign-in sends as redirect_uri.
* New work's ticket card promises acceptance criteria and comments: /v1/tickets/resolve answers
  the full read.
* A transition BOSS wrote lands on the binding, so the work list's ticket chip says the status
  the ticket is in now ("In Progress"), not the one it was imported with ("To Do").
"""
from __future__ import annotations

# Fixtures are imported from their suites, then named as parameters.
# ruff: noqa: F811

import base64
import os
from urllib.parse import parse_qs, urlsplit

from sqlalchemy import text

from apps.api.app import main

# A person's sign-in is recorded in the identity vault, which needs a master key (as in
# test_ticket_sync_api.py).
os.environ.setdefault("BOSS_MASTER_KEY", base64.b64encode(b"p29a-test-master-key-32-bytes!!!").decode())

from apps.api.tests.test_ticket_systems_api import (  # noqa: E402,F401  (fixtures)
    _connect, client, jira, project, world,
)
from tests.ticket_connectors import dataset as D  # noqa: E402

SECRET = "atlassian-app-secret-value-0001"


def test_the_connections_own_oauth_app_is_set_in_settings_and_used_by_connect(world, client, engine,
                                                                              project, jira, monkeypatch):
    monkeypatch.setenv("BOSS_PUBLIC_URL", "http://localhost:3000")
    monkeypatch.delenv("BOSS_JIRA_OAUTH_CLIENT_ID", raising=False)
    from boss_engine.identity import vault
    vault.reset_master_key_cache()
    with world.acting_as(world.ctx(world.owner)):
        cid = _connect(client, project, jira).json()["connection"]["id"]
        before = client.get("/v1/ticket-systems", params={"project_id": project}).json()["connections"][0]
        assert before["oauth_app"] == {"client_id": "", "secret_set": False, "from_host": False,
                                       "callback_url": "http://localhost:3000/v1/ticket-systems/oauth/callback"}

        # A secret without its client id, or an unknown key, is refused whole.
        assert client.patch(f"/v1/ticket-systems/{cid}", json={"oauth_app": {"client_id": "", "client_secret": SECRET}}
                            ).status_code == 422
        assert client.patch(f"/v1/ticket-systems/{cid}", json={"oauth_app": {"client": "x"}}).status_code == 422

        saved = client.patch(f"/v1/ticket-systems/{cid}",
                             json={"oauth_app": {"client_id": "atl-client-1", "client_secret": SECRET}})
        assert saved.status_code == 200, saved.text
        assert SECRET not in saved.text
        app = saved.json()["connection"]["oauth_app"]
        assert app["client_id"] == "atl-client-1" and app["secret_set"] is True
        with engine.connect() as db:
            stored = db.execute(text("SELECT configuration FROM work_tracker_providers WHERE id = :i"),
                                {"i": cid}).scalar()
        assert stored["oauth_client_id"] == "atl-client-1" and stored["oauth_client_secret_ref"]
        assert SECRET not in str(stored)

        # Other settings in the same PATCH still merge; a blank secret keeps the stored one.
        both = client.patch(f"/v1/ticket-systems/{cid}",
                            json={"oauth_app": {"client_id": "atl-client-2"}, "pickup": {"mode": "auto_start"}})
        assert both.status_code == 200, both.text
        assert both.json()["connection"]["oauth_app"]["secret_set"] is True
        assert both.json()["connection"]["settings"]["pickup"]["mode"] == "auto_start"

        # "Connect with Atlassian" now starts with this app and the callback URL shown above.
        started = client.post(f"/v1/ticket-systems/{cid}/oauth/start/load", json={})
        assert started.status_code == 200, started.text
        query = {k: v[0] for k, v in parse_qs(urlsplit(started.json()["authorize_url"]).query).items()}
        assert query["client_id"] == "atl-client-2"
        assert query["redirect_uri"] == app["callback_url"]

        # A blank client id removes the app AND its secret: the reference is revoked and the value
        # is gone from the store; host settings apply again.
        with engine.connect() as db:
            ref_id = db.execute(text("SELECT configuration->>'oauth_client_secret_ref' FROM work_tracker_providers "
                                     "WHERE id = :i"), {"i": cid}).scalar()
        cleared = client.patch(f"/v1/ticket-systems/{cid}", json={"oauth_app": {"client_id": ""}})
        assert cleared.json()["connection"]["oauth_app"]["client_id"] == ""
        assert cleared.json()["connection"]["oauth_app"]["secret_set"] is False
        from boss_engine.services.secret_backends import SecretUnavailable, resolve_secret_ref
        with engine.connect() as db:
            assert db.execute(text("SELECT status FROM secret_refs WHERE id = :i"), {"i": ref_id}).scalar() == "revoked"
            try:
                resolve_secret_ref(db, ref_id, main.settings, project_id=project)
            except SecretUnavailable:
                pass
            else:
                raise AssertionError("the cleared client secret still resolves")
        refused = client.post(f"/v1/ticket-systems/{cid}/oauth/start/load", json={})
        assert refused.status_code >= 400 and "Settings" in refused.text


def test_resolve_answers_the_full_ticket_for_new_works_card(world, client, project, jira):
    with world.acting_as(world.ctx(world.owner)):
        assert _connect(client, project, jira).status_code == 201
        found = client.get("/v1/tickets/resolve", params={"project_id": project, "text": f"KAN-{D.STORY}"})
    assert found.status_code == 200, found.text
    ticket = found.json()["tickets"][0]
    assert ticket["acceptance_criteria"] == D.AC_ITEMS
    assert isinstance(ticket["comments"], list) and "body" in ticket


def test_a_transition_boss_wrote_is_the_status_the_work_list_shows(world, client, engine, project, jira):
    from boss_engine.services import ticket_sync

    with world.acting_as(world.ctx(world.owner)):
        cid = _connect(client, project, jira).json()["connection"]["id"]
        got = client.post(f"/v1/projects/{project}/tickets/import", json={
            "connection_id": cid, "items": [{"key": f"KAN-{D.STORY}"}]}).json()["imported"]
    binding = got[0]["binding_id"]
    with engine.begin() as db:
        was = db.execute(text("SELECT external_state FROM work_tracker_bindings WHERE id = :b"), {"b": binding}).scalar()
        ticket_sync._mark_binding(db, binding, True, None, state="In Progress")
        moved = db.execute(text("SELECT external_state FROM work_tracker_bindings WHERE id = :b"), {"b": binding}).scalar()
        ticket_sync._mark_binding(db, binding, False, {"reason": "x"})           # a failure keeps it
        kept = db.execute(text("SELECT external_state FROM work_tracker_bindings WHERE id = :b"), {"b": binding}).scalar()
    assert was != "In Progress" and moved == "In Progress" and kept == "In Progress"
