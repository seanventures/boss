"""Governed coding pipeline — the runtime that executes a code task through
role-separated stages instead of a single agent.

    reserve budget
      -> provision ONE sandbox (clone the repo)
      -> STAGE: qa authors tests        (isolated agent invocation; tests only, no impl)
      -> STAGE: SE implements           (isolated agent invocation; impl only, tests are frozen)
      -> BOSS re-runs the tests          (authoritative pass/fail)
      -> STAGE: adversarial review       (isolated agent invocation; fresh context, refute-first)
      -> QualityGate                     (FAIL-CLOSED: missing evidence = fail)
      -> propose_pull_request            (only if the gate passes)        <-- reused
      -> teardown

The point is separation of judgment: the test author, the implementer, and the reviewer are
three *different* isolated agent invocations with no shared session state, and the implementer
is forbidden from editing the tests it must satisfy. The agent is resolved via the model-
agnostic CodeAgent seam (make_code_agent: claude_code | native OpenAI/Azure) — the role prompt
is the guidance, the model impersonates the role. See docs/product/GOVERNED_CODING_PIPELINE.md.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from sqlalchemy import text

from ..config import Settings
from ..db import begin
from .code_agent import make_code_agent
from .code_task_service import _fail, _release, _set_phase, load_code_task_run
from .core import emit_event, make_id
from .github_app_adapter import GitHubAppAdapter
from .pipeline_profiles import infer_risk_tier, resolve_profile
from .sandbox import SandboxError
from .sandbox_provider import LocalSubprocessProvider
from .software_factory import SoftwareFactoryService

_DEFAULT_TEST_CMD = "python3 -m unittest discover -v"


def _is_test_path(path: str) -> bool:
    base = path.rsplit("/", 1)[-1]
    return (
        base.startswith("test_")
        or base.endswith("_test.py")
        or "/tests/" in f"/{path}"
        or path.startswith("tests/")
    )


def _hash_files(root: Path, paths: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for p in paths:
        fp = root / p
        try:
            out[p] = hashlib.sha256(fp.read_bytes()).hexdigest()
        except OSError:
            out[p] = "<missing>"
    return out


def _record_quality_gate(connection, *, run_id, project_id, task_id, job_id, evidence, gates):
    passed = [k for k, v in gates.items() if v]
    failed = [k for k, v in gates.items() if not v]
    status = "pass" if not failed else "fail"
    qg_id = make_id("qg")
    connection.execute(
        text("""
            INSERT INTO quality_gates (
                id, project_id, task_id, code_task_run_id, risk_tier,
                test_author_independent, reviewer_independent, tests_passed,
                adversarial_verdict, status, fail_closed, gates_passed, gates_failed,
                evidence, created_by_job_id)
            VALUES (:id, :p, :t, :run, :tier, :tai, :ri, :tp, :av, :st, true,
                    CAST(:gp AS JSONB), CAST(:gf AS JSONB), CAST(:ev AS JSONB), :job)
            ON CONFLICT (project_id, code_task_run_id) DO UPDATE
              SET status=EXCLUDED.status, gates_passed=EXCLUDED.gates_passed,
                  gates_failed=EXCLUDED.gates_failed, evidence=EXCLUDED.evidence
        """),
        {"id": qg_id, "p": project_id, "t": task_id, "run": run_id,
         "tier": int(evidence.get("risk_tier", 2)),
         "tai": bool(evidence.get("test_author_independent")),
         "ri": bool(evidence.get("reviewer_independent")),
         "tp": bool(evidence.get("tests_passed")),
         "av": evidence.get("adversarial_verdict", "not_run"),
         "st": status, "gp": json.dumps(passed), "gf": json.dumps(failed),
         "ev": json.dumps(evidence), "job": job_id},
    )
    emit_event(connection, project_id, "quality_gate.recorded", "boss", "quality_gate", qg_id,
               {"status": status, "gates_passed": passed, "gates_failed": failed,
                "code_task_run_id": run_id})
    return qg_id, status


# --------------------------------------------------------------------------- #
#  Stage prompts                                                              #
# --------------------------------------------------------------------------- #
def _qa_prompt(title, instructions, criteria, test_cmd):
    crit = "\n".join(f"- {c}" for c in criteria) or "- (none specified)"
    return (
        "You are a QA engineer. Your ONLY job is to author tests for the task below, BEFORE any "
        "implementation exists.\n\n"
        f"TASK: {title}\n\n{instructions}\n\nACCEPTANCE CRITERIA:\n{crit}\n\n"
        "RULES:\n"
        "- Create test file(s) ONLY, using the stdlib `unittest` module (e.g. `test_<thing>.py`).\n"
        "- Do NOT create or modify any implementation/source file. Import the module-under-test by "
        "its intended name even though it does not exist yet.\n"
        "- Cover: happy path, the most likely failure, boundary/edge cases, and validation/error paths.\n"
        f"- Do NOT run anything and do NOT commit. (BOSS will run `{test_cmd}`.)\n"
        "- End with a one-line summary of the cases you wrote.\n"
    )


def _impl_prompt(title, instructions, test_files, test_cmd):
    tf = ", ".join(test_files) or "(the test files in this repo)"
    return (
        "You are a software engineer. QA has ALREADY written the tests; implement the change to make "
        "those existing tests pass.\n\n"
        f"TASK: {title}\n\n{instructions}\n\n"
        f"RULES:\n"
        f"- Edit/create implementation files ONLY. You MUST NOT edit, delete, or weaken any test file "
        f"({tf}) — they are the spec. If a test looks wrong, STOP and explain rather than change it.\n"
        f"- Run `{test_cmd}` and iterate until the tests pass.\n"
        "- Do NOT commit. End with a one-line summary of what you changed.\n"
    )


def _review_prompt(title):
    return (
        "You are an adversarial code reviewer with FRESH context — you did NOT write this code or "
        "these tests. Your job is to REFUTE the change.\n\n"
        f"TASK UNDER REVIEW: {title}\n\n"
        "Run `git diff` to see the change. Read the implementation and the tests. Try to find a REAL "
        "correctness or edge-case failure the tests miss (invalid inputs, empty/boundary values, case "
        "sensitivity, error handling, off-by-one).\n\n"
        "Do NOT edit any files. End your response with EXACTLY one final line, either:\n"
        "  VERDICT: SURVIVED\n"
        "  VERDICT: REFUTED\n"
    )


def _parse_verdict(summary: str) -> str:
    m = re.findall(r"VERDICT:\s*(SURVIVED|REFUTED)", summary.upper())
    if not m:
        return "not_run"
    return "survived" if m[-1] == "SURVIVED" else "refuted"


# --------------------------------------------------------------------------- #
#  The governed job handler                                                   #
# --------------------------------------------------------------------------- #
def run_governed_code_task(engine, settings: Settings, jobs, run_id: str) -> dict:
    with begin(engine) as c:
        run = load_code_task_run(c, run_id)
    if run is None:
        return {"status": "not_found", "done": True}
    if run["status"] != "running" or run["pull_request_url"]:
        return {"status": run["status"], "done": True,
                "pull_request_url": run["pull_request_url"], "decision_id": run["decision_id"]}

    project_id = run["project_id"]
    task_id = run["task_id"]
    spec = run["spec"]
    fctx = run["finalize_ctx"] or {}
    job_id = fctx.get("created_by_job_id")
    sf = SoftwareFactoryService(settings)
    test_cmd = (spec.get("test_commands") or [_DEFAULT_TEST_CMD])[0]
    title = run["title"]
    instructions = spec.get("instructions", "")
    criteria = spec.get("acceptance_criteria", [])
    # Cost/rigor dial: pick the profile (per-task override > project setting) and infer the
    # risk tier, clamped to the profile's range. This scales effort to the work.
    profile = resolve_profile(spec.get("profile") or settings.pipeline_profile)
    risk_tier = infer_risk_tier({**spec, "title": title}, profile)

    with begin(engine) as c:
        reservation = sf.budget.reserve(
            c, project_id, category="code_task",
            amount_usd_micros=int(settings.code_task_budget_usd_micros),
            reason=f"governed code task: {title[:64]}", expires_in_minutes=45, job_id=job_id)
    if reservation.get("status") != "reserved":
        with begin(engine) as c:
            return _fail(c, run_id, project_id,
                         f"Budget reservation denied: {reservation.get('status')}", status="blocked")
    reservation_id = reservation["reservation_id"]
    with begin(engine) as c:
        c.execute(text("UPDATE code_task_runs SET reservation_id=:r WHERE id=:id"),
                  {"r": reservation_id, "id": run_id})

    owner = spec.get("owner") or settings.target_github_owner
    repo = spec.get("repo") or settings.target_github_repo
    base_branch = spec.get("base_branch") or "main"
    app = GitHubAppAdapter(settings)
    token = app.token_for_repo(owner, repo) if app.configured else settings.github_token

    provider = LocalSubprocessProvider(settings)
    agent = make_code_agent(settings)  # model-agnostic: claude_code | native (OpenAI/Azure)
    sandbox = None
    cost = 0
    try:
        sandbox = provider.acquire(task_id=run_id, tenant_id=project_id)
        with begin(engine) as c:
            _set_phase(c, run_id, "provisioning")
        sandbox.provision(owner, repo, base_branch=base_branch,
                          work_branch=f"boss/code/{run_id}", token=token)
        root = sandbox.path

        # STAGE 1 — test authoring (qa), isolated context, tests only.
        with begin(engine) as c:
            _set_phase(c, run_id, "write_tests")
        r1 = agent.run_role(_qa_prompt(title, instructions, criteria, test_cmd), root)
        cost += int(r1.get("cost_usd_micros") or 0)
        test_files = [fc.path for fc in sandbox.diff() if _is_test_path(fc.path)]
        if not test_files:
            with begin(engine) as c:
                _release(sf, c, project_id, reservation_id, "no independent tests authored")
                return _fail(c, run_id, project_id, "Gate fail: test author produced no test files.")
        tests_before = _hash_files(root, test_files)

        # STAGE 2 — implementation (software_engineer), tests frozen.
        with begin(engine) as c:
            _set_phase(c, run_id, "implement")
        r2 = agent.run_role(_impl_prompt(title, instructions, test_files, test_cmd), root)
        cost += int(r2.get("cost_usd_micros") or 0)
        tests_after = _hash_files(root, test_files)
        tests_untouched = tests_before == tests_after
        impl_files = [fc.path for fc in sandbox.diff() if not _is_test_path(fc.path)]

        # Authoritative verification — BOSS runs the tests itself.
        with begin(engine) as c:
            _set_phase(c, run_id, "run_tests")
        test_result = sandbox.run(test_cmd)
        tests_passed = bool(test_result.ok)

        # STAGE 3 — adversarial review (adversarial_reviewer), fresh context, read-only.
        with begin(engine) as c:
            _set_phase(c, run_id, "review_code")
        r3 = agent.run_role(_review_prompt(title), root, read_only=True)
        cost += int(r3.get("cost_usd_micros") or 0)
        verdict = _parse_verdict(r3.get("summary", ""))

        # QUALITY GATE — fail-closed.
        gates = {
            "tests_written_first": bool(test_files),
            "coder_did_not_edit_tests": tests_untouched,
            "code_written": bool(impl_files),
            "written_tests_pass": tests_passed,
            "code_review_passed": verdict == "survived",
        }
        evidence = {
            "risk_tier": risk_tier,
            "profile": profile["name"],
            "cost_usd_micros": cost,
            "stage_costs": {
                "write_tests": int(r1.get("cost_usd_micros") or 0),
                "implement": int(r2.get("cost_usd_micros") or 0),
                "review_code": int(r3.get("cost_usd_micros") or 0),
            },
            "engine": getattr(agent, "engine", "unknown"),
            "model": r2.get("usage", {}).get("model"),
            "test_author_independent": True, "reviewer_independent": True,
            "tests_passed": tests_passed, "adversarial_verdict": verdict,
            "test_files": test_files, "impl_files": impl_files,
            "test_command": test_cmd, "test_exit_code": test_result.exit_code,
            "qa_summary": r1.get("summary", "")[:400],
            "impl_summary": r2.get("summary", "")[:400],
            "review_summary": r3.get("summary", "")[:800],
        }
        with begin(engine) as c:
            c.execute(text("UPDATE code_task_runs SET state=CAST(:s AS JSONB), "
                           "usage=jsonb_build_object('cost_usd_micros', CAST(:cost AS bigint), "
                           "'model', CAST(:m AS text)), updated_at=now() WHERE id=:id"),
                      {"s": json.dumps(evidence), "cost": cost,
                       "m": r2.get("usage", {}).get("model"), "id": run_id})
            qg_id, qg_status = _record_quality_gate(
                c, run_id=run_id, project_id=project_id, task_id=task_id, job_id=job_id,
                evidence=evidence, gates=gates)

        if qg_status != "pass":
            with begin(engine) as c:
                _release(sf, c, project_id, reservation_id, f"quality gate failed: {qg_id}")
                return _fail(c, run_id, project_id,
                             f"QualityGate FAIL ({qg_id}); failing gates: "
                             f"{[k for k,v in gates.items() if not v]}")

        # GATE PASSED — open the PR via the existing software-factory path.
        with begin(engine) as c:
            _set_phase(c, run_id, "opening_pr")
            proposal = sf.propose_pull_request(
                c, project_id, task_id, reservation_id=reservation_id,
                dedupe_key=f"code:task:{run_id}", title=title,
                branch_name=f"boss/code/{run_id}",
                summary=(f"Governed pipeline: tests by qa, impl by SE, adversarial review SURVIVED. "
                         f"QualityGate {qg_id} PASS.\n\n{r2.get('summary','')}"),
                files=sandbox.diff(), created_by_job_id=job_id)

        status = proposal.get("status")
        if status not in ("created", "succeeded"):
            with begin(engine) as c:
                c.execute(text("UPDATE code_task_runs SET status='blocked', phase='done', "
                               "decision_id=:d, error=:e, updated_at=now() WHERE id=:id"),
                          {"d": proposal.get("decision_id"), "e": f"PR not created: {status}", "id": run_id})
            return {"status": "blocked", "done": True, "quality_gate_id": qg_id, **proposal}

        with begin(engine) as c:
            c.execute(text("UPDATE code_task_runs SET status='completed', phase='done', "
                           "pull_request_url=:u, pull_request_number=:n, decision_id=:d, updated_at=now() WHERE id=:id"),
                      {"u": proposal.get("pull_request_url"), "n": proposal.get("pull_request_number"),
                       "d": proposal.get("decision_id"), "id": run_id})
            emit_event(c, project_id, "code_task.pr_opened", "boss", "code_task_run", run_id,
                       {"pull_request_url": proposal.get("pull_request_url"),
                        "decision_id": proposal.get("decision_id"), "quality_gate_id": qg_id})
        return {"status": "completed", "done": True, "quality_gate_id": qg_id, **proposal}

    except (SandboxError, RuntimeError) as exc:
        with begin(engine) as c:
            _release(sf, c, project_id, reservation_id, f"governed code task error: {exc}")
            return _fail(c, run_id, project_id, str(exc))
    finally:
        if sandbox is not None:
            sandbox.teardown()
