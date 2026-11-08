"""ADD A SERVER (agentic-factory 3-add-a-server.html), on a real database, with the machine faked
at the ONE transport every host call uses (deploy_service._ssh), so verify, the read, Docker and
the watch run their real code:

* the words: an address, user@host, an ssh line or a sentence; a private key in the input is
  refused before anything else;
* the read probes the network before any credential and creates nothing (assistant:chat, never
  a step-up); a viewer is refused;
* Add and set up is host:manage: it records the server with a ROLE, hands over access (BOSS's
  key, your key, a stored key), never creates an app target, and refuses in words;
* the agent waits for the key with no "I ran it", then reads, installs, verifies, readies and
  watches; on Community a missing Docker waits in words and goes on once it appears;
* a ready server shows on a project's Deployments with its role; production is offered, never
  picked; onboarding never offers a prod server for staging;
* the Infra agent's watch says when the disk passes the line and prunes build cache alone only
  when the edition may write to servers;
* the clock (POST /v1/servers/tick) advances due setups.
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from apps.api.app import main
from apps.api.tests.identity_fixtures import IdentityWorld
from boss_engine.config import get_settings
from boss_engine.services import add_server, deploy_service

HOSTKEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIHVkZXYtZmFrZS1ob3N0LWtleS1mb3ItYWYtc2VydmVy"


class Machines:
    """Fake machines by address: they let in only an authorised key."""

    def __init__(self):
        self.m: dict[str, dict] = {}
        self.calls: list[str] = []

    def add(self, address: str, **kw) -> dict:
        m = {"authorized": set(), "docker": "27.3.1", "exporter": False, "disk_pct": 12, "arch": "x86_64"}
        m.update(kw)
        self.m[address] = m
        return m

    def authorise(self, address: str, public_line: str) -> None:
        self.m[address]["authorized"].add(public_line.split()[1])

    def ssh(self, key_path, host, user, port, command, timeout=180, stdin_text=None):
        self.calls.append(command)
        m = self.m.get(host)
        if m is None:
            return subprocess.CompletedProcess([], 255, "", "ssh: connect to host: Connection timed out")
        pub = subprocess.run(["ssh-keygen", "-y", "-f", key_path], capture_output=True, text=True).stdout.split()
        if len(pub) < 2 or pub[1] not in m["authorized"]:
            return subprocess.CompletedProcess([], 255, "", f"{user}@{host}: Permission denied (publickey).")
        out = []
        if "BOSS_SSH_OK" in command:
            out += ["BOSS_SSH_OK", "DOCKER_OK" if m["docker"] else "DOCKER_MISSING", m["arch"]]
        if "BOSS_CAP" in command:
            total = 100 * 1024 * 1024
            out += ["BOSS_CAP cpus 4", "BOSS_CAP load 0.1", "BOSS_CAP mem 8388608 4194304",
                    f"BOSS_CAP disk {total} {total * m['disk_pct'] // 100}", "BOSS_CAP os Ubuntu 22.04.4 LTS"]
        if "BOSS_X" in command:
            out += [f"BOSS_X arch {m['arch']}", "BOSS_X listen 22", "BOSS_X listen 8001"]
            if m["docker"]:
                out += [f"BOSS_X docker Docker version {m['docker']}, build x"]
            if m["exporter"]:
                out += ["BOSS_X exporter running"]
        if "apt-get install" in command and "docker.io" in command:
            m["docker"] = "27.3.1"
            out += [f"Docker version {m['docker']}"]
        if "boss-node-exporter" in command and "docker run" in command:
            m["exporter"] = True
        if "builder prune" in command:
            m["disk_pct"] = 40
        return subprocess.CompletedProcess([], 0, "\n".join(out) + "\n", "")


@pytest.fixture
def machines(monkeypatch):
    fake = Machines()
    monkeypatch.setattr(deploy_service, "_ssh", fake.ssh)
    monkeypatch.setattr(add_server, "_tcp", lambda a, p, timeout=2.5: a in fake.m and int(p) == 22)
    monkeypatch.setattr(add_server, "_keyscan", lambda a, p, timeout=4: f"{a} {HOSTKEY}\n" if a in fake.m else "")
    return fake


@pytest.fixture(autouse=True)
def one_turn_at_a_time(monkeypatch):
    """The route gives the agent its first turn on a thread; here each turn is the test's own."""
    from apps.api.app.routers import add_server as route

    monkeypatch.setattr(route, "_turn_soon", lambda host_id: None)


@pytest.fixture
def writes(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "boss_edition", "enterprise", raising=False)
    monkeypatch.setattr(s, "ssh_host_deploy_allowed", True, raising=False)
    return s


@pytest.fixture(scope="module")
def world(engine) -> IdentityWorld:
    w = IdentityWorld.create(engine)
    w.owner = w.person("as-owner")
    w.bind(w.owner, "owner")
    w.viewer = w.person("as-viewer")
    w.bind(w.viewer, "viewer")
    return w


@pytest.fixture(scope="module")
def client():
    return TestClient(main.app, raise_server_exceptions=False)


def _as(world, who):
    return world.acting_as(world.ctx(who))


def _addr() -> str:
    return f"203.0.113.{uuid.uuid4().int % 200 + 20}"


def _add(world, client, pid: str, address: str, **spec):
    body = {"project_id": pid, "address": address, "ssh_user": "ubuntu", "role": "staging",
            "access": {"mode": "boss_key", "backend": "database"}, **spec}
    with _as(world, world.owner):
        return client.post("/v1/servers", json=body)


def _advance(engine, host_id: str) -> dict:
    with engine.begin() as c:
        add_server.claim(c, host_id, force=True)
        return add_server.advance(c, get_settings(), host_id)


def _view(engine, host_id: str) -> dict:
    with engine.begin() as c:
        return add_server.view(c, get_settings(), host_id)


# ── the words ──────────────────────────────────────────────────────────────────────────────

def test_the_one_input_reads_every_shape():
    w = add_server.read_words("ubuntu@203.0.113.40\nA staging box.")
    assert (w["address"], w["user"], w["kind"], w["role"]) == ("203.0.113.40", "ubuntu", "ssh", "staging")
    w = add_server.read_words("ssh -p 2222 -i ~/.ssh/work deploy@build.example.com")
    assert (w["address"], w["user"], w["port"], w["identity_file"]) == ("build.example.com", "deploy", 2222, "~/.ssh/work")
    assert add_server.read_words("root@10.0.0.5:2200 for production")["port"] == 2200
    assert add_server.read_words("root@10.0.0.5 for production")["role"] == "prod"
    assert add_server.read_words("10.0.0.7 is our CI runner")["role"] == "builds"
    assert add_server.read_words("a cloud box for staging on hetzner")["kind"] == "cloud"
    assert add_server.read_words("enroll the build box behind NAT")["kind"] == "dials_in"
    assert add_server.read_words("call it edge-7, it's at 10.1.2.3")["name_hint"] == "edge-7"
    assert add_server.read_words("a staging box please, e.g. fast")["address"] is None
    with pytest.raises(add_server.ServerRefused, match="Never paste a key here"):
        add_server.read_words("-----BEGIN OPENSSH PRIVATE KEY-----\nxyz")


def test_the_plan_follows_the_boxes():
    lines = add_server.plan_lines({"docker": True, "exporter": False, "caddy": True}, {"host_health": False}, "prod")
    assert "Install Docker and Caddy for TLS (if missing)" in lines
    assert any("each prod deploy still asks you" in x for x in lines)
    assert not any("watching" in x for x in lines)


# ── the read: words + probe, creating nothing ──────────────────────────────────────────────

def test_the_read_probes_before_any_credential_and_creates_nothing(world, client, engine, machines):
    pid = world.project_a
    addr = _addr()
    machines.add(addr)
    with engine.connect() as c:
        before = c.execute(text("SELECT count(*) FROM deployment_hosts")).scalar()
    with _as(world, world.owner):
        r = client.post(f"/v1/projects/{pid}/servers/read", json={"text": f"ubuntu@{addr}\nA staging box."})
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["address"] == addr and b["user"] == "ubuntu" and b["role"] == "staging"
    assert b["probe"]["port_open"] is True and b["probe"]["host_key"].startswith("SHA256:")
    assert b["probe"]["web"] == {"80": "free", "443": "free"}
    assert b["name"].startswith("boss-") and b["plan"][0].startswith("Read OS")
    with engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM deployment_hosts")).scalar() == before
    with _as(world, world.owner):
        key = client.post(f"/v1/projects/{pid}/servers/read", json={"text": "-----BEGIN RSA PRIVATE KEY-----"})
    assert key.status_code == 422 and "Never paste a key here" in key.json()["message"]
    with _as(world, world.viewer):
        assert client.post(f"/v1/projects/{pid}/servers/read", json={"text": addr}).status_code in (403, 404)


# ── Add and set up ─────────────────────────────────────────────────────────────────────────

def test_add_and_set_up_waits_for_the_key_then_sets_itself_up(world, client, engine, machines, writes):
    pid = world.project_a
    addr = _addr()
    machines.add(addr, docker=None)
    r = _add(world, client, pid, addr, name=f"s-{uuid.uuid4().hex[:6]}", watch={"disk_pct": 85})
    assert r.status_code == 200, r.text
    host_id = r.json()["host_id"]
    with engine.connect() as c:
        h = c.execute(text("SELECT labels, status, key_public FROM deployment_hosts WHERE id = :h"),
                      {"h": host_id}).mappings().one()
        assert h["labels"]["role"] == "staging" and h["status"] == "registered" and h["key_public"]
        assert not c.execute(text("SELECT 1 FROM deployment_targets WHERE host_id = :h"), {"h": host_id}).first(), \
            "adding a server never creates an app target"
    # the key has not landed: the agent waits, with the line to run, and no "I ran it"
    assert _advance(engine, host_id)["phase"] == "key"
    v = _view(engine, host_id)
    assert v["phase"] == "key" and v["state"] == "needs you" and v["blocked"] is None
    assert "authorized_keys" in v["gate"]["line"] and v["steps"][0]["detail"] == "waiting for the key"
    # the person runs the line; the agent's next turn sees it land and goes all the way
    machines.authorise(addr, v["gate"]["line"].split("'")[3])
    out = _advance(engine, host_id)
    assert out["phase"] == "watching", out
    v = _view(engine, host_id)
    assert [s["state"] for s in v["steps"]] == ["done"] * 6
    assert v["gate"] is None and v["ready"]
    assert v["stats"]["memory"] == "8.0 GB" and v["stats"]["alert_pct"] == 85 and v["stats"]["free_ports"] == 999
    lines = " | ".join(x["line"] for x in v["log"])
    for said in ("Key landed", "Read the machine", "Installing Docker", "Verified", "Ready in the staging pool"):
        assert said in lines, lines
    assert any("apt-get install" in c for c in machines.calls), "Docker came from the one provision path"
    with engine.connect() as c:
        assert c.execute(text("SELECT status FROM deployment_hosts WHERE id = :h"), {"h": host_id}).scalar() == "verified"
        assert c.execute(text("SELECT 1 FROM events WHERE entity_id = :h AND event_type = 'server.ready'"),
                         {"h": host_id}).first()


def test_on_community_a_missing_docker_waits_in_words_until_it_appears(world, client, engine, machines):
    pid = world.project_a
    addr = _addr()
    machines.add(addr, docker=None)
    host_id = _add(world, client, pid, addr, name=f"c-{uuid.uuid4().hex[:6]}").json()["host_id"]
    machines.authorise(addr, _view(engine, host_id)["gate"]["line"].split("'")[3])
    out = _advance(engine, host_id)
    assert out["phase"] == "install" and "Docker missing" in out["blocked"] and "agent sees it" in out["blocked"]
    assert not any("apt-get install" in c for c in machines.calls), "Community never installs"
    machines.m[addr]["docker"] = "26.1.0"
    assert _advance(engine, host_id)["phase"] == "watching"


def test_your_own_key_or_a_stored_key_and_refusals_in_words(world, client, engine, machines):
    pid = world.project_a
    addr = _addr()
    machines.add(addr)
    d = tempfile.mkdtemp()
    path = os.path.join(d, "k")
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", path], check=True)
    priv, pub = open(path).read(), open(path + ".pub").read()
    machines.authorise(addr, pub)
    name = f"m-{uuid.uuid4().hex[:6]}"
    r = _add(world, client, pid, addr, name=name, access={"mode": "my_key", "backend": "database", "private_key": priv})
    assert r.status_code == 200, r.text
    assert "PRIVATE" not in r.text
    host_id = r.json()["host_id"]
    assert _view(engine, host_id)["gate"] is None, "no line: the key already works"
    assert _advance(engine, host_id)["phase"] == "watching"
    # stored key: a deploy key the project keeps in the vault, picked by name
    with _as(world, world.owner):
        made = client.post("/v1/secrets/keypair", json={"project_id": pid, "actor": "op",
                                                         "name": f"deploy-key-{uuid.uuid4().hex[:4]}", "backend": "database"})
        assert made.status_code in (200, 201), made.text
        ref = made.json().get("secret_ref_id") or made.json().get("id")
        pub = made.json()["public_key"]
        listed = client.post(f"/v1/projects/{pid}/servers/read", json={"text": addr}).json()["stored_keys"]
    assert any(k["id"] == ref and k["name"].startswith("deploy-key-") for k in listed), listed
    assert all("PRIVATE" not in json.dumps(k) for k in listed)
    assert not any("host" in k["name"] for k in listed), "each server's own BOSS key is not offered"
    addr2 = _addr()
    machines.add(addr2)
    machines.authorise(addr2, pub)
    r = _add(world, client, pid, addr2, name=f"m2-{uuid.uuid4().hex[:6]}", access={"mode": "stored_key", "secret_ref_id": ref})
    assert r.status_code == 200, r.text
    assert _advance(engine, r.json()["host_id"])["phase"] == "watching"
    # refusals, in words
    bad = _add(world, client, pid, _addr(), access={"mode": "my_key", "private_key": "ssh-ed25519 AAAA public"})
    assert bad.status_code == 422 and "PRIVATE half" in bad.json()["message"]
    bad = _add(world, client, pid, _addr(), access={"mode": "stored_key", "secret_ref_id": "sref_nope"})
    assert bad.status_code == 422 and "stored SSH key" in bad.json()["message"]
    bad = _add(world, client, pid, _addr(), name=name)
    assert bad.status_code == 409 and "already has a server named" in bad.json()["message"]
    bad = _add(world, client, pid, "not an address!")
    assert bad.status_code == 422 and "name the machine first" in bad.json()["message"]
    bad = _add(world, client, pid, _addr(), role="everything")
    assert bad.status_code == 422 and "staging, prod or builds" in bad.json()["message"]
    with _as(world, world.viewer):
        assert client.post("/v1/servers", json={"project_id": pid, "address": _addr()}).status_code in (403, 404)


# ── where a ready server shows up ──────────────────────────────────────────────────────────

def test_a_ready_server_shows_on_deployments_with_its_role_and_prod_is_never_picked(world, client, engine, machines):
    pid = world.project_b
    ids = {}
    for role in ("staging", "prod"):
        addr = _addr()
        machines.add(addr)
        host_id = _add(world, client, pid, addr, name=f"{role}-{uuid.uuid4().hex[:5]}", role=role).json()["host_id"]
        machines.authorise(addr, _view(engine, host_id)["gate"]["line"].split("'")[3])
        assert _advance(engine, host_id)["phase"] == "watching"
        ids[role] = host_id
    with _as(world, world.owner):
        d = client.get(f"/v1/projects/{pid}/deployments")
    assert d.status_code == 200, d.text
    servers = {s["host_id"]: s for s in d.json()["servers"]}
    assert servers[ids["staging"]]["role_words"] == "Staging pool" and servers[ids["staging"]]["state"] == "ready"
    assert servers[ids["staging"]]["action"]["do"] == "staging_target"
    assert servers[ids["prod"]]["action"]["label"] == "Use for production" and not servers[ids["prod"]]["action"].get("do")
    assert d.json()["add_server_href"].endswith("add=server")
    from boss_engine.services.project_experience.onboarding import _offered_host

    with engine.connect() as c:
        offered = _offered_host(c, pid)
    assert offered and offered["id"] != ids["prod"], "a prod server is never offered for staging"


# ── the Infra agent's watch, and the clock ─────────────────────────────────────────────────

def test_the_watch_says_disk_and_prunes_cache_only_when_it_may(world, client, engine, machines, writes):
    pid = world.project_a
    addr = _addr()
    machines.add(addr)
    host_id = _add(world, client, pid, addr, name=f"w-{uuid.uuid4().hex[:6]}").json()["host_id"]
    machines.authorise(addr, _view(engine, host_id)["gate"]["line"].split("'")[3])
    assert _advance(engine, host_id)["phase"] == "watching"
    # a Re-verify keeps what the machine said about itself (the server page's capacity)
    from boss_engine.services.deployment_fleet import verify_host
    with engine.begin() as c:
        verify_host(c, get_settings(), host_id=host_id)
        pf = c.execute(text("SELECT last_preflight FROM deployment_hosts WHERE id = :h"), {"h": host_id}).scalar()
    assert pf["facts"]["capacity"]["cpus"] == 4 and pf["reachable"] is True
    machines.m[addr]["disk_pct"] = 83
    with engine.begin() as c:
        out = add_server.watch_one(c, get_settings(), host_id)
    assert out["disk_pct"] == 83 and out.get("pruned")
    assert any("builder prune" in c for c in machines.calls)
    log = " | ".join(x["line"] for x in _view(engine, host_id)["log"])
    assert "Pruned Docker build cache alone" in log and "83% → 40%" in log
    with engine.connect() as c:
        assert c.execute(text("SELECT 1 FROM events WHERE entity_id = :h AND event_type = 'server.disk_high'"),
                         {"h": host_id}).first()


def test_the_clock_advances_due_setups(world, client, engine, machines):
    pid = world.project_a
    addr = _addr()
    machines.add(addr)
    host_id = _add(world, client, pid, addr, name=f"t-{uuid.uuid4().hex[:6]}").json()["host_id"]
    machines.authorise(addr, _view(engine, host_id)["gate"]["line"].split("'")[3])
    with engine.begin() as c:
        c.execute(text("UPDATE server_setups SET next_attempt_at = now() - interval '1 second' WHERE host_id = :h"),
                  {"h": host_id})
    with _as(world, world.owner):
        r = client.post("/v1/servers/tick")
    assert r.status_code == 200, r.text
    assert _view(engine, host_id)["phase"] == "watching"
    with _as(world, world.viewer):
        assert client.post("/v1/servers/tick").status_code in (403, 404)
