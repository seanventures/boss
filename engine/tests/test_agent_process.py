"""Agent process — PRD handoff chain, reviews, certify, accept."""

from boss_engine.services.agent_process import (
    PRD_TO_ACCEPTED_SPEC,
    improve_prd,
    review_prd,
    validate_process_spec,
)


THIN_PRD = "Build a thing."
RICH_PRD = """
# HookWatch PRD
Users create webhook inboxes. Operators capture events.
Acceptance: must create an inbox, must verify replay.
Data model: inbox, event. API routes documented.
Risks: no arbitrary egress. Security: allowlisted replay only.
Success metric: operator captures one event end to end.
Deploy via one-command verify.
"""


def test_default_spec_validates_and_covers_the_requested_handoffs():
    spec = validate_process_spec(PRD_TO_ACCEPTED_SPEC)
    ids = [step["id"] for step in spec["steps"]]
    assert ids[0] == "intake_prd"
    assert "review_architect" in ids and "review_cto" in ids and "review_vp" in ids
    assert ids[-1] == "captain_accept"
    actions = {step["action"] for step in spec["steps"]}
    assert actions >= {
        "accept_prd", "review_prd", "improve_prd", "build",
        "qa_review", "ux_review", "verify_build", "certify_spec", "accept_work",
    }


def test_role_reviews_are_deterministic_and_do_not_invent_scores():
    thin = review_prd(THIN_PRD, "chief_architect")
    assert thin["verdict"] == "revise"
    assert thin["failed"]
    rich = review_prd(RICH_PRD, "chief_architect")
    assert rich["verdict"] == "approve"
    assert review_prd(RICH_PRD, "cto")["verdict"] == "approve"
    assert review_prd(RICH_PRD, "vp_product")["verdict"] == "approve"


def test_improve_prd_appends_review_findings():
    reviews = [review_prd(THIN_PRD, "cto"), review_prd(THIN_PRD, "vp_product")]
    improved = improve_prd(THIN_PRD, reviews)
    assert "Improvements required by review" in improved
    assert "cto" in improved


def test_panel_certify_and_accept_require_prior_handoffs():
    from boss_engine.services.agent_process import AgentProcessService

    failed = AgentProcessService._panel_certify({}, {})
    assert failed["verdict"] == "not_certified"
    done = {
        "qa_check": {"status": "succeeded"},
        "ux_review": {"status": "succeeded"},
        "captain_verify": {"status": "succeeded"},
    }
    planned = AgentProcessService._panel_certify(done, {"simulated": True})
    assert planned["verdict"] == "certified_planned"
    live = AgentProcessService._panel_certify(done, {"build_status": "completed", "tests_passed": True})
    assert live["verdict"] == "certified"


def test_panel_does_not_certify_live_without_tests():
    """Live software certification requires tests_passed is True. Never invent a scorecard."""
    from boss_engine.services.agent_process import AgentProcessService

    done = {
        "improve_prd": {"status": "succeeded", "output": {"verdict": "improved", "prd": "# HookWatch\nAcceptance: must verify."}},
        "qa_check": {"status": "succeeded", "output": {"verdict": "qa_observed", "tests_passed": None}},
        "ux_review": {"status": "succeeded", "output": {"verdict": "reviewed", "states_covered": ["empty"], "has_e2e": False}},
        "captain_verify": {"status": "succeeded", "output": {"verdict": "verified", "tests_passed": None}},
    }
    missing_tests = AgentProcessService._panel_certify(
        done, {"build_status": "completed", "prd": "# HookWatch\nAcceptance: must verify."},
    )
    assert missing_tests["verdict"] != "certified"
    assert missing_tests["verdict"] in {"not_certified", "certified_planned"}
    assert missing_tests.get("checklist")

    failed_tests = AgentProcessService._panel_certify(
        done, {"build_status": "completed", "tests_passed": False, "prd": "# HookWatch"},
    )
    assert failed_tests["verdict"] != "certified"
    assert failed_tests["verdict"] in {"not_certified", "certified_planned"}
    names = {item["name"] for item in failed_tests["checklist"]}
    assert {"improved_prd", "qa_output", "ux_output", "verify_output"} <= names
    assert all("score" not in item for item in failed_tests["checklist"])

    simulated = AgentProcessService._panel_certify(done, {"simulated": True, "prd": "# HookWatch"})
    assert simulated["verdict"] == "certified_planned"
    assert simulated.get("checklist")


def test_accept_still_requires_certified_or_certified_planned():
    from boss_engine.config import Settings
    from boss_engine.services.agent_process import AgentProcessService

    svc = AgentProcessService(Settings())
    run = {"id": "aprun_1", "context": {}, "input": {}}
    step = {"id": "captain_accept", "role": "engineering_captain", "action": "accept_work"}
    rejected = svc._execute_step(
        None, project_id="p", actor="a", run=run, step=step,
        done={"panel_certify": {"output": {"verdict": "not_certified"}}},
    )
    assert rejected["status"] == "failed"
    assert rejected["verdict"] == "rejected"

    accepted = svc._execute_step(
        None, project_id="p", actor="a", run=run, step=step,
        done={"panel_certify": {"verdict": "certified_planned"}},
    )
    assert accepted["status"] == "succeeded"
    assert accepted["verdict"] == "accepted"

    live_ok = svc._execute_step(
        None, project_id="p", actor="a", run=run, step=step,
        done={"panel_certify": {"output": {"verdict": "certified"}}},
    )
    assert live_ok["status"] == "succeeded"


def test_refresh_build_patches_context_after_live_code_task_completes():
    """After a live code_task completes, context must record evidence — not a no-op patch."""
    from boss_engine.config import Settings
    from boss_engine.services.agent_process import AgentProcessService

    svc = AgentProcessService(Settings())
    patches = []
    svc._patch_context = lambda conn, run_id, patch: patches.append({"run_id": run_id, "patch": dict(patch)})

    conn = _FakeConn(code_task={
        "status": "completed",
        "state": {
            "tests_passed": True,
            "ui_evidence": {"states_covered": ["empty", "error"]},
            "ux_evidence": {"has_e2e": True},
        },
    })
    result = svc._refresh_build(
        conn, "proj_1",
        {"run_id": "crun_1", "build_status": "queued"},
        process_run_id="aprun_abc",
    )
    assert result["status"] == "succeeded"
    assert result["tests_passed"] is True
    assert patches, "live complete must patch process-run context"
    assert patches[0]["run_id"] == "aprun_abc"
    assert patches[0]["patch"]["tests_passed"] is True
    assert patches[0]["patch"]["build_status"] == "completed"
    assert patches[0]["patch"]["ui_evidence"]["states_covered"] == ["empty", "error"]
    assert patches[0]["patch"]["ux_evidence"]["has_e2e"] is True


def test_start_and_handoff_receipts_use_stable_dedupe_keys():
    from boss_engine.config import Settings
    from boss_engine.services.agent_process import AgentProcessService

    svc = AgentProcessService(Settings())
    calls = []

    def fake_create(conn, org_id, workspace_id, project_id, dedupe_key, effect_kind, effect_scope, payload, **kwargs):
        calls.append({"dedupe_key": dedupe_key, "effect_kind": effect_kind, "project_id": project_id})
        return {"id": f"rcpt_{len(calls)}", "dedupe_key": dedupe_key}, True

    svc.receipts.create_or_reuse = fake_create
    svc._scope = lambda conn, project_id: {"org_id": "org1", "workspace_id": "ws1", "project_id": project_id}

    # A receipt for a step that has already run is CLOSED when it is written, so this now does
    # touch the connection. `None` was fine when it only created and forgot.
    marked = []

    class _Conn:
        def execute(self, statement, params=None):
            marked.append(dict(params or {}))

            class _R:
                def mappings(self):
                    return self

                def first(self):
                    return None

            return _R()

    conn = _Conn()

    start = svc._record_process_receipt(
        conn, project_id="p1", run_id="aprun_1", step_key="start",
        payload={"simulated": True},
    )
    handoff = svc._record_process_receipt(
        conn, project_id="p1", run_id="aprun_1", step_key="intake_prd",
        payload={"status": "succeeded"},
    )

    # Not just created: CLOSED. Eleven of these per process run were sitting at in_progress
    # forever, which is what made 88% of the receipt trail unreadable.
    assert len(marked) == 2, "each step's receipt must be marked, not left open"
    assert start["id"] == "rcpt_1"
    assert handoff["id"] == "rcpt_2"
    assert [item["dedupe_key"] for item in calls] == [
        "agent_process:aprun_1:start",
        "agent_process:aprun_1:intake_prd",
    ]


def test_record_step_records_receipt_for_handoff(monkeypatch):
    from boss_engine.config import Settings
    from boss_engine.services.agent_process import AgentProcessService

    monkeypatch.setattr("boss_engine.services.agent_process.emit_event", lambda *a, **k: "evt_1")
    svc = AgentProcessService(Settings())
    recorded = []
    svc._record_process_receipt = lambda conn, **kwargs: recorded.append(kwargs) or {"id": "rcpt_h"}
    conn = _FakeConn(returning={
        "id": "apstep_1",
        "run_id": "aprun_1",
        "project_id": "p1",
        "step_key": "intake_prd",
        "role_key": "product_manager",
        "action": "accept_prd",
        "status": "succeeded",
        "output": {},
    })
    svc._record_step(
        conn,
        run={"id": "aprun_1", "project_id": "p1", "current_steps": ["intake_prd"]},
        step={"id": "intake_prd", "role": "product_manager", "action": "accept_prd"},
        result={"status": "succeeded", "verdict": "accepted"},
        actor="tester",
    )
    assert recorded
    assert recorded[0]["run_id"] == "aprun_1"
    assert recorded[0]["step_key"] == "intake_prd"
    assert recorded[0]["project_id"] == "p1"


def test_simulated_build_never_enqueues_code_task(monkeypatch):
    from boss_engine.config import Settings
    from boss_engine.services import code_task_service
    from boss_engine.services.agent_process import AgentProcessService

    called = []
    monkeypatch.setattr(
        code_task_service, "start_code_task",
        lambda *a, **k: called.append(k) or "crun_should_not_exist",
    )
    svc = AgentProcessService(Settings())
    patches = []
    svc._patch_context = lambda conn, run_id, patch: patches.append(patch)
    result = svc._execute_build(
        None,
        project_id="p",
        actor="a",
        run={"id": "aprun_sim", "title": "sim"},
        ctx={"prd": "Build a thing.", "simulated": True},
        inp={"simulated": True, "auto_build": True, "prd": "Build a thing."},
        process_run_id="aprun_sim",
    )
    assert called == []
    assert result["verdict"] == "planned"
    assert result["status"] == "succeeded"
    assert patches and patches[0]["build_status"] == "planned"


def test_attached_build_binds_existing_run_instead_of_spawning_another(monkeypatch):
    from boss_engine.config import Settings
    from boss_engine.services import code_task_service
    from boss_engine.services.agent_process import AgentProcessService

    called = []
    monkeypatch.setattr(
        code_task_service, "start_code_task",
        lambda *a, **k: called.append(k) or "crun_should_not_exist",
    )
    svc = AgentProcessService(Settings())
    patches = []
    svc._patch_context = lambda conn, run_id, patch: patches.append(patch)
    svc._refresh_build = lambda *a, **k: {
        "status": "waiting", "verdict": "running", "run_id": "crun_live",
    }
    result = svc._execute_build(
        None,
        project_id="p",
        actor="a",
        run={"id": "aprun_att", "title": "official"},
        ctx={"prd": "Fix HookWatch verify."},
        inp={"auto_build": True, "attach_run_id": "crun_live", "official": True},
        process_run_id="aprun_att",
    )
    assert called == []
    assert result["run_id"] == "crun_live"
    assert result["status"] == "waiting"
    assert patches and patches[0]["attached"] is True


class _Mappings:
    def __init__(self, row):
        self._row = row

    def first(self):
        return self._row

    def one(self):
        return self._row

    def all(self):
        return [self._row] if self._row else []


class _Result:
    def __init__(self, row=None, scalar=None):
        self._row = row
        self._scalar = scalar

    def mappings(self):
        return _Mappings(self._row)

    def scalar_one_or_none(self):
        return self._scalar


class _FakeConn:
    def __init__(self, *, code_task=None, returning=None):
        self.code_task = code_task
        self.returning = returning or {}
        self.statements = []

    def execute(self, stmt, params=None):
        sql = " ".join(str(stmt).split())
        self.statements.append((sql, params or {}))
        low = sql.lower()
        if "from code_task_runs" in low:
            return _Result(self.code_task)
        if "select id from agent_process_steps" in low:
            return _Result(scalar=None)
        if "returning" in low:
            row = dict(self.returning)
            if params:
                row.setdefault("id", params.get("id") or params.get("k"))
                row.setdefault("step_key", params.get("k"))
                row.setdefault("status", params.get("st"))
            return _Result(row)
        return _Result()


def test_custom_spec_rejects_unknown_roles_and_cycles_of_missing_targets():
    try:
        validate_process_spec({
            "entry": "a",
            "steps": [{"id": "a", "kind": "role", "role": "wizard", "action": "accept_prd"}],
        })
    except ValueError as exc:
        assert "role" in str(exc)
    else:
        raise AssertionError("unknown role must fail")
    try:
        validate_process_spec({
            "entry": "a",
            "steps": [{
                "id": "a", "kind": "role", "role": "qa", "action": "qa_review", "next": ["missing"],
            }],
        })
    except ValueError as exc:
        assert "missing" in str(exc)
    else:
        raise AssertionError("missing next must fail")
