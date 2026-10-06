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

import contextvars
import logging
import os
import time

import hashlib
import json
import re
import threading
from concurrent.futures import ThreadPoolExecutor, wait as _futures_wait
from pathlib import Path
from typing import Any, Optional

from ..config import Settings
from ..ports import sql
from ..schemas import FileChange
from . import agent_acts, design_prompts, plane_sync, review_comments, verification_target
from .bakeoff import (
    fixture_for_run as bakeoff_fixture_for_run,
    stage_fixture as stage_bakeoff_fixture,
)
from .code_agent import make_code_agent
from .model_rate_limiter import get_rate_limiter, provider_of, is_rate_limit_error
from .code_task_service import (
    _fail,
    refused_reservation,
    _engine_ports,
    _persist_sandbox_id,
    _release,
    _transaction,
    load_code_task_run,
)
from .core import create_blocking_decision, emit_event, make_id, record_policy_eval
from .github_app_adapter import GitHubAppAdapter
from .iac_verify import IacVerifyResult, detect_iac_files, is_iac_task, run_iac_verify_lane
from .llm_telemetry import clear_llm_context, record_llm_call, set_llm_context

logger = logging.getLogger(__name__)
from .pipeline_profile_service import resolve_for_run
from .project_settings import effective_review_lanes, effective_review_rounds
from .factory_quality_loop import (
    bind_test_outcomes,
    bind_verify_log_to_jtbd,
    collect_workspace_evidence_from_root,
    merge_official_jtbd,
    quality_improve_prompt,
    quality_pass_record,
    should_run_quality_pass,
)
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


# A test declaration across the languages BOSS builds in: python (def test_/async def test_),
# JS/TS (it(/test(/describe(), go (func TestX), java/kotlin (@Test).
_TEST_DEF_RE = re.compile(
    r"(?m)^\s*(?:async\s+)?def\s+test\w*\s*\(|"
    r"^\s*(?:it|test|describe)\s*[.(]|"
    r"^\s*func\s+Test\w*\s*\(|"
    r"^\s*@Test\b"
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


# Markers that a check did not COMPLETE. A check that never finished produced no evidence
# about the code, so "did not finish" must never be read as "the code is bad".
# EXACT phrases the ENGINE emits when a check could not complete. These must be engine
# strings, never ordinary words: an earlier version matched loose tokens ("timeout",
# "exceeded", "degraded"), so a reviewer writing "the fetch call has no timeout" had its
# REAL finding classified as a harness fault and was never repaired — the precise defect
# class this function exists to eliminate, reproduced inside it.
_INCONCLUSIVE_PHRASES = (
    "ran out of turns before finishing",
    "partial attempt; resume to continue",
    "review check(s) exceeded",
    "could not acquire a",           # RateLimitAcquireTimeout
)


def check_is_inconclusive(summary: str, verdict: Optional[str] = None) -> bool:
    """True when a check produced NO usable judgement (turn cap, timeout, crash, skip).

    Evidence of a real judgement WINS: if the check emitted structured findings, or a
    definite verdict, it ran — whatever prose it used. Only then do we look for the
    engine's own "did not finish" markers.
    """
    text = (summary or "").strip()
    if not text:
        return True
    # Structured evidence => the check genuinely ran.
    if _parse_findings(text):
        return False
    if (verdict or "").strip().lower() in {"survived", "refuted", "clean", "issues",
                                           "drift", "consistent"}:
        return False
    if verdict is not None and str(verdict).strip().lower() in {"not_run", "", "unknown"}:
        return True
    low = text.lower()
    return any(phrase in low for phrase in _INCONCLUSIVE_PHRASES)


def classify_gate_faults(
    *, failing: list[str], evidence: dict
) -> dict[str, str]:
    """Classify each failing gate as WORK (the change is wrong) or HARNESS (the check
    didn't finish). Only WORK is repairable by re-running the coding agent."""
    faults: dict[str, str] = {}
    for gate in failing:
        if gate == "code_review_passed":
            inconclusive = check_is_inconclusive(
                evidence.get("review_summary", ""),
                evidence.get("adversarial_verdict"),
            )
        elif gate == "consistent":
            inconclusive = check_is_inconclusive(
                evidence.get("consistency_summary", ""),
                evidence.get("consistency_verdict"),
            )
        elif gate in {"security_clean", "no_security_findings"}:
            # The pipeline records `security_clean` (see gates["security_clean"] =
            # sec_verdict == "clean"). `no_security_findings` is kept so a stored
            # older name still classifies instead of falling through to WORK.
            inconclusive = check_is_inconclusive(
                evidence.get("security_summary", ""),
                evidence.get("security_verdict"),
            )
        elif gate == "app_serves":
            # The app did not answer is WORK. The check could not even start it (no tree to
            # copy, launcher failed) is BOSS's fault, and must not send the coder to fix it.
            inconclusive = bool((evidence.get("app_serves") or {}).get("harness"))
        else:
            # Test gates are authoritative: BOSS re-runs the suite itself, so a red suite is
            # real evidence about the code, not a check that failed to finish.
            inconclusive = False
        faults[gate] = "HARNESS" if inconclusive else "WORK"
    return faults


def take_pending_guidance(engine, run_id: str, stage: Optional[str] = None) -> list[dict]:
    """Pop operator guidance queued for a RUNNING task, so the next stage can honour it.

    Without this a run is fire-and-wait: the operator watches it head somewhere wrong for
    twenty minutes and the only lever is cancel-and-restart, which throws away everything
    done so far. Claude Code lets you redirect at turn 3; BOSS could not redirect at all.

    Steering is applied at STAGE BOUNDARIES, not mid-turn — the agent is a subprocess we
    cannot interrupt safely, and a stage boundary is a consistent point where the guidance
    can be folded into the next prompt. Guidance is consumed exactly once (popped under the
    same transaction that reads it) so it cannot be applied twice.
    """
    with _transaction(engine) as c:
        row = c.execute(sql("SELECT finalize_ctx FROM code_task_runs WHERE id=:id"),
                        {"id": run_id}).mappings().first()
        if not row:
            return []
        fctx = row["finalize_ctx"] or {}
        if isinstance(fctx, str):
            try:
                fctx = json.loads(fctx)
            except Exception:  # noqa: BLE001
                return []
        pending = list(fctx.get("pending_guidance") or [])
        if not pending:
            return []
        if stage:
            # THE STEER IS ACKNOWLEDGED (factory spec §6.4): each item says when it was read
            # and by which stage act — so the cockpit's `Queued for next station` becomes
            # `Read by <name>` on a real transition, not a guess.
            read_by = None
            try:
                read_by = agent_acts.quietly(c, "sign a steer", agent_acts.stage_act,
                                             project_id=None, code_task_run_id=run_id,
                                             stage=stage)
            except Exception:  # noqa: BLE001 — an acknowledgement never blocks the guidance
                read_by = None
            from datetime import datetime as _dt, timezone as _tz
            stamp = _dt.now(_tz.utc).isoformat()
            pending = [{**item, "applied_at": stamp, "applied_stage": stage,
                        "applied_act_id": (read_by or {}).get("act_id"),
                        "applied_role": (read_by or {}).get("role_key")} for item in pending]
            row_p = c.execute(sql("SELECT project_id FROM code_task_runs WHERE id=:id"),
                              {"id": run_id}).scalar()
            if row_p:
                emit_event(c, row_p, "code_task.guidance_applied", "boss", "code_task_run",
                           run_id, {"count": len(pending), "stage": stage,
                                    "act_id": (read_by or {}).get("act_id")})
        c.execute(sql(
            "UPDATE code_task_runs SET finalize_ctx = "
            "COALESCE(finalize_ctx,'{}'::jsonb) || jsonb_build_object("
            "  'pending_guidance', '[]'::jsonb,"
            "  'applied_guidance', COALESCE(finalize_ctx->'applied_guidance','[]'::jsonb) "
            "                      || CAST(:g AS JSONB)), updated_at=now() WHERE id=:id"),
            {"g": json.dumps(pending), "id": run_id})
    return pending


def _guidance_block(items: list[dict]) -> str:
    if not items:
        return ""
    lines = "\n".join(f"- {i.get('text','')}".strip() for i in items if i.get("text"))
    return (
        "\n\n=== OPERATOR STEERING (issued while this task was running) ===\n"
        "A human is watching this run and has redirected it. This OVERRIDES the earlier "
        "instructions where they conflict. Apply it now, before continuing:\n"
        f"{lines}\n"
    )


def _kill_stray_review_processes(*roots) -> int:
    """Terminate `claude` review subprocesses still running under the given sandbox roots.

    The review deadline previously called Future.cancel(), which is a NO-OP for an
    already-running future. The seat is a subprocess, so the only way to stop it is to
    signal the process.

    SAFETY: an earlier version matched any command line containing "claude" AND the path,
    which matched the CHECKING PROCESS ITSELF and killed it. So we now require the
    executable to be a claude binary, skip our own process tree, and require the sandbox
    path to appear as a real path component — never a loose substring.
    """
    import signal

    from .code_agent import is_protected as _is_protected_seat

    paths = [str(r).rstrip("/") for r in roots if r]
    paths = [p for p in paths if p and p not in ("/", "/tmp")]
    if not paths:
        return 0
    mine = {os.getpid(), os.getppid()}
    killed = 0
    try:
        from . import owned_process as _owned
        out = _owned.run(["ps", "-axo", "pid=,command="], capture_output=True, text=True,
                      timeout=10).stdout
    except Exception:  # noqa: BLE001 — best effort; never sink the run
        return 0
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(None, 1)
        if len(parts) != 2:
            continue
        try:
            pid = int(parts[0])
        except ValueError:
            continue
        if pid in mine:
            continue
        # OWNERSHIP BEATS PATTERN MATCHING.
        #
        # This reaps REVIEW seats, but a review seat and an implement agent are the same
        # binary under the same sandbox root — indistinguishable in `ps`. It killed a build
        # that was working ("claude code agent exited -9"), and the two guards above are the
        # scar tissue from the two previous times it killed the wrong thing. A process is safe
        # to kill because of who started it and why, which no command line can tell you.
        if _is_protected_seat(pid):
            continue
        cmd = parts[1]
        toks = cmd.split()
        # SOME token must be a claude EXECUTABLE (argv[0] is the interpreter when the binary
        # is a shim/wrapper, so checking argv[0] alone misses the real seat). Still far
        # stricter than "the line mentions claude", which is what killed the caller.
        if not any(os.path.basename(t) == "claude" for t in toks):
            continue
        if not any(cmd.startswith(p) or f" {p}/" in cmd or f" {p} " in cmd for p in paths):
            continue
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.kill(pid, sig)
                killed += 1
                break
            except ProcessLookupError:
                break
            except Exception:  # noqa: BLE001
                continue
    return killed


# Gates whose failure means the TEST SUITE is what needs work. Any of these and a repair must
# re-run write_tests; none of them and the suite on the branch is sound, so the repair's job is
# the code and re-authoring tests is pure cost.
_TEST_GATES = frozenset({
    "tests_strong_enough",     # the suite does not catch a one-line mutation
    "tests_written_first",     # TDD order was violated
    "coder_did_not_edit_tests",  # the coder moved the goalposts
    "written_tests_pass",      # this run's own tests fail
    "existing_tests_intact",   # pre-existing tests were weakened or deleted
})


# Every gate the pipeline can report. Used to detect a gate name this build does not know.
_ALL_GATE_NAMES = _TEST_GATES | frozenset({
    "code_written", "code_review_passed", "security_clean", "nothing_broke", "consistent",
    "app_serves",
})


# Errors that mean THE HARNESS died, not that the code was judged wanting: provider spend
# caps, rate limits, timeouts, infrastructure. A retry after one of these must resume, not redo.
# Deliberately EXACT phrases the engine itself writes, not loose tokens. A bare "timeout"
# matched the sentence "review found a missing timeout on the fetch call", which would have
# classified a genuine code finding as a platform death and skipped re-authoring the tests.
# Same failure as the earlier inconclusive-check bug: reviewer prose is not a status code.
_SYSTEM_FAILURE_MARKERS = (
    "spend limit",
    "usage limit",
    "rate limit exceeded",
    "quota exceeded",
    "too many requests",
    "timed out after",
    # Exact httpx / worker phrases. A bare "timeout" is NOT listed — reviewer prose
    # ("missing timeout on the fetch call") must stay a code verdict.
    "read operation timed out",
    "readtimeout",
    "lease expired",
    "budget reservation denied",
    "connection refused",
    "connection reset",
    "infrastructure error",
    "executable is not allowlisted",
)


def is_system_failure(error: Optional[str]) -> bool:
    """Did the previous attempt die from a platform problem rather than a code verdict?

    A spend-limit kill records NO failing gates, so resume logic that keys on gates falls back
    to "re-run everything" — which is exactly the waste the operator is trying to avoid. The
    code the attempt already wrote is on the branch (see _checkpoint), so the retry should keep
    it and continue, not re-author it.
    """
    low = (error or "").lower()
    return any(m in low for m in _SYSTEM_FAILURE_MARKERS)


def _repair_needs_write_tests(prior_failing_gates, *, prior_error: Optional[str] = None,
                              tests_on_branch: bool = False) -> bool:
    """Whether a repair must re-author tests, given what the parent attempt failed on.

    Unknown gates (an empty/missing list) return True — a retry with no recorded verdict gets
    the full pipeline. Skipping work is only justified by positive evidence that it is
    redundant, never by absence of evidence.
    """
    gates = [str(g) for g in (prior_failing_gates or [])]
    if not gates:
        # No verdict was reached. If the harness died AND the branch already carries a suite,
        # that suite is intact work — resume onto it instead of paying to re-author it.
        if tests_on_branch and is_system_failure(prior_error):
            return False
        return True
    # A gate name this build does not recognise gets the same treatment as no evidence. If a
    # gate is added later and nobody updates _TEST_GATES, the failure mode must be "did some
    # redundant work", never "silently skipped the stage that would have caught it".
    if any(g not in _ALL_GATE_NAMES for g in gates):
        return True
    return any(g in _TEST_GATES for g in gates)


class CodeTaskCancelled(Exception):
    """The operator cancelled this run. Raised at a stage boundary so the pipeline unwinds
    cleanly (releasing budget, retaining the sandbox) instead of running to completion on
    work nobody wants."""


# The full adversarial panel. A profile may enable a subset via `panel_checks`; `regression`
# is the only seat that is not an LLM call (it executes the test suite).
_ALL_PANEL_SEATS = ("review_code", "security_check", "regression", "consistency_check")

# The ONLY thing that stops an advisory profile opening a PR: there is no code to open one
# with. That is not a gate exercising judgement, it is the absence of a deliverable.
#
# `existing_tests_intact` used to sit here too, and it was wrong. RAISING a pull request is
# not a risk — it is a diff on a branch that a human reads. MERGING is the irreversible act,
# and that is where the weakened-tests verdict already blocks (_pull_request_ci_gate's
# blocked_weakened_existing_tests hard stop). Holding the PR back as well meant the operator
# got nothing to look at, which is how "22 runs, 2 PRs" happened. Findings ride ON the PR.
_ADVISORY_HARD_FLOOR = ("code_written", "app_serves")

# Gates no autonomy setting may ship past. `app_serves` is not a reviewer's opinion: BOSS
# started the build's own run command and nothing answered HTTP. The Lead Triage Inbox build
# (crun_3e370c11) completed and opened PR #218 while its app could not start at all — an
# ImportError on its first line — and the operator found out from a preview timeout. A build
# whose app does not run is not done, so exhausting the repairs BLOCKS with the app's own
# error (and the partial PR rescue keeps the work), rather than shipping with dissent.
_NEVER_SHIP_PAST = frozenset({"app_serves"})


def app_serves_check_skip_reason(fctx: dict, *, command_runtime: str = "subprocess",
                                 use_microvm: bool = False) -> Optional[str]:
    """Why the app_serves gate does not apply to this run, or None when it does.

    A NON-FINAL DECOMPOSITION SLICE is not the app yet. Slice 1 of a decomposed build may be
    the data model alone; the server is a later slice's job. Held to serving, it failed with
    "serves HTTP but declares no way to run it", and because a non-final slice never opens a
    PR the gate went straight to block — the chain stopped at slice 1 unless a convergence
    round talked the agent into writing a server its slice was not about. The FINAL slice
    carries the whole lineage and is held to serving in full.

    WHERE THE BUILD RAN IS NOT WHERE THE PROOF WOULD RUN. With BOSS_SANDBOX_COMMAND_RUNTIME=
    docker the suite and its dependency installs run in the command container; on a micro-VM
    substrate the whole build runs in the guest. The proof launches on the control-plane
    host. A Flask build whose Flask exists only in the container or the VM "failed" there with
    ModuleNotFoundError — a fact about where BOSS ran it — and, worse, running it at all put
    agent-written code on the host that the docker runtime and the VM exist to keep it off
    (second review of f16b5d4). f16b5d4 ran it and then marked a failure inconclusive; that
    still executed the code. The check is now not run there, and the record says why.
    """
    didx, dtot = (fctx or {}).get("decomposition_index"), (fctx or {}).get("decomposition_total")
    if isinstance(didx, int) and isinstance(dtot, int) and dtot > 1 and didx < dtot:
        return f"decomposition slice {didx}/{dtot}: the final slice proves the app serves"
    if use_microvm:
        return ("inconclusive: the build ran in a micro-VM, and BOSS does not start "
                "agent-written code on the control-plane host to check it")
    if str(command_runtime or "").lower() == "docker":
        return ("inconclusive: the build's commands run in the docker command runtime, and "
                "BOSS does not start agent-written code on the host to check it")
    return None


def serve_proof_known_service(spec: Optional[dict], fctx: Optional[dict]) -> bool:
    """Is this run KNOWN to build a service before the serve proof looks at its files?

    Only when it builds a named full-stack app (official_app_id: a scaffolded or benchmark app
    directory). NOT `web_app_build`: tools/builtin.py and intent_router set it on every
    build-on-existing/extend — CLIs and libraries included — so f16b5d4, which read it as "this
    serves HTTP", launched a CLI follow-up, saw it exit, and blocked it with a gate nothing may
    ship past (second review of f16b5d4). Otherwise only a run command the lineage DECLARED
    holds a build to serving (prove_app_serves). Nothing is inferred from the code: b63717c's
    "the entry starts a server when run" still launched a CLI whose main() chose `serve` with
    an if/elif or a --serve flag, and blocked it (third review of b63717c).
    """
    return bool(verification_target.official_app_id(spec or {}, fctx or {}))


def _app_serves_sentence(record: Optional[dict]) -> str:
    """The serve proof's failure in ONE wording for every place it is shown (blocked error,
    convergence finding, repair prompt) — see preview_service.app_serves_failure_sentence."""
    from .preview_service import app_serves_failure_sentence

    return app_serves_failure_sentence(record)


def blocked_gate_messages(qg_id: str, failing: list, app_serves_record: Optional[dict]
                          ) -> tuple[str, str]:
    """(code_task_runs.error, partial-PR rescue reason) for a run the quality gate blocked.

    The error used to name the failing gates only — "failing gates: ['app_serves']" — so an
    operator looking at a blocked build had to dig the app's ImportError out of the evidence
    and the event log. And the rescue reason always said "never_block is disabled", which was
    false whenever app_serves forced the block with never_block on. When the app does not
    serve, say so in app_serves_failure_sentence's words: what ran, where that command came
    from, and whether it exited or never answered.
    """
    error = (f"QualityGate FAIL ({qg_id}); failing gates: {failing}. "
             "A human can waive these + open the PR via /override.")
    reason = "quality gate blocked and never_block is disabled"
    if "app_serves" in (failing or []):
        sentence = _app_serves_sentence(app_serves_record)
        error += f" The app does not serve HTTP. {sentence[:700]}"
        reason = "the app does not serve HTTP, so the build is not done"
    return error, reason


def resolve_gate_failure_action(
    *,
    failing: list[str],
    advisory_gate: bool,
    rounds_used: int,
    max_repair_rounds: int,
    open_pr: bool = True,
    never_block: bool = True,
    faults: Optional[dict[str, str]] = None,
) -> str:
    """Decide what to do after a non-passing quality gate.

    Returns one of:
      - ``"ship"``              — open the PR (hard pass, or advisory first-pass)
      - ``"repair"``            — autonomous review-repair retry
      - ``"ship_with_dissent"`` — exhausted the loops, still failing: ship anyway, record the
        unresolved gates, and queue them as follow-up work
      - ``"block"``             — pause for human gate override (only when never_block=False)

    Advisory mode MUST ship: clearing failing gates without short-circuiting used to fall
    through into the block path and permanently stall web-app first-pass builds.

    ``never_block`` is the autonomy default. A system that halts for a human whenever its own
    reviewers stay unhappy delivers nothing on the runs that need it most — the operator ends up
    adjudicating exactly the small defects the agents were hired to handle. Shipping with the
    dissent RECORDED (and re-queued as its own task) keeps every fact a human needs to reverse
    the call, while the program keeps moving. Set BOSS_CODE_GATE_NEVER_BLOCK=false for the old
    fail-closed behaviour.
    """
    if not failing:
        return "ship"
    # A CHECK THAT NEVER RAN IS NOT AN OPINION TO SHIP PAST — checked BEFORE advisory.
    #
    # This sat below the advisory branch, which returns "ship" for anything outside the hard
    # floor. review_code is outside it, so a reviewer that ran out of turns and judged nothing
    # took the same path as a reviewer that read the diff and had a view. The escalation built
    # for exactly this case — re-run the CHECK with a doubled budget — could never fire,
    # because the action was decided before the question was asked.
    #
    # Measured: a 39-file build shipped with adversarial_verdict "not_run", review_confidence
    # 0.0 and no review comment on the pull request, while three gates were recorded as
    # advisory. Advisory means a reviewer's OPINION cannot withhold the PR. It cannot mean the
    # opinion was never needed.
    if faults and all(faults.get(g) == "HARNESS" for g in failing):
        return "inconclusive"
    if advisory_gate:
        # Advisory means a REVIEWER'S OPINION cannot withhold the PR. It does not mean
        # "ignore the gates". Two failures are facts, not opinions, and still get the full
        # repair treatment: the change broke tests that passed before it (a regression), or
        # no code was produced at all (nothing to ship). Everything else — review_code,
        # security_check, consistency_check, tests_strong_enough — is judgement, and gets
        # surfaced on the PR instead of blocking it.
        blocking = [g for g in failing if g in _ADVISORY_HARD_FLOOR]
        if not blocking:
            return "ship"
        failing = blocking
    # A repair re-runs the CODING agent. That is only a coherent remedy when a check actually
    # judged the code and found it wanting. If every failing gate is a HARNESS fault — the
    # check ran out of turns, timed out, or was skipped — then nothing has been said about the
    # code, and repairing it re-derives the whole pipeline to reach the same non-answer. This
    # single conflation produced repeated ~35-minute, ~$12 loops on changes whose tests passed.
    # Still needed: the advisory branch above may have NARROWED `failing` to the hard floor,
    # and those remaining gates can themselves be harness faults.
    if faults and failing and all(faults.get(g) == "HARNESS" for g in failing):
        return "inconclusive"
    if rounds_used < max_repair_rounds and open_pr:
        return "repair"
    if any(g in _NEVER_SHIP_PAST and (faults or {}).get(g) != "HARNESS" for g in failing):
        return "block"
    return "ship_with_dissent" if (never_block and open_pr) else "block"


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


def _ctx_block(title, instructions, criteria, *, service: bool = False) -> str:
    """Identical task-context preamble for EVERY pipeline stage — byte-identical on
    purpose. The model API's prompt cache is keyed on the request prefix, so stages
    launched back-to-back (within the cache TTL) re-READ this block from cache instead
    of cold-ingesting it in each fresh session. Keep all stage-specific text AFTER it."""
    crit = "\n".join(f"- {c}" for c in criteria or []) or "- (none specified)"

    # Standards the PLATFORM requires, as distinct from the ones `_standards_prompt` infers by
    # reading the repo. An agent cannot discover "expose /metrics in this shape" from a codebase
    # that has never done it, so BOSS has to say so — otherwise the rule lives in whichever
    # task's prose someone remembered to write it into, which is exactly what happened with the
    # first instrumented app.
    #
    # Added HERE rather than to one stage because a standard read only during design is
    # forgotten by the time code is written; this block is shared verbatim by every stage, and
    # stays byte-identical across them, so the prompt cache is unaffected.
    from .platform_standards import platform_standards

    # WHAT BOSS IS BUILDING, NOT WHAT THE BRIEF HAPPENS TO SAY.
    #
    # `applies_to` keyword-matches the prose for "http", "api", "service", "port". Real briefs
    # are written in product language — "Build an incident timeline builder for an on-call
    # engineer" — and match none of them. Checked against the directives BOSS has actually
    # built from, it fired on ZERO of them: HookWatch, Vendor-Status-Watcher, the incident
    # timeline builder, the docs freshness board. So the observability standard has been
    # silently absent from every app the platform ever shipped, which is exactly why three of
    # them reached a host with no /metrics and the monitoring page linked to dashboards for
    # data nobody was collecting.
    #
    # The pipeline knows: `web_app_build` is set before any of this runs, and a web app BOSS
    # deploys is a thing BOSS scrapes. Passing that in makes the rule apply because of what is
    # being built, not because of a word somebody used. The prose check stays as the fallback
    # for callers that genuinely do not know.
    standards = platform_standards(f"{title}\n{instructions}", force=service)
    # WHAT THE OPERATOR SAID TO BUILD AGAINST. In the shared preamble for the same reason the
    # standards are: material read only at design time is forgotten by the time code is
    # written, and this block is byte-identical across stages so the prompt cache is unaffected.
    from .platform_standards import reference_material

    references = reference_material()
    return (
        "=== TASK CONTEXT (shared verbatim across all pipeline stages) ===\n"
        f"TASK: {title}\n\n{instructions}\n\nACCEPTANCE CRITERIA:\n{crit}\n"
        f"{standards}{references}\n"
        "=== END TASK CONTEXT ===\n\n"
    )


def _qa_prompt(title, instructions, criteria, test_cmd, *, iac=False, web=False, app_root=None):
    service = bool(web or app_root)
    if web:
        scope = f"`{app_root}/`" if app_root else "the app directory"
        return (
            _ctx_block(title, instructions, criteria, service=service)
            + "You are a QA engineer for a web application. Your ONLY job is to author tests "
            "that the authoritative `npm run verify` command will actually run.\n\n"
            "RULES:\n"
            f"- Write JS/TS API tests and a Playwright/e2e spec under {scope}. "
            "Prefer `tests/` and `e2e/` inside that app.\n"
            "- Do NOT write Python unittest files. Do NOT put tests at the monorepo root.\n"
            "- Do NOT edit sibling apps or repo-root tests that belong to other products.\n"
            f"- You MAY add `{app_root or '.'}/package.json` scripts "
            "(verify, test:api, test:e2e, typecheck, lint, build, healthcheck) if they are missing.\n"
            "- Do NOT create the product implementation.\n"
            f"- Do NOT run anything and do NOT commit. (BOSS will run `{test_cmd}` inside {scope}.)\n"
            "- End with a one-line summary of the cases you wrote.\n"
            + _metrics_tests_clause(title, instructions, service=service)
            + (_IAC_GUIDANCE + _IAC_QA_GUIDANCE if iac else "")
        )
    return (
        _ctx_block(title, instructions, criteria, service=service)
        + "You are a QA engineer. Your ONLY job is to author tests for the task above, BEFORE any "
        "implementation exists.\n\n"
        "RULES:\n"
        "- Create test file(s) ONLY, using the stdlib `unittest` module (e.g. `test_<thing>.py`).\n"
        "- Do NOT create or modify any implementation/source file. Import the module-under-test by "
        "its intended name even though it does not exist yet.\n"
        "- Cover: happy path, the most likely failure, boundary/edge cases, and validation/error paths.\n"
        f"- Do NOT run anything and do NOT commit. (BOSS will run `{test_cmd}`.)\n"
        "- End with a one-line summary of the cases you wrote.\n"
        + _metrics_tests_clause(title, instructions, service=service)
        + (_IAC_GUIDANCE + _IAC_QA_GUIDANCE if iac else "")
    )


def _metrics_tests_clause(title: str, instructions: str, *,
                          service: bool = False) -> str:
    """QA's share of the observability standard, for anything that serves traffic.

    The standard told the BUILDER what to expose and said rendering "must be testable". Nothing
    then tested it, and a standard no stage verifies is a suggestion: three apps reached a host
    with a monitoring page linking to dashboards for metrics nobody had checked existed.

    Same `applies_to` gate as the standard itself, so a CLI tool or a library is not asked for
    an endpoint it has no reason to have.
    """
    from .platform_standards import METRICS_TEST_REQUIREMENT, applies_to

    if not (service or applies_to(f"{title}\n{instructions}")):
        return ""
    return "\n\n" + METRICS_TEST_REQUIREMENT + "\n"


def _run_declaration_rule() -> str:
    """The implement stage's share of the build contract: say HOW TO RUN what was built.

    The Lead Triage Inbox build (crun_3e370c11) finished green and its preview then guessed
    lead_triage_core/approvals.py as the app, ran it as a script, and died on a relative import.
    Every stage that WRITES the implementation carries this rule, and the app_serves gate
    (preview_service.prove_app_serves) starts the declared command before the build can pass.
    The text lives beside the code that reads the file, so the two cannot drift.
    """
    from .preview_service import RUN_DECLARATION_INSTRUCTION

    return RUN_DECLARATION_INSTRUCTION


def _impl_prompt(title, instructions, test_files, test_cmd, *, criteria=(), iac=False, ui=False, app_root=None):
    tf = ", ".join(test_files) or "(the test files in this repo)"
    scope = (
        f"- Work ONLY under `{app_root}/`. Do not edit sibling apps.\n"
        if app_root else ""
    )
    return (
        _ctx_block(title, instructions, criteria, service=bool(ui or app_root))
        + "You are a software engineer. QA has ALREADY written the tests; implement the change to "
        "make those existing tests pass.\n\n"
        f"RULES:\n"
        f"{scope}"
        f"- Edit/create implementation files ONLY. You MUST NOT edit, delete, or weaken any test file "
        f"({tf}) — they are the spec. If a test looks wrong, STOP and explain rather than change it.\n"
        f"- Run `{test_cmd}` from the app directory and iterate until the tests pass. "
        "Create parent directories for SQLite/data files before opening them.\n"
        + _run_declaration_rule()
        + "- Do NOT commit. End with a one-line summary of what you changed.\n"
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


def _json_dumps(value) -> str:
    import json as _json

    return _json.dumps(value)


def _comment_signers(c, run_id: str, review_seat) -> dict:
    """{stage: {act_id, role_key}}: the newest act that ran each review stage of this build.
    For a panel, the code review is the seat whose findings stand for it."""
    out: dict = {}
    try:
        rows = c.execute(sql(
            "SELECT act_id, role_key, step_id, seat FROM agent_acts WHERE kind = 'stage' "
            "AND code_task_run_id = :r AND step_id IN ('review_code','security_check',"
            "'consistency_check') ORDER BY started_at"), {"r": run_id}).mappings().all()
    except Exception:  # noqa: BLE001 — a signature is never worth losing the comments
        return out
    for r in rows:
        stage = r["step_id"]
        if stage == "review_code" and review_seat and r["seat"] not in (None, review_seat):
            continue
        out[stage] = {"act_id": r["act_id"], "role_key": r["role_key"]}
    return out


def _record_review_comments(engine, run_id: str, project_id: str, evidence: dict,
                            *, round_i: int = 0) -> int:
    """Turn the panel's findings into durable, line-anchored comments AS THE REVIEW HAPPENS.

    They used to exist only as prose in `evidence`, and only became comments when the pull
    request was opened — which is after every fix round has already run. So the artifact the
    implementer worked from and the artifact a human later read on the PR were produced by two
    different code paths from the same text, at different times, and could disagree.

    Recording here makes one set of comments the single source: the repair round reads it, the
    pull request posts it, and `list_review_comments` shows the same thing to a person.

    Idempotent on (stage, path, line, severity, body): a re-review of the same defect updates
    nothing rather than stacking a second copy of a comment the implementer has already seen.
    """
    fresh: list = []
    for stage, key in (("review_code", "review_summary"),
                       ("security_check", "security_summary"),
                       ("consistency_check", "consistency_summary")):
        fresh += review_comments.parse_findings(evidence.get(key) or "", stage=stage)
    if not fresh:
        return 0
    try:
        with _transaction(engine) as c:
            row = c.execute(sql(
                "SELECT finalize_ctx FROM code_task_runs WHERE id=:id AND project_id=:p"),
                {"id": run_id, "p": project_id}).mappings().first()
            existing = list(((row or {}).get("finalize_ctx") or {}).get("review_comments") or [])
            signers = _comment_signers(c, run_id, evidence.get("review_seat"))
            seen = {(e.get("stage"), e.get("path"), e.get("line"), e.get("severity"),
                     (e.get("body") or "")[:120]) for e in existing}
            added = []
            for comment in fresh:
                key_t = (comment.stage, comment.path, comment.line, comment.severity,
                         comment.body[:120])
                if key_t in seen:
                    continue
                seen.add(key_t)
                signed = signers.get(comment.stage) or {}
                added.append({"id": make_id("rc"), "round": round_i, **comment.to_dict(),
                              # The agent who wrote it, from the act that ran the stage —
                              # never the parser's default (factory spec §6.4).
                              "author_role_key": signed.get("role_key"),
                              "act_id": signed.get("act_id")})
            if not added:
                return 0
            c.execute(sql(
                "UPDATE code_task_runs SET finalize_ctx = COALESCE(finalize_ctx,'{}'::jsonb) "
                "|| jsonb_build_object('review_comments', "
                "COALESCE(finalize_ctx->'review_comments','[]'::jsonb) || CAST(:e AS JSONB)), "
                "updated_at=now() WHERE id=:id AND project_id=:p"),
                {"e": _json_dumps(added), "id": run_id, "p": project_id})
            emit_event(c, project_id, "code_task.review_comments_recorded", "boss",
                       "code_task_run", run_id,
                       {"added": len(added), "round": round_i,
                        "anchored": sum(1 for a in added if a.get("path") and a.get("line"))})
        return len(added)
    except Exception as exc:  # noqa: BLE001 — a review must not die recording its own output
        logger.warning("code_task.review_comments_not_recorded run=%s %s: %s",
                       run_id, type(exc).__name__, exc)
        return 0


def _open_review_comments(engine, run_id: str, project_id: str) -> list:
    """Every unresolved comment on this run — the panel's and the operator's, in one list.

    Operator comments first: a person who pointed at a line has said something more specific
    than a reviewer did, and a repair round is bounded.
    """
    out = _open_operator_comments(engine, run_id, project_id, include_agents=True)
    out.sort(key=lambda c: (0 if c.author_kind == review_comments.AUTHOR_HUMAN else 1,
                            -c.blocking_rank))
    return out


def _open_operator_comments(engine, run_id: str, project_id: str,
                            *, include_agents: bool = False) -> list:
    """Line comments a person left on this run that nobody has dealt with yet.

    Read at the repair round rather than at run start: a comment made WHILE the agent was
    working must reach the next attempt, not the one after it.
    """
    try:
        with _transaction(engine) as c:
            row = c.execute(sql(
                "SELECT finalize_ctx FROM code_task_runs WHERE id=:id AND project_id=:p"),
                {"id": run_id, "p": project_id}).mappings().first()
    except Exception as exc:  # noqa: BLE001 — a repair must not die reading an optional input
        logger.warning("code_task.operator_comments_unreadable run=%s %s: %s",
                       run_id, type(exc).__name__, exc)
        return []
    raw = ((row or {}).get("finalize_ctx") or {}).get("review_comments") or []
    out = []
    for entry in raw:
        if (entry or {}).get("status", "open") != "open":
            continue
        if (not include_agents
                and (entry or {}).get("author_kind") != review_comments.AUTHOR_HUMAN):
            continue
        try:
            out.append(review_comments.ReviewComment.from_dict(entry))
        except Exception:  # noqa: BLE001 — one malformed row must not hide the rest
            continue
    return out


def _all_review_comments(engine, run_id: str, project_id: str) -> list:
    """Every comment on this run, whatever its status."""
    try:
        with _transaction(engine) as c:
            row = c.execute(sql(
                "SELECT finalize_ctx FROM code_task_runs WHERE id=:id AND project_id=:p"),
                {"id": run_id, "p": project_id}).mappings().first()
    except Exception:  # noqa: BLE001
        return []
    out = []
    for entry in (((row or {}).get("finalize_ctx") or {}).get("review_comments") or []):
        try:
            out.append(review_comments.ReviewComment.from_dict(entry))
        except Exception:  # noqa: BLE001
            continue
    return out


def _post_review_comments_to_pr(sf, proposal: dict, evidence: dict, engine, project_id,
                                run_id: str) -> dict:
    """Leave the panel's findings ON the pull request, at the lines they are about.

    The review already produced graded, located findings, and until now they lived only in the
    run's evidence — so the PR carried a paragraph of prose and a human reviewer had to map it
    back onto the diff themselves. GitHub's review API takes the anchors directly.

    Best effort, always: a review that cannot be posted must never block a pull request that
    passed its gates. A failure is an event, not an exception.
    """
    number = proposal.get("pull_request_number")
    if not number:
        return {"posted": False, "reason": "no pull request number"}
    # The stored set, recorded when the review ran — including comments the implementer has
    # since addressed, because a pull request that shows only what is still broken hides the
    # conversation that got it there.
    comments = _all_review_comments(engine, run_id, project_id)
    if not comments:
        for stage, key in (("review_code", "review_summary"),
                           ("security_check", "security_summary"),
                           ("consistency_check", "consistency_summary")):
            comments += review_comments.parse_findings(evidence.get(key) or "", stage=stage)
    # WHAT THE REVIEWERS ACTUALLY WROTE, even when it will not parse.
    #
    # This returned here, silently — no event, no log, nothing on any screen. On a 39-file
    # build the security and consistency reviewers each produced a substantial review in PROSE
    # ("## Security Review — Documentation Freshness Board...", "I reviewed the implementation
    # (serve_freshness.py, freshness_core/*, ...)"), none of it in the graded form
    # parse_findings expects. So the pull request carried nothing at all, and the operator had
    # no way to tell "the reviewers found nothing" from "the reviewers were never asked".
    #
    # An unparseable review is worth less than an anchored one and far more than silence.
    summaries: list[str] = []
    if not comments:
        for stage, key in (("review_code", "review_summary"),
                           ("security_check", "security_summary"),
                           ("consistency_check", "consistency_summary")):
            text_out = str(evidence.get(key) or "").strip()
            if text_out:
                summaries.append(f"### {stage}\n\n{text_out[:4000]}")
        if not summaries:
            with _transaction(engine) as c:
                emit_event(c, project_id, "code_task.review_comments_absent", "boss",
                           "code_task_run", run_id,
                           {"pull_request_number": number,
                            "reason": "no reviewer produced findings or a summary",
                            "stages": ["review_code", "security_check",
                                       "consistency_check"]})
            return {"posted": False, "reason": "no structured findings"}
    inline = review_comments.to_github_comments(comments)
    loose = review_comments.unanchored_body(comments)
    body = ("BOSS review panel — findings from the independent reviewers.\n\n"
            + (loose + "\n\n" if loose else "")
            + ("\n\n".join(summaries) + "\n\n"
               + "_These are the reviewers' own summaries: nothing in them could be anchored "
                 "to a line, so they are reproduced in full rather than dropped._\n\n"
               if summaries else "")
            + "Severity decides the gate; MINOR and NIT are recorded as follow-up work.")
    try:
        # COMMENT, never REQUEST_CHANGES: the gate already decided whether this ships, and a
        # second blocking opinion on the PR would be a second place that decision lives.
        result = sf.github.create_review(int(number), body=body, event="COMMENT",
                                         comments=inline or None)
        with _transaction(engine) as c:
            emit_event(c, project_id, "code_task.review_comments_posted", "boss",
                       "code_task_run", run_id,
                       {"pull_request_number": number, "anchored": len(inline),
                        "unanchored": len(comments) - len(inline),
                        "prose_summaries": len(summaries)})
        return {"posted": True, "anchored": len(inline),
                "unanchored": len(comments) - len(inline),
                "review_url": getattr(result, "url", None)}
    except Exception as exc:  # noqa: BLE001 — never block a passing PR on a comment
        with _transaction(engine) as c:
            emit_event(c, project_id, "code_task.review_comments_failed", "boss",
                       "code_task_run", run_id,
                       {"pull_request_number": number,
                        "error": f"{type(exc).__name__}: {exc}"[:200]})
        return {"posted": False, "reason": f"{type(exc).__name__}: {exc}"[:200]}


def _anchored_findings(summary: str, root, *, stage: str, header: str) -> str:
    """A review pass as line-anchored instructions, with the code quoted at each place.

    Falls back to the raw summary when nothing parsed. A reviewer that ignores the FINDING
    contract must still reach the implementer — dropping its words because they were badly
    formatted would turn a formatting problem into a quality one.
    """
    comments = review_comments.parse_findings(summary or "", stage=stage)
    rendered = review_comments.render_for_implementer(comments, root) if comments else ""
    if rendered:
        return f"{header}\n{rendered}"
    return f"{header}\n" + (summary or "")[:3000]


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


def _solo_prompt(title, instructions, criteria, test_cmd, *, iac=False, web=False, app_root=None):
    if web:
        scope = f"`{app_root}/`" if app_root else "the app directory"
        return (
            _ctx_block(title, instructions, criteria)
            + "You are a software engineer. Implement the web app AND the tests that "
            f"`{test_cmd}` will run inside {scope}.\n\n"
            "RULES:\n"
            f"- Work ONLY under {scope}. Do not edit sibling apps.\n"
            "- Write JS/TS API and e2e tests, not Python unittest.\n"
            f"- Run `{test_cmd}` from {scope} and iterate until it passes. "
            "Create parent directories for SQLite/data files before opening them.\n"
            + _run_declaration_rule()
            + "- Do NOT commit. End with a one-line summary of what you built.\n"
            + (_IAC_GUIDANCE + _IAC_QA_GUIDANCE if iac else "")
        )
    return (
        _ctx_block(title, instructions, criteria)
        + "You are a software engineer. Implement the task above AND write tests for it, then make "
        "the tests pass. This is a low-risk task, so one agent does both.\n\n"
        "RULES:\n"
        "- Create the implementation file(s) and stdlib `unittest` test file(s).\n"
        f"- Run `{test_cmd}` and iterate until it passes. Do NOT commit.\n"
        + _run_declaration_rule()
        + "- End with a one-line summary of what you built.\n"
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
        + "For EVERY defect you find, emit one line in EXACTLY this form. SAY WHERE: give the\n"
        "file and line the defect is ON, the way you would leave a comment on a pull request.\n"
        "A reviewer who says only 'error handling is weak' hands the implementer a search\n"
        "problem on top of a fix, and on a large change it will guess wrong.\n\n"
        "  FINDING: <SEVERITY> <path>:<line> — <what is wrong there>\n\n"
        "Severity is graded, and it is what decides whether the change ships — grade honestly:\n"
        "  FINDING: BLOCKER app/pay.py:88 — data loss, security hole, or the feature does not work\n"
        "  FINDING: MAJOR   lib/rules.py:14 — an acceptance criterion is materially violated\n"
        "  FINDING: MINOR   ui/card.tsx:31 — a real but non-blocking defect (cosmetic, rare edge case)\n"
        "  FINDING: NIT     lib/util.py:7 — style or preference\n"
        "If a defect genuinely has no single location (something MISSING, or spread across the\n"
        "change) omit the path and say so in the text — an unanchored finding still counts.\n"
        "List MINOR/NIT findings too — they are recorded and fixed as separate follow-up work, so "
        "you lose nothing by grading accurately. Do NOT inflate a cosmetic issue to MAJOR to force "
        "attention, and do NOT suppress one to let the change through.\n\n"
        + "Do NOT edit any files. End your response with EXACTLY one final line:\n"
        "  VERDICT: REFUTED   — ONLY if you found a CONCRETE defect that violates the task/criteria.\n"
        "  VERDICT: SURVIVED  — if the change satisfies the task and criteria. This is the default "
        "when you did not find a real, in-scope defect.\n"
    )


# Severity ladder for the review gate. Index = how blocking it is; a finding at or above the
# configured threshold fails `code_review_passed`, everything below is recorded as follow-up work.
_SEVERITY_ORDER = ["nit", "minor", "major", "blocker"]


def _parse_findings(summary: str, *, stage: str | None = None) -> list[dict]:
    """Extract the reviewer's machine-readable FINDING lines, with their locations.

    Returns [] when the reviewer emitted none, which the caller MUST treat as "no structured
    signal" and fall back to the binary verdict — an empty list here never means "clean".

    Delegates to `review_comments.parse_findings` so the gate and the pull-request comments are
    reading the same lines; two parsers for one contract is two chances to disagree about
    whether a change is safe. `path`/`line` are None for an unanchored finding, and `text` is
    kept so every existing caller is unaffected.
    """
    return [
        {"severity": c.severity, "text": c.body[:300], "path": c.path, "line": c.line,
         "end_line": c.end_line, "stage": c.stage}
        for c in review_comments.parse_findings(summary or "", stage=stage)
    ]


def _review_gate_from_findings(
    summary: str, verdict: str, block_severity: str = "major",
) -> tuple[bool, list[dict], list[dict]]:
    """Decide `code_review_passed` from graded findings instead of a single bit.

    Returns (passed, blocking, non_blocking). When the reviewer emitted no structured findings
    we defer entirely to ``verdict`` — the pre-existing fail-closed behaviour — so a model that
    ignores the FINDING contract can never accidentally turn a refutation into a pass.
    """
    findings = _parse_findings(summary)
    if not findings:
        return verdict == "survived", [], []
    try:
        floor = _SEVERITY_ORDER.index((block_severity or "major").strip().lower())
    except ValueError:
        floor = _SEVERITY_ORDER.index("major")
    blocking = [f for f in findings if _SEVERITY_ORDER.index(f["severity"]) >= floor]
    non_blocking = [f for f in findings if _SEVERITY_ORDER.index(f["severity"]) < floor]
    return (not blocking), blocking, non_blocking


def _test_arbiter_prompt(title, criteria, *, instructions="", test_output="", change=""):
    """Rule on a FAILING test: is the code wrong, or did the test author encode the criteria wrong?

    Frozen tests are the moat — the implementer must never edit its own oracle. But a test that
    asserts something the criteria never required is unsatisfiable, and the run then burns every
    repair round against it. This is the independent third opinion that breaks that deadlock.
    """
    return (
        _ctx_block(title, instructions, criteria)
        + "You are an INDEPENDENT test arbiter. You did NOT write this code and did NOT write "
        "these tests. A test is currently FAILING. Decide ONE thing: is the IMPLEMENTATION wrong, "
        "or is the TEST wrong?\n\n"
        "A test is WRONG only when it asserts something the task and acceptance criteria never "
        "required — it invents a stricter contract, hard-codes an incidental detail (exact "
        "wording, ordering, whitespace, a specific colour), or contradicts a stated criterion. "
        "A test is RIGHT — and the code is wrong — whenever it checks something the criteria DO "
        "require, even if satisfying it is awkward or the implementation would need real work.\n\n"
        "Default to CODE_WRONG. Ruling TEST_WRONG lets an agent rewrite the oracle, so it demands "
        "a concrete, quotable justification tied to the criteria. If you are unsure, rule "
        "CODE_WRONG.\n\n"
        f"FAILING TEST OUTPUT:\n```\n{(test_output or '')[:4000]}\n```\n\n"
        + _change_clause(change)
        + "Do NOT edit any files. End your response with EXACTLY one final line:\n"
        "  ARBITER: CODE_WRONG — the test faithfully encodes the criteria; fix the implementation.\n"
        "  ARBITER: TEST_WRONG <test name or file> — the test over-specifies beyond the criteria.\n"
    )


def _parse_arbiter(summary: str) -> tuple[str, str]:
    """-> (ruling, target). Anything unparseable is CODE_WRONG: never amend the oracle by accident."""
    m = re.search(r"(?im)^\s*ARBITER:\s*(CODE_WRONG|TEST_WRONG)\b[\s—:-]*(.*)$", summary or "")
    if not m:
        return "code_wrong", ""
    return m.group(1).lower(), (m.group(2) or "").strip()[:200]


def _test_amend_prompt(criteria, ruling_target: str, rationale: str) -> str:
    """Narrow mandate for the ONE agent allowed to touch a test — and only the arbitrated one."""
    return (
        "An independent arbiter ruled that a TEST is wrong: it asserts more than the agreed "
        f"acceptance criteria require.\n\nTEST AT FAULT: {ruling_target or '(see rationale)'}\n"
        f"ARBITER'S RATIONALE:\n{(rationale or '')[:2000]}\n\n"
        f"ACCEPTANCE CRITERIA (the ONLY contract that counts):\n{criteria}\n\n"
        "Amend ONLY that test, and ONLY so far as to stop asserting what the criteria never "
        "required. Do NOT weaken any assertion that maps to a criterion, do NOT delete the test, "
        "and do NOT touch the implementation — you are fixing the oracle, not making it pass. "
        "State in one line what you changed and why it still enforces the criteria."
    )


def _already_spawned_followup(engine, parent_run_id: str) -> bool:
    """Has this parent already produced its follow-up task?

    ONCE PER PARENT, NOT ONCE PER ATTEMPT. The existing guard stops a follow-up spawning its
    own follow-up, which bounds the DEPTH of the chain and says nothing about its WIDTH. The
    finalisation path can run more than once for the same run — a lease lost and requeued, a
    job redelivered, a retry — and each pass spawned another identical task.

    Observed: sixteen copies of "Follow-up: deferred review findings — Build path verification",
    all cancelled. Two costs, and the second is worse than the first. Model budget, obviously.
    But `list_code_tasks` returns newest-first, so sixteen duplicates fill the window the chat
    agent reads, and it told the operator "all recent runs are cancelled and have no pull
    request" while the completed run carrying PR #161 sat just below the fold. The agent was
    not wrong about what it could see; it could not see the answer.

    Keyed on the event the spawn already writes, so there is no new state to keep in step.
    """
    from .core import has_event

    try:
        with _transaction(engine) as c:
            return has_event(c, parent_run_id, "code_task.deferred_findings_task")
    except Exception:  # noqa: BLE001 — a check that cannot run must not block a shipped PR
        logger.exception("could not check for an existing follow-up on %s", parent_run_id)
        return False


def _spawn_deferred_findings_task(
    engine, settings, jobs, *, project_id, task_id, spec, base_branch,
    findings: list[dict], parent_run_id: str, parent_title: str,
    parent_root: Optional[str] = None, parent_origin: Optional[dict] = None,
):
    """Queue sub-threshold review findings as their own code task on the shipped branch.

    Deferring a finding must never mean losing it: this is what makes "ship the phase over a
    cosmetic defect" safe, because the defect leaves as tracked work rather than as a note in a
    receipt nobody reads. Best-effort by construction — a PR that is already open must not be
    failed by bookkeeping.
    """
    lines = [f"- [{f['severity'].upper()}] {f['text']}"
             for f in (findings or []) if f.get("text")]
    if not lines:
        return None
    from .code_task_service import start_code_task
    body = "\n".join(lines[:20])
    with _transaction(engine) as c:
        return start_code_task(
            c, project_id=project_id, task_id=task_id,
            title=f"Follow-up: deferred review findings — {parent_title}"[:160],
            instructions=(
                "An adversarial review of the parent change graded these findings BELOW the "
                "blocking threshold, so the change shipped and they were deferred to this task. "
                "Fix them on top of the shipped branch.\n\n"
                f"DEFERRED FINDINGS (from run {parent_run_id}):\n{body}\n\n"
                "Fix ONLY these. Do not refactor beyond them and do not weaken existing tests."),
            acceptance_criteria=[f"Deferred finding addressed: {ln[:180]}" for ln in lines[:20]],
            test_commands=spec.get("test_commands") or [],
            owner=spec.get("owner"), repo=spec.get("repo"),
            base_branch=base_branch,
            # Recursion guard: a follow-up never spawns its own follow-up, so a reviewer that
            # always emits one NIT can't generate an unbounded chain of tasks (and spend).
            #
            # LINEAGE. A follow-up fixes findings ON TOP OF the shipped branch, so it belongs to
            # the same lineage as the run it came from — it is not new work. Spawning it as its
            # own root left root_run_id NULL, and everything keyed on lineage then saw a task
            # with one file to its name: its preview could not tell which app it belonged to and
            # fell back to whatever the repo contained (it showed Sudoku for an N-Queens
            # follow-up), and it cloned the base branch instead of the branch holding the code
            # it was sent to fix. Carrying the root fixes both at the source.
            finalize_ctx={"deferred_from_run": parent_run_id, "is_followup": True,
                          "root_run_id": parent_root or parent_run_id,
                          "parent_run_id": parent_run_id,
                          # WHERE THE APP CAME FROM IS A PROPERTY OF THE APP.
                          #
                          # `retry_code_task` already carries `origin` forward; a follow-up is
                          # created fresh and did not, so it had none. The card's spine narrates
                          # the newest COMPLETED run — which for a shipped app is usually this
                          # follow-up — and therefore reported "Research: none / Spec: none" for
                          # an app that came from a panel and has a PRD. Same reasoning as the
                          # root_run_id note above: lineage must survive the hop.
                          **({"origin": parent_origin} if parent_origin else {})},
            jobs=jobs, profile=spec.get("profile"),
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


def _lane_api_key(
    settings: Settings,
    engine_name: str,
    attr: str,
    *,
    connection=None,
    project_id: str | None = None,
):
    """SecretRef first when a project is bound; settings attr is last-resort only."""
    if connection is not None and project_id:
        from .credential_resolve import resolve_project_credential
        from .model_rate_limiter import provider_of

        return resolve_project_credential(
            connection,
            settings,
            project_id=project_id,
            provider=provider_of(engine_name),
            env_fallback_attr=attr,
        )
    return getattr(settings, attr, None)


def _guest_engine_fields(
    settings: Settings,
    engine_name: str,
    model,
    *,
    connection=None,
    project_id: str | None = None,
) -> dict:
    """Map a routed engine lane onto guest_spec credential fields (hybrid substrate).
    OpenAI-compatible lanes (openrouter/groq/deepseek) run as the guest 'openai' engine
    pointed at the lane's base_url; claude_code rides the OAuth token; echo = no agent
    (clone + run test_commands only, used for the regression stage).

    When ``connection`` + ``project_id`` are supplied, keys come from
    ``resolve_project_credential`` (SecretRef first, env last-resort).
    """
    def key(attr: str):
        return _lane_api_key(
            settings, engine_name, attr, connection=connection, project_id=project_id
        )

    if engine_name == "echo":
        return {"engine": "echo"}
    if engine_name == "claude_code":
        return {"engine": "claude_code",
                "claude_oauth_token": key("claude_code_oauth_token")}
    if engine_name == "openrouter":
        return {"engine": "openai", "openai_api_key": key("openrouter_api_key"),
                "openai_base_url": settings.openrouter_base_url, "model": model}
    if engine_name == "groq":
        return {"engine": "openai", "openai_api_key": key("groq_api_key"),
                "openai_base_url": settings.groq_base_url, "model": model}
    if engine_name == "deepseek":
        return {"engine": "openai", "openai_api_key": key("deepseek_api_key"),
                "openai_base_url": settings.deepseek_base_url, "model": model}
    if engine_name == "ollama":
        # Local-model lane. Note for sandboxed guests: "localhost" is the guest,
        # not the host — point OLLAMA_BASE_URL at a host-reachable address
        # (e.g. http://host.docker.internal:11434/v1) when tasks run in a VM.
        return {"engine": "openai", "openai_api_key": key("ollama_api_key"),
                "openai_base_url": settings.ollama_base_url, "model": model}
    if engine_name == "vllm":
        return {"engine": "openai", "openai_api_key": key("vllm_api_key"),
                "openai_base_url": settings.vllm_base_url, "model": model}
    if engine_name == "azure":
        # Explicit Azure frontier lane in a guest VM. A routed model (azure:<deploy>)
        # overrides the default deployment; no openai_base_url so the guest can't misroute.
        return {"engine": "openai", "azure_endpoint": settings.azure_openai_endpoint,
                "azure_deployment": model or settings.azure_openai_deployment,
                "azure_api_version": settings.azure_openai_api_version,
                "azure_key": key("azure_openai_key")}
    # native/openai/azure -> the platform lane, same env the simple path sends
    return {"engine": "openai", "openai_api_key": key("openai_api_key"),
            "openai_base_url": settings.openai_base_url,
            "model": model or settings.openai_model,
            "azure_endpoint": settings.azure_openai_endpoint,
            "azure_deployment": settings.azure_openai_deployment,
            "azure_api_version": settings.azure_openai_api_version,
            "azure_key": key("azure_openai_key")}


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
        # A PLAN GATE HOLDS ITS BUILD; IT DOES NOT TIME OUT. Every blocking decision gets a 24h
        # hold deadline, and an expired plan question dropped out of "Needs you" while the build
        # sat at "Needs you · plan" for weeks (goldeneye, 2026-10). No deadline: it waits for
        # the person, and every surface counts it (decisions.expire_overdue keeps it so).
        c.execute(sql("UPDATE decisions SET deadline_at = NULL WHERE id = :d"), {"d": dec_id})
        c.execute(sql("UPDATE code_task_runs SET status='blocked', phase='awaiting_plan_approval', "
                       "decision_id=:d, updated_at=now() WHERE id=:id"),
                  {"d": dec_id, "id": run_id})
    return {"status": "blocked", "done": True, "awaiting_plan_approval": True, "decision_id": dec_id}


# --------------------------------------------------------------------------- #
#  The governed job handler                                                   #
# --------------------------------------------------------------------------- #
def run_governed_code_task(engine, settings: Settings, jobs, run_id: str,
                           *, fencing_token: Optional[str] = None) -> dict:
    """One governed attempt, with the MCP servers of the agent it runs as (skills-mcp E6).

    A task started as an agent (`spec.role_key`) hands every seat's harness exactly that
    agent's held servers (code_task_mcp); the file carrying their sign-in is removed however
    the attempt ends."""
    from .code_task_mcp import prepare

    mcp = None
    with _transaction(engine) as c:
        run = load_code_task_run(c, run_id)
        role = str(((run or {}).get("spec") or {}).get("role_key") or "").strip()
        if run is not None and role:
            mcp = prepare(c, settings, run["project_id"], role)
    from . import owned_process
    try:
        # EVERYTHING THIS BUILD STARTS IS ITS OWN (stability spec §4a): agents, review seats,
        # commands, the serve proof — so Stop ends all of it, and nothing it started outlives it.
        with owned_process.owner_scope("code_run", run_id,
                                       project_id=(run or {}).get("project_id"), fresh=True):
            return _run_governed_code_task(engine, settings, jobs, run_id,
                                           fencing_token=fencing_token, mcp=mcp)
    finally:
        if mcp is not None:
            mcp.cleanup()
        # Whatever is still running under this build's name ends with it (a background child
        # an exception unwound past, a seat the panel deadline gave up on).
        owned_process.terminate_owner(run_id, reason="run_ended", grace=2.0)


def _run_governed_code_task(engine, settings: Settings, jobs, run_id: str,
                            *, fencing_token: Optional[str] = None, mcp: Any = None) -> dict:
    from .run_fencing import RunFenced, assert_owner, claim_run, hash_token  # noqa: F401
    _fence = hash_token(fencing_token)
    with _transaction(engine) as c:
        run = load_code_task_run(c, run_id)
        if run is not None and _fence:
            # Unconditional by design: the control plane already decided who holds the lease,
            # so by the time we execute we ARE the owner and any prior executor is stale.
            claim_run(c, run_id, _fence)
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
    # Rules a ROLE mandated, loaded once for the whole run so every stage's shared preamble is
    # byte-identical (the prompt cache depends on that) and so a rule added mid-build cannot
    # change the instructions between stages of the same task.
    from .platform_standards import load_mandated_standards
    from .platform_standards import load_reference_links
    with engine.begin() as _c:
        _n_mandated = load_mandated_standards(_c, project_id)
    _n_refs = load_reference_links((spec or {}).get("reference_links"))
    if _n_refs:
        logger.info("code_task.reference_links run=%s count=%s", run_id, _n_refs)
    if _n_mandated:
        logger.info("code_task.mandated_standards run=%s count=%s", run_id, _n_mandated)
    ports = _engine_ports()
    budget = ports.budget
    # Resolved here, before the factory is built, so the clone/branch/PR target and the token
    # below are the SAME decision rather than two that can drift apart.
    _owner = spec.get("owner") or settings.target_github_owner
    _repo = spec.get("repo") or settings.target_github_repo
    sf = SoftwareFactoryService(settings, owner=_owner, repo=_repo, ports=ports)
    # Prefer Mission Control primary binding credentials when present (P0-6 dual-plane).
    try:
        with _transaction(engine) as c:
            plane = sf.bind_project_github(
                c, project_id, owner=_owner, repo=_repo,
                selector=spec.get("repository") or (
                    f"{_owner}/{_repo}" if _owner and _repo else None
                ),
            )
            if plane.get("owner") and plane.get("repo"):
                _owner = plane["owner"]
                _repo = plane["repo"]
            emit_event(
                c, project_id, "code_task.github_plane", "boss", "code_task_run",
                run_id,
                {
                    "github_plane": plane.get("github_plane"),
                    "github_binding_id": plane.get("github_binding_id"),
                    "full_name": plane.get("full_name"),
                    "source": plane.get("source"),
                },
            )
    except Exception:  # noqa: BLE001 — never block a run on plane bookkeeping
        pass
    title = run["title"]
    # APP-3 (#93): when the task didn't specify tests, resolve the target from the project type —
    # web/TS gets build/typecheck/lint/e2e (the scaffold's scripts) so the repair loop optimizes a
    # web-native signal; Python still defaults to unittest. Explicit test_commands always win.
    #
    # THE WHOLE LIST. Taking [0] was leftover from when the web target was one
    # `&&` string. After 262d3482 split it so the sandbox could run it, [0] is
    # `npm install` and `_run_verify` treated a successful install as verified.
    verify_commands = verification_target.resolve_authoritative_test_commands(spec, title)
    # Display / prompts only. Never pass this joined string to sandbox.run —
    # the sandbox refuses `&&` and would otherwise execute only argv[0].
    test_cmd = " && ".join(verify_commands) if verify_commands else ""
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
    if resume_design is None:
        # RESUME ACROSS ATTEMPTS. A repair re-ran standards+spec+plan+review_plan every time —
        # four agent sessions (~10 min, ~$5 on the strong lane) re-deriving a plan for a task
        # that had not changed. Measured: every retry re-ran 10/10 stages and inherited 0 of the
        # parent's 42-44 state keys. NOTE: do NOT guard on `plan_ok` — it is computed as
        # ("REVISE" not in summary), and the plan reviewer emits "PLAN: REVISE" as a soft
        # suggestion even when it states the plan is sound, so plan_ok is false on EVERY run
        # measured, including crun_12ae7cbf which shipped PR #60 clean. Guarding on it would
        # silently disable resume. Inherit whenever a real plan exists; the repair feedback
        # still drives what changes, and the reviewer still judges the result.
        # RE-ENTRY INTO THE SAME RUN. A lease reclaim (worker restart, crash, expired lease)
        # re-executes the SAME code.task job from stage one — measured on crun_b4b6383d:
        # three full passes, 15 agent sessions, $6.83, never finishing. The design this run
        # already produced is persisted in its own state (see design_persisted), and nothing
        # read it: resume only ever consulted a PARENT's design via inherited_design.
        # A run must first ask what IT already did.
        own = prior_state.get("design")
        if isinstance(own, dict) and (own.get("plan") or "").strip():
            resume_design = own
            with _transaction(engine) as c:
                emit_event(c, project_id, "code_task.design_reused_own", "boss",
                           "code_task_run", run_id,
                           {"why": "re-entry after lease reclaim; skipping the design chain"})
        inherited = fctx.get("inherited_design")
        if resume_design is None and isinstance(inherited, dict) and (inherited.get("plan") or "").strip():
            resume_design = inherited
            with _transaction(engine) as c:
                emit_event(c, project_id, "code_task.design_resumed", "boss",
                           "code_task_run", run_id,
                           {"from_run": fctx.get("parent_run_id"),
                            "skipped_stages": ["standards", "spec", "plan", "review_plan"]})

    with _transaction(engine) as c:
        reservation = budget.reserve(
            c, project_id, category="code_task",
            amount_usd_micros=int(settings.code_task_budget_usd_micros),
            reason=f"governed code task: {title[:64]}", expires_in_minutes=45, job_id=job_id,
            risk_tier=spec.get("risk_tier"))
    if reservation.get("status") != "reserved":
        return refused_reservation(engine, jobs, run_id=run_id, project_id=project_id,
                                   reservation=reservation, fencing_token=fencing_token)
    reservation_id = reservation["reservation_id"]
    with _transaction(engine) as c:
        c.execute(sql("UPDATE code_task_runs SET reservation_id=:r WHERE id=:id"),
                  {"r": reservation_id, "id": run_id})

    owner, repo = _owner, _repo
    base_branch = spec.get("base_branch") or "main"
    # Feedback-iteration: attempts after the first share ONE branch derived from the root
    # run and clone the prior attempt's code, so the model builds ON prior work instead of
    # regenerating from base. The PR on that branch is reused (see create_pull_request).
    root_run = fctx.get("root_run_id") or run_id
    work_branch = f"boss/code/{root_run}"
    if root_run != run_id and sf.github.branch_exists(work_branch):
        base_branch = work_branch  # prior attempt's code is present in the clone
    app = GitHubAppAdapter(settings)
    # Shared decision — see repo_token. Prefer the bound MC adapter's token when the
    # factory plane is mission_control_binding so clone and PR use the same credentials.
    from .github_app_adapter import repo_token

    token = None
    if getattr(sf, "github_plane", None) == "mission_control_binding":
        try:
            token = sf.github._auth_token()  # noqa: SLF001 — intentional dual-plane share
        except Exception:  # noqa: BLE001
            token = None
    if not token:
        # Same third source as the ungoverned path: the credential this project registered in
        # Setup, after the App and the environment.
        with _transaction(engine) as c:
            token = repo_token(settings, owner, repo,
                               connection=c, project_id=project_id)

    provider = LocalSubprocessProvider(settings)
    # Lane defaults: builder engine for design/tests/implement, adversary engine for
    # review/security/consistency/mutation — the model that wrote the code isn't the one
    # judging it (removes self-review bias). VAG B2 layers per-STEP (engine, model) routing
    # on top: profile model_routing or spec["model_config"] may pin any step to any engine
    # (route_for_step), so e.g. implement runs native/deepseek while review stays claude_code.
    # Per-run model_config default (official POST engine=… or spend-cap failover)
    # wins over the process default so route_for_step / _route_escalated stay on
    # the pinned engine instead of silently falling back to claude_code.
    _mc = spec.get("model_config") if isinstance(spec.get("model_config"), dict) else {}
    _mc_default = _mc.get("default")
    _pinned_engine = ""
    if isinstance(_mc_default, dict):
        _pinned_engine = str(_mc_default.get("engine") or "").strip().lower()
    elif isinstance(_mc_default, str) and _mc_default.strip():
        _pinned_engine = _mc_default.split(":", 1)[0].strip().lower()
        # ASK THE REGISTRY, not a list of three. This allow-list predates the harness seam and
        # silently dropped every engine added since — a run pinned to "codex:gpt-5-codex" or
        # "openrouter:…" in string form fell back to the process default and ran somewhere
        # else, with nothing saying so.
        try:
            from .harness_registry import get_harness

            get_harness(_pinned_engine)
        except Exception:  # noqa: BLE001 — an unknown engine is not a pin
            _pinned_engine = ""
    # The project's choices, layered over the deployment's, ONCE — every `settings.*_engine`
    # read below and in the functions this calls then sees them. Credentials for several
    # providers can be connected at once, so which model fills which role is a decision
    # somebody makes in the console, not one frozen into the container at deploy time.
    from .project_settings import applied as _applied_settings

    from .setup_service import build_engine_for as _build_engine_for

    with _transaction(engine) as _c:
        settings = _applied_settings(_c, project_id, settings)
        # AND A DEFAULT THAT MATCHES THIS INSTALLATION. The chain used to end at the literal
        # "claude_code", so an install that had connected only an OpenRouter key shelled out
        # to a `claude` binary it does not have. This only speaks when the configured engine
        # cannot run here — see build_engine_for.
        _resolved_engine = _build_engine_for(_c, settings, project_id)
    build_engine = (
        _pinned_engine or _resolved_engine or settings.code_agent_engine or "claude_code"
    ).lower()
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
            with _transaction(engine) as c:
                a = make_code_agent(
                    settings,
                    engine=engine_name,
                    connection=c,
                    project_id=project_id,
                    mcp=mcp,
                )
            if redact_external:
                a.redact = True
            agents[engine_name] = a
        # BOSS-407: give the native lane's `bash` tool the SAME runner BOSS uses for
        # verification, so an agent-run command gets the sandbox's tool caches, the
        # repo-local node_modules/.bin on PATH and the process-group kill — not a second,
        # subtly different execution path. Attached per call because the sandbox is
        # provisioned after this closure is defined; it is idempotent.
        if command_container is not None:
            a.command_runner = command_container.as_command_runner()
        elif sandbox is not None and callable(getattr(sandbox, "run", None)):
            a.command_runner = (
                lambda cmd, timeout=None, _sb=sandbox: _sb.run(cmd, timeout=timeout)
            )
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

    # THE MODEL ROUTER (docs/product/MODEL_ROUTER.md). Every stage boundary asks it. In Fixed
    # mode (the default) it answers with exactly the pre-router resolution — route_for_step →
    # apply_budget_pressure → materialize_route, and the hard-problems lane on escalation — and
    # records why; in Smart mode it picks a tier from the task, floors and the escalation
    # ledger, then a source with headroom.
    from .model_router.stages import StageRouter

    stage_router = StageRouter(
        transaction=lambda: _transaction(engine), settings=settings, project_id=project_id,
        run_id=run_id, spec=spec, title=title, profile=profile, build_engine=build_engine,
        review_engine=review_engine, budget_state=_budget_state, fctx=fctx,
        risk_tier=risk_tier)

    def _take(step: str, decision) -> tuple:
        stage_routes[step] = {"engine": decision.engine, "model": decision.model,
                              "route_id": decision.id, "tier": decision.tier,
                              **({"budget_lite": True} if decision.budget_lite else {}),
                              **({"escalated": True} if decision.escalation else {})}
        # Every llm_calls row of this boundary carries the route that chose its model: by its
        # stage (`routes`), which also reaches the panel's worker threads, else the last one.
        set_llm_context(route_id=decision.id, routes=_route_ids())
        return decision.engine, decision.model

    def _route_ids() -> dict:
        return {s: r["route_id"] for s, r in stage_routes.items() if r.get("route_id")}

    def _route(step: str):
        """Resolve a step to (agent, model) through the router. Call from the MAIN thread
        only (the agent cache is not locked)."""
        eng, model = _take(step, stage_router.route(step))
        return _agent_for(eng), model

    def _route_escalated(step: str):
        """The step again, one boundary later, on the evidence that its route produced no
        usable output (write_tests) or that this is the final attempt (implement). Fixed mode:
        the hard-problems lane, as before; Smart mode: the escalation rules. MAIN thread only."""
        signal = "no_output" if step == "write_tests" else "final_attempt"
        eng, model = _take(step, stage_router.route(step, signal=signal,
                                                    previous=stage_routes.get(step)))
        return _agent_for(eng), model

    def _vm_escalation(step: str, escalate: bool, signal: str) -> str:
        """A micro-VM stage's escalation. Fixed: the hard-problems engine override, as before.
        Smart: the signal goes on the ledger and the stage routes normally (_route_names),
        so the escalation rules decide the tier."""
        if not escalate:
            return ""
        if stage_router.is_smart():
            stage_router.signal(step, signal)
            return ""
        return settings.code_agent_strong_engine

    def _route_names(step: str):
        """Like _route but returns (engine_name, model) without building a host agent —
        the micro-VM substrate maps the route onto guest engine env instead. (No budget
        pressure here, as before.)"""
        return _take(step, stage_router.route(step, budget=False))

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
    _app_id = verification_target.official_app_id(spec, fctx)

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
        }
        if _app_id:
            guest_spec["app_root"] = _app_id
        with _transaction(engine) as c:
            guest_spec.update(
                _guest_engine_fields(
                    settings,
                    eng_name,
                    model,
                    connection=c,
                    project_id=project_id,
                )
            )
        set_llm_context(stage=stage_name)
        # BURST CONTROL (#111 extended to the guest lane): a whole guest build is one long,
        # turn-dense Anthropic consumer. Dispatch it under the model rate limiter's per-provider
        # slot so concurrent guest builds are bounded and a rolling-window burst can't happen.
        # The guest surfaces a 429/usage-limit in its RESULT (not as an exception), so classify
        # res.error too — that feeds the AIMD backoff (halve concurrency + wait) on the next call.
        limiter = get_rate_limiter(settings)
        provider = provider_of(eng_name)
        # WAITING FOR THE LANE IS NOT THE SAME AS DOING THE WORK.
        #
        # Both used to land in the phase's wall time as one undifferentiated number, so a stage
        # that queued four minutes for a slot and then ran for one was indistinguishable from a
        # stage that worked for five. Measured across 70 runs, 71% of wall time was not model
        # latency — and no record said which part of it was queueing. These two numbers are how
        # that question gets answered from data instead of by inference.
        _stage_started = time.monotonic()
        with limiter.slot(provider) as _slot:
            _work_started = time.monotonic()
            res = mv.run_code_task(guest_spec, tenant_id=project_id)
            _work_ms = int((time.monotonic() - _work_started) * 1000)
            if is_rate_limit_error(getattr(res, "error", None) or getattr(res, "summary", "")):
                limiter.report_rate_limited(provider)
            else:
                limiter.report_ok(provider)
        _record_stage_timing(
            stage_name, step,
            waited_ms=int(getattr(_slot, "waited_seconds", 0.0) * 1000),
            work_ms=_work_ms,
            total_ms=int((time.monotonic() - _stage_started) * 1000),
            provider=provider, engine_name=eng_name,
        )
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
        sf.github.ensure_branch(work_branch, base_branch)
        for fc in files:
            sf.github.upsert_file(work_branch, fc.path, fc.content, message)
            accumulated[fc.path] = fc.content
        guest_base = work_branch

    def _declare_preview_app() -> None:
        """Record WHICH app this build produced, so no later step has to work it out.

        The preview used to re-derive this from the finished repository, and got it wrong five
        times in five different ways — each fix teaching the resolver one more shape while the
        shared sandbox repo gained another app on every run. The build is the only place that
        knows the answer for certain: `accumulated` is exactly the files this run wrote, before
        anything else is layered on top.

        Never raises and never blocks the PR. A build that cannot name its app is the case that
        already existed — the resolver still tries — so failing here would trade a working
        fallback for a lost delivery.
        """
        try:
            from .preview_service import DECLARED_APP_FILE, declare_app_entry
            # The files the PR will carry. On the local substrate `accumulated` holds only
            # what the last checkpoint pushed, which predates any fix round — the same tree
            # the app_serves gate just proved, not an older one.
            built = (dict(accumulated) if use_microvm
                     else {fc.path: fc.content for fc in sandbox.diff()})
            entry = declare_app_entry(list(built.keys()), built.get)
            if not entry:
                return
            sf.github.upsert_file(
                work_branch, DECLARED_APP_FILE, entry + "\n",
                f"governed: declare preview app ({entry})")
            accumulated[DECLARED_APP_FILE] = entry + "\n"
            with _transaction(engine) as c:
                emit_event(c, project_id, "code_task.preview_app_declared", "boss",
                           "code_task_run", run_id, {"entry": entry})
        except Exception as exc:  # noqa: BLE001 — a declaration must never sink a finished build
            logger.warning("could not declare the preview app for %s: %s", run_id, exc)

    _lease_owner_at_start: dict[str, Optional[str]] = {"v": None}

    def _lease_owner_now() -> Optional[str]:
        try:
            with _transaction(engine) as c:
                return c.execute(sql(
                    "SELECT lease_owner FROM jobs WHERE id=:j"), {"j": job_id}).scalar()
        except Exception:  # noqa: BLE001
            return _lease_owner_at_start["v"]  # unknown -> assume unchanged, never false-abort

    def _emit_cmd(label: str, result) -> None:
        """Put one sandbox command's cost in the ACTIVITY LOG, not just the container log.

        A phase reading "Engineer implementing — 27m46s — no model call" is accurate and
        useless: it says the model was not the cost without saying what was. The cost is here —
        `npm install`, the Playwright browser download, the test command — and until now none
        of it reached the log an operator is told to look at, so the only honest answer to
        "where did the time go" was "open a shell on the box".

        A slow command is a WARN so it stands out from the ordinary ones without needing a
        filter. Never raises: a diagnostic that can fail a build is worse than no diagnostic.
        """
        try:
            ms = int(getattr(result, "duration_ms", 0) or 0)
            with _transaction(engine) as c:
                emit_event(
                    c, project_id,
                    "sandbox.command_slow" if ms >= 60_000 else "sandbox.command",
                    "boss", "code_task_run", run_id,
                    {"label": label,
                     "command": str(getattr(result, "command", ""))[:200],
                     "duration_ms": ms,
                     "seconds": round(ms / 1000, 1),
                     "exit_code": getattr(result, "exit_code", None),
                     "timed_out": bool(getattr(result, "timed_out", False))},
                )
        except Exception:  # noqa: BLE001 — instrumentation never fails a run
            pass

    def _record_stage_timing(stage_name: str, step: str, *, waited_ms: int, work_ms: int,
                             total_ms: int, provider: str, engine_name: str) -> None:
        """Append one stage's wait/work split to the run, and log it.

        `phase_timings` says a phase took five minutes. It cannot say whether those were spent
        queueing for a model lane, booting a VM, or generating code — and the three have
        completely different fixes. This is the missing half: per stage, how long the lane made
        it wait before any work started.

        Never fails a run. A diagnostic that can break a build is worse than no diagnostic.
        """
        try:
            entry = {
                "stage": stage_name, "step": step,
                "waited_ms": int(waited_ms), "work_ms": int(work_ms),
                "total_ms": int(total_ms),
                "provider": provider, "engine": engine_name,
            }
            queue_pct = (100.0 * waited_ms / total_ms) if total_ms else 0.0
            logger.info(
                "stage timing run=%s stage=%s waited=%.1fs work=%.1fs (%.0f%% of it was queueing)",
                run_id, stage_name, waited_ms / 1000, work_ms / 1000, queue_pct,
            )
            with _transaction(engine) as c:
                # IN THE ACTIVITY LOG, not only in a container log nobody can read. "Where did
                # the time go" has to be answerable from the screen the operator is sent to.
                emit_event(
                    c, project_id,
                    "stage.queued" if waited_ms >= 30_000 else "stage.timing",
                    "boss", "code_task_run", run_id,
                    {"stage": stage_name, "step": step,
                     "waited_seconds": round(waited_ms / 1000, 1),
                     "work_seconds": round(work_ms / 1000, 1),
                     "queueing_pct": round(queue_pct),
                     "provider": provider, "engine": engine_name},
                )
                c.execute(
                    sql("UPDATE code_task_runs SET finalize_ctx = "
                        "COALESCE(finalize_ctx, '{}'::jsonb) || jsonb_build_object("
                        "  'stage_timings', COALESCE(finalize_ctx->'stage_timings', '[]'::jsonb)"
                        "    || jsonb_build_array(CAST(:entry AS JSONB))), "
                        "updated_at = now() WHERE id = :id"),
                    {"entry": json.dumps(entry), "id": run_id},
                )
        except Exception:  # noqa: BLE001 — instrumentation never fails a run
            pass

    def _phase(c, name: str) -> None:
        """Set the run's phase under the fence.

        `_set_phase` wrote unguarded from every stage, which is why the console showed the
        phase moving BACKWARDS (implement -> write_tests) while two executions raced. A fenced
        executor's phase write now affects 0 rows and raises instead of rewriting history.
        """
        # Fence the write AND keep everything _set_phase did. My first version wrote only
        # `phase = :ph` and silently dropped phase_timings/phase_started_at, so the console's
        # per-step timings — the operator's main way of seeing where a task spends its time —
        # went blank on every run after the fencing change. A guard that quietly removes
        # behaviour is a regression wearing a safety jacket.
        # EVERY PHASE IS A STAGE BOUNDARY (factory test D1). Stop was only checked before
        # write_tests and implement, so a build stopped during its checks ran on through review
        # and opened its PR. Reads only (MVCC): it never waits on the row `c` may hold.
        _abort_if_cancelled(f"phase->{name}")
        from .run_fencing import guarded_update
        guarded_update(
            c, run_id, _fence,
            """phase = :ph,
               finalize_ctx = COALESCE(finalize_ctx, '{}'::jsonb)
                 || jsonb_build_object('phase_started_at', to_jsonb(now()))
                 || CASE
                      WHEN finalize_ctx ? 'phase_started_at' AND phase IS NOT NULL THEN
                        jsonb_build_object(
                          'phase_timings',
                          COALESCE(finalize_ctx->'phase_timings', '[]'::jsonb)
                          || jsonb_build_array(jsonb_build_object(
                               'phase', phase,
                               'ms', GREATEST(0, EXTRACT(EPOCH FROM (
                                      now() - (finalize_ctx->>'phase_started_at')::timestamptz
                                    )) * 1000)::bigint)))
                      ELSE '{}'::jsonb
                    END""",
            {"ph": name}, where=f"phase->{name}")

    def _close_phase() -> None:
        """Bank the CURRENT phase's duration, without starting another one.

        A phase is only ever timed by the phase that FOLLOWS it, so the last one is never
        recorded: the run ends through `_fenced_status(..., "phase='done'")`, which is raw SQL
        that bypasses `_phase`. `opening_pr` — pushing the branch, opening the PR, evaluating
        gates — therefore contributed nothing to `phase_timings` on every run ever.

        Measured on crun_3659274905154: phases summed to 13.6m against 16.3m of wall clock, and
        the missing 2.7m sat between `code_task.gate_advisory` and `code_task.pr_opened` with no
        phase, no turns and no log line. 17% of the run, invisible — indistinguishable, from the
        console, from the stalls this whole investigation was about.

        Deliberately does NOT write `phase`. When another execution has already finished this
        run, overwriting its verdict is exactly what the terminal-write guard exists to prevent;
        appending a timing entry is safe either way.
        """
        from .run_fencing import guarded_update
        try:
            # ITS OWN TRANSACTION, COMMITTED BEFORE THE TERMINAL WRITE.
            #
            # The first version took a connection and was called inside the same `with
            # _transaction(...)` block as `_fenced_status` — which opens a SECOND connection and
            # updates the SAME row. The outer transaction held a row lock on the run, the inner
            # one waited for it, and the outer could not commit until the inner returned. The
            # run hung in `opening_pr` for 26 minutes with no error, its pull request already
            # created and its receipt already written. Nothing before this wrote to
            # `code_task_runs` from that block, which is why the nesting had been harmless.
            with _transaction(engine) as c:
                guarded_update(
                    c, run_id, _fence,
                    """finalize_ctx = COALESCE(finalize_ctx, '{}'::jsonb)
                     || CASE
                          WHEN finalize_ctx ? 'phase_started_at' AND phase IS NOT NULL THEN
                            jsonb_build_object(
                              'phase_timings',
                              COALESCE(finalize_ctx->'phase_timings', '[]'::jsonb)
                              || jsonb_build_array(jsonb_build_object(
                                   'phase', phase,
                                   'ms', GREATEST(0, EXTRACT(EPOCH FROM (
                                          now() - (finalize_ctx->>'phase_started_at')::timestamptz
                                        )) * 1000)::bigint)))
                          ELSE '{}'::jsonb
                        END""",
                    {}, where="phase->close")
        except Exception:  # noqa: BLE001 — a timing entry must never cost a finished run its PR
            pass

    def _fenced_status(status: str, extra_set: str = "", params: Optional[dict] = None) -> bool:
        """Set the run's status under BOTH the fence and the state machine.

        Returns False when the transition is refused because the run is already terminal —
        which means another execution finished it, and this one must not overwrite that.
        Raises RunFenced (-> CodeTaskCancelled) when this execution has been fenced.
        """
        from .run_fencing import guarded_status
        with _transaction(engine) as c:
            return guarded_status(c, run_id, _fence, status, extra_set, params)

    def _abort_if_cancelled(where: str) -> None:
        """Stop a run the operator cancelled. Cancellation was a DB update nobody read: one
        cancelled run kept executing for 40 minutes and $6.73 afterwards, and cancelled runs
        later overwrote their own status. Checked at stage boundaries so it costs one cheap
        query per stage and never interrupts an agent mid-call."""
        try:
            with _transaction(engine) as c:
                st = c.execute(sql("SELECT status FROM code_task_runs WHERE id=:id"),
                               {"id": run_id}).scalar()
        except Exception:  # noqa: BLE001 — never let the check itself break a run
            return
        # ANY terminal status stops this executor, not just `cancelled`.
        # crun_ccfc0212 was marked FAILED with its job deadlettered, and kept working: it
        # spent a further $5.63 across review_code, write_tests and a $4.01 implement,
        # delivering to a run that had already ended. Cancellation was covered and fencing was
        # covered; "the run is finished and I am still going" was not — the same half-covered
        # shape as the PR rescue being wired only to crashes.
        if st in ("cancelled", "failed", "completed", "superseded"):
            raise CodeTaskCancelled(
                f"run is already terminal ({st}); stopping this executor at {where}")
        # A STOP PRESSED WHILE THIS EXECUTOR HELD THE RUN ROW is a request row, not a status
        # (code_task_service.cancel_run, factory test D1): honoured here, at the boundary.
        try:
            from .code_task_service import pending_stop
            with _transaction(engine) as c:
                stop = pending_stop(c, run_id)
        except Exception:  # noqa: BLE001
            stop = None
        if stop:
            raise CodeTaskCancelled(f"stopped by {stop.get('actor')}; stopping at {where}")
        # DUPLICATE-EXECUTION GUARD. If another worker has taken this job's lease, THIS
        # execution is a duplicate and must stop. Measured on job_2fbe88e6: a lease expired,
        # a second worker reclaimed it, the first kept running, and both executed the same
        # stages — two write_tests completed 29 seconds apart, 6 agent processes on one task,
        # 87 minutes with no implementation. The worker-side 409 check catches this only when
        # a heartbeat happens to fire; checking at every stage boundary makes it structural.
        # Fencing is the authority now: if another execution has claimed this run, stop.
        # The stage-boundary check is belt-and-braces — the guarded writes below would reject
        # us anyway, but failing fast here avoids paying for a whole stage first.
        try:
            with _transaction(engine) as c:
                assert_owner(c, run_id, _fence, where)
        except RunFenced as exc:
            raise CodeTaskCancelled(str(exc)) from exc

    def _rescue_pr(reason: str) -> Optional[str]:
        """Open a [partial] PR for whatever this run built. Safe to call from ANY exit.

        THE DELIVERABLE IS THE PR. This was first written inline in the `except Exception`
        handler, which covered crashes and nothing else — five other exits (no_test_files,
        not_a_code_task, budget denied, gate override, PR-step exhausted) `return` directly
        and skipped it entirely, so a run could still hand the operator an error string while
        its code sat in a sandbox about to be deleted.

        Idempotent: propose_pull_request dedupes on code:task:<run_id>, so calling it here and
        again later cannot open two PRs. Returns the url or None; never raises.
        """
        try:
            n = _checkpoint("exit")
            if not n:
                return None
            with _transaction(engine) as c:
                pr = sf.propose_pull_request(
                    c, project_id, task_id, reservation_id=None,
                    dedupe_key=f"code:task:{run_id}", title=f"[partial] {title}",
                    branch_name=work_branch,
                    summary=("INCOMPLETE — this run did not finish.\n\n"
                             f"It stopped because: {reason}\n\n"
                             f"The {n} file(s) it had written are on this branch, so the work is "
                             "not lost and can be reviewed or continued with a retry. The quality "
                             "gates did NOT run to completion — treat this as work-in-progress, "
                             "not a reviewed change."),
                    files=([FileChange(path=p, content=content)
                            for p, content in accumulated.items()]
                           if use_microvm else sandbox.diff()),
                    created_by_job_id=job_id, actual_usd_micros=cost)
                if pr.get("status") in ("created", "succeeded"):
                    url = pr.get("pull_request_url") or pr.get("url")
                    c.execute(sql("UPDATE code_task_runs SET pull_request_url=:u, updated_at=now() "
                                  "WHERE id=:id AND pull_request_url IS NULL"),
                              {"u": url, "id": run_id})
                    emit_event(c, project_id, "code_task.partial_pr_opened", "boss",
                               "code_task_run", run_id,
                               {"url": url, "files": n, "reason": reason[:200]})
                    return url
        except Exception as exc:  # noqa: BLE001 — a failed rescue must never mask the real exit
            try:
                with _transaction(engine) as c:
                    emit_event(c, project_id, "code_task.partial_pr_failed", "boss",
                               "code_task_run", run_id,
                               {"error": f"{type(exc).__name__}: {exc}"[:200]})
            except Exception:  # noqa: BLE001
                pass
        return None

    def _mark_stage_done(stage: str) -> None:
        """Record that a stage finished, so a resumed attempt can skip it.

        Paired with _checkpoint: the checkpoint preserves the WORK, this preserves the
        KNOWLEDGE of what is already done. Without both, a retry after a provider spend-limit
        re-derives stages whose output is already sitting on the branch.
        """
        try:
            with _transaction(engine) as c:
                c.execute(sql(
                    "UPDATE code_task_runs SET finalize_ctx="
                    "COALESCE(finalize_ctx,'{}'::jsonb) || jsonb_build_object("
                    "  'completed_stages', "
                    "  COALESCE(finalize_ctx->'completed_stages','[]'::jsonb) || to_jsonb(:s::text)"
                    "), updated_at=now() WHERE id=:id"),
                    {"s": stage, "id": run_id})
        except Exception:  # noqa: BLE001 — bookkeeping must never fail a run
            pass

    def _checkpoint(label: str) -> int:
        """Push whatever the sandbox holds to the work branch. Best-effort, never fatal.

        WHY THIS EXISTS. On the micro-VM substrate each stage commits its files (_commit_stage
        above). On the LOCAL substrate nothing was pushed until the PR was opened, so the whole
        run lived in an ephemeral temp dir: a provider spend-limit, a timeout, or a crash threw
        away every completed stage. Measured: four runs died on "You've hit your monthly spend
        limit" with no branch and no recorded files, and a retry then re-ran write_tests and
        implement from nothing. `write_tests` alone is 16-26 turns and $1.45-$3.01.

        Checkpointing after each stage means a failure costs the CURRENT stage, never the run.
        """
        if use_microvm:
            return 0  # that path already commits per stage
        try:
            files = sandbox.diff()
        except Exception:  # noqa: BLE001 — a dead sandbox must not mask the real failure
            return 0
        if not files:
            return 0
        try:
            sf.github.ensure_branch(work_branch, base_branch)
            for fc in files:
                sf.github.upsert_file(work_branch, fc.path, fc.content,
                                      f"governed: checkpoint after {label} — {title[:50]}")
                accumulated[fc.path] = fc.content
            with _transaction(engine) as c:
                c.execute(sql(
                    "UPDATE code_task_runs SET finalize_ctx=COALESCE(finalize_ctx,'{}'::jsonb) "
                    "|| jsonb_build_object('work_branch', CAST(:b AS text), "
                    "'checkpointed_after', CAST(:s AS text)), updated_at=now() WHERE id=:id"),
                    {"b": work_branch, "s": label, "id": run_id})
                emit_event(c, project_id, "code_task.work_checkpointed", "boss",
                           "code_task_run", run_id,
                           {"after": label, "files": len(files), "branch": work_branch})
            return len(files)
        except Exception as exc:  # noqa: BLE001 — losing a checkpoint must not fail the run
            with _transaction(engine) as c:
                emit_event(c, project_id, "code_task.checkpoint_failed", "boss",
                           "code_task_run", run_id,
                           {"after": label, "error": f"{type(exc).__name__}: {exc}"[:200]})
            return 0

    def _finish_vms(success: bool) -> None:
        """Hold successful stage VMs for a bounded rootfs-preview window; retain failed
        VMs for inspection. The sandbox service owns TTL reaping in both cases."""
        if use_microvm and mv and success:
            for sid in guest_vm_ids:
                mv.hold(sid, ttl_seconds=7200, reason="governed build available for preview")

    def _honour_cancel(exc):
        # Operator intent, not a failure: stop, keep the work on the branch, release budget.
        _finish_vms(success=False)
        try:
            _checkpoint("cancelled")
        except Exception:  # noqa: BLE001
            pass
        with _transaction(engine) as c:
            _release(budget, c, project_id, reservation_id, "cancelled by operator", run_id=run_id)
            emit_event(c, project_id, "code_task.cancel_honoured", "boss", "code_task_run",
                       run_id, {"detail": str(exc)[:200]})
        try:
            # The stop that was only a request becomes the run's status, now that nothing of
            # this executor holds the row.
            from .code_task_service import honour_stop_request
            with _transaction(engine) as c:
                honour_stop_request(c, run_id)
        except Exception:  # noqa: BLE001 — the executor has stopped either way
            pass
        return {"status": "cancelled", "done": True, "reason": str(exc)}

    sandbox = None
    review_sandbox = None
    command_container = None  # BOSS-410, only when the docker command runtime is selected
    cost = 0
    try:
        sandbox = provider.acquire(task_id=run_id, tenant_id=project_id)
        with _transaction(engine) as c:
            _phase(c, "provisioning")
        sandbox.provision(owner, repo, base_branch=base_branch,
                          work_branch=work_branch, token=token)
        root = sandbox.path
        # BOSS-410: with the docker command runtime selected, agent-authored commands run in
        # a network-less container with the workspace bind-mounted, rather than as a
        # subprocess in the control plane's own container. One container per task. A failure
        # to start is loud — never a silent downgrade to the weaker path.
        if str(getattr(settings, "sandbox_command_runtime", "subprocess")).lower() == "docker":
            from .container_runner import ContainerCommandRunner

            command_container = ContainerCommandRunner(root, settings, task_id=run_id)
            command_container.start()
            logger.info("code_task.command_container run=%s id=%s", run_id,
                        (command_container.container_id or "")[:12])
        # BOSS-415: a bake-off task may ship the codebase it starts from, so a brownfield
        # task ("find where this lives among many files") is deterministic instead of
        # depending on what earlier runs left behind. Normal runs carry no fixture.
        _fixture = bakeoff_fixture_for_run(run.get("finalize_ctx"))
        if _fixture:
            _staged = stage_bakeoff_fixture(root, _fixture)
            logger.info("code_task.fixture_staged run=%s files=%s", run_id, len(_staged))
        # THE REPO'S OWN DECLARATION, READ FROM THE CLONE. The target above was resolved before
        # there was a checkout, so it could only guess from the task text. A monorepo that ships
        # boss.verify.yaml (BOSS itself does) declares its suites; a pytest project gets pytest,
        # not a unittest discover that collects nothing. Explicit test_commands still win.
        # PINNED HERE, BEFORE ANY AGENT RUNS: every later verify plans from this copy, never
        # from the tree the implementer edits (see verification_target.PinnedVerifyManifest).
        _pinned_manifest = verification_target.pin_verify_manifest(root)
        _gate_plan0 = verification_target.resolve_gate_plan(
            spec, title, root, app_id=_app_id, pinned=_pinned_manifest)
        if _gate_plan0.source != "task":
            verify_commands = list(_gate_plan0.commands)
            test_cmd = ((verification_target.manifest_display(root, _pinned_manifest)
                         if _gate_plan0.from_manifest else " && ".join(verify_commands))
                        or test_cmd)
        _web_verify = (
            verification_target.detect_kind(spec, title) == "web"
            or str(test_cmd or "").startswith("npm ")
            or bool(_app_id)
        ) and not _gate_plan0.from_manifest
        last_verify_cwd = None
        last_verify_output = ""

        def _changed_paths() -> list[str]:
            """Every path this run has changed: what is checkpointed plus the live tree."""
            paths = set(accumulated)
            if not use_microvm:
                try:
                    paths.update(sandbox.diff_summary().paths)
                except Exception:  # noqa: BLE001 — scope falls back to the whole suite
                    pass
            return sorted(paths)

        def _run_manifest_verify(plan, *, record: bool):
            """boss.verify.yaml: the affected suites, from the repo root, no scaffold repair."""
            nonlocal last_verify_output
            from .sandbox import CommandResult

            if plan.error or not plan.commands:
                # A manifest that does not validate fails closed with its own message. A plan
                # that runs nothing passes only when nothing it leaves out is code (a docs-only
                # change); changed code no test covers fails below.
                failed = bool(plan.error)
                result = CommandResult(
                    command="", argv=[], exit_code=1 if failed else 0, duration_ms=0,
                    timed_out=False, stdout="" if failed else plan.description + "\n",
                    stderr=plan.description if failed else "")
                results = [result]
            else:
                results = verification_target.run_steps_until_failure(
                    sandbox, plan.steps, timeout=plan.timeout_seconds)
                result = results[-1]
            if result.ok and plan.uncovered:
                # Zero tests ran for these files: that is no evidence, not a pass.
                from .verify_manifest import uncovered_message

                message = uncovered_message(plan.uncovered)
                result = CommandResult(
                    command="", argv=[], exit_code=1, duration_ms=0, timed_out=False,
                    stdout="", stderr=message)
                results.append(result)
            output = (
                f"[boss] {plan.description}\n"
                + "".join(((item.stdout or "") + "\n" + (item.stderr or "") + "\n")
                          for item in results)
            )[-4000:]
            if record:
                last_verify_output = output
            return result

        def _run_verify(*, record: bool = True):
            """Authoritative test command, scoped to the official app directory."""
            nonlocal last_verify_cwd, last_verify_output
            tampered_manifest = verification_target.verify_manifest_tampered(
                _pinned_manifest, root)
            if tampered_manifest:
                # Fail this attempt like a frozen-test edit, then put the base version back so
                # neither the next attempt nor the pull request carries the agent's copy.
                from .sandbox import CommandResult
                from .verify_manifest import MANIFEST_NAME

                _manifest_path = Path(root) / MANIFEST_NAME
                if _pinned_manifest.text is None:
                    _manifest_path.unlink(missing_ok=True)
                else:
                    _manifest_path.write_text(_pinned_manifest.text, encoding="utf-8")
                if record:
                    last_verify_output = f"[boss] {tampered_manifest}\n"
                return CommandResult(command="", argv=[], exit_code=1, duration_ms=0,
                                     timed_out=False, stdout="", stderr=tampered_manifest)
            plan = verification_target.resolve_gate_plan(
                spec, title, root, app_id=_app_id, changed=_changed_paths(),
                pinned=_pinned_manifest)
            cwd = plan.cwd
            try:
                last_verify_cwd = str(cwd.relative_to(root)) if cwd != root else "."
            except ValueError:
                last_verify_cwd = str(cwd)
            if plan.from_manifest:
                return _run_manifest_verify(plan, record=record)
            try:
                verification_target.ensure_verify_script(cwd)
            except Exception:  # noqa: BLE001 — never fail a run on script bookkeeping
                pass
            prep_note = ""
            try:
                prep = verification_target.prepare_web_verify_env(
                    cwd,
                    scenarios=(fctx.get("qa_benchmark") or {}).get("acceptance_scenarios"),
                    app_id=_app_id,
                )
            except Exception as exc:  # noqa: BLE001
                prep = {
                    "install_command": verification_target.NPM_INSTALL_CMD,
                    "browser_install_command": verification_target.PLAYWRIGHT_INSTALL_CMD,
                    "changed": [],
                    "error": f"{type(exc).__name__}: {exc}",
                }
                prep_note = f"[boss] prepare_web_verify_env failed: {prep['error']}\n"
            else:
                restored = prep.get("restored") if isinstance(prep.get("restored"), dict) else {}
                prep_note = (
                    f"[boss] restore restored={bool(restored.get('restored'))} "
                    f"changed={(prep.get('changed') or [])[:16]}\n"
                )
            if str(test_cmd or "").startswith("npm "):
                install_timeout = max(
                    300,
                    int(getattr(settings, "sandbox_validation_timeout_seconds", 120) or 120),
                )
                install = sandbox.run(
                    str(prep.get("install_command") or verification_target.NPM_INSTALL_CMD),
                    cwd=str(cwd),
                    timeout=install_timeout,
                )
                _emit_cmd("install dependencies", install)
                if install.exit_code != 0:
                    last_verify_output = (
                        prep_note
                        + (install.stdout or "")
                        + "\n"
                        + (install.stderr or "")
                    )[-4000:]
                    return install
                _emit_cmd("install browsers", sandbox.run(
                    str(
                        prep.get("browser_install_command")
                        or verification_target.PLAYWRIGHT_INSTALL_CMD
                    ),
                    cwd=str(cwd),
                    timeout=install_timeout,
                ))
            # npm install / the implementer can recreate leftover Playwright
            # configs. Force the official HookWatch tree again immediately
            # before the authoritative command so e2e cannot start itty-router
            # or a metrics stub.
            try:
                forced = verification_target.prepare_web_verify_env(
                    cwd,
                    scenarios=(fctx.get("qa_benchmark") or {}).get("acceptance_scenarios"),
                    app_id=_app_id,
                )
                prep_note += (
                    f"[boss] pre-verify restore restored="
                    f"{bool((forced.get('restored') or {}).get('restored'))} "
                    f"changed={(forced.get('changed') or [])[:16]}\n"
                )
            except Exception as exc:  # noqa: BLE001
                prep_note += f"[boss] pre-verify restore failed: {type(exc).__name__}: {exc}\n"
            class _CwdSandbox:
                def run(self, command):
                    return sandbox.run(command, cwd=str(cwd))

            results = verification_target.run_until_failure(_CwdSandbox(), plan.commands)
            if not results:
                from .sandbox import CommandResult

                result = CommandResult(
                    command="",
                    argv=[],
                    exit_code=1,
                    duration_ms=0,
                    timed_out=False,
                    stdout="",
                    stderr="no verification commands",
                )
            else:
                result = results[-1]
            output = (
                prep_note
                + "".join(
                    ((item.stdout or "") + "\n" + (item.stderr or "") + "\n")
                    for item in results
                )
            )[-4000:]
            if record:
                last_verify_output = output
            if result.ok and verification_target.verify_output_disproves_pass(output):
                from dataclasses import replace as _dc_replace

                result = _dc_replace(
                    result,
                    exit_code=1,
                    stderr=(result.stderr or "")
                    + "\n[boss] verify output disproves pass (swallowed failure)\n",
                )
            return result

        if _app_id and not (root / _app_id / "package.json").is_file():
            try:
                from .scaffold_service import scaffold_project

                seeded = scaffold_project(name=_app_id, kind="fullstack")
                app_dir = root / _app_id
                for rel, content in (seeded.get("files") or {}).items():
                    dest = app_dir / Path(rel)
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    if not dest.exists():
                        dest.write_text(content, encoding="utf-8")
                with _transaction(engine) as c:
                    emit_event(
                        c, project_id, "code_task.app_scaffold_seeded", "boss",
                        "code_task_run", run_id,
                        {"app_root": _app_id, "kind": "fullstack"},
                    )
            except Exception:  # noqa: BLE001 — missing seed must not sink the run
                pass
        elif _app_id:
            try:
                verification_target.ensure_verify_script(root / _app_id)
            except Exception:  # noqa: BLE001
                pass

        # REGRESSION FLOOR — applies at EVERY tier, costs no agent time.
        # The gate set below tier 2 was only {code_written, written_tests_pass}: `nothing_broke`
        # is computed inside the tier-2 review panel, so a cheaper tier silently shipped with no
        # regression gate at all. Worse, at tier 0 the implementer authors the tests, so nothing
        # stopped it from weakening or deleting the repo's EXISTING tests to turn the suite
        # green. Rigor is a dial; not regressing what already works is not — it is a floor.
        # Snapshot every pre-existing test file now and assert afterwards that none were
        # modified or removed. Pure hashing: no model, no measurable time.
        preexisting_tests = sorted(
            str(p.relative_to(root)) for p in root.rglob("*")
            if p.is_file() and _is_test_path(str(p.relative_to(root)))
            and ".git/" not in str(p.relative_to(root))
        )
        # Count the TESTS in each pre-existing file, not its bytes — see the floor below.
        preexisting_test_counts: dict[str, int] = {}
        for _p in preexisting_tests:
            try:
                preexisting_test_counts[_p] = len(
                    _TEST_DEF_RE.findall((root / _p).read_text(errors="ignore")))
            except OSError:
                preexisting_test_counts[_p] = 0

        # Which steps run is set by the risk tier (the cost dial): tier 0 = one agent writes
        # code + tests; tier >=1 = a separate tester; tier >=2 = add a fresh-context code review.
        do_separate_tests = risk_tier >= 1
        # The adversarial panel (code review + security + consistency + full-suite regression)
        # runs its checks CONCURRENTLY — measured wall time 3.3 min for all four, against 12.8
        # min for the sequential design chain. It was gated at tier 2, so every cheaper run
        # shipped with "not run at this rigor" on all four gates. That is not a faster pipeline,
        # it is a pipeline doing less work — and it is not a result anyone can trust.
        # Verification is cheap because it is parallel; PLANNING is what is expensive. So the
        # panel now runs from tier 1, and only the design chain stays tier-2+.
        # PR REVIEW IS MANDATORY, INCLUDING FOR SMALL CHANGES (operator decision).
        # A small diff is exactly where a cheap reviewer catches a real mistake, so the panel
        # runs from tier 1. This was briefly reverted to tier 2 after it caused a 191-minute
        # run — but that was NOT the panel's cost: BOSS_CODE_AGENT_MAX_CONCURRENCY was pinned
        # to 1 so the "parallel" checks serialized, and a hung seat could not be killed because
        # Future.cancel() is a no-op on a running future. Both are fixed (concurrency 4;
        # _kill_stray_review_processes), so the panel costs ~3 min of WALL time, not 25.
        do_review = risk_tier >= 1
        # ADVISORY GATE: when True, failing quality checks are SURFACED but do NOT block the PR —
        # the build still ships a PR + preview and the user retries-to-improve. Quality feedback
        # without a dead-end (product decision: always produce a result on the first pass).
        #
        # This was hardcoded False and only ever turned on for web-app builds, so for every
        # normal task an unhappy reviewer could withhold the deliverable outright. Measured
        # over three days: 22 runs, 2 PRs, with runs recording "PR not created: blocked".
        # A reviewer's opinion is INPUT to the operator, not a veto over shipping — so the
        # profile now owns this choice, and `lean` sets it True. The panel still runs and
        # every verdict is still recorded; it just cannot swallow the PR.
        # NOT clamped by tier. Sensitive work gets MORE review (the seat list below is
        # restored to the full panel), never a withheld PR — the two are independent. What
        # protects main on a sensitive change is the merge gate, not refusing to show the
        # operator the diff.
        advisory_gate = bool(profile.get("advisory_gate", False))

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
            advisory_gate = advisory_gate or risk_tier < 2

        # DESIGN PHASE (tier >= 2): standards -> spec -> plan -> review plan, BEFORE any code.
        # Spec-driven: the refined spec + plan are fed into the downstream coding steps. (The human
        # approve-plan gate is Wire-Phase 2b; for now review plan records a PLAN: OK/REVISE note.)
        design: dict = {}
        do_design = risk_tier >= 2 and not is_web_app
        if not do_design:
            # Official follow-ups are risk-tier 1 and skip the design chain.
            # The frozen benchmark PRD / inherited parent plan is still the plan.
            if isinstance(resume_design, dict) and str(resume_design.get("plan") or "").strip():
                design = resume_design
            else:
                inherited = fctx.get("inherited_design")
                if isinstance(inherited, dict) and str(inherited.get("plan") or "").strip():
                    design = inherited
                else:
                    bench = fctx.get("qa_benchmark") if isinstance(fctx.get("qa_benchmark"), dict) else {}
                    scenarios = bench.get("acceptance_scenarios") or []
                    prompt = str(bench.get("prompt") or "").strip()
                    if prompt or scenarios:
                        design = {
                            "plan": prompt or "\n".join(str(item) for item in scenarios),
                            "plan_ok": True,
                            "source": "qa_benchmark",
                        }
        if do_design and resume_design:
            # Resuming after human plan approval — reuse the approved design, skip re-running it.
            design = resume_design
            instructions = (f"{instructions}\n\nREFINED SPEC:\n{design.get('spec', '')}"
                            f"\n\nTECHNICAL PLAN:\n{design.get('plan', '')}")
        elif do_design:
            with _transaction(engine) as c:
                _phase(c, "design")
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
            # PERSIST THE PLAN THE MOMENT IT EXISTS.
            # `state` was only written when a run FINISHED, so a mid-run death lost the design
            # along with everything else — both spend-limit failures recorded an EMPTY state, and
            # their retries re-ran standards+spec+plan+review_plan to re-derive a plan for a task
            # that had not changed (~3.5 min and ~$0.90 each, every time). The resume path reads
            # state.design, so it has to be there before the thing that might kill us.
            with _transaction(engine) as c:
                c.execute(sql(
                    "UPDATE code_task_runs SET state=COALESCE(state,'{}'::jsonb) "
                    "|| jsonb_build_object('design', CAST(:d AS JSONB)), updated_at=now() "
                    "WHERE id=:id"), {"d": json.dumps(design), "id": run_id})
                emit_event(c, project_id, "code_task.design_persisted", "boss",
                           "code_task_run", run_id, {"plan_chars": len(design.get("plan") or "")})
            _mark_stage_done("design")
            # FAIL FAST: if the design reviewer says this isn't a coding task for this repo (a
            # research/PRD/spec deliverable, or an unrelated repo) it was mis-routed into codegen.
            # Stop NOW — in one design pass — instead of authoring tests + a whole codebase and only
            # catching the mismatch at the quality gate ~an hour later.
            if _is_not_a_code_task(design["plan_review"]):
                _finish_vms(success=False)
                reason_line = next((ln.strip() for ln in design["plan_review"].splitlines()
                                    if "NOT_A_CODE_TASK" in ln.upper()), "")
                _rescue_pr("the design review judged this not a coding task for this repo")
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

        # RESUME FROM WHERE IT FAILED. A repair re-ran write_tests unconditionally, even when the
        # parent failed purely on review opinion — 24 turns / 8.6 min / a full agent session to
        # re-derive a suite already sitting on the branch. Which gates failed tells us whether
        # the tests are actually stale: if none of the test-related gates failed, the suite stood
        # up to the run and the repair's job is the CODE. Skipping is only safe when tests are
        # genuinely present on the branch — otherwise there is nothing to reuse.
        # A run re-entered by lease reclaim must honour ITS OWN completed_stages, not only a
        # parent's. Reusing the design on re-entry (design_reused_own) was half the fix:
        # crun_c867b12a was re-entered twice, correctly skipped the design chain both times,
        # and then re-ran write_tests both times because completed_stages was consulted only
        # for retries. A stage this run already finished is finished.
        _resume_done = set(fctx.get("resume_completed_stages") or [])
        _resume_done |= {str(s) for s in (fctx.get("completed_stages") or [])}
        # `preexisting_tests` means THE REPO HAS TESTS — not that THIS LINEAGE wrote any.
        # Skipping on it alone shipped a calculator and a stock tracker straight to implement
        # with no tests of their own, because boss-sandbox's main branch happens to contain 18
        # test files from unrelated apps. The lineage branch did not even exist, so the clone
        # fell back to main and those files looked like "the suite for this task".
        # Only the LINEAGE'S OWN record proves tests were authored for this work: it completed
        # write_tests, or a real gate verdict was reached (which requires a suite to judge).
        _lineage_wrote_tests = bool(
            "write_tests" in _resume_done or fctx.get("prior_failing_gates"))
        # This run finishing write_tests is direct evidence — it does not need a lineage.
        _own_tests_done = "write_tests" in {str(s) for s in (fctx.get("completed_stages") or [])}
        if (do_separate_tests and (_own_tests_done or (root_run != run_id and preexisting_tests))
                and (_own_tests_done or _lineage_wrote_tests)
                and ("write_tests" in _resume_done
                     or not _repair_needs_write_tests(
                         fctx.get("prior_failing_gates"),
                         prior_error=fctx.get("prior_error"),
                         tests_on_branch=bool(preexisting_tests)))):
            do_separate_tests = False
            with _transaction(engine) as c:
                emit_event(c, project_id, "code_task.tests_resumed", "boss",
                           "code_task_run", run_id,
                           {"skipped_stage": "write_tests",
                            "prior_failing_gates": list(fctx.get("prior_failing_gates") or []),
                            "existing_tests": len(preexisting_tests),
                            "why": ("no test-related gate failed, so the existing suite is the "
                                    "oracle for this repair")})

        if do_separate_tests:
            # STEP write tests (qa), tests only.
            _abort_if_cancelled("write_tests")
            with _transaction(engine) as c:
                _phase(c, "write_tests")

            def _attempt_write_tests(escalate: bool):
                """One QA/write-tests pass → (r1, test_files, test_changes|None). escalate
                forces the frontier model + a sharper prompt to recover a weak first pass."""
                nonlocal cost
                prompt = _qa_prompt(
                    title, instructions, criteria, test_cmd,
                    iac=iac_task, web=_web_verify, app_root=_app_id,
                )
                # AUGMENT, DON'T RE-AUTHOR. On a repair the prior attempt's tests are ALREADY in
                # the working tree (the retry clones the lineage branch), and the review names the
                # exact gap — e.g. "the defects are all in app.js, which no test covers". Sending
                # the from-scratch prompt made the retry re-derive a whole suite it already had:
                # ~4.6 min and ~$1 to rewrite 240 passing tests instead of covering the one hole.
                # ...or when THIS run was interrupted and resumed its own work branch. The
                # condition used to be the repair lineage alone, which was right while a
                # re-delivered run always started from an empty tree. Now that provisioning
                # restores the checkpointed branch, a same-run resume ALSO arrives with a
                # working suite already present — and sending the from-scratch prompt into that
                # tree is the exact failure this block was written to stop.
                _resumed = bool(getattr(sandbox, "resumed_from_checkpoint", False))
                if root_run != run_id or _resumed:
                    existing = sorted(
                        str(p.relative_to(root)) for p in root.rglob("*")
                        if p.is_file() and _is_test_path(str(p.relative_to(root)))
                        and ".git/" not in str(p.relative_to(root)))
                    if existing:
                        # NOTE: `evidence` is not bound this early in the pipeline — the
                        # findings live in the iteration feedback carried on fctx/instructions.
                        _hist = fctx.get("feedback_history") or [{}]
                        gap = (_hist[-1].get("fix_note")
                               or (instructions.split("=== ITERATION FEEDBACK", 1)[-1]
                                   if "=== ITERATION FEEDBACK" in instructions else "")) or ""
                        # TWO DIFFERENT SITUATIONS, TWO DIFFERENT INSTRUCTIONS. A repair has
                        # reviewers' findings to act on. A RESUME has none: the previous
                        # attempt was killed mid-run, not judged, so there is no gap to close
                        # and telling the agent to close one would send it looking for a
                        # finding that does not exist.
                        _resume_only = _resumed and not gap.strip()
                        _listing = "\n".join(f"  - {p}" for p in existing[:40])
                        if _resume_only:
                            prompt = (
                                "This run was INTERRUPTED and has resumed. A working test "
                                "suite it already authored is present in this tree and "
                                "passes:\n" + _listing
                                + "\n\nDO NOT rewrite, re-author or delete any of them — that "
                                "work is done. Verify they still cover the task below, ADD "
                                "only what is genuinely missing, and otherwise change "
                                "nothing.\n\n"
                                f"TASK CONTEXT: {title}\n\nRun `{test_cmd}` and leave the "
                                "whole suite green. Touch as few files as possible.")
                        else:
                            prompt = (
                                "This is a REPAIR iteration. A working test suite ALREADY "
                                "EXISTS in this tree and currently passes:\n" + _listing
                                + "\n\nDO NOT rewrite or re-author these tests, and do not "
                                "delete any of them. Your ONLY job is to CLOSE THE SPECIFIC "
                                "COVERAGE GAP the reviewers identified, by ADDING focused "
                                "tests (or extending an existing file where that is the "
                                "natural home).\n\n"
                                f"GAP TO CLOSE:\n{gap[:2000]}\n\n"
                                f"TASK CONTEXT: {title}\n\nRun `{test_cmd}` and leave the "
                                "whole suite green. Touch as few files as possible.")
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
                        engine_override=_vm_escalation("write_tests", escalate, "no_output"),
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
            # On a REPAIR the tree ALREADY holds a passing suite, so "wrote no new test files"
            # is a legitimate outcome — the augment pass may judge coverage sufficient. Escalating
            # there is actively harmful: it re-issues the FROM-SCRATCH prompt on the frontier
            # model and re-authors a suite that already exists. Measured: augment pass 7 turns /
            # 23s / $0.16, then the escalation spent 31 turns / 356s / $1.84 on opus — worse than
            # the original from-scratch run it was meant to avoid. Only escalate when the tree has
            # NO tests at all.
            _repair_with_tests = (root_run != run_id) and bool(preexisting_tests)
            if not test_files and _repair_with_tests:
                test_files = list(preexisting_tests)
                with _transaction(engine) as c:
                    emit_event(c, project_id, "code_task.tests_reused", "boss",
                               "code_task_run", run_id,
                               {"step": "write_tests", "reason": "repair_augment_added_no_files",
                                "existing_tests": len(preexisting_tests),
                                "note": "the existing suite stands as the oracle; not re-authoring"})
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
            else:
                _checkpoint("write_tests")
            _mark_stage_done("write_tests")
            if not test_files:
                with _transaction(engine) as c:
                    emit_event(c, project_id, "code_task.step_exhausted", "boss",
                               "code_task_run", run_id,
                               {"step": "write_tests",
                                "reason": "no_test_files_after_escalation"})
                    _release(budget, c, project_id, reservation_id, "no independent tests authored", run_id=run_id)
                _rescue_pr("the test author produced no test files")
                with _transaction(engine) as c:
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
        _abort_if_cancelled("implement")
        with _transaction(engine) as c:
            _phase(c, "implement")
        base_impl_prompt = (_impl_prompt(
                                title, instructions, test_files, test_cmd, criteria=criteria,
                                iac=iac_task, ui=ui_task, app_root=_app_id)
                            if do_separate_tests
                            else _solo_prompt(
                                title, instructions, criteria, test_cmd,
                                iac=iac_task, web=_web_verify, app_root=_app_id))
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
            # `attempt` counts IN-RUN repair passes, so a CROSS-RUN retry starts at 0 and used to
            # receive the full build prompt — "implement the task" — telling an agent whose working
            # tree already contains a finished 2,300-line app to build it. The only hint otherwise
            # was the iteration-feedback text buried in `instructions`. A repair must PATCH what is
            # there, not re-derive it, so a cross-run retry is treated as a repair from its first
            # pass, carrying the reviewers' findings as the concrete evidence to fix.
            # STAGE-BOUNDARY STEERING: fold in anything the operator sent while this run has
            # been executing, so a wrong direction costs one stage instead of the whole run.
            _steer = take_pending_guidance(
                engine, run_id, stage="implement" if do_separate_tests else "solo")
            if _steer:
                base_impl_prompt = base_impl_prompt + _guidance_block(_steer)
                with _transaction(engine) as c:
                    emit_event(c, project_id, "code_task.steered", "operator",
                               "code_task_run", run_id,
                               {"stage": "implement", "count": len(_steer),
                                "guidance": [i.get("text", "")[:200] for i in _steer]})
            _cross_run_repair = (root_run != run_id) and attempt == 0
            if attempt == 0 and not _cross_run_repair:
                impl_prompt = base_impl_prompt
                _iref = getattr(sandbox, "integration_ref", None)
                # Emit it either way, so the next failure is diagnosable instead of guessable.
                with _transaction(engine) as c:
                    emit_event(c, project_id, "code_task.integration_ref", "boss",
                               "code_task_run", run_id,
                               {"ref": _iref,
                                "error": getattr(sandbox, "integration_fetch_error", None),
                                "available": bool(_iref)})
                if _iref:
                    impl_prompt = impl_prompt + (
                        f"\n\n=== INTEGRATION BRANCH AVAILABLE ===\n"
                        f"`{_iref}` is fetched in this repo and holds work merged since this "
                        "branch was cut. If your task refers to code you cannot find, it is "
                        f"probably there: inspect it with `git show {_iref}:<path>` or "
                        f"`git diff HEAD..{_iref} --stat`.\n"
                        "TO BRING IT IN, DO NOT COMMIT. BOSS detects your work as UNCOMMITTED "
                        "changes in the tree (`git add -A; git diff --cached`), so anything you "
                        "commit yourself becomes INVISIBLE to it and is dropped from the PR. "
                        f"Use `git merge --no-commit --no-ff {_iref}` (leaves changes staged), "
                        f"or `git checkout {_iref} -- <paths>` for specific files. Resolve any "
                        "conflicts, leave everything staged/uncommitted, and keep BOTH sides' "
                        "functionality unless told otherwise.")
            else:
                _ev = evidence_text
                if _cross_run_repair and not (_ev or "").strip():
                    _hist = fctx.get("feedback_history") or [{}]
                    _ev = (_hist[-1].get("fix_note")
                           or (instructions.split("=== ITERATION FEEDBACK", 1)[-1]
                               if "=== ITERATION FEEDBACK" in instructions else "")
                           or "(see the iteration feedback in the task instructions)")
                impl_prompt = _repair_prompt(base_impl_prompt, _ev, max(1, attempt))
            if use_microvm:
                res2 = _guest_stage(
                    "implement", "implement" if do_separate_tests else "solo", impl_prompt,
                    test_cmds=list(verify_commands),
                    stage_title=f"SE: implement — {title}" + (f" (repair {attempt})" if attempt else ""),
                    engine_override=_vm_escalation("implement", escalate_impl, "final_attempt"))
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
                if not use_microvm:
                    _checkpoint("implement" + (f"-repair{attempt}" if attempt else ""))
                # Same reason as the design above: state was only written at the END, so a
                # spend-limit kill lost the record of WHICH files this task built — which is
                # exactly what the preview resolver needs to pick the right app.
                try:
                    with _transaction(engine) as c:
                        c.execute(sql(
                            "UPDATE code_task_runs SET state=COALESCE(state,'{}'::jsonb) "
                            "|| jsonb_build_object('impl_files', CAST(:f AS JSONB)), "
                            "updated_at=now() WHERE id=:id"),
                            {"f": json.dumps([p for p in accumulated
                                              if not _is_test_path(p)]), "id": run_id})
                except Exception:  # noqa: BLE001 — bookkeeping never fails a run
                    pass
                if not attempt:
                    _mark_stage_done("implement")
                impl_files = [p for p in accumulated if not _is_test_path(p)]
                # Authoritative verification: the guest RUNNER (our code, in-VM) executed the
                # test command after the agent stopped — that in-VM result is the verdict.
                with _transaction(engine) as c:
                    _phase(c, "run_tests")
                tests_passed = bool(res2.tests_passed)
                test_exit_code = res2.tests[0].exit_code if res2.tests else None
                fail_output = (res2.tests[0].stdout if res2.tests else "") or ""
            else:
                set_llm_context(stage="implement" if do_separate_tests else "solo")
                _prev_route = dict(stage_routes.get("implement") or {})
                a, m = _route_escalated("implement") if escalate_impl else _route("implement")
                # Resume-based repair: continue the implementer's own session so the
                # repair rides the warm prompt cache (task context + its prior work are
                # cache READS, not fresh ingestion). Escalated attempts stay cold —
                # different model, fresh eyes. Cold full-prompt fallback if resume fails.
                # A session is never switched to another model mid-flight: a route that
                # moved (the router escalated after repeated failures) starts cold too.
                _same_route = (_prev_route.get("engine"), _prev_route.get("model")) == (
                    stage_routes["implement"].get("engine"), stage_routes["implement"].get("model"))
                resume_sid = impl_session_id if (attempt and not escalate_impl
                                                 and _same_route) else None
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
                # FINISH THE WORK BEFORE JUDGING IT. The implementer hitting its turn cap
                # returns a PARTIAL change, and the pipeline used to hand that half-built diff
                # straight to the reviewers — who correctly found it wanting, which triggered a
                # full from-scratch repair that hit the same cap again. Measured on one small
                # feature: implement ran 41/40 turns twice, and the run spent 28 sessions /
                # 96 model-minutes / $39 without ever finishing the feature.
                # Resuming the implementer's OWN session continues the work on a warm cache
                # (its prior turns are cache reads, not fresh ingestion), which is both far
                # cheaper and far faster than re-deriving the change from zero.
                _cont = 0
                _max_cont = max(0, int(getattr(settings, "code_impl_max_continuations", 3) or 3))
                while (check_is_inconclusive(r2.get("summary", ""))
                       and _cont < _max_cont and impl_session_id):
                    _cont += 1
                    with _transaction(engine) as c:
                        emit_event(c, project_id, "code_task.implement_continued", "boss",
                                   "code_task_run", run_id,
                                   {"continuation": _cont, "of": _max_cont,
                                    "reason": "implementer hit its turn cap — resuming its own "
                                              "session to finish, not judging a partial change"})
                    try:
                        rc = a.run_role(
                            "Your previous turn ended because you ran out of turns, not because "
                            "the work was done. CONTINUE from exactly where you stopped and "
                            "FINISH the task. Do not restart or re-plan. When the change is "
                            "complete and the tests pass, end with a summary of what you changed.",
                            root, model=m, resume_session_id=impl_session_id,
                            max_turns=int(getattr(settings, "code_agent_max_turns", 40) or 40))
                    except Exception:
                        break
                    cost += int(rc.get("cost_usd_micros") or 0)
                    impl_session_id = rc.get("session_id") or impl_session_id
                    r2 = {**r2, "summary": (rc.get("summary") or r2.get("summary", ""))}
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
                    _phase(c, "run_tests")
                test_result = _run_verify()
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
            # Evidence for the router: the next implement boundary may move up a tier
            # (Smart mode's escalation rules; Fixed mode ignores the ledger).
            stage_router.signal("implement", "tests_failed")
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
                evidence_text = verification_target.attach_failing_sources(
                    f"$ {test_cmd} (exit {test_exit_code})\n{fail_output[-3000:]}",
                    root,
                )

        # Freeze implement-time verify so later panel/mutation probes cannot
        # replace the official /report evidence.
        official_verify_output = last_verify_output
        official_tests_passed = tests_passed
        official_test_exit = test_exit_code

        # QUALITY LOOP — one bounded improve pass after a green authoritative test run
        # when UI/UX evidence is thin. Frozen tests stay frozen. If the pass breaks
        # tests, restore the last green implementation files.
        workspace_evidence: dict = {}
        quality_passes_used = 0
        _app_id = str((fctx.get("qa_benchmark") or {}).get("id") or "").strip() or None
        try:
            workspace_evidence = collect_workspace_evidence_from_root(root, app_id=_app_id)
            workspace_evidence = merge_official_jtbd(
                workspace_evidence,
                (fctx.get("qa_benchmark") or {}).get("acceptance_scenarios"),
            )
            workspace_evidence = bind_verify_log_to_jtbd(
                workspace_evidence, last_verify_output
            )
        except Exception:  # noqa: BLE001
            workspace_evidence = {}
        if should_run_quality_pass(
            workspace_evidence, tests_passed=bool(tests_passed and impl_files),
        ):
            snapshot = {}
            for _qp in impl_files:
                _qf = root / _qp
                if _qf.is_file():
                    try:
                        snapshot[_qp] = _qf.read_text(encoding="utf-8")
                    except OSError:
                        pass
            q_prompt = quality_improve_prompt(workspace_evidence, title=title)
            with _transaction(engine) as c:
                emit_event(
                    c, project_id, "code_task.quality_pass", "boss",
                    "code_task_run", run_id,
                    {"reason": "thin_ui_ux_evidence", "attempt": 1},
                )
            q_ok = tests_passed
            try:
                if use_microvm:
                    qres = _guest_stage(
                        "quality", "implement", q_prompt, test_cmds=list(verify_commands),
                        stage_title=f"SE: quality — {title}",
                    )
                    q_changes = [fc for fc in qres.diff if not _is_test_path(fc.path)]
                    _commit_stage(q_changes, f"governed: quality — {title[:60]}")
                    q_ok = bool(qres.tests_passed)
                    if q_ok:
                        tests_passed = True
                        test_exit_code = qres.tests[0].exit_code if qres.tests else test_exit_code
                        impl_files = [p for p in accumulated if not _is_test_path(p)] or impl_files
                else:
                    set_llm_context(stage="quality")
                    qa, qm = _route("implement")
                    qa.run_role(q_prompt, root, model=qm)
                    q_test = _run_verify()
                    q_ok = bool(q_test.ok)
                    if q_ok:
                        tests_passed = True
                        test_exit_code = q_test.exit_code
                        official_tests_passed = True
                        official_test_exit = q_test.exit_code
                        official_verify_output = last_verify_output
                        impl_files = [
                            fc.path for fc in sandbox.diff() if not _is_test_path(fc.path)
                        ] or impl_files
            except Exception:  # noqa: BLE001 — quality pass must not sink a green run
                q_ok = False
            reverted = False
            if not q_ok and snapshot:
                for _qp, _qc in snapshot.items():
                    try:
                        (root / _qp).write_text(_qc, encoding="utf-8")
                    except OSError:
                        pass
                reverted = True
                tests_passed = True
            quality_passes_used = 1
            repair_log.append(quality_pass_record(
                attempt=1, tests_passed=q_ok and not reverted, reverted=reverted,
                gaps=[{"dimension": "ui"}, {"dimension": "ux"}],
            ))
            try:
                workspace_evidence = collect_workspace_evidence_from_root(root, app_id=_app_id)
            except Exception:  # noqa: BLE001
                pass

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
        deferred_findings: list[dict] = []  # sub-threshold review findings → follow-up task
        test_arbitrations: list[dict] = []  # CODE_WRONG/TEST_WRONG rulings on failing tests
        panel_size = 1
        review_seat = None  # the panel seat whose findings stand for the review (§6.4)
        review_cost = 0
        mutation_caught = None
        regressions_ok = None
        panel_parallel = False
        if do_review:
            with _transaction(engine) as c:
                _phase(c, "review_panel")
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
                    # Each seat of a real panel signs its own act (factory §6.4); a panel of
                    # one is just the review.
                    set_llm_context(routes=_route_ids(), db_engine=engine, project_id=project_id, run_id=run_id,
                                    stage="review_code",
                                    seat=(seat + 1) if panel_size > 1 else None)
                    rr = seat_agent.run_role(
                        _review_prompt(title, criteria, instructions=instructions, ui=ui_task, change=change_block),
                        review_root, read_only=True, model=seat_model)
                    with lock:
                        panel_cost["v"] += int(rr.get("cost_usd_micros") or 0)
                    panel["review_votes"][seat] = (rr, _parse_verdict(rr.get("summary", "")))
                return _run

            review_checks = [_make_review(i, a, m) for i, (a, m) in enumerate(review_members)]

            def _do_security():
                set_llm_context(routes=_route_ids(), db_engine=engine, project_id=project_id, run_id=run_id,
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
                    set_llm_context(routes=_route_ids(), db_engine=engine, project_id=project_id, run_id=run_id,
                                    stage="regression")
                    rr = _guest_stage("run_all_tests", "regression", "Run the full test suite.",
                                      test_cmds=list(verify_commands),   # APP-3: web-aware regression target
                                      stage_title=f"Regression — {title}",
                                      engine_override="echo")
                    panel["full"] = bool(rr.tests_passed)
                else:
                    # Official canaries already ran the authoritative suite
                    # during implement. Re-running here clobbers last_verify_output
                    # and can start a leftover webServer. Reuse that result.
                    if _app_id:
                        panel["full"] = bool(tests_passed)
                    else:
                        panel["full"] = bool(_run_verify(record=False).ok)

            def _do_consistency():
                set_llm_context(routes=_route_ids(), db_engine=engine, project_id=project_id, run_id=run_id,
                                stage="consistency_check")
                rr = consistency_agent.run_role(
                    _consistency_prompt(title, criteria, design, instructions=instructions, change=change_block),
                    review_root, read_only=True, model=consistency_model)
                with lock:
                    panel_cost["v"] += int(rr.get("cost_usd_micros") or 0)
                panel["consistency"] = (rr, _parse_consistency(rr.get("summary", "")))

            # WHICH seats sit on the panel is a profile choice. The default is the full
            # adversarial panel; `lean` narrows it to ["regression"] so a prototype run keeps
            # test-first discipline and the "does it actually run" signal, without the three
            # LLM seats that argue about the code. A dropped seat is recorded as not_run (see
            # the setdefaults above) rather than silently passing — advisory mode then ships
            # the PR with that stated plainly, instead of pretending the check was clean.
            # A profile's leniency applies only INSIDE its own tier range. `sensitive` work
            # (auth, secrets, payments) overrides the tier cap upward — and if the narrowed
            # seat list survived that override, `lean` would become a way to ship credential
            # handling with no security review at all. Above tier 1 the full panel is restored
            # no matter what the profile asked for.
            enabled_seats = set(
                _ALL_PANEL_SEATS if risk_tier >= 2
                else (profile.get("panel_checks") or _ALL_PANEL_SEATS))
            checks = []
            if "review_code" in enabled_seats:
                checks += review_checks
            if "security_check" in enabled_seats:
                checks.append(_do_security)
            if "regression" in enabled_seats:
                checks.append(_do_full)
            if "consistency_check" in enabled_seats:
                checks.append(_do_consistency)
            # Panel checks are independent + read-only, so run them ALL in parallel rather
            # than in slow batches of code_agent_max_concurrency (which is tuned for the
            # general lane). A low general concurrency was serializing the panel into ~2-3
            # batches of ~7-10min each → ~20min wall-clock. Provider 429s are handled by the
            # adapter's retry, so wider parallelism here is safe.
            # THE PROJECT'S OWN SETTING, when it has one. A knob that changes nothing is
            # worse than no knob: this is the number an operator turns when review is
            # saturating the box, so it has to be the number that is read.
            from .project_settings import effective as _effective_setting
            with _transaction(engine) as _sc:
                _lanes = _effective_setting(
                    _sc, project_id, settings, "code_agent_max_concurrency")
            # Floor of 6, capped by the panel: project_settings.effective_review_lanes.
            max_workers = effective_review_lanes(_lanes, panel_size=len(checks))
            # UNDER MEMORY PRESSURE THE PANEL SITS ONE SEAT AT A TIME (stability spec §4b.3,
            # L1+): each seat is a ~300 MB CLI, and six at once is what tips a small machine
            # into swap. Slower, and it finishes.
            # project_settings.memory_limited_seats: the same reading Govern shows on the board.
            try:
                from .project_settings import memory_limited_seats
                with _transaction(engine) as _lc:
                    _seats_now = memory_limited_seats(_lc)
                if _seats_now is not None:
                    max_workers = min(max_workers, _seats_now)
            except Exception:  # noqa: BLE001 — an unread level never changes the panel
                pass
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
                        # A SEAT OWNS ITS PROCESSES, inside the build that owns it: the
                        # deadline below ends exactly this seat's tree, and a cancel of the
                        # build ends every seat's (owned_process.terminate_owner).
                        from . import owned_process as _op
                        with _op.owner_scope("review_seat", f"{run_id}:{label}"):
                            fn()
                    except Exception as exc:  # noqa: BLE001 — a failed review must not crash the run
                        with lock:
                            review_errors.append(f"{label}: {exc.__class__.__name__}: {exc}"[:200])
                _run.seat_label = label  # type: ignore[attr-defined]
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
                # A thread starts with an EMPTY context: copied here, each seat runs inside the
                # build's owner scope (and its LLM context) instead of belonging to nobody.
                futs = [pool.submit(contextvars.copy_context().run, g) for g in guarded]
                seat_of_future = {f: g.seat_label for f, g in zip(futs, guarded)}
                _done, not_done = _futures_wait(futs, timeout=review_deadline + 60)
                if not_done:
                    with lock:
                        review_errors.append(
                            f"{len(not_done)} review check(s) exceeded {review_deadline}s — degraded")
                    # Future.cancel() returns False for an ALREADY-RUNNING future — verified. So
                    # this loop logged "degraded" while the hung check kept running, the job kept
                    # renewing its lease, and the run stalled for over two hours before a reclaim.
                    # A review seat is a `claude -p` SUBPROCESS: to actually stop it we must kill
                    # the process, not cancel the future.
                    from . import owned_process as _op
                    owned_killed = 0
                    for f in not_done:
                        f.cancel()
                        owned_killed += _op.terminate_owner(
                            f"{run_id}:{seat_of_future.get(f)}", reason="deadline")["ended"]
                    killed = owned_killed + _kill_stray_review_processes(root, review_root)
                    with lock:
                        review_errors.append(
                            f"terminated {killed} stuck review process(es) past the deadline"
                            if killed else
                            "no stuck review process found to terminate (already exited)")
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
            if panel_size > 1:
                # Which seat's findings stand for the review, so its comments are signed by
                # that seat's act and not the panel's (factory spec §6.4).
                review_seat = next(
                    (i + 1 for i, v in enumerate(panel["review_votes"]) if v and v[0] is r3),
                    None)
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
            # Official /report reads tests_passed + last_verify_output. The mutation
            # probe is supposed to fail; never let that failure clobber implement-time
            # evidence. Skip the probe when authoritative verify already failed.
            implement_verify_output = last_verify_output
            implement_tests_passed = tests_passed
            implement_test_exit = test_exit_code
            implement_verify_cwd = last_verify_cwd
            if not tests_passed:
                mutation_caught = None
            else:
                with _transaction(engine) as c:
                    _phase(c, "check_tests")
                if use_microvm:
                    if impl_files:
                        res5 = _guest_stage("check_tests", "check_tests",
                                            _mutate_prompt(impl_files), test_cmds=list(verify_commands),
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
                        mut = _run_verify(record=False)
                        mutation_caught = not bool(mut.ok)
                        for p, content in orig.items():
                            (root / p).write_text(content, encoding="utf-8")
                        last_verify_output = implement_verify_output
                        tests_passed = implement_tests_passed
                        test_exit_code = implement_test_exit
                        last_verify_cwd = implement_verify_cwd

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
                _phase(c, "iac_verify")
            iac_result = _run_iac_lane(settings, files=changed_files,
                                       workdir=None if use_microvm else root)

        # FINAL GATE — fail-closed; only the checks that apply at this tier.
        # A REPAIR continuation that finds the prior code already correct makes NO new diff
        # (impl_files empty) — but the work branch still HAS that test-passing code. Treat a no-op
        # iteration on already-good code as "code present" so a CONVERGED run isn't false-failed.
        _is_repair_iteration = root_run != run_id
        # Regression floor, evaluated at every tier (see the snapshot after provision).
        # Compares TEST COUNT, not file hashes. Hash equality flagged every edit as
        # "weakened", including strengthening ones: on PR #62 it blocked a change that
        # renamed a misnamed test, kept its assertion, and ADDED a case for the gap it had
        # never covered (test_streak 16 -> 17 tests, test_persistence 22 -> 24). Editing a
        # test file is normal; LOSING coverage is the regression. A file that is deleted, or
        # comes back with fewer tests than it had, is what must never pass silently.
        # Declared by the task (create/extend/retry may set it) — never inferred, so an agent
        # cannot grant itself permission by deciding a deletion was reasonable.
        _retirement_allowed = bool(spec.get("allow_test_retirement"))
        _weakened: list[str] = []
        for _p in preexisting_tests:
            _fp = root / _p
            if not _fp.exists():
                _weakened.append(f"{_p} (deleted)")
                continue
            try:
                _now_n = len(_TEST_DEF_RE.findall(_fp.read_text(errors="ignore")))
            except OSError:
                continue
            _was_n = preexisting_test_counts.get(_p, 0)
            if _now_n < _was_n:
                _weakened.append(f"{_p} ({_was_n} -> {_now_n} tests)")
        gates = {
            "code_written": bool(impl_files) or (_is_repair_iteration and tests_passed),
            "written_tests_pass": tests_passed,
            # An existing test that was edited or deleted is the one way a green suite can
            # still mean "we broke something" — so this gate holds at tier 0 too.
            # TEST RETIREMENT. A blanket ban on removing tests makes some correct changes
            # IMPOSSIBLE, not merely gated. Consolidating two stock apps required deleting
            # four tests that existed only to assert the app being retired was still present:
            # main's suite asserted `web-stocks/` EXISTS while this branch's consolidation
            # guard asserted it must NOT. Both were "existing tests", so every attempt was
            # trapped — delete and main's tests fail, keep and the guard fails. Five attempts
            # and ~$40 went into a task that was unsatisfiable under the gate's own rules, and
            # a human ultimately deleted the tests by hand.
            #
            # So retirement becomes a DECLARED, JUSTIFIED act rather than a forbidden one:
            # a task may set allow_test_retirement, and every removal is then recorded with
            # the reason and surfaced on the PR for a human to reverse. The gate still fails
            # for UNdeclared removals — an agent quietly deleting an inconvenient test is the
            # thing this guard exists to catch, and that is unchanged.
            "existing_tests_intact": (not _weakened) or _retirement_allowed,
            # Web/official: echo stubs and missing vite/playwright cannot ship.
            "web_package_honest": True,
            # recorded below whether allowed or not, so the PR always states what went
        }
        _web_contract: dict | None = None
        if _web_verify:
            try:
                _web_contract = verification_target.evaluate_web_package_contract(
                    verification_target.resolve_verify_cwd(root, app_id=_app_id)
                )
            except Exception:  # noqa: BLE001
                _web_contract = {"ok": False, "stubs": ["unreadable"], "missing_runners": []}
            gates["web_package_honest"] = bool(_web_contract.get("ok"))

        # THE APP RUNS — proven here, not discovered by the operator's first preview.
        #
        # The Lead Triage Inbox build (crun_3e370c11, PR paramg/boss-sandbox#218) passed every
        # gate and completed; its preview then failed twice on "ImportError: attempted relative
        # import with no known parent package". Nothing between "tests pass" and "done" had ever
        # started the app. So once the suite is green, BOSS starts what the build declared (or,
        # failing a declaration, what the preview would start) and requires an HTTP answer.
        # A failure is a WORK gate with the app's own error, fed back to the implementer.
        def _check_app_serves(files: dict) -> dict:
            from .preview_service import lineage_impl_files, prove_app_serves

            with _transaction(engine) as c:
                _phase(c, "serve_check")
            # THE LINEAGE, NOT THIS DIFF ALONE. A follow-up of a declared app whose diff does
            # not contain the boss.preview.command proved `python3 main.py` instead of the
            # declared command the preview runs (second review of f16b5d4). The proof is given
            # the same file list the preview uses (lineage_impl_files) plus this diff.
            try:
                lineage = lineage_impl_files(engine, run)
            except Exception as exc:  # noqa: BLE001 — a fact about BOSS, not about the code
                record = {"ok": False, "harness": True, "command": None, "source": "none",
                          "error": f"could not read this app's lineage to check it: "
                                   f"{type(exc).__name__}: {exc}"[:600]}
            else:
                # Held to serving only when the lineage DECLARED a run command or the run
                # builds a named full-stack app — never inferred from the code (third review
                # of b63717c), never `web_app_build`, which every build-on-existing/extend
                # carries, CLIs and libraries included.
                record = prove_app_serves(
                    root, list(files.keys()), lineage=lineage,
                    known_service=serve_proof_known_service(spec, fctx),
                    timeout=float(getattr(settings, "code_task_serve_check_timeout_seconds",
                                          180) or 180),
                )
            with _transaction(engine) as c:
                emit_event(c, project_id, "code_task.app_serve_checked", "boss",
                           "code_task_run", run_id,
                           {k: (v[:600] if isinstance(v, str) else v)
                            for k, v in record.items()})
            return record

        app_serves_record: dict = {"skipped": "the tests did not pass, so there is no app to start"}
        _serve_skip = app_serves_check_skip_reason(
            fctx, command_runtime=getattr(settings, "sandbox_command_runtime", "subprocess"),
            use_microvm=use_microvm)
        if _serve_skip:
            app_serves_record = {"skipped": _serve_skip}
        elif tests_passed:
            app_serves_record = _check_app_serves(changed_files)
            gates["app_serves"] = bool(app_serves_record.get("ok"))

        if _weakened and _retirement_allowed:
            with _transaction(engine) as c:
                c.execute(sql(
                    "UPDATE code_task_runs SET finalize_ctx="
                    "COALESCE(finalize_ctx,'{}'::jsonb) || jsonb_build_object("
                    "'retired_tests', CAST(:r AS JSONB)), updated_at=now() WHERE id=:id"),
                    {"r": json.dumps(_weakened), "id": run_id})
                emit_event(c, project_id, "code_task.tests_retired", "boss",
                           "code_task_run", run_id,
                           {"retired": _weakened,
                            "declared_by": "task spec (allow_test_retirement)",
                            "note": "surfaced on the PR so a human can reverse it"})
        if do_separate_tests:
            gates["tests_written_first"] = bool(test_files)
            gates["coder_did_not_edit_tests"] = tests_untouched
        # BOUND BEFORE THE BRANCH THAT FILLS IT.
        #
        # This was declared inside `if do_review:` and read at the bottom of the function
        # regardless, so every run with do_review False -- which is every run at risk_tier 0 --
        # died with "cannot access local variable 'skipped_seats'". It is a UnboundLocalError,
        # so it surfaced as reason_code=pipeline_exception / failure_class=infra, which reads as
        # a platform problem rather than a two-line bug.
        skipped_seats: dict[str, str] = {}
        if do_review:
            # SEVERITY-CALIBRATED: a NIT must not sink a phase the way a BLOCKER does. Findings
            # below the threshold are carried in `deferred_findings` and spawned as their own
            # task after the PR opens, so nothing the reviewer caught is lost.
            _rev_pass, _blocking, _deferred = _review_gate_from_findings(
                r3.get("summary", ""), verdict,
                getattr(settings, "code_review_block_severity", "major"))
            deferred_findings = _deferred
            gates["code_review_passed"] = _rev_pass
            gates["security_clean"] = sec_verdict == "clean"
            # A SEAT THE PROFILE DID NOT CONVENE IS NOT A FAILED CHECK.
            # Removing a reviewer from the panel while leaving its gate expecting a verdict
            # made that gate FAIL ("not_run" != "clean"), and a failing gate is exactly what
            # the in-run convergence loop below tries to fix — by running the very reviewer
            # the profile had disabled, one at a time instead of in parallel. So `lean` ran
            # the full adversarial review anyway, slower. Measured on crun_9b8e732b:
            # review_code 11 turns/$0.66 + security_check 10 turns/$0.56 on a run whose
            # panel was ["regression"].
            # Mark them satisfied-because-not-applicable, and record WHY in the evidence so
            # the PR still says plainly that no reviewer looked at this.
            for _seat, _gate in (("review_code", "code_review_passed"),
                                 ("security_check", "security_clean"),
                                 ("consistency_check", "consistent")):
                if _seat not in enabled_seats:
                    gates[_gate] = True
                    # NB: `evidence` is not bound until further down, so collect here and
                    # merge below. Reading it here would NameError exactly when a profile
                    # skips a seat — i.e. on every lean run.
                    skipped_seats[_seat] = (
                        f"not convened: profile '{profile.get('name')}' does not include this seat")
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
            + (["iac_verify"] if iac_result is not None else []) \
            + (["serve_check"] if "app_serves" in gates else [])
        workspace_evidence = bind_test_outcomes(
            workspace_evidence or {}, tests_passed=bool(tests_passed),
        )
        evidence = {
            "risk_tier": risk_tier,
            "profile": profile["name"],
            **({"web_package_contract": _web_contract} if _web_contract is not None else {}),
            # Named here (not written earlier) because `evidence` does not exist until this
            # point — writing to it above was a latent crash that would have fired precisely
            # when a run DID weaken an existing test, i.e. when the gate matters most.
            **({"weakened_existing_tests": _weakened[:20]} if _weakened else {}),
            # What BOSS started, and whether it answered — the receipt for the app_serves gate.
            "app_serves": app_serves_record,
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
            # Sub-threshold review findings ship WITH the change and become their own task —
            # recorded here so the receipt shows what was knowingly deferred, not hidden.
            "deferred_findings": deferred_findings,
            "test_arbitrations": test_arbitrations,
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
            # Every stage's route (model_router): tier, reason, escalation, and the signals.
            "routing": stage_router.evidence(),
            "engine": stage_routes.get("implement", {}).get("engine", build_engine),
            "build_engine": build_engine,
            "review_engine": review_engine,
            "model": r2.get("model") or r2.get("usage", {}).get("model"),
            "review_model": (r3.get("model") or r3.get("usage", {}).get("model")
                             or r4.get("model") or r6.get("model")),
            "substrate": "microvm" if use_microvm else "local",
            "sandbox_task_ids": guest_vm_ids,
            "test_author_independent": do_separate_tests, "reviewer_independent": do_review,
            "tests_passed": official_tests_passed, "adversarial_verdict": verdict,
            "test_files": test_files, "impl_files": impl_files,
            "test_command": test_cmd, "test_exit_code": official_test_exit,
            "verify_cwd": last_verify_cwd,
            "last_verify_output": official_verify_output or last_verify_output,
            "tests": (
                [{
                    "command": test_cmd,
                    "exit_code": test_exit_code,
                    "timed_out": False,
                    "ok": bool(tests_passed),
                }]
                if test_cmd and test_exit_code is not None
                else []
            ),
            "files_changed": (len(impl_files) + len(test_files)) or None,
            "turns": (
                sum(
                    int((res or {}).get("turns") or ((res or {}).get("usage") or {}).get("turns") or 0)
                    for res in (r1, r2, r3, r4, r5, r6)
                    if isinstance(res, dict)
                ) or None
            ),
            # VAG Phase 3 repair loop: how many evidence-fed retries the implementer used
            # (0 = passed/failed first try) and the per-attempt outcome trail.
            "repair_attempts": repair_attempts, "repair_max": max_repair,
            "repair_log": repair_log, "impl_escalated_recovered": escalated_recovered,
            "quality_passes": quality_passes_used,
            "ui_evidence": (workspace_evidence or {}).get("ui_evidence") or {},
            "ux_evidence": (workspace_evidence or {}).get("ux_evidence") or {},
            "capability_evidence": (workspace_evidence or {}).get("capability_evidence") or {},
            "interface_evidence": (workspace_evidence or {}).get("interface_evidence") or {},
            "qa_summary": r1.get("summary", "")[:400],
            "impl_summary": r2.get("summary", "")[:400],
            "review_summary": r3.get("summary", "")[:4000],
            "review_seat": review_seat,
            "security_summary": r4.get("summary", "")[:4000],
            "consistency_summary": r6.get("summary", "")[:1500],
        }
        if skipped_seats:
            # Record NOT-RUN seats on the run. `lean` marking these gates satisfied must never
            # read as "a reviewer approved this" — the PR has to say plainly that none looked.
            evidence.update({f"{k}_skipped": v for k, v in skipped_seats.items()})

        with _transaction(engine) as c:
            c.execute(sql("UPDATE code_task_runs SET state=CAST(:s AS JSONB), "
                           "usage=jsonb_build_object('cost_usd_micros', CAST(:cost AS bigint), "
                           "'model', CAST(:m AS text)), updated_at=now() WHERE id=:id"),
                      {"s": json.dumps(evidence), "cost": cost,
                       "m": evidence.get("model"), "id": run_id})
            qg_id, qg_status = _record_quality_gate(
                c, run_id=run_id, project_id=project_id, task_id=task_id, job_id=job_id,
                evidence=evidence, gates=gates)

        # Materialise the panel's findings as comments NOW — before any fix round runs — so the
        # implementer, the pull request and list_review_comments all read one recorded set
        # rather than three renderings of the same prose at three different moments. Placed
        # after `evidence` is assembled, for the reason the comment inside it already gives.
        _record_review_comments(engine, run_id, project_id, evidence)

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
            # An experiment's arm (experiments.ARM_KNOBS) may set this build's rounds.
            _spec_rounds = spec.get("review_rounds") if isinstance(spec, dict) else None
            inrun_max = effective_review_rounds(
                _spec_rounds if isinstance(_spec_rounds, int)
                else getattr(settings, "code_review_inrun_max_rounds", 2))
            # Test gates are convergeable too. Previously a failing test skipped this loop entirely
            # and fell through to a FULL cold re-run — discarding the warm session and re-paying for
            # spec/plan — which is why repeated attempts never converged on test failures.
            _convergeable = {"code_review_passed", "consistent",
                             "written_tests_pass", "nothing_broke",
                             "web_package_honest", "app_serves"}
            if (failing and not use_microvm and impl_session_id
                    and set(failing) <= _convergeable):
              try:  # a convergence failure must DEGRADE to the legacy paths, never kill the run
                for round_i in range(1, inrun_max + 1):
                    finding_bits = []
                    # TEST ARBITRATION: before making the implementer chase the oracle, establish
                    # that the oracle is right. An over-specified test is unsatisfiable, and every
                    # round spent against it is wasted.
                    if ("written_tests_pass" in failing
                            and getattr(settings, "code_test_arbiter_enabled", True)):
                        _tr = _run_verify()
                        if not _tr.ok:
                            with _transaction(engine) as c:
                                _phase(c, "test_arbitration")
                            set_llm_context(routes=_route_ids(), db_engine=engine, project_id=project_id,
                                            run_id=run_id, stage="review_code")
                            a_arb, m_arb = _route("review_code")
                            r_arb = a_arb.run_role(
                                _test_arbiter_prompt(
                                    title, criteria, instructions=instructions,
                                    test_output=_tr.stdout,
                                    change=_render_change_for_review(
                                        {fc.path: fc.content for fc in sandbox.diff()})),
                                review_root, read_only=True, model=m_arb)
                            cost += int(r_arb.get("cost_usd_micros") or 0)
                            ruling, target = _parse_arbiter(r_arb.get("summary", ""))
                            test_arbitrations.append({
                                "round": round_i, "ruling": ruling, "target": target,
                                "rationale": (r_arb.get("summary") or "")[:1200]})
                            with _transaction(engine) as c:
                                emit_event(c, project_id, "code_task.test_arbitration", "boss",
                                           "code_task_run", run_id,
                                           {"round": round_i, "ruling": ruling, "target": target})
                            if ruling == "test_wrong":
                                # ONLY the test-author lane may touch the oracle — never the
                                # implementer's session, so `coder_did_not_edit_tests` keeps meaning.
                                with _transaction(engine) as c:
                                    _phase(c, "write_tests")
                                set_llm_context(routes=_route_ids(), db_engine=engine, project_id=project_id,
                                                run_id=run_id, stage="write_tests")
                                a_ta, m_ta = _route("write_tests")
                                r_ta = a_ta.run_role(
                                    _test_amend_prompt(criteria, target,
                                                       r_arb.get("summary", "")),
                                    root, model=m_ta)
                                cost += int(r_ta.get("cost_usd_micros") or 0)
                            else:
                                finding_bits.append(
                                    "FAILING TESTS (the arbiter confirmed these tests are "
                                    "CORRECT — fix the implementation, do NOT touch the "
                                    "tests):\n" + (_tr.stdout or "")[:3000])
                    # THE REVIEW COMMENTS, as recorded when the review ran. One stored set is
                    # read here, posted on the pull request and shown by list_review_comments,
                    # so the implementer and a human are looking at the same thing. Operator
                    # comments sort first: a person who pointed at a line has said something
                    # more specific than a reviewer did, and a repair round is bounded.
                    _open = _open_review_comments(engine, run_id, project_id)
                    if _open:
                        finding_bits.insert(0, "REVIEW COMMENTS ON THIS CHANGE:\n"
                                            + review_comments.render_for_implementer(_open, root))
                    elif "code_review_passed" in failing:
                        # Nothing was recorded — a reviewer that emitted no parseable finding
                        # must still reach the implementer rather than be silenced.
                        finding_bits.append(_anchored_findings(
                            r3.get("summary") or "", root, stage="review_code",
                            header="ADVERSARIAL REVIEW FINDINGS:"))
                    if "consistent" in failing and not _open:
                        finding_bits.append(_anchored_findings(
                            r6.get("summary") or "", root, stage="consistency_check",
                            header="CONSISTENCY FINDINGS:"))
                    if "app_serves" in failing:
                        # The app's OWN error, not "preview timed out": this is what lets the
                        # implementer fix an ImportError instead of guessing at a port.
                        finding_bits.append(
                            "THE APP DOES NOT SERVE HTTP. "
                            + _app_serves_sentence(app_serves_record)[:2600]
                            + "\nFix the app (or its boss.preview.command) so that command "
                            "serves HTTP on $PORT.")
                    with _transaction(engine) as c:
                        emit_event(c, project_id, "code_task.inrun_convergence_round", "boss",
                                   "code_task_run", run_id,
                                   {"round": round_i, "of": inrun_max, "failing_gates": failing})
                        _phase(c, "implement")
                    # A TEST_WRONG round amends only the oracle — there is nothing for the
                    # implementer to do, and resuming it with an empty brief invites churn.
                    if finding_bits:
                        set_llm_context(routes=_route_ids(), db_engine=engine, project_id=project_id, run_id=run_id,
                                        stage="implement")
                        a_fix, m_fix = _route("implement")
                        # A ROUND THAT CHANGES NOTHING IS NOT A ROUND OF PROGRESS.
                        #
                        # Measured on a live loop (deepseek implementing, Claude reviewing): one
                        # fix round in two read the anchored comment, called two tools, edited
                        # NO file and returned a confident summary. The convergence budget is
                        # bounded, so a silent no-op spends a third of it and the next round
                        # starts from the same code with the same findings. Nothing in the run
                        # said so — the phase looked identical to one that worked.
                        _before = {fc.path: fc.content for fc in sandbox.diff()}
                        rfix = a_fix.run_role(
                            _targeted_fix_prompt("\n\n".join(finding_bits), round_i), root,
                            model=m_fix, resume_session_id=impl_session_id)
                        impl_session_id = rfix.get("session_id") or impl_session_id
                        cost += int(rfix.get("cost_usd_micros") or 0)
                        if {fc.path: fc.content for fc in sandbox.diff()} == _before:
                            logger.warning(
                                "code_task.fix_round_changed_nothing run=%s round=%s", run_id,
                                round_i)
                            with _transaction(engine) as c:
                                emit_event(c, project_id, "code_task.fix_round_noop", "boss",
                                           "code_task_run", run_id,
                                           {"round": round_i, "of": inrun_max,
                                            "failing_gates": failing,
                                            "cost_usd_micros": int(rfix.get("cost_usd_micros") or 0)})
                        r2 = {**r2, "summary": (rfix.get("summary") or r2.get("summary", ""))}
                    # Re-verify against the oracle. `nothing_broke` is the same full-suite run
                    # (regressions_ok = panel["full"]), so one execution refreshes both gates.
                    _post = _run_verify()
                    tests_passed = bool(_post.ok)
                    official_tests_passed = tests_passed
                    official_test_exit = _post.exit_code
                    official_verify_output = last_verify_output
                    gates["written_tests_pass"] = tests_passed
                    if "nothing_broke" in gates:
                        gates["nothing_broke"] = tests_passed
                    if tests_passed and "app_serves" in gates:
                        # Green again: re-prove the app against the code as it now stands.
                        app_serves_record = _check_app_serves(
                            {fc.path: fc.content for fc in sandbox.diff()})
                        gates["app_serves"] = bool(app_serves_record.get("ok"))
                        evidence["app_serves"] = app_serves_record
                    if not tests_passed:
                        # Still red is no longer a dead end: re-arbitrate next round (the fix may
                        # be partial, or an amended test still over-specified). Only exhausting
                        # the rounds drops through to the legacy repair/block paths.
                        failing = [k for k, v in gates.items() if not v]
                        if round_i >= inrun_max or (set(failing) - _convergeable):
                            break
                        continue
                    change_block = _render_change_for_review(
                        {fc.path: fc.content for fc in sandbox.diff()})
                    impl_files = [fc.path for fc in sandbox.diff()
                                  if not _is_test_path(fc.path)] or impl_files
                    if "code_review_passed" in failing:
                        with _transaction(engine) as c:
                            _phase(c, "review_code")
                        set_llm_context(routes=_route_ids(), db_engine=engine, project_id=project_id,
                                        run_id=run_id, stage="review_code")
                        rr = review_agent.run_role(
                            _review_prompt(title, criteria, instructions=instructions,
                                           ui=ui_task, change=change_block),
                            review_root, read_only=True, model=review_model)
                        cost += int(rr.get("cost_usd_micros") or 0)
                        verdict = _parse_verdict(rr.get("summary", ""))
                        r3 = rr
                        review_confidence = 1.0 if verdict == "survived" else 0.0
                        _rev_pass, _blocking, _deferred = _review_gate_from_findings(
                            rr.get("summary", ""), verdict,
                            getattr(settings, "code_review_block_severity", "major"))
                        deferred_findings = _deferred
                        # Materialise the panel's findings as comments NOW, before any fix
                        # round runs, so the implementer and the pull request read the
                        # same artifact instead of two renderings of the same prose.
                        _record_review_comments(engine, run_id, project_id, evidence)
                        _record_review_comments(engine, run_id, project_id, evidence)
                        gates["code_review_passed"] = _rev_pass
                    if "consistent" in failing:
                        with _transaction(engine) as c:
                            _phase(c, "consistency_check")
                        set_llm_context(routes=_route_ids(), db_engine=engine, project_id=project_id,
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
                            "deferred_findings": deferred_findings,
                            "test_arbitrations": test_arbitrations,
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
                    if set(failing) - {"code_review_passed", "consistent", "app_serves"}:
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
            gate_faults = classify_gate_faults(failing=failing, evidence=evidence)
            # Non-final decomposition slices suppress the PR and advance the chain on
            # completed_no_pr. They must NOT take ship_with_dissent: that path marks the
            # slice completed, skips the deferred-findings follow-up (spawned only after a
            # PR opens), and lets advance_decomposition build the next slice on unresolved
            # security/test failures. Treat them like open_pr=false for gate disposition.
            _didx, _dtot = fctx.get("decomposition_index"), fctx.get("decomposition_total")
            _nonfinal_slice = (isinstance(_didx, int) and isinstance(_dtot, int)
                               and _dtot > 1 and _didx < _dtot)
            _can_ship_pr = spec.get("open_pr", True) is not False and not _nonfinal_slice
            action = resolve_gate_failure_action(
                failing=failing,
                advisory_gate=advisory_gate,
                rounds_used=rounds_used,
                max_repair_rounds=max_repair_rounds,
                open_pr=_can_ship_pr,
                never_block=bool(getattr(settings, "code_gate_never_block", True)),
                faults=gate_faults,
            )
            if action == "inconclusive":
                # The checks never finished, so nothing is known about the code. Re-run the
                # CHECK with an escalated budget — never the coding agent. Bounded; if it is
                # still inconclusive we ship with the dissent recorded honestly as "not
                # reviewed", which is a materially different claim from "review failed".
                escalations = int(fctx.get("check_escalations", 0) or 0)
                max_escalations = max(0, int(
                    getattr(settings, "code_review_max_escalations", 2) or 2))
                with _transaction(engine) as c:
                    emit_event(c, project_id, "code_task.check_inconclusive", "boss",
                               "code_task_run", run_id,
                               {"failing_gates": failing, "faults": gate_faults,
                                "escalation": escalations + 1, "of": max_escalations,
                                "reason": "check did not complete — escalating the CHECK, "
                                          "not repairing the code"})
                if escalations < max_escalations:
                    base_turns = int(getattr(settings, "code_agent_review_max_turns", 16) or 16)
                    escalated = min(120, base_turns * (2 ** (escalations + 1)))
                    for gate in failing:
                        if gate != "code_review_passed":
                            continue
                        with _transaction(engine) as c:
                            _phase(c, "review_code")
                        set_llm_context(routes=_route_ids(), db_engine=engine, project_id=project_id,
                                        run_id=run_id, stage="review_code")
                        rr = review_agent.run_role(
                            _review_prompt(title, criteria, instructions=instructions,
                                           ui=ui_task, change=change_block),
                            review_root, read_only=True, model=review_model,
                            max_turns=escalated)
                        cost += int(rr.get("cost_usd_micros") or 0)
                        verdict = _parse_verdict(rr.get("summary", ""))
                        r3 = rr
                        _rev_pass, _blocking, _deferred = _review_gate_from_findings(
                            rr.get("summary", ""), verdict,
                            getattr(settings, "code_review_block_severity", "major"))
                        deferred_findings = _deferred
                        # Materialise the panel's findings as comments NOW, before any fix
                        # round runs, so the implementer and the pull request read the
                        # same artifact instead of two renderings of the same prose.
                        _record_review_comments(engine, run_id, project_id, evidence)
                        gates["code_review_passed"] = _rev_pass
                        evidence["review_summary"] = (rr.get("summary") or "")[:4000]
                        evidence["adversarial_verdict"] = verdict
                        evidence["review_escalated_turns"] = escalated
                    failing = [k for k, v in gates.items() if not v]
                    with _transaction(engine) as c:
                        c.execute(sql(
                            "UPDATE code_task_runs SET finalize_ctx="
                            "COALESCE(finalize_ctx,'{}'::jsonb) || jsonb_build_object("
                            "'check_escalations', CAST(:n AS int)), updated_at=now() "
                            "WHERE id=:id"), {"n": escalations + 1, "id": run_id})
                    gate_faults = classify_gate_faults(failing=failing, evidence=evidence)
                    action = resolve_gate_failure_action(
                        failing=failing, advisory_gate=advisory_gate,
                        rounds_used=rounds_used, max_repair_rounds=max_repair_rounds,
                        open_pr=_can_ship_pr,
                        never_block=bool(getattr(settings, "code_gate_never_block", True)),
                        faults=gate_faults)
                if action == "inconclusive":
                    # Escalations exhausted: ship, recording that the code was NOT reviewed.
                    # Still respect _can_ship_pr — a non-final slice / open_pr=false run must
                    # not ship_with_dissent and advance broken mid-chain work.
                    action = ("ship_with_dissent"
                              if (bool(getattr(settings, "code_gate_never_block", True))
                                  and _can_ship_pr)
                              else "block")
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
            elif action == "ship_with_dissent":
                # AUTONOMY TERMINAL: convergence and repair are spent and the panel is still
                # unhappy. Ship, but make the dissent impossible to miss — the failing gates and
                # the reviewer's reasoning land on the run AND become their own task, so a human
                # can reverse this without having to discover it first.
                dissent = {
                    "unresolved_gates": failing,
                    "adversarial_verdict": verdict,
                    "review_confidence": review_confidence,
                    "review_summary": (r3.get("summary") or "")[:2000],
                    "consistency_summary": (r6.get("summary") or "")[:1000],
                    "rounds_used": rounds_used,
                    "quality_gate_id": qg_id,
                }
                evidence["unresolved_dissent"] = dissent
                # Carry the unresolved gates into the follow-up task alongside deferred findings.
                deferred_findings = list(deferred_findings) + [
                    {"severity": "major", "text": f"Unresolved quality gate '{g}' at ship time"}
                    for g in failing]
                with _transaction(engine) as c:
                    c.execute(sql(
                        "UPDATE code_task_runs SET state=CAST(:s AS JSONB), "
                        "finalize_ctx=COALESCE(finalize_ctx,'{}'::jsonb) "
                        "|| jsonb_build_object('unresolved_dissent', CAST(:d AS JSONB)), "
                        "updated_at=now() WHERE id=:id"),
                        {"s": json.dumps(evidence), "d": json.dumps(dissent), "id": run_id})
                    emit_event(c, project_id, "code_task.shipped_with_dissent", "boss",
                               "code_task_run", run_id, dissent)
                # Fall through to GATE PASSED below — ship it.
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
                # COOPERATIVE CANCELLATION. Cancelling marks the run row and kills the queued
                # JOB, but the pipeline already executing inside the worker keeps going — so a
                # run cancelled 25 minutes earlier reached this point, spawned a repair child,
                # and two runs burned tokens on the same lineage concurrently. Before spending
                # money on a child, confirm this run is still meant to be alive.
                with _transaction(engine) as c:
                    _cur = c.execute(sql("SELECT status FROM code_task_runs WHERE id=:id"),
                                     {"id": run_id}).scalar()
                if _cur in ("cancelled", "failed"):
                    with _transaction(engine) as c:
                        emit_event(c, project_id, "code_task.repair_abandoned", "boss",
                                   "code_task_run", run_id,
                                   {"reason": f"run is {_cur}; not spawning a repair child",
                                    "failing_gates": failing})
                    return {"status": _cur, "done": True, "cancelled_before_repair": True}
                # PERSIST THE WORK BEFORE HANDING IT TO A REPAIR.
                # The sandbox is ephemeral and the work branch was only ever pushed when a PR
                # was opened. A run that goes to `repairing` never opens one, so its entire tree
                # was DESTROYED at teardown — and retry_code_task's promise to the next attempt
                # ("the previous attempt's code is ALREADY PRESENT in your working tree on
                # branch X") was false. The child cloned a branch that did not exist, found
                # nothing, and rebuilt from scratch. Proven on crun_bda12caa: it authored 2,320
                # lines, opened no PR, no lineage branch exists on the remote, and its reviewer
                # reported being handed "a different tree" with 158 tests instead of 240.
                # Every "repair" in this program has therefore been a disguised rebuild.
                try:
                    _files = (list(accumulated.items()) if use_microvm
                              else [(fc.path, fc.content) for fc in sandbox.diff()])
                    if _files:
                        sf.github.ensure_branch(work_branch, base_branch)
                        for _path, _content in _files:
                            sf.github.upsert_file(
                                work_branch, _path, _content,
                                f"wip({run_id}): persist attempt for repair")
                        with _transaction(engine) as c:
                            emit_event(c, project_id, "code_task.work_persisted", "boss",
                                       "code_task_run", run_id,
                                       {"branch": work_branch, "files": len(_files),
                                        "why": "so the repair patches this tree instead of "
                                               "rebuilding it from nothing"})
                except Exception as exc:  # never let persistence failure block the repair
                    with _transaction(engine) as c:
                        emit_event(c, project_id, "code_task.work_persist_failed", "boss",
                                   "code_task_run", run_id,
                                   {"branch": work_branch,
                                    "error": f"{type(exc).__name__}: {exc}"[:200]})
                from .code_task_service import retry_code_task
                nxt = retry_code_task(
                    engine, settings, jobs, run_id,
                    fix_note=("Autonomous review-repair: the quality gate blocked on "
                              f"{', '.join(failing)}. Address EVERY review finding below so ALL "
                              "gates pass, and do NOT regress the checks that already passed. If "
                              "'tests_strong_enough' failed, STRENGTHEN the tests so a one-line "
                              "mutation to the logic is caught."
                              # The app's own startup error, so the repair fixes THAT rather
                              # than rediscovering it (crun_3e370c11's ImportError).
                              + (" 'app_serves' failed: "
                                 + _app_serves_sentence(app_serves_record)[:1600]
                                 if "app_serves" in failing else "")))
                with _transaction(engine) as c:
                    # Fenced: a stale execution must not be able to declare a repair round.
                    if not _fenced_status(
                            "repairing",
                            "phase='done', error=:e, finalize_ctx=COALESCE(finalize_ctx,'{}'::jsonb) "
                            "|| jsonb_build_object('review_repair_next_run', CAST(:nx AS text), "
                            "'review_repair_round', CAST(:rd AS int), "
                            "'failing_gates', CAST(:fg AS JSONB))",
                            {"e": f"Auto review-repair round {rounds_used + 1} (gates: {failing}); "
                                  f"iterating autonomously in {nxt.get('run_id')}.",
                             "fg": json.dumps(list(failing)),
                             "nx": nxt.get("run_id"), "rd": rounds_used + 1}):
                        emit_event(c, project_id, "code_task.terminal_write_refused", "boss",
                                   "code_task_run", run_id, {"attempted": "repairing"})
                return {"status": "repairing", "done": True, "review_repair": True,
                        "next_run_id": nxt.get("run_id"), "round": rounds_used + 1,
                        "failing_gates": failing}
            elif action == "block":
                _finish_vms(success=False)  # retain the stage VMs for inspection
                _blocked_error, _blocked_reason = blocked_gate_messages(
                    qg_id, failing, app_serves_record)
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
                        {"e": _blocked_error, "fc": json.dumps(ctx), "id": run_id})
                _rescue_pr(_blocked_reason)
                return {"status": "blocked", "done": True, "awaiting_gate_override": True,
                        "quality_gate_id": qg_id, "failing_gates": failing}

        # GATE PASSED — open the PR via the existing software-factory path.
        _finish_vms(success=True)
        _declare_preview_app()
        # ONE APPROVAL PER BUILD, not per slice. A decomposed build breaks a product into ordered
        # slices that all commit to the SAME lineage branch (via _commit_stage). Opening a PR +
        # human approval on every slice is what a customer never wants — 6 tasks, 6 PRs, 6 gates
        # for one website. So only the FINAL slice opens a PR (the whole accumulated site, one
        # approval); earlier slices complete silently on the shared branch and advance the chain.
        # Recompute for the happy path (gate-failure branch may not have run).
        _didx, _dtot = fctx.get("decomposition_index"), fctx.get("decomposition_total")
        _nonfinal_slice = (isinstance(_didx, int) and isinstance(_dtot, int)
                           and _dtot > 1 and _didx < _dtot)
        if spec.get("open_pr", True) is False or _nonfinal_slice:
            # Eval/bake-off runs (open_pr=false) measure the gate not the delivery; a non-final
            # decomposition slice defers its PR to the final slice. Either way: record the verified
            # outcome, keep the work on the shared branch, and let the chain advance.
            # Same reason as the PR path, and the same rule: its OWN transaction,
            # committed before the terminal write takes a lock on the same row.
            _close_phase()
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
                if not _fenced_status("completed", "phase='done'"):
                    emit_event(c, project_id, "code_task.terminal_write_refused", "boss",
                               "code_task_run", run_id, {"attempted": "completed_no_pr"})
                emit_event(c, project_id, "code_task.completed_no_pr", "boss",
                           "code_task_run", run_id,
                           {"quality_gate_id": qg_id,
                            "reason": "decomposition_slice" if _nonfinal_slice else "open_pr_false",
                            "slice": f"{_didx}/{_dtot}" if _nonfinal_slice else None})
            plane_sync.on_event(engine, settings, run_id, "code_task.completed_no_pr")  # P2
            return {"status": "completed", "done": True, "quality_gate_id": qg_id,
                    "pr_suppressed": True, "decomposition_nonfinal": _nonfinal_slice}

        with _transaction(engine) as c:
            _phase(c, "opening_pr")
            # The budget reservation is taken for 45 minutes at run start. propose_pull_request
            # BLOCKS when it has expired ("Missing active reservation for PR proposal"), so any
            # run longer than 45 minutes could not open a PR at all — regardless of how good the
            # code was. Observed on crun_9f8febe8: every quality gate resolved, the work was
            # finished and PAID FOR, and the PR was refused purely because a stalled stage had
            # pushed the run past the reservation window.
            #
            # Blocking here saves nothing: the spend has ALREADY happened. Refusing the PR only
            # destroys the work the user paid for. So re-acquire a reservation and proceed.
            try:
                if not budget.assert_active_reservation(c, project_id, reservation_id):
                    renewed = budget.reserve(
                        c, project_id, category="code_task",
                        amount_usd_micros=int(settings.code_task_budget_usd_micros),
                        reason=f"re-reserve to open PR (original expired): {title[:48]}",
                        expires_in_minutes=30, job_id=job_id, risk_tier=spec.get("risk_tier"))
                    if renewed.get("status") == "reserved":
                        reservation_id = renewed["reservation_id"]
                        emit_event(c, project_id, "code_task.reservation_renewed", "boss",
                                   "code_task_run", run_id,
                                   {"reason": "original reservation expired before the PR step; "
                                              "the spend already occurred, so the PR proceeds",
                                    "reservation_id": reservation_id})
            except Exception as exc:  # never let bookkeeping sink finished work
                emit_event(c, project_id, "code_task.reservation_renew_failed", "boss",
                           "code_task_run", run_id, {"error": f"{type(exc).__name__}: {exc}"[:200]})
            # THE STOP WINS WHEN IT CAN (factory walk D1). The PR step is one long transaction
            # with no phase change inside, so a Stop pressed during it was only seen after the
            # PR existed. Checked right before the first GitHub write (the push and the PR).
            _abort_if_cancelled("before opening the PR")
            proposal = sf.propose_pull_request(
                c, project_id, task_id, reservation_id=reservation_id,
                dedupe_key=f"code:task:{run_id}", title=title,
                branch_name=work_branch,
                summary=(f"Governed pipeline: tests by qa, impl by SE, adversarial review SURVIVED. "
                         f"QualityGate {qg_id} PASS.\n\n{r2.get('summary','')}"
                         + agent_acts.pr_trailers(c, run_id)),
                files=([FileChange(path=p, content=content) for p, content in accumulated.items()]
                       if use_microvm else sandbox.diff()),
                created_by_job_id=job_id, actual_usd_micros=cost,
                evidence=evidence, quality_gate_id=qg_id, run_id=run_id)

        status = proposal.get("status")
        # RETRY THE PR ITSELF. A transient GitHub error, an expired reservation, a network
        # blip — none of these are reasons to hand the operator nothing after a run that
        # PASSED its gates. Two runs blocked tonight on "PR not created: failed" having done
        # all the work. The PR is the deliverable; opening it gets the same persistence as
        # any other critical step.
        for _pr_attempt in (2, 3):
            if status in ("created", "succeeded", "reused"):
                break
            with _transaction(engine) as c:
                emit_event(c, project_id, "code_task.pr_retry", "boss", "code_task_run", run_id,
                           {"attempt": _pr_attempt, "prior_status": status})
                # First attempt may have released the reservation (model overflow,
                # missing entitlement). Retries with the dead id only mint
                # "Missing active reservation" decisions. Re-reserve each try.
                try:
                    if not budget.assert_active_reservation(c, project_id, reservation_id):
                        renewed = budget.reserve(
                            c, project_id, category="code_task",
                            amount_usd_micros=int(settings.code_task_budget_usd_micros),
                            reason=f"re-reserve PR retry {_pr_attempt}: {title[:48]}",
                            expires_in_minutes=30, job_id=job_id, risk_tier=spec.get("risk_tier"))
                        if renewed.get("status") == "reserved":
                            reservation_id = renewed["reservation_id"]
                            emit_event(
                                c, project_id, "code_task.reservation_renewed", "boss",
                                "code_task_run", run_id,
                                {"reason": "retry after reservation release/expiry",
                                 "attempt": _pr_attempt, "reservation_id": reservation_id},
                            )
                except Exception as exc:  # never let bookkeeping sink finished work
                    emit_event(
                        c, project_id, "code_task.reservation_renew_failed", "boss",
                        "code_task_run", run_id,
                        {"attempt": _pr_attempt, "error": f"{type(exc).__name__}: {exc}"[:200]},
                    )
                _abort_if_cancelled("before retrying the PR")
                try:
                    proposal = sf.propose_pull_request(
                        c, project_id, task_id, reservation_id=reservation_id,
                        dedupe_key=f"code:task:{run_id}", title=title,
                        branch_name=work_branch,
                        summary=(f"Governed pipeline: tests by qa, impl by SE. "
                                 f"QualityGate {qg_id}.\n\n{r2.get('summary','')}"
                                 + agent_acts.pr_trailers(c, run_id)),
                        files=([FileChange(path=p, content=content)
                                for p, content in accumulated.items()]
                               if use_microvm else sandbox.diff()),
                        created_by_job_id=job_id, actual_usd_micros=cost,
                        evidence=evidence, quality_gate_id=qg_id, run_id=run_id)
                    status = proposal.get("status")
                except Exception as exc:  # noqa: BLE001
                    emit_event(c, project_id, "code_task.pr_retry_failed", "boss",
                               "code_task_run", run_id,
                               {"attempt": _pr_attempt, "error": f"{type(exc).__name__}: {exc}"[:200]})
        # AN ALREADY-OPEN PULL REQUEST IS A SUCCESS, NOT A FAILURE.
        #
        # The retry loop above breaks on "reused" — it knows a deduped effect means the work is
        # done — and then this check rejected the very status the loop accepted. Two conditions
        # about the same thing, two lines apart, disagreeing.
        #
        # The cost: a run interrupted AFTER its PR was opened could never complete. On the
        # re-delivery the proposal hits its dedupe key, returns "reused", and the run is blocked
        # with `PR not created after 3 attempts: reused` and a null pull_request_url — while the
        # pull request sits open on GitHub. Measured on crun_1da493dc, whose PR was created at
        # 23:50:46 and whose run ended blocked an hour later.
        #
        # Guarded on actually HAVING a url: a reuse of some earlier effect that produced no pull
        # request is not a shipped change, and must still block.
        _reused_pr = status == "reused" and bool(proposal.get("pull_request_url"))
        if status not in ("created", "succeeded") and not _reused_pr:
            rescued = _rescue_pr(f"the pull-request step failed 3 times ({status})")
            with _transaction(engine) as c:
                c.execute(sql("UPDATE code_task_runs SET status='blocked', phase='done', "
                               "decision_id=:d, error=:e, updated_at=now() WHERE id=:id"),
                          {"d": proposal.get("decision_id"),
                           "e": (f"PR not created after 3 attempts: {status}"
                                 + (f" — partial work shipped as {rescued}" if rescued else "")),
                           "id": run_id})
            return {"status": "blocked", "done": True, "quality_gate_id": qg_id, **proposal}

        # The findings go ON the pull request, at the lines they are about.
        evidence["review_comments_posted"] = _post_review_comments_to_pr(
            sf, proposal, evidence, engine, project_id, run_id)

        # Bank `opening_pr` before the terminal write erases the clock it was measured
        # by — in its OWN transaction, committed first, never nested inside the one the
        # terminal write runs in.
        _close_phase()
        with _transaction(engine) as c:
            _ok = _fenced_status(
                "completed",
                "phase='done', pull_request_url=:u, pull_request_number=:n, decision_id=:d",
                {"u": proposal.get("pull_request_url"),
                 "n": proposal.get("pull_request_number"),
                 "d": proposal.get("decision_id")})
            if not _ok:
                # Another execution already finished this run. Do not overwrite its verdict.
                emit_event(c, project_id, "code_task.terminal_write_refused", "boss",
                           "code_task_run", run_id, {"attempted": "completed"})
            emit_event(c, project_id, "code_task.pr_opened", "boss", "code_task_run", run_id,
                       {"pull_request_url": proposal.get("pull_request_url"),
                        "decision_id": proposal.get("decision_id"), "quality_gate_id": qg_id})
        plane_sync.on_event(engine, settings, run_id, "code_task.pr_opened")  # P2: comment the PR link
        # WHAT THIS RUN COST, DELIVERED AND LOST — recorded now, while every fact is fresh.
        #
        # Answering "what went wrong" on an 85-minute build took two hours of reading container
        # logs, job leases, sandbox file mtimes and gate events by hand, and every fact needed
        # was already in the database. Deterministic and read-only, so it is a summary of
        # evidence rather than a second opinion about it, and wrapped because a retrospective
        # that can fail the run it describes is worse than none.
        try:
            from .run_retrospective import record as _record_retrospective
            with _transaction(engine) as c:
                _record_retrospective(c, project_id, run_id)
        except Exception:  # noqa: BLE001
            logger.warning("retrospective not recorded for %s", run_id, exc_info=True)

        # AUTO-PREVIEW FOR APPS. Operator: previews matter for an APP — a service can be
        # exercised by its tests or a container deploy in CI/CD instead. A browsable app whose
        # PR just opened should already be running when the operator looks at it, rather than
        # requiring a click and a ~1 minute build first.
        #
        # The distinction is made from what the run BUILT, not from the task's wording:
        # resolve_app_entry returns "static"/"node" for something a browser can open, and "py"
        # for a server-shaped entrypoint. Only the former is auto-previewed. Deliberately
        # best-effort and AFTER the PR is recorded — a preview failure must never affect the
        # delivery, and the operator can still press Run preview for anything skipped.
        try:
            if getattr(settings, "auto_preview_apps", True) and not fctx.get("is_followup"):
                # Ask the SAME source the preview resolver uses, not a different one.
                # This first read `accumulated`, which on the local substrate holds only what
                # _checkpoint pushed — test files — so a stock trading app with
                # web-stock/index.html was classified a service and never previewed. Its
                # state.impl_files had the html all along. One source of truth about "what did
                # this task build", or the two answers drift. Again.
                from .preview_service import lineage_impl_files as _lineage_files
                try:
                    with _transaction(engine) as _c:
                        _row = _c.execute(sql(
                            "SELECT state, finalize_ctx, id FROM code_task_runs WHERE id=:i"),
                            {"i": run_id}).mappings().first()
                    _built = _lineage_files(engine, dict(_row)) if _row else []
                except Exception:  # noqa: BLE001
                    _built = []
                if not _built:
                    _built = [pth for pth in accumulated if not _is_test_path(pth)]
                _is_browsable = any(
                    pth.endswith((".html", ".jsx", ".tsx", ".vue", ".svelte")) for pth in _built)
                if _is_browsable:
                    from .preview_service import start_preview_for_run
                    _pv = start_preview_for_run(engine, settings, run_id, ttl_minutes=120)
                    with _transaction(engine) as c:
                        emit_event(c, project_id, "code_task.auto_preview", "boss",
                                   "code_task_run", run_id,
                                   {"status": _pv.get("status"), "url": _pv.get("url"),
                                    "preview_id": _pv.get("id")})
                else:
                    with _transaction(engine) as c:
                        emit_event(c, project_id, "code_task.auto_preview_skipped", "boss",
                                   "code_task_run", run_id,
                                   {"why": "service/library build — no browsable entrypoint; "
                                           "exercise it via tests or a container deploy in CI/CD"})
        except Exception as _pvexc:  # noqa: BLE001 — never let a preview affect the PR
            with _transaction(engine) as c:
                emit_event(c, project_id, "code_task.auto_preview_failed", "boss",
                           "code_task_run", run_id,
                           {"error": f"{type(_pvexc).__name__}: {_pvexc}"[:200]})
        # DEFERRED FINDINGS → their own task on the shipped branch. After the PR exists, so a
        # bookkeeping failure can never cost the delivery; guarded so follow-ups don't recurse.
        followup_run_id = None
        if (deferred_findings
                and getattr(settings, "code_review_followup_tasks", True)
                and not fctx.get("is_followup")
                and not _already_spawned_followup(engine, run_id)):
            try:
                followup_run_id = _spawn_deferred_findings_task(
                    engine, settings, jobs, project_id=project_id, task_id=task_id,
                    spec=spec, base_branch=work_branch, findings=deferred_findings,
                    parent_run_id=run_id, parent_title=title,
                    parent_root=(fctx.get("root_run_id") or run_id),
                    parent_origin=fctx.get("origin"))
                with _transaction(engine) as c:
                    emit_event(c, project_id, "code_task.deferred_findings_task", "boss",
                               "code_task_run", run_id,
                               {"followup_run_id": followup_run_id,
                                "findings": deferred_findings[:20]})
            except Exception as exc:  # noqa: BLE001 — never fail a shipped PR on follow-up bookkeeping
                with _transaction(engine) as c:
                    emit_event(c, project_id, "code_task.deferred_findings_task_failed", "boss",
                               "code_task_run", run_id,
                               {"error": f"{type(exc).__name__}: {exc}"[:300],
                                "findings": deferred_findings[:20]})
        return {"status": "completed", "done": True, "quality_gate_id": qg_id,
                "deferred_findings": deferred_findings,
                "followup_run_id": followup_run_id, **proposal}

    except RunFenced as exc:
        # THE LOSER OF A RACE MUST NOT BURY THE WINNER.
        # A fenced executor no longer owns this run — another execution does, and may still be
        # working successfully. Falling through to the generic handler marked the whole run
        # FAILED on crun_ccfc0212 even though its work was checkpointed and a sibling was
        # mid-flight. Exit quietly, touch nothing: every write we could attempt would be
        # rejected anyway, and the owner is responsible for the run's outcome.
        _finish_vms(success=False)
        try:
            with _transaction(engine) as c:
                emit_event(c, project_id, "code_task.fenced_exit", "boss", "code_task_run",
                           run_id, {"detail": str(exc)[:200],
                                    "note": "duplicate execution stood down; owner continues"})
        except Exception:  # noqa: BLE001
            pass
        return {"status": "superseded", "done": True, "reason": str(exc)}
    except CodeTaskCancelled as exc:
        return _honour_cancel(exc)
    except Exception as exc:  # noqa: BLE001 — fail the run gracefully on any agent/model error
        # A STAGE THAT DIED OF A STOP IS A STOP. Cancel now ends the build's processes at once
        # (owned_process.terminate_owner, from the API's stop watch), so the stage in flight
        # fails with "agent exited -15" — which must read as the cancel it is, not a failure.
        from . import owned_process as _owned
        if _owned.draining() and not _owned.was_cancelled(run_id):
            # A DEPLOY IS NOT A FAILURE (stability spec §4a.5). The API is draining: its
            # processes were ended on purpose. The job goes back to the queue without spending
            # an attempt and the run is marked interrupted_by_deploy, so the next API resumes
            # it instead of the operator finding a failed build they did not cause.
            from .drain import hand_back_interrupted
            handed = hand_back_interrupted(engine, jobs, run_id, project_id, fencing_token)
            if handed is not None:
                return handed
        if _owned.was_cancelled(run_id) or isinstance(exc, _owned.OwnerCancelled):
            return _honour_cancel(CodeTaskCancelled(f"stopped while running: {exc}"[:300]))
        _finish_vms(success=False)  # retain stage VMs for root-causing via the console
        # A blank reason makes every retry blind — always record at least the exception type
        # and the substrate the run was on (that's the first question when root-causing).
        reason = (str(exc).strip() or f"{type(exc).__name__} with no message")
        reason = f"{reason} [substrate={execution_substrate}]"
        from .code_task_service import classify_pipeline_exception
        from .spend_cap_failover import apply_spend_cap_failover
        fclass, rcode = classify_pipeline_exception(reason)
        # SHIP WHAT EXISTS, EVEN ON A FAILURE — same helper every other exit uses, so the
        # rule lives in exactly one place and cannot drift between paths again.
        _partial_pr = _rescue_pr(reason)

        with _transaction(engine) as c:
            _release(budget, c, project_id, reservation_id, f"governed code task error: {reason}", run_id=run_id)
            if guest_vm_ids:
                _persist_sandbox_id(c, run_id, guest_vm_ids[-1])
            if _partial_pr:
                c.execute(sql("UPDATE code_task_runs SET pull_request_url=:u, updated_at=now() "
                              "WHERE id=:id AND pull_request_url IS NULL"),
                          {"u": _partial_pr, "id": run_id})
                reason = f"{reason} — partial work shipped as {_partial_pr}"
            # Claude org monthly spend limit: hop this SAME run onto a configured
            # alternate engine once. No invented pass, no Decision auto-apply.
            # If no alternate is configured, fall through to today's escalate path.
            failover = apply_spend_cap_failover(
                c,
                jobs=jobs,
                project_id=project_id,
                run_id=run_id,
                task_id=task_id,
                settings=settings,
                current_engine=build_engine,
                error=reason,
                spec=spec,
                finalize_ctx=fctx,
                state=run.get("state") if isinstance(run, dict) else None,
            )
            if failover:
                return failover
            # Model TPM/credits/budget exhaustion: escalate to a Decision so capacity
            # failures are visible in the inbox rather than silent DLQ churn (W2.5).
            decision_id = None
            if fclass == "needs_human" and rcode in {
                "model_rate_limit", "budget_exhausted",
            }:
                try:
                    decision_id = create_blocking_decision(
                        c, project_id, task_id,
                        (
                            "Model capacity exhausted (TPM / rate limit / credits / budget). "
                            "Code task stopped — raise limits, wait for quota reset, or "
                            "requeue after capacity is available."
                        ),
                        {
                            "action": "restore_model_capacity_or_raise_budget",
                            "run_id": run_id,
                            "reason_code": rcode,
                            "failure_class": fclass,
                            "failure_reason": reason[:500],
                        },
                        {},
                        "model_capacity_exhausted",
                    )
                    c.execute(
                        sql("UPDATE code_task_runs SET decision_id=:d, updated_at=now() "
                            "WHERE id=:id AND decision_id IS NULL"),
                        {"d": decision_id, "id": run_id},
                    )
                    emit_event(
                        c, project_id, "code_task.model_capacity_escalated", "boss",
                        "code_task_run", run_id,
                        {"decision_id": decision_id, "reason_code": rcode},
                    )
                except Exception:  # noqa: BLE001 — never mask the original failure
                    pass
            result = _fail(c, run_id, project_id, reason,
                           failure_class=fclass, reason_code=rcode)
            if decision_id:
                result = {**result, "decision_id": decision_id}
            return result
    finally:
        # Its stages end with it: a build that finished, failed or now waits on a person stops
        # showing its agents as working (agent_acts; never raises).
        from .agent_acts import settle_code_task

        settle_code_task(engine, run_id)
        # Request/worker threads are pooled — a stale run_id here would mis-attribute
        # the next job's (or run_research's) llm_calls rows.
        clear_llm_context()
        if command_container is not None:
            # A leaked container outlives the run that made it and nothing else reaps it.
            command_container.stop()
        if sandbox is not None:
            sandbox.teardown()
        if review_sandbox is not None:
            review_sandbox.teardown()
