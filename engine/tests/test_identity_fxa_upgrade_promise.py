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
    # authz audits a denial or a critical allow on its OWN connection, which would wait on this
    # test's open transaction: not what these tests are about
    monkeypatch.setattr(authz, "_audit", lambda *args, **kwargs: None)
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

    import random

    community_install(db)
    # a fresh source: the per-source backoff of earlier runs must not turn this into a soft lock
    facts = signin.RequestFacts(ip=f"10.{random.randint(0, 255)}.{random.randint(0, 255)}.7",
                                user_agent="pytest", request_origin="http://localhost:3000")
    answer = signin.password_login(db, facts, email=email, password="wrong")
    assert answer.status in (401, 409)
    if answer.status == 401:
        # (whichever org the database's default is: other suites may have left one behind)
        failed = db.execute(text("SELECT details FROM identity_security_events WHERE event_type = "
                                 "'auth.login.failed' AND ip = CAST(:ip AS inet) ORDER BY id DESC "
                                 "LIMIT 1"), {"ip": facts.ip}).scalar()
        assert failed and str(failed["identifier"]).startswith("hmac:"), failed


# ---- the host tools keep working: their own service-account keys, never the shared key ---------

def _key_ctx(db, key: str, *, header: str = "authorization"):
    value = f"Bearer {key}" if header == "authorization" else key
    req = RequestCredentials(method="GET", path="/v1/projects",
                             headers={"user-agent": "boss.sh", header: value}, cookies={},
                             client_ip="127.0.0.1")
    return authn.authenticate(req, db=db)


@pytest.mark.parametrize("header", ["authorization", "x-boss-api-key"])
def test_the_host_tools_key_outlives_the_shared_key(db, shared, header):
    from boss_engine.identity import host_tools

    _claim(db, shared)
    created = host_tools.issue_host_key(db, org_id=ORG)
    assert created.key.startswith("boss_sak_")
    ctx = _key_ctx(db, created.key, header=header)     # the rc5 tools' header works too
    assert ctx.principal.id == host_tools.HOST_TOOLS_PRINCIPAL and ctx.legacy is None
    # A step-up permission in a service account's key scopes works (IDENTITY.md §4.6): `boss
    # connect` registers models (model_provider:manage, step-up) with this key.
    decision = authz.check(ctx, "model_provider:manage", ResolvedScope.org_level(ORG), db=db)
    assert decision.allowed, decision.reason
    assert authz._step_up_exempt(ctx)  # noqa: SLF001
    # (the support's checker for the rest: a real check of a critical permission, or a denial,
    # is audited on its own connection, which would wait on this test's open transaction)
    assert fake_check(db, ctx, "workflow_run:start", project="boss-dogfood").allowed
    assert not fake_check(db, ctx, "user:invite").allowed          # never people and access
    assert host_tools.host_tools_permissions() <= migration.CONSOLE_AFTER_CLAIM_SET
    # no legacy cutoff applies to it, and it is not marked deprecated
    assert legacy.deprecation_headers(ctx) == []
    # issuing again ends the earlier key (boss identity host-key is a rotation)
    again = host_tools.issue_host_key(db, org_id=ORG)
    with pytest.raises(authn.AuthnError):
        _key_ctx(db, created.key)
    assert _key_ctx(db, again.key).principal.id == host_tools.HOST_TOOLS_PRINCIPAL


def test_the_mcp_key_is_pinned_to_its_project(db, shared):
    from boss_engine.identity import host_tools

    _claim(db, shared)
    db.execute(text("INSERT INTO projects (id, workspace_id, name, slug) VALUES ('other', "
                    "'ws_boss', 'other', 'other') ON CONFLICT DO NOTHING"))
    created = host_tools.issue_mcp_key(db, org_id=ORG, project_id="boss-dogfood")
    ctx = _key_ctx(db, created.key)
    assert ctx.credential.project_ids == frozenset({"boss-dogfood"})
    assert fake_check(db, ctx, "workflow_run:start", project="boss-dogfood").allowed
    assert not fake_check(db, ctx, "workflow_run:start", project="other").allowed
    assert not fake_check(db, ctx, "user:invite").allowed
    with pytest.raises(ValueError):
        host_tools.issue_mcp_key(db, org_id=ORG, project_id="nope")
    with pytest.raises(ValueError):
        host_tools.issue_mcp_key(db, org_id=ORG, project_id="boss-dogfood", wanted=["no_such_cap"])


def test_the_host_key_commands_are_host_shell_only():
    """Reached from no HTTP route and no MCP tool, like recover-owner (the same pin)."""
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[2]
    for path in list((root / "apps/api/app").rglob("*.py")) + list(
            (root / "engine/boss_engine/mcp").rglob("*.py")):
        text_ = path.read_text(encoding="utf-8")
        assert "issue_host_key" not in text_ and "issue_mcp_key" not in text_, path


# ---- a host that cannot fence the build sandbox keeps BOSS up (audit U6) ------------------------

def test_a_host_without_the_network_fence_holds_builds_and_says_how_to_fix_it(tmp_path, monkeypatch):
    from boss_engine.services import sandbox_identity

    monkeypatch.setattr(sandbox_identity, "SHIM_ROOT", str(tmp_path))
    monkeypatch.setenv("BOSS_SANDBOX_NETWORK", "enforce")
    assert sandbox_identity.network_problem() is None
    (tmp_path / sandbox_identity.NETWORK_PROBLEM_MARKER).write_text(
        "iptables owner-match rules could not be installed\n")
    assert not sandbox_identity.network_fenced()
    reason = sandbox_identity.unavailable_reason()
    assert "BOSS_SANDBOX_NETWORK=off" in reason and "SANDBOX.md" in reason
    assert sandbox_identity.status()["network_problem"]


def test_the_entrypoint_starts_without_the_fence_unless_told_to_refuse():
    import pathlib

    script = (pathlib.Path(__file__).resolve().parents[2] / "scripts/sandbox/entrypoint.sh").read_text()
    failure = script[script.index("if ! fence_network; then"):script.index('chown root:root "$SHIM_ROOT/.network-fenced"')]
    # the only exit on a failed fence is the explicit `required` mode
    assert failure.count("exit 1") == 1
    assert 'if [ "$MODE" = "required" ]; then' in failure
    assert ".network-unfenced" in failure


# ---- rc5's SSO providers come across (P0-3) ------------------------------------------------------

def test_rc5_sso_providers_are_imported_once_switched_off(db, shared):
    community_install(db)
    for pid, kind, extra in (("eip_okta", "oidc", {"issuer": "https://acme.okta.example", "audience": "client-123"}),
                             ("eip_adfs", "saml", {"entity_id": "urn:adfs", "sso_url": "https://adfs.example/sso"})):
        db.execute(text(
            "INSERT INTO enterprise_identity_providers (id, org_id, project_id, provider_key, "
            "provider_type, display_name, issuer, audience, entity_id, sso_url, status) VALUES "
            "(:id, :o, 'boss-dogfood', :id, :t, :n, :iss, :aud, :ent, :sso, 'active')"),
            {"id": pid, "o": ORG, "t": kind, "n": pid.upper(), "iss": extra.get("issuer"),
             "aud": extra.get("audience"), "ent": extra.get("entity_id"), "sso": extra.get("sso_url")})
    report = migration.seed(db, settings=shared, org_id=ORG)
    assert report.providers == 2
    rows = {r["imported_from"]: r for r in db.execute(text(
        "SELECT imported_from, protocol, status, issuer, client_id, saml_sso_url FROM identity_providers "
        "WHERE imported_from IS NOT NULL")).mappings()}
    assert rows["eip_okta"]["protocol"] == "oidc" and rows["eip_okta"]["client_id"] == "client-123"
    assert rows["eip_adfs"]["issuer"] == "urn:adfs" and rows["eip_adfs"]["saml_sso_url"] == "https://adfs.example/sso"
    assert {r["status"] for r in rows.values()} == {"disabled"}     # no secret yet: an Owner turns it on
    assert migration.seed(db, settings=shared, org_id=ORG).providers == 0   # once


# ---- Enforce SSO holds for every way in (P2; IDENTITY.md §4.8) ---------------------------------------

def test_enforce_sso_refuses_every_first_factor_but_its_own_provider(db, shared, monkeypatch):
    from boss_engine.identity import email_signin, users

    _claim(db, shared)
    db.execute(text("INSERT INTO identity_providers (id, org_id, protocol, name, domains, enforce_sso, "
                    "status) VALUES ('idp_acme', :o, 'oidc', 'Acme', ARRAY['acme.test'], true, 'active'), "
                    "('idp_other', :o, 'oidc', 'Other', ARRAY[]::text[], false, 'active')"), {"o": ORG})
    pid = users.create_user(db, org_id=ORG, email="dana@acme.test", display_name="Dana",
                            email_verified=True, created_by="test")
    dana = users.by_id(db, pid)
    for method in ("pwd", "magic_link", "email_code", "sso:idp_other", "passkey"):
        refused = signin.sso_refusal(db, dana, _facts(), method=method)
        assert refused is not None and refused.body == {"error": "sso_required"}, method
    assert signin.sso_refusal(db, dana, _facts(), method="sso:idp_acme") is None
    # the same path every flow takes
    assert signin.finish_first_factor(db, dana, _facts(), method="magic_link",
                                      email_method=True).status == 403
    # no email sign-in, reset or sign-up is sent for that domain (the same uniform answer)
    assert email_signin._eligible(db, "dana@acme.test") is None  # noqa: SLF001
    # an Owner keeps the passkey break-glass
    owner = users.by_id(db, users.create_user(db, org_id=ORG, email="root@acme.test",
                                              display_name="Root", email_verified=True))
    migration.bind(db, ORG, owner.principal_id, "arl_owner", "org", ORG)
    assert signin.sso_refusal(db, owner, _facts(), method="passkey") is None
    assert signin.sso_refusal(db, owner, _facts(), method="pwd") is not None


# ---- identity housekeeping (P2): keys, the security log's retention, the egress log, the catalogue ---

def _sak(db, *, expires=None, created=None, name="ci"):
    from boss_engine.identity import host_tools

    host_tools.issue_host_key(db, org_id=ORG, revoke_others=False)     # the account and role
    created_key = host_tools._mint(db, ORG, host_tools.HOST_TOOLS_PRINCIPAL, name,  # noqa: SLF001
                                   frozenset({"system:read"}), None, revoke_others=False)
    db.execute(text("UPDATE identity_api_credentials SET expires_at = :e, created_at = "
                    "COALESCE(:c, created_at) WHERE id = :k"),
               {"e": expires, "c": created, "k": created_key.id})
    return created_key.id


def test_keys_are_reminded_once_logged_when_expired_and_revoked_when_unused(db, shared, monkeypatch):
    from datetime import datetime, timezone

    from boss_engine.identity import hygiene, policy, users
    from engine.tests.identity_s5_support import events

    claim, now = _claim(db, shared)
    soon = _sak(db, expires=now + timedelta(days=2), name="soon")
    gone = _sak(db, expires=now - timedelta(hours=1), name="gone")
    first = hygiene.key_hygiene(db, org_id=ORG, now=now)
    assert first["expired"] == 1 and first["reminded"] == 2           # 14 and 3 days, once each
    again = hygiene.key_hygiene(db, org_id=ORG, now=now)
    assert again == {"expired": 0, "reminded": 0, "auto_revoked": 0}
    assert {e["target_id"] for e in events(db, "api_key.expired")} == {gone}
    # the owner of the service account sees it on every page
    db.execute(text("UPDATE identity_service_accounts SET owner_principal_ids = ARRAY[:p] WHERE "
                    "principal_id = (SELECT principal_id FROM identity_api_credentials WHERE id = :k)"),
               {"p": claim.principal_id, "k": soon})
    banners = posture.banners(claim.ctx, db=db, now=now)
    assert any(b["kind"] == "api_key_expiring" and "soon" in b["message"] for b in banners)
    # the unused-key policy revokes, audited
    real = policy.get_policy
    monkeypatch.setattr(policy, "get_policy", lambda org, *, db=None: __import__("dataclasses").replace(
        real(org, db=db), key_auto_revoke_unused_days=30))
    stale = _sak(db, expires=now + timedelta(days=200), created=now - timedelta(days=45), name="stale")
    db.execute(text("UPDATE identity_api_credentials SET last_used_at = :t WHERE id = :k"),
               {"t": now - timedelta(days=40), "k": stale})
    done = hygiene.key_hygiene(db, org_id=ORG, now=now)
    assert done["auto_revoked"] >= 1
    assert db.execute(text("SELECT revoked_at FROM identity_api_credentials WHERE id = :k"),
                      {"k": stale}).scalar() is not None


def test_the_security_log_prunes_itself_past_retention_and_still_verifies(db, shared):
    from boss_engine.identity import audit, hygiene

    _claim(db, shared)
    from datetime import datetime, timezone

    later = datetime.now(timezone.utc) + timedelta(days=hygiene.retention_days() + 2)
    assert hygiene.audit_retention(db, org_id=ORG, now=later) > 0
    assert audit.verify_chain(ORG, db=db).ok
    assert db.execute(text("SELECT count(*) FROM identity_security_events WHERE org_id = :o AND "
                           "event_type = 'bootstrap.claimed'"), {"o": ORG}).scalar() == 1


def test_the_egress_log_has_daily_partitions_and_keeps_30_days(db):
    from datetime import datetime, timezone

    from boss_engine.identity import hygiene

    now = datetime.now(timezone.utc)
    db.execute(text("INSERT INTO identity_egress_log (ts, org_id, run_id, provider, method, "
                    "path_template, decision) VALUES (:t, :o, 'run_old', 'github', 'GET', '/x', 'allowed')"),
               {"t": now - timedelta(days=45), "o": ORG})
    out = hygiene.egress_partitions(db, now=now)
    assert out["purged"] >= 1
    tomorrow = (now + timedelta(days=1)).strftime("%Y%m%d")
    assert db.execute(text("SELECT to_regclass(:n)"), {"n": f"identity_egress_log_{tomorrow}"}).scalar()
    assert hygiene.egress_partitions(db, now=now)["created"] == 0          # idempotent


def test_a_permission_dropped_from_the_catalogue_is_retired_with_an_event(db, shared):
    from boss_engine.identity import hygiene
    from engine.tests.identity_s5_support import events

    _claim(db, shared)
    db.execute(text("INSERT INTO identity_permissions (key, resource, action, title, description, "
                    "category, scope_kind, step_up, delegable, critical, system_only, catalogue_version) "
                    "VALUES ('ghost:haunt', 'ghost', 'haunt', 'Haunt', 'Gone.', 'Operations', 'org', "
                    "false, false, false, false, 0)"))
    role = migration.ensure_custom_role(db, ORG, "ghosts", "Ghosts", ["ghost:haunt", "system:read"],
                                        system=False)
    assert hygiene.retire_removed_permissions(db, org_id=ORG) == ["ghost:haunt"]
    assert db.execute(text("SELECT count(*) FROM identity_access_role_permissions WHERE role_id = :r"),
                      {"r": role}).scalar() == 1
    event = events(db, "catalogue.permission_removed")[-1]
    assert event["target_id"] == "ghost:haunt" and event["details"]["roles"] == 1
    assert hygiene.retire_removed_permissions(db, org_id=ORG) == []
