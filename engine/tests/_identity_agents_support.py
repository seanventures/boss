"""Shared support for S3's identity tests (agents, delegation, tokens, vault, egress).

A throwaway Postgres holds ONLY the identity block of db/schema.sql (plus the generated catalogue
seed and a few stub tables the cascade halts), so these tests exercise the real tables, triggers
and constraints without a full stack. The URL must name a disposable database (db_guard rule);
without BOSS_IDENTITY_TEST_DATABASE_URL every DB test is skipped.

The other streams' functions (S2's authz, S5's audit and policy, S1's rate limiter) are FAKED here
through their frozen signatures, as IDENTITY_BUILD.md §3 prescribes: plain functions, `db=` in.
"""
from __future__ import annotations

import base64
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import pytest

ROOT = Path(__file__).resolve().parents[2]
DB_ENV = "BOSS_IDENTITY_TEST_DATABASE_URL"
MASTER_KEY = base64.b64encode(b"k" * 32).decode()
ISSUER = "https://boss.test"
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
BASE = "https://boss.test"


def database_url() -> Optional[str]:
    url = os.environ.get(DB_ENV)
    if not url:
        return None
    name = url.rsplit("/", 1)[-1].split("?")[0].lower()
    if not any(m in name for m in ("test", "scratch", "tmp", "ci")):
        raise RuntimeError(f"{DB_ENV} must name a disposable database")
    return url


def identity_block() -> str:
    sql = (ROOT / "db" / "schema.sql").read_text(encoding="utf-8")
    start = sql.index("-- BOSS IDENTITY (docs/product/IDENTITY.md")
    end = sql.index("-- ================================ end BOSS IDENTITY")
    return sql[start:end]


STUBS = """
CREATE TABLE IF NOT EXISTS orgs (id TEXT PRIMARY KEY, name TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS workspaces (id TEXT PRIMARY KEY, org_id TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS projects (id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS roles (id TEXT PRIMARY KEY, role_key TEXT, agent_id TEXT, born_by TEXT,
  project_id TEXT, display_name TEXT, sponsor TEXT, spec JSONB);
ALTER TABLE roles ADD COLUMN IF NOT EXISTS spec JSONB;
CREATE TABLE IF NOT EXISTS code_task_runs (id TEXT PRIMARY KEY, project_id TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'running');
ALTER TABLE code_task_runs ADD COLUMN IF NOT EXISTS spec JSONB NOT NULL DEFAULT '{}'::jsonb;
CREATE TABLE IF NOT EXISTS code_task_stop_requests (run_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, actor TEXT NOT NULL,
  reason TEXT, phase TEXT, requested_at TIMESTAMPTZ NOT NULL DEFAULT now(), honoured_at TIMESTAMPTZ);
CREATE TABLE IF NOT EXISTS workflow_runs (id TEXT PRIMARY KEY, status TEXT NOT NULL, paused_at TIMESTAMPTZ,
  blocked_reason TEXT, updated_at TIMESTAMPTZ, policy_snapshot JSONB NOT NULL DEFAULT '{}'::jsonb);
CREATE TABLE IF NOT EXISTS workflow_schedules (id TEXT PRIMARY KEY, enabled BOOLEAN NOT NULL DEFAULT true,
  input JSONB NOT NULL DEFAULT '{}'::jsonb, last_error TEXT, updated_at TIMESTAMPTZ);
CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, project_id TEXT, kind TEXT, payload JSONB NOT NULL DEFAULT '{}'::jsonb,
  status TEXT NOT NULL DEFAULT 'queued', last_error JSONB, updated_at TIMESTAMPTZ);
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp();
"""

_TRUNCATE = ("identity_security_events", "identity_egress_log", "identity_egress_credentials", "identity_dpop_replay",
             "identity_run_credentials", "identity_delegation_grant_permissions", "identity_delegation_grants",
             "identity_agent_ceilings", "identity_agent_passports", "identity_signing_keys", "identity_vault_items",
             "identity_vault_keks", "identity_sessions", "identity_api_credentials", "identity_principals",
             "code_task_stop_requests", "code_task_runs", "workflow_runs", "workflow_schedules", "roles", "jobs",
             "identity_legacy_mappings", "projects", "workspaces", "identity_revocation_notices", "identity_rate_limits",
             "identity_github_app", "identity_pending_consents")

_schema_ready = False


def make_engine():
    from sqlalchemy import create_engine

    url = database_url()
    if url is None:
        pytest.skip(f"{DB_ENV} not set (a throwaway Postgres)")
    return create_engine(url.replace("postgresql://", "postgresql+psycopg://"), pool_size=8, max_overflow=4)


def ensure_schema(engine) -> None:
    global _schema_ready
    if _schema_ready:
        return
    from boss_engine.db import split_statements

    seed = (ROOT / "db" / "identity_seed.sql").read_text(encoding="utf-8")
    raw = engine.raw_connection()
    try:
        with raw.cursor() as cur:
            for stmt in split_statements(STUBS) + split_statements(identity_block()) + split_statements(seed):
                cur.execute(stmt)
        raw.commit()
    finally:
        raw.close()
    _schema_ready = True


def reset(engine) -> None:
    from sqlalchemy import text

    with engine.begin() as c:
        c.execute(text("SET LOCAL session_replication_role = replica"))   # bypass append-only / last-owner triggers
        c.execute(text("TRUNCATE " + ", ".join(_TRUNCATE) + " CASCADE"))
        c.execute(text("INSERT INTO orgs (id, name) VALUES ('org_boss','BOSS'), ('org_other','Other') "
                       "ON CONFLICT DO NOTHING"))


# ---- fakes of the other streams ------------------------------------------------------------------------

@dataclass
class World:
    """Who holds what. `perms[(principal, project or None)]` -> permissions."""
    perms: dict[tuple[str, Optional[str]], set[str]] = field(default_factory=dict)
    events: list[Any] = field(default_factory=list)
    policy_overrides: dict[str, Any] = field(default_factory=dict)

    def grant(self, principal: str, perms, project: Optional[str] = None) -> None:
        self.perms.setdefault((principal, project), set()).update(perms)

    def revoke(self, principal: str, perms, project: Optional[str] = None) -> None:
        self.perms.setdefault((principal, project), set()).difference_update(perms)

    def held(self, principal: str, project: Optional[str]) -> frozenset[str]:
        return frozenset(self.perms.get((principal, None), set()) | (self.perms.get((principal, project), set())
                                                                     if project else set()))

    def types(self) -> list[str]:
        return [e.event_type for e in self.events]


def install_fakes(monkeypatch, world: World) -> None:
    from boss_engine.identity import audit, authz, policy
    from boss_engine.identity.authz import Decision, DenyReason, EffectivePermissions

    def record(event, *, db):
        assert event.event_type in audit.EVENT_TYPES, event.event_type
        for k in (event.details or {}):
            assert not re.search(r"token|secret|password|credential", k), k
        world.events.append(event)
        return audit.RecordedEvent(id=len(world.events), seq=len(world.events), hash=b"x")

    def effective_permissions(subject, scope, *, db=None, with_why=False):
        pid = subject.id if hasattr(subject, "id") else subject.principal.id
        return EffectivePermissions(permissions=world.held(pid, scope.project_id), scope=scope, epoch=0)

    def check(ctx, permission, scope, *, db=None):
        if ctx.run_token is not None:
            from boss_engine.identity import tokens

            return tokens.decide(ctx.run_token, permission, scope, db=db)
        if permission in world.held(ctx.principal.id, scope.project_id):
            return Decision.allow(permission, scope)
        return Decision.deny(permission, scope, DenyReason.MISSING_PERMISSION)

    def get_policy(org_id, *, db):
        return policy.IdentityPolicy(**world.policy_overrides)

    monkeypatch.setattr(audit, "record", record)
    monkeypatch.setattr(authz, "effective_permissions", effective_permissions)
    monkeypatch.setattr(authz, "check", check)
    monkeypatch.setattr(authz, "bump_epoch", lambda org_id, *, db, reason: 1)
    monkeypatch.setattr(policy, "get_policy", get_policy)


# ---- people, agents, contexts ------------------------------------------------------------------------

def add_person(c, pid: str, *, org: str = "org_boss", status: str = "active", name: Optional[str] = None) -> None:
    from sqlalchemy import text

    c.execute(text("INSERT INTO identity_principals (id, org_id, kind, status, display_name) VALUES (:i, :o, 'user', :s, :n)"),
              {"i": pid, "o": org, "s": status, "n": name or pid})


def session_ctx(pid: str, org: str = "org_boss"):
    from datetime import datetime, timedelta, timezone

    from boss_engine.identity import AuthContext, AuthMethod, Principal, PrincipalKind, PrincipalStatus, SessionRef

    now = datetime.now(timezone.utc)
    return AuthContext(Principal(pid, PrincipalKind.USER, org, PrincipalStatus.ACTIVE, pid), AuthMethod.SESSION,
                       session=SessionRef("ses_test", now, ("pwd", "totp"), now + timedelta(hours=1),
                                          now + timedelta(days=1)))


def agent_ctx(aid: str, org: str = "org_boss"):
    from boss_engine.identity import AuthContext, AuthMethod, Principal, PrincipalKind, PrincipalStatus

    return AuthContext(Principal(aid, PrincipalKind.AGENT, org, PrincipalStatus.ACTIVE, aid), AuthMethod.SYSTEM)


BUILDER = ("code_task:read", "code_task:create", "code_task:steer", "repository:read", "github:read",
           "github:sync", "preview:read", "preview:manage")


# ---- the standard world: Alice (operator at prj_web), Atlas (planner) -> Forge (builder) -> Vera --------

@dataclass
class Env:
    engine: Any
    world: World
    alice: str = "usr_alice"
    bob: str = "usr_bob"
    atlas: str = "agt_atlas000001"
    forge: str = "agt_forge000001"
    vera: str = "agt_vera0000001"
    rook: str = "agt_rook0000001"
    project: str = "prj_web"

    def db(self):
        return self.engine.begin()

    def grant(self, *, agents=None, permissions=BUILDER, children=("agt_forge000001", "agt_vera0000001"),
              subdelegation=True, max_depth=3, repos=None, gh=None, task_run_id=None, person=None,
              expires_at=None):
        from boss_engine.identity import delegation

        resources = {}
        if repos is not None:
            resources["github"] = {"repos": list(repos), **(gh or {"contents": "write", "pull_requests": "write"})}
        req = delegation.GrantRequest(agent_ids=tuple(agents or (self.atlas,)), permissions=frozenset(permissions),
                                      project_ids=(self.project,), resources=resources,
                                      allow_subdelegation=subdelegation, allowed_children=tuple(children),
                                      max_depth=max_depth, task_run_id=task_run_id, expires_at=expires_at,
                                      purpose="test")
        with self.db() as c:
            return delegation.create_grant(session_ctx(person or self.alice), req, db=c)


def build_env(monkeypatch) -> Env:
    from sqlalchemy import text

    from boss_engine.identity import cascade, github_app, grants, passports, signing_keys, vault

    monkeypatch.setenv("BOSS_MASTER_KEY", MASTER_KEY)
    monkeypatch.delenv("BOSS_MASTER_KEY_FILE", raising=False)
    monkeypatch.setenv("BOSS_PUBLIC_URL", ISSUER)
    vault.reset_master_key_cache()
    signing_keys.clear_cache()
    grants.clear_guard_cache()
    monkeypatch.setattr(cascade, "AFTER_COMMIT", [])
    monkeypatch.setattr(github_app, "_RUN_TOKENS", {})
    engine = make_engine()
    ensure_schema(engine)
    reset(engine)
    world = World()
    install_fakes(monkeypatch, world)
    env = Env(engine, world)
    with engine.begin() as c:
        vault.check_master_key(db=c)
        signing_keys.provision(org_id="org_boss", db=c)
        add_person(c, env.alice, name="Alice")
        add_person(c, "usr_mallory", org="org_other")
        world.grant(env.alice, set(BUILDER) | {"delegation:grant", "agent_passport:read", "workflow_run:read",
                                               "code_task:retry", "meeting:read", "decision:read"}, env.project)
        world.grant(env.alice, {"delegation:grant"})
        passports.ensure_passport(org_id="org_boss", agent_id=env.atlas, role_key="atlas", display_name="Atlas",
                                  sponsor_principal_id=env.alice, db=c, can_spawn=True,
                                  allowed_children=("forge", "vera"))
        passports.ensure_passport(org_id="org_boss", agent_id=env.forge, role_key="forge", display_name="Forge",
                                  sponsor_principal_id=env.alice, db=c, can_spawn=True, allowed_children=("vera",))
        passports.ensure_passport(org_id="org_boss", agent_id=env.vera, role_key="vera", display_name="Vera",
                                  sponsor_principal_id=env.alice, db=c, can_spawn=True,
                                  allowed_children=("atlas", "rook"))
        passports.ensure_passport(org_id="org_boss", agent_id=env.rook, role_key="rook", display_name="Rook",
                                  sponsor_principal_id=env.alice, db=c)
        # Atlas is a planner; let it carry the builder abilities it hands to Forge in these tests.
        for key in BUILDER:
            c.execute(text("INSERT INTO identity_agent_ceilings VALUES (:a, :k) ON CONFLICT DO NOTHING"),
                      {"a": env.atlas, "k": key})
        for key in ("code_task:read", "code_task:steer", "repository:read", "github:read", "preview:read"):
            c.execute(text("INSERT INTO identity_agent_ceilings VALUES (:a, :k) ON CONFLICT DO NOTHING"),
                      {"a": env.rook, "k": key})
    return env


def start(env: Env, grant_id: str, *, agent=None, run="run_ct:task1", scope=BUILDER, repos=(), audiences=None,
          person=None, github_needs=None):
    from boss_engine.identity import runs
    from boss_engine.identity.delegation import AUD_API

    with env.db() as c:
        return runs.start_run(session_ctx(person or env.alice),
                              runs.RunStart(run_id=run, agent_id=agent or env.atlas, project_id=env.project,
                                            repos=tuple(repos), scope=frozenset(scope),
                                            github_needs=github_needs),
                              grant_id=grant_id, db=c, audiences=audiences or (AUD_API,))


def verify(env: Env, shim, *, audience="boss:api", method="GET", url=BASE + "/v1/code-tasks", token=None, proof=None):
    from boss_engine.identity import tokens

    tok = token if token is not None else shim.tokens[audience].access_token
    p = proof if proof is not None else shim.key.proof(method, url, access_token=tok)
    with env.db() as c:
        return tokens.verify_run_token(tok, audience=audience, dpop_proof=p, htm=method, htu=url, db=c)


def spawn(env: Env, parent, child_agent: str, name: str, scope=BUILDER):
    from boss_engine.identity import runs

    with env.db() as c:
        return runs.spawn_child(parent, child_agent_id=child_agent, child_name=name, scope=frozenset(scope),
                                db=c, base_url=BASE)


# ---- a GitHub that records every call ----------------------------------------------------------------

class FakeGitHub:
    """Stands in for api.github.com and github.com. Records (method, url, headers, body)."""

    def __init__(self):
        import itertools

        self.calls: list[tuple[str, str, dict, bytes]] = []
        self.user_repo_perms: dict[tuple[str, str], dict] = {}     # (user token, repo) -> permissions
        self.issued: list[str] = []
        self.deleted: list[str] = []
        self.refreshes = 0
        self.rotating_refresh = itertools.count(1)
        self.compare_status = "ahead"
        self.mint_status = 201
        #: The remote's git objects the egress push check may ask about: commit -> tree, and
        #: tree -> [{"mode", "path", "sha"}]. An unknown commit's tree is the empty tree.
        self.remote_commits: dict[str, str] = {}
        self.remote_trees: dict[str, list[dict]] = {EMPTY_TREE: []}
        self.remote_blobs: dict[str, bytes] = {}
        self.pull_heads: dict[int, str] = {}

    def __call__(self, method, url, headers, body):
        import json as _json
        from datetime import datetime, timedelta, timezone

        from boss_engine.identity.github_app import Response

        self.calls.append((method, url, dict(headers), body or b""))
        auth = headers.get("Authorization", "")
        if method == "POST" and "/access_tokens" in url:
            if self.mint_status != 201:
                return Response(self.mint_status, b'{"message":"permissions requested are not granted"}')
            tok = f"ghs_FAKEINSTALLATION{len(self.issued):04d}aaaaaaaaaaaaaaaa"
            self.issued.append(tok)
            exp = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat().replace("+00:00", "Z")
            return Response(201, _json.dumps({"token": tok, "expires_at": exp}).encode())
        if method == "DELETE" and url.endswith("/installation/token"):
            self.deleted.append(auth.split(" ", 1)[1])
            return Response(204, b"")
        if method == "GET" and "/git/commits/" in url:
            sha = url.rsplit("/", 1)[1]
            return Response(200, _json.dumps({"sha": sha, "tree": {"sha": self.remote_commits.get(sha, EMPTY_TREE)}}).encode())
        if method == "GET" and "/git/trees/" in url:
            sha = url.rsplit("/", 1)[1]
            if sha not in self.remote_trees:
                return Response(404, b"{}")
            return Response(200, _json.dumps({"sha": sha, "tree": self.remote_trees[sha], "truncated": False}).encode())
        if method == "GET" and "/git/blobs/" in url:
            import base64 as _b64

            sha = url.rsplit("/", 1)[1]
            if sha not in self.remote_blobs:
                return Response(404, b"{}")
            return Response(200, _json.dumps({"sha": sha, "encoding": "base64",
                                              "content": _b64.b64encode(self.remote_blobs[sha]).decode()}).encode())
        if method == "GET" and "/pulls/" in url:
            n = int(url.rsplit("/", 1)[1])
            repo = url.split("/repos/", 1)[1].rsplit("/pulls/", 1)[0]
            if n not in self.pull_heads:
                return Response(404, b"{}")
            return Response(200, _json.dumps({"number": n, "head": {"ref": self.pull_heads[n],
                                                                    "repo": {"full_name": repo}}}).encode())
        if method == "GET" and "/compare/" in url:
            return Response(200, _json.dumps({"status": self.compare_status}).encode())
        if method == "GET" and "/repos/" in url:
            repo = url.split("/repos/", 1)[1]
            token = auth.split(" ", 1)[1]
            perms = self.user_repo_perms.get((token, repo))
            return Response(200 if perms is not None else 404, _json.dumps({"permissions": perms or {}}).encode())
        if "login/oauth/access_token" in url:
            import time as _t

            self.refreshes += 1
            _t.sleep(0.3)                    # a slow provider makes a race likely without the lock
            n = next(self.rotating_refresh)
            return Response(200, _json.dumps({"access_token": f"gho_NEW{n}", "refresh_token": f"ghr_NEW{n}",
                                              "expires_in": 28800}).encode())
        if method == "DELETE" and "/applications/" in url:
            return Response(204, b"")
        return Response(404, b"{}")

    def urls(self, method=None):
        return [u for m, u, _, _ in self.calls if method is None or m == method]


def install_github(monkeypatch, env: Env) -> FakeGitHub:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    from boss_engine.identity import github_app, vault

    gh = FakeGitHub()
    monkeypatch.setattr(github_app, "TRANSPORT", gh)
    monkeypatch.setenv("BOSS_GITHUB_APP_ID", "12345")
    monkeypatch.setenv("BOSS_GITHUB_APP_INSTALLATION_ID", "999")
    monkeypatch.setenv("BOSS_GITHUB_APP_CLIENT_ID", "Iv1.test")
    monkeypatch.setenv("BOSS_GITHUB_APP_CLIENT_SECRET", "client-secret-test")
    monkeypatch.setitem(vault.REFRESHERS, "github", github_app._oauth_refresh)
    monkeypatch.setitem(vault.REVOKERS, "github", github_app._oauth_revoke)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption()).decode()
    with env.db() as c:
        vault.store(org_id="org_boss", owner_principal_id=None, kind=vault.VaultItemKind.APP_PRIVATE_KEY,
                    provider="github", secret=pem, db=c)
    return gh


def link_github(env: Env, person: str, token: str, *, refresh: str = "ghr_OLD", expires_in_s: int = 3600) -> str:
    from datetime import datetime, timedelta, timezone

    from boss_engine.identity import vault

    with env.db() as c:
        return vault.store(org_id="org_boss", owner_principal_id=person, kind=vault.VaultItemKind.OAUTH_USER,
                           provider="github", secret=token, refresh=refresh,
                           access_expires_at=datetime.now(timezone.utc) + timedelta(seconds=expires_in_s), db=c)
