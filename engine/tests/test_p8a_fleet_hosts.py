"""P8a (BOSSAPP-1063): agent hosts against a FAKE boss-hostd, on Postgres — enrolment with a
one-time join token and a CSR, the host proof, revocation (P8a-T1: a revoked host's heartbeat is
refused, and so is its renewal), heartbeat attestation, the level-based desired-state watch, the
scheduler's hard anti-affinity, guest-call audit, and the placement rules (local unchanged,
per_run off while frozen, a microvm run never claimed by a local worker).
boss-agent-hosts-lld.md §3.1, §3.2, §3.14.1, §4.1."""
from __future__ import annotations

import os
import threading
import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from ._fleet_support import (FakeHost, cleanup_project, deploy_agent, fleet_app, join_token,
                             make_project, org_of, use_test_master_key)

TEST_DB = os.environ.get("BOSS_TEST_DATABASE_URL",
                         "postgresql+psycopg://boss:boss@localhost:5432/boss_test")


@pytest.fixture(scope="module")
def eng():
    engine = create_engine(TEST_DB, future=True)
    try:
        with engine.begin() as c:
            c.execute(text("SELECT 1 FROM agent_hosts LIMIT 1"))
    except Exception as error:  # pragma: no cover
        pytest.skip(f"test database unavailable: {error}")
    return engine


@pytest.fixture()
def world(eng, monkeypatch):
    use_test_master_key(monkeypatch)
    project = make_project(eng)
    hosts: list = []
    client = TestClient(fleet_app(eng))

    def host(**kw) -> FakeHost:
        h = FakeHost(client, **kw)
        hosts.append(h)
        return h

    yield {"project": project, "org": org_of(eng, project), "client": client, "host": host}
    cleanup_project(eng, project, [h.host_id for h in hosts if h.host_id])


def test_a_host_enrols_with_a_one_time_join_token_and_a_csr(eng, world):
    token = join_token(eng, world["org"], pool="gpu-free", labels={"rack": "r1"})
    host = world["host"]()
    out = host.enrol(token)
    assert out["host_id"].startswith("hst_") and out["vm_prefix"].endswith("/23")
    assert out["spiffe_id"] == f"spiffe://{out['trust_domain']}/host/{out['host_id']}"
    assert "BEGIN CERTIFICATE" in out["certificate_pem"] and out["svid_intermediate_pem"]
    assert out["heartbeat_seconds"] == 2

    from cryptography import x509

    cert = x509.load_pem_x509_certificate(out["certificate_pem"].encode())
    ca = x509.load_pem_x509_certificate(out["ca_pem"].encode())
    life = cert.not_valid_after_utc - cert.not_valid_before_utc
    assert 23.9 * 3600 < life.total_seconds() <= 24 * 3600 + 400, "a 24 h host certificate"
    cert.verify_directly_issued_by(ca)
    svid = x509.load_pem_x509_certificate(out["svid_intermediate_pem"].encode())
    assert svid.extensions.get_extension_for_class(x509.BasicConstraints).value.path_length == 0

    # The token is single-use: the second enrol is refused like an unknown one.
    again = world["host"]()
    refused = again.enrol(token, expect=401)
    assert refused["detail"]["error"] == "join_token_invalid"
    unknown = world["host"]().enrol("boss_hjt_nope", expect=401)
    assert unknown["detail"]["error"] == "join_token_invalid"
    with eng.begin() as c:
        row = c.execute(text("SELECT pool, labels, status FROM agent_hosts WHERE id = :h"),
                        {"h": host.host_id}).mappings().one()
        stored = c.execute(text("SELECT count(*) FROM agent_host_join_tokens "
                                "WHERE token_hash = :t"), {"t": token}).scalar()
    assert row["pool"] == "gpu-free" and row["labels"]["rack"] == "r1"
    assert stored == 0, "the join token is stored hashed, never raw"


def test_the_host_proof_is_bound_to_method_path_time_and_single_use(eng, world):
    host = world["host"]()
    host.enrol(join_token(eng, world["org"]))
    path = f"/v1/hosts/{host.host_id}/heartbeat"
    ok = host.call("POST", path, json={})
    assert ok.status_code == 200, ok.text

    headers = host.headers("POST", path)
    assert world["client"].post(path, json={}, headers=headers).status_code == 200
    replay = world["client"].post(path, json={}, headers=headers)
    assert replay.status_code == 401 and replay.json()["detail"]["error"] == "proof_replay"

    wrong_path = world["client"].post(path, json={},
                                      headers=host.headers("POST", "/v1/hosts/x/heartbeat"))
    assert wrong_path.json()["detail"]["error"] == "proof_htu"
    from boss_engine.services.fleet_ca import sign_proof

    stale = sign_proof(host.key, host.cert_pem, htm="POST", path=path, iat=int(time.time()) - 600)
    old = world["client"].post(path, json={}, headers={"Authorization": f"BossHost {stale}"})
    assert old.json()["detail"]["error"] == "proof_iat"
    assert world["client"].post(path, json={}).status_code == 401, "no credential, no host"

    # A host acts only for itself.
    other = world["host"](ed25519=True)
    other.enrol(join_token(eng, world["org"]))
    cross = other.call("POST", path, json={})
    assert cross.status_code == 403 and cross.json()["detail"]["error"] == "not_this_host"


def test_a_revoked_host_is_refused_its_heartbeat_and_its_renewal(eng, world):
    """P8a-T1."""
    from boss_engine.services import fleet
    from boss_engine.services.fleet_ca import make_csr

    host = world["host"]()
    host.enrol(join_token(eng, world["org"]))
    renewed = host.call("POST", f"/v1/hosts/{host.host_id}/renew",
                        json={"csr_pem": make_csr(host.key)})
    assert renewed.status_code == 200, renewed.text
    assert renewed.json()["certificate_pem"] != host.cert_pem
    with eng.begin() as c:
        fleet.revoke(c, org_id=world["org"], host_id=host.host_id, actor="ceo",
                     reason="decommissioned")
    beat = host.heartbeat()
    assert beat.status_code == 403 and beat.json()["detail"]["error"] == "host_revoked"
    renew = host.call("POST", f"/v1/hosts/{host.host_id}/renew",
                      json={"csr_pem": make_csr(host.key)})
    assert renew.status_code == 403, "revocation is refusing the renewal"
    host.cert_pem = renewed.json()["certificate_pem"]
    assert host.heartbeat().status_code == 403, "every certificate of the host is revoked"
    with eng.begin() as c:
        kinds = [r[0] for r in c.execute(text(
            "SELECT kind FROM host_events WHERE host_id = :h ORDER BY id"),
            {"h": host.host_id}).all()]
    assert kinds[:3] == ["enrolled", "renewed", "revoked"]


def test_a_heartbeat_records_the_attestation_facts_and_observed_state(eng, world):
    host = world["host"]()
    host.enrol(join_token(eng, world["org"]))
    out = host.heartbeat(facts={"kvm_live": True, "firecracker_live": True, "jailer_on": True,
                                "seccomp_default": True, "smt_off": True, "ksm_off": True,
                                "strict_secrets": True, "net_policy_ok": True,
                                "microcode": "0xb4"},
                         capacity={"vcpu": 64}, pressure={"mem": 0.2}, bgp_state="idle")
    assert out.status_code == 200, out.text
    body = out.json()
    assert body["desired_generation"] >= 1 and body["renew"] is False
    with eng.begin() as c:
        row = c.execute(text(
            "SELECT kvm_live, firecracker_live, jailer_on, seccomp_default, smt_off, ksm_off, "
            "strict_secrets, net_policy_ok, facts, capacity, bgp_state, last_heartbeat_at "
            "FROM agent_hosts WHERE id = :h"), {"h": host.host_id}).mappings().one()
    assert all(row[k] for k in ("kvm_live", "firecracker_live", "jailer_on", "seccomp_default",
                                "smt_off", "ksm_off", "strict_secrets", "net_policy_ok"))
    assert row["facts"]["microcode"] == "0xb4" and row["capacity"] == {"vcpu": 64}


def _hosts(world, eng, n):
    out = []
    for _ in range(n):
        h = world["host"]()
        h.enrol(join_token(eng, world["org"]))
        out.append(h)
    return out


def test_desired_state_is_level_based_with_a_generation_and_a_long_poll(eng, world):
    from boss_engine.services import fleet_scheduler

    host = _hosts(world, eng, 1)[0]
    first = host.desired(since=0)
    assert first["changed"] and first["vms"] == [], "a reconnect with since=0 is a full resync"
    quiet = host.desired(wait=0.3)
    assert quiet == {"changed": False, "host_id": host.host_id,
                     "generation": host.generation}

    seen: dict = {}
    waiter = threading.Thread(target=lambda: seen.update(host.desired(wait=10)))
    waiter.start()
    time.sleep(0.3)
    dep = deploy_agent(eng, world["project"], replicas=1)
    fleet_scheduler.reconcile(eng)
    waiter.join(12)
    assert seen.get("changed"), "the long-poll wakes on the new generation"
    vms = seen["vms"]
    assert len(vms) == 1 and vms[0]["deployment_id"] == dep["id"]
    assert vms[0]["state"] == "running" and vms[0]["vm_ip"]
    assert vms[0]["spiffe_id"].endswith(f"/host/{host.host_id}/vm/{vms[0]['vm_id']}")
    assert not any(k for k in str(vms).split("'") if k.startswith(("boss_", "lease_"))), \
        "no credential in the desired state"


def test_production_replicas_are_never_co_located(eng, world, monkeypatch):
    """Hard anti-affinity (§3.14.1): 3 prod replicas on 2 hosts place 2 and say degraded."""
    from boss_engine.services import agent_deployments, fleet_scheduler

    hosts = _hosts(world, eng, 2)
    dep = deploy_agent(eng, world["project"], env="staging", runtime="local")
    with eng.begin() as c:
        agent_deployments.deploy(c, world["project"], dep["agent_key"], "prod", actor="ceo",
                                 version=1)
        with pytest.raises(agent_deployments.DeploymentError, match="at least two"):
            agent_deployments.set_runtime(c, world["project"], dep["agent_key"], "prod",
                                          actor="ceo", runtime="microvm", replicas=1)
    with eng.begin() as c:
        prod = agent_deployments.set_runtime(c, world["project"], dep["agent_key"], "prod",
                                             actor="ceo", runtime="microvm", replicas=3)
    report = fleet_scheduler.reconcile(eng)
    with eng.begin() as c:
        rows = c.execute(text("SELECT host_id FROM agent_replicas WHERE deployment_id = :d "
                              "AND desired_state = 'running'"), {"d": prod["id"]}).all()
        state = c.execute(text("SELECT placement_state FROM agent_deployments WHERE id = :d"),
                          {"d": prod["id"]}).scalar()
    assert sorted(r[0] for r in rows) == sorted(h.host_id for h in hosts)
    assert state == "degraded"
    assert any(d["deployment_id"] == prod["id"] for d in report["degraded"])

    # A third host appears: the missing replica lands there, nowhere else.
    third = _hosts(world, eng, 1)[0]
    fleet_scheduler.reconcile(eng)
    with eng.begin() as c:
        rows = c.execute(text("SELECT host_id FROM agent_replicas WHERE deployment_id = :d "
                              "AND desired_state = 'running'"), {"d": prod["id"]}).all()
    assert len(rows) == 3 and len({r[0] for r in rows}) == 3 and third.host_id in {
        r[0] for r in rows}


def test_a_silent_host_turns_unreachable_and_its_replica_moves(eng, world):
    from boss_engine.services import fleet_scheduler

    a, b = _hosts(world, eng, 2)
    dep = deploy_agent(eng, world["project"], replicas=1)
    fleet_scheduler.reconcile(eng)
    with eng.begin() as c:
        holder = c.execute(text("SELECT host_id FROM agent_replicas WHERE deployment_id = :d "
                                "AND desired_state = 'running'"), {"d": dep["id"]}).scalar()
        c.execute(text("UPDATE agent_hosts SET last_heartbeat_at = now() - interval '1 minute' "
                       "WHERE id = :h"), {"h": holder})
    report = fleet_scheduler.reconcile(eng)
    assert holder in report["unreachable"]
    with eng.begin() as c:
        moved = c.execute(text("SELECT host_id FROM agent_replicas WHERE deployment_id = :d "
                               "AND desired_state = 'running'"), {"d": dep["id"]}).scalar()
    assert moved and moved != holder


def test_the_reconciler_is_leader_safe(eng, world):
    from boss_engine.services import fleet_scheduler

    with eng.begin() as c:
        c.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": fleet_scheduler.LOCK_KEY})
        assert fleet_scheduler.reconcile(eng) == {"skipped": "busy"}


def test_guest_calls_are_ingested_once_and_never_hold_a_handle(eng, world):
    host = _hosts(world, eng, 1)[0]
    calls = [{"call_id": "c1", "route": "model", "vm_id": "vm_1", "run_id": "arun_1",
              "epoch": 3, "dest_host": "models.boss", "method": "POST",
              "path_template": "/v1/messages", "status": 200, "bytes_in": 10, "bytes_out": 20,
              "duration_ms": 5, "handle_fp": "sha256:ab12", "verdict": "allow"},
             {"call_id": "c2", "route": "git", "verdict": "deny", "rejected_by": "route",
              "path_template": "/x?token=boss_egr_abcdefgh1234"},
             {"call_id": "c3", "route": "ftp"}]
    out = host.call("POST", f"/v1/hosts/{host.host_id}/guest-calls", json={"calls": calls})
    assert out.status_code == 200, out.text
    body = out.json()
    assert body["accepted"] == 1
    assert {r["reason"] for r in body["rejected"]} == {"raw_handle_refused", "route_unknown"}
    again = host.call("POST", f"/v1/hosts/{host.host_id}/guest-calls",
                      json={"calls": calls[:1]}).json()
    assert again["accepted"] == 0 and again["duplicates"] == 1, "a retried batch is idempotent"
    with eng.begin() as c:
        rows = c.execute(text("SELECT * FROM guest_calls WHERE host_id = :h"),
                         {"h": host.host_id}).mappings().all()
    assert len(rows) == 1 and "boss_egr_" not in str(dict(rows[0]))


def test_per_run_is_off_while_microvm_claims_are_frozen(eng, world, monkeypatch):
    from boss_engine.services import agent_deployments

    dep = deploy_agent(eng, world["project"], runtime="local")
    monkeypatch.setenv("BOSS_MICROVM_CLAIMS_FROZEN", "true")
    from boss_engine.config import get_settings

    get_settings.cache_clear()
    try:
        with eng.begin() as c, pytest.raises(agent_deployments.DeploymentError,
                                             match="per_run VMs are built but off"):
            agent_deployments.set_runtime(c, world["project"], dep["agent_key"], "dev",
                                          actor="ceo", runtime="microvm", vm_mode="per_run")
        with eng.begin() as c:  # replica is the activated mode, frozen or not
            out = agent_deployments.set_runtime(c, world["project"], dep["agent_key"], "dev",
                                                actor="ceo", runtime="microvm")
        assert out["runtime"] == "microvm" and out["vm_mode"] == "replica"
    finally:
        get_settings.cache_clear()


def test_a_local_deployment_is_unchanged_and_a_microvm_run_is_never_a_local_workers(eng, world):
    from boss_engine.config import get_settings
    from boss_engine.services import agent_deployments, thread_runs
    from boss_engine.services.jobs import JobLeaseService

    local = deploy_agent(eng, world["project"], runtime="local")
    assert local["runtime"] == "local" and local["vm_mode"] is None
    remote = deploy_agent(eng, world["project"], runtime="microvm")
    ids = {}
    with eng.begin() as c:
        for name, dep in (("local", local), ("remote", remote)):
            tid = thread_runs.ensure_thread(c, project_id=world["project"], actor="ceo",
                                            agent_id=dep["agent_key"])
            ids[name] = agent_deployments.admit_run(
                c, dep, thread_id=tid, messages=[{"role": "user", "content": "hi"}],
                trigger="api")
    with eng.begin() as c:
        payloads = {n: c.execute(text("SELECT payload FROM jobs WHERE id = :j"),
                                 {"j": a["job_id"]}).scalar() for n, a in ids.items()}
        fctx = c.execute(text("SELECT finalize_ctx FROM agent_runs WHERE id = :r"),
                         {"r": ids["local"]["run_id"]}).scalar()
    assert "runtime" not in payloads["local"] and "runtime" not in fctx, "local is unchanged"
    assert payloads["remote"]["runtime"] == "microvm"
    assert payloads["remote"]["deployment_id"] == remote["id"]
    claimed = []
    # Inside ONE transaction, rolled back: nothing another test or process owns is leased.
    with eng.connect() as c:
        while True:
            job = JobLeaseService(get_settings()).claim_job(c, "w-local", ["agent.run"], 60)
            if job is None:
                break
            claimed.append(job["id"])
        c.rollback()
    assert ids["local"]["job_id"] in claimed, "the local run is a local worker's, as before"
    assert ids["remote"]["job_id"] not in claimed, "a microvm run is never a local worker's"
