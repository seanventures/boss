"""BOSSAPP-1140: configuration is deployed through BOSS's own governance, end to end.

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
import shutil
import tempfile
import threading
from pathlib import Path

import pytest
from sqlalchemy import text

from boss_engine.config import Settings
from boss_engine.services import config_deploy, governed_host_deploy
from boss_engine.services.decisions import InvalidDecisionTransitionError
from boss_engine.services.deploy_policy import set_approval_policy
from boss_engine.services.jobs import JobLeaseService

from .test_workflow_capability_runner import eng, project  # noqa: F401

def _fresh_settings() -> Settings:
    """Settings read NOW: get_settings() is cached for the process, and these tests change the
    operator's rules (BOSS_CONFIG_DEPLOY_ROOTS) between calls."""
    return Settings()


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
def dirs(tmp_path, monkeypatch):
    # The operator's rules: configuration goes only under this root; one reload command allowed.
    monkeypatch.setenv("BOSS_CONFIG_DEPLOY_ROOTS", str(tmp_path))
    monkeypatch.setenv("BOSS_CONFIG_RELOAD_COMMANDS", "touch .reloaded")
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
    settings = _fresh_settings()
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
        # Staging ships without asking because a PERSON said so. Production's policy says the
        # same — and a PROMOTION to production still waits for a person (review of #119).
        set_approval_policy(c, project_id=project, environment="staging", requires_approval=False,
                            actor="usr_owner", note="staging is for trying things")
        set_approval_policy(c, project_id=project, environment="production", requires_approval=False,
                            actor="usr_owner", note="a policy can never answer a promotion")

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
    kept = staging_dir.parent / ".boss-versions" / "tenants" / planned["deployment_run_id"] / "lumen-pay.yaml"
    assert kept.read_text().endswith("version: 0\n"), "the previous version is kept beside the directory"
    assert not (staging_dir / ".boss-versions").exists(), "never inside the watched directory"
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
        # As the decisions API does: the decision is answered first, then its follow-on runs.
        c.execute(text("UPDATE decisions SET status = 'approved', approved_by = :a WHERE id = :d"),
                  {"a": PERSON, "d": decision["id"]})
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
    settings = _fresh_settings()
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
    """The API-onboarding chain: gwlint --fix produced the corrected YAML in the fenced step; deploy_config
    ships that artifact's exact bytes under the path it was produced at."""
    from boss_engine.services.local_fenced import keep_produced

    settings = _fresh_settings()
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


def test_config_goes_only_where_the_operator_allows(eng, project, gateway, dirs, tmp_path, monkeypatch):  # noqa: F811
    """Review of #119 (High): a directory under BOSS_CONFIG_DEPLOY_ROOTS only (checked again
    on the machine against the resolved path), reload commands from the allowlist only, and a
    reload is always set by a person; the tool is never pressed by a model itself."""
    from boss_engine.tools.builtin import build_default_registry

    settings = _fresh_settings()
    assert build_default_registry().get("create_config_target").reversibility == "grants_authority"

    def make(directory, actor=PERSON, **kw):
        with eng.begin() as c:
            return config_deploy.create_config_target(
                c, _fresh_settings(), project_id=project, app_name=f"G{abs(hash(str(directory))) % 10**6}",
                environment="staging", directory=str(directory),
                health_url=f"{gateway['base']}/readyz", actor=actor, **kw)

    for outside in ("/etc", str(Path.home() / "boss"), str(tmp_path), f"{tmp_path}/../x"):
        with pytest.raises(config_deploy.ConfigDeployError):
            make(outside)
    monkeypatch.setenv("BOSS_CONFIG_DEPLOY_ROOTS", "")
    with pytest.raises(config_deploy.ConfigDeployError, match="no config deploy roots"):
        make(dirs["staging"])
    monkeypatch.setenv("BOSS_CONFIG_DEPLOY_ROOTS", str(tmp_path))
    with pytest.raises(config_deploy.ConfigDeployError, match="allowlist"):
        make(dirs["staging"], reload_command="rm -rf ~")
    with pytest.raises(config_deploy.ConfigDeployError, match="a person sets it"):
        make(dirs["staging"], actor="agent:deploy-lead", reload_command="touch .reloaded")
    with pytest.raises(config_deploy.ConfigDeployError, match="a person sets it"):
        make(dirs["staging"], actor="agent:deploy-lead", reload_url=f"{gateway['base']}/-/reload")
    made = make(dirs["staging"], reload_command="touch .reloaded")
    assert made["ok"] and made["reload_command"] == "touch .reloaded"

    # A directory under the root that is a SYMLINK out of it: refused on the machine, untouched.
    outside = Path(tempfile.mkdtemp(prefix="cfg-outside-"))
    try:
        (outside / "keep.yaml").write_text("untouched\n")
        link = tmp_path / "staging" / "escape"
        link.symlink_to(outside, target_is_directory=True)
        escape = make(link)
        with eng.begin() as c:
            set_approval_policy(c, project_id=project, environment="staging", requires_approval=False,
                                actor="usr_owner")
        planned = config_deploy.deploy_files(eng, settings, project_id=project, actor=PERSON,
                                             target_id=escape["target_id"], files={"keep.yaml": "pwned\n"})
        assert planned["status"] == "planned", planned
        result = _apply(eng, settings, project, planned["deployment_run_id"])
        assert result["status"] == "blocked", result
        assert (outside / "keep.yaml").read_text() == "untouched\n"
        with eng.begin() as c:
            assert "outside the allowed" in _run(c, planned["deployment_run_id"])["failure_reason"]
    finally:
        shutil.rmtree(outside, ignore_errors=True)

    # Roots narrowed after the target was made: the next plan is refused in words.
    monkeypatch.setenv("BOSS_CONFIG_DEPLOY_ROOTS", "/srv/elsewhere")
    refused = config_deploy.deploy_files(eng, _fresh_settings(), project_id=project, actor=PERSON,
                                         target_id=made["target_id"], files={"a.yaml": "a"})
    assert refused["status"] == "blocked" and "outside the allowed" in refused["reason"]


def test_a_config_deploy_straight_to_production_waits_for_a_person(eng, project, gateway, dirs):  # noqa: F811
    """Re-review of #119: deploy_config aimed at production is a person's answer, whatever
    production's policy says and whatever gate a step passes in."""
    settings = _fresh_settings()
    with eng.begin() as c:
        prod = _target(c, settings, project, "production", dirs["production"], gateway)
        set_approval_policy(c, project_id=project, environment="production", requires_approval=False,
                            actor="usr_owner", note="a policy can never answer production")
    planned = config_deploy.deploy_files(eng, settings, project_id=project, actor=PERSON,
                                         target_id=prod["target_id"], files={"a.yaml": "a: 1\n"},
                                         approval_decision_id="dec_some_gate")
    assert planned["status"] == "planned", planned
    assert planned["approval"] is None and planned["run_status"] == "awaiting_approval"
    with eng.begin() as c:
        decision = c.execute(text("SELECT status FROM decisions WHERE id = :d"),
                             {"d": planned["decision_id"]}).scalar_one()
    assert decision == "pending" and not (dirs["production"] / "a.yaml").exists()


def test_config_targets_are_made_only_by_create_config_target(eng, project, gateway, dirs, monkeypatch):  # noqa: F811
    """POST /v1/deployments and the deployment.target.register skill would skip the roots, the
    reload allowlists and the person: both reach ResourcePlaneService.create_deployment_target,
    which refuses config_dir unless create_config_target asks."""
    import inspect
    from types import SimpleNamespace

    from apps.api.app import main as api_main
    from boss_engine.services import skill_executors
    from boss_engine.services.resource_plane import ResourcePlaneService

    settings = _fresh_settings()
    rp = ResourcePlaneService(settings)
    args = dict(project_id=project, app_name="Sneaky", repo="config:x", environment="staging",
                provider_account_id=None, target_ref="local:/etc", domains=[],
                required_resources=[{"kind": "config_dir", "directory": "/etc"}], secret_refs=[],
                health_checks=[], rollback_points=[], actor=PERSON)
    for kind in ("config_dir", "Config-Dir"):
        with eng.begin() as c, pytest.raises(ValueError, match="create_config_target"):
            rp.create_deployment_target(c, target_kind=kind, **args)
    with eng.begin() as c, pytest.raises(ValueError, match="create_config_target"):
        skill_executors.execute_deployment_target_register(
            SimpleNamespace(connection=c, project_id=project, actor=PERSON, deployments=rp),
            {"provider": "config_dir", "app_id": "Sneaky", "repo": "config:x",
             "environment": "staging", "target_ref": "local:/etc"})
    route = inspect.getsource(api_main.create_deployment_target)
    assert "resource_plane.create_deployment_target(" in route and "config_target" not in route
    assert "except (ProviderAccountNotFoundError, ValueError)" in route   # → 422, not 500

    # A reload URL: loopback or the target's host or the allowlist; anything else is refused.
    def make(url, actor=PERSON, env="staging", app="UrlGw"):
        with eng.begin() as c:
            return config_deploy.create_config_target(
                c, _fresh_settings(), project_id=project, app_name=app, environment=env,
                directory=str(dirs["staging"]), health_url=f"{gateway['base']}/readyz",
                reload_url=url, actor=actor)
    with pytest.raises(config_deploy.ConfigDeployError, match="BOSS_CONFIG_RELOAD_URLS"):
        make("http://169.254.169.254/latest/meta-data")
    with pytest.raises(config_deploy.ConfigDeployError, match="a person sets it"):
        make(f"{gateway['base']}/-/reload", actor="agent:deploy-lead")
    monkeypatch.setenv("BOSS_CONFIG_RELOAD_URLS", "https://gw.internal.example/-/reload")
    assert make("https://gw.internal.example/-/reload")["ok"]


def test_two_runs_in_flight_each_promote_only_what_they_staged(eng, project, gateway, dirs):  # noqa: F811
    """BOSSAPP-1167: promote_config is bound to its own run's staging step.

    Two onboarding runs, A and B, each stage a different tenant file; B's staging deploy
    succeeds after A's. Before the fix A's promotion took "the latest succeeded staging deploy
    for the app" — B's bundle. Now the runner accepts only a deployment THIS run's config_deploy
    step produced, the tool refuses a source whose bytes differ from that step's hash, and
    inside a run there is no fallback at all."""
    from boss_engine.services.github_actions import WORKFLOW_RUN_ID
    from boss_engine.services.workflow_capability_runner import (SettledCapabilityError,
                                                                 _bound_promotion)
    from boss_engine.tools.builtin import build_default_registry

    settings = _fresh_settings()
    gateway["state"].update(healthy=True, reloads=0)
    with eng.begin() as c:
        _target(c, settings, project, "staging", dirs["staging"], gateway)
        production = _target(c, settings, project, "production", dirs["production"], gateway)
        set_approval_policy(c, project_id=project, environment="staging", requires_approval=False,
                            actor="usr_owner", note="staging is for trying things")
    tools = {s.name: s for s in build_default_registry().specs()}

    class Ctx:
        engine = eng

    Ctx.settings = settings

    def stage(run_id, name, body):
        token = WORKFLOW_RUN_ID.set(run_id)
        try:
            out = tools["deploy_config"].handler(
                Ctx(), project_id=project, actor=PERSON,
                args={"app_name": "Northgate Gateway", "environment": "staging",
                      "files": {name: body}})
        finally:
            WORKFLOW_RUN_ID.reset(token)
        assert out["status"] == "planned", out
        assert _apply(eng, settings, project, out["deployment_run_id"])["status"] == "succeeded"
        # What the config_deploy step's output holds once its deployment succeeded.
        return {"capability": "config_deploy", "status": "succeeded",
                "deployment_id": out["deployment_id"], "bundle_sha256": out["bundle_sha256"]}

    a = stage("wfr_tenant_a", "lumen-pay.yaml", "id: lumen-pay\nversion: 1\n")
    b = stage("wfr_tenant_b", "orbit-travel.yaml", "id: orbit-travel\nversion: 1\n")
    assert a["bundle_sha256"] != b["bundle_sha256"]
    with eng.begin() as c:
        packet = _run(c, a["deployment_id"])["release_packet"]
    assert packet["workflow_run_id"] == "wfr_tenant_a", "the deployment names the run that made it"

    spec = {"entry_step": "validate", "steps": [
        {"id": "validate", "kind": "capability", "binding": {"custom_step": "step_gwlint"}},
        {"id": "deploy_staging", "kind": "capability",
         "binding": {"capability": "config_deploy"}},
        {"id": "relay", "kind": "capability", "binding": {"custom_step": "step_mcp_relay"}},
        {"id": "retry_staging", "kind": "capability",
         "binding": {"capability": "config_deploy"}},
    ]}
    run_a = {"run_id": "wfr_tenant_a", "steps": {
        "deploy_staging": a, "validate": {"exit_code": 0},
        # A custom (MCP) step's output may SAY it is a config deploy of B's bundle: forged.
        "relay": {**b, "capability": "config_deploy"},
        # A config_deploy step that FAILED into its error path is not a staging result.
        "retry_staging": {**b, "step_status": "failed"}}}
    binding = {"app_name": "Northgate Gateway", "from_environment": "staging",
               "environment": "production"}

    def bind(context, inputs):
        with eng.begin() as c:
            return _bound_promotion(c, context, inputs, spec=spec)

    # The runner: no id, another run's id (however it is dressed up), a hash that disagrees.
    with pytest.raises(SettledCapabilityError, match="needs the deployment"):
        bind(run_a, binding)
    with pytest.raises(SettledCapabilityError, match="not deployed by this run"):
        bind(run_a, {**binding, "deployment_id": b["deployment_id"]})
    with pytest.raises(SettledCapabilityError, match="not the one"):
        bind(run_a, {**binding, "deployment_id": a["deployment_id"],
                     "bundle_sha256": b["bundle_sha256"]})
    # A real config_deploy step whose output names B's deployment: the database says B's run
    # planned it, so A cannot promote it.
    forged = {**run_a, "steps": {**run_a["steps"], "deploy_staging": b}}
    with pytest.raises(SettledCapabilityError, match="not planned by this workflow run"):
        bind(forged, {**binding, "deployment_id": b["deployment_id"]})
    bound = bind(run_a, {**binding, "deployment_id": a["deployment_id"]})
    assert bound["deployment_run_id"] == a["deployment_id"]
    assert bound["bundle_sha256"] == a["bundle_sha256"] and "deployment_id" not in bound

    def promote(run_id, args):
        token = WORKFLOW_RUN_ID.set(run_id)
        try:
            return tools["promote_config"].handler(Ctx(), project_id=project, actor=PERSON,
                                                   args=args)
        finally:
            WORKFLOW_RUN_ID.reset(token)

    # The tool, inside a run: no fallback to "the latest" (which is B's), and a source whose
    # bytes differ from the expected hash is refused before anything is planned.
    no_source = promote("wfr_tenant_a", binding)
    assert no_source["status"] == "failed" and "own staging step" in no_source["reason"]
    crossed = promote("wfr_tenant_a", {**binding, "deployment_id": b["deployment_id"],
                                       "bundle_sha256": a["bundle_sha256"]})
    assert crossed["status"] == "failed" and "not planned by this workflow run" in crossed["reason"]
    crossed = promote("wfr_tenant_b", {**binding, "deployment_id": b["deployment_id"],
                                       "bundle_sha256": a["bundle_sha256"]})
    assert crossed["status"] == "failed" and "different bundle" in crossed["reason"]
    # `deployment_id` is read (it was silently dropped before) and A ships A's bytes.
    promoted = promote("wfr_tenant_a", {**bound, "target_id": production["target_id"]})
    assert promoted["status"] == "planned", promoted
    assert promoted["promoted_from"] == a["deployment_id"]
    assert promoted["bundle_sha256"] == a["bundle_sha256"] != b["bundle_sha256"]
    alias = promote("", {"deployment_id": b["deployment_id"], "target_id": production["target_id"]})
    assert alias["status"] == "planned" and alias["promoted_from"] == b["deployment_id"]
    # Outside a workflow too, the source is named, never "the latest for the app".
    guessed = promote("", binding)
    assert guessed["status"] == "failed" and "Name the deployment" in guessed["reason"]


def test_manual_config_deploy_and_promote_config_workflow_gates_ask_once(
    eng, project, gateway, dirs,  # noqa: F811
):
    """BOSSAPP-1410: a manual workflow gate on config_deploy (staging) or promote_config
    (production) produces ONE decision card total — approving the step gate lets the deployment
    run without opening a second card, while unapproved production still asks a person."""
    from boss_engine.services import workflow_capability_runner as runner
    from boss_engine.services import workflows
    from boss_engine.services.decisions import DecisionService

    settings = _fresh_settings()
    gateway["state"].update(healthy=True, reloads=0)
    staging_dir, production_dir = dirs["staging"], dirs["production"]

    class _RunnerCtx:
        def __init__(self):
            self.engine = eng
            self.settings = settings

        def begin(self):
            return eng.begin()

    ctx = _RunnerCtx()

    with eng.begin() as c:
        _target(c, settings, project, "staging", staging_dir, gateway)
        production = _target(c, settings, project, "production", production_dir, gateway)
        # Even if staging has requires_approval=True (default) and production has no auto policy,
        # the manual workflow step gate is the ONE card.
        created = workflows.create_definition(
            c, project_id=project, workflow_key="cfg_single_gate",
            name="Config Single Gate", description=None, actor=PERSON, namespace="product",
        )
        version = workflows.publish_version(
            c,
            project_id=project,
            definition_id=created["id"],
            actor=PERSON,
            spec={
                "entry_step": "stage",
                "steps": [
                    {
                        "id": "stage",
                        "kind": "capability",
                        "approval": {"mode": "manual"},
                        "binding": {
                            "capability": "config_deploy",
                            "inputs": {
                                "app_name": "Northgate Gateway",
                                "environment": "staging",
                                "files": {"lumen-pay.yaml": "tenant: lumen-pay\nversion: 7\n"},
                            },
                        },
                        "next": ["promote"],
                    },
                    {
                        "id": "promote",
                        "kind": "capability",
                        "approval": {"mode": "manual"},
                        "binding": {
                            "capability": "promote_config",
                            "inputs": {
                                "deployment_id": {"from_step": "stage", "field": "deployment_id"},
                                "bundle_sha256": {"from_step": "stage", "field": "bundle_sha256"},
                                "target_id": production["target_id"],
                            },
                        },
                        "next": [],
                    },
                ],
            },
        )
        workflows.start_run(
            c, project_id=project, workflow_version_id=version["id"],
            actor=PERSON, input_data={}, dedupe_key="dk_cfg_single_gate",
        )

    try:
        with eng.begin() as c:
            runner.tick(c, ctx, project_id=project)
            decisions = c.execute(
                text("SELECT id, status, question FROM decisions WHERE project_id = :p "
                     "ORDER BY created_at"), {"p": project},
            ).mappings().all()
            assert len(decisions) == 1 and decisions[0]["status"] == "pending"
            stage_dec = decisions[0]["id"]
        with eng.begin() as c:
            DecisionService(settings).transition(
                c, stage_dec, action="approve", actor=PERSON, note="ship staging",
            )
        with eng.begin() as c:
            runner.tick(c, ctx, project_id=project)

        with eng.begin() as c:
            stage_run = c.execute(
                text("SELECT id, status, decision_id FROM deployment_runs "
                     "WHERE project_id = :p AND environment = 'staging'"), {"p": project},
            ).mappings().one()
            assert stage_run["status"] == "approved"
            assert stage_run["decision_id"] == stage_dec
            # Zero extra cards created for staging.
            all_decs = c.execute(
                text("SELECT id, status FROM decisions WHERE project_id = :p"), {"p": project},
            ).mappings().all()
            assert len(all_decs) == 1

        # Complete the staging deployment job so promote_config can run.
        assert _apply(eng, settings, project, stage_run["id"])["status"] == "succeeded"

        with eng.begin() as c:
            runner.tick(c, ctx, project_id=project)
            runner.tick(c, ctx, project_id=project)
            all_decs = c.execute(
                text("SELECT id, status, question FROM decisions WHERE project_id = :p "
                     "ORDER BY created_at"), {"p": project},
            ).mappings().all()
            assert len(all_decs) == 2 and all_decs[1]["status"] == "pending"
            prod_dec = all_decs[1]["id"]
        with eng.begin() as c:
            DecisionService(settings).transition(
                c, prod_dec, action="approve", actor=PERSON, note="ship prod",
            )
        with eng.begin() as c:
            runner.tick(c, ctx, project_id=project)

        with eng.begin() as c:
            prod_run = c.execute(
                text("SELECT id, status, decision_id FROM deployment_runs "
                     "WHERE project_id = :p AND environment = 'production'"), {"p": project},
            ).mappings().one()
            assert prod_run["status"] == "approved"
            assert prod_run["decision_id"] == prod_dec
            final_decs = c.execute(
                text("SELECT id, status, question, approved_by FROM decisions "
                     "WHERE project_id = :p AND status <> 'withdrawn'"),
                {"p": project},
            ).mappings().all()
            assert len(final_decs) == 2, f"Expected 1 active card per step gate, got {[dict(d) for d in final_decs]}"

        # Direct/unapproved production deploy without a gate decision STILL asks a person.
        unapproved = config_deploy.deploy_files(
            eng, settings, project_id=project, actor=PERSON,
            target_id=production["target_id"],
            files={"lumen-pay.yaml": "tenant: lumen-pay\nversion: 8\n"},
        )
        assert unapproved["status"] == "planned"
        assert unapproved["run_status"] == "awaiting_approval"
        assert unapproved["decision_id"]
        with eng.begin() as c:
            total_decs = c.execute(
                text("SELECT count(*) FROM decisions WHERE project_id = :p "
                     "AND status <> 'withdrawn'"), {"p": project},
            ).scalar_one()
            assert total_decs == 3
    finally:
        with eng.begin() as c:
            for tbl in ("deployment_gate_uses", "workflow_step_runs", "workflow_runs",
                        "workflow_versions", "workflow_definitions"):
                c.execute(text(f"DELETE FROM {tbl} WHERE project_id = :p"), {"p": project})
