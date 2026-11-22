"""Contract tests: a list of HTTP cases, each with a method, headers, a body and what it must
answer — run against a deployed thing, judged case by case, reported as a table (BOSSAPP-1143).

`smoke_test` asks one question: does GET / answer 2xx. An API gateway that was just given a new
tenant's routes needs a dozen: does a valid JWT reach the upstream (200), does no token get a
401, does the eleventh request in a second get a 429, does the rejected path stay rejected. Each
of those is a method, maybe a header, maybe a JSON body, an expected status and a few JSON fields
— so that is exactly what a case is.

Pure: no database, no settings. `run_cases` takes the cases and returns the results; the tool
(`contract_test`, tools/builtin.py) and the capability (`contract_test`,
workflow_capabilities.py) decide who may run it and what a failure does to the run.
"""

from __future__ import annotations

import ipaddress
import json
import re
import time
import urllib.parse
from typing import Any, Iterable, Mapping, Optional

#: The methods a case may use. Anything else is a typo, refused before a request is made.
METHODS = ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS")

#: A single request never waits longer than this, whatever the case asks for.
MAX_TIMEOUT_S = 60
#: Cases per step. Enough for an onboarding's contract; a thousand-case suite is a test runner's
#: job, not a workflow step's.
MAX_CASES = 100
#: How much of a body is kept on the result, so a run page stays readable.
BODY_EXCERPT = 2000


class CaseError(ValueError):
    """A case that cannot be run as written. Said with the case's name and the field."""


class HostRefused(ValueError):
    """A request (or a redirect hop) to a host a contract test may not reach."""


#: Never, whatever the project says: cloud metadata endpoints (the shared ticket-connector list,
#: connectors/tickets/http.py) — and every link-local address with them.
def _blocked_hosts() -> tuple[str, ...]:
    from ..connectors.tickets.http import _BLOCKED_HOSTS

    return tuple(_BLOCKED_HOSTS)


def _addresses(host: str, port: int = 0) -> list[str]:
    """DNS, ONCE per request: the addresses checked are the addresses connected to (pinned), so
    a name that answers differently a moment later (DNS rebinding) changes nothing."""
    from ..identity import http as safe_http

    try:
        return [str(ipaddress.ip_address(host))]
    except ValueError:
        pass
    try:
        return [a.split("%", 1)[0] for a in safe_http._resolve(host, port or 80)]
    except OSError:
        return []          # does not resolve: the request fails as "did not answer"


def _default_port(scheme: str) -> int:
    return 443 if scheme == "https" else 80


def origin_of(url: str) -> Optional[tuple[str, str, int]]:
    """(scheme, host, port) of an http(s) URL — what the Community allow-list matches."""
    parts = urllib.parse.urlsplit(url or "")
    scheme = (parts.scheme or "").lower()
    host = (parts.hostname or "").lower().rstrip(".")
    if scheme not in ("http", "https") or not host:
        return None
    try:
        port = parts.port or _default_port(scheme)
    except ValueError:
        return None
    return scheme, host, int(port)


def check_host(url: str, allowed_private: frozenset[str] = frozenset(), *,
               project_only: bool = False,
               origins: frozenset[tuple[str, str, int]] = frozenset()) -> list[str]:
    """THE SSRF GUARD, for the first request and every redirect hop. Returns the addresses the
    request may connect to — the ones it was checked against (pinned).

    A contract test sends whatever method, headers and body a caller writes, and returns the
    answer — so where it may send is the whole security question. Metadata and link-local hosts
    are refused always (the shared connector rule, `connectors/tickets/http.check_host`), by
    name or by any address the name resolves to.

    Paid editions: loopback, private and otherwise non-public addresses are refused unless the
    host is one of `allowed_private` (the project's own deployment targets and hosts, from the
    database, never from the arguments).

    `project_only` (Community, BOSSAPP-1166): Community has no egress network policy, so the guard
    is the policy. The request's exact ORIGIN — scheme, host AND port — must be one of
    `origins`: the URLs of deployment targets a PERSON created (`project_origins`). Another
    port on the same host (the BOSS API on localhost), a public host, or a target an agent
    created is refused."""
    origin = origin_of(url)
    if origin is None:
        raise HostRefused(f"{url!r} is not an http(s) URL.")
    scheme, host, port = origin
    addresses = _addresses(host, port)
    parsed = []
    for raw in addresses:
        try:
            parsed.append(ipaddress.ip_address(raw))
        except ValueError:
            continue
    if host in _blocked_hosts() or any(a.is_link_local for a in parsed) or \
            any(raw in _blocked_hosts() for raw in addresses):
        raise HostRefused(f"{host} is a metadata or link-local address; a contract test never "
                          "sends there.")
    if project_only:
        if origin not in origins:
            raise HostRefused(
                f"{scheme}://{host}:{port} is not one of this project's deployment targets. In "
                "Community a contract test reaches only the exact scheme, host and port of a "
                "deployment target a person created (its health or public URL).")
        return addresses
    if host in allowed_private:
        return addresses
    from ..identity import http as safe_http

    if host == "localhost" or not addresses or \
            any(not safe_http._is_public(raw) for raw in addresses):
        if not addresses:
            return addresses          # does not resolve: "did not answer", nothing sent
        raise HostRefused(
            f"{host} is a loopback or private address and not one of this project's "
            "deployment targets. A contract test reaches the project's own deployments and "
            "public hosts only.")
    return addresses


#: Redirect hops one case follows, each checked like the first request.
MAX_REDIRECTS = 5


def _one_case(index: int, raw: Any, base_url: str, shared_headers: Mapping[str, str]) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise CaseError(f"case {index + 1} is not an object.")
    name = str(raw.get("name") or "").strip() or f"case {index + 1}"
    method = str(raw.get("method") or "GET").strip().upper()
    if method not in METHODS:
        raise CaseError(f"{name}: method {method!r} is not one of {', '.join(METHODS)}.")
    url = str(raw.get("url") or "").strip()
    path = str(raw.get("path") or "").strip()
    if not url:
        if not path:
            raise CaseError(f"{name}: give a url, or a path with a base_url.")
        if not base_url:
            raise CaseError(f"{name}: has a path but the step has no base_url.")
        url = base_url.rstrip("/") + "/" + path.lstrip("/")
    if not url.startswith(("http://", "https://")):
        raise CaseError(f"{name}: url must start with http:// or https://.")
    headers = {str(k): str(v) for k, v in dict(shared_headers or {}).items()}
    own = raw.get("headers") or {}
    if not isinstance(own, Mapping):
        raise CaseError(f"{name}: headers must be an object.")
    headers.update({str(k): str(v) for k, v in own.items()})
    body: Optional[bytes] = None
    if raw.get("json") is not None:
        body = json.dumps(raw["json"]).encode("utf-8")
        headers.setdefault("Content-Type", "application/json")
    elif raw.get("body") is not None:
        body = str(raw["body"]).encode("utf-8")
    expect_status = raw.get("expect_status")
    if expect_status in (None, ""):
        expected: Optional[tuple[int, ...]] = None
    else:
        values = expect_status if isinstance(expect_status, (list, tuple)) else [expect_status]
        try:
            expected = tuple(int(v) for v in values)
        except (TypeError, ValueError):
            raise CaseError(f"{name}: expect_status {expect_status!r} is not an HTTP status.") from None
    expect_json = raw.get("expect_json") or {}
    if not isinstance(expect_json, Mapping):
        raise CaseError(f"{name}: expect_json must be an object of path -> expected value.")
    exists = raw.get("expect_json_exists") or []
    if isinstance(exists, str):
        exists = [exists]
    if not isinstance(exists, (list, tuple)):
        raise CaseError(f"{name}: expect_json_exists must be a list of paths.")
    contains = raw.get("expect_body_contains")
    return {
        "name": name, "method": method, "url": url, "headers": headers, "body": body,
        "expect_status": expected, "expect_json": dict(expect_json),
        "expect_json_exists": [str(p) for p in exists],
        "expect_body_contains": None if contains in (None, "") else str(contains),
    }


_SEGMENT = re.compile(r"([^.\[\]]+)|\[(\d+)\]")


def json_path(value: Any, path: str) -> tuple[bool, Any]:
    """Read `$.a.b[0].c` (or `a.b.0.c`) out of parsed JSON: (found, value)."""
    text = str(path or "").strip()
    if text.startswith("$"):
        text = text[1:]
    text = text.lstrip(".")
    if not text:
        return True, value
    current = value
    for name, index in _SEGMENT.findall(text):
        if index != "":
            if not isinstance(current, list) or int(index) >= len(current):
                return False, None
            current = current[int(index)]
            continue
        if isinstance(current, list) and name.isdigit():
            if int(name) >= len(current):
                return False, None
            current = current[int(name)]
            continue
        if not isinstance(current, Mapping) or name not in current:
            return False, None
        current = current[name]
    return True, current


def _exchange(url: str, method: str, headers: Mapping[str, str], body: Optional[bytes],
              addresses: list[str], host_header: str, timeout_s: float
              ) -> tuple[int, dict[str, str], bytes]:
    """One HTTP exchange with a PINNED address: connect to an address the guard checked, never
    to whatever the name resolves to now. TLS still verifies the certificate against the name."""
    from ..identity import http as safe_http

    parts = urllib.parse.urlsplit(url)
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower().rstrip(".")
    port = parts.port or _default_port(scheme)
    target = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    deadline = time.monotonic() + timeout_s
    last: Optional[BaseException] = None
    for ip in safe_http._candidates(addresses):
        try:
            sock = safe_http._open_connection(scheme, host, ip, port, timeout_s)
        except OSError as error:
            last = error
            continue
        try:
            return safe_http._http_exchange(sock, method, host_header, target, headers, body,
                                            deadline)
        finally:
            sock.close()
    raise ConnectionError(f"could not connect to {host}:{port}"
                          + (f" ({type(last).__name__})" if last else ""))


def _credential_header(name: str) -> bool:
    lowered = str(name).lower()
    return lowered in ("authorization", "proxy-authorization", "cookie") or any(
        word in lowered for word in ("token", "secret", "api-key", "apikey", "auth", "session",
                                     "password"))


def _send(case: dict[str, Any], timeout_s: float,
          allowed_private: frozenset[str] = frozenset(),
          project_only: bool = False,
          origins: frozenset[tuple[str, str, int]] = frozenset(),
          secret_headers: frozenset[str] = frozenset()) -> dict[str, Any]:
    started = time.time()
    url, method, body = case["url"], case["method"], case["body"]
    headers = {k: v for k, v in case["headers"].items() if k.lower() != "host"}
    asked_host = next((v for k, v in case["headers"].items() if k.lower() == "host"), None)
    first = origin_of(url)

    def elapsed() -> int:
        return round((time.time() - started) * 1000)

    for hop in range(MAX_REDIRECTS + 1):
        try:
            addresses = check_host(url, allowed_private, project_only=project_only,
                                   origins=origins)
        except HostRefused as refused:
            word = "refused" if hop == 0 else "refused a redirect"
            return {"status": None, "ms": 0 if hop == 0 else elapsed(), "headers": {}, "body": "",
                    "error": f"{word}: {refused}", "refused": True}
        parts = urllib.parse.urlsplit(url)
        # The case's own Host header (a virtual host on the gateway) holds while the request
        # stays on the origin it was written for.
        host_header = (asked_host if asked_host and origin_of(url) == first
                       else parts.netloc)
        try:
            if not addresses:
                raise ConnectionError(f"{parts.hostname} does not resolve")
            status, answer_headers, raw = _exchange(url, method, headers, body, addresses,
                                                    host_header, timeout_s)
        except Exception as error:  # noqa: BLE001 — unreachable is a RESULT, not a crash
            return {"status": None, "ms": elapsed(), "headers": {}, "body": "",
                    "error": f"{type(error).__name__}: {str(error)[:300]}"}
        location = answer_headers.get("location")
        if status in (301, 302, 303, 307, 308) and location and hop < MAX_REDIRECTS:
            url = urllib.parse.urljoin(url, location)
            if origin_of(url) != first:
                # CREDENTIALS STAY WITH THEIR ORIGIN: a redirect elsewhere never carries the
                # case's token, cookie or key (review of #125).
                headers = {k: v for k, v in headers.items()
                           if not _credential_header(k) and k.lower() not in secret_headers}
            if status in (301, 302, 303) and method not in ("GET", "HEAD"):
                method, body = "GET", None
                headers = {k: v for k, v in headers.items() if k.lower() != "content-type"}
            continue
        return {"status": int(status), "ms": elapsed(), "headers": answer_headers,
                "body": raw[:BODY_EXCERPT * 4].decode("utf-8", errors="replace"), "error": None}
    return {"status": None, "ms": elapsed(), "headers": {}, "body": "",
            "error": f"more than {MAX_REDIRECTS} redirects"}


def _judge(case: dict[str, Any], answer: dict[str, Any]) -> list[str]:
    """Every way the answer fell short of the case, in plain sentences. Empty = passed."""
    failures: list[str] = []
    status = answer["status"]
    if status is None:
        if answer.get("refused"):
            return [str(answer["error"])]
        return [f"did not answer: {answer['error'] or 'no response'}"]
    wanted = case["expect_status"]
    if wanted is not None and status not in wanted:
        want = " or ".join(str(w) for w in wanted)
        failures.append(f"answered {status}, expected {want}")
    elif wanted is None and not 200 <= status < 300:
        failures.append(f"answered {status}, expected a 2xx")
    body = answer["body"]
    if case["expect_body_contains"] and case["expect_body_contains"] not in body:
        failures.append(f"body does not contain {case['expect_body_contains']!r}")
    if case["expect_json"] or case["expect_json_exists"]:
        try:
            parsed = json.loads(body) if body.strip() else None
        except ValueError:
            return failures + ["body is not JSON"]
        for path, expected in case["expect_json"].items():
            found, actual = json_path(parsed, path)
            if not found:
                failures.append(f"{path}: missing")
            elif actual != expected:
                failures.append(f"{path}: got {json.dumps(actual)[:120]}, "
                                f"expected {json.dumps(expected)[:120]}")
        for path in case["expect_json_exists"]:
            found, _ = json_path(parsed, path)
            if not found:
                failures.append(f"{path}: missing")
    return failures


def results_table(results: Iterable[Mapping[str, Any]]) -> str:
    """The results as a markdown table — what the step's output shows and a PR comment carries."""
    lines = ["| Case | Request | Status | Time | Result |", "|---|---|---|---|---|"]
    for r in results:
        verdict = "pass" if r.get("ok") else "FAIL — " + "; ".join(r.get("failures") or [])
        status = r.get("status")
        lines.append("| {name} | {method} {url} | {status} | {ms} ms | {verdict} |".format(
            name=str(r.get("name", "")).replace("|", "\\|"),
            method=r.get("method", ""), url=str(r.get("url", "")).replace("|", "\\|"),
            status="—" if status is None else status, ms=r.get("ms", 0),
            verdict=verdict.replace("|", "\\|")))
    return "\n".join(lines)


class HeaderSecretRefused(RuntimeError):
    """A secret reference refused for a header, with words that are safe to show (never a value)."""


def http_auth_problem(db: Any, ref_id: str, project_id: str) -> Optional[str]:
    """Only a secret reference a person marked for HTTP auth (kind api_token, metadata
    `http_auth: true`) may become a request header: a provider's own key (an LLM or GitHub token)
    or a private key is never something a test spec can read out into a request."""
    from sqlalchemy import text

    row = db.execute(text("SELECT secret_kind, metadata FROM secret_refs WHERE id = :i"),
                     {"i": ref_id}).mappings().first()
    if row is None:
        return f"secret reference {ref_id} does not exist"
    meta = row["metadata"] if isinstance(row["metadata"], dict) else {}
    if str(row["secret_kind"] or "") != "api_token" or meta.get("http_auth") is not True:
        return (f"secret reference {ref_id} is not marked for HTTP auth (api_token with "
                "metadata http_auth: true), so it cannot be sent as a request header")
    return None


def resolve_header_secrets(cases: Any, headers: Any, resolver: Any
                           ) -> tuple[Any, Any, Optional[str], frozenset[str]]:
    """Header values that are SECRET REFERENCES, resolved at run time (never a raw token in a spec).

    A header value is a string, or {"secret_ref": "<secret ref id>", "prefix": "Bearer "}: the
    reference is what the workflow spec, the step's recorded input and the logs hold; the value
    exists only in the request this step sends (and, once resolved, is registered for redaction
    by the resolver). `resolver(ref_id) -> str`. Returns (cases, headers, error): `error` names
    the header and the reference id, never a value. The fourth item is the (lower-case) names of
    the headers that held a secret: `run_cases(secret_headers=…)` never sends them to another
    origin on a redirect."""
    secret_names: set[str] = set()

    def one(where: str, mapping: Any) -> tuple[Any, Optional[str]]:
        if not isinstance(mapping, Mapping):
            return mapping, None
        out: dict[str, Any] = {}
        for name, value in mapping.items():
            if isinstance(value, Mapping) and "secret_ref" in value:
                ref = str(value.get("secret_ref") or "").strip()
                if not ref:
                    return mapping, f"{where}header {name!r}: secret_ref is empty."
                try:
                    out[name] = f"{value.get('prefix') or ''}{resolver(ref)}"
                    secret_names.add(str(name).lower())
                except Exception as exc:  # noqa: BLE001 — say which reference, never what it held
                    why = (str(exc)[:200] if isinstance(exc, HeaderSecretRefused)
                           else type(exc).__name__)
                    return mapping, (f"{where}header {name!r}: the secret reference {ref} could "
                                     f"not be resolved ({why}).")
            else:
                out[name] = value
        return out, None

    none: frozenset[str] = frozenset()
    shared, error = one("", headers)
    if error:
        return cases, headers, error, none
    if not isinstance(cases, (list, tuple)):
        return cases, shared, None, frozenset(secret_names)
    resolved = []
    for i, case in enumerate(cases):
        if isinstance(case, Mapping) and isinstance(case.get("headers"), Mapping):
            own, error = one(f"case {i + 1}: ", case["headers"])
            if error:
                return cases, headers, error, none
            case = {**case, "headers": own}
        resolved.append(case)
    return resolved, shared, None, frozenset(secret_names)


def run_cases(cases: Any, *, base_url: str = "", timeout_s: Any = 10,
              headers: Optional[Mapping[str, str]] = None,
              allowed_private: Iterable[str] = (),
              deadline_s: Optional[float] = None,
              project_only: bool = False,
              origins: Iterable[tuple[str, str, int]] = (),
              secret_headers: Iterable[str] = ()) -> dict[str, Any]:
    """Run every case and say, per case and in total, what passed.

    Returns `ok: False` with an `error` when the cases cannot be run as written (not a list, a
    bad method, a path with no base_url) — nothing is sent then. Otherwise `ok` is True and the
    verdict is in `failed`: the tool ran; whether the contract held is the step's judgement
    (workflow_outcomes.contract_test). `project_only` refuses every request whose scheme, host
    and port are not one of `origins` (`check_host`).
    """
    if isinstance(cases, str):
        try:
            cases = json.loads(cases)
        except ValueError:
            return {"ok": False, "error": "cases must be a list of case objects (JSON)."}
    if not isinstance(cases, (list, tuple)) or not cases:
        return {"ok": False, "error": "cases must be a non-empty list of case objects."}
    if len(cases) > MAX_CASES:
        return {"ok": False, "error": f"at most {MAX_CASES} cases per step; got {len(cases)}."}
    try:
        timeout = min(MAX_TIMEOUT_S, max(1, int(timeout_s or 10)))
    except (TypeError, ValueError):
        return {"ok": False, "error": f"timeout_s {timeout_s!r} is not a number of seconds."}
    base = str(base_url or "").strip()
    if base and not base.startswith(("http://", "https://")):
        return {"ok": False, "error": "base_url must start with http:// or https://."}
    shared = headers if isinstance(headers, Mapping) else {}
    try:
        prepared = [_one_case(i, raw, base, shared) for i, raw in enumerate(cases)]
    except CaseError as error:
        return {"ok": False, "error": str(error)}

    allowed = frozenset(str(h).lower().rstrip(".") for h in allowed_private if h)
    allowed_origins = frozenset((str(o[0]).lower(), str(o[1]).lower().rstrip("."), int(o[2]))
                                for o in origins if o)
    # THE TURN'S CLOCK: no case is sent after `deadline_s`, and none waits past it.
    ends = time.monotonic() + float(deadline_s) if deadline_s else None
    results: list[dict[str, Any]] = []
    for case in prepared:
        left = (ends - time.monotonic()) if ends is not None else timeout
        if left <= 0:
            results.append({"name": case["name"], "method": case["method"], "url": case["url"],
                            "status": None, "ms": 0, "ok": False,
                            "failures": ["not sent: out of time for this turn"],
                            "error": None, "body_excerpt": ""})
            continue
        answer = _send(case, max(1.0, min(float(timeout), left)), allowed, project_only,
                       allowed_origins, frozenset(str(h).lower() for h in secret_headers))
        failures = _judge(case, answer)
        results.append({
            "name": case["name"], "method": case["method"], "url": case["url"],
            "status": answer["status"], "ms": answer["ms"], "ok": not failures,
            "failures": failures, "error": answer["error"],
            "body_excerpt": answer["body"][:BODY_EXCERPT],
        })
    failed = sum(1 for r in results if not r["ok"])
    return {
        "ok": True,
        "total": len(results), "passed": len(results) - failed, "failed": failed,
        "cases": results,
        "results_table": results_table(results),
        "failed_cases": [r["name"] for r in results if not r["ok"]],
    }


def project_deployment_hosts(db: Any, project_id: str) -> frozenset[str]:
    """The hosts this project deploys to: its deployment targets' domains and refs, and its
    registered deployment hosts. The only private hosts a contract test may reach."""
    from sqlalchemy import text

    hosts: set[str] = set()

    def add(value: Any) -> None:
        raw = str(value or "").strip()
        if not raw:
            return
        parsed = urllib.parse.urlsplit(raw if "://" in raw else f"http://{raw}")
        if parsed.hostname:
            hosts.add(parsed.hostname.lower().rstrip("."))

    for queries in (
        ("SELECT domains, target_ref FROM deployment_targets WHERE project_id = :p", True),
        ("SELECT address FROM deployment_hosts WHERE project_id = :p", False),
    ):
        sql, is_target = queries
        try:
            with db.begin_nested():
                rows = db.execute(text(sql), {"p": project_id}).all()
        except Exception:  # noqa: BLE001 — a table this install lacks adds nothing
            continue
        for row in rows:
            if is_target:
                domains = row[0] if isinstance(row[0], list) else []
                for d in domains:
                    add(d.get("host") or d.get("domain") if isinstance(d, dict) else d)
                add(row[1])
            else:
                add(row[0])
    # The guard's own refusals still win: a metadata or link-local host never becomes allowed.
    return frozenset(h for h in hosts if h not in _blocked_hosts())


def project_origins(db: Any, project_id: str) -> frozenset[tuple[str, str, int]]:
    """COMMUNITY'S ALLOW-LIST (BOSSAPP-1166): the exact (scheme, host, port) of the URLs of this
    project's deployment targets that a PERSON created — health checks, domains, public URL.
    A target an agent created adds nothing: an agent that could add a target could otherwise
    point a contract test anywhere. A bare domain allows https:443 and http:80 on that name;
    a registered SSH host has no port, so it is not an origin."""
    from sqlalchemy import text

    from .agent_identity import is_person

    try:
        with db.begin_nested():
            rows = db.execute(text(
                "SELECT domains, health_checks, public_url, created_by FROM deployment_targets "
                "WHERE project_id = :p"), {"p": project_id}).mappings().all()
    except Exception:  # noqa: BLE001 — a schema without the columns allows nothing
        return frozenset()
    out: set[tuple[str, str, int]] = set()
    for row in rows:
        if not is_person(str(row["created_by"] or ""), db, project_id):
            continue
        urls: list[str] = []
        for d in (row["domains"] if isinstance(row["domains"], list) else []):
            urls.append(str((d.get("url") or d.get("host") or d.get("domain"))
                            if isinstance(d, dict) else d or ""))
        for h in (row["health_checks"] if isinstance(row["health_checks"], list) else []):
            urls.append(str(h.get("url") or "") if isinstance(h, dict) else str(h or ""))
        urls.append(str(row["public_url"] or ""))
        for raw in urls:
            raw = raw.strip()
            if not raw:
                continue
            if "://" in raw:
                origin = origin_of(raw)
                if origin:
                    out.add(origin)
            else:
                host = raw.split("/", 1)[0].lower().rstrip(".")
                if host and ":" not in host:
                    out.update({("https", host, 443), ("http", host, 80)})
    return frozenset(o for o in out if o[1] not in _blocked_hosts())


__all__ = ["CaseError", "HeaderSecretRefused", "http_auth_problem", "resolve_header_secrets", "HostRefused", "check_host", "origin_of", "project_deployment_hosts", "project_origins", "MAX_CASES", "METHODS", "json_path", "results_table", "run_cases"]


# ── the project's own contract tests, from its repository ─────────────────────────────────────

#: Where a repository declares its contract tests ("Contract tests after staging" reads it).
REPO_FILE = ".boss/contract_tests.yaml"


def from_repo(text: Optional[str]) -> dict[str, Any]:
    """The cases a repository declares in ``REPO_FILE``: ``{cases, headers?}`` (``base_url`` is
    the staging target's, never the file's). Raises CaseError when the file is not a
    ``cases:`` list, said without its content (a header may hold a token)."""
    import yaml

    if not text:
        raise CaseError(f"The repository declares no contract tests ({REPO_FILE}).")
    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError:
        raise CaseError(f"{REPO_FILE} does not parse as YAML.") from None
    cases = data.get("cases") if isinstance(data, Mapping) else None
    if not isinstance(cases, list) or not cases:
        raise CaseError(f"{REPO_FILE} needs a non-empty `cases:` list.")
    out: dict[str, Any] = {"cases": cases[:MAX_CASES]}
    if isinstance(data.get("headers"), Mapping):
        out["headers"] = dict(data["headers"])
    return out
