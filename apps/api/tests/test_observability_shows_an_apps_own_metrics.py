"""Observability's app drawer carries the app's OWN traffic, scraped from its declared /metrics.

The proof path an operator (or a test) drives: an app deployed with `metrics: {path: /metrics}`
in boss.deploy.yaml is scraped by `POST /v1/delivery/health/sweep` (the worker's idle sweep) or
`POST /v1/observability/apps/{id}/probe` (Probe now), and `GET /v1/observability/apps/{id}`
answers `metrics` — request rate, error rate, p50/p95, the last scrape in words, the alert.
"""
from __future__ import annotations

import http.server
import json
import threading
from uuid import uuid4

from sqlalchemy import text

from apps.api.app.db import begin
from apps.api.tests._obs_api_seed import PROJECT_ID


class _App:
    def __init__(self):
        self.body = 'http_requests_total{status="200"} 10\n'
        self.status = 200
        app = self

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                data = app.body.encode()
                self.send_response(app.status if self.path == "/metrics" else 200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *a):
                pass

        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.origin = f"http://127.0.0.1:{self.srv.server_address[1]}"


def _stack_app(c, origin: str, metrics: dict | None) -> str:
    tid = f"t_metrics_{uuid4().hex[:8]}"
    c.execute(text(
        "INSERT INTO deployment_targets (id, project_id, app_name, repo, environment, target_kind, "
        " target_ref, status, created_by, domains, public_url) VALUES (:t, :p, :a, 'acme/hd', "
        " 'prod', 'stack', :a, 'active', 'test', '[]'::jsonb, :u)"),
        {"t": tid, "p": PROJECT_ID, "a": tid, "u": origin + "/"})
    c.execute(text(
        "INSERT INTO stack_deployments (id, project_id, target_id, repository, ref, commit_sha, "
        " manifest, status, created_by, finished_at) VALUES (:id, :p, :t, 'acme/hd', 'main', "
        " 'abc', CAST(:m AS jsonb), 'healthy', 'test', now())"),
        {"id": f"sd_{uuid4().hex[:8]}", "p": PROJECT_ID, "t": tid,
         "m": json.dumps({"metrics": metrics})})
    return tid


def test_probe_now_scrapes_and_the_drawer_shows_traffic(client, engine):
    app = _App()
    try:
        with begin(engine) as c:
            tid = _stack_app(c, app.origin, {"path": "/metrics", "via": "public"})
            bare = _stack_app(c, app.origin, None)

        r = client.post(f"/v1/observability/apps/{tid}/probe", json={"project_id": PROJECT_ID})
        assert r.status_code == 200, r.text
        assert r.json()["metrics_scraped"] == 1
        app.body = ('http_requests_total{status="200"} 50\n'
                    'http_requests_total{status="503"} 10\n')
        assert client.post(f"/v1/observability/apps/{tid}/probe",
                           json={"project_id": PROJECT_ID}).status_code == 200

        m = client.get(f"/v1/observability/apps/{tid}",
                       params={"project_id": PROJECT_ID}).json()["metrics"]
        assert m["declared"] and m["state"] == "ok" and m["word"] == "Scraped"
        assert m["requests"] == 50 and m["errors"] == 10 and m["error_rate"] == 0.2
        assert m["route"] == "public" and m["url"] == app.origin + "/metrics"
        assert m["alert"]["state"] == "firing" and m["alert"]["incident_id"]

        app.status = 404
        client.post(f"/v1/observability/apps/{tid}/probe", json={"project_id": PROJECT_ID})
        m = client.get(f"/v1/observability/apps/{tid}",
                       params={"project_id": PROJECT_ID}).json()["metrics"]
        assert m["state"] == "failing" and m["word"] == "Metrics endpoint returned 404"

        none = client.get(f"/v1/observability/apps/{bare}",
                          params={"project_id": PROJECT_ID}).json()["metrics"]
        assert none["declared"] is False

        swept = client.post("/v1/delivery/health/sweep").json()
        assert "scraped" in swept, swept
    finally:
        app.srv.shutdown()
