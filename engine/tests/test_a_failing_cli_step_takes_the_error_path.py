"""A workflow step that runs a command fails when the command exits non-zero (AMEX-G3,
BOSSAPP-1139).

`skill.sandboxed_command` returned `exit_code` and the runner read only the invocation's own
status, so a validator that exited 1 — "violations found", the one thing it exists to say — went
green and the run went on to deploy. Now:

* a sandboxed_command skill carries `fail_on_exit`, ON for every skill authored from here;
* skills authored before the flag existed are given an explicit `false` by db/schema.sql, so
  nothing changes under a running workflow;
* with it on, a non-zero exit fails the step: the reason names the code and quotes the log tail,
  the step run keeps `exit_code` and `log_tail` as evidence, and `on_error` / retries apply.
"""
from __future__ import annotations

import re
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text

from boss_engine.services import skill_definitions as sd
from boss_engine.services import workflow_capability_runner as runner
from boss_engine.services import workflow_custom_steps as custom_steps
from boss_engine.services import workflows

from .test_workflow_capability_runner import RecordingRegistry, RecordingTool
from .amex_workflow_fixtures import eng, project  # noqa: F401
from .test_workflow_custom_steps import HOLDER, Ctx, _agent

IMAGE = "ghcr.io/northgate/gwlint@sha256:" + "a" * 64
REPO = Path(__file__).resolve().parents[2]


# ── the flag ───────────────────────────────────────────────────────────────────────────────

def _config(**extra):
    return sd.validate_config("sandboxed_command",
                              {"image": IMAGE, "command": "gwlint validate {file}", **extra},
                              ["file"])


def test_a_new_sandboxed_command_skill_fails_on_exit_by_default():
    assert _config()["fail_on_exit"] is True


@pytest.mark.parametrize("given", [False, "false", "0", "no", "off", 0])
def test_an_author_can_switch_it_off(given):
    assert _config(fail_on_exit=given)["fail_on_exit"] is False


def test_fails_on_exit_reads_the_stored_config():
    assert sd.fails_on_exit({"config": {"fail_on_exit": True}}) is True
    assert sd.fails_on_exit({"config": {"fail_on_exit": False}}) is False
    # A row that never went through validate_config nor the schema's backfill: judged as the
    # author of a new skill would expect, not silently the old way.
    assert sd.fails_on_exit({"config": {"image": IMAGE}}) is True
    assert sd.fails_on_exit(None) is False
    assert sd.fails_on_exit({}) is False


# ── the backfill ───────────────────────────────────────────────────────────────────────────

BACKFILL_KEY = "amex_g3_fail_on_exit_false"


def _backfill_block(key: str) -> str:
    """The schema's one-time backfill, under `key` so the test does not depend on (or disturb)
    the marker the real boot already wrote."""
    sql = (REPO / "db" / "schema.sql").read_text(encoding="utf-8")
    match = re.search(r"DO \$\$\nDECLARE changed INTEGER;.*?END \$\$;", sql, re.S)
    assert match and BACKFILL_KEY in match.group(0), "db/schema.sql lost the AMEX-G3 backfill"
    return match.group(0).replace(BACKFILL_KEY, key)


def _raw_skill(c, project_id, key, config):
    import json
    c.execute(text(
        "INSERT INTO skill_definitions (id, project_id, skill_key, label, behaviour_kind, "
        "inputs, produces, config, risk_tier, trusted, enabled, created_by) VALUES "
        "(:i, :p, :k, :k, 'sandboxed_command', '[]'::jsonb, '[]'::jsonb, "
        "CAST(:c AS JSONB), 1, true, true, 'test')"),
        {"i": f"skd_{uuid.uuid4().hex[:10]}", "p": project_id, "k": key,
         "c": json.dumps(config)})


def test_skills_authored_before_the_flag_keep_todays_behaviour_once(eng, project):
    """The backfill writes an explicit false ONLY where the key is absent, and runs ONCE: a
    key-less row that appears after it ran is not touched by the next boot."""
    key = f"test_{uuid.uuid4().hex[:8]}"
    block = _backfill_block(key)
    try:
        with eng.begin() as c:
            _raw_skill(c, project, "old_lint", {"image": IMAGE, "command": "lint"})
            _raw_skill(c, project, "new_lint", {"image": IMAGE, "command": "lint",
                                                "fail_on_exit": True})
            c.exec_driver_sql(block)
            _raw_skill(c, project, "later_lint", {"image": IMAGE, "command": "lint"})
            c.exec_driver_sql(block)               # the next boot: recorded, so a no-op
            old = sd.get_skill(c, project, "old_lint")
            new = sd.get_skill(c, project, "new_lint")
            later = sd.get_skill(c, project, "later_lint")
            assert c.execute(text("SELECT count(*) FROM schema_backfills WHERE key=:k"),
                             {"k": key}).scalar() == 1
    finally:
        with eng.begin() as c:
            c.execute(text("DELETE FROM schema_backfills WHERE key=:k"), {"k": key})
    assert old["config"]["fail_on_exit"] is False and sd.fails_on_exit(old) is False
    assert new["config"]["fail_on_exit"] is True and sd.fails_on_exit(new) is True
    assert "fail_on_exit" not in later["config"], "the second boot rewrote a row"


def test_an_edit_that_does_not_send_the_flag_keeps_the_stored_value(eng, project):
    """The skill drawer did not send the key and validate_config defaulted it to true, so the
    first edit of a backfilled skill silently switched it on."""
    def save(config):
        sd.save_skill(c, project_id=project, actor="test", skill_key="lint",
                      label="Lint", behaviour_kind="sandboxed_command",
                      inputs=[{"name": "file", "required": True}], produces=[{"name": "report"}],
                      config={"image": IMAGE, "command": "lint {file}", **config},
                      risk_tier=0, trusted=True)
        return sd.get_skill(c, project, "lint")["config"]["fail_on_exit"]

    with eng.begin() as c:
        assert save({"fail_on_exit": False}) is False
        assert save({}) is False, "an edit without the key flipped it on"
        assert save({"fail_on_exit": True}) is True
        assert save({}) is True


# ── the step ───────────────────────────────────────────────────────────────────────────────

def _authored(c, project_id, key, *, fail_on_exit):
    sd.save_skill(
        c, project_id=project_id, actor="test", skill_key=key, label="Validate tenant YAML",
        behaviour_kind="sandboxed_command", inputs=[{"name": "file", "required": True}],
        produces=[{"name": "report"}],
        config={"image": IMAGE, "command": "gwlint validate {file}",
                "fail_on_exit": fail_on_exit},
        risk_tier=0, trusted=True)
    _agent(c, project_id, HOLDER, key)
    custom_steps.create(
        c, project_id=project_id, label="Validate", binding_kind="skill", skill_id=key,
        actor="test", step_key=f"step_{key}", inputs=[{"name": "file", "required": True}])
    return f"step_{key}"


def _publish(c, project_id, step_key):
    name = f"wf_{uuid.uuid4().hex[:6]}"
    definition = workflows.create_definition(
        c, project_id=project_id, workflow_key=name, name=name, description=None,
        actor="test", namespace="product")
    return workflows.publish_version(
        c, project_id=project_id, definition_id=definition["id"], actor="test",
        spec={"entry_step": "validate", "steps": [
            # NO CONDITION STEP. The exit code is the step's own verdict: it fails into `reject`.
            {"id": "validate", "kind": "capability", "next": ["deploy"], "on_error": "reject",
             "max_attempts": 1, "approval": {"mode": "auto"},
             "binding": {"custom_step": step_key, "inputs": {"file": "tenants/lumen-pay.yaml"}}},
            {"id": "deploy", "kind": "capability", "approval": {"mode": "auto"},
             "binding": {"capability": "cost_check", "inputs": {}}},
            {"id": "reject", "kind": "capability", "approval": {"mode": "auto"},
             "binding": {"capability": "cost_check", "inputs": {}}},
        ]})


def _start(c, project_id, version_id):
    return workflows.start_run(
        c, project_id=project_id, actor="test", input_data={},
        dedupe_key=f"dk_{uuid.uuid4().hex[:10]}", workflow_version_id=version_id)


def _rows(c, run_id):
    rows = c.execute(text("SELECT step_id, status, output, last_error FROM workflow_step_runs "
                          "WHERE workflow_run_id=:r"), {"r": run_id}).mappings().all()
    return {r["step_id"]: dict(r) for r in rows}


def _run_status(c, run_id):
    return c.execute(text("SELECT status FROM workflow_runs WHERE id=:r"), {"r": run_id}).scalar()


EXITED = {"status": "succeeded",
          "result": {"skill_key": "gwlint", "image": IMAGE, "exit_code": 1, "produced": {},
                     "log_tail": "tenants/lumen-pay.yaml:14 SEC-004 upstream must be https://\n"
                                 "1 violation\nexit status 1"}}


def _wire(monkeypatch, invoke_result):
    import boss_engine.services.agent_loop as agent_loop
    monkeypatch.setattr(agent_loop, "_invoke_agent_skill", lambda *a, **k: dict(invoke_result))
    # The error step and the happy-path step are `cost_check`: one call, finishes in the call.
    monkeypatch.setattr(runner, "_tool_registry",
                        lambda: RecordingRegistry([RecordingTool("get_costs", {"spend_usd": 0})]))


def test_a_non_zero_exit_fails_the_step_and_the_run_takes_on_error(eng, project, monkeypatch):
    _wire(monkeypatch, EXITED)
    with eng.begin() as c:
        step_key = _authored(c, project, f"gwlint_{uuid.uuid4().hex[:6]}", fail_on_exit=True)
        run = _start(c, project, _publish(c, project, step_key)["id"])
        runner.tick(c, Ctx(eng), project_id=project)
        rows = _rows(c, run["id"])
        validate = rows["validate"]
        assert validate["status"] == "failed", rows
        # THE REASON IS THE CODE AND THE LOG, not "the skill did not run".
        assert "exited with code 1" in validate["last_error"]
        assert "SEC-004" in validate["last_error"]
        # THE EVIDENCE SURVIVES on the step run.
        assert validate["output"]["exit_code"] == 1
        assert "SEC-004" in validate["output"]["log_tail"]
        assert validate["output"]["_step"]["custom_step"] == step_key
        # The run went on along `on_error`, not into a dead letter.
        assert validate["output"]["error_path_taken"] == "reject"
        assert _run_status(c, run["id"]) == "running"
        runner.tick(c, Ctx(eng), project_id=project)
        rows = _rows(c, run["id"])
    assert rows["reject"]["status"] == "succeeded"
    assert rows["deploy"]["status"] == "skipped", rows["deploy"]
    with eng.begin() as c:
        assert _run_status(c, run["id"]) == "completed"


def test_a_skill_with_the_flag_off_keeps_todays_behaviour(eng, project, monkeypatch):
    """What every skill authored before the flag was given by the schema backfill."""
    _wire(monkeypatch, EXITED)
    with eng.begin() as c:
        step_key = _authored(c, project, f"gwlint_{uuid.uuid4().hex[:6]}", fail_on_exit=False)
        run = _start(c, project, _publish(c, project, step_key)["id"])
        runner.tick(c, Ctx(eng), project_id=project)
        rows = _rows(c, run["id"])
    assert rows["validate"]["status"] == "succeeded", rows["validate"]
    assert rows["validate"]["output"]["exit_code"] == 1, "the code is still on the output"


def test_a_zero_exit_passes_with_the_flag_on(eng, project, monkeypatch):
    _wire(monkeypatch, {"status": "succeeded",
                        "result": {**EXITED["result"], "exit_code": 0, "log_tail": "ok"}})
    with eng.begin() as c:
        step_key = _authored(c, project, f"gwlint_{uuid.uuid4().hex[:6]}", fail_on_exit=True)
        run = _start(c, project, _publish(c, project, step_key)["id"])
        runner.tick(c, Ctx(eng), project_id=project)
        runner.tick(c, Ctx(eng), project_id=project)
        rows = _rows(c, run["id"])
    assert rows["validate"]["status"] == "succeeded"
    assert rows["deploy"]["status"] == "succeeded"
    assert rows["reject"]["status"] == "skipped"


def test_the_log_tail_is_redacted_before_it_is_kept(eng, project, monkeypatch):
    from boss_engine.services import run_secret_redaction

    secret = f"sk-live-{uuid.uuid4().hex}"
    run_secret_redaction.register(secret, run_id=f"other-run-{uuid.uuid4().hex[:6]}")
    leaked = {"status": "succeeded",
              "result": {**EXITED["result"], "log_tail": f"auth failed for token {secret}"}}
    _wire(monkeypatch, leaked)
    with eng.begin() as c:
        step_key = _authored(c, project, f"gwlint_{uuid.uuid4().hex[:6]}", fail_on_exit=True)
        run = _start(c, project, _publish(c, project, step_key)["id"])
        runner.tick(c, Ctx(eng), project_id=project)
        validate = _rows(c, run["id"])["validate"]
    assert validate["status"] == "failed"
    assert secret not in validate["last_error"] and "[SECRET]" in validate["last_error"]
    assert secret not in validate["output"]["log_tail"]


def test_a_built_in_skill_that_reports_an_exit_code_is_judged_as_before(eng, project):
    """`fail_on_exit` is an authored skill's setting. A built-in that happens to return an
    `exit_code` (toolchain commands do) is not failed by it."""
    with eng.begin() as c:
        assert custom_steps.exit_failure(c, project, "toolchain.command", "Run",
                                         {"exit_code": 2, "log_tail": "x"}) is None
        assert custom_steps.exit_failure(c, project, "gwlint", "Run", {"exit_code": 0}) is None
        assert custom_steps.exit_failure(c, project, "gwlint", "Run", {"exit_code": True}) is None
        assert custom_steps.exit_failure(c, project, "gwlint", "Run", "not a dict") is None


# ── the error path reads what the failed step said (AMEX-G10, BOSSAPP-1168) ────────────────

def _publish_report_on_error(c, project_id, step_key):
    name = f"wf_{uuid.uuid4().hex[:6]}"
    definition = workflows.create_definition(
        c, project_id=project_id, workflow_key=name, name=name, description=None,
        actor="test", namespace="product")
    return workflows.publish_version(
        c, project_id=project_id, definition_id=definition["id"], actor="test",
        spec={"entry_step": "validate", "steps": [
            {"id": "validate", "kind": "capability", "next": ["deploy"], "on_error": "report",
             "max_attempts": 1, "approval": {"mode": "auto"},
             "binding": {"custom_step": step_key, "inputs": {"file": "tenants/lumen-pay.yaml"}}},
            {"id": "deploy", "kind": "capability", "approval": {"mode": "auto"},
             "binding": {"capability": "cost_check", "inputs": {}}},
            # THE ERROR STEP BINDS THE FAILED STEP'S OUTPUT, as any step binds a result.
            {"id": "report", "kind": "capability", "approval": {"mode": "auto"},
             "next": ["fixable"],
             "binding": {"capability": "smoke_test", "inputs": {
                 "url": {"from_step": "validate", "field": "log_tail"}}}},
            # …and a condition after it judges the failed step's exit code.
            {"id": "fixable", "kind": "condition",
             "expression": {"path": "steps.validate.output.exit_code", "op": "eq", "value": 1},
             "on_true": "fix", "on_false": "human_only"},
            {"id": "fix", "kind": "capability", "approval": {"mode": "auto"},
             "binding": {"capability": "cost_check", "inputs": {}}},
            {"id": "human_only", "kind": "capability", "approval": {"mode": "auto"},
             "binding": {"capability": "cost_check", "inputs": {}}},
        ]})


def test_the_error_path_reads_the_failed_steps_output(eng, project, monkeypatch):
    import boss_engine.services.agent_loop as agent_loop

    monkeypatch.setattr(agent_loop, "_invoke_agent_skill", lambda *a, **k: dict(EXITED))
    probe = RecordingTool("http_probe", {"status": 200, "ms": 1})
    monkeypatch.setattr(runner, "_tool_registry", lambda: RecordingRegistry(
        [probe, RecordingTool("get_costs", {"spend_usd": 0})]))
    with eng.begin() as c:
        step_key = _authored(c, project, f"gwlint_{uuid.uuid4().hex[:6]}", fail_on_exit=True)
        run = _start(c, project, _publish_report_on_error(c, project, step_key)["id"])
        runner.tick(c, Ctx(eng), project_id=project)
        assert _rows(c, run["id"])["validate"]["status"] == "failed"
        # In the run context, marked as a failure — never mistaken for a result.
        seen = runner._run_context(c, run["id"])["steps"]["validate"]
        assert seen["step_status"] == "failed" and seen["exit_code"] == 1
        for _ in range(4):
            runner.tick(c, Ctx(eng), project_id=project)
        rows = _rows(c, run["id"])
    assert rows["report"]["status"] == "succeeded", rows["report"]
    assert probe.calls and "SEC-004" in probe.calls[0]["args"]["url"], \
        "the error step never received the validator's report"
    assert rows["fixable"]["output"]["result"] is True
    assert rows["fix"]["status"] == "succeeded" and rows["human_only"]["status"] == "skipped"
    assert rows["deploy"]["status"] == "skipped"


def test_a_failure_still_being_retried_is_not_an_answer(eng, project, monkeypatch):
    """Only a terminal failure that went on along its error path is read; an attempt that will
    run again is not in the context."""
    _wire(monkeypatch, EXITED)
    with eng.begin() as c:
        step_key = _authored(c, project, f"gwlint_{uuid.uuid4().hex[:6]}", fail_on_exit=True)
        run = _start(c, project, _publish(c, project, step_key)["id"])
        c.execute(text("UPDATE workflow_step_runs SET status='failed', "
                       "output=CAST(:o AS JSONB) WHERE workflow_run_id=:r AND step_id='validate'"),
                  {"o": '{"exit_code": 1}', "r": run["id"]})
        assert "validate" not in runner._run_context(c, run["id"])["steps"]


@pytest.mark.parametrize("fail_on_exit", [True, False])
def test_every_tail_is_redacted_on_failure_and_on_success(eng, project, monkeypatch,
                                                          fail_on_exit):
    """Review of #125: `stderr_tail` (and any `*_tail`) reached the run unscrubbed, and a
    successful step's output was not scrubbed at all — yet the error path and every later step
    read it."""
    from boss_engine.services import run_secret_redaction

    secret = f"sk-live-{uuid.uuid4().hex}"
    run_secret_redaction.register(secret, run_id=f"other-run-{uuid.uuid4().hex[:6]}")
    leaked = {"status": "succeeded",
              "result": {**EXITED["result"], "log_tail": f"token {secret}",
                         "stderr_tail": f"curl: auth {secret}", "trace_tail": secret}}
    _wire(monkeypatch, leaked)
    with eng.begin() as c:
        step_key = _authored(c, project, f"gwlint_{uuid.uuid4().hex[:6]}",
                             fail_on_exit=fail_on_exit)
        run = _start(c, project, _publish(c, project, step_key)["id"])
        runner.tick(c, Ctx(eng), project_id=project)
        validate = _rows(c, run["id"])["validate"]
    assert validate["status"] == ("failed" if fail_on_exit else "succeeded")
    for field in ("log_tail", "stderr_tail", "trace_tail"):
        assert secret not in validate["output"][field], field



def test_a_step_cannot_name_both_a_custom_step_and_a_capability():
    """Review of #125: the runner runs the custom step while a reader of the template believed
    the capability (a forged "config_deploy" staging step)."""
    _, problems = workflows.normalise_workflow_spec({"entry_step": "s", "steps": [
        {"id": "s", "kind": "capability",
         "binding": {"custom_step": "step_relay", "capability": "config_deploy"}}]},
        check_agents=False)
    assert any("both a custom step and a capability" in str(p) for p in problems), problems
