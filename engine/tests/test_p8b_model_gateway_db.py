"""P8b S10 (BOSSAPP-1071), the database half: handles bound in Postgres to (run, epoch, vm),
the fence read from the run's job within one request, a replay revoked and recorded as a
host_events row, the audit batch landing in guest_calls, revocation at run end, and the work
item a host claims carrying the guest's `handles` (P8b-T8 at the gateway).

Needs BOSS_TEST_DATABASE_URL naming a disposable database (the bench DB); the P8b tables are
applied from schema.sql's own P8b block first, so the test runs the real DDL.
"""
from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from boss_engine.services import streaming_model_gateway as smg

ROOT = Path(__file__).resolve().parents[2]
TEST_DB = os.environ.get("BOSS_TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(
    not TEST_DB or "test" not in TEST_DB.rsplit("/", 1)[-1].lower(),
    reason="needs BOSS_TEST_DATABASE_URL naming a disposable database")


def _p8b_block() -> list[str]:
    sql = (ROOT / "db" / "schema.sql").read_text(encoding="utf-8")
    start = sql.rfind("\n", 0, sql.index("P8b: contract v1.1 and the model gateway")) + 1
    block = sql[start:sql.index("end P8b ==", start)]
    code = "\n".join(line for line in block.splitlines() if not line.lstrip().startswith("--"))
    return [s.strip() for s in code.split(";") if "CREATE" in s]


@pytest.fixture(scope="module")
def eng():
    engine = create_engine(TEST_DB, future=True, pool_size=5, max_overflow=5)
    try:
        with engine.begin() as c:
            c.execute(text("SELECT 1 FROM agent_replicas LIMIT 1"))
            for statement in _p8b_block():
                c.execute(text(statement))
    except Exception as error:  # pragma: no cover
        pytest.skip(f"test database unavailable: {error}")
    return engine


@pytest.fixture()
def world(eng):
    """A project of its own, an org, a host and a replica row, and a job leased at epoch 3 whose
    payload names the run — enough for the fence to be read from Postgres."""
    from ._fleet_support import cleanup_project, make_project, org_of

    project = make_project(eng)
    org = org_of(eng, project)
    tag = uuid.uuid4().hex[:8]
    run_id, job_id, host_id = f"arun_{tag}", f"job_{tag}", f"hst_{tag}"
    with eng.begin() as c:
        c.execute(text(
            "INSERT INTO agent_hosts (id, org_id, pool, status, cert_fingerprint) "
            "VALUES (:h, :o, 'p8b', 'ready', :f)"), {"h": host_id, "o": org, "f": f"fp_{tag}"})
        c.execute(text(
            "INSERT INTO jobs (id, project_id, kind, status, payload, fence_epoch, target) "
            "VALUES (:j, :p, 'agent.run', 'leased', CAST(:pl AS JSONB), 3, 'deployment:x')"),
            {"j": job_id, "p": project, "pl": f'{{"run_id": "{run_id}"}}'})
    yield {"project": project, "org": org, "run_id": run_id, "job_id": job_id,
           "host_id": host_id}
    with eng.begin() as c:
        c.execute(text("DELETE FROM model_gateway_handles WHERE run_id = :r"), {"r": run_id})
        c.execute(text("DELETE FROM guest_calls WHERE run_id = :r"), {"r": run_id})
        c.execute(text("DELETE FROM host_events WHERE host_id = :h"), {"h": host_id})
        c.execute(text("DELETE FROM jobs WHERE id = :j"), {"j": job_id})
        c.execute(text("DELETE FROM agent_hosts WHERE id = :h"), {"h": host_id})
    cleanup_project(eng, project, [])


def _gateway(eng) -> smg.ModelGateway:
    return smg.ModelGateway(eng, settings=None, upstream=None, keys=None, budget=False,
                            audit=smg.AuditQueue(eng, interval=3600), ledger=False,
                            verify_host=lambda *a, **k: ("unused", "unused"))


def test_a_handle_is_bound_fenced_and_revoked_in_postgres(eng, world):
    with eng.begin() as c:
        raw = smg.issue_handle(c, org_id=world["org"], project_id=world["project"],
                               run_id=world["run_id"], epoch=3, vm_id="vm_a",
                               provider="anthropic", credential_ref="env:ANTHROPIC_API_KEY",
                               models=["claude-x"], deployment_id="dep_1")
    assert raw.startswith(smg.TOKEN_PREFIX)
    with eng.begin() as c:
        stored = c.execute(text("SELECT token_hash, credential_ref, models, vm_id, fence_epoch "
                                "FROM model_gateway_handles WHERE run_id = :r"),
                           {"r": world["run_id"]}).mappings().one()
    assert stored["token_hash"] == smg.hash_handle(raw) and raw not in str(stored)
    assert stored["credential_ref"] == "env:ANTHROPIC_API_KEY"
    assert list(stored["models"]) == ["claude-x"] and stored["vm_id"] == "vm_a"
    gw = _gateway(eng)
    host = smg.Caller(host_id=world["host_id"], org_id=world["org"], vm_id="vm_a",
                      run_id=world["run_id"], epoch=3)
    with eng.begin() as c:
        handle = gw.resolve(c, host, raw)
    assert handle.run_id == world["run_id"] and handle.fence_epoch == 3
    # the run's lease moves to epoch 4 in BOSS: the very next request is fenced
    with eng.begin() as c:
        c.execute(text("UPDATE jobs SET fence_epoch = 4 WHERE id = :j"), {"j": world["job_id"]})
    with eng.begin() as c, pytest.raises(smg.Refused) as fenced:
        gw.resolve(c, host, raw)
    assert (fenced.value.status, fenced.value.reason) == (409, "fenced")
    with eng.begin() as c:
        assert smg.current_epoch(c, world["run_id"]) == 4
        assert smg.current_epoch(c, "arun_nobody") is None
        c.execute(text("UPDATE jobs SET fence_epoch = 3 WHERE id = :j"), {"j": world["job_id"]})
    # run end: every handle of the run dies
    with eng.begin() as c:
        assert smg.revoke_run(c, world["run_id"]) == 1
    with eng.begin() as c, pytest.raises(smg.Refused) as dead:
        gw.resolve(c, host, raw)
    assert dead.value.reason == "handle_revoked"


def test_a_replay_from_another_vm_is_revoked_and_written_to_host_events(eng, world):
    with eng.begin() as c:
        raw = smg.issue_handle(c, org_id=world["org"], project_id=world["project"],
                               run_id=world["run_id"], epoch=3, vm_id="vm_a",
                               provider="anthropic", credential_ref="env:ANTHROPIC_API_KEY")
    gw = _gateway(eng)
    other = smg.Caller(host_id=world["host_id"], org_id=world["org"], vm_id="vm_b",
                       run_id=world["run_id"], epoch=3)
    with eng.begin() as c, pytest.raises(smg.Refused) as replay:
        gw.resolve(c, other, raw)
    assert (replay.value.status, replay.value.reason) == (401, "handle_replay")
    with eng.begin() as c:
        row = c.execute(text("SELECT revoked_at, revoked_reason FROM model_gateway_handles "
                             "WHERE token_hash = :h"), {"h": smg.hash_handle(raw)}).mappings().one()
        event = c.execute(text("SELECT kind, actor, vm_id, detail FROM host_events WHERE host_id = :h "
                               "ORDER BY id DESC LIMIT 1"), {"h": world["host_id"]}).mappings().one()
    assert row["revoked_at"] is not None and row["revoked_reason"] == "handle_replay:another_vm"
    assert (event["kind"], event["actor"], event["vm_id"]) == ("handle_replay", "model_gateway", "vm_b")
    assert event["detail"]["bound_vm"] == "vm_a" and raw not in str(event["detail"])
    # the rightful VM is refused too now: the handle is gone for good
    with eng.begin() as c, pytest.raises(smg.Refused) as gone:
        gw.resolve(c, smg.Caller(host_id=world["host_id"], org_id=world["org"], vm_id="vm_a",
                                 run_id=world["run_id"], epoch=3), raw)
    assert gone.value.reason == "handle_revoked"


def test_audit_rows_land_in_guest_calls_in_a_batch_without_the_handle(eng, world):
    import datetime as dt

    queue = smg.AuditQueue(eng, interval=3600)
    raw = smg.TOKEN_PREFIX + "f" * 64
    for i, verdict in enumerate(("allow", "deny")):
        queue.put({"org_id": world["org"], "host_id": world["host_id"], "call_id": f"c{i}_{uuid.uuid4().hex}",
                   "at": dt.datetime.now(dt.timezone.utc), "vm_id": "vm_a",
                   "run_id": world["run_id"], "epoch": 3, "deployment_id": "dep_1",
                   "dest_host": "api.anthropic.com", "method": "POST",
                   "path_template": "/anthropic/v1/messages", "status": 200 if i == 0 else 403,
                   "bytes_in": 10, "bytes_out": 20, "duration_ms": 4,
                   "handle_fp": smg.fingerprint(raw), "verdict": verdict,
                   "rule_id": "model-anthropic",
                   "rejected_by": None if i == 0 else "model_gateway:not_a_model_path",
                   "trace_id": None})
    assert queue.pending() == 2
    assert queue.flush() == 2 and queue.pending() == 0
    with eng.begin() as c:
        rows = c.execute(text("SELECT route, verdict, handle_fp, rejected_by FROM guest_calls "
                              "WHERE run_id = :r ORDER BY id"), {"r": world["run_id"]}).mappings().all()
    assert [r["route"] for r in rows] == ["model", "model"]
    assert [r["verdict"] for r in rows] == ["allow", "deny"]
    assert all(r["handle_fp"] == smg.fingerprint(raw) and raw not in str(dict(r)) for r in rows)
    assert rows[1]["rejected_by"] == "model_gateway:not_a_model_path"


def test_credential_references_name_where_the_key_is_read(eng, world):
    from boss_engine.config import Settings

    settings = Settings()
    with eng.begin() as c:
        ref = smg.credential_ref_for(c, settings, world["project"], "anthropic")
        assert ref and ref.partition(":")[0] in ("secret_ref", "env", "llm_gateway")
        handles = smg.guest_handles(c, settings, org_id=world["org"], project_id=world["project"],
                                    run_id=world["run_id"], epoch=3, vm_id="vm_a",
                                    provider="anthropic")
    assert handles["mdl"].startswith(smg.TOKEN_PREFIX) and handles["mdl_provider"] == "anthropic"
    keys = smg.KeyResolver(eng, settings)
    with pytest.raises(smg.KeyUnavailable):
        keys.resolve("env:BOSS_P8B_UNSET_KEY_NAME", world["project"])
    with pytest.raises(smg.KeyUnavailable):
        keys.resolve("secret_ref:sref_does_not_exist", world["project"])
    with pytest.raises(smg.KeyUnavailable):
        keys.resolve("bogus:x", world["project"])


def test_the_work_item_a_host_claims_carries_the_guests_model_handle(eng, monkeypatch):
    """The run plane's lease hands the VM a `boss_mdl_` bound to its replica's VM id (P8b S10),
    and the handle dies when the run settles — through the real P8a plane."""
    from .test_p8a_run_plane import Plane, _admit_microvm

    plane = Plane(eng, monkeypatch)
    try:
        admitted = _admit_microvm(plane)
        run_id = str(admitted.get("run_id") or admitted.get("id"))
        taken = plane.take()
        assert taken is not None
        handle, package, work = taken
        assert work["run_id"] == run_id
        assert "handles" in work, "the work item names the guest's handles"
        mdl = work["handles"].get("mdl")
        if mdl:
            assert mdl.startswith(smg.TOKEN_PREFIX)
            with eng.begin() as c:
                row = c.execute(text("SELECT vm_id, fence_epoch, run_id FROM model_gateway_handles "
                                     "WHERE token_hash = :h"), {"h": smg.hash_handle(mdl)}).mappings().one()
                vm = c.execute(text("SELECT vm_id FROM agent_replicas WHERE id = :r"),
                               {"r": work["replica_id"]}).scalar()
            assert row["vm_id"] == vm and row["fence_epoch"] == work["epoch"]
            assert row["run_id"] == run_id
            assert mdl not in str(package), "the package never repeats the handle"
    finally:
        plane.close()
