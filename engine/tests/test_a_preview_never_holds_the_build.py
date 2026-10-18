"""A preview never holds the build, and its readiness check fails fast on a missing route.

crun_16c5e245 (rc10.8, 2026-10-18) was written `completed` at 19:31:42; its code.task job stayed
"running" until 19:41:48 while the auto-preview, called inline after the PR, polled an app that
answered 404 at `/` every 0.8 s for 600 s — 600 because a pom.xml at the root of the shared
repository made the Node app beside it "Java". The build's settle, its follow-up task and the
work queued behind its worker slot all waited, and the preview failed anyway.
"""

from __future__ import annotations

import http.server
import inspect
import json
import threading
import time

from sqlalchemy import text

from boss_engine.services import governed_code_task_service as gcts
from boss_engine.services import preview_service as ps

from .test_build_profile import _seed


class _NotFound(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        self.send_error(404, "Not Found")

    def log_message(self, *_args):
        return


def _serve(handler):
    server = http.server.HTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _events(eng, run_id, kind):
    with eng.connect() as c:
        return [d if isinstance(d, dict) else json.loads(d) for d in c.execute(text(
            "SELECT data FROM events WHERE entity_id = :r AND event_type = :k"),
            {"r": run_id, "k": kind}).scalars().all()]


def test_a_hanging_preview_frees_the_build_in_seconds_and_records_its_failure(monkeypatch):
    from ._obs_seed import pg_engine

    eng = pg_engine()
    run_id = _seed(eng, "completed")
    release, asked = threading.Event(), {}

    def _hangs(engine, settings, rid, **kwargs):
        asked.update(kwargs)
        release.wait(30)                      # an app that never answers
        return {"id": "prev_x", "status": "failed",
                "error": "the app IS listening on port 1, but GET / returned 404."}

    monkeypatch.setattr(gcts, "_transaction", lambda e: e.begin())
    monkeypatch.setattr(ps, "lineage_impl_files", lambda *_a, **_k: ["web/index.html"])
    monkeypatch.setattr(ps, "start_preview_for_run", _hangs)

    started = time.monotonic()
    thread = gcts.auto_preview_after_pr(eng, gcts.Settings(), project_id="p_build_profile",
                                        run_id=run_id, accumulated=["web/index.html"])
    assert time.monotonic() - started < 3, "the build waited on its preview"
    assert thread is not None and thread.is_alive()
    # Bounded: the auto-preview never gets the 600 s window.
    assert 0 < asked["ready_timeout"] <= 180

    release.set()
    thread.join(10)
    assert not thread.is_alive()
    recorded = _events(eng, run_id, "code_task.auto_preview")
    assert [e["status"] for e in recorded] == ["failed"]
    assert "404" in recorded[0]["error"]


def test_a_preview_that_raises_is_recorded_not_lost(monkeypatch):
    from ._obs_seed import pg_engine

    eng = pg_engine()
    run_id = _seed(eng, "completed")

    def _boom(*_a, **_k):
        raise RuntimeError("sandbox gone")

    monkeypatch.setattr(gcts, "_transaction", lambda e: e.begin())
    monkeypatch.setattr(ps, "lineage_impl_files", lambda *_a, **_k: ["web/index.html"])
    monkeypatch.setattr(ps, "start_preview_for_run", _boom)
    thread = gcts.auto_preview_after_pr(eng, gcts.Settings(), project_id="p_build_profile",
                                        run_id=run_id)
    thread.join(10)
    failed = _events(eng, run_id, "code_task.auto_preview_failed")
    assert failed and "sandbox gone" in failed[0]["error"]


def test_the_build_path_never_starts_a_preview_inline():
    """The only call to start_preview_for_run in the build pipeline is the background one."""
    src = inspect.getsource(gcts)
    helper = inspect.getsource(gcts.auto_preview_after_pr)
    assert src.count("start_preview_for_run(") == helper.count("start_preview_for_run(") == 1
    assert "threading.Thread(" in helper


def test_a_404_at_the_health_path_fails_in_seconds_not_the_start_window(tmp_path):
    server = _serve(_NotFound)
    try:
        port = server.server_address[1]
        log = tmp_path / "_preview.log"
        log.write_text(f"listening on http://0.0.0.0:{port}\n")
        started = time.monotonic()
        assert ps._discover_working_port(str(log), port, timeout=600, absent_grace=1.0) is None
        assert time.monotonic() - started < 6
    finally:
        server.shutdown()
    assert ps.ABSENT_ROUTE_GRACE_SECONDS <= 30


def test_a_route_that_appears_within_the_grace_is_still_found(tmp_path):
    born = time.monotonic() + 1.0

    class _Late(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if time.monotonic() < born:
                self.send_error(404)
                return
            body = b"<html><body>ok</body></html>"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            return

    server = _serve(_Late)
    try:
        port = server.server_address[1]
        log = tmp_path / "_preview.log"
        log.write_text(f"listening on http://0.0.0.0:{port}\n")
        assert ps._discover_working_port(str(log), port, timeout=20, absent_grace=5.0) == port
    finally:
        server.shutdown()


def test_the_failure_names_the_declared_health_path_it_asked(tmp_path):
    server = _serve(_NotFound)
    try:
        port = server.server_address[1]
        log = tmp_path / "_preview.log"
        log.write_text(f"listening on http://0.0.0.0:{port}\n")
        why = ps._serve_failure(str(log), port, "/healthz")
        assert "GET /healthz returned 404" in why
        assert ps._classify_start_failure(why)[0] == "no_health_route"
        root = ps._serve_failure(str(log), port)
        assert "GET / returned 404" in root
        assert ps._classify_start_failure(root)[0] == "no_page_at_root"
    finally:
        server.shutdown()


def test_a_node_app_beside_a_root_pom_waits_the_node_window(tmp_path):
    """boss-sandbox: package.json and pom.xml both at the root; the preview runs `npm start`."""
    (tmp_path / "pom.xml").write_text("<project/>")
    (tmp_path / "package.json").write_text('{"scripts": {"start": "node server/index.js"}}')
    npm = ["sh", "-lc", "npm install --include=dev && npm start"]
    assert ps.preview_start_timeout(tmp_path, command=npm) == 180.0
    assert ps.preview_start_timeout(tmp_path, command=["sh", "-lc", "./mvnw spring-boot:run"]) == 600.0
    assert ps.preview_start_timeout(tmp_path, command=["./mvnw"], limit=180.0) == 180.0
