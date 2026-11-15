"""Contract tests: a list of HTTP cases, each with a method, headers, a body and what it must
answer — run against a deployed thing, judged case by case, reported as a table (AMEX-G7,
BOSSAPP-1143).

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
import socket
import time
import urllib.parse
import urllib.error
import urllib.request
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


def _addresses(host: str) -> list[ipaddress._BaseAddress]:
    try:
        return [ipaddress.ip_address(host)]
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return []          # does not resolve: the request fails as "did not answer"
    out = []
    for info in infos:
        try:
            out.append(ipaddress.ip_address(info[4][0].split("%", 1)[0]))
        except ValueError:
            continue
    return out


def check_host(url: str, allowed_private: frozenset[str] = frozenset()) -> None:
    """THE SSRF GUARD, for the first request and every redirect hop.

    A contract test sends whatever method, headers and body a caller writes, and returns the
    answer — so where it may send is the whole security question. Metadata and link-local hosts
    are refused always (the shared connector rule, `connectors/tickets/http.check_host`).
    Loopback, private and otherwise non-public addresses — by literal or by what the name
    resolves to — are refused unless the host is one of `allowed_private`: the project's OWN
    deployment targets and hosts, resolved by the caller from the database, never from the
    arguments."""
    parts = urllib.parse.urlsplit(url or "")
    host = (parts.hostname or "").lower().rstrip(".")
    if not host or (parts.scheme or "").lower() not in ("http", "https"):
        raise HostRefused(f"{url!r} is not an http(s) URL.")
    addresses = _addresses(host)
    if host in _blocked_hosts() or any(a.is_link_local for a in addresses):
        raise HostRefused(f"{host} is a metadata or link-local address; a contract test never "
                          "sends there.")
    if host in allowed_private:
        return
    if any(not a.is_global for a in addresses) or host in ("localhost",):
        raise HostRefused(
            f"{host} is a loopback or private address and not one of this project's "
            "deployment targets. A contract test reaches the project's own deployments and "
            "public hosts only.")


class _CheckedRedirects(urllib.request.HTTPRedirectHandler):
    """Every redirect hop goes through the same guard as the first request."""

    def __init__(self, allowed_private: frozenset[str]):
        self.allowed_private = allowed_private

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        check_host(newurl, self.allowed_private)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


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


def _send(case: dict[str, Any], timeout_s: float,
          allowed_private: frozenset[str] = frozenset()) -> dict[str, Any]:
    request = urllib.request.Request(case["url"], data=case["body"], method=case["method"],
                                     headers=case["headers"])
    started = time.time()
    try:
        check_host(case["url"], allowed_private)
    except HostRefused as refused:
        return {"status": None, "ms": 0, "headers": {}, "body": "",
                "error": f"refused: {refused}", "refused": True}
    opener = urllib.request.build_opener(_CheckedRedirects(allowed_private))
    try:
        with opener.open(request, timeout=timeout_s) as response:  # noqa: S310 — host checked
            raw = response.read(BODY_EXCERPT * 4)
            return {"status": int(response.status), "ms": round((time.time() - started) * 1000),
                    "headers": {k.lower(): v for k, v in response.headers.items()},
                    "body": raw.decode("utf-8", errors="replace"), "error": None}
    except urllib.error.HTTPError as error:
        raw = error.read(BODY_EXCERPT * 4) if hasattr(error, "read") else b""
        return {"status": int(error.code), "ms": round((time.time() - started) * 1000),
                "headers": {k.lower(): v for k, v in (error.headers or {}).items()},
                "body": raw.decode("utf-8", errors="replace"), "error": None}
    except HostRefused as refused:
        return {"status": None, "ms": round((time.time() - started) * 1000), "headers": {},
                "body": "", "error": f"refused a redirect: {refused}", "refused": True}
    except Exception as error:  # noqa: BLE001 — unreachable is a RESULT, not a crash
        cause = getattr(error, "reason", None)
        if isinstance(cause, HostRefused):
            return {"status": None, "ms": round((time.time() - started) * 1000),
                    "headers": {}, "body": "", "error": f"refused a redirect: {cause}",
                    "refused": True}
        return {"status": None, "ms": round((time.time() - started) * 1000), "headers": {},
                "body": "", "error": f"{type(error).__name__}: {str(error)[:300]}"}


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


def run_cases(cases: Any, *, base_url: str = "", timeout_s: Any = 10,
              headers: Optional[Mapping[str, str]] = None,
              allowed_private: Iterable[str] = (),
              deadline_s: Optional[float] = None) -> dict[str, Any]:
    """Run every case and say, per case and in total, what passed.

    Returns `ok: False` with an `error` when the cases cannot be run as written (not a list, a
    bad method, a path with no base_url) — nothing is sent then. Otherwise `ok` is True and the
    verdict is in `failed`: the tool ran; whether the contract held is the step's judgement
    (workflow_outcomes.contract_test).
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
        answer = _send(case, max(1.0, min(float(timeout), left)), allowed)
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


__all__ = ["CaseError", "HostRefused", "check_host", "project_deployment_hosts", "MAX_CASES", "METHODS", "json_path", "results_table", "run_cases"]
