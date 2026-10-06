"""The token service: run tokens, actor assertions, RFC 8693 exchange, renewal, introspection, and
the run-token half of the decision. Owned by S3. IDENTITY.md §7.4–§7.7.

Run token (`boss-run+jwt`, EdDSA, ≤5 min, ONE audience, DPoP-bound):
    sub = the person (obo) or the agent (autonomous); act = the agents, current actor OUTERMOST;
    boss_chain = the same chain flat, in delegation order; boss_depth = len(chain).

Effective permission for a run token, evaluated LIVE on every use and at every hop:
    person_live(scope) ∩ ceiling(agent_1) ∩ … ∩ ceiling(agent_n) ∩ grant ∩ token.scope ∩ task
    and permission.delegable and depth ≤ min(org, grant, every passport) and no cycle.
Narrowing only: an exchange computes requested ∩ parent.scope ∩ ceiling(child) (∩ the live
intersection), and an empty result is a refusal, never "the requested set" (Kaappu flaw #5).
A child token never outlives its parent token, its grant, or (on renewal) its parent run.

Every failure is uniform to the caller (401 `unauthenticated`, 400 `invalid_grant` /
`invalid_request` / `invalid_scope`, 429) and carries an internal `reason` for lineage only.
"""
from __future__ import annotations

import os
import secrets
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Optional

from . import catalogue, jwks
from .authn import AuthnError
from .delegation import (
    ACTOR_ASSERTION_TTL_SECONDS,
    AUD_API,
    AUD_EGRESS,
    AUD_MCP,
    AUD_TOKEN,
    CLOCK_SKEW_SECONDS,
    RUN_AUDIENCES,
    RUN_TOKEN_TTL_SECONDS,
    TOKEN_TYPE_ACTOR,
    TOKEN_TYPE_RUN,
    TYP_ACTOR,
    TYP_RUN,
    IssuedToken,
    TokenExchangeRequest,
)
from .principal import (
    ActorChain,
    AgentInstance,
    AuthContext,
    AuthMethod,
    PrincipalKind,
    RunMode,
    RunTokenClaims,
    TaskBinding,
)

ISSUER_ENV = "BOSS_PUBLIC_URL"
EGRESS_SERVICE_KEY_ENV = "BOSS_EGRESS_SERVICE_KEY"
EXCHANGE_LIMIT_PER_MINUTE = 60
GRANT_TYPE_RENEW = "urn:boss:grant-type:run-renewal"


class TokenError(AuthnError):
    """`code`/`status` go to the caller; `reason` is internal (lineage, tests), never a secret."""

    def __init__(self, code: str, status: int, reason: str, retry_after: Optional[int] = None):
        super().__init__(code, status)
        self.reason = reason
        self.retry_after = retry_after


def _invalid(reason: str) -> TokenError:
    return TokenError("unauthenticated", 401, reason)


def _refused(reason: str, code: str = "invalid_grant") -> TokenError:
    return TokenError(code, 400 if code != "forbidden" else 403, reason)


def issuer() -> str:
    return (os.environ.get(ISSUER_ENV) or "urn:boss:local").rstrip("/")


def _now() -> int:
    return int(time.time())


def _dt(ts: int) -> datetime:
    return datetime.fromtimestamp(ts, timezone.utc)


# ---- shared checks -----------------------------------------------------------------------------------

def _require_delegable_scope(scope: frozenset[str]) -> None:
    """The token service's own check, independent of the PDP: a non-delegable key is never minted."""
    for key in scope:
        if not catalogue.is_known(key):
            raise _refused(f"unknown_permission:{key}", "invalid_scope")
        if not catalogue.meta(key).delegable:
            raise _refused(f"not_delegable:{key}", "invalid_scope")


@dataclass(frozen=True)
class LiveState:
    """What a run token stands on right now."""
    allowed: frozenset[str]          # the live intersection, before token.scope
    max_depth: int
    narrowed_by: Mapping[str, frozenset[str]]


def live_state(db: Any, *, org_id: str, mode: RunMode, sub: str, chain: ActorChain,
               grant_id: Optional[str], task: TaskBinding) -> LiveState:
    """Re-check liveness of EVERYTHING the token stands on, and compute the live intersection.
    Raises TokenError on anything inactive (person, any agent in the chain, the grant, org)."""
    from . import grants, passports

    narrowed: dict[str, frozenset[str]] = {}
    depth_caps = [grants.org_max_depth(org_id, db=db)]
    agents = list(chain.hops)
    if mode == RunMode.OBO:
        person = grants.principal(db, sub)
        if person is None or person.kind != PrincipalKind.USER or person.org_id != org_id:
            raise _invalid("subject_unknown")
        if not person.active:
            raise _invalid("subject_inactive")
        grant = grants.load(grant_id, db=db)
        if grant is None or grant.org_id != org_id or grant.user_principal_id != sub:
            raise _invalid("grant_unknown")
        if not grants.is_live(grant):
            raise _invalid("grant_inactive")
        if not agents or not grants.names_agent(grant, agents[0].agent_id):
            raise _invalid("grant_does_not_name_agent")
        if not grants.run_matches(grant, task.run_id):
            raise _invalid("grant_task_mismatch")
        if task.project_id and task.project_id not in grant.project_ids:
            raise _invalid("grant_project_mismatch")
        allowed = grants.person_live(db, person, task.project_id)
        narrowed["person"] = allowed
        allowed = allowed & grant.permissions
        narrowed[f"grant:{grant.id}"] = grant.permissions
        depth_caps.append(grant.max_depth)
    else:
        root = passports.get(sub, db=db)
        if root is None or root.org_id != org_id:
            raise _invalid("subject_unknown")
        if not root.active:
            raise _invalid("chain_inactive")
        if not root.autonomous:
            raise _invalid("not_autonomous")
        own = grants.person_live(db, grants.principal(db, sub), task.project_id)   # the agent's own bindings
        allowed = own & root.ceiling
        narrowed["agent_bindings"] = own
        narrowed[f"ceiling:{root.agent_id}"] = root.ceiling
        depth_caps.append(root.max_depth)
    seen: set[str] = set()
    for hop in agents:
        if hop.agent_id in seen:
            raise _invalid("cycle")
        seen.add(hop.agent_id)
        p = passports.get(hop.agent_id, db=db)
        if p is None or p.org_id != org_id:
            raise _invalid("chain_unknown")
        if not p.active:
            raise _invalid("chain_inactive")
        allowed = allowed & p.ceiling
        narrowed[f"ceiling:{p.agent_id}"] = p.ceiling
        depth_caps.append(p.max_depth)
    delegable = catalogue.delegable()
    return LiveState(allowed=frozenset(allowed & delegable), max_depth=min(depth_caps), narrowed_by=narrowed)


def _jti_live(db: Any, jti: str, org_id: str) -> bool:
    from sqlalchemy import text

    row = db.execute(text("SELECT org_id, revoked_at, expires_at FROM identity_run_credentials WHERE jti = :j"),
                     {"j": jti}).first()
    return bool(row and row[0] == org_id and row[1] is None)


# ---- minting -------------------------------------------------------------------------------------------

def _sign_run(db: Any, claims: dict[str, Any]) -> str:
    from . import signing_keys

    key = signing_keys.active_key("run", db=db)
    return jwks.sign({"alg": jwks.ALG, "typ": TYP_RUN, "kid": key.kid}, claims, key.private_key)


def _store_credential(db: Any, claims: RunTokenClaims) -> None:
    import json

    from sqlalchemy import text

    from . import lineage

    db.execute(text(
        "INSERT INTO identity_run_credentials (jti, parent_jti, org_id, run_id, grant_id, subject_principal_id, "
        " mode, chain, depth, aud, scope_hash, cnf_jkt, issued_at, expires_at, task, scope, claims) VALUES (:jti, "
        " :pjti, :org, :run, :grant, :sub, :mode, CAST(:chain AS JSONB), :depth, :aud, :sh, :jkt, :iat, :exp, "
        " CAST(:task AS JSONB), :scope, CAST(:claims AS JSONB))"),
        {"jti": claims.jti, "pjti": claims.parent_jti, "org": claims.org_id, "run": claims.task.run_id,
         "grant": claims.grant_id, "sub": claims.sub, "mode": claims.mode.value,
         "chain": json.dumps(claims.chain.to_boss_chain()), "depth": claims.depth, "aud": claims.aud,
         "sh": lineage.scope_hash(claims.scope), "jkt": claims.cnf_jkt, "iat": _dt(claims.iat),
         "exp": _dt(claims.exp), "task": json.dumps(_claims_dict(claims)["boss_task"], default=str),
         "scope": sorted(claims.scope), "claims": json.dumps(redacted_claims(claims), default=str)})


#: Claims the ledger never keeps verbatim: the DPoP key binding is shown as "[redacted]".
REDACTED_CLAIM = "[redacted]"


def redacted_claims(claims: RunTokenClaims) -> dict[str, Any]:
    """What the token SAID, for the console's claims view and the ledger: every claim, with the
    `cnf` key binding redacted. Never a signature or the token itself (they are not claims)."""
    out = _claims_dict(claims)
    out["cnf"] = {"jkt": REDACTED_CLAIM}
    return out


def _claims_dict(c: RunTokenClaims) -> dict[str, Any]:
    out: dict[str, Any] = {
        "iss": c.iss, "typ": TYP_RUN, "aud": c.aud, "sub": c.sub, "boss_org": c.org_id,
        "boss_mode": c.mode.value, "boss_chain": c.chain.to_boss_chain(), "boss_depth": c.depth,
        "boss_grant": c.grant_id,
        "boss_task": {"run": c.task.run_id, "project": c.task.project_id, "step": c.task.step,
                      "resources": dict(c.task.resources)},
        "scope": " ".join(sorted(c.scope)), "cnf": {"jkt": c.cnf_jkt}, "jti": c.jti,
        "boss_parent_jti": c.parent_jti, "iat": c.iat, "exp": c.exp}
    act = c.chain.to_act_claim()
    if act is not None:
        out["act"] = act
    return out


def _issue(db: Any, claims: RunTokenClaims, event_type: str) -> IssuedToken:
    from . import lineage

    token = _sign_run(db, _claims_dict(claims))
    _store_credential(db, claims)
    lineage.token_event(db, claims, event_type)
    return IssuedToken(access_token=token, expires_in=max(0, claims.exp - _now()), jti=claims.jti,
                       scope=claims.scope)


def _new_jti() -> str:
    from .ids import new_id

    return new_id("rt")


def _exp_cap(now: int, grant: Any, *others: Optional[int]) -> int:
    exp = now + RUN_TOKEN_TTL_SECONDS
    if grant is not None and grant.expires_at is not None:
        exp = min(exp, int(grant.expires_at.timestamp()))
    for o in others:
        if o is not None:
            exp = min(exp, o)
    return exp


def mint_run_token(*, subject: AuthContext, agent: AgentInstance, grant_id: Optional[str],
                   task: TaskBinding, audience: str, scope: frozenset[str], cnf_jkt: str,
                   db: Any) -> IssuedToken:
    from . import grants, lineage, passports

    org_id = subject.principal.org_id if subject else ""
    try:
        if audience not in RUN_AUDIENCES:
            raise _refused("bad_audience", "invalid_request")
        if not cnf_jkt or not isinstance(cnf_jkt, str):
            raise _refused("no_dpop_key", "invalid_request")
        if subject is None or subject.run_token is not None:
            raise _refused("first_hop_needs_a_principal_context", "invalid_request")   # later hops: exchange
        scope = frozenset(scope)
        _require_delegable_scope(scope)
        p = passports.get(agent.agent_id, db=db)
        if p is None or p.org_id != org_id:
            raise _refused("unknown_agent")
        if not p.active:
            raise _refused("agent_inactive")
        if subject.principal.kind == PrincipalKind.USER:
            mode, sub, chain = RunMode.OBO, subject.principal.id, ActorChain((agent,))
            grant = grants.load(grant_id, db=db)
            if grant is None:
                raise _refused("grant_required")
            if task.resources.get("github"):
                allowed_repos = set(grants.github_resources(grant).get("repos") or ())
                if not set(task.resources["github"]) <= allowed_repos:
                    raise _refused("repo_outside_grant")
                grants.check_user_repo_access(org_id, sub, grants.github_resources(grant), db=db)
        elif subject.principal.kind == PrincipalKind.AGENT and subject.principal.id == agent.agent_id:
            mode, sub, chain, grant = RunMode.AUTONOMOUS, agent.agent_id, ActorChain(), None
        else:
            raise _refused("subject_cannot_delegate")
        state = live_state(db, org_id=org_id, mode=mode, sub=sub, chain=chain,
                           grant_id=grant.id if grant else None, task=task)
        if chain.depth > state.max_depth:
            raise _refused("depth")
        new_scope = scope & state.allowed
        if not new_scope:
            raise _refused("empty_scope", "invalid_scope")
        now = _now()
        claims = RunTokenClaims(iss=issuer(), aud=audience, sub=sub, org_id=org_id, mode=mode, chain=chain,
                                grant_id=grant.id if grant else None, task=task, scope=frozenset(new_scope),
                                cnf_jkt=cnf_jkt, jti=_new_jti(), parent_jti=None, iat=now,
                                exp=_exp_cap(now, grant))
        if claims.exp <= now:
            raise _refused("grant_expired")
        issued = _issue(db, claims, "token.issued")
        if grant is not None:
            grants.mark_first_use(db, grant)
        return issued
    except TokenError as exc:
        lineage.denied(db, org_id=org_id, event_type="token.denied", reason=exc.reason, run_id=task.run_id,
                       grant_id=grant_id, sub=subject.principal.id if subject else None)
        raise


# ---- verification --------------------------------------------------------------------------------------

def _parse_claims(raw: Mapping[str, Any]) -> RunTokenClaims:
    try:
        mode = RunMode(raw["boss_mode"])
        chain_list = raw.get("boss_chain") or []
        chain = ActorChain(tuple(AgentInstance.parse(s) for s in chain_list))
        if ActorChain.from_act_claim(raw.get("act")) != chain:
            raise _invalid("act_chain_disagree")
        if int(raw.get("boss_depth", -1)) != chain.depth:
            raise _invalid("depth_claim")
        sub = str(raw["sub"])
        if mode == RunMode.OBO and (not sub.startswith("usr_") or chain.depth < 1 or not raw.get("boss_grant")):
            raise _invalid("obo_shape")
        if mode == RunMode.AUTONOMOUS and not sub.startswith("agt_"):
            raise _invalid("autonomous_shape")
        task_raw = raw.get("boss_task") or {}
        task = TaskBinding(run_id=str(task_raw["run"]), project_id=task_raw.get("project"),
                           step=task_raw.get("step"), resources=task_raw.get("resources") or {})
        scope = frozenset(str(raw.get("scope", "")).split())
        cnf = raw.get("cnf") or {}
        claims = RunTokenClaims(iss=str(raw["iss"]), aud=str(raw["aud"]), sub=sub, org_id=str(raw["boss_org"]),
                                mode=mode, chain=chain, grant_id=raw.get("boss_grant"), task=task, scope=scope,
                                cnf_jkt=str(cnf["jkt"]), jti=str(raw["jti"]), parent_jti=raw.get("boss_parent_jti"),
                                iat=int(raw["iat"]), exp=int(raw["exp"]))
    except TokenError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise _invalid("claims_shape") from exc
    for key in claims.scope:
        if not catalogue.is_known(key) or not catalogue.meta(key).delegable:
            raise _invalid("scope_not_delegable")
    return claims


def _verify(token: str, *, audiences: frozenset[str], dpop_proof: Optional[str], htm: str, htu: str,
            db: Any, now: Optional[int] = None) -> RunTokenClaims:
    from . import dpop, signing_keys

    now = _now() if now is None else now
    if not isinstance(token, str) or not token:
        raise _invalid("no_token")
    try:
        _, raw = jwks.verify(token, typ=TYP_RUN,
                             key_for_kid=lambda kid: signing_keys.verification_key(kid, "run", db=db))
    except jwks.JoseError as exc:
        raise _invalid(exc.code) from exc
    if raw.get("typ") not in (None, TYP_RUN):
        raise _invalid("bad_typ")
    if raw.get("iss") != issuer():
        raise _invalid("bad_iss")
    aud = raw.get("aud")
    if not isinstance(aud, str) or aud not in audiences:      # exactly one audience, a string
        raise _invalid("bad_aud")
    claims = _parse_claims(raw)
    if claims.exp <= now - CLOCK_SKEW_SECONDS:
        raise _invalid("expired")
    if claims.iat > now + CLOCK_SKEW_SECONDS:
        raise _invalid("iat_future")
    if "nbf" in raw and int(raw["nbf"]) > now + CLOCK_SKEW_SECONDS:
        raise _invalid("nbf")
    if claims.exp - claims.iat > RUN_TOKEN_TTL_SECONDS:
        raise _invalid("lifetime")
    try:
        dpop.verify_proof(dpop_proof, htm=htm, htu=htu, access_token=token, expected_jkt=claims.cnf_jkt, db=db)
    except dpop.DPoPError as exc:
        raise _invalid(exc.code) from exc
    if not _jti_live(db, claims.jti, claims.org_id):
        raise _invalid("jti_revoked")
    return claims


def verify_run_token(token: str, *, audience: str, dpop_proof: Optional[str], htm: str, htu: str,
                     db: Any) -> RunTokenClaims:
    if audience not in RUN_AUDIENCES:
        raise _invalid("bad_expected_audience")
    return _verify(token, audiences=frozenset({audience}), dpop_proof=dpop_proof, htm=htm, htu=htu, db=db)


def context_for(claims: RunTokenClaims, *, db: Any) -> AuthContext:
    """The AuthContext a verified run token yields (S1's `authenticate` calls verify + this)."""
    from . import grants

    if claims.mode == RunMode.OBO:
        principal = grants.principal(db, claims.sub)
    else:
        principal = grants.principal(db, claims.sub)
    if principal is None:
        raise _invalid("subject_unknown")
    if claims.mode == RunMode.OBO and not principal.active:
        # Defence in depth (verify_run_token's live_state already refuses it): a person who is
        # disabled stops every run acting for them at the next call, on every path.
        raise _invalid("subject_inactive")
    if claims.chain.current is not None:
        agent = grants.principal(db, claims.chain.current.agent_id)
        if agent is None:
            raise _invalid("chain_unknown")
        principal = agent       # the actor is the agent; `on_behalf_of` names the person
    return AuthContext(principal=principal, method=AuthMethod.RUN_TOKEN, run_token=claims)


# ---- actor assertions and the exchange -------------------------------------------------------------------

def mint_actor_assertion(*, parent: RunTokenClaims, child_agent_id: str, child_run_id: str, child_jkt: str,
                         db: Any) -> str:
    """The run supervisor mints this when it creates a child run for a Passport (spawn_subagent, a
    workflow step). It names the child INSTANCE, its parent run and jti, and the child's DPoP key.
    It is only valid at the token endpoint (aud boss:token) for 60 s, once."""
    from . import signing_keys

    if not child_run_id.startswith(parent.task.run_id + "/"):
        raise _refused("child_run_not_under_parent", "invalid_request")
    now = _now()
    claims = {"iss": issuer(), "typ": TYP_ACTOR, "aud": AUD_TOKEN, "sub": f"{child_agent_id}#{child_run_id}",
              "boss_org": parent.org_id, "boss_parent_run": parent.task.run_id, "boss_parent_jti": parent.jti,
              "cnf": {"jkt": child_jkt}, "jti": _new_jti(), "iat": now, "exp": now + ACTOR_ASSERTION_TTL_SECONDS}
    key = signing_keys.active_key("actor", db=db)
    return jwks.sign({"alg": jwks.ALG, "typ": TYP_ACTOR, "kid": key.kid}, claims, key.private_key)


def _verify_actor(token: str, *, db: Any, now: int) -> dict[str, Any]:
    from sqlalchemy import text

    from . import signing_keys

    try:
        _, raw = jwks.verify(token, typ=TYP_ACTOR,
                             key_for_kid=lambda kid: signing_keys.verification_key(kid, "actor", db=db))
    except jwks.JoseError as exc:
        raise _refused(f"actor_{exc.code}") from exc
    if raw.get("iss") != issuer() or raw.get("aud") != AUD_TOKEN:
        raise _refused("actor_iss_aud")
    try:
        exp, iat = int(raw["exp"]), int(raw["iat"])
        AgentInstance.parse(str(raw["sub"]))
        jkt = str(raw["cnf"]["jkt"])
        jti = str(raw["jti"])
    except (KeyError, TypeError, ValueError) as exc:
        raise _refused("actor_shape") from exc
    if exp <= now - CLOCK_SKEW_SECONDS or iat > now + CLOCK_SKEW_SECONDS or exp - iat > ACTOR_ASSERTION_TTL_SECONDS:
        raise _refused("actor_time")
    if not jkt:
        raise _refused("actor_no_key")
    used = db.execute(text("INSERT INTO identity_dpop_replay (jkt, jti, expires_at) VALUES ('actor-assertion', :j, :e) "
                           "ON CONFLICT DO NOTHING RETURNING 1"), {"j": jti, "e": _dt(exp + CLOCK_SKEW_SECONDS)}).scalar()
    if not used:
        raise _refused("actor_replayed")
    return raw


# in-process fallback limiter (used only while S1's Postgres limiter is not built; it still limits)
_local_hits: dict[str, deque] = {}
_local_lock = threading.Lock()


def _rate_limit(run_id: str, *, db: Any) -> None:
    from . import ratelimit

    try:
        decision = ratelimit.hit("token_exchange.run", run_id, limit=EXCHANGE_LIMIT_PER_MINUTE,
                                 window_seconds=60, db=db)
        allowed, retry = decision.allowed, decision.retry_after_seconds
    except NotImplementedError:
        now = time.monotonic()
        with _local_lock:
            q = _local_hits.setdefault(run_id, deque())
            while q and now - q[0] > 60:
                q.popleft()
            allowed = len(q) < EXCHANGE_LIMIT_PER_MINUTE
            if allowed:
                q.append(now)
            retry = int(60 - (now - q[0])) + 1 if q and not allowed else None
    if not allowed:
        raise TokenError("rate_limited", 429, "exchange_flood", retry_after=retry or 60)


def exchange_token(request: TokenExchangeRequest, *, htm: str, htu: str, db: Any) -> IssuedToken:
    """RFC 8693, IDENTITY.md §7.5 checks 1–8 in order; any failure denies. The chain is built ONLY
    from verified tokens: a client-supplied `act`/`boss_chain` is not a field this reads (A44)."""
    from . import grants, lineage, passports

    org_id = ""
    run_id = None
    try:
        # 0. Types are exact. An alternate subject_token_type cannot route around actor validation
        #    (Keycloak #52691): there is exactly one accepted combination.
        if (request.subject_token_type != TOKEN_TYPE_RUN or request.actor_token_type != TOKEN_TYPE_ACTOR
                or request.requested_token_type != TOKEN_TYPE_RUN or not request.actor_token):
            raise _refused("token_types", "invalid_request")
        if request.audience not in RUN_AUDIENCES:
            raise _refused("bad_audience", "invalid_target")
        now = _now()
        # 1. The subject (parent) token, with its DPoP proof, at THIS endpoint.
        parent = _verify(request.subject_token, audiences=frozenset({AUD_API, AUD_TOKEN}),
                         dpop_proof=request.dpop_proof, htm=htm, htu=htu, db=db, now=now)
        org_id, run_id = parent.org_id, parent.task.run_id
        _rate_limit(parent.task.run_id, db=db)
        # 2. The actor assertion: minted by BOSS for a child run of this exact parent.
        actor = _verify_actor(request.actor_token, db=db, now=now)
        child = AgentInstance.parse(str(actor["sub"]))
        if actor.get("boss_org") != parent.org_id:
            raise _refused("cross_org")
        if actor.get("boss_parent_run") != parent.task.run_id or actor.get("boss_parent_jti") != parent.jti:
            raise _refused("not_a_child_of_subject")
        if not child.run_id.startswith(parent.task.run_id + "/"):
            raise _refused("not_a_child_run")
        child_p = passports.get(child.agent_id, db=db)
        if child_p is None or child_p.org_id != parent.org_id:
            raise _refused("cross_org")
        if not child_p.active:
            raise _refused("child_inactive")
        # 3. The caller may delegate to this child.
        if parent.chain.current is not None:
            parent_p = passports.get(parent.chain.current.agent_id, db=db)
        else:
            parent_p = passports.get(parent.sub, db=db)           # an autonomous root
        if parent_p is None or not parent_p.may_spawn(child_p):
            raise _refused("child_not_allowed")                    # A39
        grant = None
        if parent.mode == RunMode.OBO:
            grant = grants.load(parent.grant_id, db=db)
            if grant is None or not grants.child_allowed(grant, child_p.agent_id, child_p.role_key):
                raise _refused("grant_forbids_subdelegation")
        # 4. Depth and cycles, against the minimum along the WHOLE chain (live).
        chain = parent.chain.extend(child)
        if chain.has_cycle or child.agent_id == parent.sub:
            raise _refused("cycle")                                # A46
        # 5. Everything in the chain is live; compute the live intersection.
        try:
            state = live_state(db, org_id=parent.org_id, mode=parent.mode, sub=parent.sub, chain=chain,
                               grant_id=parent.grant_id, task=parent.task)
        except TokenError as exc:
            raise _refused(exc.reason) from exc
        if chain.depth > state.max_depth:
            raise _refused("depth")                                # A45
        # 6. Narrow. Empty stays empty.
        requested = frozenset(request.scope)
        _require_delegable_scope(requested)
        new_scope = requested & parent.scope & child_p.ceiling & state.allowed
        if not new_scope:
            raise _refused("empty_scope", "invalid_scope")         # A48
        # 7. Lifetime: never past the parent token or the grant.
        exp = _exp_cap(now, grant, parent.exp)
        if exp <= now:
            raise _refused("parent_expired")
        task = TaskBinding(run_id=child.run_id, project_id=parent.task.project_id, step=parent.task.step,
                           resources=parent.task.resources)
        claims = RunTokenClaims(iss=issuer(), aud=request.audience, sub=parent.sub, org_id=parent.org_id,
                                mode=parent.mode, chain=chain, grant_id=parent.grant_id, task=task,
                                scope=new_scope, cnf_jkt=str(actor["cnf"]["jkt"]), jti=_new_jti(),
                                parent_jti=parent.jti, iat=now, exp=exp)
        # 8. Mint, record, return.
        return _issue(db, claims, "token.exchanged")
    except TokenError as exc:
        lineage.denied(db, org_id=org_id, event_type="token.denied", reason=exc.reason, run_id=run_id)
        raise


def renew_run_token(token: str, *, dpop_proof: Optional[str], htm: str, htu: str, db: Any) -> IssuedToken:
    """The shim's 4-minute renewal: the same hop, re-checked live, never longer than the grant or
    (for a child) than the parent RUN's live credentials. Revoking anything upstream stops it."""
    from sqlalchemy import text

    from . import grants

    claims = _verify(token, audiences=RUN_AUDIENCES, dpop_proof=dpop_proof, htm=htm, htu=htu, db=db)
    state = live_state(db, org_id=claims.org_id, mode=claims.mode, sub=claims.sub, chain=claims.chain,
                       grant_id=claims.grant_id, task=claims.task)
    if claims.depth > state.max_depth:
        raise _invalid("depth")
    scope = claims.scope & state.allowed
    if not scope:
        raise _refused("empty_scope", "invalid_scope")
    now = _now()
    parent_exp: Optional[int] = None
    if claims.depth >= 2:
        parent_run = claims.chain.hops[-2].run_id
        latest = db.execute(text("SELECT max(expires_at) FROM identity_run_credentials WHERE run_id = :r "
                                 "AND revoked_at IS NULL AND expires_at > now()"), {"r": parent_run}).scalar()
        if latest is None:
            raise _invalid("parent_run_ended")
        parent_exp = int(latest.timestamp())
    grant = grants.load(claims.grant_id, db=db)
    exp = _exp_cap(now, grant, parent_exp)
    new = RunTokenClaims(iss=claims.iss, aud=claims.aud, sub=claims.sub, org_id=claims.org_id, mode=claims.mode,
                         chain=claims.chain, grant_id=claims.grant_id, task=claims.task, scope=scope,
                         cnf_jkt=claims.cnf_jkt, jti=_new_jti(), parent_jti=claims.parent_jti, iat=now, exp=exp)
    db.execute(text("UPDATE identity_run_credentials SET revoked_at = now() WHERE jti = :j"), {"j": claims.jti})
    return _issue(db, new, "token.issued")


# ---- the run-token half of the decision ------------------------------------------------------------------

def decide(claims: RunTokenClaims, permission: str, scope: Any, *, db: Any):
    """The PDP for a run-token context (S2's `authz.check` delegates its OBO/autonomous base set
    here). Returns an authz.Decision; a deny names what narrowed it (`ceiling:agt_forge`, …)."""
    from . import lineage
    from .authz import Decision, DenyReason

    def deny(reason: DenyReason, narrowed: tuple[str, ...] = ()):
        lineage.denied(db, org_id=claims.org_id, event_type="authz.denied", reason=reason.value,
                       run_id=claims.task.run_id, grant_id=claims.grant_id, chain=claims.chain.to_boss_chain(),
                       sub=claims.sub, jti=claims.jti, permission=permission,
                       details={"narrowed_by": list(narrowed)})
        return Decision.deny(permission, scope, reason, narrowed)

    if not catalogue.is_known(permission):
        return deny(DenyReason.UNKNOWN_PERMISSION)
    if not catalogue.meta(permission).delegable:
        return deny(DenyReason.NOT_DELEGABLE)                      # A49: never, whatever the chain
    if not _jti_live(db, claims.jti, claims.org_id):
        return deny(DenyReason.CREDENTIAL_INVALID)
    if scope is not None and getattr(scope, "org_id", claims.org_id) != claims.org_id:
        return deny(DenyReason.NOT_FOUND)
    project = getattr(scope, "project_id", None)
    if project and claims.task.project_id and project != claims.task.project_id:
        return deny(DenyReason.OUT_OF_SCOPE, ("task",))
    task = claims.task if not project else TaskBinding(claims.task.run_id, project, claims.task.step,
                                                       claims.task.resources)
    try:
        state = live_state(db, org_id=claims.org_id, mode=claims.mode, sub=claims.sub, chain=claims.chain,
                           grant_id=claims.grant_id, task=task)
    except TokenError as exc:
        reason = {"subject_inactive": DenyReason.PRINCIPAL_INACTIVE, "chain_inactive": DenyReason.CHAIN_INACTIVE,
                  "grant_inactive": DenyReason.GRANT_INACTIVE, "grant_project_mismatch": DenyReason.OUT_OF_SCOPE,
                  "grant_task_mismatch": DenyReason.OUT_OF_SCOPE}.get(exc.reason, DenyReason.CREDENTIAL_INVALID)
        return deny(reason, (exc.reason,))
    if claims.depth > state.max_depth:
        return deny(DenyReason.MISSING_PERMISSION, ("depth",))
    if permission not in claims.scope:
        return deny(DenyReason.MISSING_PERMISSION, ("token_scope",))
    if permission not in state.allowed:
        narrowed = tuple(name for name, perms in state.narrowed_by.items() if permission not in perms)
        return deny(DenyReason.MISSING_PERMISSION, narrowed)
    return Decision.allow(permission, scope)


# ---- introspection ---------------------------------------------------------------------------------------

def egress_service_key_ok(presented: Optional[str]) -> bool:
    expected = os.environ.get(EGRESS_SERVICE_KEY_ENV) or ""
    return bool(expected) and bool(presented) and secrets.compare_digest(presented.encode(), expected.encode())


#: The egress proxy's own principal (catalogue system_principals). It authenticates to
#: /v1/identity/introspect with a `boss_sak_` key of its own; BOSS_EGRESS_SERVICE_KEY stays a fallback.
EGRESS_PRINCIPAL = "sys_egress"


def egress_caller_ok(authorization: Optional[str], legacy_key: Optional[str], *, db: Any,
                     ip: Optional[str] = None) -> bool:
    """Is this server-to-server caller the egress service? A presented bearer DECIDES: it must be
    a live `boss_sak_` key whose principal is the active `sys_egress` system principal (checked
    live: a revoked key or a suspended principal is refused at once). Without a bearer, the legacy
    `X-Boss-Egress-Key` (BOSS_EGRESS_SERVICE_KEY) is accepted, as before."""
    from .authn import AuthnError, parse_api_key
    from .principal import PrincipalKind

    scheme, _, presented = (authorization or "").partition(" ")
    presented = presented.strip()
    if scheme.lower() == "bearer" and presented:
        parsed = parse_api_key(presented)
        if parsed is None or parsed.kind != "sak" or not parsed.checksum_ok:
            return False
        from .api_keys import authenticate_key

        try:
            ctx = authenticate_key(db, presented, parsed, ip=ip, user_agent=None, request_id=None)
        except AuthnError:
            return False
        p = ctx.principal
        return p.id == EGRESS_PRINCIPAL and p.kind == PrincipalKind.SYSTEM and p.active
    if authorization:
        return False                       # some other scheme: never fall through to the shared key
    return egress_service_key_ok(legacy_key)


def issue_egress_service_key(db: Any, *, org_id: str, created_by: str = "sys_host_cli",
                             revoke_others: bool = False) -> Any:
    """Mint the egress service's `boss_sak_` key (host shell: `boss identity egress-key`). Shown
    ONCE by the caller; only its SHA-256 is stored. `revoke_others` ends every earlier key of
    sys_egress (a rotation without overlap)."""
    from sqlalchemy import text

    from . import audit, ids
    from .api_keys import CreatedKey
    from .authn import hash_token, mint_api_key

    db.execute(text("INSERT INTO identity_principals (id, org_id, kind, status, display_name, created_by) "
                    "VALUES (:id, :o, 'system', 'active', 'BOSS egress', 'identity') ON CONFLICT (id) DO NOTHING"),
               {"id": EGRESS_PRINCIPAL, "o": org_id})
    if revoke_others:
        db.execute(text("UPDATE identity_api_credentials SET revoked_at = now(), revoked_by = :by "
                        "WHERE principal_id = :p AND revoked_at IS NULL"), {"by": created_by, "p": EGRESS_PRINCIPAL})
    key = mint_api_key("sak")
    kid = ids.new_id("key")
    db.execute(text("INSERT INTO identity_api_credentials (id, principal_id, kind, name, token_hash, scopes, "
                    "created_by) VALUES (:id, :p, 'sak', 'boss-egress', :th, '{}', :by)"),
               {"id": kid, "p": EGRESS_PRINCIPAL, "th": hash_token(key), "by": created_by})
    audit.record(audit.SecurityEvent(org_id=org_id, event_type="api_key.created", outcome=audit.Outcome.SUCCESS,
                                     actor_principal_id=created_by, actor_kind="system", target_type="api_key",
                                     target_id=kid, details={"kind": "sak", "owner": EGRESS_PRINCIPAL,
                                                             "revoked_others": revoke_others}), db=db)
    return CreatedKey(kid, key, None)


def introspect(token: str, *, db: Any) -> dict[str, Any]:
    """RFC 7662 for the egress service only (the route checks its credential: A40). Signature, time
    and jti liveness, plus the grant and chain status — and it RETURNS `act` (Keycloak #53029).
    No DPoP here: the caller is the proxy, not the token holder."""
    from . import grants, signing_keys

    try:
        _, raw = jwks.verify(token, typ=TYP_RUN,
                             key_for_kid=lambda kid: signing_keys.verification_key(kid, "run", db=db))
        if raw.get("iss") != issuer() or raw.get("aud") not in RUN_AUDIENCES:
            return {"active": False}
        claims = _parse_claims(raw)
        now = _now()
        if claims.exp <= now - CLOCK_SKEW_SECONDS or not _jti_live(db, claims.jti, claims.org_id):
            return {"active": False}
        live_state(db, org_id=claims.org_id, mode=claims.mode, sub=claims.sub, chain=claims.chain,
                   grant_id=claims.grant_id, task=claims.task)
    except (jwks.JoseError, TokenError):
        return {"active": False}
    grant = grants.load(claims.grant_id, db=db)
    out = {"active": True, "sub": claims.sub, "aud": claims.aud, "iss": claims.iss, "exp": claims.exp,
           "iat": claims.iat, "jti": claims.jti, "scope": " ".join(sorted(claims.scope)),
           "boss_org": claims.org_id, "boss_mode": claims.mode.value, "boss_chain": claims.chain.to_boss_chain(),
           "boss_depth": claims.depth, "boss_grant": claims.grant_id,
           "boss_grant_status": grant.status.value if grant else None,
           "boss_task": raw.get("boss_task"), "cnf": {"jkt": claims.cnf_jkt}, "token_type": "DPoP"}
    if raw.get("act") is not None:
        out["act"] = raw["act"]
    return out


# ---- MCP: no token passthrough, audience-bound tokens -------------------------------------------------------

class TokenPassthroughRefused(RuntimeError):
    """A BOSS credential was about to be sent to an upstream MCP server. Never (MCP security BCP)."""


_BOSS_PREFIXES = ("boss_pat_", "boss_sak_", "boss_scim_", "boss_egr_", "bss_")
_BOSS_TYPS = frozenset({TYP_RUN, TYP_ACTOR, "dpop+jwt"})


def is_boss_credential(value: str) -> bool:
    v = (value or "").strip()
    scheme, _, rest = v.partition(" ")
    for candidate in (v, rest):
        if candidate.startswith(_BOSS_PREFIXES):
            return True
        if candidate.count(".") == 2:
            try:
                if jwks.split(candidate).header.get("typ") in _BOSS_TYPS:
                    return True
            except jwks.JoseError:
                pass
    return False


def refuse_passthrough(headers: Mapping[str, str]) -> dict[str, str]:
    """The headers an UPSTREAM MCP call may carry: only the upstream server's own configured auth.
    A BOSS session, key, run token, actor assertion, DPoP proof or egress credential is refused."""
    out: dict[str, str] = {}
    for k, v in headers.items():
        if k.lower() in ("dpop", "cookie", "x-boss-csrf", "x-boss-egress-key"):
            raise TokenPassthroughRefused(k)
        if is_boss_credential(str(v)):
            raise TokenPassthroughRefused(k)
        out[k] = v
    return out


def verify_for_mcp(token: str, *, dpop_proof: Optional[str], htm: str, htu: str, db: Any) -> RunTokenClaims:
    """BOSS as an MCP server accepts only tokens minted FOR it (aud boss:mcp: the resource
    indicator, RFC 8707). An API or egress token is refused here, and vice versa."""
    return verify_run_token(token, audience=AUD_MCP, dpop_proof=dpop_proof, htm=htm, htu=htu, db=db)


__all__ = ["TokenError", "mint_run_token", "verify_run_token", "exchange_token", "renew_run_token",
           "mint_actor_assertion", "decide", "introspect", "live_state", "context_for", "issuer", "redacted_claims", "egress_caller_ok",
           "issue_egress_service_key", "EGRESS_PRINCIPAL",
           "AUD_API", "AUD_MCP", "AUD_EGRESS", "GRANT_TYPE_RENEW",
           "refuse_passthrough", "verify_for_mcp", "TokenPassthroughRefused"]
