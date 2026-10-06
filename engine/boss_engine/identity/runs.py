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
        shim.egress_credential = issue_egress_credential(shim, sandbox_id=sandbox_id or start.run_id, db=db)
    with _lock:
        _SHIMS[start.run_id] = shim
    return shim


def spawn_child(parent: Shim, *, child_agent_id: str, child_name: str, scope: frozenset[str],
                db: Any, base_url: str, audience: str = AUD_API) -> Shim:
    """Spawn a sub-agent through BOSS: the supervisor mints the actor assertion for the child run
    (bound to the CHILD's new key) and the parent's shim performs the RFC 8693 exchange with its own
    token and DPoP proof. The child never sees the parent's token or key."""
    parent_token = parent.tokens[AUD_API]
    parent_claims = tokens.verify_run_token(
        parent_token.access_token, audience=AUD_API,
        dpop_proof=parent.key.proof("POST", base_url + "/v1/identity/spawn", access_token=parent_token.access_token),
        htm="POST", htu=base_url + "/v1/identity/spawn", db=db)
    child = Shim(run_id=f"{parent.run_id}/{child_name}")
    assertion = tokens.mint_actor_assertion(parent=parent_claims, child_agent_id=child_agent_id,
                                            child_run_id=child.run_id, child_jkt=child.key.jkt, db=db)
    htu = base_url + TOKEN_ENDPOINT
    request = TokenExchangeRequest(
        subject_token=parent_token.access_token, subject_token_type=TOKEN_TYPE_RUN, actor_token=assertion,
        actor_token_type=TOKEN_TYPE_ACTOR, audience=audience, scope=frozenset(scope),
        dpop_proof=parent.key.proof("POST", htu, access_token=parent_token.access_token))
    child.tokens[audience] = tokens.exchange_token(request, htm="POST", htu=htu, db=db)
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
    with _lock:
        for rid in [k for k in _SHIMS if k == run_id or k.startswith(run_id + "/")]:
            _SHIMS.pop(rid, None)


_SHIMS: dict[str, Shim] = {}
_lock = threading.Lock()


def shim_for(run_id: str) -> Optional[Shim]:
    with _lock:
        return _SHIMS.get(run_id)


# ---- the per-run egress credential ---------------------------------------------------------------------

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
    return raw


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


def resolve_egress(raw: Optional[str], *, db: Any) -> RunTokenClaims:
    """boss_egr -> run -> run token -> LIVE checks (credential, jti, grant, chain, person)."""
    from sqlalchemy import text

    if not raw or not raw.startswith(EGRESS_PREFIX) or len(raw) > 128:
        raise tokens.TokenError("unauthenticated", 401, "no_egress_credential")
    h = _hash(raw)
    row = db.execute(text("SELECT run_id, jti, revoked_at, expires_at FROM identity_egress_credentials "
                          "WHERE token_hash = :h"), {"h": h}).mappings().first()
    if row is None or row["revoked_at"] is not None or row["expires_at"] <= datetime.now(timezone.utc):
        raise tokens.TokenError("unauthenticated", 401, "egress_credential_dead")
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
class CloneAccess:
    """How a sandbox reaches a repo through egress: a credential-free URL plus the git config that
    makes git ask the shim (never a file in the workspace) for the run's boss_egr credential."""
    run_id: str
    clone_url: str
    git_config: tuple[tuple[str, str], ...]
    helper_env: Mapping[str, str] = field(default_factory=dict)


def egress_access(shim: Shim, *, owner: str, repo: str) -> CloneAccess:
    import os

    base = (os.environ.get(EGRESS_URL_ENV) or "").rstrip("/")
    socket_path = serve_shim_socket(shim)
    # git runs the helper with the action ("get", "store", "erase"); only "get" answers.
    helper = ("!f() { python3 -c 'import socket,sys; s=socket.socket(socket.AF_UNIX); s.connect(sys.argv[1]); "
              "s.sendall(sys.argv[2].encode()); sys.stdout.write(s.recv(4096).decode())' "
              f"{socket_path} \"$1\"; }}; f")
    return CloneAccess(
        run_id=shim.run_id, clone_url=f"{base}/github/{owner}/{repo}.git",
        git_config=((f"url.{base}/github/.insteadOf", "https://github.com/"),
                    ("credential.helper", ""), ("credential.helper", helper),
                    ("credential.useHttpPath", "true")),
        helper_env={"BOSS_SHIM_SOCKET": socket_path})


# ---- the in-process shim socket ----------------------------------------------------------------------------

_SOCKETS: dict[str, Any] = {}


def serve_shim_socket(shim: Shim) -> str:
    """A unix socket (0600, outside any workspace) answering git's credential helper with the run's
    boss_egr credential. The credential is held in this process only. NOTE: a same-uid sandbox can
    connect too; only a separate-uid shim (microVM) keeps it from the agent. The credential is
    egress-only, run-bound and ≤1 h either way."""
    import os
    import socket
    import tempfile

    with _lock:
        existing = _SOCKETS.get(shim.run_id)
    if existing is not None:
        return existing[0]
    # AF_UNIX paths are short (104 bytes on macOS): default to /tmp, not a long per-user TMPDIR.
    root = os.environ.get("BOSS_SHIM_DIR") or ("/tmp/boss-shim" if os.path.isdir("/tmp") else
                                               os.path.join(tempfile.gettempdir(), "boss-shim"))
    os.makedirs(root, mode=0o700, exist_ok=True)
    path = os.path.join(root, hashlib.sha256(shim.run_id.encode()).hexdigest()[:24] + ".sock")
    if os.path.exists(path):
        os.unlink(path)
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(path)
    os.chmod(path, 0o600)
    srv.listen(4)

    def loop() -> None:
        while True:
            try:
                conn, _ = srv.accept()
            except OSError:
                return
            with conn:
                try:
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


def start_code_task_run(engine: Any, *, task_run_id: str, project_id: str, owner: str, repo: str,
                        role_key: Optional[str], acting: Any = None,
                        job_id: Optional[str] = None) -> Optional[tuple[Shim, CloneAccess]]:
    """Wire a code task to its delegated principal: a run token for the person's grant, an egress
    credential, and the clone access. The principal is the ambient one, else the one stamped on the
    job that created the build (R8). None (the legacy path) when egress is off or the build carries
    no verified principal — the caller then keeps today's behaviour, which posture reports."""
    from . import lineage

    if not egress_enabled():
        return None
    acting = acting or lineage.current() or _acting_of_job(engine, job_id)
    if not egress_enabled() or acting is None or not acting.grant_id:
        return None
    with engine.begin() as db:
        agent_id = agent_for_role(db, project_id, role_key or "forge")
        if not agent_id:
            return None
        if acting.run_id and acting.run_id.startswith("run_wf:"):
            rid = f"{acting.run_id}/ct:{task_run_id}"        # a build inside a workflow run
        elif acting.run_id and acting.run_id.startswith("run_ct:"):
            rid = acting.run_id
        else:
            rid = f"run_ct:{task_run_id}"
        shim = start_run(subject_for(acting, db=db),
                         RunStart(run_id=rid, agent_id=agent_id, project_id=project_id, repos=(f"{owner}/{repo}",),
                                  scope=frozenset({"code_task:read", "code_task:steer", "repository:read",
                                                   "github:read", "github:sync"})),
                         grant_id=acting.grant_id, db=db, audiences=(AUD_API,))
    return shim, egress_access(shim, owner=owner, repo=repo)


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
    raw = token() if callable(token) else token
    yield git_access(kind, owner, repo, raw or "")


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
