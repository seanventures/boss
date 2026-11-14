"""A person's own Jira sign-in (Atlassian OAuth 2.0 3LO) against the fake Jira, on the test DB.

Authorize URL -> code -> token exchange -> accessible-resources (cloudId) -> calls to
api.atlassian.com/ex/jira/{cloudId} as that person; ROTATING refresh tokens persisted on every
refresh, one refresh when two calls race; an expired/refused refresh -> needs_person_sign_in; the
bot (API token) fallback; and the identity every call acted as."""

from __future__ import annotations

import base64
import os
import threading
import uuid

import pytest
from sqlalchemy import create_engine, text

os.environ.setdefault("BOSS_MASTER_KEY",
                      base64.b64encode(b"p29a-test-master-key-32-bytes!!!").decode())

from boss_engine.connectors.tickets import auth as ticket_auth  # noqa: E402
from boss_engine.connectors.tickets import http as ticket_http  # noqa: E402
from boss_engine.connectors.tickets.contract import AuthFailed, TicketConnection, TicketRef  # noqa: E402,E501
from boss_engine.connectors.tickets.registry import get_ticket_adapter  # noqa: E402
from boss_engine.identity import vault  # noqa: E402
from boss_engine.services import connection_credentials as creds  # noqa: E402

from .fake_jira import CLIENT_ID, CLIENT_SECRET, CLOUD_ID, EMAIL, TOKEN, FakeJira  # noqa: E402

TEST_DB = os.environ.get("BOSS_TEST_DATABASE_URL", os.environ.get(
    "DATABASE_URL", "postgresql+psycopg://boss:boss@localhost:5432/boss_test"))
ALICE = "usr_tix_alice"
BOB = "usr_tix_bob"
CID = "kit:tickets:jira"
REDIRECT = "https://boss.example/v1/tickets/oauth/callback"


@pytest.fixture(scope="module")
def eng():
    engine = create_engine(TEST_DB, future=True)
    try:
        with engine.begin() as c:
            c.execute(text("SELECT 1 FROM connection_credentials LIMIT 1"))
            c.execute(text("SELECT connection_id FROM mcp_oauth_pending LIMIT 1"))
    except Exception as error:  # pragma: no cover
        pytest.skip(f"test database unavailable or not migrated: {type(error).__name__}")
    vault.reset_master_key_cache()
    return engine


@pytest.fixture()
def fake(monkeypatch):
    server = FakeJira()
    real = ticket_http._urllib_transport

    def route(method, url, headers, data, timeout, context):
        # auth.atlassian.com / api.atlassian.com are the fake (after the host check passed on
        # the real URL); the path is kept.
        for host in ("https://auth.atlassian.com", "https://api.atlassian.com"):
            if url.startswith(host):
                url = server.base + url[len(host):]
        return real(method, url, headers, data, timeout, context)

    monkeypatch.setattr(ticket_http, "TRANSPORT", route)
    monkeypatch.setattr(ticket_http, "SLEEP", lambda s: None)
    yield server
    server.close()


@pytest.fixture()
def project(eng):
    pid = f"p_tix_{uuid.uuid4().hex[:8]}"
    with eng.begin() as c:
        wid = c.execute(text("SELECT id FROM workspaces LIMIT 1")).scalar()
        if wid is None:
            pytest.skip("test database has no workspace")
        c.execute(text("INSERT INTO projects (id, workspace_id, name, slug) VALUES "
                       "(:i, :w, 'tickets oauth', :i)"), {"i": pid, "w": wid})
        org = creds.org_of_project(c, pid)
        for person in (ALICE, BOB):
            c.execute(text("INSERT INTO identity_principals (id, org_id, kind, status, "
                           "display_name) VALUES (:i, :o, 'user', 'active', :i) "
                           "ON CONFLICT (id) DO NOTHING"), {"i": person, "o": org})
    yield pid
    with eng.begin() as c:
        items = list(c.execute(text("SELECT vault_item_id FROM connection_credentials WHERE "
                                    "project_id = :p"), {"p": pid}).scalars().all())
        items += list(c.execute(text("SELECT id FROM identity_vault_items WHERE provider = :c "
                                     "AND id NOT IN (SELECT vault_item_id FROM "
                                     "connection_credentials)"), {"c": CID}).scalars().all())
        for table in ("connection_credentials", "mcp_oauth_pending", "events"):
            c.execute(text(f"DELETE FROM {table} WHERE project_id = :p"), {"p": pid})
        if items:
            c.execute(text("DELETE FROM identity_vault_items WHERE id = ANY(:i)"), {"i": items})
        c.execute(text("DELETE FROM projects WHERE id = :p"), {"p": pid})


def _conn(c, fake, pid, person=None, *, bot=False, **extra):
    config = {"base_url": fake.base, "email": EMAIL, "project_key": "KAN",
              "oauth_client_id": CLIENT_ID, "oauth_client_secret": CLIENT_SECRET, **extra}
    if bot:
        config["api_token"] = TOKEN
    return TicketConnection("jira", config, project_id=pid, db=c, person_id=person)


def _sign_in(eng, fake, pid, person=ALICE):
    with eng.begin() as c:
        started = ticket_auth.begin_oauth(_conn(c, fake, pid), person, redirect_uri=REDIRECT)
    code, state = fake.approve(started["authorize_url"])
    assert state == started["state"]
    with eng.begin() as c:
        return ticket_auth.complete_oauth(_conn(c, fake, pid), state, code, completed_by=person)


def _expire(eng, pid, person=ALICE):
    with eng.begin() as c:
        c.execute(text("UPDATE identity_vault_items SET access_expires_at = now() - interval "
                       "'1 minute' WHERE id = (SELECT vault_item_id FROM connection_credentials "
                       "WHERE project_id = :p AND subject_id = :s)"), {"p": pid, "s": person})


def test_sign_in_then_every_call_acts_as_the_person(eng, fake, project):
    with eng.begin() as c:
        started = ticket_auth.begin_oauth(_conn(c, fake, project), ALICE, redirect_uri=REDIRECT)
    url = started["authorize_url"]
    assert url.startswith("https://auth.atlassian.com/authorize?")
    assert "audience=api.atlassian.com" in url and "offline_access" in url
    assert CLIENT_SECRET not in url
    code, state = fake.approve(url)
    with eng.begin() as c:
        done = ticket_auth.complete_oauth(_conn(c, fake, project), state, code,
                                          completed_by=ALICE)
    assert done["resource_id"] == CLOUD_ID and done["person"] == ALICE
    with eng.begin() as c:
        row = creds.find(c, project, connection_id=CID, subject_kind="user", subject_id=ALICE)
        assert row and row["kind"] == "oauth" and row["status"] == "active"
        # the token lives only in the vault
        raw = c.execute(text("SELECT CAST(row_to_json(cc) AS TEXT) FROM connection_credentials cc "
                             "WHERE id = :i"), {"i": row["id"]}).scalar()
        assert not any(token in raw for token in fake.access)
    fake.log.clear()
    with eng.begin() as c:
        conn = _conn(c, fake, project, ALICE, bot=True)
        ticket = get_ticket_adapter("jira").get_ticket(conn, TicketRef("jira", key="KAN-2"))
        assert ticket.title == "Add Apple Pay"
        assert ticket.url == f"{fake.base}/browse/KAN-2"         # the site, not the API base
        assert conn.attribution and all(
            a["auth_kind"] == "oauth2" and a["subject_kind"] == "user" and a["subject_id"] == ALICE
            for a in conn.attribution)
    api_calls = [r for r in fake.log if "/rest/api/3/" in r.path]
    assert api_calls and all(r.path.startswith(f"/ex/jira/{CLOUD_ID}/") for r in api_calls)
    assert all(r.headers["authorization"].startswith("Bearer ") for r in api_calls)
    with eng.begin() as c:
        recorded = c.execute(text("SELECT actor FROM events WHERE project_id = :p AND "
                                  "event_type = 'ticket_system.call'"),
                             {"p": project}).mappings().all()
    assert recorded and all(r["actor"] == ALICE for r in recorded)


def test_the_state_is_single_use_signed_and_bound_to_its_person(eng, fake, project):
    with eng.begin() as c:
        started = ticket_auth.begin_oauth(_conn(c, fake, project), ALICE, redirect_uri=REDIRECT)
    code, state = fake.approve(started["authorize_url"])
    forged = state[:-2] + ("AA" if not state.endswith("AA") else "BB")
    with eng.begin() as c, pytest.raises(ticket_auth.OAuthError) as raised:
        ticket_auth.complete_oauth(_conn(c, fake, project), forged, code)
    assert raised.value.code == "invalid_state"
    with eng.begin() as c, pytest.raises(ticket_auth.OAuthError) as raised:
        ticket_auth.complete_oauth(_conn(c, fake, project), state, code, completed_by=BOB)
    assert raised.value.code == "not_the_starter"
    with eng.begin() as c, pytest.raises(ticket_auth.OAuthError) as raised:   # spent above
        ticket_auth.complete_oauth(_conn(c, fake, project), state, code, completed_by=ALICE)
    assert raised.value.code == "invalid_state"
    with eng.begin() as c, pytest.raises(ticket_auth.OAuthError) as raised:
        ticket_auth.begin_oauth(_conn(c, fake, project), "someone", redirect_uri=REDIRECT)
    assert raised.value.code == "bad_subject"


def test_without_a_sign_in_the_bot_token_acts_and_is_recorded(eng, fake, project):
    _sign_in(eng, fake, project, ALICE)
    with eng.begin() as c:
        conn = _conn(c, fake, project, BOB, bot=True)
        get_ticket_adapter("jira").get_ticket(conn, TicketRef("jira", key="KAN-2"))
        assert {(a["auth_kind"], a["subject_kind"]) for a in conn.attribution} == \
            {("api_token", "org")}
    assert all(not r.path.startswith("/ex/jira/") for r in fake.log if "/rest/" in r.path)


def test_no_sign_in_and_no_bot_needs_a_person(eng, fake, project):
    for policy in ("per_user", "per_user_fallback"):
        with eng.begin() as c:
            conn = _conn(c, fake, project, BOB, credential_policy=policy)
            with pytest.raises(AuthFailed) as raised:
                get_ticket_adapter("jira").get_ticket(conn, TicketRef("jira", key="KAN-2"))
        assert raised.value.reason == "needs_person_sign_in"
    # per_user never falls back to the bot, even when there is one
    with eng.begin() as c:
        conn = _conn(c, fake, project, BOB, bot=True, credential_policy="per_user")
        with pytest.raises(AuthFailed) as raised:
            get_ticket_adapter("jira").get_ticket(conn, TicketRef("jira", key="KAN-2"))
    assert raised.value.reason == "needs_person_sign_in"


def test_refresh_rotates_and_the_new_refresh_token_is_kept(eng, fake, project):
    _sign_in(eng, fake, project)
    for round_ in (1, 2, 3):
        _expire(eng, project)
        with eng.begin() as c:
            conn = _conn(c, fake, project, ALICE)
            get_ticket_adapter("jira").get_ticket(conn, TicketRef("jira", key="KAN-2"))
        # each refresh burned the previous refresh token; the next one worked only because the
        # rotated token was persisted
        assert fake.refreshes == round_
        assert len(fake.refresh_tokens) == 1


def test_two_racing_calls_refresh_once(eng, fake, project):
    _sign_in(eng, fake, project)
    _expire(eng, project)
    fake.refresh_delay = 0.3
    errors: list[BaseException] = []

    def call():
        try:
            with eng.begin() as c:
                get_ticket_adapter("jira").get_ticket(_conn(c, fake, project, ALICE),
                                                      TicketRef("jira", key="KAN-2"))
        except BaseException as error:  # noqa: BLE001
            errors.append(error)

    threads = [threading.Thread(target=call) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert not errors, errors
    assert fake.refreshes == 1


def test_a_refused_refresh_needs_the_person_again_or_falls_back_to_the_bot(eng, fake, project):
    _sign_in(eng, fake, project)
    _expire(eng, project)
    fake.refresh_tokens.clear()            # the provider revoked / expired the refresh token
    with eng.begin() as c:
        with pytest.raises(AuthFailed) as raised:
            get_ticket_adapter("jira").get_ticket(_conn(c, fake, project, ALICE),
                                                  TicketRef("jira", key="KAN-2"))
    assert raised.value.reason == "needs_person_sign_in"
    with eng.begin() as c:
        row = creds.find(c, project, connection_id=CID, subject_kind="user", subject_id=ALICE)
        assert row["status"] == "needs_reauth"
        conn = _conn(c, fake, project, ALICE, bot=True)
        get_ticket_adapter("jira").get_ticket(conn, TicketRef("jira", key="KAN-2"))
        assert conn.attribution[0]["auth_kind"] == "api_token"
    # signing in again restores the person's own identity
    _sign_in(eng, fake, project)
    with eng.begin() as c:
        conn = _conn(c, fake, project, ALICE, bot=True)
        get_ticket_adapter("jira").get_ticket(conn, TicketRef("jira", key="KAN-2"))
        assert conn.attribution[0]["auth_kind"] == "oauth2"


def test_disconnect_removes_the_person_sign_in(eng, fake, project):
    _sign_in(eng, fake, project)
    with eng.begin() as c:
        item = creds.find(c, project, connection_id=CID, subject_kind="user",
                          subject_id=ALICE)["vault_item_id"]
        assert ticket_auth.disconnect_person(_conn(c, fake, project), ALICE) is True
        assert ticket_auth.disconnect_person(_conn(c, fake, project), ALICE) is False
        status = c.execute(text("SELECT status FROM identity_vault_items WHERE id = :i"),
                           {"i": item}).scalar()
        assert status == "revoked"
        with pytest.raises(AuthFailed) as raised:
            get_ticket_adapter("jira").get_ticket(_conn(c, fake, project, ALICE),
                                                  TicketRef("jira", key="KAN-2"))
    assert raised.value.reason == "needs_person_sign_in"
