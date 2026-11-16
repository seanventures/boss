"""BOSSAPP-1265: a person edits a per-run outbound allowlist.

The proxy refuses a host that is not on that list. A secret named for the
run is injected on the outbound request and is not written into the receipt.
"""
from __future__ import annotations

import json
import os
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from boss_engine.services import run_egress

SECRET = "s3cr3t-for-run-1265-do-not-store"
URL = os.environ.get("DATABASE_URL", "")


pytestmark = pytest.mark.skipif(
    not URL or "test" not in URL.lower(),
    reason="DATABASE_URL must name a test database",
)


@pytest.fixture(scope="module")
def engine():
    eng = create_engine(URL, future=True)
    yield eng
    eng.dispose()


@pytest.fixture()
def project(engine):
    project_id = "p1265" + uuid.uuid4().hex[:10]
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO projects (id, workspace_id, name, slug) "
                "VALUES (:id, 'ws_boss', 'run egress', :slug)"
            ),
            {"id": project_id, "slug": project_id},
        )
    return project_id


def _receipt_text(connection, project_id: str) -> str:
    rows = connection.execute(
        text(
            """
            SELECT id::text, dedupe_key, effect_kind, status,
                   effect_scope::text, COALESCE(result::text, ''),
                   COALESCE(error::text, ''), COALESCE(provider_ref, '')
            FROM receipts
            WHERE project_id = :project_id
            """
        ),
        {"project_id": project_id},
    ).all()
    return "\n".join(" ".join(str(part) for part in row) for row in rows)


def _upstream(seen: list):
    def upstream(method, url, headers, body):
        seen.append({
            "method": method,
            "url": url,
            "headers": {str(k).lower(): v for k, v in dict(headers).items()},
            "body": body,
        })
        return {"status": 204, "body": b""}

    return upstream


def test_a_host_that_is_not_on_the_allowlist_is_refused(engine, project):
    """Dropping the host check would let the request leave."""
    seen: list = []
    with engine.begin() as connection:
        run_egress.set_allowlist(
            connection, project_id=project, run_id="run_off",
            hosts=["allowed.example"], actor="person:ada",
        )
        run_egress.put_run_secret(
            connection, project_id=project, run_id="run_off",
            value=SECRET, actor="person:ada",
        )
        with pytest.raises(run_egress.HostRefused) as caught:
            run_egress.proxy(
                connection, project_id=project, run_id="run_off",
                host="blocked.example", method="GET", path="/v1/items",
                upstream=_upstream(seen),
            )
        assert "blocked.example" in str(caught.value)
        assert seen == []
        assert SECRET not in _receipt_text(connection, project)


def test_the_run_secret_is_injected_and_kept_out_of_the_receipt(engine, project):
    """Forgetting the header, or copying the value into the receipt, fails this."""
    seen: list = []
    with engine.begin() as connection:
        run_egress.set_allowlist(
            connection, project_id=project, run_id="run_ok",
            hosts=["allowed.example"], actor="person:ada",
        )
        stored = run_egress.put_run_secret(
            connection, project_id=project, run_id="run_ok",
            value=SECRET, actor="person:ada",
        )
        assert stored["secret_name"] == "run:run_ok"
        assert SECRET not in json.dumps(stored)
        result = run_egress.proxy(
            connection, project_id=project, run_id="run_ok",
            host="allowed.example", method="POST", path="/v1/items",
            upstream=_upstream(seen),
        )
        assert len(seen) == 1
        assert seen[0]["url"] == "https://allowed.example/v1/items"
        assert seen[0]["method"] == "POST"
        assert seen[0]["headers"]["x-boss-run-secret"] == SECRET
        assert seen[0]["headers"]["x-boss-run-secret-name"] == "run:run_ok"
        assert SECRET not in json.dumps(result)
        body = _receipt_text(connection, project)
        assert SECRET not in body
        assert "allowed.example" in body
        assert "run:run_ok" in body
        assert result["status"] == 204


def test_a_repeat_of_the_same_request_does_not_call_out_again(engine, project):
    """A second identical request must not spend the outbound call twice."""
    seen: list = []
    upstream = _upstream(seen)
    with engine.begin() as connection:
        run_egress.set_allowlist(
            connection, project_id=project, run_id="run_once",
            hosts=["allowed.example"], actor="person:ada",
        )
        run_egress.put_run_secret(
            connection, project_id=project, run_id="run_once",
            value=SECRET, actor="person:ada",
        )
        first = run_egress.proxy(
            connection, project_id=project, run_id="run_once",
            host="allowed.example", method="GET", path="/once",
            upstream=upstream,
        )
    with engine.begin() as connection:
        second = run_egress.proxy(
            connection, project_id=project, run_id="run_once",
            host="allowed.example", method="GET", path="/once",
            upstream=upstream,
        )
        rows = connection.execute(
            text("SELECT count(*) FROM receipts WHERE project_id = :project_id"),
            {"project_id": project},
        ).scalar()
    assert seen[0]["headers"]["x-boss-run-secret"] == SECRET
    assert len(seen) == 1
    assert first["receipt_id"] == second["receipt_id"]
    assert rows == 1
    assert SECRET not in json.dumps(second)


def test_a_person_can_replace_the_allowlist_and_the_old_host_is_refused(engine, project):
    """An edit that only appends would still let the removed host through."""
    seen: list = []
    with engine.begin() as connection:
        run_egress.set_allowlist(
            connection, project_id=project, run_id="run_edit",
            hosts=["old.example", "kept.example"], actor="person:ada",
        )
        listed = run_egress.get_allowlist(
            connection, project_id=project, run_id="run_edit",
        )
        assert listed["hosts"] == ["old.example", "kept.example"]
        run_egress.set_allowlist(
            connection, project_id=project, run_id="run_edit",
            hosts=["kept.example"], actor="person:ada",
        )
        run_egress.put_run_secret(
            connection, project_id=project, run_id="run_edit",
            value=SECRET, actor="person:ada",
        )
        with pytest.raises(run_egress.HostRefused):
            run_egress.proxy(
                connection, project_id=project, run_id="run_edit",
                host="old.example", method="GET", path="/",
                upstream=_upstream(seen),
            )
        assert seen == []
        ok = run_egress.proxy(
            connection, project_id=project, run_id="run_edit",
            host="kept.example", method="GET", path="/",
            upstream=_upstream(seen),
        )
        assert ok["status"] == 204
        assert seen[0]["headers"]["x-boss-run-secret-name"] == "run:run_edit"


def test_an_agent_cannot_edit_the_allowlist_or_the_secret(engine, project):
    """Accepting an agent actor would let a run widen its own allowlist."""
    with engine.begin() as connection:
        with pytest.raises(run_egress.RunEgressError) as caught:
            run_egress.set_allowlist(
                connection, project_id=project, run_id="run_agent",
                hosts=["allowed.example"], actor="agent:coder",
            )
        assert caught.value.status == 403
        with pytest.raises(run_egress.RunEgressError):
            run_egress.put_run_secret(
                connection, project_id=project, run_id="run_agent",
                value=SECRET, actor="agent:coder",
            )


def test_an_upstream_failure_is_visible_and_does_not_record_the_secret(engine, project):
    """Swallowing the upstream error would look like a successful call."""
    def upstream(method, url, headers, body):
        raise RuntimeError(f"upstream down {SECRET}")

    with engine.begin() as connection:
        run_egress.set_allowlist(
            connection, project_id=project, run_id="run_fail",
            hosts=["allowed.example"], actor="person:ada",
        )
        run_egress.put_run_secret(
            connection, project_id=project, run_id="run_fail",
            value=SECRET, actor="person:ada",
        )
        with pytest.raises(run_egress.RunEgressError) as caught:
            run_egress.proxy(
                connection, project_id=project, run_id="run_fail",
                host="allowed.example", method="GET", path="/down",
                upstream=upstream,
            )
        assert caught.value.status == 502
        assert SECRET not in str(caught.value)
        assert SECRET not in _receipt_text(connection, project)


def test_http_edits_the_list_refuses_an_off_list_host_and_hides_the_secret(engine, project):
    """The mounted routes are how a person edits the list and sends the call."""
    from apps.api.app.routers.gap_1265 import router

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    put = client.put(
        "/v1/runs/run_http/outbound-allowlist",
        json={"project_id": project, "actor": "person:ada", "hosts": ["api.example.com"]},
    )
    assert put.status_code == 200
    assert put.json()["hosts"] == ["api.example.com"]
    secret = client.put(
        "/v1/runs/run_http/egress-secret",
        json={"project_id": project, "actor": "person:ada", "value": SECRET},
    )
    assert secret.status_code == 200
    assert secret.json()["secret_name"] == "run:run_http"
    assert SECRET not in secret.text
    refused = client.post(
        "/v1/runs/run_http/egress",
        json={"project_id": project, "host": "other.example", "method": "GET", "path": "/x"},
    )
    assert refused.status_code == 403
    assert "other.example" in refused.text
    assert SECRET not in refused.text
    allowed = client.post(
        "/v1/runs/run_http/egress",
        json={
            "project_id": project, "host": "api.example.com",
            "method": "GET", "path": "/x",
        },
    )
    assert allowed.status_code == 200
    assert SECRET not in allowed.text
    with engine.begin() as connection:
        assert SECRET not in _receipt_text(connection, project)
        assert "api.example.com" in _receipt_text(connection, project)
