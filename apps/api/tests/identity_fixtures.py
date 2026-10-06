"""Identity test fixtures, shared by every stream (IDENTITY_BUILD.md §5: owned by S2).

    world = IdentityWorld.create(engine)            # its own org, workspace and two projects
    alice = world.person("alice")                    # an active user principal
    world.bind(alice, "operator", project=world.project_a)
    with world.acting_as(world.ctx(alice)):          # the guard authenticates every request as alice
        client.get(...)

Each world uses fresh ids, so tests never see each other's principals or bindings, and the
fixtures never touch the shared seeded org. `acting_as` replaces only the AUTHENTICATION step
(stream S1's `authn.authenticate`, which is not built yet) — scope resolution, the PDP and the
handlers run for real.
"""
from __future__ import annotations

import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator, Optional

from sqlalchemy import text

from boss_engine.identity import authz
from boss_engine.identity.principal import (
    ActorChain,
    AgentInstance,
    AuthContext,
    AuthMethod,
    CredentialRef,
    Principal,
    PrincipalKind,
    PrincipalStatus,
    RunMode,
    RunTokenClaims,
    SessionRef,
    TaskBinding,
)

ROLE_IDS = {k: f"arl_{k}" for k in ("owner", "admin", "operator", "viewer", "auditor")}


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


@dataclass
class IdentityWorld:
    engine: Any
    org: str
    workspace: str
    project_a: str
    project_b: str
    principals: dict[str, Principal] = field(default_factory=dict)

    # ---- construction ------------------------------------------------------------------------
    @classmethod
    def create(cls, engine: Any) -> "IdentityWorld":
        tag = uuid.uuid4().hex[:10]
        world = cls(engine, f"org_t{tag}", f"ws_t{tag}", f"pa-{tag}", f"pb-{tag}")
        with engine.begin() as db:
            db.execute(text("INSERT INTO orgs (id, name) VALUES (:o, :o)"), {"o": world.org})
            db.execute(text("INSERT INTO workspaces (id, org_id, name) VALUES (:w, :o, :w)"),
                       {"w": world.workspace, "o": world.org})
            for pid in (world.project_a, world.project_b):
                db.execute(text("INSERT INTO projects (id, workspace_id, name, slug) "
                                "VALUES (:p, :w, :p, :p)"), {"p": pid, "w": world.workspace})
        return world

    def principal(self, name: str, *, kind: PrincipalKind = PrincipalKind.USER,
                  status: PrincipalStatus = PrincipalStatus.ACTIVE, network_bound: bool = False,
                  principal_id: Optional[str] = None) -> Principal:
        prefix = {PrincipalKind.USER: "usr", PrincipalKind.SERVICE_ACCOUNT: "sva",
                  PrincipalKind.AGENT: "agt", PrincipalKind.SYSTEM: "sys"}[kind]
        pid = principal_id or _id(prefix)
        with self.engine.begin() as db:
            db.execute(text(
                "INSERT INTO identity_principals (id, org_id, kind, status, display_name, network_bound) "
                "VALUES (:id, :o, :k, :s, :n, :nb)"),
                {"id": pid, "o": self.org, "k": kind.value, "s": status.value, "n": name,
                 "nb": network_bound})
        principal = Principal(pid, kind, self.org, status, name, network_bound)
        self.principals[name] = principal
        return principal

    def person(self, name: str, **kw: Any) -> Principal:
        return self.principal(name, kind=PrincipalKind.USER, **kw)

    def agent(self, name: str, *, ceiling: tuple[str, ...] = (), sponsor: Optional[Principal] = None,
              can_spawn: bool = False, allowed_children: tuple[str, ...] = (),
              max_depth: int = 3) -> Principal:
        agent = self.principal(name, kind=PrincipalKind.AGENT)
        with self.engine.begin() as db:
            db.execute(text(
                "INSERT INTO identity_agent_passports (agent_id, role_key, sponsor_principal_id, "
                "can_spawn, allowed_children, max_depth) VALUES (:a, :r, :s, :c, :ch, :d)"),
                {"a": agent.id, "r": name, "s": (sponsor.id if sponsor else agent.id), "c": can_spawn,
                 "ch": list(allowed_children), "d": max_depth})
            for key in ceiling:
                db.execute(text("INSERT INTO identity_agent_ceilings (agent_id, permission_key) "
                                "VALUES (:a, :k)"), {"a": agent.id, "k": key})
        return agent

    def bind(self, subject: Principal | str, role: str, *, project: Optional[str] = None,
             subject_type: str = "principal") -> str:
        """A binding written directly (no grant rules): test setup, not the API under test."""
        binding_id = _id("rbd")
        role_id = ROLE_IDS.get(role, role)
        with self.engine.begin() as db:
            db.execute(text(
                "INSERT INTO identity_role_bindings (id, org_id, subject_type, subject_id, role_id, "
                "scope_type, scope_id, granted_by) VALUES (:id, :o, :st, :s, :r, :t, :i, 'test')"),
                {"id": binding_id, "o": self.org, "st": subject_type,
                 "s": subject if isinstance(subject, str) else subject.id, "r": role_id,
                 "t": "project" if project else "org", "i": project or self.org})
            authz.bump_epoch(self.org, db=db, reason="test.bind")
        return binding_id

    def custom_role(self, key: str, permissions: tuple[str, ...] | list[str]) -> str:
        """A custom access role in this org holding exactly `permissions` (test setup: no grant
        ceiling). Returns its id, which `bind` takes as the role."""
        role_id = _id("arl")
        with self.engine.begin() as db:
            db.execute(text(
                "INSERT INTO identity_access_roles (id, org_id, key, name, builtin, bindable_scopes) "
                "VALUES (:id, :o, :k, :k, false, ARRAY['org','project'])"),
                {"id": role_id, "o": self.org, "k": f"{key}-{role_id[-6:]}"})
            for permission in permissions:
                db.execute(text("INSERT INTO identity_access_role_permissions (role_id, permission_key) "
                                "VALUES (:r, :p)"), {"r": role_id, "p": permission})
            authz.bump_epoch(self.org, db=db, reason="test.custom_role")
        return role_id

    def group(self, key: str, *, members: tuple[Principal, ...] = (), source: str = "manual") -> str:
        gid = _id("grp")
        with self.engine.begin() as db:
            db.execute(text("INSERT INTO identity_groups (id, org_id, key, name, source) "
                            "VALUES (:g, :o, :k, :k, :s)"), {"g": gid, "o": self.org, "k": key, "s": source})
            for m in members:
                db.execute(text("INSERT INTO identity_group_members (group_id, principal_id, source) "
                                "VALUES (:g, :p, :s)"), {"g": gid, "p": m.id, "s": source})
            authz.bump_epoch(self.org, db=db, reason="test.group")
        return gid

    def claimed(self) -> None:
        """Record bootstrap.claimed, which arms the DB's last-Owner trigger (amendment A9)."""
        with self.engine.begin() as db:
            db.execute(text(
                "INSERT INTO identity_security_events (org_id, seq, stream, event_type, outcome, "
                "prev_hash, hash) VALUES (:o, 1, 'security', 'bootstrap.claimed', 'success', "
                "'\\x00'::bytea, '\\x00'::bytea) ON CONFLICT DO NOTHING"), {"o": self.org})

    # ---- contexts ------------------------------------------------------------------------------
    def ctx(self, principal: Principal, *, mfa: bool = True,
            method: AuthMethod = AuthMethod.SESSION) -> AuthContext:
        now = datetime.now(timezone.utc)
        session = SessionRef(id=_id("ses"), mfa_at=now if mfa else None, auth_methods=("password",),
                             idle_expires_at=now + timedelta(hours=1),
                             absolute_expires_at=now + timedelta(days=1))
        return AuthContext(principal, method, session=session if method == AuthMethod.SESSION else None)

    def key_ctx(self, principal: Principal, scopes: tuple[str, ...], *,
                project_ids: Optional[tuple[str, ...]] = None, revoked: bool = False) -> AuthContext:
        key_id = _id("key")
        with self.engine.begin() as db:
            db.execute(text(
                "INSERT INTO identity_api_credentials (id, principal_id, kind, name, token_hash, scopes, "
                "project_ids, created_by, revoked_at) VALUES (:id, :p, 'pat', 'test', :h, :s, :pj, 'test', "
                ":rv)"), {"id": key_id, "p": principal.id, "h": uuid.uuid4().hex, "s": list(scopes),
                          "pj": list(project_ids) if project_ids is not None else None,
                          "rv": datetime.now(timezone.utc) if revoked else None})
        cred = CredentialRef(id=key_id, kind="pat", principal_id=principal.id, scopes=frozenset(scopes),
                             project_ids=frozenset(project_ids) if project_ids is not None else None)
        return AuthContext(principal, AuthMethod.API_KEY, credential=cred)

    def run_ctx(self, person: Principal, chain: tuple[Principal, ...], *, grant_permissions: tuple[str, ...],
                token_scope: tuple[str, ...], project_id: Optional[str] = None,
                grant_status: str = "active", allowed_children: tuple[str, ...] = (),
                allow_subdelegation: bool = False, mode: RunMode = RunMode.OBO) -> AuthContext:
        """An agent run acting for `person` (OBO): the jti ledger row, the grant and its
        permissions are real rows, as the token service would write them."""
        grant_id, jti, run_id = _id("dlg"), _id("rt"), _id("run")
        hops = tuple(AgentInstance(a.id, f"{run_id}{i}") for i, a in enumerate(chain))
        now = datetime.now(timezone.utc)
        with self.engine.begin() as db:
            db.execute(text(
                "INSERT INTO identity_delegation_grants (id, org_id, user_principal_id, agent_ids, "
                "project_ids, status, allow_subdelegation, allowed_children) VALUES "
                "(:g, :o, :u, :a, :p, :s, :sub, :ch)"),
                # A grant always names its projects (grants.create refuses none): a run with no
                # task project is granted every project of this world.
                {"g": grant_id, "o": self.org, "u": person.id, "a": [a.id for a in chain],
                 "p": [project_id] if project_id else [self.project_a, self.project_b],
                 "s": grant_status,
                 "sub": allow_subdelegation, "ch": list(allowed_children)})
            for key in grant_permissions:
                db.execute(text("INSERT INTO identity_delegation_grant_permissions (grant_id, permission_key) "
                                "VALUES (:g, :k)"), {"g": grant_id, "k": key})
            db.execute(text(
                "INSERT INTO identity_run_credentials (jti, org_id, run_id, grant_id, subject_principal_id, "
                "mode, chain, depth, aud, scope_hash, cnf_jkt, issued_at, expires_at) VALUES "
                "(:j, :o, :r, :g, :s, :m, CAST(:c AS jsonb), :d, 'boss:api', 'x', 'jkt', :i, :e)"),
                {"j": jti, "o": self.org, "r": run_id, "g": grant_id,
                 "s": person.id if mode == RunMode.OBO else chain[-1].id, "m": mode.value,
                 "c": "[" + ",".join(f'"{h.sub}"' for h in hops) + "]", "d": len(hops),
                 "i": now, "e": now + timedelta(minutes=5)})
        claims = RunTokenClaims(
            iss="boss", aud="boss:api", sub=person.id if mode == RunMode.OBO else chain[-1].id,
            org_id=self.org, mode=mode, chain=ActorChain(hops), grant_id=grant_id,
            task=TaskBinding(run_id=run_id, project_id=project_id), scope=frozenset(token_scope),
            cnf_jkt="jkt", jti=jti, parent_jti=None, iat=int(now.timestamp()),
            exp=int((now + timedelta(minutes=5)).timestamp()))
        actor = chain[-1] if mode == RunMode.AUTONOMOUS else person
        return AuthContext(actor, AuthMethod.RUN_TOKEN, run_token=claims)

    # ---- the request seam ------------------------------------------------------------------------
    @contextmanager
    def acting_as(self, ctx: Optional[AuthContext], *, mode: str = "enforce") -> Iterator[None]:
        """Every request (and every handler's request_context) authenticates as `ctx`, and the
        guard runs in `mode`. Restores both on exit."""
        from apps.api.app import identity_guard
        from boss_engine.config import get_settings

        settings = get_settings()
        saved = (identity_guard._authenticate, settings.authz_mode, settings.authz_router_modes)
        def fake(creds, conn=None):
            if conn is not None and ctx is not None:
                conn.state.identity = ctx     # what the real guard keeps for the handler
            return ctx

        identity_guard._authenticate = fake
        settings.authz_mode, settings.authz_router_modes = mode, ""
        try:
            yield
        finally:
            identity_guard._authenticate, settings.authz_mode, settings.authz_router_modes = saved
