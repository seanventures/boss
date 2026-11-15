"""P8b-10 (BOSSAPP-1077): the IPVS directors' control-plane side against a FAKE boss-lbd and a
FAKE boss-hostd, on Postgres — a director enrols with a role-bound join token and a CSR (client +
server auth certificate), heartbeats and takes the director lease per VRRP group (at most one
holder; the lease ends a split brain), long-polls a desired state that lists only SERVING replicas
behind each VIP with the route per host and the VRRP grouping both sides agree on; a VIP is
allocated from the pool on a production deployment's first placement (D8); the host's desired
state carries the directors (GRE sources) and the withdraw-push endpoints; the host's
PUT/DELETE /v1/vips/… is the durable record and wakes the directors; a host cannot use director
routes and a director cannot use host routes.
boss-agent-hosts-lld.md §3.14.2, §3.14.4, §3.1; docs/infra/HOST_API.md §9."""
from __future__ import annotations

import os
import threading
import time
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from ._fleet_support import (FakeHost, cleanup_project, deploy_agent, fleet_app, join_token,
                             make_project, org_of, own_pool, use_test_master_key)

TEST_DB = os.environ.get("BOSS_TEST_DATABASE_URL",
                         "postgresql+psycopg://boss:boss@localhost:5432/boss_test")


@pytest.fixture(scope="module")
def eng():
    engine = create_engine(TEST_DB, future=True)
    try:
        with engine.begin() as c:
            c.execute(text("SELECT 1 FROM director_nodes LIMIT 1"))
    except Exception as error:  # pragma: no cover
        pytest.skip(f"test database unavailable: {error}")
    return engine


class FakeDirector(FakeHost):
    """boss-lbd on the wire: the same proof, its own routes."""

    def enrol(self, token: str, *, address: str = "192.0.2.10", expect: int = 201,
              role: str = "director") -> dict:  # type: ignore[override]
        from boss_engine.services.fleet_ca import make_csr

        body = {"join_token": token, "csr_pem": make_csr(self.key, "boss-lbd"),
                "hostname": self.hostname, "backend": "process", "role": role,
                "address": address}
        response = self.client.post("/v1/hosts/enrol", json=body)
        assert response.status_code == expect, response.text
        if expect == 201:
            self.enrolment = response.json()
            self.host_id = self.enrolment["host_id"]
            self.cert_pem = self.enrolment["certificate_pem"]
        return response.json()

    def heartbeat(self, **body):  # type: ignore[override]
        return self.call("POST", f"/v1/directors/{self.host_id}/heartbeat",
                         json={"groups_master": [], **body})

    def desired(self, since=None, wait: float = 0.0) -> dict:  # type: ignore[override]
        response = self.call("GET", f"/v1/directors/{self.host_id}/desired",
                             params={"since": self.generation if since is None else since,
                                     "wait": wait})
        assert response.status_code == 200, response.text
        out = response.json()
        if out.get("changed"):
            self.generation = int(out["generation"])
        return out


def cleanup_directors(engine, ids: list, org: str) -> None:
    with engine.begin() as c:
        if ids:
            c.execute(text("DELETE FROM director_leases WHERE holder = ANY(:d)"), {"d": ids})
            c.execute(text("DELETE FROM agent_certs WHERE host_id = ANY(:d)"), {"d": ids})
            c.execute(text("DELETE FROM host_events WHERE host_id = ANY(:d)"), {"d": ids})
            c.execute(text("DELETE FROM director_nodes WHERE id = ANY(:d)"), {"d": ids})
        c.execute(text("DELETE FROM vip_pool WHERE org_id = :o AND cidr = CAST(:c AS CIDR)"),
                  {"o": org, "c": POOL})


POOL = "203.0.113.64/28"


@pytest.fixture()
def world(eng, monkeypatch):
    use_test_master_key(monkeypatch)
    project = make_project(eng)
    org = org_of(eng, project)
    hosts: list = []
    directors: list = []
    client = TestClient(fleet_app(eng))

    def host(**kw) -> FakeHost:
        h = FakeHost(client, **kw)
        hosts.append(h)
        return h

    def director(**kw) -> FakeDirector:
        d = FakeDirector(client, **kw)
        directors.append(d)
        return d

    # The bench DB is shared: every test's org-wide director set must be its own, so retire any
    # director another run left behind in this org before starting.
    with eng.begin() as c:
        c.execute(text("UPDATE director_nodes SET revoked_at = now(), status = 'retired' "
                       "WHERE org_id = :o AND revoked_at IS NULL"), {"o": org})
        c.execute(text("DELETE FROM director_leases WHERE org_id = :o"), {"o": org})
    yield {"project": project, "org": org, "client": client, "host": host, "director": director,
           "pool": own_pool()}
    cleanup_project(eng, project, [h.host_id for h in hosts if h.host_id])
    cleanup_directors(eng, [d.host_id for d in directors if d.host_id], org)


def test_a_director_enrols_with_a_role_bound_join_token(eng, world):
    from cryptography import x509
    from cryptography.x509.oid import ExtendedKeyUsageOID

    # A host token never enrols a director, and a director token never enrols a host.
    host_token = join_token(eng, world["org"], pool=world["pool"])
    refused = world["director"]().enrol(host_token, expect=400)
    assert refused["detail"]["error"] == "join_token_role"
    dir_token = join_token(eng, world["org"], role="director")
    as_host = world["host"]().enrol(dir_token, expect=400)
    assert as_host["detail"]["error"] == "join_token_role"

    a = world["director"]()
    out = a.enrol(dir_token, address="192.0.2.10")
    assert out["host_id"].startswith("dir_") and out["role"] == "director"
    assert out["endpoints"]["desired"] == f"/v1/directors/{out['host_id']}/desired"
    cert = x509.load_pem_x509_certificate(out["certificate_pem"].encode())
    ekus = cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
    assert ExtendedKeyUsageOID.CLIENT_AUTH in ekus and ExtendedKeyUsageOID.SERVER_AUTH in ekus, \
        "a director both calls the control plane and serves host pushes"
    san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert "192.0.2.10" in [str(ip) for ip in san.get_values_for_type(x509.IPAddress)]
    assert cert.subject.get_attributes_for_oid(x509.NameOID.COMMON_NAME)[0].value == out["host_id"]
    with eng.begin() as c:
        row = c.execute(text("SELECT kind FROM agent_certs WHERE host_id = :d"),
                        {"d": a.host_id}).scalar()
        assert row == "director"
    # The same token is spent.
    world["director"]().enrol(dir_token, expect=401)


def test_host_and_director_routes_refuse_each_other(eng, world):
    host = world["host"]()
    host.enrol(join_token(eng, world["org"], pool=world["pool"]))
    director = world["director"]()
    director.enrol(join_token(eng, world["org"], role="director"))
    # A director on a host route, a host on a director route.
    r = director.call("POST", f"/v1/hosts/{host.host_id}/heartbeat", json={})
    assert r.status_code == 401 and r.json()["detail"]["error"] == "host_credential_required"
    r = host.call("POST", f"/v1/directors/{director.host_id}/heartbeat", json={})
    assert r.status_code == 401 and r.json()["detail"]["error"] == "director_credential_required"
    # A director for another director.
    other = world["director"]()
    other.enrol(join_token(eng, world["org"], role="director"))
    r = other.call("POST", f"/v1/directors/{director.host_id}/heartbeat", json={})
    assert r.status_code == 403 and r.json()["detail"]["error"] == "not_this_director"
    # Its own: fine.
    r = director.heartbeat()
    assert r.status_code == 200 and r.json()["director_id"] == director.host_id


def test_the_director_lease_has_one_holder_per_group(eng, world):
    a, b = world["director"](), world["director"]()
    a.enrol(join_token(eng, world["org"], role="director"), address="192.0.2.10")
    b.enrol(join_token(eng, world["org"], role="director"), address="192.0.2.11")
    # A is MASTER of group 0, B of group 1: each gets its lease.
    la = a.heartbeat(groups_master=[0]).json()["leases"]
    lb = b.heartbeat(groups_master=[1]).json()["leases"]
    assert la == {"0": a.host_id} and lb == {"0": a.host_id, "1": b.host_id}
    # Split brain: B also claims group 0 while A holds it: refused (A stays the holder), and B
    # learns who holds it, which its track script turns into FAULT.
    lb = b.heartbeat(groups_master=[0, 1]).json()["leases"]
    assert lb["0"] == a.host_id and lb["1"] == b.host_id
    # A renews its own lease freely.
    assert a.heartbeat(groups_master=[0]).json()["leases"]["0"] == a.host_id
    # An expired lease is taken over.
    with eng.begin() as c:
        c.execute(text("UPDATE director_leases SET expires_at = now() - interval '1 second' "
                       "WHERE holder = :a AND vrrp_group = 0"), {"a": a.host_id})
    assert b.heartbeat(groups_master=[0, 1]).json()["leases"]["0"] == b.host_id
    # B is no longer MASTER of group 0 (A took over, B's keepalived went BACKUP/FAULT): B's lease
    # of group 0 is released at once — A does not wait out the TTL.
    assert b.heartbeat(groups_master=[1]).json()["leases"].get("0") is None
    assert a.heartbeat(groups_master=[0]).json()["leases"]["0"] == a.host_id
    b.heartbeat(groups_master=[0, 1])  # restore for the row check below
    with eng.begin() as c:
        rows = c.execute(text("SELECT groups_master, keepalived_live, status FROM director_nodes "
                              "WHERE id = :b"), {"b": b.host_id}).mappings().one()
    assert list(rows["groups_master"]) == [0, 1] and rows["status"] == "ready"


def test_desired_state_lists_serving_real_servers_routes_and_agreed_groups(eng, world):
    from boss_engine.services import fleet_directors, fleet_scheduler

    org, pool = world["org"], world["pool"]
    with eng.begin() as c:
        fleet_directors.add_vip_pool(c, org_id=org, cidr=POOL)
    a, b = world["director"](), world["director"]()
    a.enrol(join_token(eng, org, role="director"), address="192.0.2.10")
    b.enrol(join_token(eng, org, role="director"), address="192.0.2.11")
    h1, h2 = world["host"](), world["host"]()
    for h in (h1, h2):
        h.enrol(join_token(eng, org, pool=pool))
    # A production microVM deployment with 2 replicas: a VIP is allocated on first placement.
    dep = deploy_agent(eng, world["project"], env="prod", replicas=2, pool=pool)
    fleet_scheduler.reconcile(eng)
    with eng.begin() as c:
        vip, ports = c.execute(text("SELECT host(vip), vip_ports FROM agent_deployments WHERE id = :d"),
                               {"d": dep["id"]}).one()
    assert vip == "203.0.113.65" and list(ports) == [443], "the first free VIP of the pool, port 443"
    # Hosts report their address with the heartbeat; the directors need it for the route.
    h1.heartbeat(address="198.51.100.1", replicas=[])
    h2.heartbeat(address="198.51.100.2", replicas=[])
    # Sides follow id order (both directors compute the same order from the same ids).
    first, second = sorted([a, b], key=lambda d: d.host_id)
    # Nothing serving yet: the VIP exists with no members.
    d = a.desired(since=0)
    assert d["changed"] and d["peer"]["id"] == b.host_id
    assert d["self"]["side"] == ("A" if a is first else "B")
    assert [v["vip"] for v in d["vips"]] == [vip] and d["vips"][0]["members"] == []
    assert d["vips"][0]["scheduler"] == "rr" and d["vip_pools"] == [POOL]
    routes = {h["host_id"]: (h["address"], h["vm_prefix"]) for h in d["hosts"]}
    assert routes[h1.host_id][0] == "198.51.100.1" and routes[h1.host_id][1].endswith("/23")
    assert d["groups"] == [{"group": 0, "vrid": 1, "master": first.host_id, "vips": [vip]}], \
        "the control plane decides who is MASTER of each group, so A and B agree"
    # The hosts' desired state names the directors (GRE sources) and the push endpoints.
    hd = h1.desired(since=0)
    assert sorted(hd["directors"]) == ["192.0.2.10", "192.0.2.11"]
    assert [p["id"] for p in hd["director_push"]] == [first.host_id, second.host_id]
    assert hd["director_push"][0]["port"] == 7446
    assert all(v["vip"] == vip for v in hd["vms"])
    # Both hosts turn their replica serving: the feed carries both members, with their weight,
    # and the director's generation moved (a waiting long-poll wakes).
    gen_before = a.generation
    woke: dict = {}

    def wait_for_change() -> None:
        woke["out"] = a.desired(since=gen_before, wait=5)

    t = threading.Thread(target=wait_for_change)
    t.start()
    time.sleep(0.3)
    h1.serve_all()
    t.join(timeout=8)
    assert woke["out"]["changed"] and woke["out"]["generation"] > gen_before
    h2.serve_all()
    d = b.desired(since=0)
    members = {m["replica_id"]: m for m in d["vips"][0]["members"]}
    assert len(members) == 2 and all(m["weight"] == 1 for m in members.values())
    assert {m["host_id"] for m in members.values()} == {h1.host_id, h2.host_id}
    assert all(m["vm_ip"] for m in members.values())
    assert d["self"]["side"] == ("B" if b is second else "A")
    assert d["groups"][0]["master"] == first.host_id
    # One replica leaves `serving`: it leaves the feed at once.
    rid = next(iter(h1.vms))
    h1.vms[rid]["state"] = "unhealthy"
    h1.state_gen += 1
    r = h1.call("POST", f"/v1/hosts/{h1.host_id}/observed",
                json={"replicas": [h1.vms[rid]], "state_gen": h1.state_gen})
    assert r.status_code == 200, r.text
    d2 = a.desired(since=0)
    assert d2["generation"] > d["generation"]
    assert [m["replica_id"] for m in d2["vips"][0]["members"]] == [r for r in members if r != rid]
    # A dev deployment never takes a VIP.
    dev = deploy_agent(eng, world["project"], env="dev", replicas=1, pool=pool)
    fleet_scheduler.reconcile(eng)
    with eng.begin() as c:
        assert c.execute(text("SELECT vip FROM agent_deployments WHERE id = :d"),
                         {"d": dev["id"]}).scalar() is None
    # A retired director leaves the pair: the other becomes MASTER of every group.
    with eng.begin() as c:
        fleet_directors.retire(c, org_id=org, director_id=a.host_id, actor="ceo")
    d3 = b.desired(since=0)
    assert d3["peer"] is None and d3["groups"][0]["master"] == b.host_id
    assert a.heartbeat().status_code == 403, "a retired director is refused"


def test_the_host_records_a_withdraw_at_the_control_plane_and_wakes_directors(eng, world):
    from boss_engine.services import fleet_directors, fleet_scheduler

    org, pool = world["org"], world["pool"]
    with eng.begin() as c:
        fleet_directors.add_vip_pool(c, org_id=org, cidr=POOL)
    a = world["director"]()
    a.enrol(join_token(eng, org, role="director"), address="192.0.2.10")
    h1, h2 = world["host"](), world["host"]()
    for h in (h1, h2):
        h.enrol(join_token(eng, org, pool=pool))
    dep = deploy_agent(eng, world["project"], env="prod", replicas=2, pool=pool)
    fleet_scheduler.reconcile(eng)
    h1.serve_all()
    h2.serve_all()
    with eng.begin() as c:
        vip = c.execute(text("SELECT host(vip) FROM agent_deployments WHERE id = :d"),
                        {"d": dep["id"]}).scalar()
    rid = next(iter(h1.vms))
    gen = a.desired(since=0)["generation"]
    with eng.begin() as c:
        vm_ip = c.execute(text("SELECT host(vm_ip) FROM agent_replicas WHERE id = :r"),
                          {"r": rid}).scalar()
    # Drain: weight 0 recorded, directors woken.
    r = h1.call("PUT", f"/v1/vips/{vip}/members/{rid}",
                json={"vm_ip": vm_ip, "weight": 0, "reason": "process_exit"})
    assert r.status_code == 200, r.text
    assert r.json()["weight"] == 0 and "_wake" not in r.json()
    d = a.desired(since=0)
    assert d["generation"] > gen
    assert {m["replica_id"]: m["weight"] for m in d["vips"][0]["members"]}[rid] == 0
    # Back: weight 2.
    r = h1.call("PUT", f"/v1/vips/{vip}/members/{rid}", json={"vm_ip": vm_ip, "weight": 2})
    assert r.status_code == 200
    assert {m["replica_id"]: m["weight"] for m in a.desired(since=0)["vips"][0]["members"]}[rid] == 2
    # Another host's replica, a wrong VIP, a wrong vm_ip: refused.
    r = h2.call("PUT", f"/v1/vips/{vip}/members/{rid}", json={"vm_ip": vm_ip, "weight": 0})
    assert r.status_code == 403 and r.json()["detail"]["error"] == "replica_not_on_host"
    r = h1.call("PUT", f"/v1/vips/203.0.113.99/members/{rid}", json={"vm_ip": vm_ip, "weight": 0})
    assert r.status_code == 409 and r.json()["detail"]["error"] == "vip_mismatch"
    r = h1.call("PUT", f"/v1/vips/{vip}/members/{rid}", json={"vm_ip": "10.9.9.9", "weight": 0})
    assert r.status_code == 409 and r.json()["detail"]["error"] == "vm_ip_mismatch"
    # DELETE (confirmed death) is weight 0 as the record; the serving state comes from /observed.
    r = h1.call("DELETE", f"/v1/vips/{vip}/members/{rid}", params={"vm_ip": vm_ip, "reason": "process_exit"})
    assert r.status_code == 200 and r.json()["weight"] == 0
    with eng.begin() as c:
        kinds = [k for (k,) in c.execute(text(
            "SELECT kind FROM host_events WHERE host_id = :h AND kind LIKE 'vip_member_%' "
            "ORDER BY id"), {"h": h1.host_id}).all()]
    assert kinds == ["vip_member_withdrawn", "vip_member_registered", "vip_member_withdrawn"]
    # Latest decision wins: register (seq 20) then a late withdraw (seq 10) changes nothing.
    r = h1.call("PUT", f"/v1/vips/{vip}/members/{rid}", json={"vm_ip": vm_ip, "weight": 3, "seq": 20})
    assert r.status_code == 200 and r.json()["weight"] == 3
    r = h1.call("PUT", f"/v1/vips/{vip}/members/{rid}", json={"vm_ip": vm_ip, "weight": 0, "seq": 10})
    assert r.status_code == 200 and r.json()["stale"] is True
    with eng.begin() as c:
        assert c.execute(text("SELECT rs_weight FROM agent_replicas WHERE id = :r"),
                         {"r": rid}).scalar() == 3
    # D8: the deployment leaves microvm (or retires) → its VIP returns to the pool.
    with eng.begin() as c:
        c.execute(text("UPDATE agent_deployments SET status = 'retired' WHERE id = :d"),
                  {"d": dep["id"]})
    fleet_scheduler.reconcile(eng)
    with eng.begin() as c:
        assert c.execute(text("SELECT vip FROM agent_deployments WHERE id = :d"),
                         {"d": dep["id"]}).scalar() is None
    assert a.desired(since=0)["vips"] == []


def test_grouping_is_deterministic_and_the_console_sees_directors_and_pools(eng, world):
    from boss_engine.services import fleet_directors

    vips = [f"203.0.113.{i}" for i in range(120)]
    groups = fleet_directors.group_vips(reversed(vips), ["dir_b", "dir_a"])
    assert [len(g["vips"]) for g in groups] == [50, 50, 20]
    assert [g["master"] for g in groups] == ["dir_a", "dir_b", "dir_a"]
    assert [g["vrid"] for g in groups] == [1, 2, 3]
    assert fleet_directors.scheduler_for([{"slots": 4}, {"slots": 4}]) == "rr"
    assert fleet_directors.scheduler_for([{"slots": 4}, {"slots": 8}]) == "wlc"
    org = world["org"]
    a = world["director"]()
    a.enrol(join_token(eng, org, role="director"), address="192.0.2.10")
    a.heartbeat(groups_master=[0], keepalived_live=True, ipvs={"services": 3, "dests": 9})
    with eng.begin() as c:
        fleet_directors.add_vip_pool(c, org_id=org, cidr=POOL)
        listed = {d["id"]: d for d in fleet_directors.list_directors(c, org)}
        pools = fleet_directors.list_vip_pools(c, org)
    assert listed[a.host_id]["address"] == "192.0.2.10" and listed[a.host_id]["leases_held"] == [0]
    assert listed[a.host_id]["ipvs_state"] == {"services": 3, "dests": 9}
    assert any(p["cidr"] == POOL and p["routing"] == "ipvs" for p in pools)
    with eng.begin() as c:
        with pytest.raises(fleet_directors.DirectorError):
            fleet_directors.add_vip_pool(c, org_id=org, cidr="not-a-network")


def test_the_wire_shape_boss_lbd_decodes(eng, world):
    """Pins the JSON boss-lbd's hostapi.DirectorDesired / DirectorHeartbeatResponse read."""
    from boss_engine.services import fleet_directors, fleet_scheduler

    org, pool = world["org"], world["pool"]
    with eng.begin() as c:
        fleet_directors.add_vip_pool(c, org_id=org, cidr=POOL)
    a = world["director"]()
    a.enrol(join_token(eng, org, role="director"), address="192.0.2.10")
    h = world["host"]()
    h.enrol(join_token(eng, org, pool=pool))
    deploy_agent(eng, world["project"], env="prod", replicas=2, pool=pool)  # 1 host: degraded, 1 placed
    fleet_scheduler.reconcile(eng)
    h.heartbeat(address="198.51.100.1", replicas=[])
    h.serve_all()
    d = a.desired(since=0)
    assert set(d) >= {"changed", "director_id", "generation", "status", "self", "peer", "directors",
                      "vip_pools", "vips", "groups", "hosts"}
    v = d["vips"][0]
    assert set(v) >= {"vip", "ports", "scheduler", "deployment_id", "group", "members"}
    assert set(v["members"][0]) == {"replica_id", "vm_ip", "host_id", "weight", "slots"}
    assert set(d["hosts"][0]) == {"host_id", "address", "vm_prefix"}
    assert set(d["self"]) == {"id", "address", "side", "port"}
    hb = a.heartbeat(groups_master=[0]).json()
    assert set(hb) >= {"director_id", "desired_generation", "leases", "lease_expires", "renew",
                       "heartbeat_ms"}
    assert hb["lease_expires"]["0"] > 0 and hb["heartbeat_ms"] == 500
    assert uuid.UUID is not None


def test_vrrp_groups_are_stable_when_vips_are_added(eng, world):
    """A VIP keeps its group (and so its MASTER side) whatever is allocated later."""
    from boss_engine.services import fleet_directors, fleet_scheduler

    org, pool = world["org"], world["pool"]
    with eng.begin() as c:
        fleet_directors.add_vip_pool(c, org_id=org, cidr=POOL)
        fleet_directors.add_vip_pool(c, org_id=org, cidr="2001:db8:8b10::/120")  # v6: skipped
    a = world["director"]()
    a.enrol(join_token(eng, org, role="director"), address="192.0.2.10")
    h1, h2 = world["host"](), world["host"]()
    for h in (h1, h2):
        h.enrol(join_token(eng, org, pool=pool))
    first = deploy_agent(eng, world["project"], env="prod", replicas=2, pool=pool)
    fleet_scheduler.reconcile(eng)
    before = {g["group"]: g["vips"] for g in a.desired(since=0)["groups"]}
    with eng.begin() as c:
        vip1, g1 = c.execute(text("SELECT host(vip), vrrp_group FROM agent_deployments WHERE id = :d"),
                             {"d": first["id"]}).one()
        assert ":" not in vip1, "an IPv6 pool never feeds the IPv4 directors"
        # A lower address appears later (hand-set): it must not take vip1's place in its group.
        second = deploy_agent(eng, world["project"], env="prod", replicas=2, pool=pool)
        c.execute(text("UPDATE agent_deployments SET vip = CAST('203.0.113.64' AS INET) WHERE id = :d"),
                  {"d": second["id"]})
        # allocate_vip on a deployment that already holds one answers what it holds.
        assert fleet_directors.allocate_vip(c, org_id=org, deployment_id=second["id"]) == "203.0.113.64"
    fleet_scheduler.reconcile(eng)
    after = {g["group"]: g["vips"] for g in a.desired(since=0)["groups"]}
    assert vip1 in after[g1] and after[g1][: len(before[g1])] != [] and before == {g1: [vip1]}
    with eng.begin() as c:
        assert c.execute(text("SELECT vrrp_group FROM agent_deployments WHERE id = :d"),
                         {"d": first["id"]}).scalar() == g1
        c.execute(text("DELETE FROM vip_pool WHERE org_id = :o AND cidr = CAST('2001:db8:8b10::/120' AS CIDR)"),
                  {"o": org})
