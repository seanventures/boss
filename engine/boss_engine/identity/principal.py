"""Who is acting: Principal, the agent ActorChain with RFC 8693 `act` nesting, and AuthContext.

FROZEN IN PHASE 0 (docs/product/IDENTITY_BUILD.md). Every stream builds against these types; a change
needs a written amendment in IDENTITY_BUILD.md §7.

A request has exactly one AuthContext, built by `identity.authn.authenticate` from ONE verified
credential (a session cookie, an API key, or a run token) — never from a body field, a query
parameter or a client-set header (IDENTITY.md §2). Handlers receive it; they never construct one.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Mapping, Optional


class PrincipalKind(StrEnum):
    USER = "user"                        # a person: usr_
    SERVICE_ACCOUNT = "service_account"  # sva_
    AGENT = "agent"                      # agt_ (the Passport; instances live only in tokens)
    SYSTEM = "system"                    # sys_worker, sys_collector, sys_scim_*, sys_host_cli


class PrincipalStatus(StrEnum):
    PENDING = "pending"
    ACTIVE = "active"
    DISABLED = "disabled"     # people and service accounts
    SUSPENDED = "suspended"   # agents
    REVOKED = "revoked"       # terminal, for everyone


ID_PREFIX = {
    PrincipalKind.USER: "usr_",
    PrincipalKind.SERVICE_ACCOUNT: "sva_",
    PrincipalKind.AGENT: "agt_",
    PrincipalKind.SYSTEM: "sys_",
}


class AuthMethod(StrEnum):
    """How the request's credential was presented (stored on sessions and audit rows)."""
    SESSION = "session"            # __Host-boss_session cookie
    API_KEY = "api_key"            # boss_pat_ / boss_sak_ / boss_scim_
    RUN_TOKEN = "run_token"        # boss-run+jwt with DPoP
    LEGACY_PASSWORD = "legacy_password"   # the shared console password (bridge, operator decision 1)
    LEGACY_API_KEY = "legacy_api_key"     # BOSS_API_KEY / BOSS_API_KEYS (bridge)
    NETWORK = "network"            # BOSS_CONSOLE_AUTH=network (bridge only, operator decision 2)
    OIDC_BEARER = "oidc_bearer"    # today's OIDC API bearer (bridge until M5)
    SYSTEM = "system"              # in-process system principal (worker, collector)


@dataclass(frozen=True)
class Principal:
    id: str
    kind: PrincipalKind
    org_id: str
    status: PrincipalStatus
    display_name: str
    network_bound: bool = False
    legacy_member_ids: tuple[str, ...] = ()

    @property
    def is_person(self) -> bool:
        return self.kind == PrincipalKind.USER

    @property
    def active(self) -> bool:
        return self.status == PrincipalStatus.ACTIVE


# ---- agents: instances, chains and RFC 8693 `act` ----------------------------------------------

@dataclass(frozen=True)
class AgentInstance:
    """A running agent: `agt_<id>#run_<id>`. It has no row; it exists in tokens and lineage."""
    agent_id: str   # agt_...
    run_id: str     # run_...

    @property
    def sub(self) -> str:
        return f"{self.agent_id}#{self.run_id}"

    @classmethod
    def parse(cls, sub: str) -> "AgentInstance":
        agent_id, sep, run_id = sub.partition("#")
        if not sep or not agent_id.startswith("agt_") or not run_id:
            raise ValueError(f"not an agent instance subject: {sub!r}")
        return cls(agent_id=agent_id, run_id=run_id)


@dataclass(frozen=True)
class ActorChain:
    """The agents acting, in delegation order: hops[0] acted first (directly for the person),
    hops[-1] is the CURRENT actor.

    RFC 8693 nests `act` with the current actor OUTERMOST:
        {"act": {"sub": "agt_forge#run_8812", "act": {"sub": "agt_atlas#run_8811"}}}
    for Alice -> Atlas -> Forge. `boss_chain` is the same chain, flat, in delegation order.
    BOSS evaluates EVERY actor in the chain (the intersection of all ceilings), not only the
    outermost one the RFC says matters (IDENTITY.md §7.5).
    """
    hops: tuple[AgentInstance, ...] = ()

    @property
    def depth(self) -> int:
        return len(self.hops)

    @property
    def current(self) -> Optional[AgentInstance]:
        return self.hops[-1] if self.hops else None

    @property
    def agent_ids(self) -> tuple[str, ...]:
        return tuple(h.agent_id for h in self.hops)

    @property
    def has_cycle(self) -> bool:
        """The same agent twice is a cycle, whatever the runs (IDENTITY.md §7.7)."""
        return len(set(self.agent_ids)) != len(self.hops)

    def extend(self, child: AgentInstance) -> "ActorChain":
        return ActorChain(self.hops + (child,))

    def to_act_claim(self) -> Optional[dict[str, Any]]:
        """The nested RFC 8693 `act` claim, current actor outermost. None for an empty chain."""
        claim: Optional[dict[str, Any]] = None
        for hop in self.hops:  # build inside-out: the first actor ends up innermost
            claim = {"sub": hop.sub, **({"act": claim} if claim else {})}
        return claim

    def to_boss_chain(self) -> list[str]:
        return [h.sub for h in self.hops]

    @classmethod
    def from_act_claim(cls, act: Optional[Mapping[str, Any]]) -> "ActorChain":
        outermost_first: list[AgentInstance] = []
        node = act
        while node:
            outermost_first.append(AgentInstance.parse(str(node["sub"])))
            node = node.get("act")
        return cls(tuple(reversed(outermost_first)))


# ---- what authenticated the request ------------------------------------------------------------

@dataclass(frozen=True)
class SessionRef:
    id: str                       # ses_ public id; never the token
    mfa_at: Optional[datetime]
    auth_methods: tuple[str, ...]
    idle_expires_at: datetime
    absolute_expires_at: datetime


@dataclass(frozen=True)
class CredentialRef:
    id: str                       # key_ public id; never any part of the key
    kind: str                     # pat | sak | scim | legacy_env
    principal_id: str
    scopes: frozenset[str]        # catalogue permissions
    project_ids: Optional[frozenset[str]] = None
    expires_at: Optional[datetime] = None


class RunMode(StrEnum):
    OBO = "obo"                   # on behalf of a person (default)
    AUTONOMOUS = "autonomous"     # the agent as itself


@dataclass(frozen=True)
class TaskBinding:
    run_id: str
    project_id: Optional[str]
    step: Optional[str] = None
    resources: Mapping[str, Any] = field(default_factory=dict)   # {"github": ["acme/web"]}


@dataclass(frozen=True)
class RunTokenClaims:
    """The verified claims of a `boss-run+jwt` (IDENTITY.md §7.5). Built only by verification."""
    iss: str
    aud: str                      # boss:api | boss:mcp | boss:egress
    sub: str                      # usr_ (obo) or agt_ (autonomous)
    org_id: str                   # claim boss_org
    mode: RunMode                 # claim boss_mode
    chain: ActorChain             # claims act / boss_chain (must agree)
    grant_id: Optional[str]       # claim boss_grant
    task: TaskBinding             # claim boss_task
    scope: frozenset[str]         # claim scope (space separated)
    cnf_jkt: str                  # DPoP key thumbprint
    jti: str
    parent_jti: Optional[str]
    iat: int
    exp: int

    @property
    def depth(self) -> int:
        return self.chain.depth


@dataclass(frozen=True)
class AuthContext:
    """Everything the PDP needs about one request. Exactly one of session / credential / run_token
    is set for a real credential; a legacy bridge sets `legacy` instead (and maps to a principal)."""
    principal: Principal
    method: AuthMethod
    session: Optional[SessionRef] = None
    credential: Optional[CredentialRef] = None
    run_token: Optional[RunTokenClaims] = None
    legacy: Optional[str] = None          # identity_legacy_mappings.legacy_kind when bridged
    ip: Optional[str] = None
    user_agent: Optional[str] = None
    request_id: Optional[str] = None

    @property
    def on_behalf_of(self) -> Optional[str]:
        """The person an agent acts for (run token sub in obo mode)."""
        if self.run_token and self.run_token.mode == RunMode.OBO:
            return self.run_token.sub
        return None

    @property
    def chain(self) -> ActorChain:
        return self.run_token.chain if self.run_token else ActorChain()

    @property
    def is_human_session(self) -> bool:
        """Approvals need this (§6.13): a person, signed in with a session, no bearer."""
        return self.principal.is_person and self.session is not None and self.run_token is None
