"""POST /v1/identity/setup-link: `boss identity setup-link` for an install with no shell (shared-server:
a managed host reached only through an ops API — no SSH, no logs). The install's admin key
(BOSS_API_KEY, the same trust as a shell on the box) gets a fresh one-hour claim link while there
is no Owner; nobody else does, and nothing ever logs or records the token.

The real app, the real legacy bridge (the S1 world fakes it) and the real audit log.
"""
from __future__ import annotations

import logging
import re

import pytest
from sqlalchemy import text

from apps.api.app import main
from apps.api.tests.identity_s1_world import (  # noqa: F401  (world is a fixture)
    HEADERS, STRONG, csrf, fresh_client, make_user, sign_in, world)
from apps.api.tests.test_identity_claim_to_signed_in_owner import real_world  # noqa: F401  (fixture)
from boss_engine.config import get_settings
from boss_engine.identity import host_tools, legacy, migration

ADMIN_KEY = "install-admin-key-setup-link-000000000000"
OTHER_KEY = "a-lesser-env-key-setup-link-0000000000000"
REAL_BRIDGE = legacy.bridge          # captured before the S1 world swaps in its fake
URL_RE = re.compile(r"^https?://[^#]+/claim#t=(bst_[A-Za-z0-9_\-]+)$")
ROUTE = "/v1/identity/setup-link"


@pytest.fixture
def keyed(real_world, monkeypatch):  # noqa: F811
    for settings in (main.settings, get_settings()):
        monkeypatch.setattr(settings, "require_api_auth", True)
        monkeypatch.setattr(settings, "api_keys", f"{ADMIN_KEY},{OTHER_KEY}")
        monkeypatch.setattr(settings, "api_key_principals", f"{ADMIN_KEY}:ceo,{OTHER_KEY}:ceo")
        monkeypatch.setattr(settings, "legacy_console_api_key", ADMIN_KEY)
    monkeypatch.setattr(legacy, "bridge", REAL_BRIDGE)
    legacy.invalidate_cache()
    migration.forget_seeded()
    yield real_world
    legacy.invalidate_cache()
    migration.forget_seeded()


def _as(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def _issue(key: str = ADMIN_KEY):
    return fresh_client().post(ROUTE, headers=_as(key))


def _claim(token: str, email: str = "owner@example.test"):
    return fresh_client().post("/v1/auth/setup/claim", headers=HEADERS, json={
        "name": "Api Owner", "email": email, "setup_token": token, "password": STRONG})


def test_the_admin_key_gets_a_link_that_claims_the_install(keyed, engine, capsys, caplog):
    caplog.set_level(logging.DEBUG)
    first = _issue()
    assert first.status_code == 200, first.text
    assert first.headers["cache-control"] == "no-store"
    body = first.json()
    assert set(body) == {"url", "expires_at"} and body["expires_at"]
    old = URL_RE.match(body["url"]).group(1)

    # the CLI's semantics: each call is a NEW link that voids the last
    again = _issue().json()
    token = URL_RE.match(again["url"]).group(1)
    assert token != old
    assert _claim(old).status_code in (400, 401, 404)

    claimed = _claim(token)
    assert claimed.status_code == 200, claimed.text

    with engine.begin() as db:
        rows = db.execute(text("SELECT actor_principal_id, details::text AS d, row_to_json(e)::text AS j "
                               "FROM identity_security_events e "
                               "WHERE event_type = 'identity.setup_link_issued' ORDER BY seq")).all()
        everything = db.execute(text("SELECT coalesce(string_agg(row_to_json(e)::text, ''), '') "
                                     "FROM identity_security_events e")).scalar()
    assert len(rows) == 2 and {r.actor_principal_id for r in rows} == {legacy.LEGACY_CONSOLE_PRINCIPAL}
    printed = capsys.readouterr()
    logged = "\n".join(r.getMessage() for r in caplog.records)
    for secret in (old, token):
        assert secret not in everything
        assert secret not in printed.out and secret not in printed.err and secret not in logged

    # claimed: gone for good, to the admin key too (like the claim route)
    assert _issue().status_code == 404


def test_no_key_is_401(keyed):
    assert fresh_client().post(ROUTE).status_code == 401


def test_a_lesser_key_or_a_person_is_403(keyed, engine):
    assert _issue(OTHER_KEY).status_code == 403
    with engine.begin() as db:
        host_key = host_tools.issue_host_key(db, org_id=migration.default_org(db)).key
    assert _issue(host_key).status_code == 403

    make_user(engine, "person@example.test", role="owner")
    person = fresh_client()
    assert sign_in(person, "person@example.test").status_code == 200
    refused = person.post(ROUTE, headers=csrf(person))
    assert refused.status_code == 403, refused.text
    with engine.begin() as db:
        assert db.execute(text("SELECT count(*) FROM identity_auth_challenges WHERE kind = 'setup' "
                               "AND consumed_at IS NULL AND expires_at > now()")).scalar() == 0


def test_the_link_is_rate_limited(keyed, monkeypatch):
    from boss_engine.identity import claim

    monkeypatch.setattr(claim, "LINKS_PER_HOUR", 2)
    assert _issue().status_code == 200 and _issue().status_code == 200
    limited = _issue()
    assert limited.status_code == 429 and int(limited.headers["retry-after"]) > 0


def test_the_options_say_a_link_can_be_issued_over_the_api(keyed):
    claim = fresh_client().get("/v1/auth/options", headers=HEADERS).json()["claim"]
    assert claim["link_over_api"] is True
