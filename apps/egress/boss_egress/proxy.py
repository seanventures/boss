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
"""
from __future__ import annotations

import base64
import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Optional
from urllib.parse import parse_qsl

from boss_engine.identity import egress_rules, github_app, lineage, runs, tokens
from boss_engine.identity.principal import RunTokenClaims

log = logging.getLogger("boss.egress")

GIT_UPSTREAM_ENV = "BOSS_GITHUB_GIT_URL"
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


class EgressProxy:
    def __init__(self, engine: Any, *, upstream: Optional[Upstream] = None):
        self.engine = engine
        self.upstream = upstream or _httpx_upstream

    # ---- the one entry point ----------------------------------------------------------------------
    def handle(self, req: EgressRequest) -> EgressResponse:
        provider, _, rest_raw = req.path.lstrip("/").partition("/")
        rest_raw = "/" + rest_raw
        claims: Optional[RunTokenClaims] = None
        template = "{unparsed}"
        token = ""
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
            pattern = github_app.push_ref_pattern(claims, db=db)
            pairs = parse_qsl(req.query, keep_blank_values=True)
            query = dict(pairs)
            if len(query) != len(pairs):
                # One key twice: the rules would judge one value and GitHub might act on the other.
                return self._refuse(db, claims, provider, req.method, template, "duplicate_query_key")
            minted: dict[str, str] = {}

            def installation_token() -> str:
                if "t" not in minted:
                    minted["t"] = self._installation_token(claims, db)
                return minted["t"]

            if provider == "github":
                decision = egress_rules.check_git(req.method, rest, query, perms=perms, repos=repos)
            elif provider == "github-api":
                body_json = None
                if req.body and req.method.upper() in ("POST", "PUT", "PATCH"):
                    try:
                        body_json = json.loads(req.body)
                    except ValueError:
                        body_json = None
                try:
                    decision = egress_rules.check_api(
                        req.method, rest, perms=perms, repos=repos,
                        body=body_json if isinstance(body_json, dict) else None, push_pattern=pattern,
                        pr_head=lambda repo, n: self._pr_head(repo, n, installation_token()))
                except github_app.GitHubRefused as exc:
                    return self._refuse(db, claims, provider, req.method, "/repos/{o}/{r}/pulls/{n}", exc.code)
            else:
                decision = egress_rules.RuleDecision(False, "{unknown}", "unknown_provider")
            template = decision.template
            if not decision.allowed:
                return self._refuse(db, claims, provider, req.method, template, decision.reason)
            try:
                token = installation_token()
            except github_app.GitHubRefused as exc:
                return self._refuse(db, claims, provider, req.method, template, exc.code)
            if provider == "github" and rest.endswith("/git-receive-pack") and req.method.upper() == "POST":
                if req.headers.get("content-encoding"):
                    return self._refuse(db, claims, provider, req.method, template, "encoded_push")
                verdict = self._check_push(req.body, rest, perms, pattern, token)
                if not verdict.allowed:
                    return self._refuse(db, claims, provider, req.method, template, verdict.reason)
            try:
                url, headers = self._upstream_request(provider, encoded, req, token)
            except egress_rules.PathRefused as exc:
                return self._refuse(db, claims, provider, req.method, template, str(exc))
        # Upstream call outside the DB transaction.
        try:
            resp = self.upstream(req.method.upper(), url, headers, req.body)
        except Exception:  # noqa: BLE001
            resp = EgressResponse(502, {}, b'{"error":"upstream_unreachable"}')
        with self.engine.begin() as db:
            self._log(db, claims, provider, req.method, template, resp.status, len(req.body), len(resp.body),
                      "allowed", None)
        out_headers = {k: v for k, v in resp.headers.items() if k.lower() in _PASS_RESPONSE}
        return EgressResponse(resp.status, out_headers, resp.body)

    # ---- pieces -----------------------------------------------------------------------------------
    def _installation_token(self, claims: RunTokenClaims, db: Any) -> str:
        cached = github_app.cached_for_run(claims.task.run_id)
        if cached is not None:
            return cached.token
        return github_app.mint_for_run(claims, task=claims.task, db=db).token

    def _pr_head(self, repo: str, number: int, token: str) -> Optional[str]:
        resp = github_app._call("GET", f"/repos/{repo}/pulls/{number}", token=token)
        if resp.status != 200:
            return None
        data = resp.json() or {}
        head = data.get("head") or {}
        if str((head.get("repo") or {}).get("full_name", "")).lower() != repo.lower():
            return None                      # a fork's branch is never the run's
        return str(head.get("ref") or "") or None

    def _check_push(self, body: bytes, rest: str, perms: Mapping[str, str], pattern: str,
                    token: str) -> egress_rules.RuleDecision:
        tpl = "/{o}/{r}.git/git-receive-pack"
        repo = "/".join(rest.strip("/").split("/")[:2]).removesuffix(".git")

        def remote_json(path: str) -> Optional[dict]:
            resp = github_app._call("GET", f"/repos/{repo}{path}", token=token)
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
            resp = github_app._call("GET", f"/repos/{repo}/compare/{old}...{descendant}", token=token)
            return resp.status == 200 and (resp.json() or {}).get("status") in ("ahead", "identical")

        return egress_rules.check_push(updates, objects, push_pattern=pattern, perms=perms, is_ancestor=is_ancestor,
                                       remote_tree=remote_tree, remote_commit_tree=remote_commit_tree)

    def _upstream_request(self, provider: str, encoded: str, req: EgressRequest,
                          token: str) -> tuple[str, dict[str, str]]:
        headers = {k: v for k, v in req.headers.items() if k.lower() in _FORWARD}
        if provider == "github":
            base = (os.environ.get(GIT_UPSTREAM_ENV) or "https://github.com").rstrip("/")
            basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
            headers["Authorization"] = f"Basic {basic}"
        else:
            base = github_app.api_base()
            headers["Authorization"] = f"Bearer {token}"
        return upstream_url(base, encoded, req.query), headers

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
