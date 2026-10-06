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
#: Inbound headers that never cross the proxy.
_STRIP = {"authorization", "cookie", "proxy-authorization", "x-forwarded-for", "x-forwarded-host",
          "x-forwarded-proto", "x-real-ip", "host", "content-length", "connection", "dpop",
          "x-boss-egress", "x-github-token", "private-token"}
_PASS_RESPONSE = {"content-type", "cache-control", "etag", "link", "location", "x-github-request-id",
                  "x-ratelimit-remaining", "x-ratelimit-reset", "content-encoding"}


@dataclass(frozen=True)
class EgressRequest:
    method: str
    path: str                                  # "/github/acme/web.git/info/refs" or "/github-api/repos/…"
    query: str = ""
    headers: Mapping[str, str] = field(default_factory=dict)   # lowercase keys
    body: bytes = field(default=b"", repr=False)


@dataclass(frozen=True)
class EgressResponse:
    status: int
    headers: Mapping[str, str] = field(default_factory=dict)
    body: bytes = field(default=b"", repr=False)


#: (method, url, headers, body) -> EgressResponse. Replaced in tests.
Upstream = Callable[[str, str, Mapping[str, str], bytes], EgressResponse]


def _httpx_upstream(method: str, url: str, headers: Mapping[str, str], body: bytes) -> EgressResponse:
    import httpx

    with httpx.Client(timeout=60.0, follow_redirects=False) as client:
        resp = client.request(method, url, headers=dict(headers), content=body or None)
        return EgressResponse(resp.status_code, dict(resp.headers), resp.content)


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
        provider, _, rest = req.path.lstrip("/").partition("/")
        rest = "/" + rest
        claims: Optional[RunTokenClaims] = None
        template = "{unparsed}"
        with self.engine.begin() as db:
            try:
                claims = runs.resolve_egress(_credential(req), db=db)
            except tokens.TokenError as exc:
                self._deny_event(db, None, provider, req.method, template, exc.reason)
                return EgressResponse(401, {"content-type": "application/json"},
                                      b'{"error":"unauthenticated"}')
            perms = github_app.run_permissions(claims, db=db)
            repos = github_app.run_repos(claims.task)
            pattern = github_app.push_ref_pattern(claims, db=db)
            query = dict(parse_qsl(req.query))
            if provider == "github":
                decision = egress_rules.check_git(req.method, rest, query, perms=perms, repos=repos)
            elif provider == "github-api":
                body_json = None
                if req.body and req.method.upper() in ("POST", "PUT", "PATCH"):
                    try:
                        body_json = json.loads(req.body)
                    except ValueError:
                        body_json = None
                decision = egress_rules.check_api(req.method, rest, perms=perms, repos=repos,
                                                  body=body_json if isinstance(body_json, dict) else None,
                                                  push_pattern=pattern)
            else:
                decision = egress_rules.RuleDecision(False, "{unknown}", "unknown_provider")
            template = decision.template
            if not decision.allowed:
                return self._refuse(db, claims, provider, req.method, template, decision.reason)
            try:
                token = self._installation_token(claims, db)
            except github_app.GitHubRefused as exc:
                return self._refuse(db, claims, provider, req.method, template, exc.code)
            if provider == "github" and rest.endswith("/git-receive-pack") and req.method.upper() == "POST":
                verdict = self._check_push(req.body, rest, perms, pattern, token)
                if not verdict.allowed:
                    return self._refuse(db, claims, provider, req.method, template, verdict.reason)
        # Upstream call outside the DB transaction.
        url, headers = self._upstream_request(provider, rest, req, token)
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

    def _check_push(self, body: bytes, rest: str, perms: Mapping[str, str], pattern: str,
                    token: str) -> egress_rules.RuleDecision:
        tpl = "/{o}/{r}.git/git-receive-pack"
        try:
            updates, pack = egress_rules.parse_receive_pack(body)
            objects = egress_rules.parse_pack(pack)
        except (ValueError, IndexError, Exception):  # noqa: BLE001 — anything unparseable is refused
            return egress_rules.RuleDecision(False, tpl, "unparseable_push")
        repo = "/".join(rest.strip("/").split("/")[:2]).removesuffix(".git")

        def is_ancestor(old: str, descendant: str) -> bool:
            resp = github_app._call("GET", f"/repos/{repo}/compare/{old}...{descendant}", token=token)
            return resp.status == 200 and (resp.json() or {}).get("status") in ("ahead", "identical")

        return egress_rules.check_push(updates, objects, push_pattern=pattern, perms=perms, is_ancestor=is_ancestor)

    def _upstream_request(self, provider: str, rest: str, req: EgressRequest, token: str) -> tuple[str, dict[str, str]]:
        headers = {k: v for k, v in req.headers.items() if k.lower() not in _STRIP}
        if provider == "github":
            base = (os.environ.get(GIT_UPSTREAM_ENV) or "https://github.com").rstrip("/")
            basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
            headers["Authorization"] = f"Basic {basic}"
        else:
            base = github_app.api_base()
            headers["Authorization"] = f"Bearer {token}"
        url = base + rest + (f"?{req.query}" if req.query else "")
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
