"""Shared pieces of the P8a tests: a fake boss-hostd (enrols with a CSR, signs every call with its
certificate's key, heartbeats, watches), the fleet router mounted on a bare app, and a project
with a deployed agent. Nothing here holds or prints a real credential."""
from __future__ import annotations

import base64
import uuid
from typing import Any, Optional

from sqlalchemy import text

#: A test-only vault master key (never a real one): the fleet CA's key is sealed with a key
#: derived from it.
TEST_MASTER_KEY = base64.b64encode(b"p8a-fleet-test-master-key-000000").decode()


def use_test_master_key(monkeypatch) -> None:
    from boss_engine.identity import vault
    from boss_engine.services import fleet_ca

    monkeypatch.setenv("BOSS_MASTER_KEY", TEST_MASTER_KEY)
    monkeypatch.delenv("BOSS_MASTER_KEY_FILE", raising=False)
    vault.reset_master_key_cache()
    fleet_ca._CA_CACHE.clear()


def fleet_app(engine) -> Any:
    from fastapi import FastAPI

    from apps.api.app.routers.fleet import build_fleet_router

    app = FastAPI()
    app.include_router(build_fleet_router(engine=engine))
    return app


class FakeHost:
    """What boss-hostd does on the wire, in Python: its key never leaves this object."""

    def __init__(self, client: Any, *, backend: str = "process", hostname: str = "",
                 ed25519: bool = False):
        from cryptography.hazmat.primitives.asymmetric import ec, ed25519 as ed

        self.client = client
        self.key = ed.Ed25519PrivateKey.generate() if ed25519 else ec.generate_private_key(
            ec.SECP256R1())
        self.backend, self.hostname = backend, hostname or f"host-{uuid.uuid4().hex[:6]}"
        self.host_id: Optional[str] = None
        self.cert_pem: Optional[str] = None
        self.enrolment: dict = {}
        self.generation = 0
        self.vms: dict = {}
        self.tokens: dict = {}  # run id -> run token: host memory only
        self.state_gen = 0
        self.resyncs = 0
        self.polls: list = []

    def enrol(self, join_token: str, *, svid: bool = True, facts: Optional[dict] = None,
              expect: int = 201) -> dict:
        from boss_engine.services.fleet_ca import make_csr

        body = {"join_token": join_token, "csr_pem": make_csr(self.key),
                "hostname": self.hostname, "backend": self.backend,
                "facts": facts or {"kvm_live": False, "strict_secrets": True},
                "capacity": {"vcpu": 8, "mem_mib": 16384, "slots": 40}}
        if svid:
            from cryptography.hazmat.primitives.asymmetric import ec

            body["svid_csr_pem"] = make_csr(ec.generate_private_key(ec.SECP256R1()), "svid")
        response = self.client.post("/v1/hosts/enrol", json=body)
        assert response.status_code == expect, response.text
        if expect == 201:
            self.enrolment = response.json()
            self.host_id = self.enrolment["host_id"]
            self.cert_pem = self.enrolment["certificate_pem"]
        return response.json()

    def headers(self, method: str, path: str) -> dict:
        from boss_engine.services.fleet_ca import sign_proof

        return {"Authorization": "BossHost " + sign_proof(self.key, self.cert_pem, htm=method,
                                                          path=path)}

    def call(self, method: str, path: str, **kw: Any) -> Any:
        headers = {**self.headers(method, path), **(kw.pop("headers", None) or {})}
        return self.client.request(method, path, headers=headers, **kw)

    def heartbeat(self, **body: Any) -> Any:
        return self.call("POST", f"/v1/hosts/{self.host_id}/heartbeat",
                         json={"observed_generation": self.generation, **body})

    def desired(self, since: Optional[int] = None, wait: float = 0.0) -> dict:
        response = self.call("GET", f"/v1/hosts/{self.host_id}/desired",
                             params={"since": self.generation if since is None else since,
                                     "wait": wait})
        assert response.status_code == 200, response.text
        out = response.json()
        if out.get("changed"):
            self.generation = int(out["generation"])
        return out

    def signer(self):
        from boss_engine.runtime.client import HostSigner

        return HostSigner(self.key, self.cert_pem)

    # ── what boss-hostd does for its VMs (docs/infra/HOST_API.md §2.8, §4) ─────────────────

    def serve_all(self, *, slots: int = 4) -> list:
        """Take the whole desired state and push every running VM as `serving` through
        /observed with a bumped state_gen — what a healthy host does on every change."""
        vms = [v for v in self.desired(since=0)["vms"] if v["state"] == "running"]
        self.vms = {v["replica_id"]: {"replica_id": v["replica_id"], "vm_id": v["vm_id"],
                                      "deployment_id": v["deployment_id"], "state": "serving",
                                      "busy": 0, "slots": slots} for v in vms}
        self.state_gen += 1
        out = self.call("POST", f"/v1/hosts/{self.host_id}/observed", json={
            "replicas": list(self.vms.values()), "state_gen": self.state_gen})
        assert out.status_code == 200, out.text
        return list(self.vms)

    def claim(self, *, wait_ms: int = 0, ttl: Optional[int] = None) -> list:
        """THE host's one work request — `{wait_ms, state_gen}`, never a replica list. A
        {resync: true} answer is met with ONE PUT of the full map, then the poll again. The run
        tokens stay here (host memory), never in the guest."""
        from boss_engine.runtime.client import HostWorkClient, HttpTransport

        work = HostWorkClient(HttpTransport(self.client, signer=self.signer()))
        for _ in range(2):
            out = work.next(state_gen=self.state_gen, wait_ms=wait_ms, ttl=ttl)
            self.polls.append({"wait_ms": wait_ms, "state_gen": self.state_gen})
            if not out.get("resync"):
                break
            self.resyncs += 1
            work.put_replicas(self.host_id, state_gen=self.state_gen,
                              replicas=list(self.vms.values()))
        runs = list(out.get("runs") or [])
        for item in runs:
            self.tokens[item["run_id"]] = item["run_token"]
        return runs

    def gateway(self, replica_id: str):
        """The replica's view of the run plane through this host's guest gateway: signed as the
        host, X-Boss-Replica set from the 'socket', the run token attached from host memory."""
        from boss_engine.runtime.client import HttpTransport, RunPlaneClient

        host = self

        class Gateway(HttpTransport):
            def request(self, method, path, *, json=None, params=None, headers=None):
                sent = {k: v for k, v in (headers or {}).items()
                        if k.lower() not in ("x-boss-replica", "x-boss-run-token")}
                sent["X-Boss-Replica"] = replica_id
                parts = path.split("/")
                if len(parts) > 4 and parts[3] == "runs" and parts[4] in host.tokens:
                    sent["X-Boss-Run-Token"] = host.tokens[parts[4]]
                return super().request(method, path, json=json, params=params, headers=sent)

        return RunPlaneClient(Gateway(self.client, signer=self.signer()), replica_id)


def org_of(engine, project_id: str) -> str:
    with engine.begin() as c:
        return str(c.execute(text(
            "SELECT w.org_id FROM projects p JOIN workspaces w ON w.id = p.workspace_id "
            "WHERE p.id = :p"), {"p": project_id}).scalar())


def short_socket_dir():
    """A short directory for Unix sockets (macOS caps the path at 104 bytes; pytest's tmp_path
    is longer). The caller removes it."""
    import tempfile

    return tempfile.mkdtemp(prefix="p8a", dir="/tmp")


def own_pool() -> str:
    """A pool of this test's own: the bench database is shared, and a host only ever serves
    the deployments of its pool, so tests never place onto each other's hosts."""
    return f"t_{uuid.uuid4().hex[:10]}"


def make_project(engine) -> str:
    pid = f"p_{uuid.uuid4().hex[:10]}"
    with engine.begin() as c:
        wid = c.execute(text("SELECT workspace_id FROM projects LIMIT 1")).scalar()
        c.execute(text("INSERT INTO projects (id, workspace_id, name, slug) "
                       "VALUES (:i, :w, 'p8a fleet', :s)"), {"i": pid, "w": wid, "s": pid})
        c.execute(text(
            "INSERT INTO project_members (id, project_id, member_id, display_name, member_type, "
            "role_keys, permissions) VALUES (:i, :p, 'ceo', 'ceo', 'human', '[\"ceo\"]'::jsonb, "
            "'[\"decision_resolve\"]'::jsonb) ON CONFLICT DO NOTHING"),
            {"i": f"pm_{pid}_ceo", "p": pid})
    return pid


def deploy_agent(engine, project_id: str, *, key: Optional[str] = None, env: str = "dev",
                 runtime: str = "microvm", replicas: int = 1,
                 pool: Optional[str] = None) -> dict:
    from boss_engine.agents import versions
    from boss_engine.services import agent_deployments

    key = key or f"triage_{uuid.uuid4().hex[:6]}"
    with engine.begin() as c:
        versions.save(c, project_id, {
            "apiVersion": "boss.agent/v1", "kind": "Agent",
            "metadata": {"key": key, "name": "Triage bot"},
            "spec": {"mission": "Triage issues.", "prompts": {"system": "I am v1."},
                     "limits": {"max_steps": 6}}}, actor="ceo", source="operator")
        dep = agent_deployments.deploy(c, project_id, key, env, actor="ceo", version=1)
        if runtime == "microvm":
            dep = agent_deployments.set_runtime(c, project_id, key, env, actor="ceo",
                                                runtime="microvm", replicas=replicas,
                                                placement={"pool": pool} if pool else None)
    return dep


def cleanup_project(engine, project_id: str, host_ids: list) -> None:
    with engine.begin() as c:
        runs = [r[0] for r in c.execute(text("SELECT id FROM agent_runs WHERE project_id = :p"),
                                        {"p": project_id}).all()]
        c.execute(text("DELETE FROM agent_checkpoints WHERE run_id = ANY(:r)"), {"r": runs})
        for table in ("agent_replicas",):
            c.execute(text(f"DELETE FROM {table} WHERE project_id = :p"), {"p": project_id})
        if host_ids:
            for table in ("guest_calls", "host_events", "agent_certs", "vm_snapshots"):
                c.execute(text(f"DELETE FROM {table} WHERE host_id = ANY(:h)"), {"h": host_ids})
            c.execute(text("DELETE FROM agent_replicas WHERE host_id = ANY(:h)"), {"h": host_ids})
            c.execute(text("DELETE FROM agent_hosts WHERE id = ANY(:h)"), {"h": host_ids})
        c.execute(text("DELETE FROM trace_spans WHERE project_id = :p"), {"p": project_id})
        for table in ("jobs", "decisions", "events", "agent_runs", "agent_threads",
                      "agent_deployment_history", "agent_deployments", "agent_spec_versions", "roles",
                      "project_members"):
            with c.begin_nested():
                c.execute(text(f"DELETE FROM {table} WHERE project_id = :p"), {"p": project_id})
        for table in ("artifacts", "projects"):
            try:
                with c.begin_nested():
                    c.execute(text(f"DELETE FROM {table} WHERE "
                                   f"{'id' if table == 'projects' else 'project_id'} = :p"),
                              {"p": project_id})
            except Exception:  # noqa: BLE001 — a row another table still holds stays; harmless
                pass


def join_token(engine, org_id: str, **kw: Any) -> str:
    from boss_engine.services import fleet

    with engine.begin() as c:
        return fleet.issue_join_token(c, org_id=org_id, actor="ceo", **kw)["token"]


__all__ = ["FakeHost", "TEST_MASTER_KEY", "cleanup_project", "deploy_agent", "fleet_app",
           "join_token", "make_project", "org_of", "own_pool", "use_test_master_key"]
