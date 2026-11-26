"""boss-egress: the reverse proxy that injects GitHub credentials, so sandboxes never hold them.
IDENTITY.md §7.10. Owned by S3.

    git:  url."https://<egress>/github/".insteadOf "https://github.com/"
    API:  GH_HOST / the GitHub API base -> https://<egress>/github-api/

Per request:
  1. take the run's `boss_egr_` credential (HTTP Basic password for git, `Authorization: Bearer`
     for the API) and map it to the run, the run's egress token and LIVE state (credential, jti,
     grant, every agent in the chain, the person) — runs.resolve_egress;
  2. check the rule table (egress_rules) against the run's repos and effective GitHub permissions,
     including the ref, force-push and workflow rules for pushes;
  3. strip every inbound credential header and inject `Authorization` with the run's per-run,
     down-scoped installation token (minted on first use, held only in this process);
  4. write one identity_egress_log row: method, path TEMPLATE, status, bytes, run, jti, chain.
     Never a body, a query string or a token.
A refusal is 403 (rules) or 401 (credential), with a fixed body that names no secret.

THE HOST ROUTE (P8b S11, BOSSAPP-1072; HOST_API.md §4.9). A microVM has no network: its git and
REST calls reach egress only through `boss-hostd`, under the guest's own spelling:

    /git/<host>/<owner>/<repo>.git/...    git smart-HTTP   (github.com, bitbucket.org)
    /api/<host>/...                       REST             (github.com | api.github.com,
                                                            bitbucket.org | api.bitbucket.org)

  0. the HOST is identified first: its `BossHost` proof (fleet_ca.verify_proof), or a client
     certificate a named TLS terminator forwarded — exactly as the model gateway and the API do;
  1. the guest's handle arrives MOVED into `X-Boss-Handle`, with the host's word on VM, run and
     epoch; the VM handle's row (identity/egress_handles.py) must agree (another VM, run or org:
     a replay — revoked, `host_events(handle_replay)`, 401; a stale epoch: 409 fenced);
  2. the run's `boss:egress` token (`X-Boss-Delegation` + the host's `DPoP` proof) is REQUIRED
     from a host that advertised `host_tokens` (fleet.host_tokens_required), as at every other
     destination; the run's authority (repos, permissions, push pattern) is that token's;
  3. then the same rule table, push inspection and injection as below — with two more rules on
     this route: a push never deletes a branch (`branch_delete`), and an unrouted git host is
     refused by name (`git_host_not_routed`; `strict_secrets_git_host_refused` in strict mode).
Bitbucket (D19) is routed: its repositories come from the token's `bitbucket` resources and the
injected credential is the install's Bitbucket token (BOSS_BITBUCKET_TOKEN, or the
username + app password pair). GitLab and every other host are refused until routed.
"""
from __future__ import annotations

import base64
import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Optional
from urllib.parse import parse_qsl

from boss_engine.identity import egress_rules, github_app, github_urls, grants, lineage, repo_scope, runs, tokens
from boss_engine.identity.principal import RunTokenClaims

log = logging.getLogger("boss.egress")

GIT_UPSTREAM_ENV = "BOSS_GITHUB_GIT_URL"
BITBUCKET_GIT_ENV = "BOSS_BITBUCKET_GIT_URL"       # default https://bitbucket.org
BITBUCKET_API_ENV = "BOSS_BITBUCKET_API_URL"       # default https://api.bitbucket.org
STRICT_ENV = "BOSS_EGRESS_STRICT_SECRETS"
#: The host route's guest-facing host names → the provider that serves them (D19).
#: The install's GitHub Enterprise Server host (when it is pointed at one) routes as GitHub, so a
#: GHES install's guests get the credential injected the same way (BOSSAPP-1286).
_GHES_HOSTS = tuple(h for h in github_urls.github_hosts() if h not in github_urls.PUBLIC_HOSTS)
GIT_HOSTS = {"github.com": "github", "bitbucket.org": "bitbucket",
             **{h: "github" for h in _GHES_HOSTS}}
API_HOSTS = {**{h: "github-api" for h in _GHES_HOSTS},
             github_urls.PUBLIC_WEB_HOST: "github-api", github_urls.PUBLIC_API_HOST: "github-api",
             "bitbucket.org": "bitbucket-api", "api.bitbucket.org": "bitbucket-api"}
def ghes_api_for(db: Any, host: str) -> str:
    """The REST base of a GitHub Enterprise Server host of this install — the install's own or
    one set on any GitHub connection (github_connections.ghes_hosts, the ONE list) — else ""."""
    from boss_engine.services.github_connections import ghes_hosts

    return ghes_hosts(db).get(str(host or "").lower().rstrip("."), "")


def _on_install_host(api: str) -> bool:
    """True when `api` is the install's own GitHub (where the App and its tokens live)."""
    return not api or github_urls.api_base(api) == github_urls.install_api_base()


def _repo_of(provider: str, rest: str) -> str:
    """owner/name a GitHub request names: /owner/repo.git/... (git), /repos/owner/repo/... (API)."""
    parts = [p for p in rest.split("/") if p]
    if provider == "github" and len(parts) >= 2:
        return f"{parts[0]}/{parts[1].removesuffix('.git')}"
    if provider == "github-api" and len(parts) >= 3 and parts[0] == "repos":
        return f"{parts[1]}/{parts[2]}"
    return ""


#: The ONLY inbound headers that cross the proxy (an allowlist: a header nobody thought of —
#: X-HTTP-Method-Override, a hop-by-hop header, a credential under a new name — never does).
_FORWARD = frozenset({"accept", "content-type", "user-agent", "git-protocol", "x-github-api-version",
                      "if-none-match", "if-modified-since", "content-encoding"})
#: Inbound headers that make the request REFUSED, not just stripped: they ask an intermediary to
#: treat the request as something other than what the rule table judged.
_REFUSE_HEADERS = frozenset({"x-http-method-override", "x-http-method", "x-method-override"})
#: The response headers that come back (the body is decoded by the client, so never
#: content-encoding).
_PASS_RESPONSE = {"content-type", "cache-control", "etag", "link", "location", "x-github-request-id",
                  "x-ratelimit-remaining", "x-ratelimit-reset"}


@dataclass(frozen=True)
class EgressRequest:
    method: str
    path: str                                  # the RAW path: "/github/acme/web.git/info/refs", "/github-api/repos/…"
    query: str = ""
    headers: Mapping[str, str] = field(default_factory=dict)   # lowercase keys
    body: bytes = field(default=b"", repr=False)
    peer: Optional[str] = None                 # the source address (the sandbox's network identity)
    #: The URL as the caller addressed it (scheme://host/path, no query): the `htu` a host's DPoP
    #: proof is checked against (P8b S13). "" when unknown (a direct, in-process call).
    url: str = ""


@dataclass(frozen=True)
class EgressResponse:
    status: int
    headers: Mapping[str, str] = field(default_factory=dict)
    body: bytes = field(default=b"", repr=False)


#: (method, url, headers, body) -> EgressResponse. Replaced in tests.
Upstream = Callable[[str, str, Mapping[str, str], bytes], EgressResponse]


def upstream_url(base: str, encoded_path: str, query: str) -> str:
    """The URL exactly as the client library will send it. The rule table was matched on the
    path this encodes; if the library would normalise it into something else, refuse."""
    import httpx

    raw = (encoded_path + (f"?{query}" if query else "")).encode("ascii")
    url = httpx.URL(base).copy_with(raw_path=httpx.URL(base).raw_path.rstrip(b"/") + raw)
    sent_path = url.raw_path.split(b"?", 1)[0].decode("ascii")
    if not sent_path.endswith(encoded_path):
        raise egress_rules.PathRefused("path_normalised")
    return str(url)


def _httpx_upstream(method: str, url: str, headers: Mapping[str, str], body: bytes) -> EgressResponse:
    import httpx

    with httpx.Client(timeout=60.0, follow_redirects=False) as client:
        request = client.build_request(method, httpx.URL(url), headers=dict(headers), content=body or None)
        resp = client.send(request)
        return EgressResponse(resp.status_code, dict(resp.headers), resp.content)


def _sha_ok(kind: int, data: bytes, sha: str) -> bool:
    import hashlib

    name = {1: b"commit", 2: b"tree", 3: b"blob", 4: b"tag"}[kind]
    return hashlib.sha1(name + b" " + str(len(data)).encode() + b"\0" + data).hexdigest() == sha


def _credential(req: EgressRequest) -> Optional[str]:
    raw = req.headers.get("authorization", "")
    scheme, _, value = raw.partition(" ")
    if scheme.lower() == "basic":
        try:
            _, _, password = base64.b64decode(value.strip()).decode("utf-8").partition(":")
            return password or None
        except Exception:  # noqa: BLE001
            return None
    if scheme.lower() in ("bearer", "token"):
        return value.strip() or None
    return None


#: Why no credential could be had for a repo that the org's GitHub connection simply does not
#: cover (or there is none). Anything else (a revoked run, a refused grant, GitHub down) still
#: refuses.
_UNCOVERED = ("github_not_connected", "app_not_installed", "repo_outside_org_token")


#: How long after it is minted an installation token may still be unknown to GitHub's git and
#: API front ends, and the pauses between re-sending a read in that window (~15 s in all).
FRESH_TOKEN_SECONDS = 60
FRESH_TOKEN_BACKOFF = (0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 4.0)
#: An installation token lives one hour from its minting (GitHub's fixed lifetime).
_INSTALLATION_TOKEN_LIFETIME = 3600


def _sleep(seconds: float) -> None:
    import time

    time.sleep(seconds)


def _is_read(provider: str, rest: str, method: str) -> bool:
    """A request that changes nothing at GitHub: safe to send again."""
    m = method.upper()
    if provider == "github":
        return m == "GET" or (m == "POST" and rest.endswith("/git-upload-pack"))
    return provider == "github-api" and m in ("GET", "HEAD")


def _token_is_fresh(run_id: str, token: str) -> bool:
    """`token` is an installation token THIS process minted for `run_id` under a minute ago."""
    from datetime import datetime, timezone

    tok = github_app.cached_for_run(run_id)
    if tok is None or tok.token != token or tok.kind != "installation":
        return False
    remaining = (tok.expires_at - datetime.now(timezone.utc)).total_seconds()
    return _INSTALLATION_TOKEN_LIFETIME - remaining < FRESH_TOKEN_SECONDS


def anonymous_read_allowed(provider: str, decision: Any, code: str, perms: Mapping[str, str]) -> bool:
    """A READ-ONLY run's git read (upload-pack: clone/fetch) of a repository no credential of the
    org covers goes to GitHub with no credential: what anyone on the internet may read, nothing
    more (a public template's import, through BOSS's system repo reader). Never a write, never
    the REST API, never a run that may write (a build whose token was refused stays refused),
    never when the refusal is about the run or the grant."""
    if provider != "github" or tuple(getattr(decision, "need", None) or ()) != ("contents", "read"):
        return False
    if any(level not in ("read", "none") for level in (perms or {}).values()):
        return False
    return code in _UNCOVERED or code.startswith("mint_failed_4")


def _strict() -> bool:
    return (os.environ.get(STRICT_ENV) or "").strip().lower() in ("1", "true", "yes")


def bitbucket_repos(claims: RunTokenClaims, db: Any) -> list[str]:
    """The run's Bitbucket repositories (`workspace/repo_slug`): its token's task binding, judged
    AGAIN against the registry and the run's grants at every request (identity/repo_scope.py) —
    the same check GitHub's mint makes — so a repository unregistered since the run started, or
    one a token names without the grant, is `repo_outside_run`. Fails closed."""
    repos = ((getattr(claims.task, "resources", None) or {}).get("bitbucket")) or []
    repos = [r for r in repos if isinstance(r, str)]
    if not repos:
        return []
    try:
        grant = grants.load(claims.grant_id, db=db)
    except Exception:  # noqa: BLE001 — unreadable grant: nothing is reachable
        return []
    return list(repo_scope.granted(db, provider=repo_scope.BITBUCKET, org_id=claims.org_id,
                                   run_id=claims.task.run_id, grant=grant,
                                   obo=claims.mode.value == "obo", repos=repos))


def bitbucket_credential() -> str:
    """The install's Bitbucket credential, held only in this process: an access token
    (BOSS_BITBUCKET_TOKEN), else `username:app_password`. "" when Bitbucket is not connected.
    Bitbucket cannot mint a per-run token, so the run's down-scoping is the rule table: its own
    repositories, its own ref pattern, no force-push, no delete."""
    token = (os.environ.get("BOSS_BITBUCKET_TOKEN") or "").strip()
    if token:
        return token
    user = (os.environ.get("BOSS_BITBUCKET_USERNAME") or "").strip()
    password = (os.environ.get("BOSS_BITBUCKET_APP_PASSWORD") or "").strip()
    return f"{user}:{password}" if user and password else ""


def _bitbucket_basic(credential: str) -> str:
    """git over HTTPS: an access token rides as `x-token-auth:<token>`; an app password as
    `username:app_password`."""
    pair = credential if ":" in credential else f"x-token-auth:{credential}"
    return "Basic " + base64.b64encode(pair.encode()).decode()


def _check_push_offline(body: bytes, pattern: str, perms: Mapping[str, str], *,
                        allow_delete: bool) -> egress_rules.RuleDecision:
    """A push to a host egress cannot ask about commits outside the pack (Bitbucket): the ref,
    delete and fast-forward rules decided from the pack alone. A non-fast-forward update whose
    old tip is not an ancestor IN THE PACK is refused as a force-push. GitHub workflow rules do
    not apply to a Bitbucket repository."""
    try:
        updates, pack = egress_rules.parse_receive_pack(body)
        objects = egress_rules.parse_pack(pack, allow_unresolved=True) if pack else {}
    except Exception:  # noqa: BLE001 — anything unparseable is refused
        return egress_rules.RuleDecision(False, "/{o}/{r}.git/git-receive-pack", "unparseable_push")
    return egress_rules.check_push(updates, objects, push_pattern=pattern,
                                   perms={**perms, "workflows": "write"},
                                   is_ancestor=lambda _old, _desc: False, allow_delete=allow_delete)


class EgressProxy:
    def __init__(self, engine: Any, *, upstream: Optional[Upstream] = None):
        self.engine = engine
        self.upstream = upstream or _httpx_upstream

    # ---- the one entry point ----------------------------------------------------------------------
    def handle(self, req: EgressRequest) -> EgressResponse:
        first = req.path.lstrip("/").split("/", 1)[0]
        if first in ("git", "api"):
            return self._host_route(req, first)
        provider, _, rest_raw = req.path.lstrip("/").partition("/")
        rest_raw = "/" + rest_raw
        # A repository on a GitHub Enterprise Server host comes as /github@<host>/... (git) or
        # /github-api@<host>/... (REST): runs.egress_access rewrites that host's origin so.
        provider, _, ghes_host = provider.partition("@")
        template = "{unparsed}"
        presented = _credential(req)
        # THE CHALLENGE (round 2 walk): git (libcurl, CURLAUTH_ANY) sends its credential only after a
        # 401 that names a scheme. Without `WWW-Authenticate: Basic` every clone and push through
        # egress failed "Authentication failed" — the helper was never asked. A request that
        # presents nothing is that first handshake, not an attack: challenged, not audited.
        challenge = {"content-type": "application/json", "www-authenticate": 'Basic realm="boss-egress"'}
        if presented is None:
            return EgressResponse(401, challenge, b'{"error":"unauthenticated"}')
        with self.engine.begin() as db:
            try:
                claims = runs.resolve_egress(presented, db=db, peer=req.peer)
            except tokens.TokenError as exc:
                self._deny_event(db, None, provider, req.method, template, exc.reason)
                return EgressResponse(401, challenge, b'{"error":"unauthenticated"}')
            # P8b S13: a token attached here must verify for THIS audience and name this run. The
            # direct route carries no host identity, so only BOSS_HOST_TOKENS_REQUIRED=1 makes it
            # required; a microVM's calls come by the host route, where it is required per host.
            from boss_engine.identity import delegation, host_delegation
            from boss_engine.services.fleet import host_tokens_required

            try:
                host_delegation.check(
                    db, req.headers, audience=delegation.AUD_EGRESS, run_id=claims.task.run_id,
                    method=req.method, url=req.url or req.path,
                    required=(not host_delegation.presented(req.headers)
                              and host_tokens_required(db, None)))
            except host_delegation.DelegationRefused as exc:
                self._deny_event(db, claims, provider, req.method, template,
                                 f"run_token_{exc.reason}")
                return EgressResponse(401, {"content-type": "application/json"},
                                      b'{"error":"run_token_refused"}')
            ghes_api = ""
            if ghes_host:
                ghes_api = ghes_api_for(db, ghes_host) if provider in ("github", "github-api") else ""
                if not ghes_api:
                    return self._refuse(db, claims, provider, req.method, template,
                                        "git_host_not_routed")
            judged = self._judge(db, claims, provider, rest_raw, req, allow_delete=True,
                                 ghes_api=ghes_api)
            if isinstance(judged, EgressResponse):
                return judged
        return self._send(claims, provider, req, judged)

    # ---- the host route (P8b S11) -------------------------------------------------------------------
    def _host_route(self, req: EgressRequest, kind: str) -> EgressResponse:
        from boss_engine.identity import delegation, egress_handles, host_delegation
        from boss_engine.services import fleet_ca
        from boss_engine.services.fleet import host_tokens_required

        h = {str(k).lower(): str(v) for k, v in req.headers.items()}
        parts = req.path.lstrip("/").split("/", 2)
        guest_host = parts[1].lower() if len(parts) > 1 else ""
        rest_raw = "/" + (parts[2] if len(parts) > 2 else "")
        provider = (GIT_HOSTS if kind == "git" else API_HOSTS).get(guest_host, "")
        template = f"/{kind}/{{host}}"
        ghes_api = ""

        def answer(status: int, reason: str) -> EgressResponse:
            from boss_engine.services.lapsed import say

            # The reason stays the machine code; the message says what lapsed and what to do.
            return EgressResponse(status, {"content-type": "application/json"},
                                  json.dumps({"error": "refused", "reason": reason,
                                              "message": say(reason)}).encode())

        with self.engine.begin() as db:
            # 0. WHO IS CALLING: an enrolled host, by its proof or a terminator's forwarded cert.
            scheme, _, proof = (h.get("authorization") or "").partition(" ")
            forwarded = h.get(fleet_ca.FORWARDED_CERT_HEADER)
            try:
                if scheme.lower() == fleet_ca.AUTH_SCHEME and proof.strip():
                    host = fleet_ca.verify_proof(db, proof.strip(), htm=req.method.upper(),
                                                 path=req.path)
                elif forwarded and fleet_ca.is_cert_terminator(req.peer):
                    host = fleet_ca.verify_forwarded_cert(db, forwarded)
                else:
                    return answer(401, "host_proof_required")
            except fleet_ca.FleetAuthError as exc:
                return answer(exc.status, exc.code)
            # 1. THE HANDLE, against the host's word.
            raw = (h.get("x-boss-handle") or "").strip()
            vm_id = (h.get("x-boss-vm") or "").strip()
            run_word = (h.get("x-boss-run") or "").strip()
            if not raw.startswith(egress_handles.PREFIX):
                return answer(401, "no_handle")
            if not vm_id or not run_word:
                return answer(400, "binding_headers_required")
            row = egress_handles.load(db, raw)
            if row is None:
                return answer(401, "unknown_handle")
            if row.revoked_at is not None:
                return answer(401, "handle_revoked")
            if not row.live():
                return answer(401, "handle_expired")
            deployment_id = (h.get("x-boss-deployment") or "").strip()
            why = None
            if row.org_id and row.org_id != host.org_id:
                why = "another_org"
            else:
                from boss_engine.services.fleet import BindingRefused, prove_host_binding

                try:
                    # An unbound handle is bound only to a VM this host holds. A VM that
                    # disagrees with an already-bound handle stays a replay, not an ownership miss.
                    if row.vm_id is None:
                        prove_host_binding(db, host.host_id, vm_id=vm_id,
                                           deployment_id=deployment_id or None)
                        row.vm_id = egress_handles.bind_vm(db, row.token_hash, vm_id)
                    if row.vm_id != vm_id:
                        why = "another_vm"
                    elif row.run_id != run_word:
                        why = "another_run"
                    else:
                        prove_host_binding(db, host.host_id, vm_id=vm_id,
                                           deployment_id=deployment_id or None)
                except BindingRefused as exc:
                    return answer(exc.status, exc.reason)
            if why:
                egress_handles.revoke(db, row.token_hash, f"handle_replay:{why}")
                try:
                    from boss_engine.services import fleet

                    with db.begin_nested():
                        fleet.host_event(db, org_id=row.org_id or host.org_id, host_id=host.host_id,
                                         vm_id=vm_id, kind="handle_replay", actor="egress",
                                         detail={"why": why, "run_id": row.run_id,
                                                 "bound_vm": row.vm_id,
                                                 "handle_fp": row.token_hash[:16],
                                                 "presented_run": run_word})
                except Exception:  # noqa: BLE001 — the refusal stands without the event
                    log.warning("handle replay on %s could not be recorded", host.host_id)
                return answer(401, "handle_replay")
            epoch_word = (h.get("x-boss-fence-epoch") or "").strip()
            if epoch_word and epoch_word != str(row.fence_epoch):
                return answer(409, "fenced")
            from boss_engine.services.streaming_model_gateway import current_epoch

            now_epoch = current_epoch(db, row.run_id)
            if now_epoch is not None and now_epoch != row.fence_epoch:
                return answer(409, "fenced")
            # 2. THE RUN'S TOKEN for egress, required per host (S13).
            try:
                claims = host_delegation.check(
                    db, h, audience=delegation.AUD_EGRESS, run_id=row.run_id,
                    method=req.method, url=req.url or req.path,
                    required=host_tokens_required(db, host.host_id))
            except host_delegation.DelegationRefused as exc:
                self._deny_event(db, None, provider or guest_host, req.method, template,
                                 f"run_token_{exc.reason}")
                return answer(401, f"run_token_{exc.reason}")
            if claims is None:
                # Nothing registers a VM handle into the in-process egress table, so a host
                # that predates S13 and attaches no boss:egress token fails closed.
                return answer(401, "egress_not_registered")
            # 3. THE ROUTE: a host egress injects for, or a refusal that names it (D19). A GHES
            # host (the install's, or one on any GitHub connection) routes as GitHub, upstream
            # to THAT host.
            ghes_api = ghes_api_for(db, guest_host)
            if ghes_api:
                provider = "github" if kind == "git" else "github-api"
            if not provider:
                reason = ("strict_secrets_git_host_refused" if _strict()
                          else "git_host_not_routed")
                return self._refuse(db, claims, guest_host or "unknown", req.method, template, reason)
            judged = self._judge(db, claims, provider, rest_raw, req, allow_delete=False,
                                 ghes_api=ghes_api)
            if isinstance(judged, EgressResponse):
                return judged
        return self._send(claims, provider, req, judged)

    # ---- the rule table, the push inspection and the credential -------------------------------------
    def _judge(self, db: Any, claims: RunTokenClaims, provider: str, rest_raw: str,
               req: EgressRequest, *, allow_delete: bool, ghes_api: str = "") -> Any:
        """An EgressResponse (a refusal), or (url, headers, template, token, rest) to send.
        `ghes_api`: the REST base of the GHES host the request is for ("" = the install's)."""
        # A host other than the install's GitHub gets ITS connection's credential, never the
        # install App's token (BOSSAPP-1286); "" keeps the install's route unchanged.
        foreign = "" if _on_install_host(ghes_api) else github_urls.api_base(ghes_api)
        template = "{unparsed}"
        # THE PATH FIRST: decoded and canonical BEFORE any rule sees it; `..`, `.`, `//` and
        # encoded separators are refused outright, and the upstream gets exactly this path.
        try:
            rest, encoded = egress_rules.canonical_path(rest_raw)
        except egress_rules.PathRefused as exc:
            return self._refuse(db, claims, provider, req.method, template, str(exc))
        if any(h in _REFUSE_HEADERS for h in req.headers):
            return self._refuse(db, claims, provider, req.method, template, "method_override")
        if len(req.body) > egress_rules.MAX_PUSH_BYTES:
            return self._refuse(db, claims, provider, req.method, template, "body_too_large")
        perms = github_app.run_permissions(claims, db=db)
        repos = github_app.run_repos(claims.task)
        bb_repos = bitbucket_repos(claims, db)
        pattern = github_app.push_ref_pattern(claims, db=db)
        pairs = parse_qsl(req.query, keep_blank_values=True)
        query = dict(pairs)
        if len(query) != len(pairs):
            # One key twice: the rules would judge one value and GitHub might act on the other.
            return self._refuse(db, claims, provider, req.method, template, "duplicate_query_key")
        minted: dict[str, str] = {}

        def installation_token() -> str:
            if "t" not in minted:
                minted["t"] = (self._connection_token(claims, db, foreign, _repo_of(provider, rest))
                               if foreign else self._installation_token(claims, db))
            return minted["t"]

        def body_json() -> Optional[dict]:
            if req.body and req.method.upper() in ("POST", "PUT", "PATCH"):
                try:
                    parsed = json.loads(req.body)
                except ValueError:
                    return None
                return parsed if isinstance(parsed, dict) else None
            return None

        if provider == "github":
            decision = egress_rules.check_git(req.method, rest, query, perms=perms, repos=repos)
        elif provider == "github-api":
            try:
                decision = egress_rules.check_api(
                    req.method, rest, perms=perms, repos=repos, body=body_json(), push_pattern=pattern,
                    pr_head=lambda repo, n: self._pr_head(repo, n, installation_token(), api=foreign))
            except github_app.GitHubRefused as exc:
                return self._refuse(db, claims, provider, req.method, "/repos/{o}/{r}/pulls/{n}", exc.code)
        elif provider == "bitbucket":
            decision = egress_rules.check_git(req.method, rest, query, perms=perms, repos=bb_repos)
        elif provider == "bitbucket-api":
            decision = egress_rules.check_bitbucket_api(req.method, rest, perms=perms, repos=bb_repos,
                                                        body=body_json(), push_pattern=pattern)
        else:
            decision = egress_rules.RuleDecision(False, "{unknown}", "unknown_provider")
        template = decision.template
        if not decision.allowed:
            return self._refuse(db, claims, provider, req.method, template, decision.reason)
        if provider in ("github", "github-api") and not foreign:
            # THE INSTALL'S ROUTE never serves a repository bound to ANOTHER GitHub host (a GHES
            # connection): `git clone https://github.com/<that owner>/<repo>` from a sandbox must
            # not pull the GHES connection's token onto github.com (BOSSAPP-1286).
            other = self._bound_elsewhere(claims, db, _repo_of(provider, rest))
            if other:
                return self._refuse(db, claims, provider, req.method, template,
                                    "repo_bound_to_other_host")
        if provider.startswith("bitbucket"):
            token = bitbucket_credential()
            if not token:
                return self._refuse(db, claims, provider, req.method, template, "bitbucket_not_connected")
        else:
            try:
                token = installation_token()
            except github_app.GitHubRefused as exc:
                if not anonymous_read_allowed(provider, decision, exc.code, perms):
                    return self._refuse(db, claims, provider, req.method, template, exc.code)
                # A READ of a repository no credential of this org covers (a public template, a
                # public dependency): forwarded with no credential at all. GitHub serves it only
                # if it is public; the run's rules above still decided the repo and the method.
                token = ""
        if provider in ("github", "bitbucket") and rest.endswith("/git-receive-pack") \
                and req.method.upper() == "POST":
            if req.headers.get("content-encoding"):
                return self._refuse(db, claims, provider, req.method, template, "encoded_push")
            if provider == "github":
                verdict = self._check_push(req.body, rest, perms, pattern, token,
                                           allow_delete=allow_delete, api=foreign)
            else:
                verdict = _check_push_offline(req.body, pattern, perms, allow_delete=allow_delete)
            if not verdict.allowed:
                return self._refuse(db, claims, provider, req.method, template, verdict.reason)
        try:
            url, headers = self._upstream_request(provider, encoded, req, token, api=foreign)
        except egress_rules.PathRefused as exc:
            return self._refuse(db, claims, provider, req.method, template, str(exc))
        except github_urls.CrossHostCredential:
            # The credential belongs to another GitHub host (check_send logged it): refused,
            # never sent (BOSSAPP-1286).
            return self._refuse(db, claims, provider, req.method, template,
                                "credential_host_mismatch")
        return (url, headers, template, token, rest)

    def _send(self, claims: RunTokenClaims, provider: str, req: EgressRequest, judged: Any) -> EgressResponse:
        url, headers, template, token, rest = judged
        # Upstream call outside the DB transaction.
        try:
            resp = self.upstream(req.method.upper(), url, headers, req.body)
            resp = self._settle_fresh_token(provider, rest, req, url, headers, token,
                                            claims.task.run_id, resp)
        except Exception:  # noqa: BLE001
            resp = EgressResponse(502, {}, b'{"error":"upstream_unreachable"}')
        with self.engine.begin() as db:
            self._log(db, claims, provider, req.method, template, resp.status, len(req.body), len(resp.body),
                      "allowed", None)
        out_headers = {k: v for k, v in resp.headers.items() if k.lower() in _PASS_RESPONSE}
        return EgressResponse(resp.status, out_headers, resp.body)

    # ---- pieces -----------------------------------------------------------------------------------
    def _settle_fresh_token(self, provider: str, rest: str, req: EgressRequest, url: str,
                            headers: Mapping[str, str], token: str, run_id: str,
                            resp: EgressResponse) -> EgressResponse:
        """A JUST-MINTED installation token is not usable at GitHub at once.

        crun_4e4b5828, crun_ada5fe88 and crun_a1a0a5a4 (2026-10-27) each failed their clone with
        "Repository not found": the run's first git read went to GitHub with an installation
        token minted milliseconds earlier and GitHub answered 404 (the token was valid; measured
        from the egress container, a fresh token for paramg/boss answered 404 for up to ~5 s in
        one mint of six, then 200). git takes the 404 as final. So a READ (a git fetch/clone, a
        REST GET) that GitHub answers 401/404 while the token is still fresh is re-sent — the
        same request, the same token, nothing wider — until GitHub has it or the window ends."""
        if resp.status not in (401, 404) or not token or not _is_read(provider, rest, req.method):
            return resp
        waited = 0.0
        for pause in FRESH_TOKEN_BACKOFF:
            if not _token_is_fresh(run_id, token):
                break
            _sleep(pause)
            waited += pause
            resp = self.upstream(req.method.upper(), url, headers, req.body)
            if resp.status not in (401, 404):
                log.info("egress: a fresh GitHub token for %s was usable after %.1fs", run_id, waited)
                break
        return resp

    def _bound_elsewhere(self, claims: RunTokenClaims, db: Any, repo: str) -> bool:
        """True when owner/repo is bound (in the run's project) to a connection on a GitHub host
        other than the install's."""
        from boss_engine.services.github_connections import api_base_for_repository

        owner, _, name = str(repo or "").partition("/")
        project = getattr(claims.task, "project_id", None)
        if not owner or not name or not project:
            return False
        with db.begin_nested():
            bound = api_base_for_repository(db, project, owner, name)
        return not _on_install_host(bound)

    def _connection_token(self, claims: RunTokenClaims, db: Any, api: str, repo: str) -> str:
        """The credential of the run's project connection that owner/repo is BOUND through on the
        GHES host `api` — never the install App's token on another host (BOSSAPP-1286). Raises
        GitHubRefused when the repository is not bound on that host or holds no credential."""
        from boss_engine.config import get_settings
        from boss_engine.services.github_connections import (GitHubConnectionService,
                                                             api_base_for_repository)

        owner, _, name = repo.partition("/")
        project = getattr(claims.task, "project_id", None)
        if not owner or not name or not project:
            raise github_app.GitHubRefused("ghes_repo_required", 403)
        with db.begin_nested():
            bound = api_base_for_repository(db, project, owner, name)
        if github_urls.api_base(bound) != github_urls.api_base(api):
            raise github_app.GitHubRefused("ghes_repo_not_bound", 403)
        with db.begin_nested():
            token = GitHubConnectionService(get_settings()).token_for_repository(
                db, project_id=project, owner=owner, repo=name)
        if not token:
            raise github_app.GitHubRefused("ghes_not_connected", 403)
        # Tagged at its source by where the secret lives (token_for_repository): an install env
        # token a GHES connection names stays the install's; the upstream check refuses it.
        return token

    def _installation_token(self, claims: RunTokenClaims, db: Any) -> str:
        cached = github_app.cached_for_run(claims.task.run_id)
        if cached is not None:
            return cached.token
        return github_app.mint_for_run(claims, task=claims.task, db=db).token

    def _pr_head(self, repo: str, number: int, token: str, api: str = "") -> Optional[str]:
        resp = github_app._call("GET", f"{api}/repos/{repo}/pulls/{number}", token=token)
        if resp.status != 200:
            return None
        data = resp.json() or {}
        head = data.get("head") or {}
        if str((head.get("repo") or {}).get("full_name", "")).lower() != repo.lower():
            return None                      # a fork's branch is never the run's
        return str(head.get("ref") or "") or None

    def _check_push(self, body: bytes, rest: str, perms: Mapping[str, str], pattern: str,
                    token: str, *, allow_delete: bool = True,
                    api: str = "") -> egress_rules.RuleDecision:
        tpl = "/{o}/{r}.git/git-receive-pack"
        repo = "/".join(rest.strip("/").split("/")[:2]).removesuffix(".git")

        def remote_json(path: str) -> Optional[dict]:
            resp = github_app._call("GET", f"{api}/repos/{repo}{path}", token=token)
            return (resp.json() or {}) if resp.status == 200 else None

        def remote_tree(sha: str):
            data = remote_json(f"/git/trees/{sha}")
            if data is None or data.get("truncated"):
                return None
            raw = egress_rules.raw_tree_from_api(data.get("tree") or [])
            if not _sha_ok(egress_rules.OBJ_TREE, raw, sha):
                return None
            return egress_rules._tree_entries(raw)

        def remote_commit_tree(sha: str) -> Optional[str]:
            data = remote_json(f"/git/commits/{sha}")
            return str((data or {}).get("tree", {}).get("sha") or "") or None

        def fetch_base(sha: str):
            data = remote_json(f"/git/trees/{sha}")
            if data is not None and not data.get("truncated"):
                raw = egress_rules.raw_tree_from_api(data.get("tree") or [])
                return (egress_rules.OBJ_TREE, raw)
            blob = remote_json(f"/git/blobs/{sha}")
            if blob is not None and blob.get("encoding") == "base64":
                return (egress_rules.OBJ_BLOB, base64.b64decode(blob.get("content") or ""))
            return None

        try:
            updates, pack = egress_rules.parse_receive_pack(body)
            objects = egress_rules.parse_pack(pack, fetch_base=fetch_base)
        except (ValueError, IndexError, Exception):  # noqa: BLE001 — anything unparseable is refused
            return egress_rules.RuleDecision(False, tpl, "unparseable_push")

        def is_ancestor(old: str, descendant: str) -> bool:
            resp = github_app._call("GET", f"{api}/repos/{repo}/compare/{old}...{descendant}", token=token)
            return resp.status == 200 and (resp.json() or {}).get("status") in ("ahead", "identical")

        return egress_rules.check_push(updates, objects, push_pattern=pattern, perms=perms, is_ancestor=is_ancestor,
                                       remote_tree=remote_tree, remote_commit_tree=remote_commit_tree,
                                       allow_delete=allow_delete)

    def _upstream_request(self, provider: str, encoded: str, req: EgressRequest,
                          token: str, api: str = "") -> tuple[str, dict[str, str]]:
        headers = {k: v for k, v in req.headers.items() if k.lower() in _FORWARD}
        if provider == "github":
            # The upstream git origin: a connection's GHES host (`api`), else explicit, else the
            # install's GitHub web origin (GHES too).
            base = (github_urls.web_base_for(api) if api else
                    (os.environ.get(GIT_UPSTREAM_ENV) or github_urls.web_base())).rstrip("/")
            if token:                       # "" = an anonymous read (anonymous_read_allowed)
                basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
                headers["Authorization"] = f"Basic {basic}"
        elif provider == "bitbucket":
            base = (os.environ.get(BITBUCKET_GIT_ENV) or "https://bitbucket.org").rstrip("/")
            headers["Authorization"] = _bitbucket_basic(token)
        elif provider == "bitbucket-api":
            base = (os.environ.get(BITBUCKET_API_ENV) or "https://api.bitbucket.org").rstrip("/")
            headers["Authorization"] = (_bitbucket_basic(token) if ":" in token
                                        else f"Bearer {token}")
        else:
            base = api or github_app.api_base()
            headers["Authorization"] = f"Bearer {token}"
        url = upstream_url(base, encoded, req.query)
        if provider in ("github", "github-api"):
            # THE SEND POINT (BOSSAPP-1286): a GitHub credential reaches only its own host.
            github_urls.check_send(token, url, where="egress")
        return url, headers

    def _refuse(self, db: Any, claims: Optional[RunTokenClaims], provider: str, method: str, template: str,
                reason: str) -> EgressResponse:
        self._log(db, claims, provider, method, template, 403, 0, 0, "denied", reason)
        self._deny_event(db, claims, provider, method, template, reason)
        return EgressResponse(403, {"content-type": "application/json"},
                              json.dumps({"error": "forbidden", "reason": reason}).encode())

    def _deny_event(self, db: Any, claims: Optional[RunTokenClaims], provider: str, method: str, template: str,
                    reason: str) -> None:
        lineage.denied(db, org_id=claims.org_id if claims else "", event_type="egress.denied", reason=reason,
                       run_id=claims.task.run_id if claims else None, grant_id=claims.grant_id if claims else None,
                       chain=claims.chain.to_boss_chain() if claims else None, sub=claims.sub if claims else None,
                       jti=claims.jti if claims else None,
                       details={"provider": provider, "method": method.upper(), "path_template": template})

    def _log(self, db: Any, claims: Optional[RunTokenClaims], provider: str, method: str, template: str,
             status: int, bytes_out: int, bytes_in: int, decision: str, reason: Optional[str]) -> None:
        from sqlalchemy import text

        if claims is None:
            return       # an unauthenticated request has no run to attribute; it is in the event log
        db.execute(text(
            "INSERT INTO identity_egress_log (org_id, run_id, jti, chain, provider, method, path_template, status, "
            " bytes_out, bytes_in, decision, deny_reason) VALUES (:org, :run, :jti, CAST(:chain AS JSONB), :p, :m, "
            " :t, :s, :bo, :bi, :d, :r)"),
            {"org": claims.org_id, "run": claims.task.run_id, "jti": claims.jti,
             "chain": json.dumps(claims.chain.to_boss_chain()), "p": provider, "m": method.upper(), "t": template,
             "s": status, "bo": bytes_out, "bi": bytes_in, "d": decision, "r": reason})
