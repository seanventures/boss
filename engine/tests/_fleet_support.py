"""Shared pieces of the P8a tests: a fake boss-hostd (enrols with a CSR, signs every call with its
certificate's key, heartbeats, watches), the fleet router mounted on a bare app, and a project
with a deployed agent. Nothing here holds or prints a real credential."""
from __future__ import annotations

import base64
import os
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


def org_of(engine, project_id: str) -> str:
    with engine.begin() as c:
        return str(c.execute(text(
            "SELECT w.org_id FROM projects p JOIN workspaces w ON w.id = p.workspace_id "
            "WHERE p.id = :p"), {"p": project_id}).scalar())


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
                 runtime: str = "microvm", replicas: int = 1) -> dict:
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
                                                runtime="microvm", replicas=replicas)
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
        with c.begin_nested():
            c.execute(text("DELETE FROM projects WHERE id = :p"), {"p": project_id})


def join_token(engine, org_id: str, **kw: Any) -> str:
    from boss_engine.services import fleet

    with engine.begin() as c:
        return fleet.issue_join_token(c, org_id=org_id, actor="ceo", **kw)["token"]


__all__ = ["FakeHost", "TEST_MASTER_KEY", "cleanup_project", "deploy_agent", "fleet_app",
           "join_token", "make_project", "org_of", "use_test_master_key"]
