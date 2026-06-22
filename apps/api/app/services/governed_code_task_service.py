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
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from sqlalchemy import text

from ..config import Settings
from ..db import begin
from .code_agent import make_code_agent
from .code_task_service import _fail, _release, _set_phase, load_code_task_run
from .core import create_blocking_decision, emit_event, make_id
from .github_app_adapter import GitHubAppAdapter
from .pipeline_profile_service import resolve_for_run
from .pipeline_profiles import infer_risk_tier, model_for_step
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


def _solo_prompt(title, instructions, criteria, test_cmd):
    crit = "\n".join(f"- {c}" for c in criteria) or "- (none specified)"
    return (
        "You are a software engineer. Implement the task below AND write tests for it, then make "
        "the tests pass. This is a low-risk task, so one agent does both.\n\n"
        f"TASK: {title}\n\n{instructions}\n\nACCEPTANCE CRITERIA:\n{crit}\n\n"
        "RULES:\n"
        "- Create the implementation file(s) and stdlib `unittest` test file(s).\n"
        f"- Run `{test_cmd}` and iterate until it passes. Do NOT commit.\n"
        "- End with a one-line summary of what you built.\n"
    )


def _review_prompt(title, criteria):
    crit = "\n".join(f"- {c}" for c in criteria) or "- (judge against the task description)"
    return (
        "You are an adversarial code reviewer with FRESH context — you did NOT write this code or "
        "these tests. Try HARD to break the change: read the implementation and the tests and look "
        "for a REAL correctness or edge-case failure (invalid inputs, empty/boundary values, case "
        "sensitivity, error handling, off-by-one).\n\n"
        f"TASK UNDER REVIEW: {title}\n\nACCEPTANCE CRITERIA (the agreed scope):\n{crit}\n\n"
        "Judge ONLY against the stated task and acceptance criteria. A concern that is OUT OF SCOPE "
        "— a feature that was not asked for, or a spec stricter than what was agreed — is NOT a "
        "defect; note it but do not fail the change for it.\n\n"
        "Do NOT edit any files. End your response with EXACTLY one final line:\n"
        "  VERDICT: REFUTED   — ONLY if you found a CONCRETE defect that violates the task/criteria.\n"
        "  VERDICT: SURVIVED  — if the change satisfies the task and criteria. This is the default "
        "when you did not find a real, in-scope defect.\n"
    )


def _parse_verdict(summary: str) -> str:
    up = summary.upper()
    m = re.findall(r"VERDICT:\s*(SURVIVED|REFUTED)", up)
    if not m:  # the model sometimes omits the "VERDICT:" prefix — accept the keyword anywhere
        m = re.findall(r"\b(SURVIVED|REFUTED)\b", up)
    if not m:
        return "not_run"
    return "survived" if m[-1] == "SURVIVED" else "refuted"


def _security_prompt(title):
    return (
        "You are a security reviewer doing a constructive code review. Check that the change "
        "follows security best practices: input validation, access control, safe handling of "
        "secrets and user data, and safe parsing.\n\n"
        f"TASK UNDER REVIEW: {title}\n\n"
        "Read the change and the implementation. Note any security concern you find. Do NOT edit "
        "any files. End your response with EXACTLY one final line, either:\n"
        "  VERDICT: CLEAN\n"
        "  VERDICT: ISSUES\n"
    )


def _parse_security(summary: str) -> str:
    m = re.findall(r"VERDICT:\s*(CLEAN|ISSUES)", summary.upper())
    if not m:
        return "not_run"
    return "clean" if m[-1] == "CLEAN" else "issues"


def _mutate_prompt(impl_files):
    fl = ", ".join(impl_files) or "the implementation file(s)"
    return (
        "Mutation testing is a standard QA practice that verifies a test suite can detect a defect. "
        f"In an implementation file ({fl}), change exactly ONE line so its behaviour differs from "
        "the spec — for example adjust a comparison operator, a constant, or remove a validation "
        "step. Edit ONLY implementation files, never the test files. Make a single change and end "
        "with a one-line summary of what you changed."
    )


# --- Design phase (Wire-Phase 2): standards -> spec -> plan -> review plan, before any code. ---
def _standards_prompt():
    return (
        "You are a senior engineer. Read this repo's existing code and summarise the coding "
        "standards a new change should follow: style, naming, error handling, test conventions, and "
        "file structure. Do NOT edit any files. Output a short bulleted list of the standards."
    )


def _spec_prompt(title, instructions, criteria):
    crit = "\n".join(f"- {c}" for c in criteria) or "- (none given)"
    return (
        "You are a product engineer. Turn the task into a clear, testable specification: a "
        "one-paragraph summary, a change list (added / changed / removed), and a clarified, testable "
        "list of acceptance criteria — resolve any ambiguity explicitly. Do NOT edit any files.\n\n"
        f"TASK: {title}\n\n{instructions}\n\nACCEPTANCE CRITERIA:\n{crit}"
    )


def _plan_prompt(title, spec):
    return (
        "You are a software architect. Given the spec, write a short technical plan: the files to "
        "add/change, the function signatures, and the key edge cases to handle. Do NOT edit any "
        f"files.\n\nTASK: {title}\n\nSPEC:\n{spec}"
    )


def _review_plan_prompt(title, plan):
    return (
        "You are a design reviewer with FRESH context. Critique the plan below for gaps, missing "
        "edge cases, or risky choices, judged against the task. Do NOT edit any files. End with "
        "EXACTLY one final line:\n  PLAN: OK\n  PLAN: REVISE\n\n"
        f"TASK: {title}\n\nPLAN:\n{plan}"
    )


def _consistency_prompt(title, criteria, design):
    crit = "\n".join(f"- {c}" for c in criteria) or "- (the task description)"
    spec = (design or {}).get("spec", "")
    extra = f"\n\nAGREED SPEC:\n{spec}" if spec else ""
    return (
        "You are a reviewer checking internal consistency. Read the implementation and the tests "
        "and confirm they agree with the task and its acceptance criteria: every criterion is "
        "implemented and tested, names and behaviour match the spec, and nothing contradicts. Do "
        "NOT edit any files. End with EXACTLY one final line:\n"
        "  CONSISTENT: YES\n  CONSISTENT: NO\n\n"
        f"TASK: {title}\n\nACCEPTANCE CRITERIA:\n{crit}{extra}"
    )


def _parse_consistency(summary: str) -> str:
    m = re.findall(r"CONSISTENT:\s*(YES|NO)", summary.upper())
    if not m:
        return "not_run"
    return "consistent" if m[-1] == "YES" else "drift"


def _requires_plan_approval(risk_tier: int) -> bool:
    # Top-risk changes pause for a human to sign off the plan BEFORE any code is written.
    return risk_tier >= 3


def _block_for_plan_approval(engine, sf, *, run_id, project_id, task_id, reservation_id,
                             design, risk_tier):
    """Wire-Phase 2b: pause the run after the design phase, persist the design so the resumed run
    reuses it, release budget + sandbox during the wait, and raise a Decision whose approval
    re-enqueues the code.task job (decisions._handle_approved_follow_on → resume_code_task)."""
    with begin(engine) as c:
        c.execute(
            text("UPDATE code_task_runs SET state = COALESCE(state, '{}'::jsonb) || CAST(:s AS JSONB), "
                 "updated_at=now() WHERE id=:id"),
            {"s": json.dumps({"design": design, "awaiting_plan_approval": True}), "id": run_id})
        _release(sf, c, project_id, reservation_id, "Paused for human plan approval (Wire-Phase 2b).")
        dec_id = create_blocking_decision(
            c, project_id, task_id,
            "Approve the technical plan before any code is written?",
            {"action": "resume_code_task", "run_id": run_id, "plan": (design.get("plan") or "")[:4000]},
            {"phase": "plan", "risk_tier": risk_tier},
            "High-risk change: the plan needs human sign-off before implementation.",
            risk_tier=risk_tier)
        c.execute(text("UPDATE code_task_runs SET status='blocked', phase='awaiting_plan_approval', "
                       "decision_id=:d, updated_at=now() WHERE id=:id"),
                  {"d": dec_id, "id": run_id})
    return {"status": "blocked", "done": True, "awaiting_plan_approval": True, "decision_id": dec_id}


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
    # Cost/rigor dial: resolve the profile (per-task override > project default > edition
    # default) and infer the risk tier, clamped to the profile's range. Scales effort to work.
    with begin(engine) as c:
        profile = resolve_for_run(c, project_id, spec.get("profile"), settings)
    risk_tier = infer_risk_tier({**spec, "title": title}, profile)
    # Resume support (Wire-Phase 2b): if this run already passed plan approval, its design was
    # persisted — reuse it and skip the design phase + the gate.
    prior_state = run.get("state") or {}
    resume_design = prior_state.get("design") if prior_state.get("awaiting_plan_approval") else None

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

        # Which steps run is set by the risk tier (the cost dial): tier 0 = one agent writes
        # code + tests; tier >=1 = a separate tester; tier >=2 = add a fresh-context code review.
        do_separate_tests = risk_tier >= 1
        do_review = risk_tier >= 2

        def _model_for(step):
            cls = model_for_step(profile, step)
            picked = (settings.code_agent_strong_model if cls == "strong"
                      else settings.code_agent_low_cost_model)
            return picked or None  # None => the engine's default model

        # DESIGN PHASE (tier >= 2): standards -> spec -> plan -> review plan, BEFORE any code.
        # Spec-driven: the refined spec + plan are fed into the downstream coding steps. (The human
        # approve-plan gate is Wire-Phase 2b; for now review plan records a PLAN: OK/REVISE note.)
        design: dict = {}
        do_design = risk_tier >= 2
        if do_design and resume_design:
            # Resuming after human plan approval — reuse the approved design, skip re-running it.
            design = resume_design
            instructions = (f"{instructions}\n\nREFINED SPEC:\n{design.get('spec', '')}"
                            f"\n\nTECHNICAL PLAN:\n{design.get('plan', '')}")
        elif do_design:
            with begin(engine) as c:
                _set_phase(c, run_id, "design")
            d1 = agent.run_role(_standards_prompt(), root, read_only=True,
                                model=_model_for("standards"))
            d2 = agent.run_role(_spec_prompt(title, instructions, criteria), root, read_only=True,
                                model=_model_for("spec"))
            d3 = agent.run_role(_plan_prompt(title, d2.get("summary", "")), root, read_only=True,
                                model=_model_for("plan"))
            d4 = agent.run_role(_review_plan_prompt(title, d3.get("summary", "")), root,
                                read_only=True, model=_model_for("review_plan"))
            for d in (d1, d2, d3, d4):
                cost += int(d.get("cost_usd_micros") or 0)
            plan_ok = "REVISE" not in d4.get("summary", "").upper()
            design = {"standards": d1.get("summary", ""), "spec": d2.get("summary", ""),
                      "plan": d3.get("summary", ""), "plan_review": d4.get("summary", ""),
                      "plan_ok": plan_ok}
            # feed the refined spec + plan forward so write-tests/implement are spec-driven
            instructions = (f"{instructions}\n\nREFINED SPEC:\n{d2.get('summary', '')}"
                            f"\n\nTECHNICAL PLAN:\n{d3.get('summary', '')}")
            # HUMAN APPROVE-PLAN GATE (Wire-Phase 2b): top risk pauses for sign-off before code.
            if _requires_plan_approval(risk_tier):
                return _block_for_plan_approval(
                    engine, sf, run_id=run_id, project_id=project_id, task_id=task_id,
                    reservation_id=reservation_id, design=design, risk_tier=risk_tier)

        r1 = {"cost_usd_micros": 0, "summary": "", "usage": {}, "model": None}
        test_files: list[str] = []
        tests_untouched = True

        if do_separate_tests:
            # STEP write tests (qa), tests only.
            with begin(engine) as c:
                _set_phase(c, run_id, "write_tests")
            r1 = agent.run_role(_qa_prompt(title, instructions, criteria, test_cmd), root,
                                model=_model_for("write_tests"))
            cost += int(r1.get("cost_usd_micros") or 0)
            test_files = [fc.path for fc in sandbox.diff() if _is_test_path(fc.path)]
            if not test_files:
                with begin(engine) as c:
                    _release(sf, c, project_id, reservation_id, "no independent tests authored")
                    return _fail(c, run_id, project_id, "Gate fail: test author produced no test files.")
            tests_before = _hash_files(root, test_files)

        # STEP implement.
        with begin(engine) as c:
            _set_phase(c, run_id, "implement")
        impl_prompt = (_impl_prompt(title, instructions, test_files, test_cmd) if do_separate_tests
                       else _solo_prompt(title, instructions, criteria, test_cmd))
        r2 = agent.run_role(impl_prompt, root, model=_model_for("implement"))
        cost += int(r2.get("cost_usd_micros") or 0)
        if do_separate_tests:
            tests_untouched = tests_before == _hash_files(root, test_files)
        else:  # tier 0: the implementer wrote the tests too
            test_files = [fc.path for fc in sandbox.diff() if _is_test_path(fc.path)]
        impl_files = [fc.path for fc in sandbox.diff() if not _is_test_path(fc.path)]

        # Authoritative verification — BOSS runs the tests itself.
        with begin(engine) as c:
            _set_phase(c, run_id, "run_tests")
        test_result = sandbox.run(test_cmd)
        tests_passed = bool(test_result.ok)

        # REVIEW PANEL (tier >= 2). The read-only checks — adversarial review, security review, and
        # the full-suite regression run — are independent, so they run CONCURRENTLY, bounded by
        # code_agent_max_concurrency (the lane capacity: extra checks wait for a free slot, and any
        # authorised model impersonates the role when one frees up). check_tests mutates the workdir,
        # so it runs afterwards, serially.
        r3 = {"cost_usd_micros": 0, "summary": "", "model": None}
        r4 = {"cost_usd_micros": 0, "summary": "", "model": None}
        r5 = {"cost_usd_micros": 0, "summary": "", "model": None}
        r6 = {"cost_usd_micros": 0, "summary": "", "model": None}
        verdict = "skipped"
        sec_verdict = "skipped"
        cons_verdict = "skipped"
        mutation_caught = None
        regressions_ok = None
        panel_parallel = False
        if do_review:
            with begin(engine) as c:
                _set_phase(c, run_id, "review_panel")
            panel: dict = {}
            panel_cost = {"v": 0}
            lock = threading.Lock()

            def _do_review():
                rr = agent.run_role(_review_prompt(title, criteria), root, read_only=True,
                                    model=_model_for("review_code"))
                with lock:
                    panel_cost["v"] += int(rr.get("cost_usd_micros") or 0)
                panel["review"] = (rr, _parse_verdict(rr.get("summary", "")))

            def _do_security():
                rr = agent.run_role(_security_prompt(title), root, read_only=True,
                                    model=_model_for("security_check"))
                with lock:
                    panel_cost["v"] += int(rr.get("cost_usd_micros") or 0)
                panel["security"] = (rr, _parse_security(rr.get("summary", "")))

            def _do_full():
                panel["full"] = bool(sandbox.run("python3 -m unittest discover -v").ok)

            def _do_consistency():
                rr = agent.run_role(_consistency_prompt(title, criteria, design), root,
                                    read_only=True, model=_model_for("consistency_check"))
                with lock:
                    panel_cost["v"] += int(rr.get("cost_usd_micros") or 0)
                panel["consistency"] = (rr, _parse_consistency(rr.get("summary", "")))

            checks = [_do_review, _do_security, _do_full, _do_consistency]
            max_workers = min(max(1, settings.code_agent_max_concurrency), len(checks))
            panel_parallel = max_workers > 1
            with ThreadPoolExecutor(max_workers=max_workers) as pool:
                list(pool.map(lambda fn: fn(), checks))
            cost += panel_cost["v"]
            r3, verdict = panel["review"]
            r4, sec_verdict = panel["security"]
            r6, cons_verdict = panel["consistency"]
            regressions_ok = panel["full"]

            # check tests (mutation) — serial: it edits the workdir, then restores it so the PR diff
            # is unchanged. snapshot impl, plant one bug, expect the tests to FAIL, restore.
            with begin(engine) as c:
                _set_phase(c, run_id, "check_tests")
            orig = {p: (root / p).read_text(errors="ignore")
                    for p in impl_files if (root / p).exists()}
            if orig:
                r5 = agent.run_role(_mutate_prompt(list(orig.keys())), root,
                                    model=_model_for("check_tests"))
                cost += int(r5.get("cost_usd_micros") or 0)
                mut = sandbox.run(test_cmd)
                mutation_caught = not bool(mut.ok)  # a real suite FAILS on the planted bug
                for p, content in orig.items():
                    (root / p).write_text(content, encoding="utf-8")

        # FINAL GATE — fail-closed; only the checks that apply at this tier.
        gates = {
            "code_written": bool(impl_files),
            "written_tests_pass": tests_passed,
        }
        if do_separate_tests:
            gates["tests_written_first"] = bool(test_files)
            gates["coder_did_not_edit_tests"] = tests_untouched
        if do_review:
            gates["code_review_passed"] = verdict == "survived"
            gates["security_clean"] = sec_verdict == "clean"
            if mutation_caught is not None:
                gates["tests_strong_enough"] = mutation_caught
            if regressions_ok is not None:
                gates["nothing_broke"] = regressions_ok
            gates["consistent"] = cons_verdict != "drift"  # block only on explicit drift

        steps_run = (["standards", "spec", "plan", "review_plan"] if do_design else []) \
            + (["write_tests"] if do_separate_tests else []) + ["implement"] \
            + (["review_code", "security_check", "check_tests", "run_all_tests", "consistency_check"]
               if do_review else [])
        evidence = {
            "risk_tier": risk_tier,
            "profile": profile["name"],
            "steps_run": steps_run,
            "cost_usd_micros": cost,
            "stage_costs": {
                "write_tests": int(r1.get("cost_usd_micros") or 0),
                "implement": int(r2.get("cost_usd_micros") or 0),
                "review_code": int(r3.get("cost_usd_micros") or 0),
                "security_check": int(r4.get("cost_usd_micros") or 0),
                "check_tests": int(r5.get("cost_usd_micros") or 0),
                "consistency_check": int(r6.get("cost_usd_micros") or 0),
            },
            "security_verdict": sec_verdict,
            "consistency_verdict": cons_verdict,
            "mutation_caught": mutation_caught,
            "regressions_ok": regressions_ok,
            "panel_parallel": panel_parallel,
            "design": design,
            "plan_ok": design.get("plan_ok") if do_design else None,
            "stage_models": {
                "write_tests": (_model_for("write_tests") or r1.get("model")) if do_separate_tests else None,
                "implement": _model_for("implement") or r2.get("model"),
                "review_code": (_model_for("review_code") or r3.get("model")) if do_review else None,
            },
            "engine": getattr(agent, "engine", "unknown"),
            "model": r2.get("model") or r2.get("usage", {}).get("model"),
            "test_author_independent": do_separate_tests, "reviewer_independent": do_review,
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
                       "m": evidence.get("model"), "id": run_id})
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
                files=sandbox.diff(), created_by_job_id=job_id, actual_usd_micros=cost)

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

    except Exception as exc:  # noqa: BLE001 — fail the run gracefully on any agent/model error
        with begin(engine) as c:
            _release(sf, c, project_id, reservation_id, f"governed code task error: {exc}")
            return _fail(c, run_id, project_id, str(exc))
    finally:
        if sandbox is not None:
            sandbox.teardown()
