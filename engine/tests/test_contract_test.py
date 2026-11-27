"""A contract-test step: HTTP cases with a method, headers, a body, an expected status and
JSON-path assertions, reported as a table; one failed case fails the step (BOSSAPP-1143).
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

from .onboarding_workflow_fixtures import add_deployment_host, eng, project  # noqa: F401

#: The local test gateway stands in for the project's own staging deployment.
LOCAL = frozenset({"127.0.0.1"})


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
        elif self.path.startswith("/to-private"):
            self.send_response(302)
            self.send_header("Location", "http://10.0.0.7/internal")
            self.end_headers()
        elif self.path.startswith("/to-metadata"):
            self.send_response(302)
            self.send_header("Location", "http://169.254.169.254/latest/meta-data/")
            self.end_headers()
        elif self.path.startswith("/to-other-origin"):
            self.send_response(302)
            self.send_header("Location", f"http://localhost:{self.server.server_port}/echo")
            self.end_headers()
        elif self.path.startswith("/to-same-origin"):
            self.send_response(302)
            self.send_header("Location", "/echo")
            self.end_headers()
        elif self.path.startswith("/echo"):
            self._send(200, json.dumps({k.lower(): v for k, v in self.headers.items()}).encode())
        elif self.path.startswith("/to-health"):
            self.send_response(302)
            self.send_header("Location", "/healthz")
            self.end_headers()
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
    ], base_url=gateway, headers={"X-Request-Id": "contract"}, allowed_private=LOCAL)
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
    ], timeout_s=2, allowed_private=LOCAL)
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
    out = ct.run_cases(json.dumps([{"url": gateway + "/healthz"}]), allowed_private=LOCAL)
    assert out["ok"] and out["passed"] == 1


# ── where it may send (SSRF) ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("url,said", [
    ("http://169.254.169.254/latest/meta-data/", "metadata or link-local"),
    ("http://metadata.google.internal/computeMetadata/v1/", "metadata or link-local"),
    ("http://[fe80::1]/", "metadata or link-local"),
    ("http://127.0.0.1:9/", "loopback or private"),
    ("http://localhost:9/", "loopback or private"),
    ("http://10.1.2.3/admin", "loopback or private"),
    ("http://192.168.0.10/", "loopback or private"),
    ("http://[::1]/", "loopback or private"),
])
def test_private_and_metadata_hosts_are_refused_before_any_request(url, said):
    out = ct.run_cases([{"name": "probe", "method": "POST", "url": url, "json": {"x": 1}}])
    assert out["ok"] and out["failed"] == 1
    case = out["cases"][0]
    assert case["status"] is None and case["ms"] == 0, "nothing was sent"
    assert said in case["failures"][0], case


def test_metadata_stays_refused_even_if_a_project_names_it():
    with pytest.raises(ct.HostRefused):
        ct.check_host("http://169.254.169.254/", frozenset({"169.254.169.254"}))


def test_the_projects_own_deployment_host_is_allowed(gateway):
    assert ct.run_cases([{"url": gateway + "/healthz"}],
                        allowed_private=LOCAL)["passed"] == 1
    assert ct.run_cases([{"url": gateway + "/healthz"}])["failed"] == 1


@pytest.mark.parametrize("path,said", [("/to-private", "10.0.0.7"),
                                       ("/to-metadata", "169.254.169.254")])
def test_every_redirect_hop_is_checked(gateway, path, said):
    out = ct.run_cases([{"url": gateway + path, "expect_status": 200}], allowed_private=LOCAL)
    case = out["cases"][0]
    assert out["failed"] == 1 and case["status"] is None
    assert case["failures"][0].startswith("refused a redirect") and said in case["failures"][0]


def test_a_redirect_within_the_allowed_host_is_followed(gateway):
    out = ct.run_cases([{"url": gateway + "/to-health", "expect_json": {"$.status": "ok"}}],
                       allowed_private=LOCAL)
    assert out["passed"] == 1, out["cases"]


def test_no_case_is_sent_after_the_turns_deadline(gateway):
    out = ct.run_cases([{"url": gateway + "/healthz"}] * 3, allowed_private=LOCAL,
                       deadline_s=-1)  # a turn whose time is already spent
    assert out["failed"] == 3
    assert all(c["failures"] == ["not sent: out of time for this turn"] for c in out["cases"])


# ── the tool and the capability ───────────────────────────────────────────────────────────

def test_the_tool_reaches_only_the_projects_own_deployments(gateway, eng, project):  # noqa: F811
    from boss_engine.config import get_settings
    from boss_engine.kernel.idempotency import declared
    from boss_engine.tools.builtin import build_default_registry

    tool = {s.name: s for s in build_default_registry().specs()}["contract_test"]
    assert tool.permission == "observability:read"
    # It can POST or DELETE: a resumed run never re-sends it on its own.
    assert tool.idempotency == "irreversible" and declared("contract_test") == "irreversible"

    class C:
        def __init__(self, edition):
            self.settings = get_settings().model_copy(update={"boss_edition": edition})

        def begin(self):
            return eng.begin()

    args = {"cases": [{"path": "/healthz", "expect_status": 200}], "base_url": gateway}
    before = tool.handler(C("enterprise"), project_id=project, actor="a", args=args)
    assert before["ok"] and before["failed"] == 1, "127.0.0.1 is not this project's yet"
    assert "loopback or private" in before["cases"][0]["failures"][0]
    with eng.begin() as c:
        add_deployment_host(c, project, "127.0.0.1")
    out = tool.handler(C("enterprise"), project_id=project, actor="a", args=args)
    assert out["ok"] and out["passed"] == 1



def _target(c, project_id, health_url, created_by):
    import uuid

    from sqlalchemy import text

    c.execute(text(
        "INSERT INTO deployment_targets (id, project_id, app_name, repo, environment, "
        "target_kind, target_ref, domains, health_checks, created_by) VALUES (:i, :p, :a, "
        "'r', 'staging', 'config_dir', 'local:/srv/x', CAST(:d AS JSONB), CAST(:h AS JSONB), :by)"),
        {"i": f"deptgt_{uuid.uuid4().hex[:10]}", "p": project_id,
         "a": f"gw-{uuid.uuid4().hex[:6]}", "d": json.dumps([health_url]),
         "h": json.dumps([{"url": health_url}]), "by": created_by})


def test_contract_test_reaches_project_targets_identically_across_editions(gateway, eng, project):  # noqa: F811
    """BOSSAPP-1460: contract_test enforces the exact same host rules across all editions."""
    from boss_engine.config import get_settings
    from boss_engine.tools.builtin import build_default_registry

    tool = {s.name: s for s in build_default_registry().specs()}["contract_test"]

    class C:
        def __init__(self, edition):
            self.settings = get_settings().model_copy(update={"boss_edition": edition})

        def begin(self):
            return eng.begin()

    with eng.begin() as c:
        add_deployment_host(c, project, "127.0.0.1")
    args = {"cases": [{"path": "/healthz", "expect_status": 200}], "base_url": gateway}
    for edition in ("community", "enterprise_self_hosted"):
        out = tool.handler(C(edition), project_id=project, actor="a", args=args)
        assert out["ok"] and out["passed"] == 1, (edition, out)
        refused = tool.handler(C(edition), project_id=project, actor="a",
                               args={"cases": [{"url": "http://169.254.169.254/latest/meta-data/"}]})
        assert refused["ok"] and refused["failed"] == 1
        assert "metadata" in refused["cases"][0]["failures"][0]
        assert refused["cases"][0]["ms"] == 0, "nothing was sent"


def test_project_only_matches_scheme_host_and_port_on_every_hop(gateway):
    port = int(gateway.rsplit(":", 1)[1])
    origins = frozenset({("http", "127.0.0.1", port)})
    assert ct.check_host(gateway + "/x", project_only=True, origins=origins) == ["127.0.0.1"]
    for url in ("http://127.0.0.1:1/", "http://8.8.8.8/", f"https://127.0.0.1:{port}/"):
        with pytest.raises(ct.HostRefused, match="project-only"):
            ct.check_host(url, project_only=True, origins=origins)
    with pytest.raises(ct.HostRefused, match="metadata"):
        ct.check_host("http://169.254.169.254/", project_only=True,
                      origins=frozenset({("http", "169.254.169.254", 80)}))
    # A redirect off the origin is refused like a first request.
    out = ct.run_cases([{"url": gateway + "/to-private"}], project_only=True, origins=origins)
    assert out["cases"][0]["failures"][0].startswith("refused a redirect")
    out = ct.run_cases([{"url": gateway + "/to-health", "expect_json": {"$.status": "ok"}}],
                       project_only=True, origins=origins)
    assert out["passed"] == 1, out["cases"]


def test_dns_is_resolved_once_and_the_checked_address_is_the_one_connected(gateway,
                                                                          monkeypatch):
    """DNS rebinding: the name answers 127.0.0.1 when checked and something else after. The
    request connects to the address that was checked, never to a second lookup's answer."""
    from boss_engine.identity import http as safe_http

    port = int(gateway.rsplit(":", 1)[1])
    answers = iter([["127.0.0.1"], ["169.254.169.254"], ["169.254.169.254"]])
    lookups: list = []
    monkeypatch.setattr(safe_http, "_resolve",
                        lambda host, p: lookups.append(host) or next(answers))
    connected: list = []
    real_open = safe_http._open_connection
    monkeypatch.setattr(safe_http, "_open_connection",
                        lambda scheme, host, ip, p, t: connected.append(ip) or
                        real_open(scheme, host, ip, p, t))
    origins = frozenset({("http", "rebind.test", port)})
    out = ct.run_cases([{"url": f"http://rebind.test:{port}/healthz"}], project_only=True,
                       origins=origins)
    assert out["passed"] == 1, out["cases"]
    assert lookups == ["rebind.test"] and connected == ["127.0.0.1"]
    # A name that resolves to the metadata address is refused even when it is an origin.
    out = ct.run_cases([{"url": f"http://rebind.test:{port}/healthz"}], project_only=True,
                       origins=origins)
    assert "metadata" in out["cases"][0]["failures"][0]


def test_a_case_keeps_its_own_host_header(gateway):
    out = ct.run_cases([{"url": gateway + "/healthz", "headers": {"Host": "lumen.demo"}}],
                       allowed_private=LOCAL)
    assert out["passed"] == 1, out["cases"]


def test_the_capability_is_catalogued_and_judged():
    cap = caps.get("contract_test")
    assert cap.tool == "contract_test" and cap.risk_tier == 0 and not cap.is_async
    # `cases` optional: without them the step runs the repository's own contract tests
    # (.boss/contract_tests.yaml — "Contract tests after staging").
    assert cap.required_inputs == () and "cases" in cap.optional_inputs
    assert set(cap.produces) >= {"passed", "failed", "total", "results_table"}
    assert caps.approval_mode_for("contract_test", "auto") == "auto"
    assert outcomes.VERDICTS["contract_test"] is outcomes.contract_test
    assert "contract_test" in outcomes.FAILURE_SHAPES
    # No counts at all is not a pass.
    assert outcomes.judge("contract_test", {"ok": True}, {}).outcome == outcomes.FAILED
    assert outcomes.judge("contract_test", {"ok": True, "total": 0, "failed": 0},
                          {}).outcome == outcomes.FAILED


def test_credentials_do_not_follow_a_redirect_to_another_origin(gateway):
    """Review of #125: a case's token, cookie or key stays with the origin it was written for."""
    secret = {"Authorization": "Bearer good", "Cookie": "s=1", "X-Api-Key": "k",
              "X-Trace": "keep"}
    allowed = frozenset({"127.0.0.1", "localhost"})
    out = ct.run_cases([{"url": gateway + "/to-other-origin", "headers": secret}],
                       allowed_private=allowed)
    seen = json.loads(out["cases"][0]["body_excerpt"])
    assert "authorization" not in seen and "cookie" not in seen and "x-api-key" not in seen
    assert seen["x-trace"] == "keep"
    out = ct.run_cases([{"url": gateway + "/to-same-origin", "headers": secret}],
                       allowed_private=allowed)
    assert json.loads(out["cases"][0]["body_excerpt"])["authorization"] == "Bearer good"


def test_integration_validator_runs_read_only_probes_and_refuses_mutating_or_foreign_secret(
    eng, project, gateway, monkeypatch,  # noqa: F811
):
    """BOSSAPP-1411: integration_validator holds observability:read (runs http_probe and
    GET/HEAD/OPTIONS contract_test directly), while mutating methods require observability:manage,
    mutating production calls require deployment:production, and secret_ref is project-scoped."""
    from types import SimpleNamespace

    from sqlalchemy import text

    from boss_engine.config import get_settings
    from boss_engine.identity import authz
    from boss_engine.identity.authz import Decision, DenyReason
    from boss_engine.identity.principal import (
        AuthContext,
        AuthMethod,
        Principal,
        PrincipalKind,
        PrincipalStatus,
    )
    from boss_engine.tools.builtin import build_default_registry

    settings = get_settings()
    monkeypatch.setattr(settings, "authz_mode", "enforce")
    monkeypatch.setattr(settings, "authz_router_modes", "")

    validator = AuthContext(
        Principal("agt_integration_validator", PrincipalKind.AGENT, "org_1",
                  PrincipalStatus.ACTIVE, "integration_validator"),
        AuthMethod.RUN_TOKEN,
    )

    def _allow_read_only(_ctx, permission, scope, **_kwargs):
        if permission == "observability:read":
            return Decision.allow(permission, scope)
        return Decision.deny(permission, scope, DenyReason.MISSING_PERMISSION)

    monkeypatch.setattr(authz, "check", _allow_read_only)

    with eng.begin() as c:
        add_deployment_host(c, project, "127.0.0.1")

    reg = build_default_registry()
    assert reg.get("http_probe").permission == "observability:read"
    assert reg.get("contract_test").permission == "observability:read"

    ctx = SimpleNamespace(settings=settings, begin=lambda: eng.begin())

    probe_out = reg.invoke(
        "http_probe", ctx, project_id=project, actor="integration_validator",
        args={"url": gateway + "/healthz"}, auth=validator,
    )
    assert probe_out["ok"] is True and probe_out["status"] == 200

    ro_out = reg.invoke(
        "contract_test", ctx, project_id=project, actor="integration_validator",
        args={"cases": [{"path": "/healthz", "method": "GET", "expect_status": 200}],
              "base_url": gateway},
        auth=validator,
    )
    assert ro_out["ok"] is True and ro_out["passed"] == 1

    # Mutating method must be refused BEFORE any HTTP request fires.
    def _no_http(*_args, **_kwargs):
        raise AssertionError("mutating contract_test should have been refused before network")

    monkeypatch.setattr(ct, "run_cases", _no_http)
    mut_out = reg.invoke(
        "contract_test", ctx, project_id=project, actor="integration_validator",
        args={"cases": [{"path": "/v1/orders", "method": "POST", "json": {"x": 1}}],
              "base_url": gateway},
        auth=validator,
    )
    assert mut_out["ok"] is False
    assert mut_out.get("permission") == "observability:manage"

    # Even with observability:manage, mutating against a production target requires deployment:production.
    def _allow_manage_not_prod(_ctx, permission, scope, **_kwargs):
        if permission in {"observability:read", "observability:manage"}:
            return Decision.allow(permission, scope)
        return Decision.deny(permission, scope, DenyReason.MISSING_PERMISSION)

    monkeypatch.setattr(authz, "check", _allow_manage_not_prod)
    with eng.begin() as c:
        c.execute(
            text("INSERT INTO deployment_targets "
                 "(id, project_id, app_name, repo, environment, target_kind, target_ref, "
                 "public_url, status, created_by) VALUES "
                 "(:id, :p, 'prod-api', 'ex/api', 'production', 'ssh', '127.0.0.1', "
                 ":u, 'active', 'test')"),
            {"id": f"deptgt_prod_{project}", "p": project, "u": gateway},
        )
    prod_out = reg.invoke(
        "contract_test", ctx, project_id=project, actor="integration_validator",
        args={"cases": [{"path": "/v1/orders", "method": "POST", "json": {"x": 1}}],
              "base_url": gateway},
        auth=validator,
    )
    assert prod_out["ok"] is False
    assert prod_out.get("permission") == "deployment:production"

    # Foreign/wrong-purpose secret_ref is rejected by project scope before network.
    with eng.begin() as c:
        ws = c.execute(text("SELECT workspace_id FROM projects WHERE id = :p"), {"p": project}).scalar_one()
        other_project = f"p_other_{project}"
        c.execute(
            text("INSERT INTO projects (id, workspace_id, name, slug) VALUES (:i, :w, 'Other', :i)"),
            {"i": other_project, "w": ws},
        )
        c.execute(
            text("INSERT INTO secret_refs "
                 "(id, project_id, scope_type, scope_id, ref_kind, ref_key, "
                 "secret_kind, storage_backend, metadata, created_by) "
                 "VALUES (:id, :p, 'project', :p, 'env', 'NOPE', 'api_token', "
                 "'environment', '{\"http_auth\": true}'::jsonb, 'test')"),
            {"id": f"sec_foreign_{project}", "p": other_project},
        )
        problem = ct.http_auth_problem(c, f"sec_foreign_{project}", project)
        assert problem is not None and "does not exist" in problem


def _contract_tool_with(engine, monkeypatch, allowed_permissions):
    """The registry's contract_test, invoked as integration_validator holding exactly
    `allowed_permissions` (authz enforced)."""
    from types import SimpleNamespace

    from boss_engine.config import get_settings
    from boss_engine.identity import authz
    from boss_engine.identity.authz import Decision, DenyReason
    from boss_engine.identity.principal import (
        AuthContext,
        AuthMethod,
        Principal,
        PrincipalKind,
        PrincipalStatus,
    )
    from boss_engine.tools.builtin import build_default_registry

    settings = get_settings()
    monkeypatch.setattr(settings, "authz_mode", "enforce")
    monkeypatch.setattr(settings, "authz_router_modes", "")
    validator = AuthContext(
        Principal("agt_integration_validator", PrincipalKind.AGENT, "org_1",
                  PrincipalStatus.ACTIVE, "integration_validator"),
        AuthMethod.RUN_TOKEN,
    )

    def _check(_ctx, permission, scope, **_kwargs):
        if permission in allowed_permissions:
            return Decision.allow(permission, scope)
        return Decision.deny(permission, scope, DenyReason.MISSING_PERMISSION)

    monkeypatch.setattr(authz, "check", _check)
    reg = build_default_registry()
    ctx = SimpleNamespace(settings=settings, begin=lambda: engine.begin())

    def call(project_id, args):
        return reg.invoke("contract_test", ctx, project_id=project_id,
                          actor="integration_validator", args=args, auth=validator)

    return call


def test_cases_given_as_a_json_string_get_the_same_checks(eng, project, gateway, monkeypatch):  # noqa: F811
    """BOSSAPP-1411 (gate review): `cases` as a JSON string (run_cases accepts it) runs, and a
    mutating case in it is still refused without observability:manage — no NameError, no way
    around the method check by stringifying. Header secret references inside string cases are
    resolved too (an empty one is refused, never sent as text)."""
    with eng.begin() as c:
        add_deployment_host(c, project, "127.0.0.1")
    call = _contract_tool_with(eng, monkeypatch, {"observability:read"})

    ro = call(project, {"cases": json.dumps([{"path": "/healthz", "expect_status": 200}]),
                        "base_url": gateway})
    assert ro["ok"] is True and ro["passed"] == 1

    empty_ref = call(project, {"cases": json.dumps([{"path": "/echo", "headers": {
        "Authorization": {"secret_ref": ""}}}]), "base_url": gateway})
    assert empty_ref["ok"] is False and "secret_ref is empty" in empty_ref["error"]

    def _no_http(*_args, **_kwargs):
        raise AssertionError("a refused case must not reach the network")

    monkeypatch.setattr(ct, "run_cases", _no_http)
    mut = call(project, {"cases": json.dumps([{"path": "/v1/x", "method": "post"}]),
                         "base_url": gateway})
    assert mut["ok"] is False and mut.get("permission") == "observability:manage"
    assert "NameError" not in str(mut)


def test_a_write_to_a_production_host_on_another_port_or_scheme_is_production(
    eng, project, gateway, dead_port, monkeypatch,  # noqa: F811
):
    """BOSSAPP-1411 (gate review): a production target named by its public_url is matched by
    HOST — a mutating case sent to that host on another port, or over the other scheme, still
    needs deployment:production."""
    from sqlalchemy import text

    host = "prod-only.example.invalid"
    with eng.begin() as c:
        c.execute(
            text("INSERT INTO deployment_targets "
                 "(id, project_id, app_name, repo, environment, target_kind, target_ref, "
                 "public_url, status, created_by) VALUES "
                 "(:id, :p, 'prod-api', 'ex/api', 'production', 'ssh', '', "
                 ":u, 'active', 'test')"),
            {"id": f"deptgt_prodalt_{project}", "p": project, "u": f"https://{host}"},
        )
    call = _contract_tool_with(eng, monkeypatch, {"observability:read", "observability:manage"})

    def _no_http(*_args, **_kwargs):
        raise AssertionError("a production write without deployment:production was sent")

    monkeypatch.setattr(ct, "run_cases", _no_http)
    for url in (f"https://{host}:8443/v1/x", f"http://{host}/v1/x",
                f"http://{host}:{dead_port}/v1/x", f"https://{host}/v1/x"):
        out = call(project, {"cases": [{"url": url, "method": "POST"}]})
        assert out["ok"] is False, url
        assert out.get("permission") == "deployment:production", (url, out)
    with eng.begin() as c:
        c.execute(text("DELETE FROM deployment_targets WHERE id = :i"),
                  {"i": f"deptgt_prodalt_{project}"})
