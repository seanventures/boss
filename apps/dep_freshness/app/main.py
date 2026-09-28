"""Dependency freshness report page.

Deployability: HOST/PORT are read only here, in the server startup path.
No other code path binds a socket or hardcodes a loopback address/fixed port.
"""

from __future__ import annotations

import html
import os
import time
from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse

from .freshness import classify
from .metrics import MetricsRegistry
from .snapshot import DependencyRecord, load_snapshot

APP_VERSION = "0.1.0"
DATA_DIR = Path(__file__).resolve().parents[1] / "data"
SNAPSHOT_PATH = DATA_DIR / "dependencies.json"

app = FastAPI(title="BOSS Dependency Freshness Report", version=APP_VERSION)
metrics = MetricsRegistry(version=APP_VERSION)
_snapshot: list[DependencyRecord] = load_snapshot(SNAPSHOT_PATH)


@app.middleware("http")
async def metrics_middleware(request: Request, call_next):
    if request.url.path == "/metrics":
        return await call_next(request)

    start = time.monotonic()
    response = await call_next(request)
    duration = time.monotonic() - start

    route = request.scope.get("route")
    route_label = route.path_format if route is not None else request.url.path
    labels = {"method": request.method, "route": route_label}
    metrics.http_request_duration_seconds.observe(duration, labels)
    metrics.http_requests_total.inc({**labels, "status": str(response.status_code)})
    return response


def _render_report(records: list[DependencyRecord]) -> str:
    rows = []
    if not records:
        rows.append("<tr><td colspan='4'>No dependencies tracked.</td></tr>")
    for record in records:
        status = classify(record.current_version, record.latest_version)
        rows.append(
            "<tr>"
            f"<td>{html.escape(record.name)}</td>"
            f"<td>{html.escape(record.current_version)}</td>"
            f"<td>{html.escape(record.latest_version) if record.latest_version else 'unknown'}</td>"
            f"<td>{html.escape(status.value)}</td>"
            "</tr>"
        )
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <title>Dependency Freshness Report</title>
</head>
<body>
  <h1>Dependency Freshness Report</h1>
  <table>
    <thead>
      <tr><th>Name</th><th>Current</th><th>Latest</th><th>Status</th></tr>
    </thead>
    <tbody>
      {"".join(rows)}
    </tbody>
  </table>
</body>
</html>"""


@app.get("/", response_class=HTMLResponse)
def report_page() -> HTMLResponse:
    return HTMLResponse(_render_report(_snapshot))


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


@app.get("/metrics")
def metrics_endpoint() -> Response:
    return Response(
        content=metrics.render(),
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )


if __name__ == "__main__":
    import uvicorn

    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run(app, host=host, port=port)
