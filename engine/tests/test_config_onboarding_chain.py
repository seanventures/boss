"""The config onboarding chain, through the real workflow runner (BOSSAPP-1132 / 1171 / 1173 / 1174).

"Onboard a config file" ends in: Staging (config_deploy) → Contract tests → Production
(promote_config, ONE approval) → Report; a failed contract test → Roll back staging
(rollback_config) → Report the rollback. These tests drive exactly those catalogue steps with the
runner's `tick`, the real config-deploy service and its governed apply job, a local HTTP
"gateway" (health, reload and the API the contract tests call) and a fake GitHub for the reports.

The build, review and merge in front of them need a coding agent and a repository; they are
covered by the build tests. Here staging ships the file from the run's input instead.
"""
from __future__ import annotations

import copy
import http.server
import json
import threading
import uuid

import pytest
from sqlalchemy import text

from boss_engine.config import Settings
from boss_engine.services import config_deploy, governed_host_deploy
from boss_engine.services import github_actions as ga
from boss_engine.services import validators
from boss_engine.services import workflow_capability_runner as runner
from boss_engine.services import workflow_catalogue as wc
from boss_engine.services import workflows
from boss_engine.services.deploy_policy import set_approval_policy
from boss_engine.services.jobs import JobLeaseService

from .test_config_is_deployed_through_a_governed_changeset import tidy  # noqa: F401
from .test_github_tools import FakeGitHub
from .test_workflow_capability_runner import eng, project  # noqa: F401

PERSON = "executive_console"          # agent_identity.CONSOLE_OPERATOR: a person
ENTRY = next(e for e in wc.CATALOGUE if e["key"] == "onboard_config_file")
TENANT = "lumen-pay.yaml"
V1 = "id: lumen-pay\nroutes:\n  - path: /v1/payments\n"


# ── the step text renderer (BOSSAPP-1171) ─────────────────────────────────────────────────────

VIEW = {"input": {"title": "Lumen Pay", "pr": 12},
        "steps": {"fix": {"output": {"validation_report": "### Validation\n| V-01 | **P0** |",
                                     "pull_request_url": "https://github.com/o/r/pull/7",
                                     "secret_ref": "sref_abcdef123456", "note": "sref_abcdef123456",
                                     "log": "token sk_live_abcdefghijklmnopqrstu",
                                     "findings": [1, 2]}},
                  "staging": {"output": {"deployed_url": "http://gw:8080", "passed": 0}}}}


def test_a_comment_carries_another_steps_result():
    out = workflows.render_step_text(
        "Rejected: {input.title}\n{steps.fix.output.validation_report}\nFix: "
        "{steps.fix.output.pull_request_url} (PR #{input.pr}, {steps.staging.output.passed} ok)",
        VIEW)
    assert out == ("Rejected: Lumen Pay\n### Validation\n| V-01 | **P0** |\nFix: "
                   "https://github.com/o/r/pull/7 (PR #12, 0 ok)")


def test_other_braces_are_text_and_a_value_is_never_filled_again():
    view = {"input": {"x": "{input.y}", "y": "no"}}
    assert workflows.render_step_text("cases: {X-Probe: boss} {input.x}", view) == \
        "cases: {X-Probe: boss} {input.y}"


def test_an_unresolved_placeholder_fails_naming_it_never_half_filled():
    for bad in ("{steps.fix.output.missing}", "{steps.nope.output.x}", "{input.absent}",
                "{steps.fix.output.findings}"):                 # a list is not text
        with pytest.raises(workflows.TemplateError, match=bad.replace(".", r"\.")
                           .replace("{", r"\{").replace("}", r"\}")):
            workflows.render_step_text(f"report: {bad}", VIEW)


def test_a_list_is_alternatives_the_first_complete_one_wins():
    assert workflows.render_step_text(
        ["A {steps.fix.output.missing}", "B {steps.staging.output.deployed_url}", "C"], VIEW) \
        == "B http://gw:8080"
    with pytest.raises(workflows.TemplateError, match="input.absent"):
        workflows.render_step_text(["{steps.x.output.y}", "{input.absent}"], VIEW)


def test_secrets_never_reach_a_steps_text():
    for named in ("{secrets.GITHUB_TOKEN}", "{env.API_KEY}", "{steps.fix.output.secret_ref}",
                  "{input.github_token}", "{steps.fix.output.api_key}"):
        with pytest.raises(workflows.TemplateError, match="names a secret"):
            workflows.render_step_text(f"x {named}", VIEW)
    # A value that IS a reference to a stored secret is refused, whatever the field is called.
    with pytest.raises(workflows.TemplateError, match="reference to a stored secret"):
        workflows.render_step_text("{steps.fix.output.note}", VIEW)
    # A credential-shaped value is masked; the words around it stay.
    said = workflows.render_step_text("{steps.fix.output.log}", VIEW)
    assert "sk_live_" not in said and said.startswith("token")
    # `tokens_used` merely contains the word: not a secret.
    assert workflows.render_step_text("{input.tokens_used}", {"input": {"tokens_used": 5}}) == "5"


def test_a_gate_card_keeps_its_rules():
    """The gate's subject/summary go through the same fill (workflows.fill_template)."""
    ctx = {"input": {"a": "x"}, "steps": {"s": {"output": {"n": "kgn_1"}}}}
    assert workflows._gate_text(None, "p", ["{input.missing}", "case {input.a}"], ctx) == "case x"
    filled, missing = workflows.fill_template("{steps.s.output.n|name}", ctx,
                                              words=lambda node, attr: f"Node {node}")
    assert (filled, missing) == ("Node kgn_1", [])


def test_only_text_inputs_of_reporting_steps_are_filled():
    from boss_engine.services.workflow_capabilities import CAPABILITIES

    assert CAPABILITIES["github_comment"].templated_inputs == ("body", "number")
    assert set(CAPABILITIES["notify"].templated_inputs) == {"message", "subject", "channel"}
    assert not CAPABILITIES["config_deploy"].templated_inputs
    ctx = {"input": {"pr": "12"}, "steps": {}}
    got = runner._rendered_inputs(CAPABILITIES["github_comment"],
                                  {"number": "{input.pr}", "body": "hi", "marker": "{input.pr}"}, ctx)
    assert got == {"number": "12", "body": "hi", "marker": "{input.pr}"}
    with pytest.raises(runner.SettledCapabilityError, match=r"'body'.*input\.gone"):
        runner._rendered_inputs(CAPABILITIES["github_comment"], {"body": "{input.gone}"}, ctx)
    # A gate's preview keeps an unfillable text as written; the step fails when it runs.
    assert runner._rendered_inputs(CAPABILITIES["github_comment"], {"body": "{input.gone}"}, ctx,
                                   strict=False) == {"body": "{input.gone}"}


# ── the validation report a PR comment carries (BOSSAPP-1131) ─────────────────────────────────

def _finding(fid, level, status, title, **kw):
    return {"id": fid, "severity": level, "status": status, "title": title,
            "source": {"rule_id": f"R-{fid}"}, "location": {"partner_line": 3}, **kw}


def test_the_report_leads_with_the_verdict_and_the_blocking_findings():
    rec = {"input": {"file": "config/tenants/lumen-pay.yaml"}, "validator": {"id": "gwlint"},
           "findings": [_finding("V-02", "P2", "open", "pii fields"),
                        _finding("V-01", "P0", "fixed", "plaintext upstream", fix_kind="auto"),
                        _finding("V-03", "P1", "open", "admin | path", fix_kind="human")]}
    md = validators.report_markdown(rec)
    assert md.splitlines()[0] == "### Validation of `config/tenants/lumen-pay.yaml`"
    assert "**1 blocking finding (P0/P1) open** · 3 found by gwlint · 1 fixed" in md
    rows = [ln for ln in md.splitlines() if ln.startswith("| V-")]
    assert rows[0].startswith("| V-03 | **P1** |") and "open: needs a person" in rows[0]
    assert "admin \\| path" in rows[0]
    assert rows[-1].startswith("| V-01 | **P0** |") and "fixed (auto)" in rows[-1]
    assert validators.checks_passed(rec) is False
    rec["findings"][2]["status"] = "fixed"
    assert validators.checks_passed(rec) is True
    assert "**Checks pass**" in validators.report_markdown(rec)
    assert validators.checks_passed({**rec, "error": "guard"}) is False
    assert validators.report_markdown(None) == "" and validators.checks_passed(None) is False


# ── the chain through the runner (BOSSAPP-1174 / 1132 / 1173) ──────────────────────────────────

class _Gateway(http.server.BaseHTTPRequestHandler):
    state = {"healthy": True, "api_ok": True, "reloads": 0}

    def do_GET(self):  # noqa: N802
        if self.path == "/readyz":
            ok = self.state["healthy"]
        elif self.path == "/v1/payments/ping":
            ok = self.state["api_ok"]
        else:
            ok = False
        self.send_response(200 if ok else 500)
        self.end_headers()
        self.wfile.write(b"ok" if ok else b"no")

    def do_POST(self):  # noqa: N802
        if self.path == "/-/reload":
            self.state["reloads"] += 1
        self.send_response(204)
        self.end_headers()

    def log_message(self, *args):
        return


@pytest.fixture(scope="module")
def gateway():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Gateway)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield {"base": f"http://127.0.0.1:{server.server_address[1]}", "state": _Gateway.state}
    server.shutdown()


@pytest.fixture()
def world(eng, project, gateway, tmp_path, monkeypatch):  # noqa: F811
    monkeypatch.setenv("BOSS_CONFIG_DEPLOY_ROOTS", str(tmp_path))
    gateway["state"].update(healthy=True, api_ok=True, reloads=0)
    staging, production = tmp_path / "staging" / "tenants", tmp_path / "production" / "tenants"
    staging.mkdir(parents=True)
    production.mkdir(parents=True)
    settings = Settings()
    with eng.begin() as c:
        for env, directory in (("staging", staging), ("production", production)):
            config_deploy.create_config_target(
                c, settings, project_id=project, app_name="Northgate Gateway", environment=env,
                directory=str(directory), reload_url=f"{gateway['base']}/-/reload",
                health_url=f"{gateway['base']}/readyz", actor=PERSON)
        # Staging ships without asking because a person said so; production always asks.
        set_approval_policy(c, project_id=project, environment="staging", requires_approval=False,
                            actor="usr_owner", note="staging is for trying things")
    fake = FakeGitHub()
    monkeypatch.setattr(ga, "adapter_for_repository", lambda db, s, p, repository: ga.BoundRepository(
        "bind_fake", "northgate/gateway", "main", fake))
    return {"settings": settings, "staging": staging, "production": production, "github": fake}


class _Ctx:
    def __init__(self, engine, settings):
        self.engine, self.settings = engine, settings

    def begin(self):
        return self.engine.begin()


def _publish(c, project_id):
    """The catalogue entry's own steps from Staging on, with Staging given the file inline."""
    steps = [copy.deepcopy(s) for s in ENTRY["steps"]
             if s["id"] not in ("fix", "review", "checks", "not_passing", "merge", "rejected")]
    for step in steps:
        if step["id"] == "staging":
            step["binding"]["inputs"] = {"files": {"from_input": "files"},
                                         "app_name": "Northgate Gateway",
                                         "environment": "staging"}
        if step["id"] == "contract_tests":
            step["binding"]["inputs"]["cases"] = [
                {"name": "ping", "path": "/v1/payments/ping", "expect_status": 200}]
    name = f"onboard_{uuid.uuid4().hex[:6]}"
    definition = workflows.create_definition(c, project_id=project_id, workflow_key=name,
                                             name="Onboard a config file", description=None,
                                             actor="test", namespace="product")
    return workflows.publish_version(c, project_id=project_id, definition_id=definition["id"],
                                     actor="test", spec={"entry_step": "staging", "steps": steps})


def _steps(c, run_id):
    rows = c.execute(text("SELECT step_id, status, output, last_error, decision_id "
                          "FROM workflow_step_runs WHERE workflow_run_id = :r "
                          "ORDER BY created_at"), {"r": run_id}).mappings().all()
    return {r["step_id"]: dict(r) for r in rows}


def _apply_jobs(eng, settings, project_id):  # noqa: F811
    """What the worker does: run every queued config apply job."""
    with eng.begin() as c:
        jobs = c.execute(text("SELECT j.id, d.id AS run FROM jobs j JOIN deployment_runs d "
                              "ON d.id = j.payload->>'deployment_run_id' "
                              "WHERE j.project_id = :p AND j.kind = 'deployment.host_apply' "
                              "AND d.status = 'approved'"),
                         {"p": project_id}).mappings().all()
    for job in jobs:
        with eng.begin() as c:
            lease = JobLeaseService(settings).lease_owned_job(
                c, project_id=project_id, job_id=job["id"], worker_id="w1", lease_ttl_seconds=600)
        governed_host_deploy.execute_host_job(eng, settings, deployment_run_id=job["run"],
                                              job_id=job["id"], worker_id="w1",
                                              lease_token=lease["lease_token"])
    return len(jobs)


def _drive(eng, world, project_id, run_id, *, until, rounds=12):  # noqa: F811
    ctx = _Ctx(eng, world["settings"])
    for _ in range(rounds):
        with eng.begin() as c:
            runner.tick(c, ctx, project_id=project_id)
        _apply_jobs(eng, world["settings"], project_id)
        with eng.begin() as c:
            steps = _steps(c, run_id)
        if until(steps):
            return steps
    return steps


def _start(eng, world, project_id):  # noqa: F811
    with eng.begin() as c:
        version = _publish(c, project_id)
        return workflows.start_run(
            c, project_id=project_id, actor=PERSON,
            input_data={"title": "Lumen Pay", "files": {TENANT: V1},
                        "notify_channel": "github:#12"},
            dedupe_key=f"dk_{uuid.uuid4().hex[:10]}", workflow_version_id=version["id"])


def _pending_decisions(c, project_id):
    return c.execute(text("SELECT id, question FROM decisions WHERE project_id = :p "
                          "AND status = 'pending'"), {"p": project_id}).mappings().all()


def test_staging_contract_tests_and_one_approval_to_production(eng, project, world):  # noqa: F811
    run = _start(eng, world, project)
    steps = _drive(eng, world, project, run["id"],
                   until=lambda s: s.get("production", {}).get("status") == "waiting")
    assert steps["staging"]["status"] == "succeeded", steps["staging"]
    assert (world["staging"] / TENANT).read_text() == V1
    assert steps["contract_tests"]["status"] == "succeeded", steps["contract_tests"]
    assert steps["contract_tests"]["output"]["passed"] == 1
    # ONE card for production: the Production step's own gate. Nothing has shipped yet.
    with eng.begin() as c:
        pending = _pending_decisions(c, project)
    assert len(pending) == 1 and steps["production"]["decision_id"] == pending[0]["id"]
    assert not (world["production"] / TENANT).exists()
    with eng.begin() as c:
        c.execute(text("UPDATE decisions SET status = 'approved', approved_by = :a WHERE id = :d"),
                  {"a": PERSON, "d": pending[0]["id"]})
    steps = _drive(eng, world, project, run["id"],
                   until=lambda s: s.get("report", {}).get("status") == "succeeded")
    assert steps["production"]["status"] == "succeeded", steps["production"]
    assert (world["production"] / TENANT).read_text() == V1
    with eng.begin() as c:
        assert _pending_decisions(c, project) == [], "the promotion asked a second time"
        prod = c.execute(text("SELECT decision_id, trigger FROM deployment_runs WHERE id = :i"),
                         {"i": steps["production"]["output"]["deployment_id"]}).mappings().one()
    assert prod["trigger"] == "promotion" and prod["decision_id"] == pending[0]["id"]
    # The outcome is on the pull request that started the run, with the staging URL.
    body = world["github"].comments[12][-1]["body"]
    assert "Onboarded: Lumen Pay" in body and world_base(world, steps) in body
    assert "contract tests 1/1 passed" in body
    assert steps["roll_back"]["status"] == "skipped"


def world_base(world, steps):
    return steps["staging"]["output"]["deployed_url"]


def test_failed_contract_tests_roll_staging_back_and_say_so(eng, project, world, gateway):  # noqa: F811
    (world["staging"] / "orbit.yaml").write_text("id: orbit\n")      # another tenant, untouched
    gateway["state"]["api_ok"] = False
    run = _start(eng, world, project)
    steps = _drive(eng, world, project, run["id"],
                   until=lambda s: s.get("report_rollback", {}).get("status") == "succeeded")
    assert steps["contract_tests"]["status"] == "failed"
    assert steps["roll_back"]["status"] == "succeeded", steps["roll_back"]
    # Staging serves what it served before: the new tenant file is gone, the other untouched.
    assert not (world["staging"] / TENANT).exists()
    assert (world["staging"] / "orbit.yaml").read_text() == "id: orbit\n"
    staged = steps["staging"]["output"]["deployment_id"]
    with eng.begin() as c:
        rows = {r["id"]: dict(r) for r in c.execute(
            text("SELECT id, status, trigger, release_receipt_ids FROM deployment_runs "
                 "WHERE project_id = :p"), {"p": project}).mappings()}
        assert _pending_decisions(c, project) == [], "a staging rollback asked a person"
    rollback = rows[steps["roll_back"]["output"]["deployment_id"]]
    assert rollback["trigger"] == "rollback" and rollback["status"] == "succeeded"
    assert rollback["release_receipt_ids"], "the rollback has its receipt"
    assert rows[staged]["status"] == "rolled_back", "the undone deployment is never promoted"
    assert steps["production"]["status"] == "skipped" and steps["report"]["status"] == "skipped"
    body = world["github"].comments[12][-1]["body"]
    assert "Rolled back from staging: Lumen Pay" in body and "| ping |" in body
    # Promoting the rolled-back deployment is refused; a second rollback is the same one.
    promoted = config_deploy.promote(eng, world["settings"], project_id=project, actor=PERSON,
                                     deployment_run_id=staged)
    assert promoted["status"] == "failed" and "rolled_back" in promoted["reason"]
    again = config_deploy.rollback(eng, world["settings"], project_id=project, actor=PERSON,
                                   deployment_run_id=staged)
    assert again["deployment_run_id"] == rollback["id"] and again["reused"] is True


def test_a_production_rollback_asks_a_person(eng, project, world):  # noqa: F811
    """Production is restored only on a person's answer, as it is promoted."""
    settings = world["settings"]
    with eng.begin() as c:
        set_approval_policy(c, project_id=project, environment="production",
                            requires_approval=False, actor="usr_owner", note="never answers prod")
    planned = config_deploy.deploy_files(eng, settings, project_id=project, actor=PERSON,
                                         app_name="Northgate Gateway", environment="production",
                                         files={TENANT: V1})
    with eng.begin() as c:
        c.execute(text("UPDATE decisions SET status = 'approved', approved_by = :a WHERE id = :d"),
                  {"a": PERSON, "d": planned["decision_id"]})
        governed_host_deploy.on_decision_approved(
            c, settings, dict(c.execute(text("SELECT * FROM decisions WHERE id = :d"),
                                        {"d": planned["decision_id"]}).mappings().one()), PERSON)
    _apply_jobs(eng, settings, project)
    assert (world["production"] / TENANT).read_text() == V1
    back = config_deploy.rollback(eng, settings, project_id=project, actor=PERSON,
                                  deployment_run_id=planned["deployment_run_id"])
    assert back["status"] == "planned" and back["run_status"] == "awaiting_approval", back
    assert back["approval"] is None and back["decision_id"]
    assert (world["production"] / TENANT).exists(), "nothing restored before a person says so"
    # Named, never guessed; a run's step only reaches its own deployments.
    assert "Name the config deployment" in config_deploy.rollback(
        eng, settings, project_id=project, actor=PERSON)["reason"]
    foreign = config_deploy.rollback(eng, settings, project_id=project, actor=PERSON,
                                     deployment_run_id=planned["deployment_run_id"],
                                     workflow_run_id="wfr_other")
    assert foreign["status"] == "failed" and "not planned by this workflow run" in foreign["reason"]


def test_a_rollback_step_rolls_back_only_what_its_run_shipped(eng, project):  # noqa: F811
    spec = {"entry_step": "staging", "steps": [
        {"id": "staging", "kind": "capability", "binding": {"capability": "config_deploy"}},
        {"id": "relay", "kind": "capability", "binding": {"custom_step": "step_relay"}},
        {"id": "back", "kind": "capability", "binding": {"capability": "rollback_config"}}]}
    context = {"run_id": "wfr_a", "steps": {
        "staging": {"deployment_id": "deprun_a"},
        "relay": {"capability": "config_deploy", "deployment_id": "deprun_b"}}}
    with eng.begin() as c:
        assert runner._bound_rollback(c, context, {}, spec=spec) == {"deployment_run_id": "deprun_a"}
        with pytest.raises(runner.SettledCapabilityError, match="not shipped by this run"):
            runner._bound_rollback(c, context, {"deployment_id": "deprun_b"}, spec=spec)
        with pytest.raises(runner.SettledCapabilityError, match="nothing of this run's"):
            runner._bound_rollback(c, {"run_id": "wfr_a", "steps": {}}, {}, spec=spec)


def test_the_rejection_report_reads_the_failed_builds_record():
    """A build that fails into `rejected` keeps its output on the run (step_status failed,
    BOSSAPP-1168): the report and the fix PR are said on the pull request."""
    rejected = next(s for s in ENTRY["steps"] if s["id"] == "rejected")
    record = {"input": {"file": "config/tenants/lumen-pay.yaml"}, "validator": {"id": "gwlint"},
              "findings": [_finding("V-01", "P0", "open", "admin route", fix_kind="human")]}
    context = {"input": {"title": "Lumen Pay"}, "steps": {"fix": {
        "step_status": "failed", "pull_request_url": "https://github.com/o/r/pull/7",
        "validation_report": validators.report_markdown(record)}}}
    message = workflows.render_step_text(rejected["binding"]["inputs"]["message"],
                                         runner.template_view(context))
    assert message.startswith("Not onboarded: Lumen Pay\n### Validation")
    assert "| V-01 | **P0** |" in message and "https://github.com/o/r/pull/7" in message
    # Nothing from the build at all: the last alternative still says what happened.
    bare = workflows.render_step_text(rejected["binding"]["inputs"]["message"],
                                      runner.template_view({"input": {"title": "Lumen Pay"}}))
    assert bare.startswith("Not onboarded: Lumen Pay\nThe file could not be checked")


def test_a_remediation_build_says_its_report_and_verdict(eng, project):  # noqa: F811
    record = {"input": {"file": "a.yaml"}, "findings": [_finding("V-01", "P1", "open", "x")]}
    with eng.begin() as c:
        rid = f"crun_{uuid.uuid4().hex[:10]}"
        c.execute(text("INSERT INTO code_task_runs (id, project_id, title, status, state) VALUES "
                       "(:i, :p, 'fix a.yaml', 'completed', CAST(:s AS JSONB))"),
                  {"i": rid, "p": project, "s": json.dumps({"validation": record})})
        try:
            out = runner._remediation_record(c, rid)
        finally:
            c.execute(text("DELETE FROM code_task_runs WHERE id = :i"), {"i": rid})
    assert out["checks_passed"] is False and out["open_findings"] == 1
    assert out["validation_report"].startswith("### Validation of `a.yaml`")



# ── one pull request event, one run (BOSSAPP-1133) ────────────────────────────────────────────

def test_one_event_starts_the_workflow_once(eng, project):  # noqa: F811
    """The intake fires one agent turn per event (webhook and poll copies of the same head
    commit are one event: test_github_intake_files_and_poll). The agent's start names what
    started it, so a retried turn or a second start for the same commit answers with the run
    already going instead of starting a second."""
    from boss_engine.tools.builtin import _start_dedupe_key, build_default_registry

    with eng.begin() as c:
        name = f"once_{uuid.uuid4().hex[:6]}"
        definition = workflows.create_definition(c, project_id=project, workflow_key=name,
                                                 name=name, description=None, actor="test",
                                                 namespace="product")
        workflows.publish_version(c, project_id=project, definition_id=definition["id"],
                                  actor="test", spec={"entry_step": "a", "steps": [
                                      {"id": "a", "kind": "capability", "next": [],
                                       "binding": {"capability": "cost_check", "inputs": {}}}]})
    tool = {s.name: s for s in build_default_registry().specs()}["start_workflow"]

    def start(once_per=None):
        args = {"directive": "onboard lumen-pay", "workflow_key": name, "title": "Lumen Pay"}
        if once_per:
            args["once_per"] = once_per
        return tool.handler(_Ctx(eng, Settings()), project_id=project, actor=PERSON, args=args)

    first = start("northgate/tenants#12@abc1234")
    again = start("northgate/tenants#12@abc1234")
    assert first["status"] == "running" and first["run_id"]
    assert again["run_id"] == first["run_id"] and again["already_started"] is True
    assert again["status"] == first["status"] == "running"
    with eng.begin() as c:
        c.execute(text("UPDATE workflow_runs SET status = 'completed' WHERE id = :r"),
                  {"r": first["run_id"]})
    assert start("northgate/tenants#12@abc1234")["status"] == "completed", "its real status"
    pushed = start("northgate/tenants#12@def5678")          # a new commit is new work
    assert pushed["run_id"] != first["run_id"] and "already_started" not in pushed
    assert start()["run_id"] != start()["run_id"], "without once_per every start is its own"
    with eng.begin() as c:
        runs = c.execute(text("SELECT count(*) FROM workflow_runs WHERE project_id = :p"),
                         {"p": project}).scalar()
    assert runs == 4
    key = _start_dedupe_key("w", "  a   b ", lambda: "x")
    assert key == _start_dedupe_key("w", "a b", lambda: "y") and "a b" not in key


# ── review of #329 ─────────────────────────────────────────────────────────────────────────────

def test_a_values_markup_is_neutralised_and_secret_refs_never_pass():
    view = {"input": {"note": "<!-- hidden --> [click](javascript:alert(1)) @everyone hi@x.com",
                      "ref": "ok line\nsref_abcdef123456\nmore",
                      "app": "see app_secret://prod/gw/API_KEY now"}}
    said = workflows.render_step_text("Note: {input.note}", view)
    assert "<!--" not in said and "&lt;!--" in said
    assert "\\[click\\](javascript:" in said, "a link cannot form"
    assert "@everyone" not in said and "hi@x.com" in said
    # The template's own words are the author's and stay as written.
    assert workflows.render_step_text("**[Docs](https://x)** <b>{input.app}</b>", view) == \
        "**[Docs](https://x)** <b>see \\[SECRET REF\\] now</b>"
    with pytest.raises(workflows.TemplateError, match="reference to a stored secret"):
        workflows.render_step_text("{input.ref}", view)


def _stage(eng, world, project_id, body=V1, run_id=None):  # noqa: F811
    planned = config_deploy.deploy_files(eng, world["settings"], project_id=project_id,
                                         actor=PERSON, app_name="Northgate Gateway",
                                         environment="staging", files={TENANT: body},
                                         workflow_run_id=run_id)
    assert planned["status"] == "planned", planned
    _apply_jobs(eng, world["settings"], project_id)
    return planned


def _close(eng, world, deployment_run_id):  # noqa: F811
    """Close a planned deployment the way BOSS does (its ChangeSet, reservation, surface)."""
    with eng.begin() as c:
        row = dict(c.execute(text("SELECT * FROM deployment_runs WHERE id = :i"),
                             {"i": deployment_run_id}).mappings().one())
        governed_host_deploy.supersede(c, world["settings"], row, reason="test", actor=PERSON)


def test_an_agent_cannot_reuse_a_released_gate_for_production(eng, project, world):  # noqa: F811
    """Production takes only the runner's verified gate of ITS OWN step, run and bundle, once."""
    from boss_engine.services import deploy_policy

    from .test_workflow_capability_runner import _publish as publish_steps, _start as start_run

    with eng.begin() as c:
        _, version = publish_steps(c, project, f"gate_{uuid.uuid4().hex[:6]}", [
            {"id": "prod", "kind": "capability", "next": [],
             "binding": {"capability": "promote_config",
                         "inputs": {"deployment_id": "x"}}}], "prod")
        run = start_run(c, project, version["id"], {})
    staged = _stage(eng, world, project, run_id=run["id"])
    with eng.begin() as c:
        gate = f"dec_gate_{uuid.uuid4().hex[:10]}"
        c.execute(text("INSERT INTO decisions (id, project_id, question, status, approved_by) "
                       "VALUES (:d, :p, 'Promote?', 'approved', :a)"),
                  {"d": gate, "p": project, "a": PERSON})
        c.execute(text("UPDATE workflow_step_runs SET decision_id = :d, binding_snapshot = "
                       "COALESCE(binding_snapshot, '{}'::jsonb) || CAST(:s AS JSONB) "
                       "WHERE workflow_run_id = :r"),
                  {"d": gate, "s": json.dumps({"capability": "promote_config",
                                               "released_for": gate}), "r": run["id"]})
        mine = deploy_policy.released_gate(c, project, gate, capability="promote_config",
                                           run_id=run["id"],
                                           bundle_sha256=staged["bundle_sha256"])
        assert mine and mine["run_id"] == run["id"]
        # Another run, or another capability, does not get it.
        assert deploy_policy.released_gate(c, project, gate, capability="promote_config",
                                           run_id="wfr_other") is None
        assert deploy_policy.released_gate(c, project, gate, capability="rollback_config",
                                           run_id=run["id"]) is None
    # An agent naming the gate as an argument, inside its own run or none: a person is asked.
    for run_id in (None, run["id"]):
        named = config_deploy.promote(eng, world["settings"], project_id=project,
                                      actor="agent:deploy-lead",
                                      deployment_run_id=staged["deployment_run_id"],
                                      approval_decision_id=gate, workflow_run_id=run_id,
                                      expected_bundle_sha256=staged["bundle_sha256"])
        assert named["status"] == "planned" and named["approval"] is None, named
        assert named["decision_id"] != gate
        _close(eng, world, named["deployment_run_id"])
    # A gate recorded for another bundle does not ship this one.
    other = config_deploy.promote(eng, world["settings"], project_id=project, actor=PERSON,
                                  deployment_run_id=staged["deployment_run_id"],
                                  workflow_run_id=run["id"],
                                  expected_bundle_sha256=staged["bundle_sha256"],
                                  released_gate={**mine, "bundle_sha256": "f" * 64})
    assert other.get("approval") is None, other
    _close(eng, world, other["deployment_run_id"])
    # The runner's own gate ships once; the same gate again asks a person.
    ok = config_deploy.promote(eng, world["settings"], project_id=project, actor=PERSON,
                               deployment_run_id=staged["deployment_run_id"],
                               workflow_run_id=run["id"],
                               expected_bundle_sha256=staged["bundle_sha256"], released_gate=mine)
    assert ok["approval"] and ok["approval"]["decision_id"] == gate, ok
    with eng.begin() as c:
        use = c.execute(text("SELECT workflow_run_id, capability, target_id, bundle_sha256 "
                             "FROM deployment_gate_uses WHERE decision_id = :d"),
                        {"d": gate}).mappings().one()
        assert use["workflow_run_id"] == run["id"] and use["capability"] == "promote_config"
        assert use["bundle_sha256"] == staged["bundle_sha256"] and use["target_id"]
        c.execute(text("DELETE FROM deployment_gate_uses WHERE decision_id = :d"), {"d": gate})
    _close(eng, world, ok["deployment_run_id"])


def test_a_file_failing_its_checks_is_rejected_on_the_pr_without_a_person(
        eng, project, world, monkeypatch):  # noqa: F811
    """S2: an open P0 after the fix goes straight to the rejection comment, with the report and
    the build's PR as the proposed fix — the run never waits at a merge card."""
    record = {"input": {"file": "config/tenants/lumen-pay.yaml"}, "validator": {"id": "gwlint"},
              "findings": [_finding("V-01", "P0", "open", "admin route exposed", fix_kind="human")]}
    real = runner._perform_capability

    def fake(connection, ctx, *, key, **kw):
        if key == "build":
            return {"capability": "build", "status": "succeeded",
                    "pull_request_url": "https://github.com/o/r/pull/7",
                    "validation_report": validators.report_markdown(record),
                    "checks_passed": validators.checks_passed(record), "open_findings": 1}
        if key == "review":
            return {"capability": "review", "status": "succeeded", "verdict": "findings"}
        return real(connection, ctx, key=key, **kw)

    monkeypatch.setattr(runner, "_perform_capability", fake)
    steps = [copy.deepcopy(s) for s in ENTRY["steps"]
             if s["id"] in ("fix", "review", "checks", "not_passing", "merge", "rejected")]
    for step in steps:
        if step["id"] == "merge":
            step["next"] = []
    with eng.begin() as c:
        name = f"reject_{uuid.uuid4().hex[:6]}"
        definition = workflows.create_definition(c, project_id=project, workflow_key=name,
                                                 name="Onboard a config file", description=None,
                                                 actor="test", namespace="product")
        version = workflows.publish_version(c, project_id=project,
                                            definition_id=definition["id"], actor="test",
                                            spec={"entry_step": "fix", "steps": steps})
        run = workflows.start_run(c, project_id=project, actor=PERSON,
                                  input_data={"title": "Lumen Pay", "directive": "onboard it",
                                              "start_from": "config/tenants/lumen-pay.yaml",
                                              "target_path": "config/tenants/lumen-pay.yaml",
                                              "notify_channel": "github:#12"},
                                  dedupe_key=f"dk_{uuid.uuid4().hex[:10]}",
                                  workflow_version_id=version["id"])
    got = _drive(eng, world, project, run["id"],
                 until=lambda s: s.get("not_passing", {}).get("status") == "succeeded")
    assert got["not_passing"]["status"] == "succeeded", got
    assert got.get("merge", {}).get("status") in (None, "skipped", "pending")
    with eng.begin() as c:
        assert _pending_decisions(c, project) == [], "a person was asked"
    body = world["github"].comments[12][-1]["body"]
    assert "Not onboarded: Lumen Pay" in body and "| V-01 | **P0** |" in body
    assert "https://github.com/o/r/pull/7" in body


def test_a_rollback_waits_for_a_deployment_in_flight_and_says_when_it_left_it_unhealthy(
        eng, project, world, gateway, monkeypatch):  # noqa: F811
    settings = world["settings"]
    first = _stage(eng, world, project)
    # Another deployment approved and not yet applied on the same directory: no rollback now.
    pending = config_deploy.deploy_files(eng, settings, project_id=project, actor=PERSON,
                                         app_name="Northgate Gateway", environment="staging",
                                         files={"orbit.yaml": "id: orbit\n"})
    refused = config_deploy.rollback(eng, settings, project_id=project, actor=PERSON,
                                     deployment_run_id=first["deployment_run_id"])
    assert refused["status"] == "failed" and "would race it" in refused["reason"], refused
    _apply_jobs(eng, settings, project)
    assert pending["status"] == "planned"
    # The restore lands but the gateway stays down: the undone deployment is never "last good".
    gateway["state"]["healthy"] = False
    monkeypatch.setattr(config_deploy, "HEALTH_WAIT_SECONDS", 1)
    back = config_deploy.rollback(eng, settings, project_id=project, actor=PERSON,
                                  deployment_run_id=first["deployment_run_id"])
    assert back["status"] == "planned", back
    _apply_jobs(eng, settings, project)
    gateway["state"]["healthy"] = True
    with eng.begin() as c:
        rows = {r["id"]: r["status"] for r in c.execute(
            text("SELECT id, status FROM deployment_runs WHERE project_id = :p"),
            {"p": project}).mappings()}
    assert rows[back["deployment_run_id"]] == config_deploy.RESTORED_UNHEALTHY
    assert rows[first["deployment_run_id"]] == "rollback_unhealthy"
    assert not (world["staging"] / TENANT).exists()


def test_an_intake_run_is_told_its_once_per_and_where_to_report():
    from boss_engine.services import intake_router

    pr = {"event_id": "ev1", "data": {"head_key": "pr:northgate/tenants#12@abc1234"}}
    assert intake_router.start_hints(pr) == {"once_per": "pr:northgate/tenants#12@abc1234",
                                             "notify_channel": "github:northgate/tenants#12"}
    assert intake_router.start_hints({"event_id": "ev2", "data": {}}) == {"once_per": "intake:ev2"}
    assert intake_router.start_hints({"text": "hello"}) == {}
    words = intake_router.start_hint_words(pr)
    assert 'once_per="pr:northgate/tenants#12@abc1234"' in words
    assert 'inputs.notify_channel="github:northgate/tenants#12"' in words
