"""AMEX-G4 (BOSSAPP-1140): configuration is deployed through BOSS's own governance, end to end.

A `config_dir` target on this machine (a temp directory) with a tiny HTTP "gateway" whose
/readyz answers 200 or 503 as the test says and whose /-/reload counts the hooks it receives.
Everything BOSS decides is real: the bundle artifact, the ChangeSet, the zero reservation, the
decision or the policy's approval, the queued job, the phased apply, the receipt, the kept
previous versions, the rollback. The transport is the same shell scripts an SSH host would run.
"""
from __future__ import annotations

import hashlib
import http.server
import json
import threading

import pytest
from sqlalchemy import text

from boss_engine.config import get_settings
from boss_engine.services import config_deploy, governed_host_deploy
from boss_engine.services.decisions import InvalidDecisionTransitionError
from boss_engine.services.deploy_policy import set_approval_policy
from boss_engine.services.jobs import JobLeaseService

from .test_workflow_capability_runner import eng, project  # noqa: F401

PERSON = "executive_console"          # agent_identity.CONSOLE_OPERATOR: a person


class _Gateway(http.server.BaseHTTPRequestHandler):
    state = {"healthy": True, "reloads": 0}

    def do_GET(self):  # noqa: N802
        ok = self.path == "/readyz" and self.state["healthy"]
        self.send_response(200 if ok else 503)
        self.end_headers()
        self.wfile.write(b"ok" if ok else b"not ready")

    def do_POST(self):  # noqa: N802
        if self.path == "/-/reload":
            self.state["reloads"] += 1
        self.send_response(204 if self.path == "/-/reload" else 404)
        self.end_headers()

    def log_message(self, *args):  # quiet
        return


@pytest.fixture(scope="module")
def gateway():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Gateway)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    yield {"base": base, "state": _Gateway.state}
    server.shutdown()


@pytest.fixture(autouse=True)
def tidy(eng, project):  # noqa: F811
    """What a config deployment records, removed after each test so the shared project fixture's
    own teardown (which knows nothing of these tables) can delete the project."""
    yield
    problems: list[str] = []
    with eng.begin() as c:
        # In dependency order: receipts hang off changesets and jobs, runs off changesets, a
        # reservation off its job, so jobs go only once nothing points at them. Two passes:
        # the second catches whatever the first could not reach yet.
        for _pass in range(2):
            for table in ("resource_receipts", "external_resources", "receipts", "cost_receipts",
                          "deployment_runs", "changeset_operations", "changesets",
                          "budget_reservations", "jobs", "surface_ownerships", "events", "artifacts",
                          "deployment_environment_policies", "decisions", "deployment_targets"):
                try:
                    with c.begin_nested():
                        if table == "changeset_operations":
                            c.execute(text("DELETE FROM changeset_operations WHERE changeset_id IN "
                                           "(SELECT id FROM changesets WHERE project_id = :p)"), {"p": project})
                        elif table == "resource_receipts":
                            c.execute(text("DELETE FROM resource_receipts WHERE changeset_id IN "
                                           "(SELECT id FROM changesets WHERE project_id = :p) "
                                           "OR project_id = :p"), {"p": project})
                        elif table == "jobs":
                            c.execute(text("UPDATE receipts SET created_by_job_id = NULL WHERE project_id = :p"),
                                      {"p": project})
                            c.execute(text("DELETE FROM jobs WHERE project_id = :p"), {"p": project})
                        else:
                            c.execute(text(f"DELETE FROM {table} WHERE project_id = :p"), {"p": project})
                except Exception as exc:  # noqa: BLE001 — a table without project_id, or a row still referenced
                    if _pass:
                        problems.append(f"{table}: {str(exc).splitlines()[0][:160]}")
    if problems:
        import sys

        print("tidy could not remove:", *problems, sep="\n  ", file=sys.stderr)


@pytest.fixture()
def dirs(tmp_path):
    staging, production = tmp_path / "staging" / "tenants", tmp_path / "production" / "tenants"
    staging.mkdir(parents=True)
    production.mkdir(parents=True)
    (staging / "lumen-pay.yaml").write_text("tenant: lumen-pay\nversion: 0\n")
    return {"staging": staging, "production": production}


def _target(c, settings, project, env, directory, gw, **kw):  # noqa: F811
    return config_deploy.create_config_target(
        c, settings, project_id=project, app_name="Northgate Gateway", environment=env,
        directory=str(directory), reload_url=f"{gw['base']}/-/reload",
        health_url=f"{gw['base']}/readyz", actor=PERSON, **kw)


def _run(c, run_id):
    return dict(c.execute(text("SELECT * FROM deployment_runs WHERE id = :i"), {"i": run_id}).mappings().one())


def _apply(engine, settings, project, run_id):  # noqa: F811
    with engine.begin() as c:
        job = c.execute(text("SELECT id FROM jobs WHERE kind = 'deployment.host_apply' "
                             "AND payload->>'deployment_run_id' = :d"), {"d": run_id}).scalar_one()
        lease = JobLeaseService(settings).lease_owned_job(
            c, project_id=project, job_id=job, worker_id="w1", lease_ttl_seconds=600)
    return governed_host_deploy.execute_host_job(
        engine, settings, deployment_run_id=run_id, job_id=job, worker_id="w1",
        lease_token=lease["lease_token"])


def _governance(c, run):
    cs = c.execute(text("SELECT status, operations, approval_decision_id, reservation_id FROM changesets "
                        "WHERE id = :c"), {"c": run["changeset_id"]}).mappings().one()
    receipts = c.execute(text("SELECT status, provider FROM resource_receipts WHERE changeset_id = :c"),
                         {"c": run["changeset_id"]}).mappings().all()
    reservation = c.execute(text("SELECT status FROM budget_reservations WHERE id = :r"),
                            {"r": cs["reservation_id"]}).mappings().one()
    return dict(cs), [dict(r) for r in receipts], dict(reservation)


def test_config_deploys_promote_and_roll_back_under_the_same_governance_as_an_app(
        eng, project, gateway, dirs):  # noqa: F811
    settings = get_settings()
    gateway["state"].update(healthy=True, reloads=0)
    staging_dir, production_dir = dirs["staging"], dirs["production"]
    v1 = "tenant: lumen-pay\nversion: 1\nroutes:\n  - path: /v1/payments\n"

    # ── Targets: a config_dir row per environment, health and reload declared ───────────────
    with eng.begin() as c:
        staging = _target(c, settings, project, "staging", staging_dir, gateway)
        production = _target(c, settings, project, "production", production_dir, gateway)
        row = c.execute(text("SELECT target_kind, target_ref, health_checks, host_id FROM deployment_targets "
                             "WHERE id = :i"), {"i": staging["target_id"]}).mappings().one()
        assert row["target_kind"] == "config_dir" and row["target_ref"] == f"local:{staging_dir}"
        assert row["health_checks"][0]["url"].endswith("/readyz") and row["host_id"] is None
        # Staging ships without asking because a PERSON said so; production keeps asking.
        set_approval_policy(c, project_id=project, environment="staging", requires_approval=False,
                            actor="usr_owner", note="staging is for trying things")

    # ── Deploy to staging: planned under the policy, applied by the worker's leased job ─────
    planned = config_deploy.deploy_files(
        eng, settings, project_id=project, actor=PERSON, app_name="Northgate Gateway",
        environment="staging", files={"lumen-pay.yaml": v1})
    assert planned["status"] == "planned", planned
    assert planned["approval"]["source"] == "policy" and planned["run_status"] == "approved"
    assert planned["files"] == [{"path": "lumen-pay.yaml", "sha256": hashlib.sha256(v1.encode()).hexdigest(),
                                 "size": len(v1)}]
    with eng.begin() as c:
        run = _run(c, planned["deployment_run_id"])
        cs, receipts, _ = _governance(c, run)
        op = cs["operations"][0]
        assert op["skill_id"] == "config_dir.files.deploy"
        assert op["desired_state"]["files"][0]["path"] == "lumen-pay.yaml"
        assert "base64" not in json.dumps(op), "the ChangeSet carries names and hashes, never content"
        assert cs["status"] == "approved" and receipts == []
        assert run["release_packet"]["kind"] == "config" and run["trigger"] == "manual"
    assert (staging_dir / "lumen-pay.yaml").read_text().endswith("version: 0\n"), "nothing touched before the apply"

    result = _apply(eng, settings, project, planned["deployment_run_id"])
    with eng.begin() as c:
        said = _run(c, planned["deployment_run_id"])
    assert result["status"] == "succeeded", (result, said["failure_reason"], said["steps"])
    assert (staging_dir / "lumen-pay.yaml").read_text() == v1
    assert gateway["state"]["reloads"] == 1
    kept = staging_dir / ".boss-versions" / planned["deployment_run_id"] / "lumen-pay.yaml"
    assert kept.read_text().endswith("version: 0\n"), "the previous version is kept beside the directory"
    assert not list(staging_dir.glob(".boss-tmp.*")), "no temp file left: write then rename"
    with eng.begin() as c:
        run = _run(c, planned["deployment_run_id"])
        cs, receipts, reservation = _governance(c, run)
        assert run["status"] == "succeeded" and run["commit_ref"] == planned["bundle_sha256"]
        assert cs["status"] == "applied" and reservation["status"] == "consumed"
        assert receipts and receipts[0]["status"] == "succeeded"
        assert [s["step"] for s in run["steps"]] == ["resolve", "preflight", "keep", "transfer", "reload",
                                                     "observe", "prune"]
        assert run["health_check_results"]["healthy"] is True
    staging_run = planned["deployment_run_id"]

    # ── A bad change: the reload answers but health fails → the previous version is restored ─
    gateway["state"]["healthy"] = False
    bad = config_deploy.deploy_files(
        eng, settings, project_id=project, actor=PERSON, target_id=staging["target_id"],
        files={"lumen-pay.yaml": v1.replace("version: 1", "version: 2")})
    assert bad["status"] == "planned" and bad["deployment_run_id"] != staging_run
    config_deploy.HEALTH_WAIT_SECONDS, saved = 2, config_deploy.HEALTH_WAIT_SECONDS
    try:
        rolled = _apply(eng, settings, project, bad["deployment_run_id"])
    finally:
        config_deploy.HEALTH_WAIT_SECONDS = saved
    assert rolled["status"] == "rolled_back", rolled
    assert (staging_dir / "lumen-pay.yaml").read_text() == v1, "what served before serves after"
    assert gateway["state"]["reloads"] == 3, "reloaded for the change, and again after the restore"
    with eng.begin() as c:
        run = _run(c, bad["deployment_run_id"])
        assert run["status"] == "rolled_back" and "health check failed" in run["failure_reason"]
        assert [s["step"] for s in run["steps"]][-2:] == ["observe", "rollback"]
        cs, receipts, reservation = _governance(c, run)
        # The ChangeSet got its verdict (the host path's word for a failed apply) and a receipt.
        assert cs["status"] in ("deadlettered", "failed", "rolled_back") and receipts
        assert reservation["status"] != "consumed"
    gateway["state"]["healthy"] = True

    # ── Promote: the exact bytes staging passed, to production, which asks a person ─────────
    refused = config_deploy.promote(eng, settings, project_id=project, actor=PERSON,
                                    deployment_run_id=bad["deployment_run_id"])
    assert refused["status"] == "failed" and "not succeeded" in refused["reason"]
    promoted = config_deploy.promote(eng, settings, project_id=project, actor=PERSON,
                                     deployment_run_id=staging_run, target_id=production["target_id"])
    assert promoted["status"] == "planned", promoted
    assert promoted["approval"] is None and promoted["run_status"] == "awaiting_approval"
    assert promoted["bundle_sha256"] == planned["bundle_sha256"] and promoted["promoted_from"] == staging_run
    assert not (production_dir / "lumen-pay.yaml").exists()
    with eng.begin() as c:
        decision = dict(c.execute(text("SELECT * FROM decisions WHERE id = :d"),
                                  {"d": promoted["decision_id"]}).mappings().one())
        assert decision["status"] == "pending" and "Promote config" in decision["question"]
        assert decision["recommendation"]["files"][0]["sha256"] == planned["files"][0]["sha256"]
        # An agent never approves a host write.
        with pytest.raises(InvalidDecisionTransitionError, match="person"):
            governed_host_deploy.on_decision_approved(c, settings, decision, "agent:deploy-lead")
    with eng.begin() as c:
        job = governed_host_deploy.on_decision_approved(c, settings, decision, PERSON)
        assert job and job.get("id")
        assert _run(c, promoted["deployment_run_id"])["status"] == "approved"
    result = _apply(eng, settings, project, promoted["deployment_run_id"])
    assert result["status"] == "succeeded", result
    assert (production_dir / "lumen-pay.yaml").read_bytes() == (staging_dir / "lumen-pay.yaml").read_bytes() == v1.encode()
    with eng.begin() as c:
        run = _run(c, promoted["deployment_run_id"])
        assert run["commit_ref"] == planned["bundle_sha256"] and run["trigger"] == "promotion"
        assert run["release_packet"]["promoted_from"] == staging_run
    assert gateway["state"]["reloads"] == 4


def test_the_plan_refuses_what_it_cannot_ship(eng, project, gateway, dirs):  # noqa: F811
    settings = get_settings()
    with eng.begin() as c:
        with pytest.raises(config_deploy.ConfigDeployError, match="health URL"):
            config_deploy.create_config_target(
                c, settings, project_id=project, app_name="Gateway", environment="staging",
                directory=str(dirs["staging"]), actor=PERSON)
        with pytest.raises(config_deploy.ConfigDeployError, match="absolute"):
            config_deploy.create_config_target(
                c, settings, project_id=project, app_name="Gateway", environment="staging",
                directory="relative/dir", health_url=f"{gateway['base']}/readyz", actor=PERSON)
        target = _target(c, settings, project, "staging", dirs["staging"], gateway)
    # Nothing to deploy, a path that leaves the directory, a missing target: words, not records.
    nothing = config_deploy.deploy_files(eng, settings, project_id=project, actor=PERSON,
                                         target_id=target["target_id"], files={})
    assert nothing["status"] == "failed" and "Nothing to deploy" in nothing["reason"]
    escape = config_deploy.deploy_files(eng, settings, project_id=project, actor=PERSON,
                                        target_id=target["target_id"], files={"../etc/x": "y"})
    assert escape["status"] == "failed" and "relative path" in escape["reason"]
    missing = config_deploy.deploy_files(eng, settings, project_id=project, actor=PERSON,
                                         app_name="Nobody", environment="staging", files={"a": "b"})
    assert missing["status"] == "failed" and "no staging config target" in missing["reason"]
    with eng.begin() as c:
        assert c.execute(text("SELECT COUNT(*) FROM deployment_runs WHERE project_id = :p"),
                         {"p": project}).scalar_one() == 0


def test_a_deploy_from_a_cli_steps_produced_artifact_ships_those_bytes(eng, project, gateway, dirs):  # noqa: F811
    """The AMEX chain: gwlint --fix produced the corrected YAML in the fenced step; deploy_config
    ships that artifact's exact bytes under the path it was produced at."""
    from boss_engine.services.local_fenced import keep_produced

    settings = get_settings()
    gateway["state"].update(healthy=True)
    fixed = b"tenant: harbor-health\nversion: 7\n"
    with eng.begin() as c:
        _target(c, settings, project, "staging", dirs["staging"], gateway)
        set_approval_policy(c, project_id=project, environment="staging", requires_approval=False,
                            actor="usr_owner")
        ids = keep_produced(c, settings, project_id=project, skill_key="gwlint.fix", produced={
            "fixed": {"path": "harbor-health.yaml", "size": len(fixed),
                      "sha256": hashlib.sha256(fixed).hexdigest(), "bytes": fixed}})
    planned = config_deploy.deploy_files(eng, settings, project_id=project, actor=PERSON,
                                         app_name="Northgate Gateway", environment="staging",
                                         produced_artifacts=ids)
    assert planned["status"] == "planned" and planned["files"][0]["path"] == "harbor-health.yaml"
    assert _apply(eng, settings, project, planned["deployment_run_id"])["status"] == "succeeded"
    assert (dirs["staging"] / "harbor-health.yaml").read_bytes() == fixed
