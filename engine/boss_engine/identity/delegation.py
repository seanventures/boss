"""Agents acting for people: delegation grants, RFC 8693 token exchange, run tokens, the
revocation cascade. Owned by stream S3.

FROZEN IN PHASE 0: constants, claim names, dataclasses and signatures. The bodies delegate to S3's
modules: grants.py, tokens.py (mint, exchange, verify, renew, introspect, decide), cascade.py,
runs.py (the run supervisor), passports.py, vault.py. IDENTITY.md §7.

Effective permission at hop n (§7.4):
    person_live(scope) ∩ ceiling(agent_1) ∩ … ∩ ceiling(agent_n) ∩ grant ∩ token.scope ∩ task
    and permission.delegable, and depth ≤ min(org, grant, every passport's max_depth), no cycles.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Mapping, Optional

from .principal import AgentInstance, AuthContext, RunTokenClaims, TaskBinding

# ---- token formats (frozen) ----------------------------------------------------------------------

GRANT_TYPE_TOKEN_EXCHANGE = "urn:ietf:params:oauth:grant-type:token-exchange"
TOKEN_TYPE_RUN = "urn:boss:token-type:run"
TOKEN_TYPE_ACTOR = "urn:boss:token-type:actor"
TYP_RUN = "boss-run+jwt"
TYP_ACTOR = "boss-actor+jwt"
ALG = "EdDSA"                                  # Ed25519; `none` and HS* are always rejected

AUD_API = "boss:api"
AUD_MCP = "boss:mcp"
AUD_EGRESS = "boss:egress"
AUD_TOKEN = "boss:token"                       # actor assertions are only for the token endpoint
RUN_AUDIENCES = frozenset({AUD_API, AUD_MCP, AUD_EGRESS})

RUN_TOKEN_TTL_SECONDS = 300
ACTOR_ASSERTION_TTL_SECONDS = 60
CLOCK_SKEW_SECONDS = 30
SHIM_RENEW_SECONDS = 240

DEFAULT_MAX_DEPTH = 3
HARD_MAX_DEPTH = 5
GRANT_DEFAULT_DAYS = 30
GRANT_MAX_DAYS = 90

JWKS_PATH = "/.well-known/boss-jwks.json"
TOKEN_ENDPOINT = "/v1/identity/token"
INTROSPECTION_ENDPOINT = "/v1/identity/introspect"

#: Claim names in a run token (IDENTITY.md §7.5). No email and no names in tokens.
RUN_TOKEN_CLAIMS = ("iss", "typ", "aud", "sub", "boss_org", "boss_mode", "act", "boss_chain",
                    "boss_depth", "boss_grant", "boss_task", "scope", "cnf", "jti",
                    "boss_parent_jti", "iat", "exp")


class GrantStatus(StrEnum):
    ACTIVE = "active"
    SUSPENDED = "suspended"
    REVOKED = "revoked"      # terminal
    EXPIRED = "expired"      # terminal


@dataclass(frozen=True)
class GrantRequest:
    """What a person asks for under My account › Agents acting for me, or in a JIT consent sheet."""
    agent_ids: tuple[str, ...]
    permissions: frozenset[str]
    project_ids: tuple[str, ...]
    resources: Mapping[str, Any] = field(default_factory=dict)  # {"github": {"repos": [...], ...}}
    allow_subdelegation: bool = False
    allowed_children: tuple[str, ...] = ()
    max_depth: int = DEFAULT_MAX_DEPTH
    expires_at: Optional[datetime] = None
    task_run_id: Optional[str] = None     # set: "for this build only"
    purpose: str = ""


@dataclass(frozen=True)
class DelegationGrant:
    id: str                               # dlg_
    org_id: str
    user_principal_id: str
    agent_ids: tuple[str, ...]
    permissions: frozenset[str]
    project_ids: tuple[str, ...]
    resources: Mapping[str, Any]
    allow_subdelegation: bool
    allowed_children: tuple[str, ...]
    max_depth: int
    status: GrantStatus
    created_at: datetime
    expires_at: Optional[datetime]
    task_run_id: Optional[str] = None
    parent_grant_id: Optional[str] = None
    purpose: str = ""


@dataclass(frozen=True)
class TokenExchangeRequest:
    """POST /v1/identity/token (RFC 8693). Client-supplied `act` / `boss_chain` are never read:
    the chain is assembled only from verified tokens (A44)."""
    subject_token: str
    subject_token_type: str               # TOKEN_TYPE_RUN only
    actor_token: str
    actor_token_type: str                 # TOKEN_TYPE_ACTOR only
    audience: str                         # one of RUN_AUDIENCES
    scope: frozenset[str]                 # the requested narrowing
    requested_token_type: str = TOKEN_TYPE_RUN
    dpop_proof: Optional[str] = None


@dataclass(frozen=True)
class IssuedToken:
    """A minted run token. `access_token` is a secret: never logged, never in an error body."""
    access_token: str = field(repr=False)
    issued_token_type: str = TOKEN_TYPE_RUN
    token_type: str = "DPoP"
    expires_in: int = RUN_TOKEN_TTL_SECONDS
    jti: str = ""
    scope: frozenset[str] = frozenset()


class CascadeTrigger(StrEnum):
    GRANT_REVOKED = "grant_revoked"
    GRANT_EXPIRED = "grant_expired"
    PERSON_DISABLED = "person_disabled"
    PERSON_DELETED = "person_deleted"
    PASSPORT_SUSPENDED = "passport_suspended"
    PASSPORT_REVOKED = "passport_revoked"
    API_KEY_REVOKED = "api_key_revoked"
    SESSION_REVOKED = "session_revoked"
    BINDING_REMOVED = "binding_removed"


@dataclass(frozen=True)
class CascadeReport:
    """What a cascade did (or, with dry_run, would do): the console's cascade preview."""
    trigger: CascadeTrigger
    target_id: str
    grants_revoked: tuple[str, ...] = ()
    grants_suspended: tuple[str, ...] = ()
    run_tokens_revoked: int = 0
    egress_credentials_revoked: int = 0
    github_tokens_revoked: int = 0
    runs_halted: tuple[str, ...] = ()
    schedules_paused: tuple[str, ...] = ()
    passports_suspended: tuple[str, ...] = ()
    sessions_revoked: int = 0
    dry_run: bool = False


def create_grant(ctx: AuthContext, request: GrantRequest, *, db: Any) -> DelegationGrant:
    """Create a grant for ctx.principal (a person, human session, `delegation:grant`, step-up).
    request.permissions ⊆ person's live permissions ∩ every named agent's ceiling, all delegable,
    at creation; re-intersected live at every use. GitHub resources pass the user-repo-access guard
    on multi-person installs (operator decision 4)."""
    from . import grants

    return grants.create_grant(ctx, request, db=db)


def mint_run_token(*, subject: AuthContext, agent: AgentInstance, grant_id: Optional[str],
                   task: TaskBinding, audience: str, scope: frozenset[str], cnf_jkt: str,
                   db: Any) -> IssuedToken:
    """The FIRST hop: the run supervisor mints a run token for a person's (or, autonomous, an
    agent's) request context. Refuses any scope containing a non-delegable key (second check)."""
    from . import tokens

    return tokens.mint_run_token(subject=subject, agent=agent, grant_id=grant_id, task=task,
                                 audience=audience, scope=scope, cnf_jkt=cnf_jkt, db=db)


def exchange_token(request: TokenExchangeRequest, *, htm: str, htu: str, db: Any) -> IssuedToken:
    """Every later hop (RFC 8693, IDENTITY.md §7.5 checks 1-8, in order; any failure denies).
    new_scope = requested ∩ parent.scope ∩ ceiling(child); empty means refused, never 'requested'."""
    from . import tokens

    if request is None:
        raise tokens.TokenError("invalid_request", 400, "no_request")
    return tokens.exchange_token(request, htm=htm, htu=htu, db=db)


def verify_run_token(token: str, *, audience: str, dpop_proof: Optional[str], htm: str, htu: str,
                     db: Any) -> RunTokenClaims:
    """Verify signature (local JWKS only; never jku/x5u/jwk), alg, typ, iss, aud, time with 30 s
    skew, DPoP (htm, htu, iat, jti replay, ath) and jti liveness. Raises AuthnError."""
    from . import tokens

    return tokens.verify_run_token(token, audience=audience, dpop_proof=dpop_proof, htm=htm, htu=htu, db=db)


def revoke_cascade(trigger: CascadeTrigger, target_id: str, *, actor: AuthContext, reason: str,
                   db: Any, dry_run: bool = False) -> CascadeReport:
    """Revoke and propagate in ONE transaction plus NOTIFY (IDENTITY.md §7.11); effect ≤5 s end to
    end. `dry_run=True` computes the preview and changes nothing."""
    from . import cascade

    return cascade.revoke_cascade(trigger, target_id, actor=actor, reason=reason, db=db, dry_run=dry_run)
