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
from ..schemas import FileChange
from .code_agent import make_code_agent
from .code_task_service import (
    _fail,
    _persist_sandbox_id,
    _release,
    _set_phase,
    load_code_task_run,
)
from .core import create_blocking_decision, emit_event, make_id, record_policy_eval
from .github_app_adapter import GitHubAppAdapter
from .llm_telemetry import clear_llm_context, record_llm_call, set_llm_context
from .pipeline_profile_service import resolve_for_run
from .pipeline_profiles import infer_risk_tier, materialize_route, route_for_step
from .sandbox_provider import LocalSubprocessProvider, MicroVMProvider
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


def _repair_prompt(base_prompt: str, evidence: str, attempt: int) -> str:
    """VAG Phase 3: re-issue the implement prompt with CONCRETE failing evidence appended.
    The bar is frozen — QA's tests are the spec and must not be touched; the frontier review
    panel still judges independently after this. The implementer's only job is to fix the
    implementation so BOSS's authoritative check passes."""
    return (
        f"{base_prompt}\n\n"
        f"--- REPAIR ATTEMPT {attempt} ---\n"
        "Your previous attempt did NOT pass BOSS's authoritative check (BOSS ran the tests "
        "itself; the agent's self-report is not trusted). The tests are FROZEN and correct — "
        "do NOT edit, delete, or weaken any test file. Fix ONLY the implementation. Concrete "
        f"evidence from the failed check:\n{evidence}\n"
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
        "You are a security reviewer. Assess the change for EXPLOITABLE vulnerabilities — things an "
        "attacker could actually use: injection (SQL / command / HTML-XSS), auth or access-control "
        "bypass, unsafe secret/credential handling, unsafe deserialization or parsing, SSRF, path "
        "traversal, or a denial-of-service vector reachable from untrusted input.\n\n"
        f"TASK UNDER REVIEW: {title}\n\n"
        "Read the change and the implementation. Classify each finding by severity "
        "(CRITICAL / HIGH / MEDIUM / LOW / ADVISORY). Do NOT edit any files.\n\n"
        "Calibrate the VERDICT by SEVERITY — not by whether further hardening is conceivable. A "
        "thorough review can always suggest more defense-in-depth; that alone must NOT fail the gate:\n"
        "  - VERDICT: ISSUES  -> ONLY if at least one CRITICAL or HIGH (exploitable) issue remains.\n"
        "  - VERDICT: CLEAN   -> no exploitable issue; any MEDIUM/LOW/ADVISORY items are acceptable "
        "(list them as recommendations — they do not block).\n\n"
        "End your response with EXACTLY one final line: 'VERDICT: CLEAN' or 'VERDICT: ISSUES'."
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


def _guest_engine_fields(settings: Settings, engine_name: str, model) -> dict:
    """Map a routed engine lane onto guest_spec credential fields (hybrid substrate).
    OpenAI-compatible lanes (openrouter/groq/deepseek) run as the guest 'openai' engine
    pointed at the lane's base_url; claude_code rides the OAuth token; echo = no agent
    (clone + run test_commands only, used for the regression stage)."""
    if engine_name == "echo":
        return {"engine": "echo"}
    if engine_name == "claude_code":
        return {"engine": "claude_code",
                "claude_oauth_token": settings.claude_code_oauth_token}
    if engine_name == "openrouter":
        return {"engine": "openai", "openai_api_key": settings.openrouter_api_key,
                "openai_base_url": settings.openrouter_base_url, "model": model}
    if engine_name == "groq":
        return {"engine": "openai", "openai_api_key": settings.groq_api_key,
                "openai_base_url": settings.groq_base_url, "model": model}
    if engine_name == "deepseek":
        return {"engine": "openai", "openai_api_key": settings.deepseek_api_key,
                "openai_base_url": settings.deepseek_base_url, "model": model}
    if engine_name == "azure":
        # Explicit Azure frontier lane in a guest VM. A routed model (azure:<deploy>)
        # overrides the default deployment; no openai_base_url so the guest can't misroute.
        return {"engine": "openai", "azure_endpoint": settings.azure_openai_endpoint,
                "azure_deployment": model or settings.azure_openai_deployment,
                "azure_api_version": settings.azure_openai_api_version,
                "azure_key": settings.azure_openai_key}
    # native/openai/azure -> the platform lane, same env the simple path sends
    return {"engine": "openai", "openai_api_key": settings.openai_api_key,
            "openai_base_url": settings.openai_base_url,
            "model": model or settings.openai_model,
            "azure_endpoint": settings.azure_openai_endpoint,
            "azure_deployment": settings.azure_openai_deployment,
            "azure_api_version": settings.azure_openai_api_version,
            "azure_key": settings.azure_openai_key}


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
    # VAG Phase 0: publish the run's identity so every run_role call below records an
    # llm_calls row with the right attribution (stage is set before each step).
    set_llm_context(db_engine=engine, project_id=project_id, run_id=run_id)
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
    # Feedback-iteration: attempts after the first share ONE branch derived from the root
    # run and clone the prior attempt's code, so the model builds ON prior work instead of
    # regenerating from base. The PR on that branch is reused (see create_pull_request).
    root_run = fctx.get("root_run_id") or run_id
    work_branch = f"boss/code/{root_run}"
    if root_run != run_id and sf.github.branch_exists(work_branch):
        base_branch = work_branch  # prior attempt's code is present in the clone
    app = GitHubAppAdapter(settings)
    token = app.token_for_repo(owner, repo) if app.configured else settings.github_token

    provider = LocalSubprocessProvider(settings)
    # Lane defaults: builder engine for design/tests/implement, adversary engine for
    # review/security/consistency/mutation — the model that wrote the code isn't the one
    # judging it (removes self-review bias). VAG B2 layers per-STEP (engine, model) routing
    # on top: profile model_routing or spec["model_config"] may pin any step to any engine
    # (route_for_step), so e.g. implement runs native/deepseek while review stays claude_code.
    build_engine = (settings.code_agent_engine or "claude_code").lower()
    review_engine = (settings.code_review_engine or "").lower() or build_engine
    # Data-boundary enforcement: restricted/confidential tasks redact PII before any external
    # model call (spec §7.4 / §25). The flag rides on EVERY agent; each run_role honours it.
    data_boundary = spec.get("data_boundary", "internal")
    redact_external = data_boundary in ("restricted", "confidential")
    if redact_external:
        with begin(engine) as c:
            record_policy_eval(c, project_id, "data_boundary", "allow", action="redact_external",
                               reason=f"{data_boundary} task — PII redacted before external model calls",
                               detail={"run_id": run_id, "data_boundary": data_boundary})

    agents: dict[str, object] = {}  # one CodeAgent per engine name, reused across steps
    stage_routes: dict[str, dict] = {}  # step -> {"engine", "model"} resolved (evidence)

    def _agent_for(engine_name: str):
        a = agents.get(engine_name)
        if a is None:
            a = make_code_agent(settings, engine=engine_name)
            if redact_external:
                a.redact = True
            agents[engine_name] = a
        return a

    def _route(step: str):
        """Resolve a step to (agent, model). Precedence: spec model_config > profile
        routing > class defaults; engine falls back to the step's lane default. Call
        from the MAIN thread only (the agent cache is not locked)."""
        r = route_for_step(profile, step, spec.get("model_config"))
        eng, model = materialize_route(
            r, build_engine=build_engine, review_engine=review_engine,
            strong_engine=settings.code_agent_strong_engine,
            low_cost_engine=settings.code_agent_low_cost_engine,
            strong_model=settings.code_agent_strong_model,
            low_cost_model=settings.code_agent_low_cost_model)
        stage_routes[step] = {"engine": eng, "model": model}
        return _agent_for(eng), model

    def _route_names(step: str):
        """Like _route but returns (engine_name, model) without building a host agent —
        the micro-VM substrate maps the route onto guest engine env instead."""
        r = route_for_step(profile, step, spec.get("model_config"))
        eng, model = materialize_route(
            r, build_engine=build_engine, review_engine=review_engine,
            strong_engine=settings.code_agent_strong_engine,
            low_cost_engine=settings.code_agent_low_cost_engine,
            strong_model=settings.code_agent_strong_model,
            low_cost_model=settings.code_agent_low_cost_model)
        stage_routes[step] = {"engine": eng, "model": model}
        return eng, model

    # ---- Hybrid substrate (VAG / sandbox spec §2): the governed pipeline's WORKING
    # stages (write tests / implement / regression / mutation — the ones that EXECUTE
    # generated code) can run each in its own micro-VM, with git as the state carrier:
    # the control plane commits each stage's returned diff to the work branch and the
    # next stage's VM clones it. Judgment (design, review panel, gates, PR) stays here.
    # Constraint honored: the sandbox-service contract is one-shot submit->poll->release
    # (no live exec), and the guest never pushes — only the control plane writes git.
    use_microvm = (settings.governed_sandbox_provider or "local").lower() in (
        "microvm", "firecracker")
    mv = MicroVMProvider(settings) if use_microvm else None
    guest_vm_ids: list[str] = []          # for release-on-success / retain-on-failure
    accumulated: dict[str, str] = {}      # path -> content committed so far (the PR diff)
    guest_base = base_branch              # advances to work_branch once commits exist

    def _guest_stage(step: str, stage_name: str, prompt: str, *, test_cmds: list[str],
                     stage_title: str, engine_override: str = ""):
        """Run one working stage in its own micro-VM and return the CodeAgentResult.
        The guest clones `guest_base` (base branch, or the work branch once stages have
        committed) and never pushes; its diff comes back for the control plane to judge."""
        nonlocal cost
        if engine_override:
            eng_name, model = engine_override, None
        else:
            eng_name, model = _route_names(step)
        guest_spec = {
            "task_id": f"{run_id}-{step}", "owner": owner, "repo": repo,
            "base_branch": guest_base, "work_branch": work_branch,
            "title": stage_title,
            "instructions": (f"{prompt}\n\nWhen your role's work is complete, set done=true "
                             "in your JSON reply."),
            "acceptance_criteria": [], "test_commands": test_cmds,
            "token": token, "timeout": settings.code_agent_timeout_seconds,
            **_guest_engine_fields(settings, eng_name, model),
        }
        set_llm_context(stage=stage_name)
        res = mv.run_code_task(guest_spec, tenant_id=project_id)
        if mv.last_sandbox_task_id:
            guest_vm_ids.append(mv.last_sandbox_task_id)
        cost += int(res.cost_usd_micros or 0)
        record_llm_call(engine_name=f"guest:{eng_name}", usage=res.usage,
                        cost_usd_micros=int(res.cost_usd_micros or 0),
                        turns=int(res.turns or 0))
        return res

    def _commit_stage(files, message: str) -> None:
        """Commit a stage's returned files to the work branch (control-plane-only git;
        requires the task repo to be the deployment's configured target repo)."""
        nonlocal guest_base
        if not files:
            return
        sf.github.ensure_branch(work_branch)
        for fc in files:
            sf.github.upsert_file(work_branch, fc.path, fc.content, message)
            accumulated[fc.path] = fc.content
        guest_base = work_branch

    def _finish_vms(success: bool) -> None:
        """Release stage VMs on success (free capacity); RETAIN them all on failure so
        the sandbox-service keeps the rootfs (~2h) for inspection via the console."""
        if use_microvm and mv and success:
            for sid in guest_vm_ids:
                mv.release(sid)

    sandbox = None
    review_sandbox = None
    cost = 0
    try:
        sandbox = provider.acquire(task_id=run_id, tenant_id=project_id)
        with begin(engine) as c:
            _set_phase(c, run_id, "provisioning")
        sandbox.provision(owner, repo, base_branch=base_branch,
                          work_branch=work_branch, token=token)
        root = sandbox.path

        # Which steps run is set by the risk tier (the cost dial): tier 0 = one agent writes
        # code + tests; tier >=1 = a separate tester; tier >=2 = add a fresh-context code review.
        do_separate_tests = risk_tier >= 1
        do_review = risk_tier >= 2

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
            set_llm_context(stage="standards")
            a, m = _route("standards")
            d1 = a.run_role(_standards_prompt(), root, read_only=True, model=m)
            set_llm_context(stage="spec")
            a, m = _route("spec")
            d2 = a.run_role(_spec_prompt(title, instructions, criteria), root, read_only=True,
                            model=m)
            set_llm_context(stage="plan")
            a, m = _route("plan")
            d3 = a.run_role(_plan_prompt(title, d2.get("summary", "")), root, read_only=True,
                            model=m)
            set_llm_context(stage="review_plan")
            a, m = _route("review_plan")
            d4 = a.run_role(_review_plan_prompt(title, d3.get("summary", "")), root,
                            read_only=True, model=m)
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
            if use_microvm:
                res1 = _guest_stage("write_tests", "write_tests",
                                    _qa_prompt(title, instructions, criteria, test_cmd),
                                    test_cmds=[], stage_title=f"QA: author tests — {title}")
                r1 = {"cost_usd_micros": res1.cost_usd_micros, "summary": res1.summary,
                      "usage": res1.usage, "model": stage_routes["write_tests"]["model"]}
                test_changes = [fc for fc in res1.diff if _is_test_path(fc.path)]
                test_files = [fc.path for fc in test_changes]
                # Commit ONLY test files — anything else the QA role wrote is discarded,
                # which is the micro-VM equivalent of the tests-first separation.
                _commit_stage(test_changes, f"governed: tests-first (qa) — {title[:60]}")
            else:
                set_llm_context(stage="write_tests")
                a, m = _route("write_tests")
                r1 = a.run_role(_qa_prompt(title, instructions, criteria, test_cmd), root, model=m)
                cost += int(r1.get("cost_usd_micros") or 0)
                test_files = [fc.path for fc in sandbox.diff() if _is_test_path(fc.path)]
            if not test_files:
                with begin(engine) as c:
                    _release(sf, c, project_id, reservation_id, "no independent tests authored")
                    return _fail(c, run_id, project_id, "Gate fail: test author produced no test files.")
            if not use_microvm:
                tests_before = _hash_files(root, test_files)
                # Snapshot the frozen tests so a tamper-then-retry starts from clean tests
                # (the repair loop restores them before re-invoking the implementer).
                tests_orig = {p: (root / p).read_text(errors="ignore") for p in test_files
                              if (root / p).exists()}

        # STEP implement — with an optional evidence-fed REPAIR LOOP (VAG Phase 3).
        # The verification bar is FROZEN: QA's tests are never edited, and the frontier
        # review panel below still judges independently. Each retry re-runs only the cheap
        # executable check (the test command) and feeds the implementer the CONCRETE failure
        # (failing test output, a tamper notice, or "no implementation"). Retries can earn a
        # pass but never soften the gate. 0 retries == the pre-Phase-3 single pass.
        with begin(engine) as c:
            _set_phase(c, run_id, "implement")
        base_impl_prompt = (_impl_prompt(title, instructions, test_files, test_cmd)
                            if do_separate_tests
                            else _solo_prompt(title, instructions, criteria, test_cmd))
        max_repair = max(0, min(5, int(
            spec.get("repair_retries", settings.code_repair_max_retries) or 0)))
        repair_attempts = 0
        repair_log: list[dict] = []
        evidence_text = ""
        r2 = {"cost_usd_micros": 0, "summary": "", "usage": {}, "model": None}
        impl_files: list[str] = []
        tests_passed = False
        test_exit_code = None
        for attempt in range(max_repair + 1):
            impl_prompt = (base_impl_prompt if attempt == 0
                           else _repair_prompt(base_impl_prompt, evidence_text, attempt))
            if use_microvm:
                res2 = _guest_stage(
                    "implement", "implement" if do_separate_tests else "solo", impl_prompt,
                    test_cmds=[test_cmd],
                    stage_title=f"SE: implement — {title}" + (f" (repair {attempt})" if attempt else ""))
                r2 = {"cost_usd_micros": res2.cost_usd_micros, "summary": res2.summary,
                      "usage": res2.usage, "model": stage_routes["implement"]["model"]}
                if do_separate_tests:
                    # The guest cloned the committed tests: any test file in ITS diff means
                    # the implementer edited a test — the tamper condition.
                    tests_untouched = not any(_is_test_path(fc.path) for fc in res2.diff)
                    impl_changes = [fc for fc in res2.diff if not _is_test_path(fc.path)]
                    tampered = [fc.path for fc in res2.diff if _is_test_path(fc.path)]
                else:  # tier 0: one agent wrote code + tests, commit both
                    test_files = [fc.path for fc in res2.diff if _is_test_path(fc.path)]
                    impl_changes = list(res2.diff)
                    tampered = []
                # Commit ONLY non-test changes (tamper edits are discarded); the next retry
                # VM re-clones the frozen tests + this attempt's committed impl from the branch.
                _commit_stage(impl_changes, f"governed: implement (SE) — {title[:60]}"
                              + (f" [repair {attempt}]" if attempt else ""))
                impl_files = [p for p in accumulated if not _is_test_path(p)]
                # Authoritative verification: the guest RUNNER (our code, in-VM) executed the
                # test command after the agent stopped — that in-VM result is the verdict.
                with begin(engine) as c:
                    _set_phase(c, run_id, "run_tests")
                tests_passed = bool(res2.tests_passed)
                test_exit_code = res2.tests[0].exit_code if res2.tests else None
                fail_output = (res2.tests[0].stdout if res2.tests else "") or ""
            else:
                set_llm_context(stage="implement" if do_separate_tests else "solo")
                a, m = _route("implement")
                r2 = a.run_role(impl_prompt, root, model=m)
                cost += int(r2.get("cost_usd_micros") or 0)
                if do_separate_tests:
                    tests_untouched = tests_before == _hash_files(root, test_files)
                    tampered = [p for p in test_files
                                if _hash_files(root, [p])[p] != tests_before.get(p)]
                else:  # tier 0: the implementer wrote the tests too
                    test_files = [fc.path for fc in sandbox.diff() if _is_test_path(fc.path)]
                    tampered = []
                impl_files = [fc.path for fc in sandbox.diff() if not _is_test_path(fc.path)]
                # Authoritative verification — BOSS runs the tests itself.
                with begin(engine) as c:
                    _set_phase(c, run_id, "run_tests")
                test_result = sandbox.run(test_cmd)
                tests_passed = bool(test_result.ok)
                test_exit_code = test_result.exit_code
                fail_output = ((test_result.stdout or "") + (test_result.stderr or ""))

            # Executable gate for handing this attempt to the frontier panel: an
            # implementation exists, the tests were untouched, and they pass. Anything else
            # is repairable with concrete feedback (a plain test failure, a tamper, or an
            # empty implementation) — the mutation/review panel is NOT re-run per retry.
            ok = tests_passed and tests_untouched and bool(impl_files)
            repair_log.append({"attempt": attempt, "tests_passed": tests_passed,
                               "tests_untouched": tests_untouched,
                               "impl_files": len(impl_files),
                               "cost_usd_micros": int(r2.get("cost_usd_micros") or 0)})
            if ok or attempt == max_repair:
                break
            repair_attempts += 1
            if not impl_files:
                evidence_text = ("No implementation file was produced. You must create or modify "
                                 "implementation source file(s) (never the test files) so the "
                                 "frozen tests can pass.")
            elif not tests_untouched:
                evidence_text = (
                    f"You modified FROZEN test file(s): {', '.join(tampered) or '(a test file)'}. "
                    "The tests are the spec and must stay byte-for-byte unchanged. Implement "
                    "against them as written; do not touch them.")
                # Local substrate keeps one working tree across retries — restore the tests
                # to their authored content so the next attempt starts from the real spec.
                if not use_microvm:
                    for p, content in tests_orig.items():
                        (root / p).write_text(content, encoding="utf-8")
            else:
                evidence_text = f"$ {test_cmd} (exit {test_exit_code})\n{fail_output[-3000:]}"

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

            # Micro-VM substrate: the working tree the reviewers judge is the WORK BRANCH
            # (the committed guest diffs), materialized as a fresh read-only host clone.
            # Reviewers only READ it — all execution of the generated code stays in VMs.
            review_root = root
            if use_microvm:
                review_sandbox = provider.acquire(task_id=f"{run_id}-review",
                                                  tenant_id=project_id)
                review_sandbox.provision(owner, repo, base_branch=guest_base,
                                         work_branch=work_branch, token=token)
                review_root = review_sandbox.path

            # Pre-resolve routes on the MAIN thread (the agent cache is not locked); the
            # closures only call run_role. Each check runs in a pool thread, and a NEW
            # thread starts with an EMPTY ContextVar context — the run identity set above
            # does not propagate. Each closure re-publishes it (plus its own stage) first.
            review_agent, review_model = _route("review_code")
            security_agent, security_model = _route("security_check")
            consistency_agent, consistency_model = _route("consistency_check")

            def _do_review():
                set_llm_context(db_engine=engine, project_id=project_id, run_id=run_id,
                                stage="review_code")
                rr = review_agent.run_role(_review_prompt(title, criteria), review_root,
                                           read_only=True, model=review_model)
                with lock:
                    panel_cost["v"] += int(rr.get("cost_usd_micros") or 0)
                panel["review"] = (rr, _parse_verdict(rr.get("summary", "")))

            def _do_security():
                set_llm_context(db_engine=engine, project_id=project_id, run_id=run_id,
                                stage="security_check")
                rr = security_agent.run_role(_security_prompt(title), review_root,
                                             read_only=True, model=security_model)
                with lock:
                    panel_cost["v"] += int(rr.get("cost_usd_micros") or 0)
                panel["security"] = (rr, _parse_security(rr.get("summary", "")))

            def _do_full():
                if use_microvm:
                    # Full-suite regression runs in its OWN VM (echo engine = no agent,
                    # just clone the work branch and execute the suite).
                    set_llm_context(db_engine=engine, project_id=project_id, run_id=run_id,
                                    stage="regression")
                    rr = _guest_stage("run_all_tests", "regression", "Run the full test suite.",
                                      test_cmds=["python3 -m unittest discover -v"],
                                      stage_title=f"Regression — {title}",
                                      engine_override="echo")
                    panel["full"] = bool(rr.tests_passed)
                else:
                    panel["full"] = bool(sandbox.run("python3 -m unittest discover -v").ok)

            def _do_consistency():
                set_llm_context(db_engine=engine, project_id=project_id, run_id=run_id,
                                stage="consistency_check")
                rr = consistency_agent.run_role(_consistency_prompt(title, criteria, design),
                                                review_root, read_only=True,
                                                model=consistency_model)
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

            # check tests (mutation) — plant one bug, expect the tests to FAIL. In the
            # micro-VM substrate the mutation runs in ITS OWN VM against the committed
            # work branch and its diff is simply never committed — nothing to restore.
            with begin(engine) as c:
                _set_phase(c, run_id, "check_tests")
            if use_microvm:
                if impl_files:
                    res5 = _guest_stage("check_tests", "check_tests",
                                        _mutate_prompt(impl_files), test_cmds=[test_cmd],
                                        stage_title=f"Mutation probe — {title}")
                    r5 = {"cost_usd_micros": res5.cost_usd_micros,
                          "summary": res5.summary, "model": stage_routes["check_tests"]["model"]}
                    # A real suite FAILS on the planted bug. Only meaningful if the
                    # mutator actually changed an implementation file.
                    mutated = any(not _is_test_path(fc.path) for fc in res5.diff)
                    mutation_caught = (not bool(res5.tests_passed)) if mutated else None
            else:
                # serial: it edits the workdir, then restores it so the PR diff is
                # unchanged. snapshot impl, plant one bug, restore after.
                orig = {p: (root / p).read_text(errors="ignore")
                        for p in impl_files if (root / p).exists()}
                if orig:
                    set_llm_context(stage="check_tests")
                    a, m = _route("check_tests")
                    r5 = a.run_role(_mutate_prompt(list(orig.keys())), root, model=m)
                    cost += int(r5.get("cost_usd_micros") or 0)
                    mut = sandbox.run(test_cmd)
                    mutation_caught = not bool(mut.ok)
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
            # Resolved per-step routing (VAG B2): model=None means the engine's default ran —
            # fall back to what the step actually reported.
            "stage_models": {
                s: r.get("model") or {"write_tests": r1, "implement": r2, "review_code": r3,
                                      "security_check": r4, "check_tests": r5,
                                      "consistency_check": r6}.get(s, {}).get("model")
                for s, r in stage_routes.items()
            },
            "stage_engines": {s: r.get("engine") for s, r in stage_routes.items()},
            "engine": stage_routes.get("implement", {}).get("engine", build_engine),
            "build_engine": build_engine,
            "review_engine": review_engine,
            "model": r2.get("model") or r2.get("usage", {}).get("model"),
            "review_model": (r3.get("model") or r3.get("usage", {}).get("model")
                             or r4.get("model") or r6.get("model")),
            "substrate": "microvm" if use_microvm else "local",
            "sandbox_task_ids": guest_vm_ids,
            "test_author_independent": do_separate_tests, "reviewer_independent": do_review,
            "tests_passed": tests_passed, "adversarial_verdict": verdict,
            "test_files": test_files, "impl_files": impl_files,
            "test_command": test_cmd, "test_exit_code": test_exit_code,
            # VAG Phase 3 repair loop: how many evidence-fed retries the implementer used
            # (0 = passed/failed first try) and the per-attempt outcome trail.
            "repair_attempts": repair_attempts, "repair_max": max_repair,
            "repair_log": repair_log,
            "qa_summary": r1.get("summary", "")[:400],
            "impl_summary": r2.get("summary", "")[:400],
            "review_summary": r3.get("summary", "")[:4000],
            "security_summary": r4.get("summary", "")[:4000],
            "consistency_summary": r6.get("summary", "")[:1500],
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
            failing = [k for k, v in gates.items() if not v]
            _finish_vms(success=False)  # retain the stage VMs for inspection
            # Human escape hatch: preserve the produced diff so an operator can WAIVE the failing
            # gate(s) and open the PR without a re-run. Governance is kept — gates the human does
            # NOT waive still block, and the PR still carries its own merge-gate Decision.
            pending = ([{"path": p, "content": content} for p, content in accumulated.items()]
                       if use_microvm else
                       [{"path": fc.path, "content": fc.content} for fc in sandbox.diff()])
            with begin(engine) as c:
                _release(sf, c, project_id, reservation_id, f"quality gate failed: {qg_id}")
                ctx = {**fctx, "pending_diff": pending, "failing_gates": failing,
                       "quality_gate_id": qg_id, "pr_summary": r2.get("summary", "")[:600],
                       **({"sandbox_task_id": guest_vm_ids[-1]} if guest_vm_ids else {})}
                c.execute(text(
                    "UPDATE code_task_runs SET status='blocked', phase='awaiting_gate_override', "
                    "error=:e, finalize_ctx=CAST(:fc AS JSONB), updated_at=now() WHERE id=:id"),
                    {"e": f"QualityGate FAIL ({qg_id}); failing gates: {failing}. "
                          f"A human can waive these + open the PR via /override.",
                     "fc": json.dumps(ctx), "id": run_id})
            return {"status": "blocked", "done": True, "awaiting_gate_override": True,
                    "quality_gate_id": qg_id, "failing_gates": failing}

        # GATE PASSED — open the PR via the existing software-factory path.
        _finish_vms(success=True)
        if spec.get("open_pr", True) is False:
            # Eval/bake-off runs measure the gate, not the delivery — record the verified
            # outcome without opening a PR on the target repo (VAG spec §8).
            with begin(engine) as c:
                _release(sf, c, project_id, reservation_id,
                         "gate passed; PR suppressed (open_pr=false)")
                c.execute(text("UPDATE code_task_runs SET status='completed', phase='done', "
                               "updated_at=now() WHERE id=:id"), {"id": run_id})
                emit_event(c, project_id, "code_task.completed_no_pr", "boss",
                           "code_task_run", run_id, {"quality_gate_id": qg_id})
            return {"status": "completed", "done": True, "quality_gate_id": qg_id,
                    "pr_suppressed": True}

        with begin(engine) as c:
            _set_phase(c, run_id, "opening_pr")
            proposal = sf.propose_pull_request(
                c, project_id, task_id, reservation_id=reservation_id,
                dedupe_key=f"code:task:{run_id}", title=title,
                branch_name=work_branch,
                summary=(f"Governed pipeline: tests by qa, impl by SE, adversarial review SURVIVED. "
                         f"QualityGate {qg_id} PASS.\n\n{r2.get('summary','')}"),
                files=([FileChange(path=p, content=content) for p, content in accumulated.items()]
                       if use_microvm else sandbox.diff()),
                created_by_job_id=job_id, actual_usd_micros=cost)

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
        _finish_vms(success=False)  # retain stage VMs for root-causing via the console
        with begin(engine) as c:
            _release(sf, c, project_id, reservation_id, f"governed code task error: {exc}")
            if guest_vm_ids:
                _persist_sandbox_id(c, run_id, guest_vm_ids[-1])
            return _fail(c, run_id, project_id, str(exc))
    finally:
        # Request/worker threads are pooled — a stale run_id here would mis-attribute
        # the next job's (or run_research's) llm_calls rows.
        clear_llm_context()
        if sandbox is not None:
            sandbox.teardown()
        if review_sandbox is not None:
            review_sandbox.teardown()
