"""A Community investigation runs as governed work (BOSSAPP-571's doorway, item 3).

`investigate` failed in Community with "Live investigations are unavailable in Community until
model calls use durable worker leases, budget reservations, and reconciled receipts". A
read-only investigation is now a durable `investigation.run` job; its one model call is spent
under the worker's lease of that job, a budget reservation and a model lease, with a receipt
settled at the actual cost — Discovery's governed stage (discovery_spend.DiscoveryStageSpend),
not a parallel one. A fixing investigation, and every host or data write, stay closed: those
need governed effect records.

Postgres is real; only the Claude CLI and the sandbox are faked.
"""

from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import text

from apps.api.app import main as api_main
from apps.api.app.services import discovery_spend
from boss_engine.services import code_agent, debug_session, sandbox
from boss_engine.services.jobs import JobLeaseService


PROJECT = "boss-dogfood"
VERDICT = ("The pool is exhausted.\n```json\n{\"cause\": \"the connection pool is exhausted\", "
           "\"evidence\": [\"pool timeout\"], \"fix\": \"raise the pool size\"}\n```")


class _Agent:
    calls: list[str] = []
    fail_with: Exception | None = None

    def __init__(self, settings=None) -> None:
        self.settings = settings

    def _invoke(self, prompt, cwd, **kwargs):
        _Agent.calls.append(kwargs.get("seat") or "")
        if _Agent.fail_with is not None:
            error, _Agent.fail_with = _Agent.fail_with, None
            raise error
        assert kwargs.get("read_only") is True, "a Community investigation only reads"
        return {"result": VERDICT, "num_turns": 4, "usage": {"input_tokens": 10}}

    @staticmethod
    def _usage(data, settings):
        return {"total_tokens": 10}, 1_500


class _Sandbox:
    def __init__(self, settings, task_id):
        self.path = Path("/tmp") / f"inv-test-{task_id}"

    def __enter__(self):
        self.path.mkdir(parents=True, exist_ok=True)
        return self

    def __exit__(self, *exc):
        return False

    def provision(self, *a, **k):
        raise RuntimeError("no repository in this test")


@pytest.fixture
def governed(client, engine, monkeypatch):
    """Community, with a model source the investigation can lease, and no real CLI."""
    monkeypatch.setattr(api_main.settings, "boss_edition", "community")
    suffix = uuid4().hex[:8].upper()
    created = client.post("/v1/secret-refs", json={
        "project_id": PROJECT, "ref_kind": "env", "ref_key": f"BOSS_TEST_INVESTIGATION_{suffix}",
        "provider": "openai", "secret_kind": "api_token", "status": "active", "actor": "ceo",
    })
    assert created.status_code == 201, created.text
    bound = client.post("/v1/entitlements/sources/openai_api/credential-ref", json={
        "project_id": PROJECT, "secret_ref_id": created.json()["id"], "actor": "ceo"})
    assert bound.status_code == 200, bound.text
    monkeypatch.setattr(discovery_spend, "RESEARCH_SOURCE_ID", "openai_api")
    monkeypatch.setattr(code_agent, "ClaudeCodeAgent", _Agent)
    monkeypatch.setattr(sandbox, "EphemeralSandbox", _Sandbox)
    monkeypatch.setattr(debug_session, "_repo_for", lambda *a, **k: ("", ""))
    _Agent.calls, _Agent.fail_with = [], None
    with engine.begin() as connection:
        connection.execute(text("UPDATE investigations SET status = 'failed' WHERE project_id = :p "
                                "AND status IN ('queued', 'running')"), {"p": PROJECT})
    return _Agent


def _open(engine, **kw) -> dict:
    with engine.begin() as connection:
        return debug_session.open_investigation(
            connection, project_id=PROJECT, question="why are orders failing?", actor="ceo",
            settings=api_main.settings, **kw)


def _lease(engine, investigation_id: str) -> dict:
    with engine.begin() as connection:
        job_id = connection.execute(text(
            "SELECT job_id FROM investigations WHERE id = :i"), {"i": investigation_id}
        ).scalar_one()
        lease = JobLeaseService(api_main.settings).lease_owned_job(
            connection, project_id=PROJECT, job_id=job_id, worker_id="worker-inv-test",
            lease_ttl_seconds=600)
    return {"job_id": job_id, "worker_id": "worker-inv-test", "lease_token": lease["lease_token"]}


def _receipts(engine, investigation_id: str) -> list[str]:
    with engine.connect() as connection:
        return [row[0] for row in connection.execute(text(
            "SELECT status FROM receipts WHERE dedupe_key LIKE :p ORDER BY created_at"),
            {"p": f"model-gateway:{PROJECT}:investigation:{investigation_id}:%"})]


def test_a_read_only_investigation_runs_under_lease_reservation_and_receipt(engine, governed):
    opened = _open(engine)
    assert opened["ok"], opened
    inv = opened["investigation_id"]
    with engine.connect() as connection:
        job = connection.execute(text(
            "SELECT j.kind, j.status FROM investigations i JOIN jobs j ON j.id = i.job_id "
            "WHERE i.id = :i"), {"i": inv}).mappings().one()
    assert job["kind"] == "investigation.run" and job["status"] == "queued"
    assert governed.calls == [], "nothing runs on the request; the worker runs it"

    out = debug_session.run_investigation(engine, api_main.settings, investigation_id=inv,
                                          lease=_lease(engine, inv))
    assert out["ok"] and out["status"] == "completed", out
    assert out["verdict"]["cause"] == "the connection pool is exhausted"
    assert governed.calls == ["investigation"]
    assert _receipts(engine, inv) == ["succeeded"], "one settled receipt for its one call"
    with engine.connect() as connection:
        reservations = connection.execute(text(
            "SELECT status FROM budget_reservations WHERE reason = :r"),
            {"r": f"Investigation {inv} stage investigate"}).scalars().all()
        settled = connection.execute(text(
            "SELECT count(*) FROM events WHERE entity_id = :i "
            "AND event_type = 'investigation.stage_settled'"), {"i": inv}).scalar_one()
    assert reservations == ["consumed"], reservations
    assert settled == 1


def test_a_call_that_may_have_been_charged_is_never_made_again(engine, governed):
    inv = _open(engine)["investigation_id"]
    governed.fail_with = TimeoutError("read timed out")
    first = debug_session.run_investigation(engine, api_main.settings, investigation_id=inv,
                                            lease=_lease(engine, inv))
    assert first["ok"] is False and "reconcile" in first["error"].lower(), first
    assert _receipts(engine, inv) == ["in_progress"]
    with engine.connect() as connection:
        decisions = connection.execute(text(
            "SELECT count(*) FROM decisions WHERE project_id = :p AND question = :q"),
            {"p": PROJECT, "q": f"Reconcile Investigation {inv} stage investigate"}).scalar_one()
    assert decisions == 1, "a person reconciles it"


def test_community_still_refuses_a_fixing_investigation_and_an_unleased_run(engine, governed):
    with pytest.raises(debug_session.InvestigationExecutionBlocked, match="effect records"):
        _open(engine, allow_fix=True)
    inv = _open(engine)["investigation_id"]
    out = debug_session.run_investigation(engine, api_main.settings, investigation_id=inv)
    assert out["status"] == "blocked" and "lease" in out["error"], out
    assert governed.calls == [], "no model call without the worker's lease"
    host = debug_session.run_on_investigation_host(
        host="any", command="touch /tmp/x", project_id=PROJECT, allow_write=True)
    assert host["ok"] is False and "effect records" in host["error"]


def test_the_worker_hands_the_api_its_lease(monkeypatch):
    from apps.worker.boss_worker import control_plane

    sent = {}

    class _Client:
        def post(self, path, json):
            sent.update(path=path, json=json)

            class _R:
                def raise_for_status(self):
                    return None

                def json(self):
                    return {"ok": True}
            return _R()

    plane = control_plane.ControlPlaneClient.__new__(control_plane.ControlPlaneClient)
    plane._client = _Client()
    plane.run_investigation({"investigation_id": "inv_x", "job_id": "job_1",
                             "worker_id": "w", "lease_token": "t"})
    assert sent["path"] == "/v1/investigations/inv_x/run"
    assert sent["json"] == {"job_id": "job_1", "worker_id": "w", "lease_token": "t"}
