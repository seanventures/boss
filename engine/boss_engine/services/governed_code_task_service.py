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
from concurrent.futures import ThreadPoolExecutor, wait as _futures_wait
from pathlib import Path

from ..config import Settings
from ..ports import sql
from ..schemas import FileChange
from . import design_prompts, plane_sync, verification_target
from .code_agent import make_code_agent
from .model_rate_limiter import get_rate_limiter, provider_of, is_rate_limit_error
from .code_task_service import (
    _fail,
    _engine_ports,
    _persist_sandbox_id,
    _release,
    _set_phase,
    _transaction,
    load_code_task_run,
)
from .core import create_blocking_decision, emit_event, make_id, record_policy_eval
from .github_app_adapter import GitHubAppAdapter
from .iac_verify import IacVerifyResult, detect_iac_files, is_iac_task, run_iac_verify_lane
from .llm_telemetry import clear_llm_context, record_llm_call, set_llm_context
from .pipeline_profile_service import resolve_for_run
from .pipeline_profiles import (
    apply_budget_pressure,
    infer_risk_tier,
    materialize_route,
    plan_gate_explanation,
    route_for_step,
    sensitive_markers_in,
)
from .sandbox_provider import LocalSubprocessProvider, MicroVMProvider
from .software_factory import SoftwareFactoryService

_DEFAULT_TEST_CMD = "python3 -m unittest discover -v"


def _resolve_use_microvm(settings, spec: dict) -> bool:
    """Substrate selection for the working stages. A per-run `spec.substrate` override wins —
    it's how the retry fallback moves a run OFF a broken micro-VM path (degraded mode) without
    a config change; else the project-level setting applies."""
    spec_substrate = str((spec or {}).get("substrate") or "").lower()
    if spec_substrate in ("local", "subprocess", "local_subprocess"):
        return False
    if spec_substrate in ("microvm", "firecracker"):
        return True
    # COMMITTED CHOICE (evidence-based): the in-container local_subprocess sandbox is the default.
    # It has actually delivered validated working apps (FitDefence); the micro-VM has not, and its
    # guest is not observable from here (no sandbox_task_id / console logs surfaced), so we can't
    # debug it. The in-container failures were caused by CONCURRENCY (>1 build) + api redeploys mid-
    # build, NOT the substrate itself. Operating rule: ONE build at a time, no deploy while a build
    # runs. Opt into the micro-VM only via an explicit governed_sandbox_provider=microvm.
    return (settings.governed_sandbox_provider or "local").lower() in ("microvm", "firecracker")


def _is_test_path(path: str) -> bool:
    # Recognize test files across languages — NOT just Python. Previously a JS/TS/Go/…
    # project's tests (e.g. foo.test.ts, foo.spec.tsx, __tests__/…) were not matched, so
    # a valid test author got "no test files" and the gate hard-failed. Match by dir
    # convention + common per-language suffixes.
    p = path.replace("\\", "/").lower()
    base = p.rsplit("/", 1)[-1]
    if any(seg in f"/{p}" for seg in ("/tests/", "/test/", "/spec/", "/__tests__/", "/e2e/")):
        return True
    if base.startswith("test_") or base.startswith("test."):
        return True
    return base.endswith((
        "_test.py", "_test.go", "_test.rs", "_test.rb", "_spec.rb",
        ".test.js", ".test.jsx", ".test.ts", ".test.tsx", ".test.mjs", ".test.cjs",
        ".spec.js", ".spec.jsx", ".spec.ts", ".spec.tsx", ".spec.mjs", ".spec.cjs",
        "test.java", "tests.java", "test.kt", "test.cs", "tests.cs",
    ))


def _hash_files(root: Path, paths: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for p in paths:
        fp = root / p
        try:
            out[p] = hashlib.sha256(fp.read_bytes()).hexdigest()
        except OSError:
            out[p] = "<missing>"
    return out


def resolve_gate_failure_action(
    *,
    failing: list[str],
    advisory_gate: bool,
    rounds_used: int,
    max_repair_rounds: int,
    open_pr: bool = True,
) -> str:
    """Decide what to do after a non-passing quality gate.

    Returns one of:
      - ``"ship"``   — open the PR (hard pass, or advisory first-pass with surfaced failures)
      - ``"repair"`` — autonomous review-repair retry
      - ``"block"``  — pause for human gate override

    Advisory mode MUST ship: clearing failing gates without short-circuiting used to fall
    through into the block path and permanently stall web-app first-pass builds.
    """
    if not failing or advisory_gate:
        return "ship"
    if rounds_used < max_repair_rounds and open_pr:
        return "repair"
    return "block"


def _record_quality_gate(connection, *, run_id, project_id, task_id, job_id, evidence, gates):
    passed = [k for k, v in gates.items() if v]
    failed = [k for k, v in gates.items() if not v]
    status = "pass" if not failed else "fail"
    qg_id = make_id("qg")
    connection.execute(
        sql("""
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


def _run_iac_lane(settings: Settings, *, files: dict[str, str], workdir) -> IacVerifyResult:
    """IaC verify lane (spec 05 §3.2 / T3) over the produced diff. Local substrate
    (workdir set): verify the existing sandbox checkout in place. Micro-VM substrate
    (workdir=None): materialize the accumulated diff into a host temp dir and verify
    there — the sandbox guest image ships no terraform/tofu toolchain, so this one
    verification runs host-side. TODO(spec 05 Open Decision #7): bundle a provider
    mirror + IaC binaries into the sandbox image so the lane runs guest-side like the
    test stages."""
    if workdir is not None:
        return run_iac_verify_lane(workdir, settings, files=files)
    import tempfile
    with tempfile.TemporaryDirectory(prefix="boss-iac-") as td:
        base = Path(td)
        for path, content in files.items():
            rel = Path(path)
            if rel.is_absolute() or ".." in rel.parts:
                continue  # never write outside the scratch checkout
            fp = base / rel
            fp.parent.mkdir(parents=True, exist_ok=True)
            fp.write_text(content, encoding="utf-8")
        return run_iac_verify_lane(base, settings, files=files)


def _iac_gates(result: IacVerifyResult) -> dict[str, bool]:
    """Per-step iac_* gates + the iac_verify rollup for the fail-closed final gate.
    Skipped steps (the plan-live placeholder) record no gate — they carry a reason in
    the evidence instead."""
    gates = {f"iac_{s.step}": s.ok for s in result.steps if not s.skipped}
    gates["iac_verify"] = result.ok
    return gates


# --------------------------------------------------------------------------- #
#  Stage prompts                                                              #
# --------------------------------------------------------------------------- #
# Infrastructure persona (spec 05 §3.2 / T4): appended to the qa/impl/solo prompts
# when the task is an IaC task (is_iac_task). Guidance only — the stage mechanics
# (tests-first, frozen tests, authoritative re-run) are unchanged; the authoritative
# Terraform verification is the T3 IaC verify lane.
_IAC_GUIDANCE = (
    "\nINFRASTRUCTURE ROLE — this is an Infrastructure-as-Code (Terraform) task. "
    "Additional rules for any .tf/.tfvars/.hcl you write:\n"
    "- Pin provider versions: a `required_providers` block with explicit version "
    "constraints (and `required_version`) in versions.tf.\n"
    "- NO hardcoded credentials or secret literals anywhere. Secrets enter only as "
    "variables marked `sensitive = true`, resolved via SecretRef-style indirection "
    "supplied by the operator at apply time.\n"
    "- Least-privilege IAM: never `*` actions with `*` resources; scope every policy "
    "to the specific actions and ARNs the module needs.\n"
    "- Prefer `for_each` over `count` so resource addresses stay stable.\n"
    "- Declare every variable in variables.tf with a `type` and `description`; expose "
    "outputs in outputs.tf.\n"
    "- Do NOT write a remote-backend block (`backend \"...\" {}`) — backend config is "
    "operator-owned, and validation runs with -backend=false.\n"
    "- Write files terraform fmt-clean (canonical 2-space HCL formatting); the fmt "
    "gate fails otherwise.\n"
)

# QA-stage reframing for pure-Terraform work: there is no Python module-under-test to
# import, so the unittest suite asserts on module STRUCTURE; the executable Terraform
# verification (fmt/validate/lint/security-scan/plan-dry) is the T3 iac_verify lane,
# which runs separately and gates the run.
_IAC_QA_GUIDANCE = (
    "\nIaC TASK NOTE: the deliverable is Terraform, so there is no Python module-under-"
    "test to import. Author stdlib `unittest` tests that assert on the MODULE STRUCTURE "
    "instead: read the .tf/.tfvars files as text (or parse .tf.json with the json "
    "module) and assert that the expected files exist, required variables/outputs are "
    "declared, provider versions are pinned, and no secret literals appear. The "
    "authoritative Terraform verification (fmt, validate, lint, security scan, plan) "
    "is BOSS's separate IaC verify lane — do not shell out to terraform yourself.\n"
)


def _ctx_block(title, instructions, criteria) -> str:
    """Identical task-context preamble for EVERY pipeline stage — byte-identical on
    purpose. The model API's prompt cache is keyed on the request prefix, so stages
    launched back-to-back (within the cache TTL) re-READ this block from cache instead
    of cold-ingesting it in each fresh session. Keep all stage-specific text AFTER it."""
    crit = "\n".join(f"- {c}" for c in criteria or []) or "- (none specified)"
    return (
        "=== TASK CONTEXT (shared verbatim across all pipeline stages) ===\n"
        f"TASK: {title}\n\n{instructions}\n\nACCEPTANCE CRITERIA:\n{crit}\n"
        "=== END TASK CONTEXT ===\n\n"
    )


def _qa_prompt(title, instructions, criteria, test_cmd, *, iac=False):
    return (
        _ctx_block(title, instructions, criteria)
        + "You are a QA engineer. Your ONLY job is to author tests for the task above, BEFORE any "
        "implementation exists.\n\n"
        "RULES:\n"
        "- Create test file(s) ONLY, using the stdlib `unittest` module (e.g. `test_<thing>.py`).\n"
        "- Do NOT create or modify any implementation/source file. Import the module-under-test by "
        "its intended name even though it does not exist yet.\n"
        "- Cover: happy path, the most likely failure, boundary/edge cases, and validation/error paths.\n"
        f"- Do NOT run anything and do NOT commit. (BOSS will run `{test_cmd}`.)\n"
        "- End with a one-line summary of the cases you wrote.\n"
        + (_IAC_GUIDANCE + _IAC_QA_GUIDANCE if iac else "")
    )


def _impl_prompt(title, instructions, test_files, test_cmd, *, criteria=(), iac=False, ui=False):
    tf = ", ".join(test_files) or "(the test files in this repo)"
    return (
        _ctx_block(title, instructions, criteria)
        + "You are a software engineer. QA has ALREADY written the tests; implement the change to "
        "make those existing tests pass.\n\n"
        f"RULES:\n"
        f"- Edit/create implementation files ONLY. You MUST NOT edit, delete, or weaken any test file "
        f"({tf}) — they are the spec. If a test looks wrong, STOP and explain rather than change it.\n"
        f"- Run `{test_cmd}` and iterate until the tests pass.\n"
        "- Do NOT commit. End with a one-line summary of what you changed.\n"
        + (_IAC_GUIDANCE if iac else "")
        + (design_prompts.design_impl_clause() if ui else "")  # APP-2: design axis for UI tasks
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


def _targeted_fix_prompt(findings: str, round_i: int) -> str:
    """In-run convergence (review → implementer conversation): hand the panel's CONCRETE
    findings back to the implementer, which resumes its own warm session and fixes only
    what was flagged — no pipeline re-run, no cold context."""
    return (
        f"--- REVIEW CONVERGENCE ROUND {round_i} ---\n"
        "The independent review panel found the following CONCRETE issues in your "
        "implementation. Fix ONLY these findings — smallest correct change. Do NOT "
        "refactor unrelated code, do NOT touch any test file, and keep every "
        "currently-passing behavior intact. Run the tests to confirm they still pass, "
        "then end with a one-line summary of what you fixed.\n\n"
        f"{findings}\n"
    )


def _repair_resume_prompt(evidence: str, attempt: int) -> str:
    """Resume-mode repair (token optimization): the implementer's session already holds
    the full task context and its own work, so the follow-up sends ONLY the evidence —
    the rest is warm prompt-cache reads instead of a cold re-ingestion."""
    return (
        f"--- REPAIR ATTEMPT {attempt} ---\n"
        "Your implementation did NOT pass BOSS's authoritative check (BOSS ran the tests "
        "itself; your self-report is not trusted). The tests are FROZEN and correct — do "
        "NOT edit, delete, or weaken any test file. Fix ONLY the implementation. Concrete "
        f"evidence from the failed check:\n{evidence}\n"
    )


def _solo_prompt(title, instructions, criteria, test_cmd, *, iac=False):
    return (
        _ctx_block(title, instructions, criteria)
        + "You are a software engineer. Implement the task above AND write tests for it, then make "
        "the tests pass. This is a low-risk task, so one agent does both.\n\n"
        "RULES:\n"
        "- Create the implementation file(s) and stdlib `unittest` test file(s).\n"
        f"- Run `{test_cmd}` and iterate until it passes. Do NOT commit.\n"
        "- End with a one-line summary of what you built.\n"
        + (_IAC_GUIDANCE + _IAC_QA_GUIDANCE if iac else "")
    )


def _render_change_for_review(files, *, max_total: int = 48000, max_per_file: int = 12000) -> str:
    """The exact files this task added/modified, inlined into the review prompt so a reviewer
    JUDGES the change instead of spending dozens of agentic turns (git diff / ls / cat / grep) to
    REDISCOVER what changed. That rediscovery — not model quality or retries — was the dominant
    review latency. Capped so a large diff can't blow the context; the reviewer can still open any
    file in the read-only tree for more depth."""
    items = (sorted(files.items()) if isinstance(files, dict)
             else sorted((fc.path, fc.content) for fc in files))
    parts, total = [], 0
    for path, content in items:
        body = content or ""
        if len(body) > max_per_file:
            body = body[:max_per_file] + f"\n… [truncated {len(content) - max_per_file} chars]"
        block = f"\n=== FILE: {path} ===\n{body}\n"
        if total + len(block) > max_total:
            parts.append(f"\n… [further changed files omitted to fit context, starting at {path}]")
            break
        parts.append(block)
        total += len(block)
    return "".join(parts).strip() or "(no file changes detected)"


def _change_clause(change: str) -> str:
    """The 'here is the change, judge THIS' preamble shared by every reviewer."""
    if not change:
        return ""
    return ("THE CHANGE UNDER REVIEW — the exact files this task added or modified is inlined "
            "below. Judge THIS change directly; you already have it, so do NOT spend turns running "
            "git diff or hunting the repo. Open another file in the read-only tree ONLY if you "
            f"genuinely need more context.\n{change}\n\n")


def _review_prompt(title, criteria, *, instructions="", ui=False, change=""):
    return (
        _ctx_block(title, instructions, criteria)
        + "You are an adversarial code reviewer with FRESH context — you did NOT write this code or "
        "these tests. Try HARD to break the change: read the implementation and the tests and look "
        "for a REAL correctness or edge-case failure (invalid inputs, empty/boundary values, case "
        "sensitivity, error handling, off-by-one).\n\n"
        + _change_clause(change)
        + "Judge ONLY against the stated task and acceptance criteria. A concern that is OUT OF SCOPE "
        "— a feature that was not asked for, or a spec stricter than what was agreed — is NOT a "
        "defect; note it but do not fail the change for it.\n\n"
        "Triage by SEVERITY: a MINOR finding (style, a robustness nicety, a missed edge case that "
        "no acceptance criterion requires) is a RECOMMENDATION — list it, but it must NOT drive the "
        "verdict. Refute ONLY for a defect that materially violates the task or a criterion.\n\n"
        + (design_prompts.design_review_clause() + "\n\n" if ui else "")  # APP-2: incoherence in scope for UI
        + "Do NOT edit any files. End your response with EXACTLY one final line:\n"
        "  VERDICT: REFUTED   — ONLY if you found a CONCRETE defect that violates the task/criteria.\n"
        "  VERDICT: SURVIVED  — if the change satisfies the task and criteria. This is the default "
        "when you did not find a real, in-scope defect.\n"
    )


def _parse_verdict(summary: str) -> str:
    up = (summary or "").upper()
    m = re.findall(r"VERDICT:\s*(SURVIVED|REFUTED)", up)
    if not m:  # the model sometimes omits the "VERDICT:" prefix — accept the keyword anywhere
        m = re.findall(r"\b(SURVIVED|REFUTED)\b", up)
    if not m:
        return "not_run"
    verdict = "survived" if m[-1] == "SURVIVED" else "refuted"
    if verdict == "refuted" and not _refutation_is_substantiated(summary):
        # A refutation MUST cite a CONCRETE defect (the reviewer's own instruction). A bare
        # "VERDICT: REFUTED" with no articulated defect must NOT autonomously fail-close the gate.
        return "survived"
    return verdict


def _refutation_is_substantiated(summary: str) -> bool:
    """True only if the reviewer articulated an actual defect, not just the verdict token."""
    body = re.sub(r"(?im)^\s*(?:#+\s*)?(?:review findings|verdict)\b.*$", "", summary or "")
    body = re.sub(r"(?im)\bVERDICT:\s*(?:SURVIVED|REFUTED)\b", "", body)
    return len(body.strip()) >= 40


def _aggregate_panel(verdicts: list[str], quorum: float) -> tuple[str, float]:
    """Combine an N-member refute panel into one verdict + a confidence score (#9).

    Each element of ``verdicts`` is 'survived' | 'refuted' | 'not_run' (only the first two
    count as a cast vote). ``confidence`` is the fraction of *cast* votes that SURVIVED —
    i.e. how many independent adversarial reviewers could NOT refute the change. The panel
    SURVIVES only if confidence >= ``quorum`` (fail-closed: a tie below quorum, or no votes
    at all, refutes). At panel_size 1 with quorum 0.5 this is exactly the pre-#9 behavior:
    one 'survived' -> (survived, 1.0); one 'refuted' -> (refuted, 0.0).
    """
    cast = [v for v in verdicts if v in ("survived", "refuted")]
    if not cast:
        return "not_run", 0.0
    survived = sum(1 for v in cast if v == "survived")
    confidence = survived / len(cast)
    return ("survived" if confidence >= quorum else "refuted"), confidence


def _security_prompt(title, *, instructions="", criteria=(), change=""):
    return (
        _ctx_block(title, instructions, criteria)
        + "You are a security reviewer. Assess the change for EXPLOITABLE vulnerabilities — things an "
        "attacker could actually use: injection (SQL / command / HTML-XSS), auth or access-control "
        "bypass, unsafe secret/credential handling, unsafe deserialization or parsing, SSRF, path "
        "traversal, or a denial-of-service vector reachable from untrusted input.\n\n"
        + _change_clause(change)
        + "Read the change and the implementation. Classify each finding by severity "
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
    return (
        _ctx_block(title, instructions, criteria)
        + "You are a product engineer. Turn the task above into a clear, testable specification: a "
        "one-paragraph summary, a change list (added / changed / removed), and a clarified, testable "
        "list of acceptance criteria — resolve any ambiguity explicitly. Do NOT edit any files."
    )


def _plan_prompt(title, spec):
    return (
        "You are a software architect. Given the spec, write a short technical plan: the files to "
        "add/change, the function signatures, and the key edge cases to handle. Do NOT edit any "
        f"files.\n\nTASK: {title}\n\nSPEC:\n{spec}"
    )


def _review_plan_prompt(title, plan):
    return (
        "You are a design reviewer with FRESH context. FIRST decide whether this task ASKS FOR "
        "CODE at all. It is NOT a coding task ONLY when its deliverable is inherently a document, "
        "research, or a written specification — e.g. 'write a PRD', 'research the options', "
        "'produce a discovery report' — where the task does not call for creating or modifying "
        "source code.\n"
        "IMPORTANT: a task that asks you to add or change code IS a coding task and goes through "
        "full review — EVEN IF the new code is a standalone/new module, or the repository already "
        "contains unrelated or messy code. Repo tidiness and how 'connected' the new module is are "
        "NOT reasons to reject; they are normal review feedback at most.\n"
        "If and ONLY if the deliverable is not code at all, respond with ONLY this single line and "
        "nothing else:\n"
        "  ROUTING: NOT_A_CODE_TASK — <one-line reason>\n\n"
        "Otherwise, critique the plan below for gaps, missing edge cases, or risky choices, judged "
        "against the task. Do NOT edit any files. End with EXACTLY one final line:\n"
        "  PLAN: OK\n  PLAN: REVISE\n\n"
        f"TASK: {title}\n\nPLAN:\n{plan}"
    )


# Match ONLY the affirmative verdict line "ROUTING: NOT_A_CODE_TASK ..." at line start — NOT a
# prose mention of the token (e.g. "...so it's not the NOT_A_CODE_TASK route"), which a plain
# substring check false-fired on. The token must immediately follow "ROUTING:".
_NOT_A_CODE_TASK_RE = re.compile(r"^\s*ROUTING:\s*NOT_A_CODE_TASK\b", re.IGNORECASE | re.MULTILINE)


def _is_not_a_code_task(plan_review: str) -> bool:
    """The plan reviewer flags a mis-routed task (deliverable is a doc/research/spec, or the repo
    is unrelated) so the pipeline can fail FAST instead of hallucinating a codebase and only
    discovering the mistake at the quality gate ~an hour later. Triggers ONLY on the affirmative
    'ROUTING: NOT_A_CODE_TASK' verdict line — a prose mention of the token does not count."""
    return bool(_NOT_A_CODE_TASK_RE.search(plan_review or ""))


def _consistency_prompt(title, criteria, design, *, instructions="", change=""):
    spec = (design or {}).get("spec", "")
    extra = f"\n\nAGREED SPEC:\n{spec}" if spec else ""
    return (
        _ctx_block(title, instructions, criteria)
        + "You are a reviewer checking internal consistency. Read the implementation and the tests "
        "and confirm they agree with the task and its acceptance criteria: every criterion is "
        "implemented and tested, names and behaviour match the spec, and nothing contradicts. Do "
        "NOT edit any files. End with EXACTLY one final line:\n"
        "  CONSISTENT: YES\n  CONSISTENT: NO\n\n"
        + _change_clause(change)
        + extra
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
    if engine_name == "ollama":
        # Local-model lane. Note for sandboxed guests: "localhost" is the guest,
        # not the host — point OLLAMA_BASE_URL at a host-reachable address
        # (e.g. http://host.docker.internal:11434/v1) when tasks run in a VM.
        return {"engine": "openai", "openai_api_key": settings.ollama_api_key,
                "openai_base_url": settings.ollama_base_url, "model": model}
    if engine_name == "vllm":
        return {"engine": "openai", "openai_api_key": settings.vllm_api_key,
                "openai_base_url": settings.vllm_base_url, "model": model}
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


def _block_for_plan_approval(engine, budget, *, run_id, project_id, task_id, reservation_id,
                             design, risk_tier, title="", sensitive_markers=None):
    """Wire-Phase 2b: pause the run after the design phase, persist the design so the resumed run
    reuses it, release budget + sandbox during the wait, and raise a Decision whose approval
    re-enqueues the code.task job (decisions._handle_approved_follow_on → resume_code_task).

    The decision explains WHY a human is needed (the concrete sensitive surfaces it touches) and
    surfaces the actual plan + automated plan review, so the operator decides with real context —
    not a naked 'approve the plan?' with nothing to look at."""
    markers = sensitive_markers or []
    plan_ok = bool(design.get("plan_ok", True))
    why = plan_gate_explanation(markers, plan_ok=plan_ok)
    if markers:
        question = (
            f"Approve the plan for “{(title or 'this task')[:70]}”? It touches "
            f"{', '.join(markers)} — BOSS won't write code until you sign off on the approach."
        )
    else:
        question = f"Approve the technical plan for “{(title or 'this task')[:70]}” before BOSS writes code?"
    with _transaction(engine) as c:
        c.execute(
            sql("UPDATE code_task_runs SET state = COALESCE(state, '{}'::jsonb) || CAST(:s AS JSONB), "
                 "updated_at=now() WHERE id=:id"),
            {"s": json.dumps({"design": design, "awaiting_plan_approval": True}), "id": run_id})
        _release(
            budget,
            c,
            project_id,
            reservation_id,
            "Paused for human plan approval (Wire-Phase 2b).",
            run_id=run_id,
        )
        dec_id = create_blocking_decision(
            c, project_id, task_id,
            question,
            {
                "action": "resume_code_task",
                "run_id": run_id,
                "why": why,
                "sensitive": markers,
                "plan_ok": plan_ok,
                "plan": (design.get("plan") or "")[:4000],
                "plan_review": (design.get("plan_review") or "")[:2000],
            },
            {"phase": "plan", "risk_tier": risk_tier},
            why,
            risk_tier=risk_tier)
        c.execute(sql("UPDATE code_task_runs SET status='blocked', phase='awaiting_plan_approval', "
                       "decision_id=:d, updated_at=now() WHERE id=:id"),
                  {"d": dec_id, "id": run_id})
    return {"status": "blocked", "done": True, "awaiting_plan_approval": True, "decision_id": dec_id}


# --------------------------------------------------------------------------- #
#  The governed job handler                                                   #
# --------------------------------------------------------------------------- #
def run_governed_code_task(engine, settings: Settings, jobs, run_id: str) -> dict:
    with _transaction(engine) as c:
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
    ports = _engine_ports()
    budget = ports.budget
    sf = SoftwareFactoryService(settings, ports=ports)
    title = run["title"]
    # APP-3 (#93): when the task didn't specify tests, resolve the target from the project type —
    # web/TS gets build/typecheck/lint/e2e (the scaffold's scripts) so the repair loop optimizes a
    # web-native signal; Python still defaults to unittest. Explicit test_commands always win.
    test_cmd = (spec.get("test_commands")
                or verification_target.default_test_commands(spec, title))[0]
    instructions = spec.get("instructions", "")
    criteria = spec.get("acceptance_criteria", [])
    # Infrastructure persona (spec 05 §3.2 / T4): IaC tasks get Terraform-aware role
    # guidance appended to the qa/impl/solo prompts; stage mechanics are unchanged.
    iac_task = is_iac_task(spec, title)
    # APP-2 (#92): UI-bearing tasks get the design axis appended to the impl + review prompts.
    ui_task = design_prompts.is_ui_bearing(spec, title)
    # Cost/rigor dial: resolve the profile (per-task override > project default > edition
    # default) and infer the risk tier, clamped to the profile's range. Scales effort to work.
    with _transaction(engine) as c:
        profile = resolve_for_run(c, project_id, spec.get("profile"), settings)
    risk_tier = infer_risk_tier({**spec, "title": title}, profile)
    # WEB-APP TIER: a scaffolded marketing/web build carries innocent words ("design tokens",
    # "deployability", "no auth/secret handling") that the substring-based sensitive-marker check
    # false-positives on, forcing tier 2 (strict review + mutation gates) and blocking the PR. For
    # web_app builds, honor the caller's intended tier instead (review=False -> 0, review=True ->
    # 2); if none was passed (decomposed slices), cap rigor at tier 1. `npm run build` is the gate.
    if fctx.get("web_app_build"):
        _intended = spec.get("risk_tier")
        risk_tier = (_intended if isinstance(_intended, int) and 0 <= _intended <= 3
                     else min(risk_tier, 1))
    # Resume support (Wire-Phase 2b): if this run already passed plan approval, its design was
    # persisted — reuse it and skip the design phase + the gate.
    prior_state = run.get("state") or {}
    resume_design = prior_state.get("design") if prior_state.get("awaiting_plan_approval") else None

    with _transaction(engine) as c:
        reservation = budget.reserve(
            c, project_id, category="code_task",
            amount_usd_micros=int(settings.code_task_budget_usd_micros),
            reason=f"governed code task: {title[:64]}", expires_in_minutes=45, job_id=job_id,
            risk_tier=spec.get("risk_tier"))
    if reservation.get("status") != "reserved":
        with _transaction(engine) as c:
            return _fail(c, run_id, project_id,
                         f"Budget reservation denied: {reservation.get('status')}", status="blocked",
                         failure_class="needs_human", reason_code="budget_denied")
    reservation_id = reservation["reservation_id"]
    with _transaction(engine) as c:
        c.execute(sql("UPDATE code_task_runs SET reservation_id=:r WHERE id=:id"),
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
        with _transaction(engine) as c:
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

    # BOSS-280: auto-lite mode enforced — at orange/red budget state, class-routed BUILD
    # steps degrade to low_cost (verification lanes and explicit pins never degrade).
    _budget_state = "green"
    try:
        with _transaction(engine) as c:
            _budget_state = str(budget.status_for_project(c, project_id).get(
                "budget_state") or "green")
    except Exception:
        _budget_state = "green"

    def _route(step: str):
        """Resolve a step to (agent, model). Precedence: spec model_config > profile
        routing > class defaults; engine falls back to the step's lane default. Call
        from the MAIN thread only (the agent cache is not locked)."""
        r = route_for_step(profile, step, spec.get("model_config"))
        r, _lite = apply_budget_pressure(r, step, _budget_state)
        eng, model = materialize_route(
            r, build_engine=build_engine, review_engine=review_engine,
            strong_engine=settings.code_agent_strong_engine,
            low_cost_engine=settings.code_agent_low_cost_engine,
            strong_model=settings.code_agent_strong_model,
            low_cost_model=settings.code_agent_low_cost_model)
        stage_routes[step] = {"engine": eng, "model": model,
                              **({"budget_lite": True} if _lite else {})}
        return _agent_for(eng), model

    def _route_escalated(step: str):
        """Force the STRONG (frontier) engine/model for a step — auto-recovery when the
        default route produced no usable output. Call from the MAIN thread only."""
        eng = (settings.code_agent_strong_engine
               or stage_routes.get(step, {}).get("engine") or build_engine)
        model = (settings.code_agent_strong_model
                 or stage_routes.get(step, {}).get("model"))
        stage_routes[step] = {"engine": eng, "model": model, "escalated": True}
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
    # Per-run override wins (set by the retry fallback after a micro-VM infra failure, or
    # explicitly on the task); else the project-level setting applies.
    use_microvm = _resolve_use_microvm(settings, spec)
    from .execution_substrate import normalize_build_substrate
    execution_substrate = normalize_build_substrate(
        "microvm" if use_microvm else "local"
    ).value
    fctx = {**fctx, "execution_substrate": execution_substrate}
    with _transaction(engine) as c:
        c.execute(
            sql("UPDATE code_task_runs SET finalize_ctx=COALESCE(finalize_ctx, '{}'::jsonb) "
                 "|| jsonb_build_object('execution_substrate', CAST(:s AS text)), updated_at=now() "
                 "WHERE id=:id"),
            {"s": execution_substrate, "id": run_id},
        )
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
            "max_turns": int(getattr(settings, "code_agent_guest_max_turns", 120) or 120),
            **_guest_engine_fields(settings, eng_name, model),
        }
        set_llm_context(stage=stage_name)
        # BURST CONTROL (#111 extended to the guest lane): a whole guest build is one long,
        # turn-dense Anthropic consumer. Dispatch it under the model rate limiter's per-provider
        # slot so concurrent guest builds are bounded and a rolling-window burst can't happen.
        # The guest surfaces a 429/usage-limit in its RESULT (not as an exception), so classify
        # res.error too — that feeds the AIMD backoff (halve concurrency + wait) on the next call.
        limiter = get_rate_limiter(settings)
        provider = provider_of(eng_name)
        with limiter.slot(provider):
            res = mv.run_code_task(guest_spec, tenant_id=project_id)
            if is_rate_limit_error(getattr(res, "error", None) or getattr(res, "summary", "")):
                limiter.report_rate_limited(provider)
            else:
                limiter.report_ok(provider)
        if mv.last_sandbox_task_id:
            guest_vm_ids.append(mv.last_sandbox_task_id)
            with _transaction(engine) as c:
                c.execute(
                    sql("UPDATE code_task_runs SET finalize_ctx=COALESCE(finalize_ctx, '{}'::jsonb) "
                         "|| jsonb_build_object('sandbox_task_id', CAST(:sid AS text), "
                         "'sandbox_task_ids', CAST(:ids AS JSONB)), updated_at=now() WHERE id=:id"),
                    {"sid": mv.last_sandbox_task_id, "ids": json.dumps(guest_vm_ids), "id": run_id},
                )
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
        """Hold successful stage VMs for a bounded rootfs-preview window; retain failed
        VMs for inspection. The sandbox service owns TTL reaping in both cases."""
        if use_microvm and mv and success:
            for sid in guest_vm_ids:
                mv.hold(sid, ttl_seconds=7200, reason="governed build available for preview")

    sandbox = None
    review_sandbox = None
    cost = 0
    try:
        sandbox = provider.acquire(task_id=run_id, tenant_id=project_id)
        with _transaction(engine) as c:
            _set_phase(c, run_id, "provisioning")
        sandbox.provision(owner, repo, base_branch=base_branch,
                          work_branch=work_branch, token=token)
        root = sandbox.path

        # Which steps run is set by the risk tier (the cost dial): tier 0 = one agent writes
        # code + tests; tier >=1 = a separate tester; tier >=2 = add a fresh-context code review.
        do_separate_tests = risk_tier >= 1
        do_review = risk_tier >= 2
        # ADVISORY GATE: when True, failing quality checks are SURFACED but do NOT block the PR —
        # the build still ships a PR + preview and the user retries-to-improve. Quality feedback
        # without a dead-end (product decision: always produce a result on the first pass).
        advisory_gate = False

        # WEB-APP BUILDS: a deterministic Vite/React/TS scaffold where `npm run build` (tsc + vite)
        # IS the test. The separate write_tests stage mis-generated Python unittest files for this
        # JS project (polluting the branch), and design adds cost without value here — so those stay
        # off. But we DO run the adversarial/security/consistency review for a quality SIGNAL, made
        # ADVISORY on a first pass (review=False -> tier < 2) so it never blocks the PR; review=True
        # (tier 2) keeps the checks BLOCKING for a hardened pass.
        is_web_app = bool(fctx.get("web_app_build"))
        if is_web_app:
            do_separate_tests = False
            do_review = True
            advisory_gate = risk_tier < 2

        # DESIGN PHASE (tier >= 2): standards -> spec -> plan -> review plan, BEFORE any code.
        # Spec-driven: the refined spec + plan are fed into the downstream coding steps. (The human
        # approve-plan gate is Wire-Phase 2b; for now review plan records a PLAN: OK/REVISE note.)
        design: dict = {}
        do_design = risk_tier >= 2 and not is_web_app
        if do_design and resume_design:
            # Resuming after human plan approval — reuse the approved design, skip re-running it.
            design = resume_design
            instructions = (f"{instructions}\n\nREFINED SPEC:\n{design.get('spec', '')}"
                            f"\n\nTECHNICAL PLAN:\n{design.get('plan', '')}")
        elif do_design:
            with _transaction(engine) as c:
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
            # FAIL FAST: if the design reviewer says this isn't a coding task for this repo (a
            # research/PRD/spec deliverable, or an unrelated repo) it was mis-routed into codegen.
            # Stop NOW — in one design pass — instead of authoring tests + a whole codebase and only
            # catching the mismatch at the quality gate ~an hour later.
            if _is_not_a_code_task(design["plan_review"]):
                _finish_vms(success=False)
                reason_line = next((ln.strip() for ln in design["plan_review"].splitlines()
                                    if "NOT_A_CODE_TASK" in ln.upper()), "")
                with _transaction(engine) as c:
                    _release(budget, c, project_id, reservation_id,
                             "not a coding task — mis-routed into the code pipeline", run_id=run_id)
                    emit_event(c, project_id, "code_task.not_a_code_task", "boss",
                               "code_task_run", run_id, {"reason": reason_line[:300]})
                    return _fail(
                        c, run_id, project_id,
                        "This task's deliverable is a document / research / specification (e.g. a "
                        "PRD or discovery write-up), not a code change in this repository — it was "
                        "routed to the coding pipeline by mistake, so no code was written. "
                        f"Reviewer: {reason_line[:200]}. Produce it as a discovery/PRD document, or "
                        "re-target a repository the change actually applies to.",
                        status="blocked", failure_class="needs_human",
                        reason_code="not_a_code_task")
            # feed the refined spec + plan forward so write-tests/implement are spec-driven
            instructions = (f"{instructions}\n\nREFINED SPEC:\n{d2.get('summary', '')}"
                            f"\n\nTECHNICAL PLAN:\n{d3.get('summary', '')}")
            # HUMAN APPROVE-PLAN GATE (Wire-Phase 2b): top risk pauses for sign-off before code.
            if _requires_plan_approval(risk_tier):
                return _block_for_plan_approval(
                    engine, budget, run_id=run_id, project_id=project_id, task_id=task_id,
                    reservation_id=reservation_id, design=design, risk_tier=risk_tier,
                    title=title,
                    sensitive_markers=sensitive_markers_in({**spec, "title": title}))

        r1 = {"cost_usd_micros": 0, "summary": "", "usage": {}, "model": None}
        test_files: list[str] = []
        tests_untouched = True

        if do_separate_tests:
            # STEP write tests (qa), tests only.
            with _transaction(engine) as c:
                _set_phase(c, run_id, "write_tests")

            def _attempt_write_tests(escalate: bool):
                """One QA/write-tests pass → (r1, test_files, test_changes|None). escalate
                forces the frontier model + a sharper prompt to recover a weak first pass."""
                nonlocal cost
                prompt = _qa_prompt(title, instructions, criteria, test_cmd, iac=iac_task)
                if escalate:
                    prompt = (
                        "Your previous attempt produced NO recognizable test files. Author "
                        "real, runnable tests NOW as actual FILES in this project's test "
                        "convention and directory (e.g. *.test.ts, *_test.py, tests/…), not a "
                        "description or plan.\n\n" + prompt
                    )
                if use_microvm:
                    res = _guest_stage(
                        "write_tests", "write_tests", prompt, test_cmds=[],
                        stage_title=f"QA: author tests — {title}",
                        engine_override=(settings.code_agent_strong_engine if escalate else ""),
                    )
                    r = {"cost_usd_micros": res.cost_usd_micros, "summary": res.summary,
                         "usage": res.usage, "model": stage_routes["write_tests"]["model"]}
                    changes = [fc for fc in res.diff if _is_test_path(fc.path)]
                    return r, [fc.path for fc in changes], changes
                set_llm_context(stage="write_tests")
                a, m = _route_escalated("write_tests") if escalate else _route("write_tests")
                r = a.run_role(prompt, root, model=m)
                cost += int(r.get("cost_usd_micros") or 0)
                return r, [fc.path for fc in sandbox.diff() if _is_test_path(fc.path)], None

            r1, test_files, test_changes = _attempt_write_tests(escalate=False)
            if not test_files:
                # SELF-HEAL: don't hard-fail on a weak/empty first pass — escalate to a
                # frontier model and retry once, visibly, before giving up.
                with _transaction(engine) as c:
                    emit_event(c, project_id, "code_task.step_escalating", "boss",
                               "code_task_run", run_id,
                               {"step": "write_tests", "reason": "no_test_files",
                                "action": "retry_on_frontier"})
                r1, test_files, test_changes = _attempt_write_tests(escalate=True)
                if test_files:
                    with _transaction(engine) as c:
                        emit_event(c, project_id, "code_task.step_recovered", "boss",
                                   "code_task_run", run_id,
                                   {"step": "write_tests", "recovered_by": "frontier_retry"})
            if use_microvm:
                # Commit ONLY test files — anything else the QA role wrote is discarded.
                _commit_stage(test_changes or [], f"governed: tests-first (qa) — {title[:60]}")
            if not test_files:
                with _transaction(engine) as c:
                    emit_event(c, project_id, "code_task.step_exhausted", "boss",
                               "code_task_run", run_id,
                               {"step": "write_tests",
                                "reason": "no_test_files_after_escalation"})
                    _release(budget, c, project_id, reservation_id, "no independent tests authored", run_id=run_id)
                    return _fail(
                        c, run_id, project_id,
                        "Test author produced no test files even after escalating to a frontier "
                        "model — likely an ambiguous task or a repo without a test setup. Review "
                        "the spec/acceptance criteria and retry.",
                        failure_class="needs_human", reason_code="no_test_files")
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
        with _transaction(engine) as c:
            _set_phase(c, run_id, "implement")
        base_impl_prompt = (_impl_prompt(title, instructions, test_files, test_cmd, criteria=criteria,
                                         iac=iac_task, ui=ui_task)
                            if do_separate_tests
                            else _solo_prompt(title, instructions, criteria, test_cmd,
                                              iac=iac_task))
        max_repair = max(0, min(5, int(
            spec.get("repair_retries", settings.code_repair_max_retries) or 0)))
        repair_attempts = 0
        repair_log: list[dict] = []
        evidence_text = ""
        impl_session_id = None  # resume-based repair (cache reuse)
        r2 = {"cost_usd_micros": 0, "summary": "", "usage": {}, "model": None}
        impl_files: list[str] = []
        tests_passed = False
        test_exit_code = None
        # Escalate-recover (E4): the OSS lane gets attempts 0..max_repair; if the frozen
        # tests still don't pass, take ONE final attempt on the FRONTIER engine before handing
        # a failing result to the gate. A cheap implementer shouldn't sink an otherwise-sound
        # task when a stronger model can pass the same frozen tests — reliability over cost.
        escalated_recovered = False
        for attempt in range(max_repair + 2):
            escalate_impl = attempt == max_repair + 1
            if escalate_impl:
                with _transaction(engine) as c:
                    emit_event(c, project_id, "code_task.step_escalating", "boss",
                               "code_task_run", run_id,
                               {"step": "implement", "reason": "tests_not_passing_on_oss",
                                "action": "retry_on_frontier", "attempt": attempt})
            impl_prompt = (base_impl_prompt if attempt == 0
                           else _repair_prompt(base_impl_prompt, evidence_text, attempt))
            if use_microvm:
                res2 = _guest_stage(
                    "implement", "implement" if do_separate_tests else "solo", impl_prompt,
                    test_cmds=[test_cmd],
                    stage_title=f"SE: implement — {title}" + (f" (repair {attempt})" if attempt else ""),
                    engine_override=(settings.code_agent_strong_engine if escalate_impl else ""))
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
                with _transaction(engine) as c:
                    _set_phase(c, run_id, "run_tests")
                tests_passed = bool(res2.tests_passed)
                test_exit_code = res2.tests[0].exit_code if res2.tests else None
                fail_output = (res2.tests[0].stdout if res2.tests else "") or ""
            else:
                set_llm_context(stage="implement" if do_separate_tests else "solo")
                a, m = _route_escalated("implement") if escalate_impl else _route("implement")
                # Resume-based repair: continue the implementer's own session so the
                # repair rides the warm prompt cache (task context + its prior work are
                # cache READS, not fresh ingestion). Escalated attempts stay cold —
                # different model, fresh eyes. Cold full-prompt fallback if resume fails.
                resume_sid = impl_session_id if (attempt and not escalate_impl) else None
                if resume_sid:
                    try:
                        r2 = a.run_role(
                            _repair_resume_prompt(evidence_text, attempt), root,
                            model=m, resume_session_id=resume_sid)
                    except Exception:
                        r2 = a.run_role(impl_prompt, root, model=m)
                else:
                    r2 = a.run_role(impl_prompt, root, model=m)
                impl_session_id = r2.get("session_id") or impl_session_id
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
                with _transaction(engine) as c:
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
            if escalate_impl and ok:
                escalated_recovered = True
                with _transaction(engine) as c:
                    emit_event(c, project_id, "code_task.step_recovered", "boss",
                               "code_task_run", run_id,
                               {"step": "implement", "recovered_by": "frontier_retry",
                                "attempt": attempt})
            if ok or attempt == max_repair + 1:
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
        review_confidence = None  # #9: fraction of the refute panel that could not refute
        panel_size = 1
        review_cost = 0
        mutation_caught = None
        regressions_ok = None
        panel_parallel = False
        if do_review:
            with _transaction(engine) as c:
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

            # REFUTE PANEL (#9): k independent adversarial reviewers, each fresh-context and
            # read-only, each trying to refute the change. Members can be spread across
            # engines (code_review_panel_engines) for model diversity; otherwise they share
            # the review lane (independent samples still de-noise a single critic). Aggregated
            # by quorum into one verdict + a confidence score. panel_size 1 == pre-#9 behavior.
            # Per-run overrides (spec) take precedence over the deployment default — lets a
            # bake-off vary panel size across configs in ONE batch (mirrors repair_retries).
            panel_size = max(1, int(
                spec.get("review_panel_size", getattr(settings, "code_review_panel_size", 1)) or 1))
            review_quorum = float(
                spec.get("review_panel_quorum", getattr(settings, "code_review_panel_quorum", 0.5))
                or 0.5)
            panel_engines = [e.strip() for e in
                             (settings.code_review_panel_engines or "").split(",") if e.strip()]
            review_members = []  # (agent, model) per panel seat
            for i in range(panel_size):
                if panel_engines:
                    review_members.append((_agent_for(panel_engines[i % len(panel_engines)]), None))
                else:
                    review_members.append((review_agent, review_model))
            panel["review_votes"] = [None] * panel_size

            # Hand every reviewer the actual change inline so it JUDGES instead of burning agentic
            # turns rediscovering the diff (the real review-latency root cause). Reviewers still
            # run in the read-only tree and can open any file for deeper context.
            change_block = _render_change_for_review(
                accumulated if use_microvm else {fc.path: fc.content for fc in sandbox.diff()})

            def _make_review(seat, seat_agent, seat_model):
                def _run():
                    set_llm_context(db_engine=engine, project_id=project_id, run_id=run_id,
                                    stage="review_code")
                    rr = seat_agent.run_role(
                        _review_prompt(title, criteria, instructions=instructions, ui=ui_task, change=change_block),
                        review_root, read_only=True, model=seat_model)
                    with lock:
                        panel_cost["v"] += int(rr.get("cost_usd_micros") or 0)
                    panel["review_votes"][seat] = (rr, _parse_verdict(rr.get("summary", "")))
                return _run

            review_checks = [_make_review(i, a, m) for i, (a, m) in enumerate(review_members)]

            def _do_security():
                set_llm_context(db_engine=engine, project_id=project_id, run_id=run_id,
                                stage="security_check")
                rr = security_agent.run_role(_security_prompt(title, instructions=instructions, criteria=criteria, change=change_block),
                                             review_root, read_only=True, model=security_model)
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
                                      test_cmds=[test_cmd],   # APP-3: web-aware regression target
                                      stage_title=f"Regression — {title}",
                                      engine_override="echo")
                    panel["full"] = bool(rr.tests_passed)
                else:
                    panel["full"] = bool(sandbox.run(test_cmd).ok)   # APP-3

            def _do_consistency():
                set_llm_context(db_engine=engine, project_id=project_id, run_id=run_id,
                                stage="consistency_check")
                rr = consistency_agent.run_role(
                    _consistency_prompt(title, criteria, design, instructions=instructions, change=change_block),
                    review_root, read_only=True, model=consistency_model)
                with lock:
                    panel_cost["v"] += int(rr.get("cost_usd_micros") or 0)
                panel["consistency"] = (rr, _parse_consistency(rr.get("summary", "")))

            checks = review_checks + [_do_security, _do_full, _do_consistency]
            # Panel checks are independent + read-only, so run them ALL in parallel rather
            # than in slow batches of code_agent_max_concurrency (which is tuned for the
            # general lane). A low general concurrency was serializing the panel into ~2-3
            # batches of ~7-10min each → ~20min wall-clock. Provider 429s are handled by the
            # adapter's retry, so wider parallelism here is safe.
            max_workers = min(len(checks), max(int(settings.code_agent_max_concurrency), 6))
            panel_parallel = max_workers > 1
            # Safe defaults so a check that times out / errors DEGRADES instead of crashing or
            # auto-passing: security 'not_run' still BLOCKS (security_clean requires 'clean');
            # adversarial/consistency 'not_run' proceed (they block only on refuted/drift).
            panel.setdefault("security", (None, "not_run"))
            panel.setdefault("consistency", (None, "not_run"))
            panel.setdefault("full", True)
            review_errors: list[str] = []

            def _guarded(fn, label):
                def _run():
                    try:
                        fn()
                    except Exception as exc:  # noqa: BLE001 — a failed review must not crash the run
                        with lock:
                            review_errors.append(f"{label}: {exc.__class__.__name__}: {exc}"[:200])
                return _run

            guarded = (
                [_guarded(c, f"review[{i}]") for i, c in enumerate(review_checks)]
                + [_guarded(_do_security, "security"),
                   _guarded(_do_full, "regression"),
                   _guarded(_do_consistency, "consistency")]
            )
            # Hard wall-clock deadline for the WHOLE panel — an engine-agnostic bound so a slow
            # review (e.g. an OpenAI-gateway retry storm the per-call timeout can't catch) can't
            # stall the phase to ~20min. Unfinished checks keep their safe defaults; the pool is
            # shut down without waiting so a leaked call finishes harmlessly in the background.
            review_deadline = int(getattr(settings, "code_agent_review_timeout_seconds", 420) or 420)
            pool = ThreadPoolExecutor(max_workers=max_workers)
            try:
                futs = [pool.submit(g) for g in guarded]
                _done, not_done = _futures_wait(futs, timeout=review_deadline + 60)
                if not_done:
                    with lock:
                        review_errors.append(
                            f"{len(not_done)} review check(s) exceeded {review_deadline}s — degraded")
                    for f in not_done:
                        f.cancel()
            finally:
                pool.shutdown(wait=False)
            if review_errors:
                with _transaction(engine) as c:
                    emit_event(c, project_id, "code_task.review_degraded", "boss",
                               "code_task_run", run_id,
                               {"errors": review_errors[:8], "deadline_s": review_deadline})
            cost += panel_cost["v"]
            # Aggregate the refute panel -> (verdict, confidence). Keep a representative
            # reviewer result for the evidence/summary — prefer a refuting seat so a human
            # sees the concrete defect that sank the gate.
            votes = [v for v in panel["review_votes"] if v]
            verdict, review_confidence = _aggregate_panel([vd for _, vd in votes], review_quorum)
            r3 = next((rr for rr, vd in votes if vd == "refuted"),
                      votes[0][0] if votes else r3)
            review_cost = sum(int((rr or {}).get("cost_usd_micros") or 0) for rr, _ in votes)
            r4, sec_verdict = panel["security"]
            r6, cons_verdict = panel["consistency"]
            # A degraded/failed check leaves a (None, "not_run") slot; downstream builds the
            # evidence dict with r4.get(...)/r6.get(...). Never let a None result crash the run —
            # normalize to an empty result so the check just reads as "not_run" (still gated).
            _empty = {"cost_usd_micros": 0, "summary": "", "model": None}
            r3 = r3 or _empty
            r4 = r4 or _empty
            r6 = r6 or _empty
            regressions_ok = panel["full"]

            # check tests (mutation) — plant one bug, expect the tests to FAIL. In the
            # micro-VM substrate the mutation runs in ITS OWN VM against the committed
            # work branch and its diff is simply never committed — nothing to restore.
            with _transaction(engine) as c:
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

        # IaC VERIFY LANE (spec 05 §3.2 / T3): whenever the produced diff touches
        # Terraform/HCL files, run fmt/validate/lint/security/plan-dry with NO cloud
        # credentials in the environment. Each step lands as its own iac_* gate in the
        # fail-closed final gate below, so a red lane blocks the run exactly like
        # failing tests do.
        changed_files = (dict(accumulated) if use_microvm
                         else {fc.path: fc.content for fc in sandbox.diff()})
        iac_result = None
        if detect_iac_files(changed_files):
            with _transaction(engine) as c:
                _set_phase(c, run_id, "iac_verify")
            iac_result = _run_iac_lane(settings, files=changed_files,
                                       workdir=None if use_microvm else root)

        # FINAL GATE — fail-closed; only the checks that apply at this tier.
        # A REPAIR continuation that finds the prior code already correct makes NO new diff
        # (impl_files empty) — but the work branch still HAS that test-passing code. Treat a no-op
        # iteration on already-good code as "code present" so a CONVERGED run isn't false-failed.
        _is_repair_iteration = root_run != run_id
        gates = {
            "code_written": bool(impl_files) or (_is_repair_iteration and tests_passed),
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
        if iac_result is not None:
            gates.update(_iac_gates(iac_result))

        steps_run = (["standards", "spec", "plan", "review_plan"] if do_design else []) \
            + (["write_tests"] if do_separate_tests else []) + ["implement"] \
            + (["review_code", "security_check", "check_tests", "run_all_tests", "consistency_check"]
               if do_review else []) \
            + (["iac_verify"] if iac_result is not None else [])
        evidence = {
            "risk_tier": risk_tier,
            "profile": profile["name"],
            "steps_run": steps_run,
            "cost_usd_micros": cost,
            "stage_costs": {
                "write_tests": int(r1.get("cost_usd_micros") or 0),
                "implement": int(r2.get("cost_usd_micros") or 0),
                # #9: whole refute panel (all seats), not just the representative seat r3.
                "review_code": review_cost or int(r3.get("cost_usd_micros") or 0),
                "security_check": int(r4.get("cost_usd_micros") or 0),
                "check_tests": int(r5.get("cost_usd_micros") or 0),
                "consistency_check": int(r6.get("cost_usd_micros") or 0),
            },
            # #9 refute-panel verifier hardening: how many independent adversarial reviewers
            # judged, and the confidence (fraction that could NOT refute). None => single-pass
            # legacy path (panel disabled / not run).
            "review_panel_size": panel_size,
            "review_confidence": review_confidence,
            "security_verdict": sec_verdict,
            "consistency_verdict": cons_verdict,
            # IaC verify lane (T3): the full per-step JSON result rides the quality-gate
            # evidence / review packet like the other stage summaries (None = no IaC in
            # the diff, lane not run).
            "iac_verify": iac_result.to_dict() if iac_result is not None else None,
            # T4: whether the task itself was classified IaC (infrastructure persona
            # applied to the qa/impl/solo prompts; routing recorded in finalize_ctx).
            "iac_task": iac_task,
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
            "repair_log": repair_log, "impl_escalated_recovered": escalated_recovered,
            "qa_summary": r1.get("summary", "")[:400],
            "impl_summary": r2.get("summary", "")[:400],
            "review_summary": r3.get("summary", "")[:4000],
            "security_summary": r4.get("summary", "")[:4000],
            "consistency_summary": r6.get("summary", "")[:1500],
        }
        with _transaction(engine) as c:
            c.execute(sql("UPDATE code_task_runs SET state=CAST(:s AS JSONB), "
                           "usage=jsonb_build_object('cost_usd_micros', CAST(:cost AS bigint), "
                           "'model', CAST(:m AS text)), updated_at=now() WHERE id=:id"),
                      {"s": json.dumps(evidence), "cost": cost,
                       "m": evidence.get("model"), "id": run_id})
            qg_id, qg_status = _record_quality_gate(
                c, run_id=run_id, project_id=project_id, task_id=task_id, job_id=job_id,
                evidence=evidence, gates=gates)

        # ADVISORY first pass (web build, review=False): a failing quality check is SURFACED
        # (advisory_failing_gates → shown in the UI) but must NOT block. Record it and FALL THROUGH
        # to the PR-open path below so there's always a result to iterate on. (The earlier bug set
        # failing=[] but stayed inside the `!= pass` block, so it still blocked with an empty list.)
        if qg_status != "pass":
            failing = [k for k, v in gates.items() if not v]

            # IN-RUN CONVERGENCE: when the only failures are JUDGMENT gates, the agents
            # converse instead of blocking or re-running the pipeline — the panel's
            # concrete findings go back to the implementer (resuming its warm session,
            # so the context is cache reads), the frozen tests re-verify, and ONLY the
            # failed judges re-review the delta. Ships when every gate turns green;
            # falls back to the legacy paths below if it can't converge.
            inrun_max = max(0, int(getattr(settings, "code_review_inrun_max_rounds", 2) or 2))
            if (failing and not use_microvm and impl_session_id
                    and set(failing) <= {"code_review_passed", "consistent"}):
              try:  # a convergence failure must DEGRADE to the legacy paths, never kill the run
                for round_i in range(1, inrun_max + 1):
                    finding_bits = []
                    if "code_review_passed" in failing:
                        finding_bits.append(
                            "ADVERSARIAL REVIEW FINDINGS:\n" + (r3.get("summary") or "")[:3000])
                    if "consistent" in failing:
                        finding_bits.append(
                            "CONSISTENCY FINDINGS:\n" + (r6.get("summary") or "")[:2000])
                    with _transaction(engine) as c:
                        emit_event(c, project_id, "code_task.inrun_convergence_round", "boss",
                                   "code_task_run", run_id,
                                   {"round": round_i, "of": inrun_max, "failing_gates": failing})
                        _set_phase(c, run_id, "implement")
                    set_llm_context(db_engine=engine, project_id=project_id, run_id=run_id,
                                    stage="implement")
                    a_fix, m_fix = _route("implement")
                    rfix = a_fix.run_role(
                        _targeted_fix_prompt("\n\n".join(finding_bits), round_i), root,
                        model=m_fix, resume_session_id=impl_session_id)
                    impl_session_id = rfix.get("session_id") or impl_session_id
                    cost += int(rfix.get("cost_usd_micros") or 0)
                    r2 = {**r2, "summary": (rfix.get("summary") or r2.get("summary", ""))}
                    if not sandbox.run(test_cmd).ok:
                        break  # the fix broke the frozen tests — legacy handling below
                    change_block = _render_change_for_review(
                        {fc.path: fc.content for fc in sandbox.diff()})
                    impl_files = [fc.path for fc in sandbox.diff()
                                  if not _is_test_path(fc.path)] or impl_files
                    if "code_review_passed" in failing:
                        with _transaction(engine) as c:
                            _set_phase(c, run_id, "review_code")
                        set_llm_context(db_engine=engine, project_id=project_id,
                                        run_id=run_id, stage="review_code")
                        rr = review_agent.run_role(
                            _review_prompt(title, criteria, instructions=instructions,
                                           ui=ui_task, change=change_block),
                            review_root, read_only=True, model=review_model)
                        cost += int(rr.get("cost_usd_micros") or 0)
                        verdict = _parse_verdict(rr.get("summary", ""))
                        r3 = rr
                        review_confidence = 1.0 if verdict == "survived" else 0.0
                        gates["code_review_passed"] = verdict == "survived"
                    if "consistent" in failing:
                        with _transaction(engine) as c:
                            _set_phase(c, run_id, "consistency_check")
                        set_llm_context(db_engine=engine, project_id=project_id,
                                        run_id=run_id, stage="consistency_check")
                        rc2 = consistency_agent.run_role(
                            _consistency_prompt(title, criteria, design,
                                                instructions=instructions, change=change_block),
                            review_root, read_only=True, model=consistency_model)
                        cost += int(rc2.get("cost_usd_micros") or 0)
                        cons_verdict = _parse_consistency(rc2.get("summary", ""))
                        r6 = rc2
                        gates["consistent"] = cons_verdict != "drift"
                    failing = [k for k, v in gates.items() if not v]
                    if not failing:
                        evidence.update({
                            "adversarial_verdict": verdict,
                            "review_confidence": review_confidence,
                            "consistency_verdict": cons_verdict,
                            "cost_usd_micros": cost,
                            "impl_files": impl_files,
                            "review_summary": (r3.get("summary") or "")[:4000],
                            "consistency_summary": (r6.get("summary") or "")[:1500],
                            "inrun_convergence_rounds": round_i,
                        })
                        with _transaction(engine) as c:
                            c.execute(sql(
                                "UPDATE code_task_runs SET state=CAST(:s AS JSONB), "
                                "usage=jsonb_build_object('cost_usd_micros', "
                                "CAST(:cost AS bigint), 'model', CAST(:m AS text)), "
                                "updated_at=now() WHERE id=:id"),
                                {"s": json.dumps(evidence), "cost": cost,
                                 "m": evidence.get("model"), "id": run_id})
                            qg_id, qg_status = _record_quality_gate(
                                c, run_id=run_id, project_id=project_id, task_id=task_id,
                                job_id=job_id, evidence=evidence, gates=gates)
                            emit_event(c, project_id, "code_task.inrun_convergence_pass",
                                       "boss", "code_task_run", run_id, {"rounds": round_i})
                        break
                    if set(failing) - {"code_review_passed", "consistent"}:
                        break  # a non-judgment gate regressed — legacy handling below
              except Exception as exc:  # noqa: BLE001
                with _transaction(engine) as c:
                    emit_event(c, project_id, "code_task.inrun_convergence_degraded", "boss",
                               "code_task_run", run_id,
                               {"error": f"{type(exc).__name__}: {exc}"[:300]})
                failing = [k for k, v in gates.items() if not v]

            rounds_used = max(0, int(fctx.get("attempt", 1)) - 1)
            # Fallback 2 (not 0): a missing/blank config field must never SILENTLY disable the moat.
            max_repair_rounds = max(0, int(getattr(settings, "code_review_repair_max_rounds", 2) or 2))
            action = resolve_gate_failure_action(
                failing=failing,
                advisory_gate=advisory_gate,
                rounds_used=rounds_used,
                max_repair_rounds=max_repair_rounds,
                open_pr=spec.get("open_pr", True) is not False,
            )
            if action == "ship" and failing and advisory_gate:
                # ADVISORY first pass: surface the failing checks (their verdicts are already
                # recorded on the run, so the UI shows exactly which failed) but PROCEED to open the
                # PR + preview. No block, no dead-end — the user retries-to-improve when they want.
                with _transaction(engine) as c:
                    c.execute(sql(
                        "UPDATE code_task_runs SET finalize_ctx=COALESCE(finalize_ctx,'{}'::jsonb) "
                        "|| jsonb_build_object('advisory_failing_gates', CAST(:f AS JSONB)), "
                        "updated_at=now() WHERE id=:id"),
                        {"f": json.dumps(failing), "id": run_id})
                    emit_event(c, project_id, "code_task.gate_advisory", "boss",
                               "code_task_run", run_id, {"advisory_failing_gates": failing})
                # Fall through to GATE PASSED below — do NOT enter repair/block.
            elif action == "repair":
                # AUTONOMOUS REVIEW-REPAIR (BOSS's moat: agents converge in a loop, not a human).
                # EVERY failure that reaches this quality gate is a code-quality issue a fresh full
                # re-run with the findings fed back can plausibly fix. Bounded by max_repair_rounds.
                _finish_vms(success=False)  # retain this round's VMs for inspection
                with _transaction(engine) as c:
                    _release(
                        budget, c, project_id, reservation_id,
                        f"auto review-repair (round {rounds_used + 1}): {failing}",
                        run_id=run_id,
                    )
                    emit_event(c, project_id, "code_task.review_repair_round", "boss",
                               "code_task_run", run_id,
                               {"round": rounds_used + 1, "of": max_repair_rounds,
                                "failing_gates": failing})
                from .code_task_service import retry_code_task
                nxt = retry_code_task(
                    engine, settings, jobs, run_id,
                    fix_note=("Autonomous review-repair: the quality gate blocked on "
                              f"{', '.join(failing)}. Address EVERY review finding below so ALL "
                              "gates pass, and do NOT regress the checks that already passed. If "
                              "'tests_strong_enough' failed, STRENGTHEN the tests so a one-line "
                              "mutation to the logic is caught."))
                with _transaction(engine) as c:
                    c.execute(sql(
                        "UPDATE code_task_runs SET status='repairing', phase='done', error=:e, "
                        "finalize_ctx=COALESCE(finalize_ctx, '{}'::jsonb) || jsonb_build_object("
                        "  'review_repair_next_run', CAST(:nx AS text), "
                        "  'review_repair_round', CAST(:rd AS int)), updated_at=now() WHERE id=:id"),
                        {"e": f"Auto review-repair round {rounds_used + 1} (gates: {failing}); "
                              f"iterating autonomously in {nxt.get('run_id')}.",
                         "nx": nxt.get("run_id"), "rd": rounds_used + 1, "id": run_id})
                return {"status": "repairing", "done": True, "review_repair": True,
                        "next_run_id": nxt.get("run_id"), "round": rounds_used + 1,
                        "failing_gates": failing}
            elif action == "block":
                _finish_vms(success=False)  # retain the stage VMs for inspection
                # Human escape hatch: preserve the produced diff so an operator can WAIVE the failing
                # gate(s) and open the PR without a re-run. Governance is kept — gates the human does
                # NOT waive still block, and the PR still carries its own merge-gate Decision.
                pending = ([{"path": p, "content": content} for p, content in accumulated.items()]
                           if use_microvm else
                           [{"path": fc.path, "content": fc.content} for fc in sandbox.diff()])
                with _transaction(engine) as c:
                    _release(budget, c, project_id, reservation_id, f"quality gate failed: {qg_id}", run_id=run_id)
                    ctx = {**fctx, "pending_diff": pending, "failing_gates": failing,
                           "quality_gate_id": qg_id, "pr_summary": r2.get("summary", "")[:600],
                           **({"sandbox_task_id": guest_vm_ids[-1]} if guest_vm_ids else {})}
                    c.execute(sql(
                        "UPDATE code_task_runs SET status='blocked', phase='awaiting_gate_override', "
                        "error=:e, finalize_ctx=CAST(:fc AS JSONB), updated_at=now() WHERE id=:id"),
                        {"e": f"QualityGate FAIL ({qg_id}); failing gates: {failing}. "
                              f"A human can waive these + open the PR via /override.",
                         "fc": json.dumps(ctx), "id": run_id})
                return {"status": "blocked", "done": True, "awaiting_gate_override": True,
                        "quality_gate_id": qg_id, "failing_gates": failing}

        # GATE PASSED — open the PR via the existing software-factory path.
        _finish_vms(success=True)
        # ONE APPROVAL PER BUILD, not per slice. A decomposed build breaks a product into ordered
        # slices that all commit to the SAME lineage branch (via _commit_stage). Opening a PR +
        # human approval on every slice is what a customer never wants — 6 tasks, 6 PRs, 6 gates
        # for one website. So only the FINAL slice opens a PR (the whole accumulated site, one
        # approval); earlier slices complete silently on the shared branch and advance the chain.
        _didx, _dtot = fctx.get("decomposition_index"), fctx.get("decomposition_total")
        _nonfinal_slice = (isinstance(_didx, int) and isinstance(_dtot, int)
                           and _dtot > 1 and _didx < _dtot)
        if spec.get("open_pr", True) is False or _nonfinal_slice:
            # Eval/bake-off runs (open_pr=false) measure the gate not the delivery; a non-final
            # decomposition slice defers its PR to the final slice. Either way: record the verified
            # outcome, keep the work on the shared branch, and let the chain advance.
            with _transaction(engine) as c:
                # BOSS-280: settle real LLM spend before releasing. A prior plain release
                # (no run_id) left the reservation inactive so the settle path failed soft
                # and bake-off / non-final-slice spend vanished from the ledger.
                _release(
                    budget, c, project_id, reservation_id,
                    ("gate passed; non-final decomposition slice — PR deferred to final slice"
                     if _nonfinal_slice else "gate passed; PR suppressed (open_pr=false)"),
                    run_id=run_id,
                )
                c.execute(sql("UPDATE code_task_runs SET status='completed', phase='done', "
                               "updated_at=now() WHERE id=:id"), {"id": run_id})
                emit_event(c, project_id, "code_task.completed_no_pr", "boss",
                           "code_task_run", run_id,
                           {"quality_gate_id": qg_id,
                            "reason": "decomposition_slice" if _nonfinal_slice else "open_pr_false",
                            "slice": f"{_didx}/{_dtot}" if _nonfinal_slice else None})
            plane_sync.on_event(engine, settings, run_id, "code_task.completed_no_pr")  # P2
            return {"status": "completed", "done": True, "quality_gate_id": qg_id,
                    "pr_suppressed": True, "decomposition_nonfinal": _nonfinal_slice}

        with _transaction(engine) as c:
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
            with _transaction(engine) as c:
                c.execute(sql("UPDATE code_task_runs SET status='blocked', phase='done', "
                               "decision_id=:d, error=:e, updated_at=now() WHERE id=:id"),
                          {"d": proposal.get("decision_id"), "e": f"PR not created: {status}", "id": run_id})
            return {"status": "blocked", "done": True, "quality_gate_id": qg_id, **proposal}

        with _transaction(engine) as c:
            c.execute(sql("UPDATE code_task_runs SET status='completed', phase='done', "
                           "pull_request_url=:u, pull_request_number=:n, decision_id=:d, updated_at=now() WHERE id=:id"),
                      {"u": proposal.get("pull_request_url"), "n": proposal.get("pull_request_number"),
                       "d": proposal.get("decision_id"), "id": run_id})
            emit_event(c, project_id, "code_task.pr_opened", "boss", "code_task_run", run_id,
                       {"pull_request_url": proposal.get("pull_request_url"),
                        "decision_id": proposal.get("decision_id"), "quality_gate_id": qg_id})
        plane_sync.on_event(engine, settings, run_id, "code_task.pr_opened")  # P2: comment the PR link
        return {"status": "completed", "done": True, "quality_gate_id": qg_id, **proposal}

    except Exception as exc:  # noqa: BLE001 — fail the run gracefully on any agent/model error
        _finish_vms(success=False)  # retain stage VMs for root-causing via the console
        # A blank reason makes every retry blind — always record at least the exception type
        # and the substrate the run was on (that's the first question when root-causing).
        reason = (str(exc).strip() or f"{type(exc).__name__} with no message")
        reason = f"{reason} [substrate={execution_substrate}]"
        from .code_task_service import classify_pipeline_exception
        fclass, rcode = classify_pipeline_exception(reason)
        with _transaction(engine) as c:
            _release(budget, c, project_id, reservation_id, f"governed code task error: {reason}", run_id=run_id)
            if guest_vm_ids:
                _persist_sandbox_id(c, run_id, guest_vm_ids[-1])
            return _fail(c, run_id, project_id, reason,
                         failure_class=fclass, reason_code=rcode)
    finally:
        # Request/worker threads are pooled — a stale run_id here would mis-attribute
        # the next job's (or run_research's) llm_calls rows.
        clear_llm_context()
        if sandbox is not None:
            sandbox.teardown()
        if review_sandbox is not None:
            review_sandbox.teardown()
