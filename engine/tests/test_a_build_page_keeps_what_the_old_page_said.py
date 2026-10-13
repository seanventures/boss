"""A build's page keeps what its old page said (the Factory redesign parity audit).

The operator, after the Factory redesign: "lots of features are broken especially the view of
each run - every run will give different PR and steps and details.. they are all missing".

The old page read every fact straight off the run row. The cockpit reads acts and gate rows,
and on the way it dropped facts the run row still holds. Measured on boss-1's four real builds
(paramg/boss-sandbox PRs #218, #219, #222):

  * every Checks row said ✓ or ✗ with NO reason: the gate row's `evidence` column is the whole
    evidence dict, keyed by evidence names, and the cockpit looked gates up in it by gate name;
  * the brief's criteria and checks, the repo and branch, the workflow and its version, which
    engines built and reviewed it — none were in the cockpit at all;
  * attempt 2 of the freshness board named no origin, though attempt 1 named the workflow run;
  * the Story showed no wall clock for BOSS's own steps (cloning, tests, starting the app, the PR).

The fixtures below are boss-1's shapes, sanitised: the same keys and values, no ids or text of
the real builds beyond what the API showed.
"""
from __future__ import annotations

from datetime import datetime, timezone

from boss_engine.services import factory_view as fv

NOW = datetime(2026, 10, 3, 12, 0, tzinfo=timezone.utc)

#: crun_3e370c11 on boss-1 ("Lead triage inbox", PR #218), sanitised.
STATE = {
    "tests": [{"ok": False, "command": "npm install && npm run build && npm run test:e2e",
               "exit_code": 1, "timed_out": False}],
    "tests_passed": False, "test_exit_code": 1,
    "adversarial_verdict": "refuted", "security_verdict": "issues",
    "consistency_verdict": "drift", "regressions_ok": False, "mutation_caught": None,
    "review_panel_size": 1, "review_confidence": 0.0,
    "files_changed": 49, "impl_files": [f"src/f{i}.ts" for i in range(34)],
    "test_files": [f"tests/t{i}.test.ts" for i in range(15)],
    "engine": "claude_code", "build_engine": "claude_code", "model": "opus",
    "review_engine": "codex", "review_model": "gpt-6-sol", "turns": 58,
    "substrate": "local_subprocess", "profile": "lean", "risk_tier": 2,
    "reviewer_independent": True, "test_author_independent": True,
    "deferred_findings": [], "web_package_contract": {"ok": True},
    "app_serves": {"ok": True, "command": "npm run start"},
}
FCTX = {
    "workflow": {"name": "Standard Product Delivery", "source": "mapping", "version": 1},
    "work_branch": "boss/code/crun_lead", "workflow_run_id": "wfr_lead",
    "execution_substrate": "local_subprocess",
    "advisory_failing_gates": ["written_tests_pass", "code_review_passed", "security_clean",
                               "nothing_broke", "consistent"],
    "phase_timings": [
        {"ms": 730, "phase": "provisioning"}, {"ms": 272900, "phase": "design"},
        {"ms": 339014, "phase": "write_tests"}, {"ms": 668, "phase": "provisioning"},
        {"ms": 1109413, "phase": "write_tests"}, {"ms": 1973264, "phase": "implement"},
        {"ms": 162138, "phase": "run_tests"}, {"ms": 35458, "phase": "run_tests"},
        {"ms": 50157, "phase": "review_panel"}, {"ms": 2140, "phase": "serve_check"},
        {"ms": 53647, "phase": "opening_pr"},
    ],
}
GATES_PASSED = ["code_written", "existing_tests_intact", "web_package_honest",
                "tests_written_first", "coder_did_not_edit_tests"]
GATES_FAILED = ["written_tests_pass", "code_review_passed", "security_clean", "nothing_broke",
                "consistent"]


def _run(**over):
    run = {
        "id": "crun_lead", "project_id": "p1", "status": "completed", "phase": "done",
        "title": "Build a lead triage inbox.", "error": None,
        "spec": {"owner": "acme", "repo": "sandbox", "base_branch": "main",
                 "acceptance_criteria": ["A lead can be assigned", "Scores stay within 0..100"],
                 "test_commands": ["npm test"]},
        "state": dict(STATE), "finalize_ctx": dict(FCTX), "usage": {"model": "opus"},
        "pull_request_url": "https://github.com/acme/sandbox/pull/218",
        "pull_request_number": 218, "decision_id": "dec_merge",
        "created_at": "2026-10-02T01:19:00+00:00", "updated_at": "2026-10-02T02:30:00+00:00",
    }
    run.update(over)
    return run


class _Result:
    def __init__(self, rows=None, scalar=None):
        self._rows = rows or []
        self._scalar = scalar

    def mappings(self):
        return self

    def all(self):
        return self._rows

    def first(self):
        return self._rows[0] if self._rows else None

    def scalar(self):
        return self._scalar


class _Conn:
    """Answers every query empty, except the ones a test names (substring → rows)."""

    def __init__(self, answers=None, runs=None):
        self.answers = answers or {}
        self.runs = runs or {}

    def execute(self, statement, params=None):
        sql = str(statement)
        for needle, rows in self.answers.items():
            if needle in sql:
                return _Result(rows)
        return _Result([])

    def get_run(self, run_id):
        return self.runs.get(run_id)


def _outputs(run, gate_row=None, state="pr_open", decision=None):
    facts = fv.Facts()
    if gate_row is not None:
        facts.gates[run["id"]] = gate_row
    st = {"state": state, "word": "PR open", "verb": "Working", "station": "pr", "k": 6,
          "n": 8, "rounds": 1, "tip": None, "preview": None}
    return fv._outputs(_Conn(), run, facts, st, [], [], decision, NOW)


def test_a_deployed_build_is_not_called_merged_while_its_pr_is_open():
    """The OUTPUTS header said "Merged" while PR #223 was open: the build had been deployed
    from its branch, and "deployed" was read as "merged" (rc10 tester)."""
    word = _outputs(_run(), state="deployed")["merge_ready"]["word"]
    assert word != "Merged"            # its merge readiness, said like any open PR's
    assert _outputs(_run(), state="merged")["merge_ready"]["word"] == "Merged"


# ── Checks say why ────────────────────────────────────────────────────────────────────────


def test_every_failed_check_says_why_from_the_gate_rows_own_evidence():
    # boss-1's gate row: evidence is the WHOLE evidence dict, keyed by evidence names.
    gate_row = {"gates_passed": GATES_PASSED, "gates_failed": GATES_FAILED,
                "evidence": dict(STATE)}
    checks = _outputs(_run(), gate_row)["checks"]
    why = {g["name"]: g["evidence"] for g in checks["gates"]}
    assert why["written_tests_pass"] == "exit 1"
    assert why["code_review_passed"] == "refuted"
    assert why["security_clean"] == "issues"
    assert why["consistent"] == "drift"
    assert why["nothing_broke"] == "a regression"
    assert why["code_written"] == "34 files"
    assert why["tests_written_first"] == "15 test files"
    assert all(why[g] for g in GATES_FAILED), why


def test_a_build_with_no_gate_row_still_lists_its_checks_from_its_verdicts():
    checks = _outputs(_run())["checks"]
    assert checks is not None, "a finished build with verdicts showed no Checks at all"
    names = {g["name"]: g for g in checks["gates"]}
    assert names["code_review_passed"]["ok"] is False
    assert names["code_review_passed"]["evidence"] == "refuted"
    assert checks["word"] == "5 failed · shipped anyway"


def test_a_mutation_and_a_waived_check_read_as_the_old_page_said():
    assert fv.gate_reason("tests_strong_enough", True, {"mutation_caught": True}) == "mutation caught"
    assert fv.gate_reason("tests_strong_enough", False, {"mutation_caught": False}) == "mutation missed"
    assert fv.gate_reason("app_serves", False, {"app_serves": {"ok": False, "error":
                          "ImportError: attempted relative import\nTraceback"}}) \
        == "ImportError: attempted relative import"


# ── Tests and changes ─────────────────────────────────────────────────────────────────────


def test_a_command_recorded_without_ok_passes_on_exit_zero():
    # Older records carry only the exit code; the old page judged exit 0 a pass.
    state = dict(STATE, tests=[{"command": "pytest -q", "exit_code": 0, "timed_out": False,
                                "duration_ms": 41000}])
    tests = _outputs(_run(state=state))["tests"]
    assert tests["word"] == "1 of 1 command passed"
    assert tests["commands"][0]["ok"] is True
    assert tests["commands"][0]["duration_ms"] == 41000


def test_changes_list_the_diff_paths_and_its_size_when_no_test_or_code_files_were_named():
    state = dict(STATE, impl_files=[], test_files=[], files_changed=3, additions=120,
                 deletions=7, paths=["app.py", "README.md", "tests/test_app.py"])
    changes = _outputs(_run(state=state))["changes"]
    assert changes is not None, "a build with a diff said Changes: Not yet"
    assert [f["path"] for f in changes["code"]] == ["app.py", "README.md", "tests/test_app.py"]
    assert changes["word"] == "3 files · +120 −7"


# ── The brief ─────────────────────────────────────────────────────────────────────────────


def test_the_brief_carries_the_contract_the_repo_the_workflow_and_the_engines():
    b = fv.brief_of(_run())
    assert b["criteria"] == ["A lead can be assigned", "Scores stay within 0..100"]
    assert b["test_commands"] == ["npm test"]
    assert (b["repo"], b["branch"]) == ("acme/sandbox", "boss/code/crun_lead")
    assert b["workflow"] == {"name": "Standard Product Delivery", "version": 1, "default": False}
    assert (b["engine"], b["model"], b["review_engine"], b["review_model"]) == \
        ("claude_code", "opus", "codex", "gpt-6-sol")
    assert b["cross_model"] is True
    assert b["built_on"] == "API host"
    assert (b["profile"], b["risk_tier"], b["turns"]) == ("lean", 2, 58)
    assert b["advisory_failing"] == 5


def test_the_brief_says_a_microvm_and_a_default_workflow_as_they_are():
    run = _run(finalize_ctx={**FCTX, "execution_substrate": "microvm", "sandbox_task_id": "vm-7",
                             "workflow": {"name": "Standard", "source": "default"}})
    b = fv.brief_of(run)
    assert b["built_on"] == "micro-VM vm-7"
    assert b["workflow"]["default"] is True


# ── The Story's machinery ─────────────────────────────────────────────────────────────────


def _story(run, acts=None):
    facts = fv.Facts()
    facts.acts[run["id"]] = acts or []
    st = {"state": "pr_open", "word": "PR open", "verb": "Working", "station": "pr", "k": 6,
          "n": 8, "rounds": 1, "tip": None, "preview": None}
    return fv._story(_Conn(), "p1", run, facts, st, [], None, NOW, None)


def test_bosss_own_steps_say_how_long_they_took():
    rows = {r["id"]: r for r in _story(_run()) if r["type"] == "boss"}
    assert rows["cloned"]["ms"] == 730 + 668
    assert rows["ran_tests"]["ms"] == 162138 + 35458
    assert rows["serve_check"]["ms"] == 2140
    assert rows["opened_pr"]["ms"] == 53647


def test_a_build_with_no_recorded_acts_still_tells_its_phases():
    rows = [r for r in _story(_run()) if r.get("what") == "phase"]
    assert [r["text"] for r in rows] == ["Planned", "Wrote tests", "Wrote tests", "Coded",
                                         "Reviewed"]
    assert rows[0]["ms"] == 272900 and rows[0]["station"] == "plan"


# ── Every build of the app, with its PR ───────────────────────────────────────────────────


def test_the_build_list_holds_retries_and_follow_ups_with_their_pr_and_cost():
    first = _run(id="crun_a", finalize_ctx={**FCTX})
    retry = _run(id="crun_b", title="Build a lead triage inbox. · retry 1",
                 finalize_ctx={"root_run_id": "crun_a", "parent_run_id": "crun_a", "attempt": 2},
                 pull_request_number=219)
    follow = _run(id="crun_c", title="Follow-up: deferred review findings — Lead triage inbox",
                  finalize_ctx={"deferred_from_run": "crun_a", "is_followup": True},
                  pull_request_number=None, pull_request_url=None)
    other = _run(id="crun_z", title="Build a weather widget.", spec={"repo": "sandbox"},
                 finalize_ctx={})
    ident = [{"id": r["id"], "title": r["title"], "created_at": r["created_at"],
              "finalize_ctx": r["finalize_ctx"], "spec": {"repo": "sandbox"}}
             for r in (follow, retry, first, other)]
    conn = _Conn({"jsonb_build_object('app_id'": ident,
                  "AND (id = :root": [follow, retry, first],
                  "FROM llm_calls WHERE code_task_run_id = ANY": [
                      {"run_id": "crun_b", "metered": 5_540_000, "included": 0, "calls": 8}]})
    items = fv._lineage(conn, "p1", retry, NOW, None)
    by = {i["run_id"]: i for i in items}
    assert set(by) == {"crun_a", "crun_b", "crun_c"}, "a follow-up of the app was missing"
    assert by["crun_b"]["pr_number"] == 219 and by["crun_b"]["cost_usd"] == 5.54
    assert by["crun_b"]["kind"] == "Retry 1"
    assert by["crun_c"]["kind"] == "Follow-up" and by["crun_c"]["same_lineage"] is False
    assert by["crun_a"]["same_lineage"] is True


def test_a_retry_names_the_origin_of_the_build_it_retries():
    first = _run(id="crun_a")
    retry = _run(id="crun_b", finalize_ctx={"root_run_id": "crun_a", "attempt": 2})
    conn = _Conn({"FROM workflow_runs r JOIN workflow_definitions d":
                  [{"name": "Standard Product Delivery"}]}, runs={"crun_a": first})
    conn.execute = (lambda orig: (lambda st, p=None: _Result(scalar="Standard Product Delivery")
                                  if "SELECT d.name FROM workflow_runs" in str(st)
                                  else orig(st, p)))(conn.execute)
    assert fv._origin(conn, "p1", retry) is None
    origin = fv.lineage_origin(conn, "p1", retry)
    assert origin == {"kind": "workflow_run", "id": "wfr_lead",
                      "label": "Standard Product Delivery"}


def test_the_preview_says_where_it_runs_and_each_step_of_starting_it():
    facts = fv.Facts()
    facts.previews["crun_lead"] = {
        "id": "prev_1", "run_id": "crun_lead", "status": "starting", "url": None, "error": None,
        "expires_at": None, "created_at": "2026-10-02T02:31:00+00:00", "substrate": "preview_docker",
        "steps": [{"step": "install", "label": "Install", "status": "ok", "duration_ms": 41000},
                  {"step": "build", "label": "Build", "status": "failed",
                   "log": "sh: vite: not found\nexit 127"}],
    }
    st = {"state": "pr_open", "word": "PR open", "verb": "Working", "station": "pr", "k": 6,
          "n": 8, "rounds": 1, "tip": None, "preview": None}
    preview = fv._outputs(_Conn(), _run(), facts, st, [], [], None, NOW)["preview"]
    assert preview["substrate"] == "docker"
    assert preview["steps"] == [
        {"label": "Install", "status": "ok", "duration_ms": 41000, "log": None},
        {"label": "Build", "status": "failed", "duration_ms": None, "log": "sh: vite: not found"},
    ]
