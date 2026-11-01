"""A failed workflow step stops the run, and the operator decides what happens next.

THE INCIDENT (boss-1, 2026-10-02). A delivery workflow's research step failed. The runner
recorded it as SUCCEEDED, with status `failed_continued` — "research failed before it finished;
the panel gets what it had reached by then" — and the workflow walked on: panel, auto-approved
spec, build. All without research. The console showed the research step green.

The product owner's rule, which this file enforces for every kind of step rather than for the
one that happened to be caught: "if a step fails it should stop, that's the expectation; there
should be a button to skip and proceed."

So:

1. A step whose work failed is never `succeeded`, and the steps after it do not start. This is
   asserted for EVERY capability in the catalogue, each through the way its own work can fail —
   and the catalogue is partitioned exhaustively, so a capability added later without a
   failure path here fails `test_every_capability_has_a_failure_path_under_test`.
2. Two ways forward, both the operator's: Retry (`workflows.retry_failed_step`) and Skip and
   proceed (`workflows.skip_failed_step`), which records the reason, the actor and an event, and
   hands downstream steps an explicit "skipped" marker instead of a value.
"""
from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import text

from boss_engine.services import workflow_capability_runner as runner
from boss_engine.services import workflow_outcomes as outcomes
from boss_engine.services import workflows
from boss_engine.services.workflow_capabilities import CAPABILITIES, CAPABILITY_KEYS

# Fixtures and helpers are shared with the runner's own suite, so these tests exercise the same
# real database and the same recording tool registry. Only non-test names are imported, so
# nothing from that module is collected twice.
from .test_workflow_capability_runner import (  # noqa: F401 - fixtures used by name
    Ctx,
    RecordingRegistry,
    RecordingTool,
    _publish,
    _start,
    _steps,
    eng,
    project,
)


# ── The partition: every capability, and how its work fails ─────────────────────────────────

#: Capabilities whose work is started and then WAITED ON. Failure arrives later, as a bad status
#: on the awaited row. Derived from the runner, not written out, so a new await kind is covered
#: the moment it exists.
AWAITED = {key: kind for key, (kind, _fields) in runner._AWAIT_KINDS.items()}

#: Approval gates. Their "failure" is a person saying no; the run must block, and skipping must
#: be refused, because a skip would turn that no into a yes.
GATES = {key for key in CAPABILITY_KEYS if CAPABILITIES[key].is_gate}

#: Capabilities that call one tool and finish in the call. Failure is whatever that tool
#: REALLY returns when the work did not happen (`outcomes.FAILURE_SHAPES`), or the tool raising.
TOOL_BACKED = {
    key for key in CAPABILITY_KEYS
    if CAPABILITIES[key].tool and key not in AWAITED and key not in GATES
}

#: Capabilities the runner performs itself (no tool). Each names the function whose failure is
#: that capability's failure. A new tool-less capability is unclassified until it is added here
#: — which is the point: somebody has to say how it fails before it can ship.
INTERNAL = {
    "report": "_write_the_run_report",
    "document": "_write_the_record",
    "review": "_observed_from_the_build",
    "test": "_observed_from_the_build",
    "monitor": "_observed_from_the_build",
    # REWRITTEN (2026-10-02): notify was listed below as commissioning nothing — a `recorded`
    # no-op that went green having told nobody, at the end of three catalogue templates and as
    # Deprecation / Sunset's whole announcement. The runner now sends it, so a send that cannot
    # happen is a failure that must stop the run like any other.
    "notify": "_notify",
    # The security review's verdict and its report (services/security_review_steps.py).
    "security_rereview": "_security_rereview",
    "security_report": "_security_report",
    # Incident response (services/incident_response.py): an investigation or a report that
    # could not be written is a failed step, never a green one.
    "investigate_incident": "_investigate_incident",
    "incident_report": "_write_the_incident_report",
    # Set up monitoring (services/monitoring_pack.py): no pack under monitoring_gate: block
    # fails the step, so the release cannot ship without one.
    "monitoring_pack": "_set_up_monitoring",
    # Scan for business rules (knowledge/rule_scan.py): a scan that cannot run fails the step.
    "scan_business_rules": "_scan_business_rules",
    # Regulatory trace (knowledge/regulatory_trace.py) and banking_regulatory_compliance's thin
    # role-agent steps (knowledge/banking_regulatory_pipeline.py): work the runner performs
    # itself, reading the graph and writing an `inferred` proposal. A proposal that could not be
    # produced is a failed step, never a green one.
    "trace_regulation": "_trace_regulation",
    "clause_intake": "_clause_intake",
    "compliance_assessment": "_compliance_assessment",
    "compliance_orchestration": "_compliance_orchestration",
    "lane_confirmation": "_lane_confirmation",
    "remediation_package": "_remediation_package",
    "compliance_closure": "_compliance_closure",
    "aml_network_mapping": "_aml_network_mapping",
    "extract_rulebook_rules": "_extract_rulebook_rules",
    "aml_analysis": "_aml_analysis",
    "sar_narrative": "_sar_narrative",
    # the DQ RCA and remediation pipeline (knowledge/dq_incident_pipeline.py)
    **{key: f"_{key}" for key in (
        "dq_monitoring", "dq_context_enrichment", "dq_historical_pattern", "dq_rca",
        "dq_orchestration", "dq_lane", "dq_rule_drafting", "dq_remediation_package",
        "dq_remediation_fix", "dq_incident_closure", "dq_escalation", "dq_eagle_intake")},
}

#: Tool-less capabilities that commission NOTHING. They are honest no-ops that say so
#: (`status: recorded`), so there is no work that could fail and be mistaken for success.
COMMISSIONS_NOTHING = {"video_edit", "social_publish"}


def test_every_capability_has_a_failure_path_under_test():
    """The catalogue is partitioned exactly — no capability unclassified, none in two classes.

    If this fails, a capability was added (or changed shape) without anyone deciding how its
    failure stops a run. Add it to the right class above; the parametrised tests below then
    prove it."""
    classes = [set(AWAITED), GATES, TOOL_BACKED, set(INTERNAL), COMMISSIONS_NOTHING]
    union = set().union(*classes)
    assert union == set(CAPABILITY_KEYS), (
        f"unclassified: {sorted(set(CAPABILITY_KEYS) - union)}; "
        f"unknown: {sorted(union - set(CAPABILITY_KEYS))}")
    assert sum(len(c) for c in classes) == len(CAPABILITY_KEYS), "a capability is in two classes"


def test_every_capability_has_an_outcome_verdict():
    """Every capability in the catalogue says, in `workflow_outcomes.VERDICTS`, how to tell
    whether its work happened. There is no default: a smoke test of an app that was down went
    green because a generic check did not understand `http_probe`'s `{"status": 503}`."""
    assert set(outcomes.VERDICTS) == set(CAPABILITY_KEYS), (
        f"no verdict: {sorted(set(CAPABILITY_KEYS) - set(outcomes.VERDICTS))}; "
        f"verdict for no capability: {sorted(set(outcomes.VERDICTS) - set(CAPABILITY_KEYS))}")


def test_every_async_capability_is_awaited():
    """`is_async` means the runner parks the step and waits. A capability declared async with
    nothing to wait on goes green on "accepted" — deploy on `planned`, retry_build on `retried`
    — and its later failure never reaches the step."""
    unawaited = sorted(k for k, c in CAPABILITIES.items()
                       if c.is_async and k not in runner._AWAIT_KINDS)
    assert not unawaited, f"async with no await spec: {unawaited}"
    for key, (kind, _fields) in runner._AWAIT_KINDS.items():
        assert kind in runner._AWAIT_TABLES, f"{key} waits on {kind!r}, which has no table"
        assert outcomes.VERDICTS[key] is outcomes.awaited_work, key


def test_every_tool_backed_capability_has_its_real_failure_shapes_recorded():
    """A capability that calls a tool is driven below with what that tool really returns on
    failure. `cost_check` is the one whose tool only fails by raising, and says so."""
    for key in sorted(TOOL_BACKED):
        assert key in outcomes.FAILURE_SHAPES, f"{key}: how does {CAPABILITIES[key].tool} fail?"


def test_no_awaited_failure_is_turned_into_a_success():
    """The specific shape of the incident: a branch in `resume_awaited` that met a bad status
    and force-COMPLETED the step anyway. There must be exactly one completion, and it must be
    guarded by the success states."""
    import inspect

    body = inspect.getsource(runner.resume_awaited)
    assert "failed_continued" not in body.replace("`failed_continued`", "")
    assert body.count("force_complete_waiting_step(") == 1
    completion = body.index("force_complete_waiting_step(")
    assert "if status in ok_states:" in body[:completion]


# ── Helpers ──────────────────────────────────────────────────────────────────────────────────

def _required(key: str) -> dict:
    return {name: "x" for name in CAPABILITIES[key].required_inputs}


def _two_step(c, project_id: str, key: str, inputs: dict | None = None):
    """[the step under test] → notify. `notify` commissions nothing, so if it so much as
    becomes ready, the run advanced past a failure."""
    _, version = _publish(c, project_id, f"stop_{key}_{uuid.uuid4().hex[:6]}", [
        {"id": "s", "kind": "capability",
         "binding": {"capability": key, "inputs": inputs if inputs is not None else _required(key)},
         "next": ["after"]},
        {"id": "after", "kind": "capability",
         "binding": {"capability": "notify", "inputs": {"message": "went on"}}, "next": []},
    ], "s")
    return _start(c, project_id, version["id"], {})


class _PendingCi:
    """A GitHub whose checks on the pull request's head have not finished."""

    repository = "acme/app"

    def pull_request(self, number):
        return {"head": {"sha": "a" * 40}}

    def check_runs(self, sha):
        return [{"name": "test", "status": "in_progress", "conclusion": None}]

    def statuses(self, sha):
        return []

    def has_workflows(self, sha):
        return True


#: A parent table whose status column is constrained to words other than "active".
_PARENT_STATUS = {"incidents": "open"}


def _insert_awaited(c, project_id: str, table: str, row_id: str, status: str) -> None:
    """Land a row in any awaited table in a terminal state, filling whatever the schema insists
    on generically — so this keeps working for every await table, including ones added later."""
    columns = c.execute(
        text("SELECT column_name, data_type FROM information_schema.columns "
             "WHERE table_name = :t AND table_schema = current_schema() "
             "AND is_nullable = 'NO' AND column_default IS NULL"),
        {"t": table},
    ).mappings().all()
    values: dict = {"id": row_id, "status": status}
    # A REQUIRED REFERENCE GETS A REAL PARENT ROW. A deployment run must name a deployment
    # target that exists; "x" breaks the foreign key. Parents are made the same generic way.
    parents = dict(c.execute(
        text("SELECT kcu.column_name, ccu.table_name FROM information_schema.table_constraints tc "
             "JOIN information_schema.key_column_usage kcu ON kcu.constraint_name = "
             "tc.constraint_name AND kcu.table_schema = tc.table_schema "
             "JOIN information_schema.constraint_column_usage ccu ON ccu.constraint_name = "
             "tc.constraint_name AND ccu.table_schema = tc.table_schema "
             "WHERE tc.constraint_type = 'FOREIGN KEY' AND tc.table_name = :t "
             "AND tc.table_schema = current_schema()"), {"t": table}).all())
    for col in columns:
        name, kind = col["column_name"], col["data_type"]
        if name in values:
            continue
        if name == "project_id":
            values[name] = project_id
        elif name in parents:
            parent_id = f"{name}_{uuid.uuid4().hex[:8]}"
            _insert_awaited(c, project_id, parents[name], parent_id,
                            _PARENT_STATUS.get(parents[name], "active"))
            values[name] = parent_id
        elif kind in ("jsonb", "json"):
            values[name] = "{}"
        elif kind in ("integer", "bigint", "smallint", "numeric", "double precision", "real"):
            values[name] = 0
        elif kind == "boolean":
            values[name] = False
        elif "timestamp" in kind:
            values[name] = "2026-10-02T00:00:00Z"
        else:
            values[name] = "x"
    has_project = c.execute(
        text("SELECT 1 FROM information_schema.columns WHERE table_name = :t "
             "AND table_schema = current_schema() AND column_name = 'project_id'"),
        {"t": table},
    ).first()
    if has_project:
        values["project_id"] = project_id
    names = ", ".join(values)
    params = ", ".join(f":{n}" for n in values)
    c.execute(text(f"INSERT INTO {table} ({names}) VALUES ({params}) "  # noqa: S608 - test
                   f"ON CONFLICT (id) DO UPDATE SET status = EXCLUDED.status"), values)


def _tick_through_retries(eng, c, project_id: str, run_id: str, times: int = 6) -> None:
    """Tick enough times for a retryable failure to use up its attempts.

    A retry is scheduled at the wall clock, but every tick here runs inside ONE transaction,
    whose `now()` stands still — so without bringing `run_after` back, the retry would never
    become claimable and the test would stop at "ready" for the wrong reason."""
    for _ in range(times):
        c.execute(text("UPDATE workflow_step_runs SET run_after = now() - interval '1 second' "
                       "WHERE workflow_run_id = :r AND status = 'ready'"), {"r": run_id})
        # A tier-2 step (deploy, redeploy) asks a person before it runs. Approve it, so the
        # thing under test — the work failing — is actually reached.
        c.execute(text("UPDATE decisions SET status = 'approved', approved_by = 'ceo' "
                       "WHERE status NOT IN ('approved', 'rejected') AND id IN ("
                       "  SELECT decision_id FROM workflow_step_runs "
                       "  WHERE workflow_run_id = :r AND status = 'waiting')"), {"r": run_id})
        runner.tick(c, Ctx(eng), project_id=project_id)


def _run_status(c, run_id: str) -> str:
    return c.execute(text("SELECT status FROM workflow_runs WHERE id=:r"), {"r": run_id}).scalar()


def _assert_stopped(c, run_id: str, steps: dict) -> None:
    assert steps["s"]["status"] != "succeeded", steps["s"]
    assert steps["s"]["status"] in {"failed", "deadlettered"}, steps["s"]["status"]
    assert "after" not in steps or steps["after"]["status"] == "pending", (
        "the run advanced past a failed step: " + str(steps.get("after")))
    assert _run_status(c, run_id) in {"failed", "deadlettered"}


def _security_tool_args(monkeypatch) -> None:
    """The security review's panel and fix build their tool's arguments from the run's earlier
    steps (a scan's findings, a person's choice); a two-step run has none, so here they are
    given, and the TOOL's work — the thing under test — is reached."""
    from boss_engine.services import security_review_steps

    monkeypatch.setattr(security_review_steps, "prepare",
                        lambda _c, _p, key, _ctx, inputs: {"args": {**inputs, "title": key,
                                                                    "instructions": key,
                                                                    "directive": key}})


def _awaited_cases():
    for key, kind in sorted(AWAITED.items()):
        if key in runner._ADVISORY_CAPABILITIES:
            continue  # pass 2 never stops the run: test_a_more_tests_pass_never_blocks_the_build
        _table, _ok, bad = runner._AWAIT_TABLES[kind]
        for status in sorted(bad):
            yield pytest.param(key, kind, status, id=f"{key}-{status}")


# ── 1. Every capability: failed work stops the run ───────────────────────────────────────────

@pytest.mark.parametrize("key,kind,status", list(_awaited_cases()))
def test_awaited_work_that_fails_stops_the_run_at_that_step(eng, project, monkeypatch,
                                                            key, kind, status):
    """Research, panel, build, preview, PLRR — and every bad status each can end in. The
    incident was research `cancelled` recorded as succeeded; no pair here may do that."""
    awaited_id = f"aw_{key}_{uuid.uuid4().hex[:8]}"
    field = runner._AWAIT_KINDS[key][1][0]
    tool_name = CAPABILITIES[key].tool
    tools = [RecordingTool(tool_name, {field: awaited_id})] if tool_name else []
    monkeypatch.setattr(runner, "_tool_registry", lambda: RecordingRegistry(tools))
    if key == "plrr":
        monkeypatch.setattr(runner, "_plrr_step", lambda *a, **k: {
            "capability": "plrr", "tool": "extend_code_task", "status": "extending",
            "run_id": awaited_id})
    if key == "preview":
        monkeypatch.setattr(runner, "_running_preview", lambda *a, **k: None)
    if key == "verify_recovery":
        # The check it parks on is under test, not how it is started from an incident.
        monkeypatch.setattr(runner, "_start_recovery_check", lambda *a, **k: {
            "capability": "verify_recovery", "tool": None, "status": "awaiting",
            "await": {"kind": "recovery_check", "id": awaited_id},
            "recovery_check_id": awaited_id})
    _security_tool_args(monkeypatch)
    if key == "architecture":
        # The architect's document needs an approved PRD; the review it waits on is under test.
        monkeypatch.setattr(runner, "_architecture_step", lambda *a, **k: {
            "capability": "architecture", "tool": None, "status": "awaiting",
            "architecture_review_id": awaited_id})
    # Deploy's "no preview, no deploy" guard is its own test; here the deployment is under test.
    monkeypatch.setattr(runner, "_preview_missing_for_run", lambda *a, **k: None)
    table = runner._AWAIT_TABLES[kind][0]
    inputs = None
    if key == "wait_for_ci":
        # wait_for_ci has no tool: it reads the project's GitHub (ci_gate.reader_for_project,
        # which "tests replace") and records its OWN wait. Give it a pull request and a reader
        # whose checks have not finished, so it parks; the wait it made is then failed below.
        from boss_engine.services import ci_gate

        monkeypatch.setattr(ci_gate, "reader_for_project", lambda *a, **k: _PendingCi())
        inputs = {"pull_request_url": "https://github.com/acme/app/pull/7"}

    with eng.begin() as c:
        run = _two_step(c, project, key, inputs)
        # Two ticks with approval between: a tier-2 step (deploy, redeploy) asks first.
        _tick_through_retries(eng, c, project, run["id"], times=2)
        parked = _steps(c, run["id"])["s"]
        assert parked["status"] == "waiting", parked
        if key == "wait_for_ci":
            awaited_id = parked["output"]["await"]["id"]
        assert parked["output"]["await"] == {"kind": kind, "id": awaited_id}

        if key == "wait_for_ci":
            c.execute(text(f"UPDATE {table} SET status=:s WHERE id=:i"),  # noqa: S608
                      {"s": status, "i": awaited_id})
        else:
            _insert_awaited(c, project, table, awaited_id, status)
        runner.tick(c, Ctx(eng), project_id=project)
        runner.tick(c, Ctx(eng), project_id=project)
        steps = _steps(c, run["id"])
        _assert_stopped(c, run["id"], steps)
        assert status in (steps["s"]["last_error"] or ""), steps["s"]["last_error"]
        c.execute(text(f"DELETE FROM {table} WHERE id=:i"), {"i": awaited_id})  # noqa: S608


@pytest.mark.parametrize("phase,reason", [("awaiting_plan_approval", None),
                                          ("awaiting_gate_override", None),
                                          ("coding", "budget_denied")])
def test_a_build_waiting_on_the_operator_has_not_failed_its_step(eng, project, monkeypatch,
                                                                 phase, reason):
    """Factory walk: a build blocked for a plan approval, a waiver or a budget is waiting on a
    person — its own card says so. Failing the step above it added a second card for the same
    wait ("Build, review and test failed on Payments plan")."""
    key = next(k for k, kind in sorted(AWAITED.items()) if kind == "code_task")
    awaited_id = f"aw_{key}_{uuid.uuid4().hex[:8]}"
    field = runner._AWAIT_KINDS[key][1][0]
    tool_name = CAPABILITIES[key].tool
    tools = [RecordingTool(tool_name, {field: awaited_id})] if tool_name else []
    monkeypatch.setattr(runner, "_tool_registry", lambda: RecordingRegistry(tools))
    monkeypatch.setattr(runner, "_preview_missing_for_run", lambda *a, **k: None)
    with eng.begin() as c:
        run = _two_step(c, project, key)
        _tick_through_retries(eng, c, project, run["id"], times=2)
        assert _steps(c, run["id"])["s"]["status"] == "waiting"
        _insert_awaited(c, project, "code_task_runs", awaited_id, "blocked")
        c.execute(text("UPDATE code_task_runs SET phase = :ph, finalize_ctx = "
                       "COALESCE(finalize_ctx, '{}'::jsonb) || CAST(:fc AS JSONB) WHERE id = :i"),
                  {"ph": phase, "fc": json.dumps({"reason_code": reason} if reason else {}),
                   "i": awaited_id})
        runner.tick(c, Ctx(eng), project_id=project)
        runner.tick(c, Ctx(eng), project_id=project)
        assert _steps(c, run["id"])["s"]["status"] == "waiting"
        assert _run_status(c, run["id"]) not in {"failed", "deadlettered"}
        c.execute(text("DELETE FROM code_task_runs WHERE id=:i"), {"i": awaited_id})


@pytest.mark.parametrize("phase", ["awaiting_gate_override", "awaiting_plan_approval"])
def test_a_step_follows_the_retry_a_person_answered_its_wait_with(eng, project, monkeypatch,
                                                                    phase):
    """HelpDesk AI wfr_f383649a8b024d97a80c: the build parked for a waiver (its reviewers left
    findings open), the person pressed Retry in the Factory, attempt 2 fixed them and opened
    PR #229 — and the workflow step went on waiting on attempt 1, which stays parked forever.
    A person's retry IS the answer to the wait: the step follows the newer attempt of the same
    lineage, then finishes the ordinary way."""
    key = next(k for k, kind in sorted(AWAITED.items()) if kind == "code_task")
    awaited_id = f"aw_{key}_{uuid.uuid4().hex[:8]}"
    retry_id = f"aw_{key}_{uuid.uuid4().hex[:8]}"
    field = runner._AWAIT_KINDS[key][1][0]
    tool_name = CAPABILITIES[key].tool
    tools = [RecordingTool(tool_name, {field: awaited_id})] if tool_name else []
    monkeypatch.setattr(runner, "_tool_registry", lambda: RecordingRegistry(tools))
    monkeypatch.setattr(runner, "_preview_missing_for_run", lambda *a, **k: None)
    with eng.begin() as c:
        run = _two_step(c, project, key)
        _tick_through_retries(eng, c, project, run["id"], times=2)
        assert _steps(c, run["id"])["s"]["status"] == "waiting"
        _insert_awaited(c, project, "code_task_runs", awaited_id, "blocked")
        c.execute(text("UPDATE code_task_runs SET phase = :ph, created_at = now() - "
                       "interval '1 hour' WHERE id = :i"), {"ph": phase, "i": awaited_id})
        runner.tick(c, Ctx(eng), project_id=project)
        step = _steps(c, run["id"])["s"]
        assert step["status"] == "waiting"
        assert (step["output"].get("await") or {}).get("id") == awaited_id, \
            "with no retry, the step keeps waiting on the person's card"
        # The person's Retry: a later attempt of the same lineage.
        _insert_awaited(c, project, "code_task_runs", retry_id, "running")
        c.execute(text("UPDATE code_task_runs SET phase = 'implement', finalize_ctx = "
                       "COALESCE(finalize_ctx, '{}'::jsonb) || CAST(:fc AS JSONB) WHERE id = :i"),
                  {"fc": json.dumps({"root_run_id": awaited_id, "attempt": 2}), "i": retry_id})
        runner.tick(c, Ctx(eng), project_id=project)
        step = _steps(c, run["id"])["s"]
        assert step["status"] == "waiting"
        assert (step["output"].get("await") or {}).get("id") == retry_id, \
            "the step follows the attempt the person started"
        assert _run_status(c, run["id"]) not in {"failed", "deadlettered"}
        c.execute(text("DELETE FROM code_task_runs WHERE id = ANY(:i)"),
                  {"i": [awaited_id, retry_id]})


@pytest.mark.parametrize("key", ["deploy", "redeploy"])
def test_a_deploy_is_done_when_its_deployment_is_healthy_not_when_it_is_planned(
        eng, project, monkeypatch, key):
    """`deploy_app` answers `planned` and the step went green on it — before anything shipped,
    and whatever happened to the deployment afterwards never reached the step. It now waits on
    the deployment run, and completes with where the app went once that run succeeds."""
    deployment_id = f"deprun_{uuid.uuid4().hex[:8]}"
    tool = RecordingTool(CAPABILITIES[key].tool,
                         {"status": "planned", "deployment_run_id": deployment_id,
                          "next_action": "approve_and_apply_changeset"})
    monkeypatch.setattr(runner, "_tool_registry", lambda: RecordingRegistry([tool]))
    monkeypatch.setattr(runner, "_preview_missing_for_run", lambda *a, **k: None)
    with eng.begin() as c:
        run = _two_step(c, project, key)
        _tick_through_retries(eng, c, project, run["id"], times=2)
        parked = _steps(c, run["id"])
        assert parked["s"]["status"] == "waiting", "planned is not shipped"
        assert "after" not in parked or parked["after"]["status"] == "pending"
        assert parked["s"]["output"]["await"] == {"kind": "deployment", "id": deployment_id}

        _insert_awaited(c, project, "deployment_runs", deployment_id, "succeeded")
        c.execute(text("UPDATE deployment_targets SET domains='[\"192.0.2.7:3014\"]' "
                       "WHERE id = (SELECT deployment_target_id FROM deployment_runs "
                       "WHERE id=:d)"), {"d": deployment_id})
        runner.tick(c, Ctx(eng), project_id=project)
        steps = _steps(c, run["id"])
        c.execute(text("DELETE FROM deployment_runs WHERE id=:d"), {"d": deployment_id})

    assert steps["s"]["status"] == "succeeded"
    assert steps["s"]["output"]["deployment_id"] == deployment_id
    assert steps["s"]["output"]["deployed_url"] == "http://192.0.2.7:3014"
    assert steps["after"]["status"] in {"ready", "succeeded"}


def test_a_retried_build_waits_on_the_attempt_it_started():
    """`retry_code_task` nests the new attempt under `result` and says `retried` at the top —
    which went green the moment the attempt was queued. The step parks on the new attempt."""
    settled = runner._settle_started_work(
        CAPABILITIES["retry_build"], "retry_build",
        {"capability": "retry_build", "tool": "retry_code_task", "status": "retried",
         "result": {"status": "queued", "run_id": "crun_new", "attempt": 2}})
    assert settled["status"] == "awaiting"
    assert settled["await"] == {"kind": "code_task", "id": "crun_new"}
    assert settled["code_task_run_id"] == "crun_new"


def _real_failure_cases():
    for key, shapes in sorted(outcomes.FAILURE_SHAPES.items()):
        for index, shape in enumerate(shapes):
            yield pytest.param(key, shape, id=f"{key}-{index}")


class _RaisingTool(RecordingTool):
    def handler(self, ctx, *, project_id, actor, args):
        raise RuntimeError(f"{self.name} fell over")


def _drive_tool(eng, project, monkeypatch, key, tool, inputs=None):
    monkeypatch.setattr(runner, "_tool_registry", lambda: RecordingRegistry([tool]))
    monkeypatch.setattr(runner, "_preview_missing_for_run", lambda *a, **k: None)
    monkeypatch.setattr(runner, "_running_preview", lambda *a, **k: None)
    _security_tool_args(monkeypatch)
    with eng.begin() as c:
        run = _two_step(c, project, key, inputs)
        _tick_through_retries(eng, c, project, run["id"])
        steps = _steps(c, run["id"])
        _assert_stopped(c, run["id"], steps)
    return steps


@pytest.mark.parametrize("key,returned", list(_real_failure_cases()))
def test_a_tool_that_really_failed_stops_the_run(eng, project, monkeypatch, key, returned):
    """Each tool-backed capability, driven with what ITS tool returns when the work did not
    happen — `http_probe`'s 503 and its `status: None` for a refused connection included, which
    a generic `{"status": "failed"}` check passed as success. Never green; the run stops."""
    steps = _drive_tool(eng, project, monkeypatch, key,
                        RecordingTool(CAPABILITIES[key].tool, returned))
    assert steps["s"]["last_error"], "a failure with no reason"


@pytest.mark.parametrize("key", sorted(k for k in CAPABILITY_KEYS if CAPABILITIES[k].tool))
def test_a_tool_that_raises_stops_the_run(eng, project, monkeypatch, key):
    """The other way any tool fails. `cost_check`'s only way."""
    steps = _drive_tool(eng, project, monkeypatch, key,
                        _RaisingTool(CAPABILITIES[key].tool, {}))
    assert "fell over" in (steps["s"]["last_error"] or "")


@pytest.mark.parametrize("expect,returned,passes", [
    (None, {"url": "u", "status": 200, "ms": 5}, True),
    (None, {"url": "u", "status": 204, "ms": 5}, True),
    (None, {"url": "u", "status": 301, "ms": 5}, False),
    (None, {"url": "u", "status": 503, "ms": 5}, False),
    (None, {"url": "u", "status": None, "ms": 5, "error": "refused"}, False),
    (201, {"url": "u", "status": 201, "ms": 5}, True),
    (201, {"url": "u", "status": 200, "ms": 5}, False),
    ("302", {"url": "u", "status": 302, "ms": 5}, True),
])
def test_a_smoke_test_passes_only_on_the_status_it_expects(expect, returned, passes):
    """`expect_status` was declared and read by nothing. Without it any 2xx passes."""
    verdict = outcomes.judge("smoke_test", returned,
                             {"url": "u", **({"expect_status": expect} if expect else {})})
    assert (verdict.outcome == outcomes.SUCCEEDED) is passes, verdict
    assert verdict.produced["reachable"] is isinstance(returned["status"], int)


def test_a_recorded_failure_shape_is_what_the_tool_returns(eng, project):
    """`FAILURE_SHAPES` is evidence only if the tools really answer that way. Each recorded
    shape is matched — same keys, same kind of status — against the real tool, called for
    real: a server that answers 503, a port nothing listens on, Community's refusal, and an
    unknown preview, build and deployment in this project."""
    import http.server
    import socket
    import threading

    from boss_engine.config import get_settings
    from boss_engine.tools.builtin import build_default_registry

    class Down(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - the stdlib's name
            self.send_response(503)
            self.end_headers()
            self.wfile.write(b"down")

        def log_message(self, *_a):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Down)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    closed = socket.socket()
    closed.bind(("127.0.0.1", 0))
    dead_port = closed.getsockname()[1]
    closed.close()

    tools = {spec.name: spec for spec in build_default_registry().specs()}

    class RealCtx:
        def __init__(self, edition):
            self.engine = eng
            self.settings = get_settings().model_copy(update={"boss_edition": edition})

        def begin(self):
            return eng.begin()

    def call(tool, args, edition="enterprise"):
        return tools[tool].handler(RealCtx(edition), project_id=project, actor="test", args=args)

    try:
        real = {
            "smoke_test": [
                call("http_probe", {"url": f"http://127.0.0.1:{server.server_port}/"}),
                call("http_probe", {"url": f"http://127.0.0.1:{dead_port}/", "timeout_s": 2}),
                call("http_probe", {"url": "http://127.0.0.1:9/"}, edition="community"),
            ],
            "teardown_preview": [call("stop_preview", {"preview_id": "prv_missing"})],
            "deploy": [call("deploy_app", {"run_id": "crun_missing", "environment": "staging"})],
            "redeploy": [call("redeploy", {"run_id": "crun_missing"})],
            "retry_build": [call("retry_code_task", {"run_id": "crun_missing"})],
            "cost_check": [],
        }
    finally:
        server.shutdown()

    def signature(shape):
        status = shape.get("status")
        return (tuple(sorted(shape)), type(status).__name__, shape.get("ok"))

    assert set(real) == set(outcomes.FAILURE_SHAPES)
    for key, shapes in outcomes.FAILURE_SHAPES.items():
        produced = {signature(r) for r in real[key]}
        for shape in shapes:
            assert signature(shape) in produced, (key, shape, real[key])
        # And the verdict agrees with itself on the real thing, not only on the recording.
        for result in real[key]:
            if result.get("ok") is not False:
                assert outcomes.judge(key, result, {}).outcome == outcomes.FAILED, (key, result)


@pytest.mark.parametrize("key", sorted(INTERNAL))
def test_work_the_runner_does_itself_stops_the_run_when_it_fails(eng, project, monkeypatch, key):
    """`report` used to catch every exception and return `status: recorded` — green, with no
    report behind it. The same lie as the incident, one capability over."""
    def explode(*_a, **_k):
        raise RuntimeError(f"{key} could not be produced")

    monkeypatch.setattr(runner, INTERNAL[key], explode)
    monkeypatch.setattr(runner, "_tool_registry", lambda: RecordingRegistry([]))
    # The scan's start check (workflows.START_INPUT_CHECKS) belongs to the scan this test
    # replaces; with no code graph here it would refuse the run before the step it is about.
    from boss_engine.services import workflows as _workflows

    monkeypatch.setattr(_workflows, "START_INPUT_CHECKS", {})

    with eng.begin() as c:
        run = _two_step(c, project, key)
        _tick_through_retries(eng, c, project, run["id"])
        steps = _steps(c, run["id"])
        _assert_stopped(c, run["id"], steps)
        assert "could not be produced" in (steps["s"]["last_error"] or "")


@pytest.mark.parametrize("key", sorted(COMMISSIONS_NOTHING))
def test_a_step_that_commissions_nothing_says_so_instead_of_claiming_work(eng, project,
                                                                          monkeypatch, key):
    """Nothing is started, so nothing can fail — but the step must not read as work done."""
    monkeypatch.setattr(runner, "_tool_registry", lambda: RecordingRegistry([]))
    with eng.begin() as c:
        run = _two_step(c, project, key)
        _tick_through_retries(eng, c, project, run["id"], times=2)
        steps = _steps(c, run["id"])
    assert steps["s"]["output"]["status"] == "recorded"


@pytest.mark.parametrize("key", sorted(GATES))
def test_a_refused_gate_blocks_the_run_and_cannot_be_skipped(eng, project, monkeypatch, key):
    """A gate's "failure" is a person saying no. The run STOPS there at a failed step
    (workflows.reject_gate: it used to sit `blocked`, "waiting on a person" who had already
    answered) — and Skip is refused, because skipping would turn that no into a yes."""
    monkeypatch.setattr(runner, "_tool_registry", lambda: RecordingRegistry([]))
    if key == "security_decide":
        # The security choice is asked only when the scan found something to fix (with nothing,
        # it steps aside as not applicable, by design). Give it findings, so a person says no.
        from boss_engine.services import security_review_steps

        monkeypatch.setattr(security_review_steps, "has_choices", lambda *a, **k: True)
    if key == "rules_review":
        # Likewise the rules review is asked only when the scan left rules waiting.
        from boss_engine.services.knowledge import rule_scan

        monkeypatch.setattr(rule_scan, "has_open_proposals", lambda *a, **k: True)
    if key == "regulatory_trace_review":
        # And the regulatory trace review is asked only when the trace left proposals open.
        from boss_engine.services.knowledge import regulatory_trace

        monkeypatch.setattr(regulatory_trace, "has_open_proposals", lambda *a, **k: True)
    with eng.begin() as c:
        run = _two_step(c, project, key)
        runner.tick(c, Ctx(eng), project_id=project)
        gate = c.execute(
            text("SELECT decision_id FROM workflow_step_runs "
                 "WHERE workflow_run_id=:r AND step_id='s'"), {"r": run["id"]}).scalar()
        assert gate, "the gate never asked anybody"
        c.execute(text("UPDATE decisions SET status='rejected' WHERE id=:d"), {"d": gate})
        runner.tick(c, Ctx(eng), project_id=project)
        steps = _steps(c, run["id"])
        assert steps["s"]["status"] == "failed"
        assert "after" not in steps or steps["after"]["status"] == "pending"
        assert _run_status(c, run["id"]) not in ("running", "completed", "succeeded")
        # Whatever state the refusal leaves the step in, Skip may not pass it.
        c.execute(text("UPDATE workflow_step_runs SET status='failed' "
                       "WHERE workflow_run_id=:r AND step_id='s'"), {"r": run["id"]})
        with pytest.raises(workflows.WorkflowConflictError, match="approval"):
            workflows.skip_failed_step(c, project_id=project, run_id=run["id"], step_id="s",
                                       actor="ceo", reason="ship it anyway")


def test_an_agent_step_whose_approved_run_fails_stops_the_run(eng, project, monkeypatch):
    """`agent_run` is the one await kind that no catalogue capability uses — an agent step that
    paused for approval waits on it. Its failure stops the run like every other."""
    from boss_engine.services import agent_steps

    awaited_id = f"arun_{uuid.uuid4().hex[:8]}"
    monkeypatch.setattr(agent_steps, "run", lambda *a, **k: {
        "status": "suspended", "agent_run_id": awaited_id})
    monkeypatch.setattr(runner, "_tool_registry", lambda: RecordingRegistry([]))
    with eng.begin() as c:
        # AN AGENT TO GIVE THE STEP TO: an agent step that names nobody goes to an agent holding
        # a usable skill, and a project with none is refused at publish (step_agents).
        role = f"helper_{uuid.uuid4().hex[:6]}"
        c.execute(text("INSERT INTO roles (id, project_id, role_key, display_name) "
                       "VALUES (:i, :p, :k, 'Helper')"),
                  {"i": f"role_{uuid.uuid4().hex[:12]}", "p": project, "k": role})
        c.execute(text("INSERT INTO role_skills (id, project_id, role_key, skill_id) "
                       "VALUES (:i, :p, :k, 'repo.read')"),
                  {"i": f"rs_{uuid.uuid4().hex[:12]}", "p": project, "k": role})
        _, version = _publish(c, project, f"agent_{uuid.uuid4().hex[:6]}", [
            {"id": "s", "kind": "capability",
             "binding": {"agent_work": True, "inputs": {"instructions": "do the thing"}},
             "next": ["after"]},
            {"id": "after", "kind": "capability",
             "binding": {"capability": "notify", "inputs": {"message": "went on"}}, "next": []},
        ], "s")
        run = _start(c, project, version["id"], {})
        runner.tick(c, Ctx(eng), project_id=project)
        assert _steps(c, run["id"])["s"]["status"] == "waiting"
        _insert_awaited(c, project, "agent_runs", awaited_id, "denied")
        runner.tick(c, Ctx(eng), project_id=project)
        _assert_stopped(c, run["id"], _steps(c, run["id"]))
        c.execute(text("DELETE FROM agent_runs WHERE id=:i"), {"i": awaited_id})


# ── 2. The operator's two ways forward ───────────────────────────────────────────────────────

def _research_then_panel(c, project_id, *, research_optional=True):
    _, version = _publish(c, project_id, f"rp_{uuid.uuid4().hex[:6]}", [
        {"id": "research", "kind": "capability",
         "binding": {"capability": "research", "inputs": {"seed": "x"}},
         "next": ["meeting"]},
        {"id": "meeting", "kind": "capability",
         "binding": {"capability": "meeting",
                     "inputs": {"directive": "build it",
                                **({"research": {"from_step": "research", "field": "dossier"}}
                                   if research_optional else
                                   {"directive": {"from_step": "research",
                                                  "field": "dossier"}})}},
         "next": []},
    ], "research")
    return _start(c, project_id, version["id"], {})


def _kill_research(c, project_id, discovery_id, state='{}'):
    c.execute(
        text("INSERT INTO discovery_runs (id, project_id, status, phase, title, state) "
             "VALUES (:i, :p, 'cancelled', 'vet', 'x', CAST(:st AS JSONB)) "
             "ON CONFLICT (id) DO UPDATE SET status='cancelled', state=EXCLUDED.state"),
        {"i": discovery_id, "p": project_id, "st": state},
    )


def test_skip_and_proceed_records_who_and_why_and_the_run_goes_on(eng, project, monkeypatch):
    """The button the product owner asked for. The skipped step reads `skipped`, never
    `succeeded`; the reason, the actor and what it was before are on the step and in an event;
    and the next step runs — told explicitly that its research input was skipped."""
    did = f"drun_{uuid.uuid4().hex[:8]}"
    discovery = RecordingTool("create_discovery", {"run_id": did})
    meeting = RecordingTool("request_meeting", {"run_id": f"mrun_{uuid.uuid4().hex[:8]}"})
    monkeypatch.setattr(runner, "_tool_registry",
                        lambda: RecordingRegistry([discovery, meeting]))

    with eng.begin() as c:
        run = _research_then_panel(c, project)
        runner.tick(c, Ctx(eng), project_id=project)
        _kill_research(c, project, did,
                       '{"research_brief": {"question": "half-finished finding"}}')
        runner.tick(c, Ctx(eng), project_id=project)
        stopped = _steps(c, run["id"])
        assert stopped["research"]["status"] == "failed"
        assert not meeting.calls, "the panel convened on top of research that died"
        # What it reached is kept where the operator can read it — on the failed step only.
        assert "half-finished finding" in str(stopped["research"]["output"])

        result = workflows.skip_failed_step(
            c, project_id=project, run_id=run["id"], step_id="research",
            actor="ceo", reason="the directive is specific enough")
        runner.tick(c, Ctx(eng), project_id=project)
        steps = _steps(c, run["id"])
        event = c.execute(
            text("SELECT actor, data FROM events WHERE project_id=:p "
                 "AND event_type='workflow.step_skipped' AND entity_id=:r"),
            {"p": project, "r": run["id"]}).mappings().first()
        c.execute(text("DELETE FROM discovery_runs WHERE id=:i"), {"i": did})

    assert result["status"] == "skipped"
    research = steps["research"]
    assert research["status"] == "skipped"
    assert research["output"]["skipped_by_operator"] is True
    assert research["output"]["skip_reason"] == "the directive is specific enough"
    assert research["output"]["skipped_by"] == "ceo"
    assert research["output"]["skipped_from"] == "failed"
    assert event and event["actor"] == "ceo"
    assert event["data"]["reason"] == "the directive is specific enough"

    assert meeting.calls, "the run did not proceed after the skip"
    handed = meeting.calls[0]["args"]
    assert "research" not in handed, "the panel was handed a value from a skipped step"
    assert "half-finished" not in str(handed), "the failed attempt's partial work leaked on"
    marker = steps["meeting"]["output"]["skipped_inputs"]["research"]
    assert marker["skipped"] is True and marker["from_step"] == "research"
    assert marker["reason"] == "the directive is specific enough"


def test_a_step_the_operator_skipped_can_be_tried_again(eng, project, monkeypatch):
    """HelpDesk D1: preview failed (no database) and was skipped; deploy, which needs a reviewed
    preview, then dead-lettered. With the cause fixed the preview must run again ON THIS RUN —
    Try again on a skipped step re-queues it, and its skip stays only as history."""
    from boss_engine.identity import consent as _consent

    monkeypatch.setattr(_consent, "require_for_retry", lambda *a, **k: None)
    did = f"drun_{uuid.uuid4().hex[:8]}"
    discovery = RecordingTool("create_discovery", {"run_id": did})
    meeting = RecordingTool("request_meeting", {"run_id": f"mrun_{uuid.uuid4().hex[:8]}"})
    monkeypatch.setattr(runner, "_tool_registry",
                        lambda: RecordingRegistry([discovery, meeting]))
    with eng.begin() as c:
        run = _research_then_panel(c, project)
        runner.tick(c, Ctx(eng), project_id=project)
        _kill_research(c, project, did, '{}')
        runner.tick(c, Ctx(eng), project_id=project)
        workflows.skip_failed_step(c, project_id=project, run_id=run["id"], step_id="research",
                                   actor="ceo", reason="no database for it yet")
        result = workflows.retry_failed_step(c, project_id=project, run_id=run["id"],
                                             step_id="research", actor="ceo",
                                             note="it has a database now")
        research = _steps(c, run["id"])["research"]
        c.execute(text("DELETE FROM discovery_runs WHERE id=:i"), {"i": did})

    assert result["was"] == "skipped" and result["status"] == "ready"
    assert research["status"] == "ready"
    assert "skipped_by_operator" not in research["output"]
    assert research["output"]["unskipped"]["reason"] == "no database for it yet"
    assert research["output"]["retry_note"] == "it has a database now"


def test_a_required_input_from_a_skipped_step_stops_that_step_with_the_reason(
        eng, project, monkeypatch):
    """A step that cannot work without the skipped output is not handed a made-up one. It
    stops, saying which step was skipped and why — and the operator decides again."""
    did = f"drun_{uuid.uuid4().hex[:8]}"
    discovery = RecordingTool("create_discovery", {"run_id": did})
    meeting = RecordingTool("request_meeting", {"run_id": "mrun_never"})
    monkeypatch.setattr(runner, "_tool_registry",
                        lambda: RecordingRegistry([discovery, meeting]))
    with eng.begin() as c:
        run = _research_then_panel(c, project, research_optional=False)
        runner.tick(c, Ctx(eng), project_id=project)
        _kill_research(c, project, did)
        runner.tick(c, Ctx(eng), project_id=project)
        workflows.skip_failed_step(c, project_id=project, run_id=run["id"],
                                   step_id="research", actor="ceo", reason="out of budget")
        # ONE tick, not "tick through every retry". The review of c8de36b found this test passed
        # on 534a1b9 too, because ticking through all the retries reaches the same end state
        # whether the skip is settled or retried. A skip is a recorded fact; the step fails
        # ONCE (SettledCapabilityError) and says why, with no retry scheduled.
        runner.tick(c, Ctx(eng), project_id=project)
        steps = _steps(c, run["id"])
        attempt = c.execute(text("SELECT attempt FROM workflow_step_runs "
                                 "WHERE workflow_run_id=:r AND step_id='meeting'"),
                            {"r": run["id"]}).scalar()
        retries = c.execute(text("SELECT count(*) FROM events WHERE entity_id=:r "
                                 "AND event_type='workflow.step_retry_scheduled'"),
                            {"r": run["id"]}).scalar()
        c.execute(text("DELETE FROM discovery_runs WHERE id=:i"), {"i": did})

    assert not meeting.calls
    assert steps["meeting"]["status"] == "deadlettered", steps["meeting"]
    assert attempt == 1
    assert retries == 0, "a settled skip was retried"
    error = steps["meeting"]["last_error"] or ""
    assert "'research' was skipped" in error and "out of budget" in error, error


def test_retry_runs_the_failed_step_again_and_the_run_resumes(eng, project, monkeypatch):
    """Retry must work for the steps that now stop: the research step is re-executed (a second
    discovery is commissioned), and once it succeeds the panel runs on its real output."""
    first, second = f"drun_{uuid.uuid4().hex[:8]}", f"drun_{uuid.uuid4().hex[:8]}"
    ids = iter([first, second])

    class Discovery(RecordingTool):
        def handler(self, ctx, *, project_id, actor, args):
            self.calls.append(dict(args))
            return {"run_id": next(ids)}

    discovery = Discovery("create_discovery", {})
    meeting = RecordingTool("request_meeting", {"run_id": f"mrun_{uuid.uuid4().hex[:8]}"})
    monkeypatch.setattr(runner, "_tool_registry",
                        lambda: RecordingRegistry([discovery, meeting]))
    with eng.begin() as c:
        run = _research_then_panel(c, project)
        runner.tick(c, Ctx(eng), project_id=project)
        _kill_research(c, project, first)
        runner.tick(c, Ctx(eng), project_id=project)
        assert _run_status(c, run["id"]) == "failed"

        retried = workflows.retry_failed_step(c, project_id=project, run_id=run["id"],
                                              step_id="research", actor="ceo")
        assert retried["status"] == "ready" and retried["was"] == "failed"
        runner.tick(c, Ctx(eng), project_id=project)
        assert len(discovery.calls) == 2, "retry did not run the step again"
        c.execute(
            text("INSERT INTO discovery_runs (id, project_id, status, phase, title, state) "
                 "VALUES (:i, :p, 'completed', 'done', 'x', "
                 "CAST('{\"research_brief\": {\"question\": \"the real finding\"}}' AS JSONB))"),
            {"i": second, "p": project})
        runner.tick(c, Ctx(eng), project_id=project)
        runner.tick(c, Ctx(eng), project_id=project)
        steps = _steps(c, run["id"])
        retried_event = c.execute(
            text("SELECT count(*) FROM events WHERE project_id=:p "
                 "AND event_type='workflow.step_retried' AND entity_id=:r"),
            {"p": project, "r": run["id"]}).scalar()
        c.execute(text("DELETE FROM discovery_runs WHERE id IN (:a, :b)"),
                  {"a": first, "b": second})

    assert steps["research"]["status"] == "succeeded"
    assert meeting.calls and "the real finding" in (meeting.calls[0]["args"].get("research") or "")
    assert retried_event == 1


def test_skip_is_refused_without_a_reason_and_for_a_step_that_did_not_fail(
        eng, project, monkeypatch):
    """A skip with no reason leaves the record unable to say why the run went on; a skip of a
    live step would abandon work in flight."""
    did = f"drun_{uuid.uuid4().hex[:8]}"
    monkeypatch.setattr(runner, "_tool_registry", lambda: RecordingRegistry(
        [RecordingTool("create_discovery", {"run_id": did})]))
    with eng.begin() as c:
        run = _research_then_panel(c, project)
        runner.tick(c, Ctx(eng), project_id=project)          # research is now waiting
        with pytest.raises(workflows.WorkflowConflictError, match="waiting"):
            workflows.skip_failed_step(c, project_id=project, run_id=run["id"],
                                       step_id="research", actor="ceo", reason="r")
        with pytest.raises(workflows.WorkflowValidationError):
            workflows.skip_failed_step(c, project_id=project, run_id=run["id"],
                                       step_id="research", actor="ceo", reason="  ")
        _kill_research(c, project, did)
        runner.tick(c, Ctx(eng), project_id=project)
        first = workflows.skip_failed_step(c, project_id=project, run_id=run["id"],
                                           step_id="research", actor="ceo", reason="r")
        # Pressed twice: the first press decided; the second reports it and changes nothing.
        again = workflows.skip_failed_step(c, project_id=project, run_id=run["id"],
                                           step_id="research", actor="ceo", reason="r2")
        events = c.execute(
            text("SELECT count(*) FROM events WHERE project_id=:p "
                 "AND event_type='workflow.step_skipped' AND entity_id=:r"),
            {"p": project, "r": run["id"]}).scalar()
        c.execute(text("DELETE FROM discovery_runs WHERE id=:i"), {"i": did})
    assert first["status"] == "skipped" and again.get("already") is True
    assert events == 1


def test_a_join_waits_for_an_operator_skip_but_not_for_a_branch_never_taken():
    """A join releases on steps that succeeded or that an operator skipped. A step skipped
    because its branch was not taken (`path_not_reached`) is not a decision about that step."""
    settled = workflows._settled_for_downstream
    assert settled({"status": "succeeded", "output": {}})
    assert settled({"status": "skipped", "output": {"skipped_by_operator": True}})
    assert not settled({"status": "skipped", "output": {"reason": "path_not_reached"}})
    assert not settled({"status": "failed", "output": {}})


# ── 3. Runs recorded before the rule ─────────────────────────────────────────────────────────

def test_research_recorded_as_failed_continued_is_rewritten_as_failed(eng, project, monkeypatch):
    """boss-1's research step still said `succeeded` in the database after the runner was fixed,
    so the API, describe_workflow_run, steps_done and the console's "Build on this research"
    all went on treating research that died as done — and Retry and Skip could never be offered
    on it. The schema reconcile rewrites those rows to `failed`, keeps their output as evidence,
    and does nothing the second time."""
    from pathlib import Path

    from boss_engine.db import split_statements

    schema = Path(__file__).resolve().parents[2] / "db" / "schema.sql"
    [statement] = [s for s in split_statements(schema.read_text(encoding="utf-8"))
                   if "failed_continued" in s and "UPDATE workflow_step_runs" in s]
    monkeypatch.setattr(runner, "_tool_registry", lambda: RecordingRegistry([]))
    with eng.begin() as c:
        run = _two_step(c, project, "notify")
        c.execute(text(
            "UPDATE workflow_step_runs SET status='succeeded', output=CAST(:o AS JSONB) "
            "WHERE workflow_run_id=:r AND step_id='s'"),
            {"r": run["id"], "o": '{"status": "failed_continued", "note": "research failed '
                                  'before it finished", "dossier": "half"}'})
        c.exec_driver_sql(statement)
        once = _steps(c, run["id"])["s"]
        c.exec_driver_sql(statement)
        twice = _steps(c, run["id"])["s"]

    assert once["status"] == "failed"
    assert once["last_error"] == "research failed before it finished"
    assert once["output"]["dossier"] == "half", "the evidence was thrown away"
    assert once["output"]["recorded_as"] == "failed_continued"
    assert twice == once
