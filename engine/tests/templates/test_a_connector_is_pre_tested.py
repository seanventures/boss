"""A connector is pre-tested: a build that adds one checks only its wiring.

Login, Stripe and PayPal are each a fully tested path in their own test tier (the template trial,
and the reference tests here with `as_connector=False`). A build that adds one used to copy the
connector's suite into the app, freeze it, have agents work around it and run it as the
`template_tests` gate. Now the build does none of that: one smoke check proves the wiring. The
app starts (the app_serves proof) and the connector's GET routes answer on it.
"""
from __future__ import annotations

import http.server
import inspect
import threading

import pytest

from boss_engine.services import governed_code_task_service as g
from boss_engine.services import preview_service
from boss_engine.templates import gate, run_ref
from boss_engine.templates.validator import tree_from_dir, validate_tree

from ._fixtures import REPO_ROOT

LOGIN = REPO_ROOT / "templates" / "apps" / "login-oauth"


def _login():
    tree = tree_from_dir(LOGIN)
    report = validate_tree(tree, source="builtin")
    tpl = {"id": "tpl_login", "stack": "node",
           "manifest": report.manifest.model_dump(mode="json", by_alias=True)}
    return report.manifest, tpl, tree.files()


def _never_run(command, cwd):
    raise AssertionError(f"a build ran the connector's own suite: {command}")


def _gate(serve, *, tests_passed=True):
    _, tpl, files = _login()
    return gate.evaluate(template=tpl, files=files, changed={"app/server.mjs": "x"}, cwd=None,
                         run_command=_never_run, tests_passed=tests_passed, serve=serve)


def test_only_plain_get_routes_are_probed():
    manifest, _, _ = _login()
    paths = gate.probe_paths(manifest)
    assert "/auth/providers" in paths
    assert not any(":" in p or "{" in p for p in paths)


def test_the_app_started_and_a_route_answered_passes_without_running_its_suite():
    r = _gate({"ok": True, "proven": True, "route_probes": {"/auth/providers": 200}})
    assert r.gates["template_tests"] is True
    tt = r.evidence["template_tests"]
    assert tt["kind"] == "connector" and tt["answered"] == ["/auth/providers"]
    assert "own test tier" in tt["connector_tests"]


@pytest.mark.parametrize("probes", [{"/auth/providers": 404}, {"/auth/providers": 502},
                                    {"/auth/providers": None}])
def test_routes_that_do_not_answer_fail_the_wiring(probes):
    r = _gate({"ok": True, "proven": True, "route_probes": probes})
    assert r.gates["template_tests"] is False
    assert "none of the connector's routes answered" in r.evidence["template_tests"]["error"]


@pytest.mark.parametrize("probes,ok", [
    # A route that needs sign-in refusing a signed-out probe is that route working.
    ({"/auth/providers": 200, "/api/me": 401, "/api/admin": 403}, True),
    ({"/auth/providers": 302, "/api/me": 401}, True),
    # A PUBLIC route refusing is broken wiring (pre-mortem finding 13): 401 counted as answered.
    ({"/auth/providers": 401}, False),
    ({"/auth/providers": 403, "/api/me": 401}, False),
    ({"/auth/providers": 400, "/api/me": 200}, False),
])
def test_401_answers_only_for_a_route_that_needs_sign_in(probes, ok):
    r = _gate({"ok": True, "proven": True, "route_probes": probes})
    assert r.gates["template_tests"] is ok, r.evidence["template_tests"]
    assert gate.route_needs_auth("/api/me") and gate.route_needs_auth("/api/admin")
    assert not gate.route_needs_auth("/auth/providers")


def test_an_app_that_did_not_start_fails_and_red_tests_fail():
    assert _gate({"ok": False, "error": "exited 1"}).gates["template_tests"] is False
    assert _gate({"ok": True, "proven": True, "route_probes": {"/auth/providers": 200}},
                 tests_passed=False).gates["template_tests"] is False


def test_an_app_boss_does_not_start_is_left_to_its_own_tests_and_says_so():
    r = _gate({"ok": True, "proven": False, "not_a_service": "not a declared service"})
    assert r.gates["template_tests"] is True
    assert "not a declared service" in r.evidence["template_tests"]["not_run"]


def test_the_connectors_tests_are_not_placed_in_the_app(tmp_path, monkeypatch):
    stack = tmp_path / "template/stacks/node"
    (stack / "tests").mkdir(parents=True)
    (stack / "tests/login.test.mjs").write_text("connector test")
    monkeypatch.setattr(run_ref, "current", lambda: {run_ref.STACK_DIR_ENV: str(stack),
                                                     run_ref.KIND_ENV: "connector"})
    work = tmp_path / "work"
    work.mkdir()
    assert g.place_template_tests(work, "helpdesk-ai") == []
    assert not (work / "helpdesk-ai").exists()


def test_the_serve_proof_probes_the_routes_it_is_given():
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200 if self.path == "/auth/providers" else 404)
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        port = srv.server_address[1]
        assert preview_service.probe_route(port, "/auth/providers") == 200
        assert preview_service.probe_route(port, "/nope") == 404
    finally:
        srv.shutdown()
    assert "probe_paths" in inspect.signature(preview_service.prove_app_serves).parameters


def test_the_pipeline_probes_the_connector_and_hands_the_proof_to_the_gate():
    src = inspect.getsource(g)
    assert "probe_paths=_connector_probe_paths(engine, run) or None" in src
    # The first judgement, its re-run after QA's fixture repair, its re-run after a warm fix
    # round, and the secret scan's re-judge (BOSSAPP-548, which re-proves the app when green):
    # EVERY call carries the app-start proof. Read from the AST, not counted as text, so a call
    # that names its proof differently is still checked and one that drops it still fails.
    import ast

    calls = [n for n in ast.walk(ast.parse(src)) if isinstance(n, ast.Call)
             and getattr(n.func, "id", None) == "_template_gates"]
    assert len(calls) >= 4
    assert all(any(k.arg == "serve" for k in c.keywords) for c in calls), \
        [c.lineno for c in calls if not any(k.arg == "serve" for k in c.keywords)]
