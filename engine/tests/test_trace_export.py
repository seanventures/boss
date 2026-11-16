"""Codex and Claude Code run logs leave only for a company-chosen endpoint.

A missing destination must not open a socket. A saved trace is one POST of the
spans that were stored, never a span that was not. A failed POST keeps those
spans and leaves a failure a caller can read.
"""
from __future__ import annotations

import json
import os
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import pytest
from sqlalchemy import create_engine, text

from apps.api.app.routers import gap_trace_export as gap
from boss_engine.services.trace_export import export_saved_trace

ORG = "org_paperclip_trace"
WORKSPACE = "ws_paperclip_trace"
PROJECT = "prj_paperclip_trace"
TRACE = "a1b2c3d4e5f60718293a4b5c6d7e8f90"
RUN_SPAN = "1111111111111111"
TOOL_SPAN = "2222222222222222"
OTHER_SPAN = "ffffffffffffffff"
ABSENT_SPAN = "9999999999999999"


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        size = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(size)
        self.server.bodies.append({"path": self.path, "body": raw})
        code = 500 if self.server.fail else 200
        message = b'{"error":"collector down"}' if code == 500 else b"{}"
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(message)))
        self.end_headers()
        self.wfile.write(message)

    def log_message(self, fmt, *args):
        return


class _Fake(ThreadingHTTPServer):
    def __init__(self):
        self.bodies = []
        self.fail = False
        super().__init__(("127.0.0.1", 0), _Handler)


def _span_ids(kind, raw):
    data = json.loads(raw)
    if kind == "otlp":
        spans = data["resourceSpans"][0]["scopeSpans"][0]["spans"]
        return [span["spanId"] for span in spans]
    return [event["span_id"] for event in data["events"]]


@pytest.fixture(scope="module")
def db():
    url = os.environ["DATABASE_URL"]
    name = url.rsplit("/", 1)[-1].split("?", 1)[0].lower()
    assert "test" in name
    engine = create_engine(url, future=True)
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO orgs (id, name) VALUES (:id, 'Paperclip') "
            "ON CONFLICT (id) DO NOTHING"), {"id": ORG})
        connection.execute(text(
            "INSERT INTO workspaces (id, org_id, name) VALUES (:id, :org, 'Paperclip') "
            "ON CONFLICT (id) DO NOTHING"), {"id": WORKSPACE, "org": ORG})
        connection.execute(text(
            "INSERT INTO projects (id, workspace_id, name, slug) "
            "VALUES (:id, :ws, 'Paperclip trace', 'paperclip-trace') "
            "ON CONFLICT (id) DO NOTHING"), {"id": PROJECT, "ws": WORKSPACE})
    yield engine
    engine.dispose()


@pytest.fixture()
def clean(db):
    with db.begin() as connection:
        connection.execute(text("DELETE FROM trace_export_attempts WHERE project_id = :p"),
                           {"p": PROJECT})
        connection.execute(text("DELETE FROM trace_spans WHERE project_id = :p"), {"p": PROJECT})
        connection.execute(text("DELETE FROM trace_export_destinations WHERE org_id = :o"),
                           {"o": ORG})
    yield db


@pytest.fixture()
def server():
    fake = _Fake()
    thread = threading.Thread(target=fake.serve_forever, daemon=True)
    thread.start()
    yield fake
    fake.shutdown()
    fake.server_close()


@pytest.fixture()
def outbound(monkeypatch):
    """Refuse any TCP connect that is not Postgres or the loopback fake server."""
    parsed = urlparse(os.environ["DATABASE_URL"])
    postgres_port = parsed.port
    real = socket.create_connection
    calls = []

    def guarded(address, *args, **kwargs):
        host, port = address[0], int(address[1])
        if host not in ("127.0.0.1", "::1", "localhost"):
            raise AssertionError(f"refusing outbound host {host}")
        if port != postgres_port:
            calls.append((host, port))
        return real(address, *args, **kwargs)

    monkeypatch.setattr(socket, "create_connection", guarded)
    return calls


def _save(engine, *, run_id, engine_name, spans):
    with engine.begin() as connection:
        for span_id, parent, kind, name in spans:
            connection.execute(text("""
                INSERT INTO trace_spans (
                    span_id, trace_id, parent_span_id, project_id, run_id, kind, name,
                    status, attributes)
                VALUES (
                    :span_id, :trace_id, :parent, :project, :run, :kind, :name,
                    'ok', CAST(:attrs AS jsonb))
            """), {"span_id": span_id, "trace_id": TRACE, "parent": parent,
                   "project": PROJECT, "run": run_id, "kind": kind, "name": name,
                   "attrs": json.dumps({"engine": engine_name} if kind == "run" else {})})


def _stored(engine, run_id):
    with engine.connect() as connection:
        return [dict(row) for row in connection.execute(text("""
            SELECT span_id, parent_span_id, kind, name, status, attributes
              FROM trace_spans
             WHERE project_id = :p AND run_id = :r
             ORDER BY span_id
        """), {"p": PROJECT, "r": run_id}).mappings()]


def _configure(kind, endpoint):
    gap.put_destination(PROJECT, gap.DestinationIn(kind=kind, endpoint=endpoint))


def test_no_endpoint_makes_no_outbound_call(clean, server, outbound):
    _save(clean, run_id="run_quiet", engine_name="codex", spans=[
        (RUN_SPAN, None, "run", "codex"),
        (TOOL_SPAN, RUN_SPAN, "tool", "read_file"),
    ])
    result = export_saved_trace(clean, project_id=PROJECT, run_id="run_quiet")
    assert result["status"] == "skipped"
    assert result["outbound"] is False
    assert outbound == []
    assert server.bodies == []
    assert [row["span_id"] for row in _stored(clean, "run_quiet")] == [RUN_SPAN, TOOL_SPAN]


@pytest.mark.parametrize("kind,path,engine_name", [
    ("otlp", "/v1/traces", "claude_code"),
    ("braintrust", "/logs", "codex"),
])
def test_a_configured_endpoint_receives_one_export_of_the_saved_trace(
        clean, server, outbound, kind, path, engine_name):
    run_id = f"run_{kind}"
    _save(clean, run_id=run_id, engine_name=engine_name, spans=[
        (RUN_SPAN, None, "run", engine_name),
        (TOOL_SPAN, RUN_SPAN, "tool", "read_file"),
    ])
    _save(clean, run_id="run_other", engine_name=engine_name, spans=[
        (OTHER_SPAN, None, "run", "other"),
    ])
    before = _stored(clean, run_id)
    _configure(kind, server_url(server, path))
    result = export_saved_trace(clean, project_id=PROJECT, run_id=run_id)
    assert result["status"] == "sent"
    assert result["span_count"] == 2
    assert len(server.bodies) == 1
    assert server.bodies[0]["path"] == path
    assert len(outbound) == 1
    sent = _span_ids(kind, server.bodies[0]["body"])
    assert sent == [RUN_SPAN, TOOL_SPAN]
    assert OTHER_SPAN not in server.bodies[0]["body"].decode()
    assert ABSENT_SPAN not in server.bodies[0]["body"].decode()
    assert _stored(clean, run_id) == before


def server_url(server, path):
    return f"http://127.0.0.1:{server.server_address[1]}{path}"


def test_a_failed_export_keeps_the_trace_and_records_a_visible_failure(clean, server, outbound):
    run_id = "run_failed"
    _save(clean, run_id=run_id, engine_name="claude_code", spans=[
        (RUN_SPAN, None, "run", "claude_code"),
        (TOOL_SPAN, RUN_SPAN, "tool", "edit_file"),
    ])
    before = _stored(clean, run_id)
    server.fail = True
    _configure("otlp", server_url(server, "/v1/traces"))
    result = export_saved_trace(clean, project_id=PROJECT, run_id=run_id)
    assert result["status"] == "failed"
    assert "500" in result["error"]
    assert _stored(clean, run_id) == before
    shown = gap.list_failures(PROJECT)
    assert shown["failures"][0]["run_id"] == run_id
    assert shown["failures"][0]["status"] == "failed"
    assert "500" in shown["failures"][0]["error"]
    assert len(server.bodies) == 1


def test_a_run_that_is_not_codex_or_claude_code_is_not_sent(clean, server, outbound):
    _save(clean, run_id="run_scripted", engine_name="scripted", spans=[
        (RUN_SPAN, None, "run", "scripted"),
    ])
    _configure("otlp", server_url(server, "/v1/traces"))
    result = export_saved_trace(clean, project_id=PROJECT, run_id="run_scripted")
    assert result["outbound"] is False
    assert server.bodies == []
    assert outbound == []
    assert [row["span_id"] for row in _stored(clean, "run_scripted")] == [RUN_SPAN]
