"""Prove a connection with a real operation, and say what the answer means.

READS NEVER PROBE; CHECKS PROVE AND TIMESTAMP. `setup_view.build` renders what this module last
recorded and never calls out. Everything here calls out, on purpose, and writes down what it
found and when.

THE REPOSITORY. The old check read `GET /repos → permissions.push`. For a fine-grained token that
object is the account's ROLE on the repository, not what the token may do, so a green "Can push"
could still fail at push or PR time — and the pull-request permission was never asked at all.
Three probes, none with an effect anybody sees:

  1. Read — `GET /repos/{o}/{r}`: the default branch, privacy, and the token's expiry header.
  2. Write — `POST /repos/{o}/{r}/git/blobs`: needs Contents: write. A 201 makes an unreferenced
     blob GitHub garbage-collects; no ref moves, so no push event, webhook or CI fires (creating a
     branch would trigger `on: push`, which is why it is not the probe).
  3. Pull requests — `POST /repos/{o}/{r}/pulls` with a head branch that does not exist: needs
     Pull requests: write. 422 "Validation Failed" means the token was allowed to ask and
     nothing was created; 403 "Resource not accessible by personal access token" means it may
     not.

THE MODEL. One completion — "Reply with OK", at most 5 tokens — through `governed_proof`, in
every edition: reserved against the budget, receipted, settled at its real cost. A subscription
is proved with one read-only CLI turn, the same way.

`http` is injectable everywhere so the suite drives these against fakes (tests/e2e/stubs and
engine/tests/fixtures/github_probe_*.json); no test reaches GitHub or a provider.
"""
from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional
from urllib.parse import urlparse

#: Days before expiry a token reads amber.
EXPIRING_DAYS = 7
#: A recorded check older than this is stale: the console re-checks in the background.
STALE_AFTER = timedelta(hours=24)

#: GitHub's word for an organization that has not yet approved a fine-grained token.
_ORG_APPROVAL = re.compile(r"organization.*(approv|forbids access via a fine-grained)", re.I | re.S)


@dataclass
class HttpAnswer:
    status: int                      # 0: the host could not be reached
    headers: dict[str, str]          # lower-cased names
    body: Any
    error: str = ""


Http = Callable[..., HttpAnswer]


def default_http(method: str, url: str, *, headers: dict[str, str],
                 json_body: Any = None, timeout: float = 15.0) -> HttpAnswer:
    """One request, never raising, never following a redirect to another host."""
    import httpx

    try:
        response = httpx.request(method, url, headers=headers, json=json_body, timeout=timeout,
                                 follow_redirects=False)
    except httpx.HTTPError as exc:
        return HttpAnswer(0, {}, None, type(exc).__name__)
    try:
        body: Any = response.json()
    except ValueError:
        body = response.text[:2000]
    return HttpAnswer(response.status_code,
                      {k.lower(): v for k, v in response.headers.items()}, body)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value else None


def parse_time(value: Any) -> Optional[datetime]:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def parse_token_expiry(header: Optional[str], *, now: Optional[datetime] = None
                       ) -> Optional[datetime]:
    """`github-authentication-token-expiration`: `2027-01-02 10:00:00 UTC` per GitHub's docs,
    `2027-01-02 10:00:00 +0500` as observed. A value within two minutes of now is ignored: GitHub
    has been seen to send the current server time for fine-grained tokens (go-github#3708), and
    reading that as "expired" would turn every working token red."""
    raw = str(header or "").strip()
    if not raw:
        return None
    parsed: Optional[datetime] = None
    for fmt in ("%Y-%m-%d %H:%M:%S %Z", "%Y-%m-%d %H:%M:%S %z", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            parsed = datetime.strptime(raw.replace(" UTC", " +0000") if fmt.endswith("%z")
                                       else raw, fmt)
            break
        except ValueError:
            continue
    if parsed is None:
        return None
    parsed = parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    parsed = parsed.astimezone(timezone.utc)
    if abs((parsed - (now or now_utc())).total_seconds()) < 120:
        return None
    return parsed


def _rate_reset(answer: HttpAnswer) -> Optional[datetime]:
    """When GitHub says it will answer again: `retry-after` first, then `x-ratelimit-reset`."""
    retry = answer.headers.get("retry-after")
    if retry and str(retry).isdigit():
        return now_utc() + timedelta(seconds=int(retry))
    reset = answer.headers.get("x-ratelimit-reset")
    if reset and str(reset).isdigit():
        return datetime.fromtimestamp(int(reset), tz=timezone.utc)
    return None


def _is_rate_limited(answer: HttpAnswer) -> bool:
    if answer.status == 429:
        return True
    return answer.status == 403 and (
        answer.headers.get("x-ratelimit-remaining") == "0"
        or "rate limit" in json.dumps(answer.body or "").lower())


def _message(answer: HttpAnswer) -> str:
    body = answer.body
    if isinstance(body, dict):
        return str(body.get("message") or "")
    return str(body or "")[:300]


def _gh_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "boss-setup-check"}


def _failure_state(answer: HttpAnswer, *, refused: str) -> str:
    """What a non-success GitHub answer means, before the probe's own `refused` state."""
    if answer.status == 0:
        return "unreachable"
    if _is_rate_limited(answer):
        return "rate_limited"
    if answer.status == 401:
        return "token_rejected"
    if answer.status == 403 and _ORG_APPROVAL.search(_message(answer)):
        return "awaiting_org_approval"
    return refused


def inspect_token(token: str, *, api_base: str = "https://api.github.com",
                  http: Optional[Http] = None) -> dict[str, Any]:
    """Who the token belongs to, when it expires, and the repositories it can see.

    Returns {ok, state, account_login, expires_at, repos, retry_at}. Never includes the token.
    """
    http = http or default_http
    base = api_base.rstrip("/")
    who = http("GET", f"{base}/user", headers=_gh_headers(token))
    if who.status != 200:
        state = _failure_state(who, refused="token_rejected")
        reset = _rate_reset(who) if state == "rate_limited" else None
        return {"ok": False, "state": state, "account_login": None, "expires_at": None,
                "repos": [], "retry_at": iso(reset)}
    expires = parse_token_expiry(who.headers.get("github-authentication-token-expiration"))
    login = (who.body or {}).get("login") if isinstance(who.body, dict) else None
    repos: list[dict[str, Any]] = []
    for page in range(1, 4):
        listed = http("GET", f"{base}/user/repos?per_page=100&page={page}&sort=updated"
                             "&affiliation=owner,collaborator,organization_member",
                      headers=_gh_headers(token))
        if listed.status != 200 or not isinstance(listed.body, list):
            break
        for item in listed.body:
            if not isinstance(item, dict) or "/" not in str(item.get("full_name") or ""):
                continue
            owner = item.get("owner") if isinstance(item.get("owner"), dict) else {}
            repos.append({
                "full_name": str(item["full_name"]),
                "private": bool(item.get("private")),
                "default_branch": item.get("default_branch"),
                "owner_type": str(owner.get("type") or "User"),
            })
        if len(listed.body) < 100:
            break
    return {"ok": True, "state": "working", "account_login": login, "expires_at": iso(expires),
            "repos": repos, "retry_at": None}


def probe_repository(token: str, full_name: str, *, api_base: str = "https://api.github.com",
                     http: Optional[Http] = None) -> dict[str, Any]:
    """Read, write and pull-request probes against one repository.

    Returns {state, checks: {read, write, pull_requests}, default_branch, private, expires_at,
    retry_at}. `state` is the one pill for the repository; each check is {ok, state, at}.
    """
    http = http or default_http
    base = api_base.rstrip("/")
    at = iso(now_utc())
    headers = _gh_headers(token)
    out: dict[str, Any] = {"default_branch": None, "private": None, "expires_at": None,
                           "retry_at": None}
    unknown = {"ok": None, "state": "not_run", "at": None}

    read = http("GET", f"{base}/repos/{full_name}", headers=headers)
    if read.status != 200 or not isinstance(read.body, dict):
        state = _failure_state(read, refused="no_access")
        if state == "rate_limited":
            out["retry_at"] = iso(_rate_reset(read))
        out.update(state=state, checks={"read": {"ok": False, "state": state, "at": at},
                                        "write": dict(unknown), "pull_requests": dict(unknown)})
        return out
    out["expires_at"] = iso(parse_token_expiry(
        read.headers.get("github-authentication-token-expiration")))
    out["default_branch"] = read.body.get("default_branch") or "main"
    out["private"] = bool(read.body.get("private"))
    checks: dict[str, Any] = {"read": {"ok": True, "state": "working", "at": at}}

    blob = http("POST", f"{base}/repos/{full_name}/git/blobs", headers=headers,
                json_body={"content": "boss setup check", "encoding": "utf-8"})
    # 409 is an EMPTY repository ("Git Repository is empty"): GitHub only says so to a caller
    # it has already authorised to write, so the permission is there.
    if blob.status in (201, 409):
        checks["write"] = {"ok": True, "state": "working", "at": at}
    else:
        checks["write"] = {"ok": False, "state": _failure_state(blob, refused="cant_write"),
                           "at": at}

    head = f"boss-setup-check-{uuid.uuid4().hex[:8]}"
    pulls = http("POST", f"{base}/repos/{full_name}/pulls", headers=headers,
                 json_body={"title": "boss setup check", "head": head,
                            "base": out["default_branch"]})
    if pulls.status in (422, 201):
        checks["pull_requests"] = {"ok": True, "state": "working", "at": at}
    else:
        checks["pull_requests"] = {"ok": False,
                                   "state": _failure_state(pulls, refused="cant_open_prs"),
                                   "at": at}
    out["checks"] = checks
    state = "working"
    for name in ("write", "pull_requests"):
        if not checks[name]["ok"]:
            state = checks[name]["state"]
            if state == "rate_limited":
                out["retry_at"] = iso(_rate_reset(blob if name == "write" else pulls))
            break
    out["state"] = state
    return out


def repository_state(checks: dict[str, Any], *, expires_at: Optional[datetime],
                     checked_at: Optional[datetime], now: Optional[datetime] = None) -> str:
    """The one pill state for a repository, from its recorded checks and token expiry."""
    now = now or now_utc()
    if expires_at is not None and expires_at <= now:
        return "expired"
    if not checks or checked_at is None:
        return "checking"
    recorded = str(checks.get("state") or "")
    if not recorded:
        for name in ("read", "write", "pull_requests"):
            part = checks.get(name) or {}
            if part.get("ok") is False:
                recorded = str(part.get("state") or "")
                break
        else:
            recorded = "working"
    if recorded == "token_rejected" and expires_at is not None and expires_at <= now:
        return "expired"
    if recorded == "working" and expires_at is not None \
            and expires_at - now <= timedelta(days=EXPIRING_DAYS):
        return "expiring"
    return recorded


# ── Models ────────────────────────────────────────────────────────────────────────────────────

_NO_CREDIT = ("insufficient", "credit", "quota", "billing", "payment required", "balance")
_NOT_FOUND = ("model_not_found", "does not exist", "no such model", "unknown model",
              "not a valid model", "model not found")


def classify_model_failure(status: int, detail: str, *, subscription: bool = False) -> str:
    """The model pill state for a failed check."""
    lowered = (detail or "").lower()
    if status == 0:
        return "unreachable"
    if status == 429 or "rate limit" in lowered or "usage limit" in lowered:
        return "rate_limited"
    if status == 402 or any(word in lowered for word in _NO_CREDIT):
        return "no_credit"
    if status == 404 or any(word in lowered for word in _NOT_FOUND):
        return "model_not_found"
    if status in (401, 403):
        return "login_expired" if subscription else "key_rejected"
    if subscription:
        from .model_failures import is_login_rejected

        if is_login_rejected(lowered):
            return "login_expired"
    return "failed"


def _retry_after(headers: dict[str, str], detail: str) -> Optional[str]:
    retry = headers.get("retry-after") if headers else None
    if retry and str(retry).isdigit():
        return iso(now_utc() + timedelta(seconds=int(retry)))
    try:
        from .provider_limits import _parse_retry_at

        when = _parse_retry_at(detail or "")
        return iso(when) if when else None
    except Exception:  # noqa: BLE001
        return None


def complete_once(*, api: str, endpoint: str, api_key: str, model: str,
                  http: Optional[Http] = None, timeout: float = 30.0) -> dict[str, Any]:
    """One tiny completion. {ok, state, status, latency_ms, usage, retry_at, host}."""
    http = http or default_http
    if api == "anthropic":
        headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01",
                   "content-type": "application/json"}
    else:
        headers = {"content-type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
    body = {"model": model, "max_tokens": 5,
            "messages": [{"role": "user", "content": "Reply with OK"}]}
    started = time.monotonic()
    answer = http("POST", endpoint, headers=headers, json_body=body, timeout=timeout)
    latency = int((time.monotonic() - started) * 1000)
    host = urlparse(endpoint).hostname or endpoint
    if answer.status in (200, 201):
        usage_raw = (answer.body or {}).get("usage") if isinstance(answer.body, dict) else {}
        usage_raw = usage_raw if isinstance(usage_raw, dict) else {}
        usage = {
            "model": model,
            "prompt_tokens": int(usage_raw.get("prompt_tokens")
                                 or usage_raw.get("input_tokens") or 0),
            "completion_tokens": int(usage_raw.get("completion_tokens")
                                     or usage_raw.get("output_tokens") or 0),
            "base_url": endpoint,
        }
        return {"ok": True, "state": "working", "status": answer.status,
                "latency_ms": latency, "usage": usage, "host": host, "retry_at": None}
    detail = _message(answer) or answer.error
    state = classify_model_failure(answer.status, detail)
    return {"ok": False, "state": state, "status": answer.status, "latency_ms": latency,
            "host": host, "error": f"HTTP {answer.status}" if answer.status else "unreachable",
            "retry_at": _retry_after(answer.headers, detail) if state == "rate_limited" else None}


def list_models(*, api: str, base_url: str, api_key: str = "", local_ollama: bool = False,
                http: Optional[Http] = None) -> list[str]:
    """The provider's own model list, for the Model picker. Empty when it will not say."""
    http = http or default_http
    base = base_url.rstrip("/")
    if local_ollama:
        root = re.sub(r"/v1/?$", "", base)
        answer = http("GET", f"{root}/api/tags", headers={}, timeout=5)
        models = (answer.body or {}).get("models") if isinstance(answer.body, dict) else None
        return sorted(str(m.get("name")) for m in models or [] if isinstance(m, dict)
                      and m.get("name"))
    headers = ({"x-api-key": api_key, "anthropic-version": "2023-06-01"} if api == "anthropic"
               else ({"Authorization": f"Bearer {api_key}"} if api_key else {}))
    answer = http("GET", f"{base}/models", headers=headers, timeout=10)
    data = (answer.body or {}).get("data") if isinstance(answer.body, dict) else None
    if answer.status != 200 or not isinstance(data, list):
        return []
    return sorted({str(item.get("id")) for item in data if isinstance(item, dict)
                   and item.get("id")})[:500]
