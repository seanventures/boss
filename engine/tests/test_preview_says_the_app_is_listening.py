"""A preview that fails because the app serves no page must say so, not report a timeout.

The interview-scorecard build shipped a JSON API: /api/* and /metrics, and no route for "/".
The preview started it correctly — its log said "Serving the interview scorecard app on
http://0.0.0.0:47053" — and the health probe's 404 at "/" was reported to the operator as
"app did not serve HTTP within timeout". That sentence says nothing ever bound, which sent the
investigation to the build machinery instead of to the missing UI. Eight preview attempts were
spent on it.
"""

from __future__ import annotations

import http.server
import threading

from boss_engine.services import preview_service as ps


class _ApiOnly(http.server.BaseHTTPRequestHandler):
    """Serves /metrics and nothing else — the shape the scorecard build produced."""

    def do_GET(self):  # noqa: N802 — BaseHTTPRequestHandler's own naming
        if self.path == "/metrics":
            body = b"app_uptime_seconds 1\n"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_error(404, "Not Found")

    def log_message(self, *_args):
        return


def _serve(handler):
    server = http.server.HTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def test_it_names_the_port_and_the_status_instead_of_claiming_a_timeout(tmp_path):
    server = _serve(_ApiOnly)
    try:
        port = server.server_address[1]
        log = tmp_path / "_preview.log"
        log.write_text(f"Serving the interview scorecard app on http://0.0.0.0:{port}\n")
        detail = ps._diagnose_listening_port(str(log), port)
        assert detail is not None
        assert f"listening on port {port}" in detail
        assert "404" in detail
        assert "no page at the site root" in detail
    finally:
        server.shutdown()


def test_the_health_probe_really_does_reject_an_api_only_app(tmp_path):
    """The premise: _http_ok fails this app, which is why the diagnosis is needed at all."""
    server = _serve(_ApiOnly)
    try:
        assert ps._http_ok(server.server_address[1]) is False
    finally:
        server.shutdown()


def test_nothing_listening_still_reads_as_a_plain_timeout(tmp_path):
    """When nothing bound, the generic message is the honest one — do not invent a diagnosis."""
    log = tmp_path / "_preview.log"
    log.write_text("error during build: Could not resolve entry module \"index.html\".\n")
    assert ps._diagnose_listening_port(str(log), 1) is None


def test_a_listening_app_is_not_classified_as_a_port_timeout():
    """port_timeout tells the operator to check HOST/PORT. This app honoured both."""
    kind, action = ps._classify_start_failure(
        "the app IS listening on port 49005, but GET / returned 404. It serves no page at the "
        "site root — check that the build produced a UI and not only an API.")
    assert kind == "no_page_at_root"
    assert "HOST/PORT" not in action
    assert "UI" in action


def test_a_build_that_never_bound_is_still_a_port_timeout():
    kind, _action = ps._classify_start_failure(
        "app did not serve HTTP within timeout. log tail: Could not resolve entry module.")
    assert kind == "port_timeout"


def test_the_shepherd_files_it_as_a_preview_failure_not_an_infrastructure_retry():
    """A missing UI is a property of the BUILD. Filed anywhere else, the prescribed repair is a
    blind retry — and retrying cannot add a page the build never produced."""
    from boss_engine.services import task_shepherd

    listening = ("the app IS listening on port 49005, but GET / returned 404. It serves no page "
                 "at the site root — check that the build produced a UI and not only an API.")
    assert task_shepherd.classify(listening) == task_shepherd.PREVIEW_FAILED
    # The phrase is what carries it there: without the wording this message has no other
    # keyword in common with the preview bucket.
    assert task_shepherd.classify(
        "the app IS listening on port 49005, but GET / returned 404."
    ) != task_shepherd.PREVIEW_FAILED


# ── ENT-REC-11: a 404 at / on a service is up only when it has a health route ────────────────

def _answering(routes: dict[str, int], default: int):
    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            status = routes.get(self.path.split("?")[0], default)
            body = b'{"ok": true}' if status < 400 else b"no"
            self.send_response(status)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            return

    return _Handler


def test_a_service_with_a_real_health_route_is_up_despite_a_404_at_home():
    """BOSSAPP-1308's gateway: 404 at /, 200 at /healthz, 404 everywhere else — up."""
    server = _serve(_answering({"/healthz": 200}, 404))
    try:
        assert ps._serving(server.server_address[1], "/", service=True) is True
    finally:
        server.shutdown()


def test_a_declared_health_path_is_the_apps_word():
    server = _serve(_answering({"/api/health": 200}, 404))
    try:
        assert ps._serving(server.server_address[1], "/api/health", service=True) is True
    finally:
        server.shutdown()


def test_a_catch_all_that_404s_only_at_home_has_no_health_route(tmp_path):
    """The REC-11 node sample: every path answers 200 JSON except /. Its /healthz is the
    catch-all, not a route, so it is NOT up, and the reason says so."""
    server = _serve(_answering({"/": 404}, 200))
    try:
        port = server.server_address[1]
        assert ps._serving(port, "/", service=True) is False
        log = tmp_path / "_preview.log"
        log.write_text("")
        detail = ps._diagnose_listening_port(str(log), port)
        assert "GET / returned 404" in detail and "catch-all" in detail
    finally:
        server.shutdown()


# ── ENT-REC-10: the serve proof asks the port BOSS assigned ──────────────────────────────────

def test_a_logged_port_that_serves_is_returned_only_after_the_assigned_port_stays_silent(
        tmp_path, monkeypatch):
    monkeypatch.setattr(ps, "WRONG_PORT_GRACE_SECONDS", 0.5)
    server = _serve(_answering({}, 200))
    try:
        port = server.server_address[1]
        log = tmp_path / "_app.log"
        log.write_text(f"listening on http://0.0.0.0:{port}\n")
        silent = ps._free_port()
        assert ps._discover_working_port(str(log), silent, timeout=5, assigned_first=True) == port
        # The preview keeps following the logged port, as before (it shows what the app serves).
        assert ps._discover_working_port(str(log), silent, timeout=5) == port
        # A log naming another URL never outruns $PORT when $PORT serves.
        other = _serve(_answering({}, 200))
        try:
            log.write_text(f"proxying to http://127.0.0.1:{port}\n")
            assert ps._discover_working_port(str(log), other.server_address[1], timeout=5,
                                             assigned_first=True) == other.server_address[1]
        finally:
            other.shutdown()
    finally:
        server.shutdown()
