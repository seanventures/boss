"""The upgrade promise, end to end (docs/community/IDENTITY_UPGRADE.md).

After the first Owner claims the install, the shared console password keeps signing in AS OPERATOR
for 30 days (plus at most one audited Owner extension of at most 30 days), every page shows the
countdown, and API clients on a legacy credential are told on every response (Deprecation, Sunset).

Audit finding U1: no code path ever signed in with the shared password after the claim, so every
teammate who used it was locked out at the claim. U5: the countdown banners were never shown.
"""
from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import text

from boss_engine.config import get_settings
from boss_engine.identity import authn, authz, legacy, migration, posture, signin
from boss_engine.identity.authn import SESSION_COOKIE, RequestCredentials
from boss_engine.identity.scope import ResolvedScope
from engine.tests.identity_s5_support import (  # noqa: F401, F811  (db, s5_engine: fixtures)
    CONSOLE_KEY,
    CONSOLE_PASSWORD,
    ORG,
    community_install,
    creds,
    db,
    fake_check,
    fake_claim,
    s5_engine,
    settings,
)


@pytest.fixture()
def shared(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "legacy_console_password", CONSOLE_PASSWORD)
    monkeypatch.setattr(s, "legacy_console_api_key", CONSOLE_KEY)
    monkeypatch.setattr(signin, "DELAY_ENABLED", False)
    return settings(legacy_console_password=CONSOLE_PASSWORD, legacy_console_api_key=CONSOLE_KEY,
                    api_key_principal_map={CONSOLE_KEY: "ceo"})


def _facts() -> signin.RequestFacts:
    return signin.RequestFacts(ip="127.0.0.1", user_agent="pytest", request_id="req_fxa_test",
                               request_origin="http://localhost:3000")


def _cookie(answer: signin.Answer) -> str:
    for header in answer.cookies:
        name, _, rest = header.partition("=")
        if name == SESSION_COOKIE:
            return rest.split(";", 1)[0]
    raise AssertionError("no session cookie")


def _ctx_for(db, cookie: str):
    req = RequestCredentials(method="GET", path="/v1/projects", headers={"user-agent": "pytest"},
                             cookies={SESSION_COOKIE: cookie}, client_ip="127.0.0.1")
    return authn.authenticate(req, db=db)


def _claim(db, cfg):
    community_install(db)
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    return fake_claim(db, name="Owner", email="owner@example.com", via="setup_token", now=now,
                      cfg=cfg), now


def test_before_the_claim_the_shared_password_only_claims(db, shared):
    community_install(db)
    answer = signin.password_login(db, _facts(), email="", password=CONSOLE_PASSWORD)
    assert answer.status == 409 and answer.body["error"] == "claim_first"
    assert not answer.cookies


def test_after_the_claim_it_signs_in_as_operator_until_the_cutoff(db, shared):
    claim, now = _claim(db, shared)
    answer = signin.password_login(db, _facts(), email="  ", password=CONSOLE_PASSWORD)
    assert answer.status == 200, answer.body
    assert answer.body["next"] == "done"
    cutoff = migration.hard_cutoff(db, ORG)
    assert cutoff is not None and cutoff <= now + timedelta(days=30, seconds=5)
    ctx = _ctx_for(db, _cookie(answer))
    assert ctx.principal.id == legacy.LEGACY_SHARED_PRINCIPAL
    assert ctx.legacy == "shared_password"
    # the session never outlives the shared password
    ends = db.execute(text("SELECT absolute_expires_at FROM identity_sessions WHERE id = :s"),
                      {"s": answer.body["session_id"]}).scalar()
    assert ends <= cutoff
    # Operator: runs the work (including the step-up actions it did before, it has no factor) ...
    assert authz.check(ctx, "workflow_run:start", ResolvedScope.in_project(ORG, "boss-dogfood"),
                       db=db).allowed
    assert authz.check(ctx, "decision:approve", ResolvedScope.in_project(ORG, "boss-dogfood"),
                       db=db).allowed
    # ... and is not an admin (a real denial is audited on its own connection, which would wait on
    # this test's open transaction: the same rules through the support's checker)
    assert not fake_check(db, ctx, "user:invite").allowed
    assert not fake_check(db, ctx, "owner:assign").allowed
    assert fake_check(db, ctx, "decision:approve", project="boss-dogfood").allowed


def test_a_wrong_shared_password_is_the_uniform_failure(db, shared):
    _claim(db, shared)
    answer = signin.password_login(db, _facts(), email="", password="not it")
    assert answer.status == 401 and answer.body == {"error": "invalid_credentials"}
    assert not answer.cookies


def test_after_the_cutoff_it_is_refused_and_its_sessions_end(db, shared, monkeypatch):
    _claim(db, shared)
    first = signin.password_login(db, _facts(), email="", password=CONSOLE_PASSWORD)
    assert first.status == 200
    cookie = _cookie(first)
    later = migration.hard_cutoff(db, ORG) + timedelta(minutes=1)
    real = legacy.check_shared_password
    monkeypatch.setattr(legacy, "check_shared_password",
                        lambda candidate, **kw: real(candidate, **{**kw, "now": later}))
    answer = signin.password_login(db, _facts(), email="", password=CONSOLE_PASSWORD)
    assert answer.status == 403 and answer.body["error"] == "shared_password_retired"
    with pytest.raises(authn.AuthnError):
        _ctx_for(db, cookie)


def test_every_page_shows_the_countdown_while_signed_in_with_it(db, shared):
    _claim(db, shared)
    answer = signin.password_login(db, _facts(), email="", password=CONSOLE_PASSWORD)
    ctx = _ctx_for(db, _cookie(answer))
    banners = posture.banners(ctx, db=db)
    countdown = [b for b in banners if b["kind"] == "legacy_password"]
    assert countdown and countdown[0]["until"] is not None
    assert "shared password" in countdown[0]["message"] and "invite" in countdown[0]["message"]


def test_the_sign_in_page_offers_it_only_while_it_works(db, shared):
    from apps.api.app.routers.identity_auth import _shared_password_offer

    community_install(db)
    assert _shared_password_offer(db, ORG) is None           # not claimed: it claims instead
    _claim(db, shared)
    offer = _shared_password_offer(db, ORG)
    assert offer is not None and offer["until"]
    migration.retire_path(ORG, legacy.LegacyKind.SHARED_PASSWORD, db=db,
                          actor_principal_id="sys_host_cli", reason="test")
    assert _shared_password_offer(db, ORG) is None


def test_legacy_api_clients_are_told_on_every_response(db, shared):
    claim, _now = _claim(db, shared)
    ctx = legacy.bridge(creds(method="api_key", member="ceo", key=CONSOLE_KEY, ip="127.0.0.1"),
                        db=db, settings=shared)
    headers = dict(legacy.deprecation_headers(ctx))
    assert headers[b"deprecation"] == b"true"
    assert b"GMT" in headers[b"sunset"]
    # a real credential is never marked
    assert legacy.deprecation_headers(claim.ctx) == []


@pytest.mark.parametrize("email", ["nobody@example.com", ""])
def test_a_failed_sign_in_is_recorded_by_the_real_audit_log_not_refused(db, shared, email):
    """Every failure event carries the identifier as the log's own HMAC (A79). Before, the sign-in
    flows hashed it with a different key, the log refused the event, and a wrong password was a
    500 whose rollback also dropped the backoff count."""
    from engine.tests.identity_s5_support import events

    community_install(db)
    answer = signin.password_login(db, _facts(), email=email, password="wrong")
    assert answer.status in (401, 409)
    if answer.status == 401:
        failed = events(db, "auth.login.failed")
        assert failed and str(failed[-1]["details"]["identifier"]).startswith("hmac:")
