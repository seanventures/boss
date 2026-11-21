"""Opening an API's preview shows "This app is an API" and links the routes that answer —
through the real preview gateway, against a real app on a loopback port that answers 404
"unmatched" at "/" and 200 at /healthz and /readyz (prev_82bfd120d83a4662a23a, 2026-11-21).
A script's fetch of "/" and every non-root path still get the app's own answer."""
from __future__ import annotations

import http.server
import threading
import uuid

import pytest
from sqlalchemy import text

from boss_engine.services import preview_landing

PAGE = {"accept": "text/html,application/xhtml+xml", "sec-fetch-mode": "navigate"}


class _Gateway(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        ok = self.path in ("/healthz", "/readyz")
        body = b"ok" if ok else b"unmatched"
        self.send_response(200 if ok else 404)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def api_preview(engine):
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Gateway)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    pid = f"prv_{uuid.uuid4().hex[:12]}"
    with engine.begin() as db:
        db.execute(text("INSERT INTO app_previews (id, project_id, repo, branch, command, mode, "
                        "status, url) VALUES (:i, 'boss-dogfood', 'o/gw', 'b', 'go run .', "
                        "'process', 'running', :u)"),
                   {"i": pid, "u": f"http://127.0.0.1:{server.server_address[1]}"})
    preview_landing._CACHE.pop(pid, None)
    yield pid
    server.shutdown()
    with engine.begin() as db:
        db.execute(text("DELETE FROM app_previews WHERE id = :i"), {"i": pid})


def test_a_page_load_of_the_root_lands_on_the_routes_that_answer(client, api_preview):
    got = client.get(f"/v1/previews/{api_preview}/web/", headers=PAGE)
    assert got.status_code == 200
    assert got.headers.get("x-boss-preview-landing") == "api"
    assert "sandbox" in got.headers.get("content-security-policy", "")
    page = got.text
    assert "This app is an API — there is no page at /" in page
    assert "HTTP 404" in page and "unmatched" in page
    assert "href='healthz'" in page and "href='readyz'" in page
    assert "href='status'" not in page, "a route that did not answer is not a link"


def test_the_linked_route_opens_through_the_same_preview(client, api_preview):
    got = client.get(f"/v1/previews/{api_preview}/web/healthz", headers=PAGE)
    assert got.status_code == 200 and got.text == "ok"


def test_a_script_and_a_missing_route_keep_the_app_s_own_404(client, api_preview):
    fetched = client.get(f"/v1/previews/{api_preview}/web/", headers={"accept": "application/json"})
    assert fetched.status_code == 404 and "x-boss-preview-landing" not in fetched.headers
    missing = client.get(f"/v1/previews/{api_preview}/web/orders/7", headers=PAGE)
    assert missing.status_code == 404 and "x-boss-preview-landing" not in missing.headers
