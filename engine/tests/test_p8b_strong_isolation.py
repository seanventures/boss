"""P8b-11 (BOSSAPP-1078): AP-D11 `strong_isolation(deployment)`, from attested facts only
(boss-agent-hosts-lld.md §3.17.4, D5, D15, D17, D21).

  * THE TRUTH TABLE (pure, no database): true only when every fact holds; each fact flipped alone
    makes it false and names why;
  * ON POSTGRES, against fake boss-hostds: a write-capable agent's A2A turns on only when two
    Firecracker hosts attest (jailer, seccomp, strict secrets, net policy, fresh heartbeat), the
    image is signed and active, and both replicas hold serving certificates; `runtime: local`
    stays refused, whatever the hosts say;
  * LAPSE → CLOSE (P8b-T6): a stale heartbeat or a fact turning false (KVM masked) closes the
    endpoint on the very next request, and the directors' feed drops the lapsed host's member;
    the scheduler's sweep records `attestation_lapsed`.
"""
from __future__ import annotations

import os
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from boss_engine.services.strong_isolation import evaluate

from ._fleet_support import (FakeHost, cleanup_project, fleet_app, join_token, make_project,
                             org_of, own_pool, use_test_master_key)

TEST_DB = os.environ.get("BOSS_TEST_DATABASE_URL",
                         "postgresql+psycopg://boss:boss@localhost:5432/boss_test")
GOOD_FACTS = {"kvm_live": True, "firecracker_live": True, "jailer_on": True,
              "seccomp_default": True, "strict_secrets": True, "net_policy_ok": True,
              "smt_off": True, "ksm_off": True, "swap_off": True}


# ── the truth table ────────────────────────────────────────────────────────────────────────

def _facts(**over):
    dep = {"id": "dep_1", "runtime": "microvm", "status": "live", "vm_mode": "replica",
           "expose": {"a2a": True}, "image_digest": "sha256:img", "strict_secrets": True}
    host = {"backend": "firecracker", "status": "ready", "revoked_at": None, "facts": {},
            "heartbeat_age": 1.0, **GOOD_FACTS}
    replica = {"id": "rep_1", "host_id": "h1", "vm_mode": "replica",
               "image_digest": "sha256:img", "tainted": False}
    images = {"sha256:img": {"status": "active", "signature": "cosign:sig"}}
    state = {"dep": dep, "host": host, "replica": replica, "images": images, "frozen": False,
             "certified": {"rep_1"}, "replicas": None}
    for key, value in over.items():
        part, _, field = key.partition("__")
        if field:
            state[part] = {**state[part], field: value}
        else:
            state[part] = value
    replicas = state["replicas"] if state["replicas"] is not None else [state["replica"]]
    return evaluate(state["dep"], replicas, {"h1": state["host"]}, frozen=state["frozen"],
                    images=state["images"], certified=state["certified"])


def test_all_facts_hold_then_strong_isolation_holds():
    verdict = _facts()
    assert verdict.ok and verdict.reasons == ()


@pytest.mark.parametrize("flip,why", [
    ({"dep__runtime": "local"}, "runtime is local"),
    ({"frozen": True}, "frozen"),
    ({"dep__status": "retired"}, "deployment is retired"),
    ({"dep__vm_mode": None}, "vm_mode is unset"),
    ({"dep__expose": {"a2a": True, "public": True}}, "per_run"),
    ({"replicas": []}, "no serving replica"),
    ({"dep__strict_secrets": False}, "strict secrets"),
    ({"replica__vm_mode": "per_run"}, "vm_mode per_run is not the deployment's replica"),
    ({"host__heartbeat_age": 10.0}, "heartbeat"),
    ({"host__heartbeat_age": None}, "heartbeat"),
    ({"host__backend": "process"}, "not a microVM"),
    ({"host__firecracker_live": False}, "firecracker_live is false"),
    ({"host__kvm_live": False}, "kvm_live is false"),
    ({"host__jailer_on": False}, "jailer_on is false"),
    ({"host__seccomp_default": False}, "seccomp_default is false"),
    ({"host__strict_secrets": False}, "strict_secrets is false"),
    ({"host__net_policy_ok": False}, "net_policy_ok is false"),
    ({"host__facts": {"fallback_tap": True}}, "fallback tap"),
    ({"host__revoked_at": "2026-11-14"}, "revoked"),
    ({"host__status": "unreachable"}, "unreachable"),
    ({"dep__image_digest": None}, "image"),
    ({"images": {"sha256:img": {"status": "candidate", "signature": "cosign:sig"}}}, "image"),
    ({"images": {"sha256:img": {"status": "active", "signature": None}}}, "image"),
    ({"replica__image_digest": "sha256:other"}, "image"),
    ({"certified": set()}, "no valid serving certificate"),
    ({"replica__tainted": True}, "tainted"),
])
def test_any_single_fact_failing_makes_it_false_and_says_why(flip, why):
    verdict = _facts(**flip)
    assert not verdict.ok
    assert any(why in r for r in verdict.reasons), verdict.reasons


def test_per_run_meets_d5_for_public_callers_and_cordoned_hosts_still_attest():
    assert _facts(dep__vm_mode="per_run", replica__vm_mode="per_run",
                  dep__expose={"a2a": True, "public": True}).ok
    assert _facts(host__status="cordoned").ok


def test_there_is_no_switch(monkeypatch):
    """No environment variable, setting or argument can force it: the module reads none."""
    import inspect

    from boss_engine.services import a2a_server, strong_isolation

    monkeypatch.setenv("BOSS_STRONG_ISOLATION", "1")
    monkeypatch.setenv("BOSS_GOVERNED_SANDBOX_PROVIDER", "microvm")
    assert a2a_server.strong_isolation() is False
    assert "environ" not in inspect.getsource(strong_isolation)


# ── on Postgres ────────────────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def eng():
    engine = create_engine(TEST_DB, future=True)
    try:
        with engine.begin() as c:
            c.execute(text("SELECT isolation_ok FROM agent_hosts LIMIT 1"))
    except Exception as error:  # pragma: no cover
        pytest.skip(f"test database unavailable: {error}")
    return engine


class World:
    """A write-capable agent deployed to prod on microVMs, two attesting Firecracker hosts."""

    def __init__(self, eng, monkeypatch):
        from boss_engine.agents import versions
        from boss_engine.services import (agent_ca, agent_deployments, agent_keys,
                                          fleet_scheduler)

        use_test_master_key(monkeypatch)
        agent_ca.reset_cache()
        monkeypatch.setattr(fleet_scheduler, "frozen", lambda settings=None: False)
        self.eng, self.monkeypatch = eng, monkeypatch
        self.project = make_project(eng)
        self.org = org_of(eng, self.project)
        self.client = TestClient(fleet_app(eng))
        self.pool = own_pool()
        self.key = f"writer_{uuid.uuid4().hex[:6]}"
        self.digest = f"sha256:{uuid.uuid4().hex}{uuid.uuid4().hex}"
        self.director = f"dir_{uuid.uuid4().hex[:10]}"
        self.vip = f"198.51.100.{(uuid.uuid4().int % 200) + 20}"
        with eng.begin() as c:
            versions.create_from_spec(c, self.project, {
                "apiVersion": "boss.agent/v1", "kind": "Agent",
                "metadata": {"key": self.key, "name": "Refund bot"},
                "spec": {"mission": "Refund orders.", "prompts": {"system": "v1"},
                         "limits": {"max_steps": 4},
                         "tools": {"skills": ["github.create_pr_sandbox"]}}},
                actor="ceo", source="operator")
            agent_keys.create_key(c, self.project, self.key, actor="ceo")
            c.execute(text("INSERT INTO vm_images (digest, status, signature) "
                           "VALUES (:d, 'active', 'cosign:sig')"), {"d": self.digest})
            agent_deployments.deploy(c, self.project, self.key, "prod", actor="ceo", version=1)
            self.dep = agent_deployments.set_runtime(
                c, self.project, self.key, "prod", actor="ceo", runtime="microvm", replicas=2,
                image_digest=self.digest, placement={"pool": self.pool})
            c.execute(text("INSERT INTO director_nodes (id, org_id, status, last_heartbeat_at) "
                           "VALUES (:i, :o, 'ready', now())"), {"i": self.director, "o": self.org})
        self.hosts = [self.new_host(), self.new_host()]
        fleet_scheduler.reconcile(eng)
        with eng.begin() as c:
            c.execute(text("UPDATE agent_deployments SET vip = CAST(:v AS INET) WHERE id = :d"),
                      {"v": self.vip, "d": self.dep["id"]})
        self.replicas = {}
        for host in self.hosts:
            host.serve_all()
            assert host.heartbeat(facts=GOOD_FACTS).status_code == 200
            (rid,) = list(host.vms)
            self.replicas[host.host_id] = rid
            assert self.cert(host, rid).status_code == 200

    def new_host(self) -> FakeHost:
        host = FakeHost(self.client, backend="firecracker")
        host.enrol(join_token(self.eng, self.org, pool=self.pool), facts=dict(GOOD_FACTS))
        return host

    def cert(self, host: FakeHost, replica: str):
        from cryptography.hazmat.primitives.asymmetric import ec

        from boss_engine.services.fleet_ca import make_csr

        return host.call("POST", "/v1/run-plane/replica/cert",
                         json={"csr_pem": make_csr(ec.generate_private_key(ec.SECP256R1()))},
                         headers={"X-Boss-Replica": replica})

    def agent(self, c) -> dict:
        from boss_engine.services import a2a_server

        agent_id = c.execute(text("SELECT agent_id FROM roles WHERE project_id = :p AND "
                                  "role_key = :r"), {"p": self.project, "r": self.key}).scalar()
        return a2a_server.resolve(c, agent_id)

    def refusal(self):
        from boss_engine.services import a2a_server

        with self.eng.begin() as c:
            return a2a_server.exposure_refusal(c, self.agent(c))

    def members(self) -> set:
        from boss_engine.services import fleet_directors

        with self.eng.begin() as c:
            desired = fleet_directors.desired(c, self.director)
        vip = next(v for v in desired["vips"] if v["vip"] == self.vip)
        return {m["replica_id"] for m in vip["members"]}

    def close(self):
        with self.eng.begin() as c:
            c.execute(text("DELETE FROM director_nodes WHERE id = :i"), {"i": self.director})
            c.execute(text("DELETE FROM vm_images WHERE digest = :d"), {"d": self.digest})
        cleanup_project(self.eng, self.project, [h.host_id for h in self.hosts if h.host_id])


@pytest.fixture()
def world(eng, monkeypatch):
    w = World(eng, monkeypatch)
    yield w
    w.close()


def test_a_write_capable_agent_opens_on_attested_microvms_and_closes_on_a_lapse(world):
    from boss_engine.services import a2a_server, fleet_scheduler
    from boss_engine.services.strong_isolation import verdict

    with world.eng.begin() as c:
        held = verdict(c, world.dep["id"])
        assert held.ok, held.reasons
        out = a2a_server.set_exposure(c, world.project, world.key, "prod", enabled=True,
                                      actor="ceo")
        assert out["a2a"] is True
        assert c.execute(text("SELECT isolation_required FROM agent_deployments WHERE id = :d"),
                         {"d": world.dep["id"]}).scalar() is True
    assert world.refusal() is None
    assert world.members() == set(world.replicas.values())

    # LAPSE 1: host B's heartbeat goes stale → closed on the very next request, member withdrawn
    a, b = world.hosts
    with world.eng.begin() as c:
        c.execute(text("UPDATE agent_hosts SET last_heartbeat_at = now() - interval '11 seconds'"
                       " WHERE id = :h"), {"h": b.host_id})
    why = world.refusal()
    assert why and "heartbeat" in why
    assert world.members() == {world.replicas[a.host_id]}

    # recovered: a fresh, attesting heartbeat reopens it (facts only — nobody flips a switch)
    assert b.heartbeat(facts=GOOD_FACTS).status_code == 200
    assert world.refusal() is None and world.members() == set(world.replicas.values())

    # LAPSE 2 (P8b-T6): KVM masked on host A → closes within one request
    assert a.heartbeat(facts={**GOOD_FACTS, "kvm_live": False,
                              "firecracker_live": False}).status_code == 200
    why = world.refusal()
    assert why and "kvm_live is false" in why
    assert world.members() == {world.replicas[b.host_id]}
    with world.eng.begin() as c:
        assert c.execute(text("SELECT isolation_ok FROM agent_hosts WHERE id = :h"),
                         {"h": a.host_id}).scalar() is False

    # a revoked serving certificate also ends it
    assert a.heartbeat(facts=GOOD_FACTS).status_code == 200
    assert world.refusal() is None
    from boss_engine.services import agent_ca

    with world.eng.begin() as c:
        agent_ca.revoke(c, replica_id=world.replicas[a.host_id], reason="test")
    assert "no valid serving certificate" in (world.refusal() or "")
    assert world.members() == {world.replicas[b.host_id]}

    # THE SWEEP: a host that simply stops beating loses its stored attestation (and the
    # directors are bumped), recorded as `attestation_lapsed`
    with world.eng.begin() as c:
        c.execute(text("UPDATE agent_hosts SET last_heartbeat_at = now() - interval '11 seconds'"
                       " WHERE id = :h"), {"h": b.host_id})
    fleet_scheduler.reconcile(world.eng)
    with world.eng.begin() as c:
        assert c.execute(text("SELECT isolation_ok FROM agent_hosts WHERE id = :h"),
                         {"h": b.host_id}).scalar() is False
        assert c.execute(text("SELECT count(*) FROM host_events WHERE host_id = :h AND "
                              "kind = 'attestation_lapsed'"), {"h": b.host_id}).scalar() == 1
    assert world.refusal() and world.members() == set()


def test_runtime_local_is_unchanged(world):
    from boss_engine.services import a2a_server, agent_deployments
    from boss_engine.services.agent_deployments import DeploymentError
    from boss_engine.services.strong_isolation import strong_isolation

    with world.eng.begin() as c:
        agent_deployments.set_runtime(c, world.project, world.key, "prod", actor="ceo",
                                      runtime="local")
        assert strong_isolation(c, world.dep["id"]) is False
        with pytest.raises(DeploymentError) as refused:
            a2a_server.set_exposure(c, world.project, world.key, "prod", enabled=True,
                                    actor="ceo")
        assert "read-only" in str(refused.value) and "runtime is local" in str(refused.value)


def test_the_kill_switch_closes_it(world, monkeypatch):
    from boss_engine.services import fleet_scheduler
    from boss_engine.services.strong_isolation import verdict

    monkeypatch.setattr(fleet_scheduler, "frozen", lambda settings=None: True)
    with world.eng.begin() as c:
        held = verdict(c, world.dep["id"])
    assert not held.ok and any("frozen" in r for r in held.reasons)
