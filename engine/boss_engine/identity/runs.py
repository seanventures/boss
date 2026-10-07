"""The run supervisor: starts a run under a delegated principal, spawns children through the token
exchange, renews, and ends it. Owned by S3. IDENTITY.md §7.6, §7.7, §7.10, §7.14.

A run is `run_<kind>:<entity>` (`ct` code task, `wf` workflow run, `ag` agent loop run, `job`, …);
a child run is `<parent run>/<name>`. The supervisor holds, per run, a `Shim`: the Ed25519 DPoP key
pair (generated here, never written anywhere) and the run's current tokens. When BOSS executes a
run in its own process (workflow steps, agent turns, the code-task orchestrator), the API process
IS the shim; in a microVM sandbox the shim is a separate uid inside the VM and only its public key
crosses the bootstrap channel (`start_run(..., dpop_jkt=...)`).

What never happens: a run gets a GitHub token (the egress proxy injects one), a long-lived BOSS
key, or a token wider than person ∩ ceilings ∩ grant ∩ task.
"""
from __future__ import annotations

import contextlib
import hashlib
import secrets
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator, Mapping, Optional

from . import dpop, tokens
from .delegation import (
    AUD_API,
    AUD_EGRESS,
    TOKEN_TYPE_ACTOR,
    TOKEN_TYPE_RUN,
    TOKEN_ENDPOINT,
    IssuedToken,
    TokenExchangeRequest,
)
from .principal import AgentInstance, AuthContext, RunTokenClaims, TaskBinding

EGRESS_PREFIX = "boss_egr_"
EGRESS_TTL = timedelta(hours=1)


def _hash(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass
class Shim:
    """The credential shim for one run: key + tokens. `repr` shows neither."""
    run_id: str
    key: dpop.DPoPKey = field(default_factory=dpop.DPoPKey.generate, repr=False)
    tokens: dict[str, IssuedToken] = field(default_factory=dict, repr=False)
    egress_credential: Optional[str] = field(default=None, repr=False)
    renewed_at: float = field(default_factory=time.monotonic, repr=False)

    def authorization(self, audience: str, method: str, url: str) -> dict[str, str]:
        """Headers for one call: `Authorization: DPoP <token>` and a fresh `DPoP` proof."""
        tok = self.tokens[audience].access_token
        return {"Authorization": f"DPoP {tok}", "DPoP": self.key.proof(method, url, access_token=tok)}

    def jti(self, audience: str) -> str:
        return self.tokens[audience].jti


@dataclass(frozen=True)
class RunStart:
    run_id: str
    agent_id: str
    project_id: Optional[str]
    repos: tuple[str, ...] = ()
    step: Optional[str] = None
    scope: frozenset[str] = frozenset()
    github_needs: Optional[Mapping[str, str]] = None


def _task(start: RunStart) -> TaskBinding:
    resources: dict[str, Any] = {}
    if start.repos:
        resources["github"] = list(start.repos)
    if start.github_needs:
        resources["github_permissions"] = dict(start.github_needs)
    return TaskBinding(run_id=start.run_id, project_id=start.project_id, step=start.step, resources=resources)


def start_run(ctx: AuthContext, start: RunStart, *, grant_id: Optional[str], db: Any,
              audiences: tuple[str, ...] = (AUD_API,), sandbox_id: Optional[str] = None) -> Shim:
    """The FIRST hop. `ctx` is the person's verified request context (obo) or the agent's own
    (autonomous). Mints one token per audience, all bound to this run's DPoP key; with repos, also
    an egress token and the run's `boss_egr_` credential."""
    shim = Shim(run_id=start.run_id)
    task = _task(start)
    agent = AgentInstance(start.agent_id, start.run_id)
    auds = list(audiences) + ([AUD_EGRESS] if start.repos and AUD_EGRESS not in audiences else [])
    for aud in auds:
        shim.tokens[aud] = tokens.mint_run_token(subject=ctx, agent=agent, grant_id=grant_id, task=task,
                                                 audience=aud, scope=start.scope, cnf_jkt=shim.key.jkt, db=db)
    if AUD_EGRESS in shim.tokens:
        shim.egress_credential = issue_egress_credential(shim, sandbox_id=sandbox_id or sandbox_network_id(), db=db)
    with _lock:
        _SHIMS[start.run_id] = shim
    return shim


def spawn_child(parent: Shim, *, child_agent_id: str, child_name: str, scope: frozenset[str],
                db: Any, base_url: str, audience: str = AUD_API,
                audiences: Optional[tuple[str, ...]] = None) -> Shim:
    """Spawn a sub-agent through BOSS: the supervisor mints the actor assertion for the child run
    (bound to the CHILD's new key) and the parent's shim performs the RFC 8693 exchange with its own
    token and DPoP proof. The child never sees the parent's token or key."""
    parent_token = parent.tokens[AUD_API]
    parent_claims = tokens.verify_run_token(
        parent_token.access_token, audience=AUD_API,
        dpop_proof=parent.key.proof("POST", base_url + "/v1/identity/spawn", access_token=parent_token.access_token),
        htm="POST", htu=base_url + "/v1/identity/spawn", db=db)
    child = Shim(run_id=f"{parent.run_id}/{child_name}")
    htu = base_url + TOKEN_ENDPOINT
    for aud in (audiences or (audience,)):
        # One exchange per audience, each with its own single-use actor assertion, all bound to
        # the CHILD's key: every one passes the full spawn checks (§7.5 1-8).
        assertion = tokens.mint_actor_assertion(parent=parent_claims, child_agent_id=child_agent_id,
                                                child_run_id=child.run_id, child_jkt=child.key.jkt, db=db)
        request = TokenExchangeRequest(
            subject_token=parent_token.access_token, subject_token_type=TOKEN_TYPE_RUN, actor_token=assertion,
            actor_token_type=TOKEN_TYPE_ACTOR, audience=aud, scope=frozenset(scope),
            dpop_proof=parent.key.proof("POST", htu, access_token=parent_token.access_token))
        child.tokens[aud] = tokens.exchange_token(request, htm="POST", htu=htu, db=db)
    with _lock:
        _SHIMS[child.run_id] = child
    return child


def renew(shim: Shim, *, db: Any, base_url: str) -> None:
    htu = base_url + TOKEN_ENDPOINT
    for aud, tok in list(shim.tokens.items()):
        shim.tokens[aud] = tokens.renew_run_token(tok.access_token,
                                                  dpop_proof=shim.key.proof("POST", htu, access_token=tok.access_token),
                                                  htm="POST", htu=htu, db=db)
    if AUD_EGRESS in shim.tokens:
        from sqlalchemy import text

        db.execute(text("UPDATE identity_egress_credentials SET jti = :j WHERE run_id = :r AND revoked_at IS NULL"),
                   {"j": shim.tokens[AUD_EGRESS].jti, "r": shim.run_id})
        if shim.egress_credential:
            register_egress(raw_hash=_hash(shim.egress_credential), token=shim.tokens[AUD_EGRESS].access_token, db=db)
            publish_egress(raw_hash=_hash(shim.egress_credential), token=shim.tokens[AUD_EGRESS].access_token,
                           strict=False)


def end_run(run_id: str, *, db: Any, org_id: str, reason: str = "run ended") -> None:
    """Run end: every token of the run and its children dies, egress credentials die, "this build
    only" grants end, and GitHub installation tokens are revoked at GitHub."""
    from sqlalchemy import text

    from . import github_app, grants

    db.execute(text("UPDATE identity_run_credentials SET revoked_at = now() WHERE (run_id = :r OR run_id LIKE :c) "
                    "AND revoked_at IS NULL"), {"r": run_id, "c": run_id + "/%"})
    db.execute(text("UPDATE identity_egress_credentials SET revoked_at = now() WHERE (run_id = :r OR run_id LIKE :c) "
                    "AND revoked_at IS NULL"), {"r": run_id, "c": run_id + "/%"})
    grants.end_task_grants(run_id, db=db, org_id=org_id)
    github_app.revoke_runs([run_id], reason)
    # A SEPARATE boss-egress process holds the run's installation tokens in ITS memory: tell it
    # (identity_revocation_notices + NOTIFY), so they are revoked at GitHub there too.
    from . import cascade

    cascade.notify(db, trigger="run_ended", target_id=run_id, runs=[run_id], reason=reason)
    with _lock:
        for rid in [k for k in _SHIMS if k == run_id or k.startswith(run_id + "/")]:
            _SHIMS.pop(rid, None)


_SHIMS: dict[str, Shim] = {}
_lock = threading.Lock()

# ---- renewal at runtime: a run's tokens (and egress) never die at 5 minutes mid-build ----------------

RENEW_AFTER_SECONDS = 240           # the shim renews every 4 minutes (tokens live 5)
_ENGINE: Any = None
_RENEWER: Optional[threading.Thread] = None


def remember_engine(engine: Any) -> None:
    """The run supervisor's engine: renew live runs' tokens in the background, and stop a build
    whose authority is revoked the moment it is (this process's cascades, after commit) or within
    seconds (another process's, by polling)."""
    global _ENGINE, _RENEWER
    from . import cascade

    _ENGINE = engine
    cascade.register_after_commit(_hard_stop_hook)
    with _lock:
        if _RENEWER is None or not _RENEWER.is_alive():
            _RENEWER = threading.Thread(target=_renew_forever, daemon=True, name="identity-run-renewer")
            _RENEWER.start()


def _hard_stop_hook(run_ids: list[str], _reason: str) -> None:
    for rid in run_ids:
        stop_run_processes(rid)


def enforce_revocations(engine: Any) -> list[str]:
    """Stop every build running in THIS process whose authority another process revoked: a live
    shim whose current token was revoked, or a build with an unhonoured `authority_revoked` stop
    request (a legacy-path build has no token). Returns the run ids stopped."""
    from sqlalchemy import text

    from ..services import owned_process

    with _lock:
        by_jti = {tok.jti: s.run_id for s in _SHIMS.values() for tok in s.tokens.values()}
    live_builds = sorted(owned_process.live_owner_ids("code_run"))
    stopped: list[str] = []
    if not by_jti and not live_builds:
        return stopped
    with engine.begin() as db:
        if by_jti:
            for jti in db.execute(text("SELECT jti FROM identity_run_credentials WHERE jti = ANY(:j) "
                                       "AND revoked_at IS NOT NULL"), {"j": list(by_jti)}).scalars().all():
                stopped.append(by_jti[jti])
        if live_builds and db.execute(text("SELECT to_regclass('code_task_stop_requests') IS NOT NULL")).scalar():
            for crun in db.execute(text("SELECT run_id FROM code_task_stop_requests WHERE run_id = ANY(:r) "
                                        "AND phase = 'authority_revoked' AND honoured_at IS NULL"),
                                   {"r": live_builds}).scalars().all():
                stopped.append(f"run_ct:{crun}")
    for rid in sorted(set(stopped)):
        with _lock:
            _SHIMS.pop(rid, None)
        stop_run_processes(rid)
    return sorted(set(stopped))


def renew_due(engine: Any, *, now: Optional[float] = None) -> dict[str, str]:
    """Renew every live shim whose tokens are older than RENEW_AFTER_SECONDS. A refusal (grant
    revoked, person disabled, parent run ended) is final for that run: its shim is dropped and its
    sandbox processes are killed, so a revoked run stops even mid-stage."""
    import time as _time

    now = _time.monotonic() if now is None else now
    out: dict[str, str] = {}
    with _lock:
        due = [s for s in _SHIMS.values() if s.tokens and now - s.renewed_at >= RENEW_AFTER_SECONDS]
    for shim in due:
        try:
            with engine.begin() as db:
                renew(shim, db=db, base_url=tokens.issuer())
            shim.renewed_at = now
            out[shim.run_id] = "renewed"
        except tokens.TokenError as exc:
            out[shim.run_id] = f"refused:{exc.reason}"
            with _lock:
                _SHIMS.pop(shim.run_id, None)
            stop_run_processes(shim.run_id)
    return out


def _renew_forever() -> None:  # pragma: no cover — the loop around renew_due
    import logging
    import time as _time

    while True:
        _time.sleep(2)
        try:
            if _ENGINE is not None:
                enforce_revocations(_ENGINE)
                renew_due(_ENGINE)
        except Exception:  # noqa: BLE001
            logging.getLogger("boss.identity.runs").warning("run supervision pass failed", exc_info=True)


def stop_run_processes(run_id: str) -> None:
    """HARD STOP: every process of the build behind `run_id` (`run_ct:<crun>`, or one nested in a
    workflow run) is killed now — through the owner registry (its process groups, wherever it is in
    its stage) and its sandbox uid (anything that escaped the group). Not only at a stage boundary."""
    from ..services import owned_process, sandbox_identity
    from . import cascade

    for kind, entity in cascade.run_segments(run_id):
        if kind != "ct":
            continue
        try:
            owned_process.terminate_owner(entity, reason="cancel", grace=0)
        except Exception:  # noqa: BLE001
            pass
        sandbox_identity.end_owner(entity)


def shim_for(run_id: str) -> Optional[Shim]:
    with _lock:
        return _SHIMS.get(run_id)


# ---- the MCP bridge: an agent run calling an API-surface tool ---------------------------------------------
#
# An API-surface tool forwards the call to the API AS THE CALLER (M8). An agent run's own token is
# bound to its run's DPoP key (and, over MCP HTTP, to audience boss:mcp), so it cannot be forwarded.
# The bridge presents a `boss:api` run token of THE SAME RUN instead: in-process delegated work
# already holds one (its shim); otherwise the bridge mints a sibling for the same first-hop agent,
# person, grant, task and scope, bound to a key only the bridge holds. A deeper chain is never
# re-minted as a first hop (that would skip the parent hops' ceilings): it is refused.

_BRIDGE: dict[str, Shim] = {}


def bridge_headers(ctx: AuthContext, method: str, path: str, *, engine: Any,
                   request_origin: str) -> Optional[dict[str, str]]:
    """`Authorization: DPoP …` + `DPoP` proof headers for one API call made for the run `ctx` acts
    in, or None when `ctx` is not an agent run (or the run cannot be bridged: fail closed)."""
    from . import lineage
    from .authn_support import public_origin

    rt = getattr(ctx, "run_token", None)
    if rt is None:
        return None
    shim = shim_for(rt.task.run_id)
    if shim is None or AUD_API not in shim.tokens or shim.jti(AUD_API) != rt.jti:
        shim = _BRIDGE.get(rt.jti)
    if shim is None:
        if rt.chain.depth != 1 or rt.chain.current is None:
            return None
        with engine.begin() as db:
            subject = subject_for(lineage.Acting.from_context(ctx), db=db)
            shim = Shim(run_id=rt.task.run_id)
            shim.tokens[AUD_API] = tokens.mint_run_token(
                subject=subject, agent=AgentInstance(rt.chain.current.agent_id, rt.task.run_id),
                grant_id=rt.grant_id, task=rt.task, audience=AUD_API, scope=frozenset(rt.scope),
                cnf_jkt=shim.key.jkt, db=db)
        with _lock:
            if len(_BRIDGE) > 512:
                _BRIDGE.clear()
            _BRIDGE[rt.jti] = shim
    with engine.begin() as db:
        base = public_origin(db) or request_origin
    return shim.authorization(AUD_API, method.upper(), base.rstrip("/") + path)


# ---- the per-run egress credential ---------------------------------------------------------------------

SANDBOX_ADDRS_ENV = "BOSS_SANDBOX_SOURCE_ADDRS"
TOFU = "tofu:"


def sandbox_network_id() -> str:
    """The network identity of the sandbox this process starts (#16): the addresses its traffic
    to boss-egress comes from. Sandboxes run in this container (as their own uid), so these are
    this container's addresses; `BOSS_SANDBOX_SOURCE_ADDRS` overrides (a NAT in between). When
    nothing usable is known (a developer's laptop), the first request binds it (trust on first
    use) and every later request must come from there."""
    import os
    import socket

    configured = [a.strip() for a in (os.environ.get(SANDBOX_ADDRS_ENV) or "").split(",") if a.strip()]
    if configured:
        return "net:" + ",".join(sorted(set(configured)))
    addrs: set[str] = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None):
            addrs.add(str(info[4][0]))
    except OSError:
        pass
    routable = {a for a in addrs if not a.startswith("127.") and a != "::1"}
    return ("net:" + ",".join(sorted(routable | {"127.0.0.1", "::1"}))) if routable else TOFU


def _check_network(db: Any, row: Mapping[str, Any], peer: Optional[str]) -> None:
    from sqlalchemy import text

    bound = str(row.get("sandbox_id") or "")
    if peer is None:
        return                                        # in-process callers (tests, the API itself)
    if bound.startswith("net:"):
        if peer not in bound[4:].split(","):
            raise tokens.TokenError("unauthenticated", 401, "egress_wrong_sandbox")
    elif bound == TOFU or not bound:
        db.execute(text("UPDATE identity_egress_credentials SET sandbox_id = :n WHERE token_hash = :h "
                        "AND (sandbox_id = :t OR sandbox_id IS NULL OR sandbox_id = '')"),
                   {"n": "net:" + peer, "h": row["token_hash"], "t": TOFU})
        again = db.execute(text("SELECT sandbox_id FROM identity_egress_credentials WHERE token_hash = :h"),
                           {"h": row["token_hash"]}).scalar()
        if str(again) != "net:" + peer:
            raise tokens.TokenError("unauthenticated", 401, "egress_wrong_sandbox")
    else:
        # A sandbox id that is not a network identity (rows from before #16): bind now.
        db.execute(text("UPDATE identity_egress_credentials SET sandbox_id = :n WHERE token_hash = :h "
                        "AND sandbox_id = :old"), {"n": "net:" + peer, "h": row["token_hash"], "old": bound})


def issue_egress_credential(shim: Shim, *, sandbox_id: str, db: Any) -> str:
    """`boss_egr_` + 32 random bytes. Stored hashed, bound to the run and sandbox, ≤1 h, revoked at
    run end. The egress proxy maps it to the run's egress token and re-checks liveness per request."""
    from sqlalchemy import text

    from .ids import new_id

    raw = EGRESS_PREFIX + secrets.token_urlsafe(32)
    db.execute(text("INSERT INTO identity_egress_credentials (id, token_hash, run_id, sandbox_id, jti, expires_at) "
                    "VALUES (:id, :h, :r, :s, :j, :e)"),
               {"id": new_id("egr"), "h": _hash(raw), "r": shim.run_id, "s": sandbox_id,
                "j": shim.tokens[AUD_EGRESS].jti, "e": datetime.now(timezone.utc) + EGRESS_TTL})
    register_egress(raw_hash=_hash(raw), token=shim.tokens[AUD_EGRESS].access_token, db=db)
    publish_egress(raw_hash=_hash(raw), token=shim.tokens[AUD_EGRESS].access_token)
    return raw


def publish_egress(*, raw_hash: str, token: str, strict: bool = True) -> bool:
    """Hand the run's egress token to a SEPARATE boss-egress process (compose's `egress`), server
    to server with the shared service key. In-process egress (tests, a single-process install)
    needs no hand-off: there is no key configured. A failed hand-off at run start fails the run:
    the credential would be refused anyway."""
    import os

    import httpx

    key = tokens.egress_service_key()
    base = (os.environ.get(EGRESS_URL_ENV) or "").rstrip("/")
    if not key or not base:
        return False
    try:
        resp = httpx.post(base + "/_boss/register", json={"credential_hash": raw_hash, "token": token},
                          headers={"X-Boss-Egress-Key": key}, timeout=10.0)
        ok = resp.status_code == 200
    except httpx.HTTPError:
        ok = False
    if not ok and strict:
        raise tokens.TokenError("unavailable", 503, "egress_unreachable")
    return ok


#: credential hash -> the run's verified egress claims (in the egress process's memory).
_EGRESS: dict[str, RunTokenClaims] = {}


def register_egress(*, raw_hash: str, token: str, db: Any) -> RunTokenClaims:
    """The supervisor hands the egress proxy the run's egress token (server to server). The proxy
    verifies its signature, issuer and audience; DPoP does not apply (the proxy never presents it)."""
    from . import jwks, signing_keys

    _, raw = jwks.verify(token, typ=tokens.TYP_RUN,
                         key_for_kid=lambda kid: signing_keys.verification_key(kid, "run", db=db))
    if raw.get("aud") != AUD_EGRESS or raw.get("iss") != tokens.issuer():
        raise tokens.TokenError("unauthenticated", 401, "not_an_egress_token")
    claims = tokens._parse_claims(raw)
    with _lock:
        _EGRESS[raw_hash] = claims
    return claims


def resolve_egress(raw: Optional[str], *, db: Any, peer: Optional[str] = None) -> RunTokenClaims:
    """boss_egr -> run -> run token -> LIVE checks (credential, network identity, jti, grant,
    chain, person). `peer` is the request's source address: a credential presented from anywhere
    but its sandbox is refused (#16)."""
    from sqlalchemy import text

    if not raw or not raw.startswith(EGRESS_PREFIX) or len(raw) > 128:
        raise tokens.TokenError("unauthenticated", 401, "no_egress_credential")
    h = _hash(raw)
    row = db.execute(text("SELECT token_hash, run_id, jti, revoked_at, expires_at, sandbox_id "
                          "FROM identity_egress_credentials WHERE token_hash = :h"), {"h": h}).mappings().first()
    if row is None or row["revoked_at"] is not None or row["expires_at"] <= datetime.now(timezone.utc):
        raise tokens.TokenError("unauthenticated", 401, "egress_credential_dead")
    _check_network(db, row, peer)
    with _lock:
        claims = _EGRESS.get(h)
    if claims is None or claims.task.run_id != row["run_id"]:
        raise tokens.TokenError("unauthenticated", 401, "egress_not_registered")
    if claims.exp <= int(datetime.now(timezone.utc).timestamp()) - tokens.CLOCK_SKEW_SECONDS:
        raise tokens.TokenError("unauthenticated", 401, "egress_token_expired")   # the supervisor stopped renewing
    if not tokens._jti_live(db, claims.jti, claims.org_id):
        raise tokens.TokenError("unauthenticated", 401, "jti_revoked")
    tokens.live_state(db, org_id=claims.org_id, mode=claims.mode, sub=claims.sub, chain=claims.chain,
                      grant_id=claims.grant_id, task=claims.task)
    return claims


# ---- wiring for BOSS's run kinds -------------------------------------------------------------------------

EGRESS_URL_ENV = "BOSS_EGRESS_URL"          # e.g. https://egress.boss.internal (set = egress is ON)


def egress_enabled() -> bool:
    import os

    return bool((os.environ.get(EGRESS_URL_ENV) or "").strip())


def subject_for(acting: Any, *, db: Any) -> AuthContext:
    """The subject context for a run the API starts for a job or a stored request: the principal
    that was VERIFIED when the work was enqueued (lineage.Acting), re-read live. Never a body field."""
    from . import grants
    from .principal import AuthMethod

    principal = grants.principal(db, acting.principal_id)
    if principal is None or not principal.active:
        raise tokens.TokenError("unauthenticated", 401, "subject_inactive")
    return AuthContext(principal=principal, method=AuthMethod.SYSTEM)


def agent_for_role(db: Any, project_id: Optional[str], role_key: Optional[str]) -> Optional[str]:
    """The Passport id for an agent role (`roles.agent_id`)."""
    from sqlalchemy import text

    if not role_key:
        return None
    if not db.execute(text("SELECT to_regclass('roles') IS NOT NULL")).scalar():
        return None
    has_project = db.execute(text("SELECT 1 FROM information_schema.columns WHERE table_name = 'roles' "
                                  "AND column_name = 'project_id'")).scalar()
    if has_project:
        return db.execute(text("SELECT agent_id FROM roles WHERE role_key = :r AND agent_id IS NOT NULL "
                               "ORDER BY (project_id = :p) DESC LIMIT 1"), {"r": role_key, "p": project_id}).scalar()
    return db.execute(text("SELECT agent_id FROM roles WHERE role_key = :r AND agent_id IS NOT NULL LIMIT 1"),
                      {"r": role_key}).scalar()


@dataclass(frozen=True)
class OwnGit:
    """How BOSS's OWN git (the clone, checkpoints, the final push) gets the run's egress
    credential: from its own process environment, never from the shim socket (which belongs to
    the run's sandbox uid) and never from disk. The agent's uid cannot read that environment."""
    config: tuple[tuple[str, str], ...]
    env: Mapping[str, str] = field(default_factory=dict, repr=False)


@dataclass(frozen=True)
class CloneAccess:
    """How a sandbox reaches a repo through egress: a credential-free URL plus the git config that
    makes git ask the shim (never a file in the workspace) for the run's boss_egr credential."""
    run_id: str
    clone_url: str
    git_config: tuple[tuple[str, str], ...]
    helper_env: Mapping[str, str] = field(default_factory=dict)
    own_git: Optional[OwnGit] = field(default=None, repr=False)


_OWN_HELPER = ("!f() { test \"$1\" = get && printf 'username=boss-egress\\npassword=%s\\n' "
               "\"$BOSS_EGR_CREDENTIAL\"; }; f")


def egress_access(shim: Shim, *, owner: str, repo: str) -> CloneAccess:
    import os

    base = (os.environ.get(EGRESS_URL_ENV) or "").rstrip("/")
    socket_path = serve_shim_socket(shim)
    # git runs the helper with the action ("get", "store", "erase"); only "get" answers.
    helper = ("!f() { python3 -c 'import socket,sys; s=socket.socket(socket.AF_UNIX); s.connect(sys.argv[1]); "
              "s.sendall(sys.argv[2].encode()); sys.stdout.write(s.recv(4096).decode())' "
              f"{socket_path} \"$1\"; }}; f")
    own = OwnGit(config=(("credential.helper", ""), ("credential.helper", _OWN_HELPER)),
                 env={"BOSS_EGR_CREDENTIAL": shim.egress_credential or ""})
    return CloneAccess(
        run_id=shim.run_id, clone_url=f"{base}/github/{owner}/{repo}.git",
        git_config=((f"url.{base}/github/.insteadOf", "https://github.com/"),
                    ("credential.helper", ""), ("credential.helper", helper),
                    ("credential.useHttpPath", "true")),
        helper_env={"BOSS_SHIM_SOCKET": socket_path}, own_git=own)


# ---- the in-process shim socket ----------------------------------------------------------------------------

_SOCKETS: dict[str, Any] = {}


def peer_uid(conn: Any) -> Optional[int]:
    """The connecting process's uid, from the kernel (SO_PEERCRED on Linux, LOCAL_PEERCRED on
    macOS). None when the platform cannot say: the caller then refuses."""
    import socket
    import struct
    import sys

    try:
        if sys.platform.startswith("linux"):
            raw = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
            return struct.unpack("3i", raw)[1]
        if sys.platform == "darwin":
            raw = conn.getsockopt(0, 0x001, 76)          # SOL_LOCAL, LOCAL_PEERCRED -> struct xucred
            return struct.unpack("Ii", raw[:8])[1]
    except OSError:
        return None
    return None


def serve_shim_socket(shim: Shim) -> str:
    """A unix socket answering git's credential helper with the run's boss_egr credential, held in
    this process only.

    Inside a run that holds a sandbox uid (services.sandbox_identity) the socket is in the
    boss-owned 0711 shim root, owned by THAT uid with mode 0600, and the kernel-reported peer uid
    must be that uid: no other run's agent can connect or be answered. Without isolation (a
    developer's machine) the socket is 0600 in a 0700 directory and only this uid is answered."""
    import os
    import socket
    import tempfile

    from ..services import sandbox_identity

    with _lock:
        existing = _SOCKETS.get(shim.run_id)
    if existing is not None:
        return existing[0]
    ident = sandbox_identity.current()
    if ident is not None:
        root = sandbox_identity.SHIM_ROOT
        allowed = {ident.uid}
    else:
        # AF_UNIX paths are short (104 bytes on macOS): default to /tmp, not a long per-user TMPDIR.
        root = os.environ.get("BOSS_SHIM_DIR") or ("/tmp/boss-shim" if os.path.isdir("/tmp") else
                                                   os.path.join(tempfile.gettempdir(), "boss-shim"))
        os.makedirs(root, mode=0o700, exist_ok=True)
        os.chmod(root, 0o700)
        allowed = {os.getuid()}
    path = os.path.join(root, hashlib.sha256(shim.run_id.encode()).hexdigest()[:24] + ".sock")
    if os.path.lexists(path):
        os.unlink(path)
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    old = os.umask(0o177)
    try:
        srv.bind(path)
    finally:
        os.umask(old)
    os.chmod(path, 0o600)
    if ident is not None:
        ident.adopt_socket(path)              # <uid>:<uid> 0600
    srv.listen(4)

    def loop() -> None:
        while True:
            try:
                conn, _ = srv.accept()
            except OSError:
                return
            with conn:
                try:
                    if peer_uid(conn) not in allowed:
                        continue                  # not this run's agent: nothing is said
                    request = conn.recv(4096).decode("utf-8", "replace")
                    if request.startswith("get") and shim.egress_credential:
                        conn.sendall(f"username=boss-egress\npassword={shim.egress_credential}\n".encode())
                except OSError:
                    pass

    threading.Thread(target=loop, daemon=True, name=f"shim-{shim.run_id[:20]}").start()
    with _lock:
        _SOCKETS[shim.run_id] = (path, srv)
    return path


def close_shim_socket(run_id: str) -> None:
    import os

    with _lock:
        entry = _SOCKETS.pop(run_id, None)
    if entry:
        try:
            entry[1].close()
            os.unlink(entry[0])
        except OSError:
            pass


def _acting_of_job(engine: Any, job_id: Optional[str]) -> Any:
    from . import lineage

    if not job_id:
        return None
    with engine.begin() as db:
        return lineage.acting_for_job_id(db, job_id)


def original_build_authority(db: Any, task_run_id: str) -> Any:
    """The authority a build was STARTED under: the principal + grant stamped on the FIRST code.task
    job of that run (written in the same transaction as the run). A retry, a resume after plan
    approval, a spend-cap failover or a lease reclaim re-enqueues the build from a system thread
    or the worker; each carries this, never the re-enqueuer's identity (#6)."""
    from sqlalchemy import text

    from . import lineage

    if not db.execute(text("SELECT to_regclass('jobs') IS NOT NULL")).scalar():
        return None
    row = db.execute(text(
        "SELECT payload, on_behalf_of, grant_id, chain, on_behalf_system FROM jobs WHERE kind = 'code.task' "
        "AND payload->>'run_id' = :r ORDER BY created_at, id LIMIT 1"), {"r": task_run_id}).mappings().first()
    if row is None:
        return None
    try:
        return lineage.acting_for_job(dict(row))
    except lineage.PrincipalRequired:
        return None


class NoAuthority(tokens.TokenError):
    """A build with no delegated grant behind it (#5-8). Under enforcement this is the answer: the
    shared GitHub token is never the fallback."""

    def __init__(self, reason: str):
        super().__init__("forbidden", 403, reason)


def _no_authority(reason: str, *, task_run_id: str) -> None:
    """Under BOSS_AUTHZ_MODE=enforce: refuse. In shadow: say so LOUDLY (once per build, at
    WARNING, with the reason) and let the caller keep the legacy path, which posture reports."""
    import logging

    from .lineage import _enforced

    if _enforced():
        raise NoAuthority(reason)
    logging.getLogger("boss.identity.runs").warning(
        "[identity] SHADOW: build %s runs WITHOUT a delegated grant (%s) and would be REFUSED under "
        "BOSS_AUTHZ_MODE=enforce; it falls back to the shared GitHub credential.", task_run_id, reason)


BUILD_SCOPE = frozenset({"code_task:read", "code_task:steer", "repository:read", "github:read", "github:sync"})
#: What a build declares it does on GitHub (the run's step needs): push its branch, open its PR.
BUILD_GITHUB_NEEDS = {"contents": "write", "pull_requests": "write", "checks": "read", "statuses": "read"}


def start_code_task_run(engine: Any, *, task_run_id: str, project_id: str, owner: str, repo: str,
                        role_key: Optional[str], acting: Any = None,
                        job_id: Optional[str] = None) -> Optional[tuple[Shim, CloneAccess]]:
    """Wire a code task to its delegated principal: a run token for the person's grant, an egress
    credential, and the clone access.

    WHOSE AUTHORITY: the one the build was started under (`original_build_authority`, else the
    job `job_id`), adopted when nobody else is acting here (the worker calling in, or the same
    person); otherwise the ambient one. A retry or failover never loses it (#6).

    NO GRANT IS AN ERROR (#5-8): no principal, a system principal, no grant, no Passport for the
    builder, or no egress proxy -> NoAuthority under enforcement (never the shared token); in
    shadow, None and a loud warning (the legacy path). A build inside a delegated run (the acting
    authority carries a chain) is minted as a CHILD hop of that run through the token exchange,
    with every spawn check (can_spawn, allowed_children, the grant's child list, depth, cycles);
    when that run is not live here, it is refused."""
    from . import lineage

    if acting is None:
        ambient = lineage.current()
        with engine.begin() as db:
            recorded = original_build_authority(db, task_run_id)
        if recorded is None and job_id:
            recorded = _acting_of_job(engine, job_id)
        acting = recorded if (recorded is not None and _adoptable(ambient, recorded)) else ambient
    if not egress_enabled():
        _no_authority("egress_not_configured", task_run_id=task_run_id)
        return None
    if acting is None or getattr(acting, "system", False) or not acting.grant_id:
        _no_authority("no_grant" if acting is not None else "no_principal", task_run_id=task_run_id)
        return None
    with engine.begin() as db:
        agent_id = agent_for_role(db, project_id, role_key or "forge")
        if not agent_id:
            _no_authority("builder_has_no_passport", task_run_id=task_run_id)
            return None
        if acting.chain:
            parent = shim_for(acting.run_id or "")
            if parent is None:
                _no_authority("parent_run_not_live", task_run_id=task_run_id)
                return None
            from . import tokens as tok

            shim = spawn_child(parent, child_agent_id=agent_id, child_name=f"ct:{task_run_id}", scope=BUILD_SCOPE,
                               db=db, base_url=tok.issuer(), audiences=(AUD_API, AUD_EGRESS))
            if f"{owner}/{repo}".lower() not in {r.lower() for r in _task_repos(shim, db)}:
                end_run(shim.run_id, db=db, org_id=acting.org_id)
                _no_authority("repo_outside_parent_task", task_run_id=task_run_id)
                return None
            shim.egress_credential = issue_egress_credential(shim, sandbox_id=sandbox_network_id(), db=db)
        else:
            if acting.run_id and acting.run_id.startswith("run_wf:"):
                rid = f"{acting.run_id}/ct:{task_run_id}"        # a build inside a workflow run
            elif acting.run_id and acting.run_id.startswith("run_ct:"):
                rid = acting.run_id
            else:
                rid = f"run_ct:{task_run_id}"
            shim = start_run(subject_for(acting, db=db),
                             RunStart(run_id=rid, agent_id=agent_id, project_id=project_id,
                                      repos=(f"{owner}/{repo}",), scope=BUILD_SCOPE,
                                      github_needs=BUILD_GITHUB_NEEDS),
                             grant_id=acting.grant_id, db=db, audiences=(AUD_API,))
    remember_engine(engine)
    return shim, egress_access(shim, owner=owner, repo=repo)


def _task_repos(shim: Shim, db: Any) -> list[str]:
    from . import jwks, signing_keys

    _, raw = jwks.verify(shim.tokens[AUD_EGRESS].access_token, typ=tokens.TYP_RUN,
                         key_for_kid=lambda kid: signing_keys.verification_key(kid, "run", db=db))
    return list(((raw.get("boss_task") or {}).get("resources") or {}).get("github") or [])


@contextlib.contextmanager
def repo_access(engine: Any, *, host: str, owner: str, repo: str, project_id: Optional[str],
                token: Any = "", role_key: Optional[str] = None) -> Iterator[Any]:
    """How BOSS-side code (an agent's shell workspace, the read tools' clones, a preview or deploy
    workdir) runs git against a repo, as a `repo_host.GitAccess`.

    With the egress proxy on and a delegated principal in context (a person's grant, or a run
    acting under one), the clone goes through boss-egress under a short run of its own (the
    run's agent for that person: run_<…>/git:<id>), and no GitHub token exists on this side; a
    refused authority raises, it never falls back to the shared token. Otherwise the legacy token
    (`token`, a string or a callable) is used — carried in git's environment only, never in the
    URL, argv or the checkout's `.git/config`, so the code that later runs in that directory can
    never read it."""
    from . import lineage
    from ..services.repo_host import GitAccess, git_access, git_config_env

    acting = lineage.current()
    kind = (host or "github").strip().lower()
    if kind == "github" and engine is not None and egress_enabled() and acting is not None and acting.grant_id:
        shim = None
        with engine.begin() as db:
            agent_id = (acting.chain[-1].split("#", 1)[0] if acting.chain
                        else agent_for_role(db, project_id, role_key or "forge"))
            if agent_id:
                rid = _nested_run_id(acting, "git", secrets.token_hex(6))
                shim = start_run(subject_for(acting, db=db),
                                 RunStart(run_id=rid, agent_id=agent_id, project_id=project_id,
                                          repos=(f"{owner}/{repo}",),
                                          scope=frozenset({"repository:read", "github:read"})),
                                 grant_id=acting.grant_id, db=db, audiences=(AUD_API,))
        if shim is not None:
            access = egress_access(shim, owner=owner, repo=repo)
            try:
                yield GitAccess(access.clone_url, git_config_env(access.git_config), via="egress")
            finally:
                close_shim_socket(shim.run_id)
                with engine.begin() as db:
                    end_run(shim.run_id, db=db, org_id=acting.org_id)
            return
    if kind == "github" and engine is not None:
        # NO GRANT (a system loop, a preview of a build, a read tool outside any run): never the
        # shared token silently (round 2 e). With egress on, the clone runs as BOSS's own read-only
        # system agent under its explicit, audited authority; under enforce without egress it is
        # refused; in shadow the legacy token is used and that use is recorded.
        if egress_enabled():
            with engine.begin() as db:
                agent_ctx = system_repo_reader(db, project_id=project_id)
                shim = None
                if agent_ctx is not None:
                    rid = f"run_sys:git:{secrets.token_hex(6)}"
                    shim = start_run(agent_ctx, RunStart(run_id=rid, agent_id=agent_ctx.principal.id,
                                                         project_id=project_id, repos=(f"{owner}/{repo}",),
                                                         scope=SYSTEM_GIT_SCOPE),
                                     grant_id=None, db=db, audiences=(AUD_API,))
            if shim is not None:
                access = egress_access(shim, owner=owner, repo=repo)
                try:
                    yield GitAccess(access.clone_url, git_config_env(access.git_config), via="egress")
                finally:
                    close_shim_socket(shim.run_id)
                    with engine.begin() as db:
                        end_run(shim.run_id, db=db, org_id=agent_ctx.principal.org_id)
                return
        if lineage._enforced():  # noqa: SLF001
            _no_authority("no grant and no system repo reader (is boss-egress configured?)",
                          task_run_id=f"git:{owner}/{repo}")
        _note_shared_token_use(engine, owner=owner, repo=repo, project_id=project_id)
    raw = token() if callable(token) else token
    yield git_access(kind, owner, repo, raw or "")


#: BOSS's own read-only GitHub reader: clones and reads for work that acts for nobody (previews of a
#: finished build, the read tools, a system loop). An AUTONOMOUS agent with an explicit authority —
#: a Passport whose ceiling and GitHub ceiling are read-only, bound to Viewer at the org by the
#: system and recorded in the audit log — instead of the shared GitHub token.
SYSTEM_REPO_READER = "agt_sys_repo_reader"
SYSTEM_GIT_SCOPE = frozenset({"repository:read", "github:read"})
SYSTEM_GIT_EXTERNAL = {"github": {"contents": "read", "metadata": "read"}}


def system_repo_reader(db: Any, *, project_id: Optional[str]) -> Optional[AuthContext]:
    """The system reader's context, creating its Passport and authority once (idempotent, audited).
    None before the install has an Owner (a Passport needs an accountable person)."""
    import json

    from sqlalchemy import text

    from . import audit, passports, users
    from .principal import AuthMethod

    org = None
    if project_id:
        org = db.execute(text("SELECT w.org_id FROM projects p JOIN workspaces w ON w.id = p.workspace_id "
                              "WHERE p.id = :p"), {"p": project_id}).scalar()
    if not org:
        from .migration import default_org
        org = default_org(db)
    if not org:
        return None
    p = passports.get(SYSTEM_REPO_READER, db=db)
    if p is None:
        owners = sorted(users.active_owner_ids(db, org))
        if not owners:
            return None
        passports.ensure_passport(org_id=org, agent_id=SYSTEM_REPO_READER, role_key="system_repo_reader",
                                  display_name="BOSS repo reader (system)", sponsor_principal_id=owners[0],
                                  db=db, autonomous=True, can_spawn=False, max_depth=0,
                                  created_by="sys_migration")
        for key in sorted(SYSTEM_GIT_SCOPE):
            db.execute(text("INSERT INTO identity_agent_ceilings (agent_id, permission_key) VALUES (:a, :k) "
                            "ON CONFLICT DO NOTHING"), {"a": SYSTEM_REPO_READER, "k": key})
        db.execute(text("UPDATE identity_agent_passports SET external_ceiling = CAST(:e AS JSONB) "
                        "WHERE agent_id = :a"), {"e": json.dumps(SYSTEM_GIT_EXTERNAL), "a": SYSTEM_REPO_READER})
        users.add_binding(db, org_id=org, subject_id=SYSTEM_REPO_READER, role_key="viewer",
                          granted_by="sys_migration",
                          reason="system: BOSS's read-only repo reader for work that acts for nobody")
        audit.record(audit.SecurityEvent(org_id=org, event_type="role_binding.added",
                                         outcome=audit.Outcome.SUCCESS, actor_principal_id="sys_migration",
                                         actor_kind="system", target_type="principal",
                                         target_id=SYSTEM_REPO_READER, scope="org",
                                         details={"role": "viewer", "why": "system repo reader",
                                                  "ceiling": sorted(SYSTEM_GIT_SCOPE),
                                                  "github": SYSTEM_GIT_EXTERNAL["github"]}), db=db)
        p = passports.get(SYSTEM_REPO_READER, db=db)
    if p is None or not p.active:
        return None
    principal = users.load_principal(db, SYSTEM_REPO_READER)
    return AuthContext(principal=principal, method=AuthMethod.SYSTEM) if principal else None


_shared_noted: set[str] = set()


def _note_shared_token_use(engine: Any, *, owner: str, repo: str, project_id: Optional[str]) -> None:
    """Shadow mode only: the shared token was used for a clone that acts for nobody. Recorded once
    per repo and process (credential.handed_out), so posture and the audit log show it."""
    key = f"{owner}/{repo}"
    if key in _shared_noted:
        return
    _shared_noted.add(key)
    try:
        from . import audit
        from .migration import default_org

        with engine.begin() as db:
            org = default_org(db)
            if org:
                audit.record(audit.SecurityEvent(
                    org_id=org, event_type="credential.handed_out", outcome=audit.Outcome.INFO,
                    actor_principal_id="sys_worker", actor_kind="system", target_type="github_repo",
                    target_id=key, details={"credential": "shared BOSS_GITHUB_TOKEN", "why": "no grant (shadow)",
                                            "project": project_id}), db=db)
    except Exception:  # noqa: BLE001 — the note never blocks the clone
        import logging
        logging.getLogger("boss.identity.runs").warning("could not record the shared-token use for %s", key)


def finish_code_task_run(engine: Any, shim: Shim, *, org_id: str) -> None:
    close_shim_socket(shim.run_id)
    with engine.begin() as db:
        end_run(shim.run_id, db=db, org_id=org_id)


# ---- in-process runs (agent loops, workflow steps) ------------------------------------------------------

def _adoptable(ambient: Any, recorded: Any) -> bool:
    """May this code act on a RECORDED authority (a job's, a workflow run's) instead of the ambient
    one? Only when nobody else is acting here: no ambient principal, a system principal (the
    worker calling in to run leased work), or the same person. A person's request never borrows
    another person's recorded grant."""
    if ambient is None:
        return True
    return str(ambient.principal_id).startswith("sys_") or (
        ambient.principal_id == recorded.principal_id and ambient.org_id in ("", recorded.org_id))


def _nested_run_id(acting: Any, kind: str, entity_id: str) -> str:
    from . import cascade

    own = cascade.run_id(kind, entity_id)                  # validates kind and entity
    parent = acting.run_id or ""
    if parent.startswith("run_") and cascade.parse_run_id(parent) is not None:
        return f"{parent}/{own[len('run_'):]}"            # run_wf:W/step:build — a child of the run
    return own


@contextlib.contextmanager
def delegated(engine: Any, *, kind: str, entity_id: str, project_id: Optional[str], role_key: Optional[str],
              scope: frozenset[str], base_url: str = "", acting: Any = None,
              job_id: Optional[str] = None) -> Iterator[Optional[AuthContext]]:
    """Run a block of in-process agent work under a delegated principal: a run token for the
    person's grant, bound to an in-process shim key, installed as the acting context AND as the
    tool-call identity (CURRENT_TOOL_AUTH), so every tool call is decided against person ∩
    ceilings ∩ grant ∩ task, and every job it enqueues carries the chain.

    Whose grant: the ambient one (a verified request or job context), or a RECORDED one — `acting`
    (a workflow run's authority) or the job `job_id` was leased from — when nobody else is acting
    here (`_adoptable`: the worker calling in, or the same person). A recorded run's work nests
    under its run id (`run_wf:W/<kind>:<entity>`), so revoking the run's grant halts it too.
    Without a verified principal and grant this yields None and changes nothing (legacy path);
    already inside a delegated run, it keeps that run."""
    from . import lineage, tokens as tok

    ambient = lineage.current()
    if ambient is not None and ambient.chain:
        yield None                                          # already inside a run
        return
    chosen = ambient
    recorded = acting
    if recorded is None and job_id and (ambient is None or not ambient.grant_id):
        with engine.begin() as db:
            recorded = lineage.acting_for_job_id(db, job_id)
    if recorded is not None:
        if recorded.chain or not _adoptable(ambient, recorded):
            yield None
            return
        chosen = recorded
    if chosen is None or not chosen.grant_id:
        yield None                                          # nothing delegated
        return
    run = _nested_run_id(chosen, kind, entity_id)
    with engine.begin() as db:
        agent_id = agent_for_role(db, project_id, role_key)
        if not agent_id:
            yield None
            return
        shim = start_run(subject_for(chosen, db=db),
                         RunStart(run_id=run, agent_id=agent_id, project_id=project_id, scope=scope),
                         grant_id=chosen.grant_id, db=db, audiences=(AUD_API,))
        url = (base_url or tok.issuer()) + "/internal/run"
        claims = tok.verify_run_token(shim.tokens[AUD_API].access_token, audience=AUD_API,
                                      dpop_proof=shim.authorization(AUD_API, "POST", url)["DPoP"],
                                      htm="POST", htu=url, db=db)
        ctx = tok.context_for(claims, db=db)
    try:
        with lineage.acting(ctx):
            yield ctx
    finally:
        with engine.begin() as db:
            end_run(run, db=db, org_id=chosen.org_id)


@contextlib.contextmanager
def on_behalf(engine: Any, acting: Any) -> Iterator[Optional[AuthContext]]:
    """In-process work under a RECORDED authority with no agent to delegate to (a built-in tool
    step of a person's workflow run): the person's live principal decides the tool calls (never
    the worker's), and jobs carry the recorded Acting. Yields None (nothing changes) when the
    authority cannot be adopted here; raises TokenError when its person is no longer active."""
    from . import lineage

    if acting is None or not _adoptable(lineage.current(), acting):
        yield None
        return
    with engine.begin() as db:
        ctx = subject_for(acting, db=db)
    with lineage.acting(acting, tool_auth=ctx):
        yield ctx


class ToolRefused(PermissionError):
    def __init__(self, permission: str, reason: str, narrowed_by: tuple[str, ...] = ()):
        super().__init__(f"{permission}: {reason}")
        self.permission = permission
        self.reason = reason
        self.narrowed_by = narrowed_by


def authorize_tool(permission: Optional[str], project_id: Optional[str], *, db: Any) -> None:
    """Inside a delegated run, a tool's declared permission is decided against the RUN TOKEN
    (live intersection; non-delegable keys never). Outside one, this does nothing (S2's registry
    hook decides). An undeclared tool inside a delegated run is refused: deny by default."""
    from . import lineage, tokens as tok
    from .scope import ResolvedScope

    ctx = lineage.current_context()
    if ctx is None or ctx.run_token is None:
        return
    claims = ctx.run_token
    if not permission:
        raise ToolRefused("(undeclared)", "undeclared_tool")
    scope = (ResolvedScope.in_project(claims.org_id, project_id) if project_id
             else ResolvedScope.org_level(claims.org_id))
    decision = tok.decide(claims, permission, scope, db=db)
    if not decision.allowed:
        raise ToolRefused(permission, decision.reason.value if decision.reason else "denied", decision.narrowed_by)
