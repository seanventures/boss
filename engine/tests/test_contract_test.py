"""A contract-test step: HTTP cases with a method, headers, a body, an expected status and
JSON-path assertions, reported as a table; one failed case fails the step (AMEX-G7,
BOSSAPP-1143).
"""
from __future__ import annotations

import http.server
import json
import socket
import threading

import pytest

from boss_engine.services import contract_tests as ct
from boss_engine.services import workflow_capabilities as caps
from boss_engine.services import workflow_outcomes as outcomes


class Gateway(http.server.BaseHTTPRequestHandler):
    """A tiny API with a contract: a token reaches /v1/accounts, no token is a 401, POST echoes
    JSON, /admin is 404, /healthz is 200 with a JSON body."""

    def _send(self, status, body=b"", content_type="application/json"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_GET(self):  # noqa: N802 - the stdlib's name
        if self.path == "/healthz":
            self._send(200, json.dumps({"status": "ok", "tenants": ["lumen-pay"],
                                        "upstreams": [{"name": "pay", "healthy": True}]}).encode())
        elif self.path.startswith("/v1/accounts"):
            if self.headers.get("Authorization") == "Bearer good":
                self._send(200, b'{"accounts": []}')
            else:
                self._send(401, b'{"error": "unauthenticated"}')
        elif self.path.startswith("/admin"):
            self._send(404, b"not found", "text/plain")
        else:
            self._send(503, b"down", "text/plain")

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        self._send(201, json.dumps({"echo": json.loads(raw or b"null"),
                                    "content_type": self.headers.get("Content-Type")}).encode())

    def log_message(self, *_a):
        pass


@pytest.fixture(scope="module")
def gateway():
    server = http.server.HTTPServer(("127.0.0.1", 0), Gateway)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


@pytest.fixture(scope="module")
def dead_port():
    closed = socket.socket()
    closed.bind(("127.0.0.1", 0))
    port = closed.getsockname()[1]
    closed.close()
    return port


# ── json paths ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("path,expected", [
    ("$.status", (True, "ok")),
    ("status", (True, "ok")),
    ("$.tenants[0]", (True, "lumen-pay")),
    ("tenants.0", (True, "lumen-pay")),
    ("$.upstreams[0].healthy", (True, True)),
    ("$.upstreams[3].healthy", (False, None)),
    ("$.missing", (False, None)),
    ("$", (True, {"status": "ok", "tenants": ["lumen-pay"], "upstreams": [{"name": "pay", "healthy": True}]})),
])
def test_json_paths(path, expected):
    doc = {"status": "ok", "tenants": ["lumen-pay"], "upstreams": [{"name": "pay", "healthy": True}]}
    assert ct.json_path(doc, path) == expected


# ── cases ──────────────────────────────────────────────────────────────────────────────────

def test_a_passing_contract(gateway):
    out = ct.run_cases([
        {"name": "health", "path": "/healthz", "expect_status": 200,
         "expect_json": {"$.status": "ok", "$.upstreams[0].healthy": True},
         "expect_json_exists": ["$.tenants"]},
        {"name": "authed read", "method": "GET", "path": "/v1/accounts",
         "headers": {"Authorization": "Bearer good"}, "expect_status": 200},
        {"name": "no token is 401", "path": "/v1/accounts", "expect_status": 401,
         "expect_json": {"$.error": "unauthenticated"}},
        {"name": "admin stays hidden", "path": "/admin/users", "expect_status": [404, 403]},
        {"name": "post echoes json", "method": "POST", "path": "/v1/accounts",
         "json": {"tenant": "lumen-pay"}, "expect_status": 201,
         "expect_json": {"$.echo.tenant": "lumen-pay",
                         "$.content_type": "application/json"}},
    ], base_url=gateway, headers={"X-Request-Id": "contract"})
    assert out["ok"] and out["failed"] == 0 and out["passed"] == 5 and out["total"] == 5
    assert out["failed_cases"] == []
    assert all(c["ok"] for c in out["cases"])
    assert out["cases"][1]["url"] == gateway + "/v1/accounts"
    assert out["results_table"].startswith("| Case | Request | Status | Time | Result |")
    assert "| health |" in out["results_table"] and "| pass |" in out["results_table"]
    assert outcomes.judge("contract_test", out, {}).outcome == outcomes.SUCCEEDED


def test_a_failing_case_fails_the_contract_and_names_why(gateway, dead_port):
    out = ct.run_cases([
        {"name": "health", "url": gateway + "/healthz", "expect_status": 200,
         "expect_json": {"$.status": "degraded", "$.tenants[0]": "lumen-pay"}},
        {"name": "wrong status", "url": gateway + "/v1/accounts", "expect_status": 200},
        {"name": "default is 2xx", "url": gateway + "/nothing"},
        {"name": "unreachable", "url": f"http://127.0.0.1:{dead_port}/x", "expect_status": 200},
        {"name": "body text", "url": gateway + "/admin", "expect_status": 404,
         "expect_body_contains": "forbidden"},
        {"name": "not json", "url": gateway + "/admin", "expect_status": 404,
         "expect_json": {"$.a": 1}},
        {"name": "fine", "url": gateway + "/healthz"},
    ], timeout_s=2)
    assert out["ok"] is True, "the tool RAN; the contract did not hold"
    assert out["total"] == 7 and out["failed"] == 6 and out["passed"] == 1
    by_name = {c["name"]: c for c in out["cases"]}
    assert by_name["health"]["failures"] == ['$.status: got "ok", expected "degraded"']
    assert by_name["wrong status"]["failures"] == ["answered 401, expected 200"]
    assert by_name["default is 2xx"]["failures"] == ["answered 503, expected a 2xx"]
    assert by_name["unreachable"]["status"] is None
    assert by_name["unreachable"]["failures"][0].startswith("did not answer: ")
    assert by_name["body text"]["failures"] == ["body does not contain 'forbidden'"]
    assert by_name["not json"]["failures"] == ["body is not JSON"]
    assert by_name["fine"]["ok"] is True
    assert "| FAIL — answered 401, expected 200 |" in out["results_table"]
    verdict = outcomes.judge("contract_test", out, {})
    assert verdict.outcome == outcomes.FAILED
    assert verdict.reason.startswith("6 of 7 contract case(s) failed: health, wrong status")
    assert verdict.produced["results_table"] == out["results_table"]
    applied = outcomes.apply("contract_test", dict(out), {})
    assert applied["status"] == "failed" and applied["error"] == verdict.reason
    assert applied["failed_cases"] == out["failed_cases"]


@pytest.mark.parametrize("cases,base,said", [
    ([], "", "non-empty list"),
    ("not json", "", "list of case objects"),
    ([{"path": "/x"}], "", "no base_url"),
    ([{"url": "ftp://x"}], "", "http:// or https://"),
    ([{"url": "http://x", "method": "BREW"}], "", "method 'BREW'"),
    ([{"url": "http://x", "expect_status": "teapot"}], "", "not an HTTP status"),
    ([{"url": "http://x", "expect_json": [1]}], "", "expect_json must be an object"),
    (["x"], "", "not an object"),
    ([{"path": "/x"}], "gopher://x", "base_url must start"),
    ([{"url": "http://x"}] * 101, "", "at most 100"),
])
def test_cases_that_cannot_run_are_refused_before_any_request(cases, base, said):
    out = ct.run_cases(cases, base_url=base)
    assert out["ok"] is False and said in out["error"], out
    assert outcomes.judge("contract_test", out, {}).outcome == outcomes.FAILED


def test_cases_may_arrive_as_a_json_string(gateway):
    """A workflow input or a chat argument may carry the list as text."""
    out = ct.run_cases(json.dumps([{"url": gateway + "/healthz"}]))
    assert out["ok"] and out["passed"] == 1


# ── the tool and the capability ───────────────────────────────────────────────────────────

def test_the_tool_runs_the_cases_and_community_refuses(gateway):
    from boss_engine.config import get_settings
    from boss_engine.tools.builtin import build_default_registry

    tool = {s.name: s for s in build_default_registry().specs()}["contract_test"]
    assert tool.permission == "observability:manage"

    class C:
        def __init__(self, edition):
            self.settings = get_settings().model_copy(update={"boss_edition": edition})

    out = tool.handler(C("enterprise"), project_id="p", actor="a",
                       args={"cases": [{"path": "/healthz", "expect_status": 200}],
                             "base_url": gateway})
    assert out["ok"] and out["passed"] == 1
    refused = tool.handler(C("community"), project_id="p", actor="a",
                           args={"cases": [{"url": gateway + "/healthz"}]})
    assert refused["ok"] is False and "Community" in refused["error"]


def test_the_capability_is_catalogued_and_judged():
    cap = caps.get("contract_test")
    assert cap.tool == "contract_test" and cap.risk_tier == 0 and not cap.is_async
    assert cap.required_inputs == ("cases",)
    assert set(cap.produces) >= {"passed", "failed", "total", "results_table"}
    assert caps.approval_mode_for("contract_test", "auto") == "auto"
    assert outcomes.VERDICTS["contract_test"] is outcomes.contract_test
    assert "contract_test" in outcomes.FAILURE_SHAPES
    # No counts at all is not a pass.
    assert outcomes.judge("contract_test", {"ok": True}, {}).outcome == outcomes.FAILED
    assert outcomes.judge("contract_test", {"ok": True, "total": 0, "failed": 0},
                          {}).outcome == outcomes.FAILED
