"""A repeated press runs once, on every write: one key, one execution.

The same duplicate came back on a new button four times: 2026-08-22 the operator and the
autopilot started one run twice; 2026-08-30 "Open preview happened twice"; 2026-09-10 duplicate
chat actions; 2026-10-02 "Stop and run again" pressed twice started two workflows. Each was
fixed for one endpoint. The rule now lives in middleware for every POST/PUT/PATCH/DELETE under
/v1/, and these tests hold it to its promises on the three actions the owner named when he said
"you'll never trigger a run" after idempotency: starting a workflow, retrying a step, starting a
preview. Each must still WORK with a key — and work again after a failed first attempt.
"""
import threading
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from apps.api.app.main import app
from boss_engine.services import workflows

PROJECT = "boss-dogfood"


def _key() -> str:
    return f"press-{uuid4()}"


@pytest.fixture
def workflow_key(engine):
    key = f"once_{uuid4().hex[:10]}"
    with engine.begin() as connection:
        definition = workflows.create_definition(
            connection, project_id=PROJECT, workflow_key=key, name="Once", description=None,
            actor="ceo", namespace=workflows.PRODUCT_NAMESPACE)
        workflows.publish_version(
            connection, project_id=PROJECT, definition_id=definition["id"], actor="ceo",
            spec={"entry_step": "decide", "steps": [{
                "id": "decide", "kind": "capability",
                "binding": {"typed_decision": {"question": "Go?",
                                               "options": {"yes": "Go", "no": "Stop"}},
                            "inputs": {"state": {"from_input": "ticket"}}}}]})
    return key


def _runs_of(engine, workflow_key) -> int:
    with engine.begin() as connection:
        return connection.execute(text(
            "SELECT count(*) FROM workflow_runs r JOIN workflow_definitions d "
            "ON d.id = r.workflow_definition_id WHERE d.workflow_key = :k"),
            {"k": workflow_key}).scalar_one()


def _start(client, workflow_key, key=None, ticket="x"):
    # Deliberately NO dedupe_key in the body: the engine would mint a fresh one per request,
    # so only the header can tie a repeat to its first press.
    return client.post("/v1/delivery-workflows/start", json={
        "project_id": PROJECT, "actor": "ceo", "workflow_key": workflow_key,
        "input": {"ticket": ticket}}, headers={"Idempotency-Key": key} if key else {})


class TestStartingAWorkflow:
    def test_a_keyed_start_still_starts_a_run(self, client, engine, workflow_key):
        response = _start(client, workflow_key, _key())
        assert response.status_code == 200, response.text
        assert response.json()["run"]["id"]
        assert _runs_of(engine, workflow_key) == 1

    def test_the_same_key_again_returns_the_first_response_and_starts_nothing(
            self, client, engine, workflow_key):
        key = _key()
        first = _start(client, workflow_key, key)
        second = _start(client, workflow_key, key)
        assert first.status_code == second.status_code == 200, second.text
        assert second.json() == first.json()
        assert second.headers.get("Idempotency-Replayed") == "true"
        assert "Idempotency-Replayed" not in first.headers
        assert _runs_of(engine, workflow_key) == 1

    def test_a_new_press_is_a_new_key_and_a_new_run(self, client, engine, workflow_key):
        """Idempotency must not swallow intent: two presses are two runs."""
        assert _start(client, workflow_key, _key()).status_code == 200
        assert _start(client, workflow_key, _key()).status_code == 200
        assert _runs_of(engine, workflow_key) == 2

    def test_concurrent_duplicates_run_the_handler_once(self, client, engine, workflow_key):
        key = _key()
        results = []

        def press():
            results.append(_start(client, workflow_key, key))

        threads = [threading.Thread(target=press) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert _runs_of(engine, workflow_key) == 1
        statuses = sorted(r.status_code for r in results)
        assert set(statuses) <= {200, 409}, [r.text for r in results]
        assert 200 in statuses
        for refused in (r for r in results if r.status_code == 409):
            assert "idempotency_key_in_progress" in refused.json()["detail"]
        assert len({r.json()["run"]["id"] for r in results if r.status_code == 200}) == 1

    def test_the_same_key_with_a_different_body_is_refused(self, client, engine, workflow_key):
        key = _key()
        assert _start(client, workflow_key, key, ticket="x").status_code == 200
        reused = _start(client, workflow_key, key, ticket="y")
        assert reused.status_code == 422, reused.text
        assert "idempotency_key_reused" in reused.json()["detail"]
        assert _runs_of(engine, workflow_key) == 1

    def test_without_the_header_nothing_changes(self, client, engine, workflow_key):
        """Backwards compatible: the worker, MCP clients and scripts send no key, and each of
        their requests runs exactly as it did before."""
        first, second = _start(client, workflow_key), _start(client, workflow_key)
        assert first.status_code == second.status_code == 200
        assert first.json()["run"]["id"] != second.json()["run"]["id"]
        assert _runs_of(engine, workflow_key) == 2
        with engine.begin() as connection:
            assert connection.execute(text(
                "SELECT count(*) FROM api_idempotency_keys")).scalar_one() == 0

    def test_a_malformed_key_is_refused_before_anything_runs(self, client, engine, workflow_key):
        response = _start(client, workflow_key, "has spaces in it")
        assert response.status_code == 400, response.text
        assert _runs_of(engine, workflow_key) == 0


@pytest.fixture
def failed_run(engine):
    key = f"retry_{uuid4().hex[:10]}"
    with engine.begin() as c:
        definition = workflows.create_definition(
            c, project_id=PROJECT, workflow_key=key, name="Retry", description=None,
            actor="ceo", namespace=workflows.PRODUCT_NAMESPACE)
        version = workflows.publish_version(
            c, project_id=PROJECT, definition_id=definition["id"], actor="ceo",
            spec={"entry_step": "research", "steps": [
                {"id": "research", "kind": "capability",
                 "binding": {"capability": "research", "inputs": {"seed": "x"}}, "next": []}]})
        run = workflows.start_run(
            c, project_id=PROJECT, actor="ceo", input_data={},
            dedupe_key=f"dk_{uuid4().hex[:10]}", workflow_version_id=version["id"])
        c.execute(text("UPDATE workflow_step_runs SET status='failed', last_error='died' "
                       "WHERE workflow_run_id=:r AND step_id='research'"), {"r": run["id"]})
        c.execute(text("UPDATE workflow_runs SET status='failed', error='died' WHERE id=:r"),
                  {"r": run["id"]})
    return run["id"]


def _retried(engine, run_id) -> int:
    with engine.begin() as c:
        return c.execute(text("SELECT count(*) FROM events WHERE entity_id=:r "
                              "AND event_type='workflow.step_retried'"),
                         {"r": run_id}).scalar_one()


class TestRetryingAStep:
    def _retry(self, client, run_id, key):
        return client.post(f"/v1/delivery-workflows/runs/{run_id}/retry-step", json={
            "project_id": PROJECT, "actor": "ceo", "step_id": "research"},
            headers={"Idempotency-Key": key})

    def test_a_keyed_retry_still_retries_and_a_double_press_retries_once(
            self, client, engine, failed_run):
        key = _key()
        first = self._retry(client, failed_run, key)
        assert first.status_code == 200, first.text
        assert first.json()["status"] == "ready"
        again = self._retry(client, failed_run, key)
        # The double press gets the first press's answer, not the 409 "already ready" a second
        # real retry would earn — and the step was retried once.
        assert again.status_code == 200, again.text
        assert again.json() == first.json()
        assert again.headers.get("Idempotency-Replayed") == "true"
        assert _retried(engine, failed_run) == 1

    def test_a_refusal_that_depends_on_the_moment_is_not_pinned_to_the_key(
            self, client, engine, failed_run):
        """409 means "not now". Remembering it would make the same press fail for a day."""
        assert self._retry(client, failed_run, _key()).status_code == 200
        key = _key()
        assert self._retry(client, failed_run, key).status_code == 409
        with engine.begin() as c:
            assert c.execute(text("SELECT count(*) FROM api_idempotency_keys WHERE "
                                  "idempotency_key=:k"), {"k": key}).scalar_one() == 0


@pytest.fixture
def built_run(engine):
    run_id = f"ctr_{uuid4().hex[:12]}"
    with engine.begin() as c:
        c.execute(text(
            "INSERT INTO code_task_runs (id, project_id, title, status, phase, created_at, "
            "updated_at) VALUES (:id, :p, 'Idempotent preview', 'completed', 'done', now(), "
            "now())"), {"id": run_id, "p": PROJECT})
    return run_id


@pytest.fixture
def preview_starts(monkeypatch):
    """Count real starts; the first call can be made to fail the way a cold host does."""
    from apps.api.app.services import preview_service

    calls = {"n": 0, "fail_first": False}

    def fake_start(_engine, _settings, run_id, **_kwargs):
        calls["n"] += 1
        if calls["fail_first"] and calls["n"] == 1:
            raise RuntimeError("docker daemon not answering")
        return {"id": f"prv_{calls['n']}", "run_id": run_id, "status": "starting"}

    monkeypatch.setattr(preview_service, "start_preview_async", fake_start)
    return calls


class TestStartingAPreview:
    def _open(self, client, run_id, key):
        return client.post(f"/v1/code-tasks/{run_id}/preview", json={"actor": "ceo"},
                           headers={"Idempotency-Key": key})

    def test_open_preview_pressed_twice_starts_one_preview(
            self, client, built_run, preview_starts):
        """2026-08-30, "Open preview happened twice"."""
        key = _key()
        first = self._open(client, built_run, key)
        second = self._open(client, built_run, key)
        assert first.status_code == 200, first.text
        assert second.json() == first.json() == {
            "id": "prv_1", "run_id": built_run, "status": "starting"}
        assert preview_starts["n"] == 1

    def test_a_first_attempt_that_crashed_does_not_stop_the_same_press_working(
            self, engine, built_run, preview_starts):
        """The owner's fear, exactly: a failure must not pin the key so nothing ever starts."""
        preview_starts["fail_first"] = True
        client = TestClient(app, raise_server_exceptions=False)
        key = _key()
        crashed = self._open(client, built_run, key)
        assert crashed.status_code == 500
        retried = self._open(client, built_run, key)
        assert retried.status_code == 200, retried.text
        assert retried.json()["status"] == "starting"
        assert preview_starts["n"] == 2


def test_a_worker_lease_call_is_never_replayed(client):
    """The lease protocol has its own fencing; a stored heartbeat would lie to a worker."""
    from apps.api.app import idempotency

    assert idempotency.exclusion_reason("POST", "/v1/worker/jobs/lease")
    assert idempotency.exclusion_reason("POST", "/v1/previews/prv_1/web/api/orders")
    assert idempotency.exclusion_reason("POST", "/v1/delivery-workflows/start") is None
    # Feedback is BOSS's own endpoint, reached from a preview: it is governed like any write.
    assert idempotency.exclusion_reason("POST", "/v1/previews/prv_1/feedback") is None
