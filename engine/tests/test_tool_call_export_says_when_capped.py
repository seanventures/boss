"""The tool-call export says when its row cap hid older rows.

Exactly max_rows matches is not a cut, and neither is a shorter file. Truncated
is true only when a further matching row exists. The HTTP response sets that on
X-Truncated (1 or 0) and still streams at most the cap.
"""
from __future__ import annotations

import json
import os
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from apps.api.app.routers.tool_calls import build_tool_calls_router
from boss_engine.services import tool_call_audit as audit

URL = os.environ.get(
    "BOSS_TOOL_EXPORT_DATABASE_URL",
    "postgresql+psycopg://boss:boss@127.0.0.1:55945/boss_tool_export_test",
)


@pytest.fixture(scope="module")
def engine():
    assert "test" in URL.lower()
    admin = create_engine(URL, future=True)
    schema = "t_tool_export_" + uuid4().hex[:8]
    with admin.begin() as c:
        c.execute(text(f'CREATE SCHEMA "{schema}"'))
    eng = create_engine(URL, future=True, connect_args={"options": f"-csearch_path={schema}"})
    raw = eng.raw_connection()
    try:
        cur = raw.cursor()
        cur.execute(audit.SCHEMA_SQL)
        cur.execute(
            "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = "
            f"'tool_call_audit_no_update' AND tgrelid = '\"{schema}\".tool_call_audit'::regclass) "
            "THEN CREATE TRIGGER tool_call_audit_no_update BEFORE UPDATE OR DELETE ON "
            f"tool_call_audit FOR EACH ROW EXECUTE FUNCTION {schema}.tool_call_audit_append_only(); "
            "END IF; END $$;")
        raw.commit()
    finally:
        raw.close()
    yield eng
    eng.dispose()
    with admin.begin() as c:
        c.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
    admin.dispose()


def _seed(engine, project: str, n: int, *, outcome: str = "succeeded") -> list[str]:
    ids = []
    for i in range(n):
        with engine.begin() as c:
            ids.append(audit.record_tool_call(
                c, project_id=project, run_id="run-1", agent="atlas", principal="usr_1",
                server_id="srv", tool="fetch", arguments={"url": "https://example.com"},
                decision="allow", matched_policy_ids=["allow"], decision_id=None,
                outcome=outcome if i else "refused", latency_ms=i))
    return ids


def _ids(export) -> list[str]:
    return [row["id"] for row in export]


def test_export_marks_truncated_only_when_a_further_row_exists(engine, monkeypatch):
    monkeypatch.setattr(audit, "_EXPORT_PAGE", 2)
    project = "p-" + uuid4().hex[:8]
    _seed(engine, project, 5)
    with engine.begin() as c:
        newest = [i["id"] for i in audit.query_tool_calls(c, project_id=project, limit=50)["items"]]

    with engine.begin() as c:
        exact = audit.export_tool_calls(c, project_id=project, max_rows=5)
        exact_ids = _ids(exact)
    assert exact.truncated is False
    assert exact_ids == newest

    with engine.begin() as c:
        short = audit.export_tool_calls(c, project_id=project, max_rows=9)
        short_ids = _ids(short)
    assert short.truncated is False and short_ids == newest

    with engine.begin() as c:
        capped = audit.export_tool_calls(c, project_id=project, max_rows=3)
        capped_ids = _ids(capped)
    # A full page (2) plus a short remainder must still notice the row past the cap.
    assert capped.truncated is True
    assert capped_ids == newest[:3]

    with engine.begin() as c:
        refused = audit.export_tool_calls(
            c, project_id=project, max_rows=1, **audit.tool_call_filters(outcome="refused"))
        refused_ids = _ids(refused)
    assert refused.truncated is False and len(refused_ids) == 1


def test_the_export_response_says_when_the_cap_hid_older_rows(engine, monkeypatch):
    monkeypatch.setattr(audit, "EXPORT_MAX_ROWS", 3)
    monkeypatch.setattr(audit, "_EXPORT_PAGE", 2)
    project = "p-" + uuid4().hex[:8]
    _seed(engine, project, 3)
    app = FastAPI()
    app.include_router(build_tool_calls_router(engine=engine, require_reader=lambda *_a: None))
    client = TestClient(app)

    exact = client.get("/v1/tool-calls/export", params={"project_id": project})
    assert exact.status_code == 200, exact.text
    assert exact.headers["x-truncated"] == "0"
    assert len([line for line in exact.text.splitlines() if line.strip()]) == 3

    with engine.begin() as c:
        audit.record_tool_call(
            c, project_id=project, run_id="run-1", agent="atlas", principal="usr_1",
            server_id="srv", tool="fetch", arguments={"url": "https://example.com"},
            decision="allow", matched_policy_ids=["allow"], decision_id=None,
            outcome="succeeded", latency_ms=9)
    hidden = client.get("/v1/tool-calls/export", params={"project_id": project})
    assert hidden.status_code == 200, hidden.text
    assert hidden.headers["x-truncated"] == "1"
    lines = [json.loads(line) for line in hidden.text.splitlines() if line.strip()]
    assert len(lines) == 3
    assert all("id" in line for line in lines)
