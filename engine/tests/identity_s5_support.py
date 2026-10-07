"""Stream S5's test support: a throwaway Postgres, install worlds shaped like today's, and fakes
of the two frozen interfaces S5 consumes before they land (IDENTITY_BUILD.md §3: "until the other
side lands, each stream tests against a fake of the frozen interface"):

  - `fake_claim` is S1's claim service as IDENTITY.md §4.11/§4.12 defines it: create the person,
    bind Owner, record `bootstrap.claimed`, then call S5's `migration.on_claimed` in the SAME
    transaction;
  - `fake_check` is S2's `authz.check` as §6.6 writes it, using S5's two published hooks
    (`legacy.system_permissions` for fixed sets, `legacy.step_up_exempt` for bridges).

The database: IDENTITY_S5_DATABASE_URL (or DATABASE_URL), which must name a test database. Every
test runs inside one transaction that is rolled back, so tests never see each other's rows.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

import pytest
from sqlalchemy import create_engine, text

from boss_engine.identity import audit, legacy, migration, policy, shadow_report
from boss_engine.identity.audit import Outcome, SecurityEvent
from boss_engine.identity.authn import RequestCredentials
from boss_engine.identity.ids import new_id
from boss_engine.identity.permissions_gen import (PERMISSION_META, ROLE_PERMISSIONS,
                                                  SYSTEM_PRINCIPALS)
from boss_engine.identity.principal import AuthContext, AuthMethod, Principal, PrincipalKind, \
    PrincipalStatus, SessionRef

REPO = Path(__file__).resolve().parents[2]
ORG = "org_boss"
PROJECT = "boss-dogfood"
NOW = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)

# Credentials of the rehearsal installs. Shaped like the real ones (install.sh generates 64-hex
# keys; the console password is free text), never real.
CONSOLE_KEY = "c0ffee" + "1" * 58
WORKER_KEY = "feed" + "2" * 60
MCP_KEY = "beef" + "3" * 60
SCIM_TOKEN = "scimtok_" + "4" * 40
CONSOLE_PASSWORD = "correct horse battery staple 2026"


def database_url() -> Optional[str]:
    url = os.environ.get("IDENTITY_S5_DATABASE_URL") or os.environ.get("DATABASE_URL") or ""
    if not url:
        return None
    name = url.rsplit("/", 1)[-1].split("?")[0]
    if "test" not in name:
        raise RuntimeError(f"refusing to use database {name!r}: not named like a test database")
    return _unconfined(url)


def _unconfined(url: str) -> str:
    """N5: the audit log's SECURITY DEFINER functions pin `search_path = public, pg_temp` (so no
    caller can hijack them), so they only work on tables in `public`. A DATABASE_URL confined to
    a private schema (`?options=-csearch_path=...`, as the API suite's conftest makes it) cannot
    run them. Such a URL gets a sibling test database of its own, created on first use, where the
    schema lives in `public` as in production."""
    from sqlalchemy.engine import make_url

    parsed = make_url(url)
    options = str(parsed.query.get("options") or "")
    if "search_path" not in options:
        return url
    target = f"{parsed.database}_s5pub"
    admin = create_engine(parsed.set(database="postgres", query={}), isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as conn:
            if not conn.execute(text("SELECT 1 FROM pg_database WHERE datname = :d"), {"d": target}).first():
                conn.execute(text(f'CREATE DATABASE "{target}"'))
    finally:
        admin.dispose()
    return parsed.set(database=target, query={}).render_as_string(hide_password=False)


@pytest.fixture(scope="session")
def s5_engine():
    url = database_url()
    if not url:
        pytest.skip("IDENTITY_S5_DATABASE_URL / DATABASE_URL not set (needs a test Postgres)")
    engine = create_engine(url, future=True)
    try:
        with engine.connect() as conn:
            ready = conn.execute(text("SELECT to_regclass('identity_security_events')")).scalar()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"test Postgres not reachable: {type(exc).__name__}")
    if not ready:
        from boss_engine.db import ensure_schema
        from boss_engine.config import get_settings
        ensure_schema(engine, get_settings())
    yield engine
    engine.dispose()


def _reset_process_state() -> None:
    migration.forget_seeded()
    legacy.forget_usage()
    legacy.forget_claimed_actor_notes()
    shadow_report.forget_flushed()
    with policy._cache_lock:
        policy._cache.clear()


@pytest.fixture()
def db(s5_engine):
    """One connection, one transaction, rolled back. Deferred constraint triggers fire at once."""
    _reset_process_state()
    conn = s5_engine.connect()
    trans = conn.begin()
    conn.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
    try:
        yield conn
    finally:
        trans.rollback()
        conn.close()
        _reset_process_state()


def settings(**overrides: Any) -> SimpleNamespace:
    """The settings the S5 code reads, without touching the process's real settings."""
    base = dict(
        default_org_id=ORG, default_project_id=PROJECT, worker_principal="system",
        legacy_console_api_key="", legacy_console_password="", legacy_console_auth="",
        api_key_principal_map={}, enterprise_scim_bearer_token_values=(),
        authz_mode="shadow", authz_router_modes="", trusted_proxies="127.0.0.0/8,::1/128",
        identity_shadow_flush_seconds=60, identity_audit_retention_days=400,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


# ---- worlds ------------------------------------------------------------------------------------

DEFAULT_MEMBERS = [  # members.py seed_default_members, verbatim capabilities
    ("ceo", "Founder / CEO", "human", ["ceo"], sorted(migration.ALL_CAPABILITIES)),
    ("cto", "Chief Technology Officer", "human", ["cto"],
     ["agent_chat", "decision_resolve", "audit_export", "health_refresh", "infra_provision",
      "providerops_manage", "github_manage", "integration_manage", "workflow_execute"]),
    ("cao", "Chief Architect", "human", ["cao"], ["decision_resolve", "audit_export",
                                                  "health_refresh"]),
    ("vp_agents", "VP Agents", "human", ["vp_agents"],
     ["role_review", "module_manage", "self_improvement_manage", "audit_export"]),
    ("release_manager", "Release Manager", "human", ["release_manager"],
     ["decision_resolve", "self_improvement_manage", "comms_manage", "health_refresh",
      "infra_provision", "github_manage", "integration_manage"]),
    ("metatron", "Metatron Operator", "system", ["metatron"],
     ["agent_chat", "command_submit", "health_refresh", "audit_export"]),
    ("coo", "COO Operator", "system", ["coo"], ["agent_chat", "health_refresh", "audit_export",
                                                "task_create"]),
    ("executive_console", "Executive Console Operator", "system", ["ceo", "ceo_delegate"],
     sorted(migration.ALL_CAPABILITIES - {"member_manage"})),
    ("system", "BOSS System Runtime", "system", ["system"], sorted(migration.ALL_CAPABILITIES)),
]


def base_world(db: Any, *, project: str = PROJECT) -> None:
    db.execute(text("INSERT INTO orgs (id, name) VALUES (:o, 'BOSS') ON CONFLICT DO NOTHING"),
               {"o": ORG})
    db.execute(text("INSERT INTO workspaces (id, org_id, name) VALUES ('ws_boss', :o, 'BOSS') "
                    "ON CONFLICT DO NOTHING"), {"o": ORG})
    db.execute(text("INSERT INTO projects (id, workspace_id, name, slug) VALUES (:p, 'ws_boss', "
                    ":p, :p) ON CONFLICT DO NOTHING"), {"p": project})


def add_member(db: Any, member_id: str, display: str, member_type: str, role_keys: list[str],
               caps: list[str], *, project: str = PROJECT, org_mirror: bool = True,
               status: str = "active") -> None:
    db.execute(text(
        "INSERT INTO project_members (id, project_id, member_id, display_name, member_type, "
        "role_keys, permissions, status) VALUES (:id, :p, :m, :d, :t, CAST(:r AS JSONB), "
        "CAST(:c AS JSONB), :s) ON CONFLICT DO NOTHING"),
        {"id": f"pm_{project}_{member_id}", "p": project, "m": member_id, "d": display,
         "t": member_type, "r": json.dumps(role_keys), "c": json.dumps(caps), "s": status})
    if org_mirror:
        db.execute(text(
            "INSERT INTO org_members (id, org_id, member_id, display_name, member_type, role_keys, "
            "permissions, status) VALUES (:id, :o, :m, :d, :t, CAST(:r AS JSONB), CAST(:c AS "
            "JSONB), :s) ON CONFLICT DO NOTHING"),
            {"id": f"om_{member_id}", "o": ORG, "m": member_id, "d": display, "t": member_type,
             "r": json.dumps(role_keys), "c": json.dumps(caps), "s": status})


def community_install(db: Any) -> None:
    """What every Community install has today: the org, the dogfood project, the default
    members (mirrored into org_members by older versions), and agent roles with Passport ids."""
    base_world(db)
    for member in DEFAULT_MEMBERS:
        add_member(db, *member)
    for i, (key, lifecycle) in enumerate((("forge", "active"), ("vera", "active"),
                                          ("atlas", "paused"))):
        db.execute(text(
            "INSERT INTO roles (id, project_id, role_key, display_name, agent_id, lifecycle) "
            "VALUES (:id, :p, :k, :d, :a, :l) ON CONFLICT DO NOTHING"),
            {"id": f"role_{key}", "p": PROJECT, "k": key, "d": key.title(),
             "a": f"agt_{key}{i:07d}", "l": lifecycle})


def add_break_glass(db: Any, member_id: str, caps: list[str], expires_at: datetime,
                    *, status: str = "active") -> str:
    gid = f"bg_{member_id}_{len(caps)}_{int(expires_at.timestamp())}"
    db.execute(text(
        "INSERT INTO break_glass_grants (id, project_id, target_member_id, requested_by, reason, "
        "granted_permissions, status, expires_at) VALUES (:id, :p, :m, 'ceo', 'incident', "
        "CAST(:c AS JSONB), :s, :e)"),
        {"id": gid, "p": PROJECT, "m": member_id, "c": json.dumps(caps), "s": status,
         "e": expires_at})
    return gid


def add_scim(db: Any) -> None:
    """A SCIM-provisioned person and group, as self_hosted_identity writes them."""
    member_id = f"scim_{PROJECT}_dana"
    db.execute(text(
        "INSERT INTO enterprise_scim_groups (id, org_id, project_id, external_id, display_name, "
        "role_keys, permissions, status) VALUES ('scimgrp_builders', :o, :p, 'ext-builders', "
        "'Builders', '[]', CAST(:c AS JSONB), 'active')"),
        {"o": ORG, "p": PROJECT, "c": json.dumps(["workflow_execute", "task_create"])})
    db.execute(text(
        "INSERT INTO enterprise_scim_users (id, org_id, project_id, external_id, user_name, "
        "display_name, member_id, role_keys, permissions, groups, raw_profile, status) VALUES "
        "('scimuser_dana', :o, :p, 'ext-dana', 'dana', 'Dana Builder', :m, '[]', CAST(:c AS "
        "JSONB), '[\"Builders\"]', CAST(:prof AS JSONB), 'active')"),
        {"o": ORG, "p": PROJECT, "m": member_id, "c": json.dumps(["workflow_execute"]),
         "prof": json.dumps({"emails": [{"value": "dana@example.com", "primary": True}]})})
    add_member(db, member_id, "Dana Builder", "human", [], ["workflow_execute", "task_create"],
               org_mirror=False)


# ---- requests as the guard builds them ----------------------------------------------------------

def creds(*, method: Optional[str] = None, member: Optional[str] = None, key: Optional[str] = None,
          ip: str = "100.101.102.103", peer: Optional[str] = None,
          headers: Optional[dict[str, str]] = None,
          cookies: Optional[dict[str, str]] = None) -> RequestCredentials:
    h = {"user-agent": "rehearsal/1.0"}
    if key:
        h["authorization"] = f"Bearer {key}"
    h.update(headers or {})
    return RequestCredentials(method="GET", path="/v1/projects", headers=h, cookies=cookies or {},
                              client_ip=ip, peer_ip=peer or ip, request_id="req_rehearsal",
                              legacy_member_id=member, legacy_auth_method=method)


# ---- the fakes of S1 and S2 ----------------------------------------------------------------------

@dataclass
class Claimed:
    principal_id: str
    ctx: AuthContext
    cutoff: datetime


def fake_claim(db: Any, *, name: str, email: str, via: str, now: datetime,
               passkey: bool = True, origin: Optional[str] = None,
               assume_member: Optional[str] = "ceo", cfg: Any = None) -> Claimed:
    """S1's claim (§4.11/§4.12): one transaction; S5's on_claimed is the last step."""
    pid = new_id("usr")
    migration.ensure_principal(db, pid, ORG, "user", "active", name, created_by="claim")
    db.execute(text("INSERT INTO identity_users (principal_id, email, email_verified_at) "
                    "VALUES (:p, :e, :t)"), {"p": pid, "e": email, "t": now})
    if passkey:
        db.execute(text(
            "INSERT INTO identity_webauthn_credentials (id, principal_id, credential_id, "
            "public_key, name) VALUES (:id, :p, :c, :k, 'This Mac')"),
            {"id": new_id("pky"), "p": pid, "c": os.urandom(16), "k": os.urandom(32)})
    migration.bind(db, ORG, pid, "arl_owner", "org", ORG, granted_by="claim",
                   reason="first Owner (claim)")
    session = SessionRef(id=new_id("ses"), mfa_at=now, auth_methods=("passkey",),
                         idle_expires_at=now + timedelta(hours=12),
                         absolute_expires_at=now + timedelta(days=7))
    principal = Principal(pid, PrincipalKind.USER, ORG, PrincipalStatus.ACTIVE, name)
    ctx = AuthContext(principal, AuthMethod.SESSION, session=session, ip="100.101.102.103",
                      user_agent="Safari", request_id="req_claim")
    already = db.execute(text("SELECT 1 FROM identity_security_events WHERE org_id = :o AND "
                              "event_type = 'bootstrap.claimed'"), {"o": ORG}).first()
    if already is None:   # an org is claimed once (F3/L4): a second "claim" is an added Owner
        audit.record(SecurityEvent.from_context(ctx, "bootstrap.claimed", Outcome.SUCCESS,
                                                target_type="principal", target_id=pid,
                                                details={"via": via, "origin": origin}), db=db)
    if origin:
        policy.put_policy(ORG, {"public_origin": origin}, db=db, actor=ctx)
    cutoff = migration.on_claimed(ORG, pid, db=db, now=now, via=via, assume_member=assume_member,
                                  settings=cfg)
    return Claimed(pid, ctx, cutoff)


def _system_set(principal_id: str, db: Any) -> Optional[frozenset[str]]:
    meta = SYSTEM_PRINCIPALS.get(principal_id)
    if meta is not None:
        return frozenset(ROLE_PERMISSIONS.get(meta.access_role, ()))
    return legacy.system_permissions(principal_id, db=db)


@dataclass
class Verdict:
    allowed: bool
    reason: str = ""
    via: list[str] = field(default_factory=list)


def fake_check(db: Any, ctx: AuthContext, permission: str, *, project: Optional[str] = None,
               now: datetime = NOW) -> Verdict:
    """§6.6, the parts that matter for migration: liveness, fixed sets, bindings (direct and via
    groups, org and project, unexpired), key scopes, step-up."""
    meta = PERMISSION_META[permission]
    row = db.execute(text("SELECT status FROM identity_principals WHERE id = :p"),
                     {"p": ctx.principal.id}).first()
    if row is None or row[0] != "active":
        return Verdict(False, "principal_inactive")
    if ctx.credential is not None:
        cred = db.execute(text("SELECT revoked_at FROM identity_api_credentials WHERE id = :i"),
                          {"i": ctx.credential.id}).first()
        if cred is None or cred[0] is not None:
            return Verdict(False, "credential_revoked")
    if ctx.principal.kind == PrincipalKind.SYSTEM:
        fixed = _system_set(ctx.principal.id, db) or frozenset()
        return Verdict(permission in fixed, "" if permission in fixed else "missing_permission")
    rows = db.execute(text(
        "SELECT ar.key, b.scope_type, b.scope_id, rp.permission_key FROM identity_role_bindings b "
        "JOIN identity_access_roles ar ON ar.id = b.role_id JOIN identity_access_role_permissions "
        "rp ON rp.role_id = b.role_id WHERE ((b.subject_type = 'principal' AND b.subject_id = :p) "
        "OR (b.subject_type = 'group' AND b.subject_id IN (SELECT group_id FROM "
        "identity_group_members WHERE principal_id = :p))) AND (b.expires_at IS NULL OR "
        "b.expires_at > :now) AND rp.permission_key = :perm"),
        {"p": ctx.principal.id, "now": now, "perm": permission}).all()
    via = [f"{r[0]}@{r[1]}" for r in rows
           if r[1] == "org" and meta.scope in ("org", "project")
           or (r[1] == "project" and meta.scope == "project" and r[2] == project)]
    if not via:
        return Verdict(False, "missing_permission")
    if ctx.credential is not None and permission not in ctx.credential.scopes:
        return Verdict(False, "outside_key_scopes")
    if meta.step_up and not legacy.step_up_exempt(ctx):
        fresh = ctx.session is not None and ctx.session.mfa_at is not None \
            and now - ctx.session.mfa_at <= timedelta(minutes=10)
        if not fresh:
            return Verdict(False, "step_up_required")
    return Verdict(True, via=via)


def events(db: Any, event_type: Optional[str] = None) -> list[dict[str, Any]]:
    q = "SELECT * FROM identity_security_events WHERE org_id = :o"
    params: dict[str, Any] = {"o": ORG}
    if event_type:
        q += " AND event_type = :t"
        params["t"] = event_type
    return [dict(r) for r in db.execute(text(q + " ORDER BY seq"), params).mappings().all()]


class written_days_ago:
    """Events recorded inside this block carry a timestamp `days` in the past (the audit log's
    prune floor keeps anything younger than 30 days). The chain hashes the timestamp the row
    really has, so the chain still verifies — this moves the writer's clock, not the rows."""

    def __init__(self, days: int) -> None:
        self.days = days

    def __enter__(self):
        shift = timedelta(days=self.days)
        self._saved = audit.clock
        audit.clock = lambda: datetime.now(timezone.utc) - shift
        return self

    def __exit__(self, *exc):
        audit.clock = self._saved
        return False
