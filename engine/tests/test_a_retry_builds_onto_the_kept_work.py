"""A retry never runs on an empty tree, and a committed secret gets one repair turn first.

HelpDesk run wfr_fb4fb5ded8c84a4dab12, 2026-10-14 (BOSSAPP-546, BOSSAPP-548):
  1. crun_ced396a4 ran 26 minutes ($11): tests passed, the app served twice, and then the secret
     scan blocked it for a literal at helpdesk-ai/auth/demo.mjs:58 the implementer was never
     shown. A blocked template build keeps "no diff with a secret in it", so it kept NOTHING.
  2. The operator's step Retry resumed it (retry_code_task, repair mode) because it had an approved
     design and finished stages, which counted as "work". crun_3933d79d started on the bare base
     branch, was told "the previous attempt's code is ALREADY PRESENT", reused other apps' tests
     as its oracle, and its reviewer found "no helpdesk-ai application code whatsoever".

The class: a retry decided "repair" from MILESTONES (design, completed stages) instead of FILES,
and a blocked build could end with no files kept. Now:
  * a blocked build keeps its work, with each flagged secret value redacted (never pushed: the
    template hold stands) — secret_scan.without_secrets, _save_workspace;
  * a retry repairs only work that was kept (saved workspace, a recorded branch push, a PR); a
    lineage that kept none is rebuilt in full — the workflow step re-runs its own build from its
    original inputs plus the note (_retried_build), a code task's Retry is a fresh build;
  * a repair whose tree holds none of the previous attempt's files stops before review;
  * the scan's findings get one implementer turn of their own before the build is blocked.
"""
from __future__ import annotations

import inspect
import json
import textwrap
import types
import uuid

import pytest
from sqlalchemy import text

from boss_engine.config import get_settings
from boss_engine.identity import lineage
from boss_engine.services import code_task_service as cts
from boss_engine.services import governed_code_task_service as gov
from boss_engine.services import workflow_capability_runner as runner
from boss_engine.services import workflows
from boss_engine.templates import secret_scan

from .test_a_resumed_build_runs_as_the_person import _Jobs, _ports  # noqa: F401
from .test_identity_agents_workflow_consent import env  # noqa: F401  (full-schema fixture)
from .test_no_step_is_blocked_by_consent import (  # noqa: F401
    RUN_INPUT, _publish, _start_consented, staffed)

# Assembled so this file itself carries no literal the scan would flag.
_LIT = "Agent" + "Pass" + "word" + "77xq"
DEMO = ("export const DEMO_USER = 'agent@example.test';\n"
        f'export const demo = {{ password: "{_LIT}" }};\n'
        "export default demo;\n")
SECRET_ERROR = ("Blocked: the change holds something that looks like a committed secret "
                "(helpdesk-ai/auth/demo.mjs:58 assigned_secret). Open the build's Checks to see "
                "which file and line looked like a secret, remove it (secrets come from the app's "
                "Secrets, never the code), then retry the build.")
NOTE = ("Remove the hard-coded value at helpdesk-ai/auth/demo.mjs:58. Read the demo password only "
        "from process.env.DEMO_PASSWORD (BOSS provides it); never put a literal password, key or "
        "token in the code.")
APP_FILES = ["helpdesk-ai/server.mjs", "helpdesk-ai/auth/demo.mjs", "helpdesk-ai/package.json"]

SPEC = {"entry_step": "build", "steps": [
    {"id": "build", "kind": "capability", "next": [],
     "binding": {"capability": "build", "inputs": {"title": "HelpDesk", "instructions": "build it",
                                                   "repository": "acme/web"}}}]}


# --- a blocked build keeps its work, never its secret ----------------------------------------------

def test_a_flagged_literal_is_redacted_in_place_and_the_rest_of_the_tree_is_kept():
    files = {"helpdesk-ai/auth/demo.mjs": DEMO, "helpdesk-ai/server.mjs": "listen(3000)\n"}
    found = [f.as_dict() for f in secret_scan.scan_files(files)]
    assert found == [{"path": "helpdesk-ai/auth/demo.mjs", "line": 2, "rule": "assigned_secret"}]
    kept, redacted, dropped = secret_scan.without_secrets(files, found)
    assert set(kept) == set(files) and redacted == ["helpdesk-ai/auth/demo.mjs"] and dropped == []
    demo = kept["helpdesk-ai/auth/demo.mjs"]
    assert _LIT not in demo and secret_scan.REDACTED in demo
    assert demo.splitlines()[0] == DEMO.splitlines()[0]          # only the flagged line changed
    assert kept["helpdesk-ai/server.mjs"] == "listen(3000)\n"
    assert secret_scan.scan_files(kept) == []                      # nothing kept trips the scan


def test_a_secret_that_cannot_be_redacted_on_its_line_drops_the_file():
    key = "-----BEGIN " + "PRIVATE KEY-----\nMIIEvQ\n-----END PRIVATE KEY-----\n"
    files = {"k.pem": key, "cfg.js": "const t = 'x';\n", "ok.js": "1\n"}
    found = [{"path": "k.pem", "line": 1, "rule": "private_key"},
             {"path": "cfg.js", "line": 1, "rule": "bound_secret:API_TOKEN"}]
    kept, redacted, dropped = secret_scan.without_secrets(files, found)
    assert sorted(dropped) == ["cfg.js", "k.pem"] and redacted == [] and list(kept) == ["ok.js"]


def test_the_blocked_build_saves_its_tree_redacted_and_pushes_nothing():
    src = textwrap.dedent(inspect.getsource(gov._run_governed_code_task))  # noqa: SLF001
    start = src.index("    def _save_workspace(")
    save = src[start:src.index("\n    def ", start + 10)]
    assert "without_secrets(" in save and "'saved_workspace'" in save
    # It no longer returns early on a failed secret gate: it redacts instead.
    assert 'if not _impl_ran[0]:\n            return 0' in save
    assert "upsert_file" not in save and "ensure_branch" not in save


def test_a_retry_can_build_onto_a_kept_workspace_or_a_recorded_push():
    assert cts.attempt_kept_work({"finalize_ctx": {"saved_workspace": {
        "files": [{"path": "helpdesk-ai/server.mjs", "content": "x"}]}}})
    assert cts.attempt_kept_work({"finalize_ctx": {"work_branch": "boss/code/crun_x"}})
    assert cts.attempt_kept_work({"pull_request_url": "https://github.com/a/b/pull/1",
                                  "finalize_ctx": {}})
    assert cts.attempt_kept_work({"status": "failed", "finalize_ctx": {"pending_delivery": {
        "files": [{"path": "a.ts", "content": "x"}]}}})          # redelivered, never rebuilt
    # MILESTONES ARE NOT WORK: crun_ced396a4's shape — a design and finished stages, no file.
    assert not cts.attempt_kept_work({
        "status": "blocked", "state": {"design": {"plan": "p"}},
        "finalize_ctx": {"completed_stages": ["design", "write_tests", "implement"],
                         "pending_diff": [], "template_hold": {"gates": ["no_committed_secrets"]}}})


def test_a_stage_commit_and_the_repair_hand_off_record_the_branch():
    src = inspect.getsource(gov._run_governed_code_task)  # noqa: SLF001
    commit = src[src.index("    def _commit_stage("):src.index("    def _record_work_branch(")]
    assert '_record_work_branch("stage_commit")' in commit
    handoff = src[src.index("wip({run_id}): persist attempt for repair"):]
    assert handoff.index('_record_work_branch("repair_handoff")') < handoff.index(
        "code_task.work_persisted")


# --- the guard: a repair is never reviewed without the work it repairs ----------------------------

def test_a_repair_whose_tree_holds_none_of_the_previous_files_stops_before_review(tmp_path):
    runs = {"crun_p": {"id": "crun_p", "state": {"impl_files": APP_FILES[:2],
                                                 "test_files": ["helpdesk-ai/tests/a.test.mjs"]},
                       "finalize_ctx": {"pending_diff": []}}}
    expected = gov.previous_attempt_paths(runs.get, {"parent_run_id": "crun_p"})
    assert "helpdesk-ai/server.mjs" in expected and gov.previous_attempt_paths(runs.get, {}) == []
    (tmp_path / "package.json").write_text("{}")                  # the base branch's own files
    verdict = gov.repair_lost_the_work(expected, tmp_path, scope="helpdesk-ai")
    assert verdict and "none of the 3 file(s)" in verdict and "BOSS's fault" in verdict
    (tmp_path / "helpdesk-ai/auth").mkdir(parents=True)
    (tmp_path / "helpdesk-ai/auth/demo.mjs").write_text("x")
    assert gov.repair_lost_the_work(expected, tmp_path, scope="helpdesk-ai") is None
    assert gov.repair_lost_the_work([], tmp_path) is None


def test_the_guard_runs_on_every_repair_before_the_review_panel():
    src = inspect.getsource(gov._run_governed_code_task)  # noqa: SLF001
    guard = src.index("repair_lost_the_work(previous_attempt_paths(")
    assert src.rindex("if _is_repair and not use_microvm:", 0, guard) > src.index(
        "quality_passes_used = 1")
    assert guard < src.index('_phase(c, "review_panel")')
    assert 'reason_code="repair_without_the_work"' in src[guard:guard + 1200]


# --- the secret scan gives the implementer one repair turn -----------------------------------------

def test_a_literal_password_gets_one_repair_turn_and_then_passes():
    tree = {"helpdesk-ai/auth/demo.mjs": DEMO}
    findings = [f.as_dict() for f in secret_scan.scan_files(tree)]
    prompts: list[str] = []

    def fix(prompt: str) -> dict:
        prompts.append(prompt)
        tree["helpdesk-ai/auth/demo.mjs"] = DEMO.replace(
            f'"{_LIT}"', "process.env.DEMO_PASSWORD")
        return {"cost_usd_micros": 5}

    def rejudge() -> dict:
        found = [f.as_dict() for f in secret_scan.scan_files(tree)]
        return {"passed": not found, "findings": found}

    record = gov.repair_committed_secrets(findings, run_fix=fix, rejudge=rejudge)
    assert record["cleared"] is True and record["findings_after"] == [] and len(prompts) == 1
    said = prompts[0]
    assert "helpdesk-ai/auth/demo.mjs:2 (assigned_secret)" in said
    assert "process.env.DEMO_PASSWORD" in said and "never a literal" in said
    assert _LIT not in said and _LIT not in json.dumps(record)    # never the value


def test_a_secret_that_persists_after_its_turn_still_blocks():
    tree = {"helpdesk-ai/auth/demo.mjs": DEMO}
    findings = [f.as_dict() for f in secret_scan.scan_files(tree)]
    record = gov.repair_committed_secrets(
        findings, run_fix=lambda prompt: {},
        rejudge=lambda: {"passed": False, "findings": findings})
    assert record["cleared"] is False and record["findings_after"] == findings
    # A secret still there is a failing template gate, which no autonomy setting ships past.
    assert "no_committed_secrets" in gov._NEVER_SHIP_PAST  # noqa: SLF001
    from boss_engine.templates.gate import hold_outcome

    held = hold_outcome(["no_committed_secrets"], secret_scan={"findings": findings, "count": 1})
    assert held and held["error"].startswith("Blocked: the change holds something that looks like")


def test_the_turn_runs_where_the_loop_can_act_before_the_gate_decides():
    src = inspect.getsource(gov._run_governed_code_task)  # noqa: SLF001
    turn = src.index("repair_committed_secrets(")
    assert src.index("for round_i in range(1, inrun_max + 1):") < turn   # after the warm rounds
    assert turn < src.index("action = resolve_gate_failure_action(", turn)
    block = src[src.rindex("if (_template_record and gates.get(\"no_committed_secrets\") is False",
                           0, turn):turn]
    assert "not use_microvm and _coder_owned_secrets()" in block
    after = src[turn:src.index("rounds_used = max(0", turn)]
    for refreshed in ("gates.update(_template_record[\"gates\"])", "_tpl_gates = dict(gates)",
                      "failing = [k for k, v in gates.items() if not v]", "_record_quality_gate("):
        assert refreshed in after, refreshed


def test_every_builder_is_told_where_secrets_come_from():
    rule = gov.SECRETS_FROM_ENV_RULE
    assert "process.env.DEMO_PASSWORD" in rule and "never a literal" in rule
    assert rule in gov._impl_prompt("t", "i", ["t.test.js"], "npm test")  # noqa: SLF001
    assert rule in gov._solo_prompt("t", "i", [], "npm test", web=True)  # noqa: SLF001
    assert rule in gov._solo_prompt("t", "i", [], "python -m unittest")  # noqa: SLF001


# --- the retry: onto the kept work, or a full build — the exact scenario --------------------------

def _blocked_build(env, run_id: str, *, fctx: dict, state: dict, note: str = NOTE) -> str:
    crun = f"crun_{uuid.uuid4().hex[:12]}"
    with env.engine.begin() as c:
        c.execute(text(
            "INSERT INTO code_task_runs (id, project_id, task_id, status, phase, title, spec, "
            "state, finalize_ctx, error) VALUES (:id, :p, NULL, 'blocked', 'done', 'HelpDesk', "
            "CAST(:spec AS JSONB), CAST(:st AS JSONB), CAST(:fc AS JSONB), :e)"),
            {"id": crun, "p": env.project, "e": SECRET_ERROR,
             "spec": json.dumps({"instructions": "build it", "acceptance_criteria": [],
                                 "test_commands": [], "owner": "acme", "repo": "web",
                                 "base_branch": "main"}),
             "st": json.dumps(state),
             "fc": json.dumps({"attempt": 1, "root_run_id": crun, "workflow_run_id": run_id,
                               "failing_gates": ["no_committed_secrets", "consistent"],
                               "template_hold": {"gates": ["no_committed_secrets"]}, **fctx})})
        c.execute(text(
            "UPDATE workflow_step_runs SET status='failed', last_error=:e, "
            "output=CAST(:o AS JSONB) WHERE workflow_run_id=:r AND step_id='build'"),
            {"r": run_id, "e": SECRET_ERROR,
             "o": json.dumps({"capability": "build", "code_task_run_id": crun})})
        c.execute(text("UPDATE workflow_runs SET status='failed' WHERE id=:r"), {"r": run_id})
        workflows.retry_failed_step(c, project_id=env.project, run_id=run_id, step_id="build",
                                    actor="ceo", note=note)
    return crun


def _step_retry(env, run_id):
    ctx = types.SimpleNamespace(engine=env.engine, settings=get_settings(), jobs=_Jobs())
    with lineage.acting(lineage.SYSTEM_ACTING), env.engine.begin() as c:
        return runner._retried_build(c, ctx, env.project, {"id": "build"},  # noqa: SLF001
                                     {"run_id": run_id},
                                     {"title": "HelpDesk", "instructions": "build it"})


def _children(env, crun) -> list[dict]:
    with env.engine.begin() as c:
        return [dict(r) for r in c.execute(text(
            "SELECT id, spec, finalize_ctx FROM code_task_runs "
            "WHERE finalize_ctx->>'parent_run_id' = :r"), {"r": crun}).mappings()]


#: crun_ced396a4 as it ended: a design, four finished stages, 24+4 files judged — and no file kept.
TONIGHT_STATE = {"design": {"plan": "auth module, ticket store, web UI"},
                 "impl_files": APP_FILES, "test_files": ["helpdesk-ai/tests/auth.test.mjs"]}
TONIGHT_FCTX = {"completed_stages": ["design", "write_tests", "implement", "review_panel"],
                "pending_diff": []}


def test_tonight_a_step_retry_of_a_blocked_build_that_kept_nothing_runs_the_steps_full_build(
        env, staffed):  # noqa: F811
    run_id = _start_consented(env, _publish(env, SPEC, f"keep_{uuid.uuid4().hex[:6]}"),
                              RUN_INPUT)["id"]
    crun = _blocked_build(env, run_id, fctx=TONIGHT_FCTX, state=TONIGHT_STATE)
    resumed, inputs, note = _step_retry(env, run_id)
    # Not a repair of the empty tree: nothing resumed, no child attempt in the lineage...
    assert resumed is None and _children(env, crun) == []
    # ...the step's own build from its original inputs, the note as the operator's guidance.
    assert note == NOTE
    assert inputs["instructions"] == f"build it\n\n{runner.RETRY_NOTE_HEADING}\n{NOTE}"
    assert inputs["title"] == "HelpDesk"
    with env.engine.begin() as c:
        said = c.execute(text("SELECT data FROM events WHERE project_id=:p AND "
                              "event_type='workflow.build_restarted' AND entity_id=:r"),
                         {"p": env.project, "r": run_id}).scalar()
    assert said and said["from_code_task_run_id"] == crun and said["with_note"] is True


def test_tonight_a_code_task_retry_of_that_build_is_a_fresh_full_build(env, staffed):  # noqa: F811
    run_id = _start_consented(env, _publish(env, SPEC, f"keep_{uuid.uuid4().hex[:6]}"),
                              RUN_INPUT)["id"]
    crun = _blocked_build(env, run_id, fctx=TONIGHT_FCTX, state=TONIGHT_STATE)
    with lineage.acting(lineage.SYSTEM_ACTING):
        out = cts.retry_code_task(env.engine, get_settings(), _Jobs(), crun, fix_note=NOTE)
    assert out["fresh_build"] is True and out["continues_from_workspace"] == 0
    (child,) = _children(env, crun)
    fctx, said = child["finalize_ctx"], child["spec"]["instructions"]
    assert fctx["fresh_build"] is True
    # None of a repair's shortcuts: no inherited design, failed gates, finished stages or seed.
    for shortcut in ("inherited_design", "prior_failing_gates", "resume_completed_stages",
                     "seed_workspace_from"):
        assert shortcut not in fctx, shortcut
    assert "ALREADY PRESENT" not in said and "FRESH build" in said and NOTE in said
    assert "demo.mjs:58" in said                                   # what stopped it, named


def test_a_step_retry_of_a_blocked_build_that_kept_its_work_builds_onto_it(env, staffed):  # noqa: F811
    run_id = _start_consented(env, _publish(env, SPEC, f"keep_{uuid.uuid4().hex[:6]}"),
                              RUN_INPUT)["id"]
    redacted = DEMO.replace(_LIT, secret_scan.REDACTED)
    kept = {"saved_workspace": {
        "files": [{"path": "helpdesk-ai/server.mjs", "content": "listen(3000)\n"},
                  {"path": "helpdesk-ai/auth/demo.mjs", "content": redacted}],
        "file_count": 2, "secrets_redacted_in": ["helpdesk-ai/auth/demo.mjs"]}}
    crun = _blocked_build(env, run_id, fctx={**TONIGHT_FCTX, **kept}, state=TONIGHT_STATE)
    resumed, _, note = _step_retry(env, run_id)
    assert resumed is not None and note == NOTE
    assert resumed["resumed_from_code_task_run_id"] == crun
    assert resumed["continues_from_workspace"] == 2 and not resumed.get("fresh_build")
    (child,) = _children(env, crun)
    assert child["finalize_ctx"]["seed_workspace_from"] == crun     # seeded from the kept tree
    assert "ALREADY PRESENT" in child["spec"]["instructions"] and NOTE in child["spec"]["instructions"]


def test_a_step_retry_of_a_build_whose_work_is_on_its_branch_repairs_the_branch(
        env, staffed):  # noqa: F811
    run_id = _start_consented(env, _publish(env, SPEC, f"keep_{uuid.uuid4().hex[:6]}"),
                              RUN_INPUT)["id"]
    crun = _blocked_build(env, run_id, fctx={"work_branch": "boss/code/x", "pending_diff": []},
                          state=TONIGHT_STATE)
    resumed, _, _ = _step_retry(env, run_id)
    assert resumed is not None and resumed["resumed_from_code_task_run_id"] == crun
    (child,) = _children(env, crun)
    assert not child["finalize_ctx"].get("fresh_build")
    assert "ALREADY PRESENT" in child["spec"]["instructions"]


@pytest.mark.parametrize("kept", [False, True])
def test_a_retry_of_a_retry_that_kept_nothing_looks_back_through_the_lineage(
        env, staffed, kept):  # noqa: F811
    """crun_3933d79d (the repair of nothing) as a parent: its own tree says nothing about the
    lineage; an ancestor's kept work is still built onto, and a lineage that kept none is fresh."""
    run_id = _start_consented(env, _publish(env, SPEC, f"keep_{uuid.uuid4().hex[:6]}"),
                              RUN_INPUT)["id"]
    root_fctx = {**TONIGHT_FCTX, **({"work_branch": "boss/code/r"} if kept else {})}
    root = _blocked_build(env, run_id, fctx=root_fctx, state=TONIGHT_STATE)
    child = f"crun_{uuid.uuid4().hex[:12]}"
    with env.engine.begin() as c:
        c.execute(text(
            "INSERT INTO code_task_runs (id, project_id, task_id, status, phase, title, spec, "
            "state, finalize_ctx, error) SELECT :id, project_id, NULL, 'cancelled', 'done', title, "
            "spec, '{}'::jsonb, CAST(:fc AS JSONB), 'cancelled' FROM code_task_runs WHERE id=:r"),
            {"id": child, "r": root,
             "fc": json.dumps({"attempt": 2, "root_run_id": root, "parent_run_id": root})})
    with lineage.acting(lineage.SYSTEM_ACTING):
        out = cts.retry_code_task(env.engine, get_settings(), _Jobs(), child, fix_note=NOTE)
    assert bool(out.get("fresh_build")) is (not kept)
