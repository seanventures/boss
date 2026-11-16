"""A service's preview is up when its health route says so, and a service or library build never
fails its run at Preview (BOSSAPP-1308, AR-18).

THE INCIDENT (wfr_51eb3963270f4084ad63, northgate-gateway, 2026-11-16). The build of a Go API
gateway skipped its auto-preview as a "service/library build". The workflow's Preview step then
started one anyway; the gateway bound, answered 404 at `/` — correctly, a gateway routes only what
its config names — and 200 at /healthz, which its boss.deploy.yaml declares as
`{target.public_url}/healthz`. The preview read that declaration as no path at all (the
placeholder held a `{`), asked `/`, and the run failed: "It serves no page at the site root".

Now: a service is probed at its declared health/ready path, then /healthz, then /readyz, and a
2xx there is up; a service or library build whose preview does not come up records
"preview skipped (service)" on the step and the run goes on. An APP's failed preview still fails.
"""
from __future__ import annotations

import http.server
import json
import threading
import uuid

import pytest
from sqlalchemy import text

from boss_engine.services import preview_service as ps
from boss_engine.services import workflow_capability_runner as runner
from boss_engine.services.workflow_capabilities import CAPABILITIES

from .test_workflow_capability_runner import (  # noqa: F401 - fixtures used by name
    Ctx,
    RecordingRegistry,
    RecordingTool,
    _publish,
    _start,
    _steps,
    eng,
    project,
)


# ── The probe ──────────────────────────────────────────────────────────────────────────────────

def _handler(routes: dict[str, tuple[int, bytes]]):
    class _Routes(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 — BaseHTTPRequestHandler's naming
            status, body = routes.get(self.path, (404, b"404 page not found\n"))
            self.send_response(status)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            return
    return _Routes


@pytest.fixture
def serve():
    servers = []

    def start(routes):
        server = http.server.HTTPServer(("127.0.0.1", 0), _handler(routes))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        return server.server_address[1]
    yield start
    for server in servers:
        server.shutdown()


def test_a_service_with_healthz_200_and_root_404_is_up(serve, tmp_path):
    port = serve({"/healthz": (200, b"ok"), "/readyz": (503, b"")})
    log = tmp_path / "preview.log"
    log.write_text(f"northgate-gateway listening on 0.0.0.0:{port} (config dir config)\n")
    assert ps._http_ok(port, "/") is False, "the premise: nothing at the root"
    assert ps._discover_working_port(str(log), port, timeout=5.0) == port


def test_readyz_is_asked_after_healthz(serve):
    port = serve({"/readyz": (200, b"")})
    assert ps._serving(port, "/") is True


def test_a_declared_ready_path_is_asked_first(serve, tmp_path):
    port = serve({"/ready": (204, b"")})
    (tmp_path / ps.READY_DECLARATION_FILE).write_text("/ready\n")
    declared = ps.preview_health_path(tmp_path)
    assert declared == "/ready"
    assert ps.service_health_paths(declared) == ["/ready", "/healthz", "/readyz"]
    assert ps._serving(port, declared) is True


def test_a_service_with_no_health_route_is_not_up_and_says_where_it_looked(serve, tmp_path):
    port = serve({})
    assert ps._serving(port, "/") is False
    log = tmp_path / "preview.log"
    log.write_text(f"listening on {port}\n")
    said = ps._diagnose_listening_port(str(log), port)
    assert "no page at the site root" in said and "/healthz" in said and "/readyz" in said


def test_a_ui_whose_root_is_an_error_page_is_not_up_because_of_its_healthz(serve):
    """The fallback is for an ABSENT root only: a 200 error page at `/` stays a failed UI."""
    port = serve({"/": (200, b""), "/healthz": (200, b"ok")})
    assert ps._serving(port, "/") is False


def test_a_deploy_check_written_against_its_target_names_the_preview_path(tmp_path):
    (tmp_path / "boss.deploy.yaml").write_text(
        "version: 1\nname: northgate-gateway\nkind: compose\n"
        "compose:\n  file: docker-compose.yml\n  service: gateway-staging\n"
        "health:\n  - url: \"{target.public_url}/healthz\"\n    expect: 200\n")
    (tmp_path / "docker-compose.yml").write_text("services:\n  gateway-staging:\n    image: x\n")
    assert ps.preview_health_path(tmp_path) == "/healthz"


def test_the_preview_health_file_still_wins(tmp_path):
    (tmp_path / ps.HEALTH_DECLARATION_FILE).write_text("/health\n")
    (tmp_path / ps.READY_DECLARATION_FILE).write_text("/ready\n")
    assert ps.preview_health_path(tmp_path) == "/health"


# ── The workflow: a service or library never fails its run at Preview ──────────────────────────

def _build(eng, project, impl_files: list[str]) -> str:  # noqa: F811
    """A finished build, committed: the service classification reads it on its own connection."""
    crun = f"crun_{uuid.uuid4().hex[:12]}"
    with eng.begin() as c:
        c.execute(text(
            "INSERT INTO code_task_runs (id, project_id, task_id, status, phase, title, spec, "
            "state, finalize_ctx) VALUES (:id, :p, NULL, 'completed', 'done', 'KAN-9 gateway', "
            "CAST(:spec AS JSONB), CAST(:st AS JSONB), '{}'::jsonb)"),
            {"id": crun, "p": project,
             "spec": json.dumps({"owner": "local_northgate", "repo": "northgate-gateway"}),
             "st": json.dumps({"impl_files": impl_files})})
    return crun


def _preview_then_notify(c, project_id: str, build_run_id: str):
    _, version = _publish(c, project_id, f"svc_preview_{uuid.uuid4().hex[:6]}", [
        {"id": "s", "kind": "capability",
         "binding": {"capability": "preview", "inputs": {"run_id": build_run_id}},
         "next": ["after"]},
        {"id": "after", "kind": "capability",
         "binding": {"capability": "notify", "inputs": {"message": "went on"}}, "next": []},
    ], "s")
    return _start(c, project_id, version["id"], {})


def _failed_preview(c, project_id: str, preview_id: str, build_run_id: str) -> None:
    from .test_a_failed_step_stops_the_run import _insert_awaited

    _insert_awaited(c, project_id, "app_previews", preview_id, "failed")
    c.execute(text("UPDATE app_previews SET run_id = :r, failure_kind = 'no_page_at_root', "
                   "error = :e WHERE id = :i"),
              {"r": build_run_id, "i": preview_id,
               "e": "the app IS listening on port 19239, but GET / returned 404."})


def _run_status(c, run_id: str) -> str:
    return c.execute(text("SELECT status FROM workflow_runs WHERE id=:r"), {"r": run_id}).scalar()


@pytest.fixture
def builds(eng):  # noqa: F811
    made: list[str] = []
    yield made
    with eng.begin() as c:
        for crun in made:
            c.execute(text("DELETE FROM code_task_runs WHERE id = :i"), {"i": crun})


def test_a_service_builds_failed_preview_is_skipped_and_the_run_goes_on(eng, project, builds,  # noqa: F811
                                                                          monkeypatch):
    crun = _build(eng, project, ["cmd/northgate-gateway/main.go", "internal/config/watcher.go",
                                 "go.mod", "Dockerfile"])
    builds.append(crun)
    preview_id = f"prev_{uuid.uuid4().hex[:10]}"
    field = runner._AWAIT_KINDS["preview"][1][0]
    tool = RecordingTool(CAPABILITIES["preview"].tool, {field: preview_id})
    monkeypatch.setattr(runner, "_tool_registry", lambda: RecordingRegistry([tool]))
    monkeypatch.setattr(runner, "_running_preview", lambda *a, **k: None)
    with eng.begin() as c:
        run = _preview_then_notify(c, project, crun)
        runner.tick(c, Ctx(eng), project_id=project)
        assert _steps(c, run["id"])["s"]["status"] == "waiting"
        _failed_preview(c, project, preview_id, crun)
        runner.tick(c, Ctx(eng), project_id=project)
        runner.tick(c, Ctx(eng), project_id=project)
        steps = _steps(c, run["id"])
        assert steps["s"]["status"] == "succeeded", steps["s"]
        assert steps["s"]["output"]["skipped"] == "service"
        assert ps.SERVICE_PREVIEW_SKIPPED in steps["s"]["output"]["note"]
        assert steps["after"]["status"] != "pending", "the run went on past Preview"
        assert _run_status(c, run["id"]) not in {"failed", "deadlettered"}
        c.execute(text("DELETE FROM app_previews WHERE id=:i"), {"i": preview_id})


def test_a_library_without_a_runnable_preview_is_skipped_and_the_run_goes_on(eng, project, builds,  # noqa: F811
                                                                              monkeypatch):
    crun = _build(eng, project, ["slugify/__init__.py", "slugify/core.py", "pyproject.toml"])
    builds.append(crun)
    tool = RecordingTool(CAPABILITIES["preview"].tool, {
        "preview": None, "error": "preview_unavailable",
        "message": "this build declares no way to run it. Write boss.preview.command."})
    monkeypatch.setattr(runner, "_tool_registry", lambda: RecordingRegistry([tool]))
    monkeypatch.setattr(runner, "_running_preview", lambda *a, **k: None)
    with eng.begin() as c:
        run = _preview_then_notify(c, project, crun)
        runner.tick(c, Ctx(eng), project_id=project)
        runner.tick(c, Ctx(eng), project_id=project)
        steps = _steps(c, run["id"])
        assert steps["s"]["status"] == "succeeded", steps["s"]
        assert steps["s"]["output"]["skipped"] == "service"
        assert ps.SERVICE_PREVIEW_SKIPPED in steps["s"]["output"]["note"]
        assert steps["after"]["status"] != "pending", "the run went on past Preview"
        assert _run_status(c, run["id"]) not in {"failed", "deadlettered"}


def test_an_apps_failed_preview_still_stops_the_run(eng, project, builds, monkeypatch):  # noqa: F811
    crun = _build(eng, project, ["web/index.html", "web/app.jsx", "server.js"])
    builds.append(crun)
    preview_id = f"prev_{uuid.uuid4().hex[:10]}"
    field = runner._AWAIT_KINDS["preview"][1][0]
    tool = RecordingTool(CAPABILITIES["preview"].tool, {field: preview_id})
    monkeypatch.setattr(runner, "_tool_registry", lambda: RecordingRegistry([tool]))
    monkeypatch.setattr(runner, "_running_preview", lambda *a, **k: None)
    with eng.begin() as c:
        run = _preview_then_notify(c, project, crun)
        runner.tick(c, Ctx(eng), project_id=project)
        _failed_preview(c, project, preview_id, crun)
        runner.tick(c, Ctx(eng), project_id=project)
        runner.tick(c, Ctx(eng), project_id=project)
        steps = _steps(c, run["id"])
        assert steps["s"]["status"] in {"failed", "deadlettered"}, steps["s"]
        assert steps["after"]["status"] == "pending"
        c.execute(text("DELETE FROM app_previews WHERE id=:i"), {"i": preview_id})
