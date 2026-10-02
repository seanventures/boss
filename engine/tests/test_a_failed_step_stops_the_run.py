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

import uuid

import pytest
from sqlalchemy import text

from boss_engine.services import workflow_capability_runner as runner
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

#: Capabilities that call one tool and finish. Failure is the tool saying so — a returned
#: `status: failed` or `ok: false`.
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
}

#: Tool-less capabilities that commission NOTHING. They are honest no-ops that say so
#: (`status: recorded`), so there is no work that could fail and be mistaken for success.
COMMISSIONS_NOTHING = {"notify", "video_edit", "social_publish"}


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
    for col in columns:
        name, kind = col["column_name"], col["data_type"]
        if name in values:
            continue
        if name == "project_id":
            values[name] = project_id
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


def _awaited_cases():
    for key, kind in sorted(AWAITED.items()):
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
    table = runner._AWAIT_TABLES[kind][0]

    with eng.begin() as c:
        run = _two_step(c, project, key)
        runner.tick(c, Ctx(eng), project_id=project)
        parked = _steps(c, run["id"])["s"]
        assert parked["status"] == "waiting", parked
        assert parked["output"]["await"] == {"kind": kind, "id": awaited_id}

        _insert_awaited(c, project, table, awaited_id, status)
        runner.tick(c, Ctx(eng), project_id=project)
        runner.tick(c, Ctx(eng), project_id=project)
        steps = _steps(c, run["id"])
        _assert_stopped(c, run["id"], steps)
        assert status in (steps["s"]["last_error"] or ""), steps["s"]["last_error"]
        c.execute(text(f"DELETE FROM {table} WHERE id=:i"), {"i": awaited_id})  # noqa: S608


@pytest.mark.parametrize("returned", [
    {"status": "failed", "error": "the tool said no"},
    {"ok": False, "error": "the tool said no"},
], ids=["status-failed", "ok-false"])
@pytest.mark.parametrize("key", sorted(TOOL_BACKED))
def test_a_tool_that_reports_failure_stops_the_run(eng, project, monkeypatch, key, returned):
    """deploy, smoke_test, redeploy, retry_build, teardown_preview, cost_check — failure said
    either way the tools say it. Retried as the step allows, then dead-lettered; never green."""
    tool = RecordingTool(CAPABILITIES[key].tool, returned)
    monkeypatch.setattr(runner, "_tool_registry", lambda: RecordingRegistry([tool]))
    monkeypatch.setattr(runner, "_preview_missing_for_run", lambda *a, **k: None)

    with eng.begin() as c:
        run = _two_step(c, project, key)
        _tick_through_retries(eng, c, project, run["id"])
        steps = _steps(c, run["id"])
        _assert_stopped(c, run["id"], steps)


@pytest.mark.parametrize("key", sorted(INTERNAL))
def test_work_the_runner_does_itself_stops_the_run_when_it_fails(eng, project, monkeypatch, key):
    """`report` used to catch every exception and return `status: recorded` — green, with no
    report behind it. The same lie as the incident, one capability over."""
    def explode(*_a, **_k):
        raise RuntimeError(f"{key} could not be produced")

    monkeypatch.setattr(runner, INTERNAL[key], explode)
    monkeypatch.setattr(runner, "_tool_registry", lambda: RecordingRegistry([]))

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
    """A gate's "failure" is a person saying no. The run blocks there — and Skip is refused,
    because skipping would turn that no into a yes."""
    monkeypatch.setattr(runner, "_tool_registry", lambda: RecordingRegistry([]))
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
        assert steps["s"]["status"] != "succeeded"
        assert "after" not in steps or steps["after"]["status"] == "pending"
        assert _run_status(c, run["id"]) == "blocked"
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
        _tick_through_retries(eng, c, project, run["id"])
        steps = _steps(c, run["id"])
        c.execute(text("DELETE FROM discovery_runs WHERE id=:i"), {"i": did})

    assert not meeting.calls
    assert steps["meeting"]["status"] in {"failed", "deadlettered"}
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
