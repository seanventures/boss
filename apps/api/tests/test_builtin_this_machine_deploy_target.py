"""BOSSAPP-1440: end-to-end deploy of a sample app to the built-in `'this machine'` target."""
from __future__ import annotations

import socket
import uuid

from sqlalchemy import text

from apps.api.app import main as api_main
from boss_engine.services import (
    deploy_truth,
    deployment_connectors,
    deployment_fleet,
    governed_host_deploy,
    infra_view,
    local_projects,
    local_repo,
    owned_process,
    production,
)
from boss_engine.services.jobs import JobLeaseService
from boss_engine.services.resource_plane import ResourcePlaneService


def _free_port_in_range(low: int = 8100, high: int = 8999) -> int:
    for port in range(low, high + 1):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RuntimeError("no free port in 8100..8999")


SAMPLE_APP_PY = """
import http.server
import os

class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ("/", "/healthz"):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"hello-this-machine")
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *args):
        pass

if __name__ == "__main__":
    port = int(os.environ["PORT"])
    host = os.environ.get("HOST", "127.0.0.1")
    http.server.HTTPServer((host, port), Handler).serve_forever()
"""


def test_fresh_local_only_project_deploys_sample_app_to_this_machine_and_reads_healthy(
        client, engine, tmp_path, monkeypatch):
    monkeypatch.setenv("BOSS_INSTALL_TIER", "dev")
    monkeypatch.setattr(api_main.settings, "artifact_dir", str(tmp_path / "artifacts"))
    monkeypatch.setattr(api_main.settings, "boss_edition", "community")
    root = tmp_path / "repos"
    root.mkdir()
    monkeypatch.setattr(local_repo, "root", lambda settings=None: root)

    pid = f"localapp-{uuid.uuid4().hex[:8]}"
    port = _free_port_in_range()
    target_id = None
    try:
        resp = client.post("/v1/projects", json={"name": f"Local Sample {pid[-4:]}", "slug": pid})
        assert resp.status_code == 201, resp.text

        with engine.begin() as c:
            bound = local_projects.create_for_project(
                c, api_main.settings, pid, name="sample-app", actor="ceo")
            hosts = deployment_fleet.list_hosts(c, pid)
            assert any(h["name"] == "this machine" and h["status"] == "verified" for h in hosts), hosts
            bare = local_repo.bare_path(bound["owner"], bound["repo"], api_main.settings)
            sha = local_repo.commit_files(
                bare, "main", {"server.py": SAMPLE_APP_PY}, "Add sample HTTP server")

            made = deployment_fleet.create_deployment_target(
                c, project_id=pid, app_name="Sample App",
                repo=bound["full_name"], environment="staging",
                host="this machine", port=port, health_path="/healthz", actor="ceo")
            assert made["ok"] is True, made
            assert made["target_kind"] == "local"
            target_id = made["target_id"]

            trow = c.execute(
                text("SELECT * FROM deployment_targets WHERE id = :i"),
                {"i": target_id}).mappings().one()
            assert production.target_verdict(dict(trow)).prod is False
            resolved = production.resolve_environment("", host=made["address"])
            assert resolved.tier == "dev" and resolved.source == "local_host"

            rp = ResourcePlaneService(api_main.settings)
            run = rp.plan_deployment_run(
                c, project_id=pid, deployment_target_id=target_id, commit_ref=sha,
                release_packet={"commit_ref": sha, "app_name": "Sample App"},
                expected_cost_usd_micros=0, reservation_id=None, actor="ceo")
            assert run["status"] == "awaiting_approval"

        approved = client.post(f"/v1/decisions/{run['decision_id']}/approve", json={"actor": "ceo"})
        assert approved.status_code == 200, approved.text

        jobs = JobLeaseService(api_main.settings)
        run_id = run["id"]
        with engine.begin() as c:
            job_id = c.execute(
                text("SELECT id FROM jobs WHERE kind = :k AND payload->>'deployment_run_id' = :d"),
                {"k": governed_host_deploy.JOB_KIND, "d": run_id}).scalar_one()
            lease = jobs.lease_owned_job(
                c, project_id=pid, job_id=job_id, worker_id="worker-local", lease_ttl_seconds=600)
        result = governed_host_deploy.execute_host_job(
            engine, api_main.settings,
            deployment_run_id=run_id,
            job_id=job_id,
            worker_id="worker-local",
            lease_token=lease["lease_token"],
        )
        assert result["status"] == "succeeded", result
        assert result["resource_receipt_id"]

        with engine.connect() as c:
            final_run = c.execute(
                text("SELECT status, health_check_results FROM deployment_runs WHERE id = :i"),
                {"i": run_id}).mappings().one()
            receipt = c.execute(
                text("SELECT status, resource_type FROM resource_receipts WHERE id = :r"),
                {"r": result["resource_receipt_id"]}).mappings().one()
            fresh_target = dict(c.execute(
                text("SELECT * FROM deployment_targets WHERE id = :i"),
                {"i": target_id}).mappings().one())
            truth = deploy_truth.truth(c, [fresh_target])[target_id]
            detail = infra_view.deploy_detail(
                c, api_main.settings, pid, f"registry:{run_id}")

        assert final_run["status"] == "succeeded"
        assert final_run["health_check_results"]["healthy"] is True
        assert receipt["status"] == "succeeded"
        assert receipt["resource_type"] == "local_host.app"
        assert truth["live"] is True and truth["word"] == "Live"
        assert (truth["live_url"] or "").rstrip("/") == f"http://127.0.0.1:{port}"
        assert detail["row"]["word"] == "Live"
        assert (detail["open_url"] or "").rstrip("/") == f"http://127.0.0.1:{port}"

        observed = deployment_connectors.connector_for(fresh_target).observe(
            fresh_target, api_main.settings)
        assert observed["healthy"] is True
    finally:
        if target_id:
            owned_process.terminate_owner(
                f"local-deploy:{target_id}", reason="test_cleanup", grace=2.0)
