"""Send a saved Codex or Claude Code trace to the company's Braintrust or OTLP endpoint.

No destination row means no outbound call. The POST body is only the spans
already stored for that run. A failed POST leaves those spans in place and
writes a trace_export_attempts row a caller can read.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any, Mapping, Optional
from urllib.parse import urlparse
from uuid import uuid4

from sqlalchemy import text

from ..db import begin

CLI_ENGINES = frozenset({"claude_code", "codex"})


class ExportError(Exception):
    """The configured endpoint did not accept the trace."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        raise ExportError(f"export failed: HTTP {code}")


def org_for_project(connection, project_id: str) -> str:
    org_id = connection.execute(text("""
        SELECT w.org_id FROM projects p
          JOIN workspaces w ON w.id = p.workspace_id
         WHERE p.id = :p
    """), {"p": project_id}).scalar()
    if not org_id:
        raise LookupError(project_id)
    return str(org_id)


def _endpoint(value: str) -> str:
    endpoint = str(value or "").strip()
    parsed = urlparse(endpoint)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("endpoint must be an http(s) URL")
    return endpoint


def set_destination(connection, *, org_id: str, kind: str, endpoint: str,
                    updated_by: str = "operator") -> dict:
    if kind not in ("braintrust", "otlp"):
        raise ValueError("kind must be braintrust or otlp")
    url = _endpoint(endpoint)
    row = connection.execute(text("""
        INSERT INTO trace_export_destinations (org_id, kind, endpoint, updated_by)
        VALUES (:org, :kind, :endpoint, :by)
        ON CONFLICT (org_id) DO UPDATE SET
            kind = EXCLUDED.kind,
            endpoint = EXCLUDED.endpoint,
            updated_by = EXCLUDED.updated_by,
            updated_at = now()
        RETURNING org_id, kind, endpoint
    """), {"org": org_id, "kind": kind, "endpoint": url, "by": updated_by}).mappings().one()
    return dict(row)


def destination(connection, org_id: str) -> Optional[dict]:
    row = connection.execute(text("""
        SELECT org_id, kind, endpoint FROM trace_export_destinations WHERE org_id = :org
    """), {"org": org_id}).mappings().first()
    if row is None or not str(row["endpoint"] or "").strip():
        return None
    return dict(row)


def _attrs(row: Mapping[str, Any]) -> dict:
    raw = row.get("attributes") or {}
    if isinstance(raw, str):
        raw = json.loads(raw)
    return dict(raw)


def saved_spans(connection, project_id: str, run_id: str) -> list[dict]:
    return [dict(row) for row in connection.execute(text("""
        SELECT span_id, trace_id, parent_span_id, run_id, kind, name, status,
               attributes, started_at, ended_at
          FROM trace_spans
         WHERE project_id = :p AND run_id = :r
         ORDER BY span_id
    """), {"p": project_id, "r": run_id}).mappings()]


def _engine_name(spans: list[Mapping[str, Any]]) -> str:
    for span in spans:
        if span["kind"] == "run":
            return str(_attrs(span).get("engine") or "")
    return ""


def _nano(value: Any) -> str:
    if isinstance(value, datetime):
        when = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return str(int(when.timestamp() * 1_000_000_000))
    return "0"


def _otlp(spans: list[Mapping[str, Any]]) -> dict:
    exported = []
    for span in spans:
        item = {"traceId": span["trace_id"], "spanId": span["span_id"], "name": span["name"],
                "startTimeUnixNano": _nano(span["started_at"]),
                "endTimeUnixNano": _nano(span["ended_at"] or span["started_at"])}
        if span["parent_span_id"]:
            item["parentSpanId"] = span["parent_span_id"]
        attributes = [{"key": "boss.span.kind", "value": {"stringValue": str(span["kind"])}},
                      {"key": "boss.span.status", "value": {"stringValue": str(span["status"])}}]
        for key, value in _attrs(span).items():
            attributes.append({"key": str(key), "value": {"stringValue": str(value)}})
        item["attributes"] = attributes
        exported.append(item)
    return {"resourceSpans": [{"scopeSpans": [{"spans": exported}]}]}


def _braintrust(spans: list[Mapping[str, Any]]) -> dict:
    roots = [span["span_id"] for span in spans if span["kind"] == "run"]
    root = roots[0] if roots else spans[0]["span_id"]
    events = []
    for span in spans:
        events.append({
            "id": span["span_id"],
            "span_id": span["span_id"],
            "root_span_id": root,
            "span_parents": [span["parent_span_id"]] if span["parent_span_id"] else [],
            "name": span["name"],
            "metadata": {"kind": span["kind"], "status": span["status"],
                         "trace_id": span["trace_id"], "run_id": span["run_id"]},
        })
    return {"events": events}


def _body(kind: str, spans: list[Mapping[str, Any]]) -> dict:
    if kind == "otlp":
        return _otlp(spans)
    if kind == "braintrust":
        return _braintrust(spans)
    raise ValueError(f"unknown export kind {kind!r}")


def _post(url: str, payload: dict) -> None:
    raw = json.dumps(payload).encode()
    request = urllib.request.Request(
        url, data=raw, method="POST",
        headers={"Content-Type": "application/json", "Content-Length": str(len(raw))})
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(request, timeout=5) as response:
            status = getattr(response, "status", 200)
            response.read()
    except ExportError:
        raise
    except urllib.error.HTTPError as exc:
        raise ExportError(f"export failed: HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise ExportError(f"export failed: {exc.reason}") from exc
    if status >= 300:
        raise ExportError(f"export failed: HTTP {status}")


def _record(engine, *, org_id: str, project_id: str, run_id: str, engine_name: str,
            trace_id: str, status: str, span_count: int, error: Optional[str]) -> None:
    with begin(engine) as connection:
        connection.execute(text("""
            INSERT INTO trace_export_attempts (
                id, org_id, project_id, run_id, engine, trace_id, status, span_count, error)
            VALUES (:id, :org, :p, :r, :engine, :trace, :status, :n, :error)
        """), {"id": f"tex_{uuid4().hex[:16]}", "org": org_id, "p": project_id, "r": run_id,
               "engine": engine_name, "trace": trace_id, "status": status, "n": span_count,
               "error": None if error is None else error[:500]})


def export_saved_trace(engine, *, project_id: str, run_id: str) -> dict:
    """One export of the spans stored for this run, or no call when nothing is configured."""
    with begin(engine) as connection:
        org_id = org_for_project(connection, project_id)
        chosen = destination(connection, org_id)
        spans = saved_spans(connection, project_id, run_id)
    engine_name = _engine_name(spans)
    if chosen is None:
        return {"status": "skipped", "outbound": False, "reason": "no endpoint",
                "span_count": len(spans)}
    if engine_name not in CLI_ENGINES:
        return {"status": "skipped", "outbound": False,
                "reason": "not a codex or claude_code trace", "span_count": len(spans)}
    if not spans:
        return {"status": "skipped", "outbound": False, "reason": "no saved trace",
                "span_count": 0}
    trace_id = next(span["trace_id"] for span in spans if span["kind"] == "run")
    payload = _body(chosen["kind"], spans)
    try:
        _post(chosen["endpoint"], payload)
    except ExportError as exc:
        _record(engine, org_id=org_id, project_id=project_id, run_id=run_id,
                engine_name=engine_name, trace_id=trace_id, status="failed",
                span_count=len(spans), error=str(exc))
        return {"status": "failed", "outbound": True, "span_count": len(spans), "error": str(exc)}
    _record(engine, org_id=org_id, project_id=project_id, run_id=run_id,
            engine_name=engine_name, trace_id=trace_id, status="sent",
            span_count=len(spans), error=None)
    return {"status": "sent", "outbound": True, "span_count": len(spans)}


def failed_exports(engine, *, project_id: str, limit: int = 50) -> list[dict]:
    with engine.connect() as connection:
        rows = connection.execute(text("""
            SELECT id, run_id, engine, trace_id, status, span_count, error, created_at
              FROM trace_export_attempts
             WHERE project_id = :p AND status = 'failed'
             ORDER BY created_at DESC, id DESC
             LIMIT :n
        """), {"p": project_id, "n": limit}).mappings()
    out = []
    for row in rows:
        item = dict(row)
        created = item.get("created_at")
        if isinstance(created, datetime):
            item["created_at"] = created.isoformat()
        out.append(item)
    return out
