"""A page that answers 2xx with real HTML is serving; only a page that RENDERS an error is not.

invoice-desk (crun_33379dfc1f874840968e, 2026-11-18), a Next.js App Router app on node:sqlite,
answered 200 at `/` with its real page, and its app_serves gate failed with "did not answer HTTP
within 180s … GET / returned 200, but the health check rejected the page it served". The light
lane showed "Checks failing" for an app that worked.

The cause: every Next.js App Router page inlines its flight payload in
`<script>self.__next_f.push(...)</script>`, and that payload carries the framework's default
not-found tree, `"404: This page could not be found."`, on every page. The error markers were
matched against the raw HTML, scripts included. node's ExperimentalWarning on stderr played no
part — and must never.
"""

from __future__ import annotations

import http.server
import threading

from boss_engine.services import preview_service as ps
from boss_engine.services import visual_gate as vg

# The shape `next start` serves for a working App Router page (trimmed from invoice-desk's own
# response): the real page in the markup, and the default not-found tree in the flight script.
NEXT_PAGE = (
    '<!DOCTYPE html><html lang="en"><head><meta charSet="utf-8"/>'
    '<script src="/_next/static/chunks/webpack.js" async=""></script>'
    '<title>Invoice Desk</title></head><body><nav class="topbar"><a href="/">Invoice Desk</a>'
    '</nav><main class="shell"><!--$?--><template id="B:0"></template>'
    '<div class="loading-state" role="status"><span class="sr-only">Loading Invoice Desk…</span>'
    '</div><!--/$--></main><div hidden id="S:0"><h1>Invoice Desk</h1><p>Add a client, then '
    'open an invoice for them.</p></div>'
    '<script>(self.__next_f=self.__next_f||[]).push([0])</script>'
    '<script>self.__next_f.push([1,"0:{\\"P\\":null,\\"c\\":[\\"\\",\\"\\"],\\"notFound\\":'
    '[[[\\"$\\",\\"title\\",null,{\\"children\\":\\"404: This page could not be found.\\"}],'
    '[\\"$\\",\\"h1\\",null,{\\"className\\":\\"next-error-h1\\",\\"children\\":404}]]]}\\n"])'
    '</script></body></html>')

# Pages that really are error pages, served with 200.
NEXT_GLOBAL_ERROR = ('<!DOCTYPE html><html id="__next_error__"><head></head><body>'
                     '<script>self.__next_f.push([0])</script></body></html>')
RENDERED_500 = "<html><body><h1>Internal Server Error</h1><p>Something broke.</p></body></html>"

NEXT_LOG = (
    "   ▲ Next.js 15.5.29\n   - Local:        http://127.0.0.1:{port}\n\n ✓ Starting...\n"
    " ✓ Ready in 263ms\n(node:45996) ExperimentalWarning: SQLite is an experimental feature "
    "and might change at any time\n(Use `node --trace-warnings ...` to show where the warning "
    "was created)\n")


def _serve(body: str, status: int = 200):
    payload = body.encode()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *_args):
            return

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


# --- the rubric ------------------------------------------------------------------------- #

def test_a_working_next_page_is_not_an_error_page():
    assert "404: This page could not be found." in NEXT_PAGE  # the premise: it IS in the HTML
    assert vg.error_page_marker(NEXT_PAGE) is None
    assert vg.is_error_page(NEXT_PAGE) is False


def test_a_rendered_error_page_still_is_one():
    assert vg.error_page_marker(NEXT_GLOBAL_ERROR) == "__next_error__"
    assert vg.error_page_marker(RENDERED_500) == "internal server error"
    assert vg.is_error_page("<html><body><h1>404</h1><p>This page could not be found.</p>"
                            "</body></html>")


def test_a_marker_in_a_comment_or_style_is_not_rendered():
    assert not vg.is_error_page("<html><!-- Internal Server Error --><style>.x:after"
                                "{content:'Application error'}</style><body><h1>Hi</h1></body>")


# --- the health check and its diagnosis -------------------------------------------------- #

def test_the_health_check_accepts_the_next_page_with_the_warning_in_its_log(tmp_path):
    server = _serve(NEXT_PAGE)
    try:
        port = server.server_address[1]
        log = tmp_path / "serve.log"
        log.write_text(NEXT_LOG.format(port=port))
        assert ps._http_ok(port) is True
        assert ps._discover_working_port(str(log), port, timeout=3) == port
    finally:
        server.shutdown()


def test_a_200_error_page_still_fails_and_the_diagnosis_names_what_it_rendered(tmp_path):
    for body, said in ((RENDERED_500, "internal server error"),
                       (NEXT_GLOBAL_ERROR, "__next_error__")):
        server = _serve(body)
        try:
            port = server.server_address[1]
            log = tmp_path / "serve.log"
            log.write_text(f"listening on http://127.0.0.1:{port}\n")
            assert ps._http_ok(port) is False
            detail = ps._diagnose_listening_port(str(log), port)
            assert f"listening on port {port}" in detail and "returned 200" in detail
            assert "error page" in detail and said in detail
            assert "health check rejected" not in detail
        finally:
            server.shutdown()


def test_a_5xx_still_fails():
    server = _serve(RENDERED_500, status=500)
    try:
        assert ps._http_ok(server.server_address[1]) is False
    finally:
        server.shutdown()


# --- the gate, end to end ---------------------------------------------------------------- #

_APP = '''\
import os, sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BODY = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "page.html"),
            encoding="utf-8").read().encode()


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(BODY)))
        self.end_headers()
        self.wfile.write(BODY)

    def log_message(self, *args):
        pass


server = ThreadingHTTPServer(("0.0.0.0", int(os.environ["PORT"])), Handler)
print(f"   - Local:        http://127.0.0.1:{server.server_port}", flush=True)
print(" \\u2713 Ready in 263ms", flush=True)
print("(node:45996) ExperimentalWarning: SQLite is an experimental feature and might change "
      "at any time", file=sys.stderr, flush=True)
server.serve_forever()
'''


def _app(tmp_path, page: str):
    (tmp_path / "server.py").write_text(_APP)
    (tmp_path / "page.html").write_text(page, encoding="utf-8")
    (tmp_path / "boss.preview.command").write_text("python3 server.py\n")
    return tmp_path


def test_the_gate_passes_a_working_next_page_with_an_experimental_warning(tmp_path):
    root = _app(tmp_path, NEXT_PAGE)
    record = ps.prove_app_serves(root, ["boss.preview.command", "server.py"], timeout=20)
    assert record.get("proven") is True and record.get("ok") is not False, record


def test_the_gate_fails_a_200_error_page_without_contradicting_itself(tmp_path):
    root = _app(tmp_path, RENDERED_500)
    record = ps.prove_app_serves(root, ["boss.preview.command", "server.py"], timeout=3)
    assert record["ok"] is False and not record.get("proven"), record
    error = record["error"]
    assert "did not answer HTTP" not in error, error
    assert "returned 200" in error and "internal server error" in error, error
    headline = ps.app_serves_headline(record)
    assert headline.startswith("the app did not serve a usable page"), headline
