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

import contextlib
import logging
import os
import time

import hashlib
import json
import re
import threading
from pathlib import Path
import tempfile
from types import SimpleNamespace
from typing import Any, Callable, Optional, Sequence

from . import build_budget as _build_budget
from . import repair_findings
from . import parallel_build as _parallel_build
from . import pr_integrity_review
from .monitoring_guard import KEEP_MONITORING_RULE as _KEEP_MONITORING
from .review_seats import run_seats as _run_seats
from .run_terminal import on_status_written
from ..config import Settings
from ..ports import sql
from ..schemas import FileChange
from ..templates import run_env as _run_env
from . import agent_acts, baseline_verify, ci_gate, design_prompts, plane_sync, review_comments, verification_target
from . import red_first, repair_review_scope, review_input, review_panel
from . import approved_prd_design
from .knowledge import learning_context, learning_writeback  # learning while executing (BOSSAPP-768)
from .bakeoff import (
    fixture_for_run as bakeoff_fixture_for_run,
    stage_fixture as stage_bakeoff_fixture,
)
from .code_agent import make_code_agent
from .engine_contract import SESSION_PREFIX, capabilities_of
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
from .llm_telemetry import clear_llm_context, get_llm_context, record_llm_call, set_llm_context

logger = logging.getLogger(__name__)

from .crash_frames import log_caught as _log_caught  # noqa: E402 — every caught exception keeps its traceback
from .crash_frames import record_swallowed as _record_swallowed  # noqa: E402
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
from .software_factory import SoftwareFactoryService, commit_change
from .workspace_perms import write_text as _ws_write

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


#: How long an auto-preview's app may take to answer before the preview is recorded failed.
#: The operator's Run preview button keeps the full window (a Java build's 600 s); a preview
#: nobody asked for does not hold a sandbox and a worker's attention that long.
AUTO_PREVIEW_READY_SECONDS = 180.0


def auto_preview_after_pr(engine, settings: Settings, *, project_id: str, run_id: str,
                          accumulated: Optional[list[str]] = None) -> Optional[threading.Thread]:
    """Start a browsable app's preview once its PR is recorded — in a background thread, so it
    never holds the build's job. Returns the thread (None when nothing was started).

    AUTO-PREVIEW FOR APPS. Operator: previews matter for an APP — a service can be exercised by
    its tests or a container deploy in CI/CD instead. A browsable app whose PR just opened should
    already be running when the operator looks at it, rather than requiring a click and a
    ~1 minute build first.

    The distinction is made from what the run BUILT, not from the task's wording: only a build
    whose files include something a browser opens (.html/.jsx/.tsx/.vue/.svelte) is previewed.

    A PREVIEW NEVER HOLDS THE BUILD. This ran inline on the code.task job: crun_16c5e245 was
    written `completed` at 19:31:42 and its job stayed "running" until 19:41:48 while the
    preview polled an app answering 404 at `/` — the build's settle, its follow-up task and,
    behind its worker slot, other work all waited for a page nobody had asked for. The preview
    now runs on its own thread, bounded by AUTO_PREVIEW_READY_SECONDS; its outcome, failure
    included, is recorded (app_previews row, app_preview.failed, code_task.auto_preview) and
    nothing about the build waits for it.
    """
    if not getattr(settings, "auto_preview_apps", True):
        return None
    try:
        # Ask the SAME source the preview resolver uses, not a different one. This first read
        # `accumulated`, which on the local substrate holds only what _checkpoint pushed — test
        # files — so a stock trading app with web-stock/index.html was classified a service and
        # never previewed. Its state.impl_files had the html all along.
        from .preview_service import lineage_impl_files as _lineage_files
        try:
            with _transaction(engine) as _c:
                _row = _c.execute(sql(
                    "SELECT state, finalize_ctx, id FROM code_task_runs WHERE id=:i"),
                    {"i": run_id}).mappings().first()
            _built = _lineage_files(engine, dict(_row)) if _row else []
        except Exception:  # noqa: BLE001
            _log_caught(logger)
            _built = []
        if not _built:
            _built = [pth for pth in (accumulated or []) if not _is_test_path(pth)]
        if not any(pth.endswith((".html", ".jsx", ".tsx", ".vue", ".svelte")) for pth in _built):
            with _transaction(engine) as c:
                emit_event(c, project_id, "code_task.auto_preview_skipped", "boss",
                           "code_task_run", run_id,
                           {"why": "service/library build — no browsable entrypoint; "
                                   "exercise it via tests or a container deploy in CI/CD"})
            return None
    except Exception as exc:  # noqa: BLE001 — never let a preview affect the PR
        _log_caught(logger)
        _record_auto_preview_failure(engine, project_id, run_id, exc)
        return None

    def _preview() -> None:
        try:
            from .preview_service import start_preview_for_run
            _pv = start_preview_for_run(engine, settings, run_id, ttl_minutes=120,
                                        ready_timeout=AUTO_PREVIEW_READY_SECONDS)
            with _transaction(engine) as c:
                emit_event(c, project_id, "code_task.auto_preview", "boss", "code_task_run",
                           run_id, {"status": _pv.get("status"), "url": _pv.get("url"),
                                    "preview_id": _pv.get("id"),
                                    **({"error": str(_pv.get("error"))[:300]}
                                       if _pv.get("error") else {})})
        except Exception as exc:  # noqa: BLE001 — a thread's exception reaches nobody else
            _log_caught(logger)
            _record_auto_preview_failure(engine, project_id, run_id, exc)

    import contextvars

    thread = threading.Thread(target=contextvars.copy_context().run, args=(_preview,),
                              name=f"boss-auto-preview-{run_id[-12:]}", daemon=True)
    thread.start()
    return thread


def _record_auto_preview_failure(engine, project_id: str, run_id: str, exc: BaseException) -> None:
    try:
        with _transaction(engine) as c:
            emit_event(c, project_id, "code_task.auto_preview_failed", "boss",
                       "code_task_run", run_id, {"error": f"{type(exc).__name__}: {exc}"[:200]})
    except Exception:  # noqa: BLE001
        _log_caught(logger)
        logger.warning("auto-preview failure not recorded for %s", run_id, exc_info=True)


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
    # A REVIEW THAT NEVER SAW THE CHANGE (review_input.hollow_reason) is the check's fault, not
    # the code's: re-run the CHECK, never send the coder to fix what nobody reviewed.
    hollow = evidence.get("review_hollow") or {}
    seat_of = {"code_review_passed": "review_code", "consistent": "consistency_check",
               "security_clean": "security_check", "no_security_findings": "security_check"}
    for gate in failing:
        if seat_of.get(gate) in hollow:
            faults[gate] = "HARNESS"
            continue
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
            # A SECRET ONLY A PERSON CAN SET (templates.run_env: needs_secret) is neither: no
            # repair round can produce it, and a coder told to "fix" it would code around it.
            if (evidence.get("app_serves") or {}).get("needs_secret"):
                faults[gate] = "PERSON"
                continue
            inconclusive = bool((evidence.get("app_serves") or {}).get("harness"))
        else:
            # Test gates are authoritative: BOSS re-runs the suite itself, so a red suite is
            # real evidence about the code, not a check that failed to finish.
            inconclusive = False
        faults[gate] = "HARNESS" if inconclusive else "WORK"
    return faults


def mark_stage_done(engine, project_id: str, run_id: str, stage: str) -> bool:
    """Record that a stage finished, so a resumed attempt can skip it. Never raises.

    Paired with the pipeline's _checkpoint: the checkpoint preserves the WORK, this preserves
    the KNOWLEDGE of what is already done. Without both, a retry after a provider spend-limit
    re-derives stages whose output is already sitting on the branch.

    The cast is CAST(:s AS text), never `:s::text`: a SQLAlchemy text clause does not read `:s::`
    as a bind parameter, so that spelling reached Postgres as a syntax error on every build
    (test_no_text_sql_uses_a_colon_cast guards the whole engine).
    """
    try:
        with _transaction(engine) as c:
            c.execute(sql(
                "UPDATE code_task_runs SET finalize_ctx="
                "COALESCE(finalize_ctx,'{}'::jsonb) || jsonb_build_object("
                "  'completed_stages', "
                "  COALESCE(finalize_ctx->'completed_stages','[]'::jsonb) || to_jsonb(CAST(:s AS text))"
                "), updated_at=now() WHERE id=:id"),
                {"s": stage, "id": run_id})
        return True
    except Exception:  # noqa: BLE001 — bookkeeping must never fail a run, but a resume
        # that cannot see this marker redoes the stage, so it is never silent either.
        _record_swallowed(logger, f"mark_stage_done:{stage}", project_id=project_id,
                          run_id=run_id, transaction=lambda: _transaction(engine))
        return False


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
                _log_caught(logger)
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
                _log_caught(logger)
                read_by = None
            from datetime import datetime as _dt, timezone as _tz
            stamp = _dt.now(_tz.utc).isoformat()
            pending = [{**item, "applied_at": stamp, "applied_stage": stage,
                        "applied_act_id": (read_by or {}).get("act_id"),
                        "applied_role": (read_by or {}).get("role_key")} for item in pending]
            row_p = c.execute(sql("SELECT project_id FROM code_task_runs WHERE id=:id"),
                              {"id": run_id}).scalar()
            if row_p:
                # A SAVEPOINT: a failed audit write must not poison the surrounding
                # transaction (Postgres aborts the whole transaction on any error until
                # rollback), which would then fail the guidance update right below it too.
                try:
                    with c.begin_nested():
                        emit_event(c, row_p, "code_task.guidance_applied", "boss",
                                   "code_task_run", run_id,
                                   {"count": len(pending), "stage": stage,
                                    "act_id": (read_by or {}).get("act_id")})
                except Exception:  # noqa: BLE001 — the audit trail never blocks the guidance
                    _log_caught(logger)
        c.execute(sql(
            "UPDATE code_task_runs SET finalize_ctx = "
            "COALESCE(finalize_ctx,'{}'::jsonb) || jsonb_build_object("
            "  'pending_guidance', '[]'::jsonb,"
            "  'applied_guidance', COALESCE(finalize_ctx->'applied_guidance','[]'::jsonb) "
            "                      || CAST(:g AS JSONB)), updated_at=now() WHERE id=:id"),
            {"g": json.dumps(pending), "id": run_id})
    return pending


def carry_pending_guidance(connection, *, parent_run_id: str, child_run_id: str) -> int:
    """Move a parent's UNREAD guidance onto the child taking over its work.

    Called in the same transaction that creates a repair child / retry / follow-up. Without
    this, guidance queued on a run that then hands off its work (to a child created with
    `parent_run_id` pointing back at it) sat in the parent's `finalize_ctx.pending_guidance`
    forever — no stage of the parent runs again to read it, and no stage of the child ever
    looked at its PARENT's queue. Three steers vanished this way (KG P6a, CX, 2026-10-30/31).

    Each carried item gains ``carried_from`` so the child's own `take_pending_guidance` read
    (unchanged, still pops once) can say where it came from. The parent's queue is cleared in
    the same statement so it can never be carried twice.

    The parent's APPLIED guidance (`finalize_ctx.applied_guidance`) is copied onto the
    child's `applied_guidance` the same way — unlike `pending_guidance`, it is never cleared
    on the parent, since it is a record of what already happened there, not a queue. Without
    this, `_operator_review_block` (which reads `applied_guidance` to show reviewers what a
    steer changed) saw nothing on a repair child/retry/follow-up even though its parent run
    had steers applied to it.
    """
    row = connection.execute(
        sql("SELECT finalize_ctx FROM code_task_runs WHERE id=:id"),
        {"id": parent_run_id}).mappings().first()
    if not row:
        return 0
    fctx = row["finalize_ctx"] or {}
    if isinstance(fctx, str):
        try:
            fctx = json.loads(fctx)
        except Exception:  # noqa: BLE001
            _log_caught(logger)
            return 0
    pending = list(fctx.get("pending_guidance") or [])
    applied = list(fctx.get("applied_guidance") or [])
    if not pending and not applied:
        return 0
    carried = [{**item, "carried_from": parent_run_id} for item in pending]
    carried_applied = [{**item, "carried_from": parent_run_id} for item in applied]
    if carried or carried_applied:
        connection.execute(sql(
            "UPDATE code_task_runs SET finalize_ctx = "
            "COALESCE(finalize_ctx,'{}'::jsonb) || jsonb_build_object("
            "  'pending_guidance', "
            "  COALESCE(finalize_ctx->'pending_guidance','[]'::jsonb) || CAST(:g AS JSONB),"
            "  'applied_guidance', "
            "  COALESCE(finalize_ctx->'applied_guidance','[]'::jsonb) || CAST(:a AS JSONB)"
            "), updated_at=now() WHERE id=:id"),
            {"g": json.dumps(carried), "a": json.dumps(carried_applied), "id": child_run_id})
    if pending:
        connection.execute(sql(
            "UPDATE code_task_runs SET finalize_ctx = "
            "COALESCE(finalize_ctx,'{}'::jsonb) || jsonb_build_object("
            "  'pending_guidance', '[]'::jsonb"
            "), updated_at=now() WHERE id=:id"),
            {"id": parent_run_id})
    return len(carried)


def late_steer_response(connection, *, run_id: str, project_id: str, guidance: str,
                         actor: str) -> dict:
    """What `steer_code_task` answers once a run is past the point anything reads its queue.

    A run marks `finalize_ctx.past_last_stage_boundary: true` at its last stage boundary — the
    point `take_pending_guidance` is never called again for THIS run. Queueing a steer there is
    a silent no-op: the operator sees "ok: true" and the guidance is never read by anything
    (crun_e470d6dc, 2026-10-30). This decides, in words, what happens instead:

    - If a follow-up/retry already exists for the run (a row whose `finalize_ctx.parent_run_id`
      names it), the steer lands on THAT run's `pending_guidance` and the answer says so.
    - If none exists yet, nothing is written — queuing it would be exactly the silent no-op
      this exists to avoid — and the answer says in words that the run finished its last stage
      and names retrying it with the guidance as the fix note.
    """
    from datetime import datetime, timezone

    entry = {"text": str(guidance)[:4000], "actor": actor,
              "at": datetime.now(timezone.utc).isoformat()}
    successor = connection.execute(
        sql("SELECT id FROM code_task_runs WHERE project_id=:p "
            "AND finalize_ctx->>'parent_run_id' = :r ORDER BY created_at DESC LIMIT 1"),
        {"p": project_id, "r": run_id}).scalar()
    if successor:
        connection.execute(sql(
            "UPDATE code_task_runs SET finalize_ctx = COALESCE(finalize_ctx,'{}'::jsonb) "
            "|| jsonb_build_object('pending_guidance', "
            "COALESCE(finalize_ctx->'pending_guidance','[]'::jsonb) || CAST(:e AS JSONB)), "
            "updated_at=now() WHERE id=:id"),
            {"e": json.dumps([entry]), "id": successor})
        return {"ok": True, "run_id": run_id, "landed_on": successor,
                "message": f"run {run_id} finished its last stage; this steer was applied "
                           f"to its follow-up {successor} instead"}
    return {"ok": False, "run_id": run_id,
            "error": f"run {run_id} has already finished its last stage, so no further stage "
                     "will ever read this guidance; retry the run and pass this as the fix "
                     "note instead of steering it"}


def should_create_followup_for_pr(gh, *, pull_request_number: int) -> tuple[bool, str]:
    """Gate an automatic "checks failing" / "deferred findings" follow-up on the PR's state.

    An operator who already merged or closed the PR by hand does not want a follow-up
    re-deriving "fix the failing checks" against work that is already in — BOSS kept spawning
    one anyway, three times in a row, on a PR the operator had merged into rel (CX PR #24,
    crun_00301249 -> crun_b82f4275 -> crun_2640c811 -> crun_03e34f7b). Called before the spawn,
    never after.

    GitHub's own `state`/`merged_at` is not enough: an operator who squash-merges a PR's
    branch into another branch LOCALLY (not through GitHub's merge button) leaves the PR
    itself sitting OPEN on GitHub forever — PR #24 above stayed open while already merged
    into `rel`. So beyond the state check, this also compares the PR's changed files' content
    at its head against the same paths on its base branch's head: if every file already reads
    the same on the base, the PR's change is already in, whatever GitHub's merge button says.
    """
    snapshot = gh.pull_request_snapshot(pull_request_number) or {}
    state = str(snapshot.get("state") or "").strip().lower()
    merged_at = snapshot.get("merged_at")
    if merged_at or state == "merged":
        return False, f"pull request #{pull_request_number} is already merged"
    if state == "closed":
        return False, f"pull request #{pull_request_number} is already closed"
    base_ref = snapshot.get("base_ref")
    head_ref = snapshot.get("head_ref")
    if base_ref and head_ref and hasattr(gh, "pull_request_files") and hasattr(gh, "read_text_file"):
        try:
            files = gh.pull_request_files(pull_request_number) or []
        except Exception:  # noqa: BLE001 — a check that cannot run must not block the follow-up
            _log_caught(logger)
            files = []
        paths = [f.get("path") or f.get("filename") for f in files
                 if (f.get("path") or f.get("filename")) and f.get("status") != "removed"]
        if paths:
            try:
                already_in_base = all(
                    gh.read_text_file(base_ref, path) == gh.read_text_file(head_ref, path)
                    for path in paths)
            except Exception:  # noqa: BLE001
                _log_caught(logger)
                already_in_base = False
            if already_in_base:
                return False, (f"pull request #{pull_request_number}'s change is already "
                                f"present on its base branch {base_ref!r}")
    return True, ""


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


def review_panel_seats(lane: str, risk_tier: int, profile: dict, *, ci_files: bool) -> set:
    """Which review-panel seats sit for a build. Tests do not enter into it.

    EVERY BUILD IS REVIEWED, WHATEVER ITS TESTS SAY (operator, 2026-10-30). Tests decide a
    merge; review is the evidence that someone read the code, and the PR carries its
    conversation. Skipping the panel when tests still failed (most often failures the change
    did not cause) opened PRs nobody had read: KG P6a PR #25 shipped a token no endpoint ever
    checked, CX PR #24 claimed observations it never made. A profile (`panel_checks`) or the
    fix lane may narrow the panel, never below one code reviewer; above tier 1 it is the full
    panel; a CI change always gets security too."""
    from . import fix_lane_runner

    seats = set(fix_lane_runner.review_seats_for(
        lane,
        _ALL_PANEL_SEATS if risk_tier >= 2
        else ((profile or {}).get("panel_checks") or _ALL_PANEL_SEATS)))
    if ci_files:
        seats |= {"review_code", "security_check"}
    seats |= {"review_code"}
    return seats


def panel_seats(enabled_seats, review_checks, *, security, regression, consistency,
                merged=None):
    """(label, fn) for exactly the review-panel seats that are enabled, in panel order.

    The one place the panel's seat list is built, from the same `enabled_seats` that decided
    which seats were skipped, so a seat reported as skipped can never still be run.

    ``merged`` (BOSSAPP-1000) is the one-session panel: when given, it stands in for EVERY
    enabled LLM seat (review/security/consistency) — one session reads the change once and
    casts each enabled seat's verdict, with a per-seat fallback inside it for a section the
    session did not deliver. The regression seat is not a model call and keeps its own slot."""
    seats = []
    if merged is not None:
        if {"review_code", "security_check", "consistency_check"} & set(enabled_seats):
            seats.append(("panel", merged))
        if "regression" in enabled_seats:
            seats.append(("regression", regression))
        return seats
    if "review_code" in enabled_seats:
        seats += [(f"review[{i}]", c) for i, c in enumerate(review_checks)]
    if "security_check" in enabled_seats:
        seats.append(("security", security))
    if "regression" in enabled_seats:
        seats.append(("regression", regression))
    if "consistency_check" in enabled_seats:
        seats.append(("consistency", consistency))
    return seats


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
        _log_caught(logger)
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
                _log_caught(logger)
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


class BuildBudgetStopped(CodeTaskCancelled):
    """A cap was reached while this build ran (build_budget.cap_reached). Raised at the next
    stage boundary that would spend — never mid-write — so the build unwinds like a stop: its
    work kept, its money so far settled. It ends blocked as `budget_denied` (Budget refused), in
    the cap's words; Retry continues from the kept work once the cap has room."""


def stop_build_for_budget(engine, budget, *, run_id: str, project_id: str,
                          reservation_id: Optional[str], message: str) -> dict:
    """THE END OF A BUILD THE CAPS STOPPED (BuildBudgetStopped, after its work was kept): its
    money so far settled on its reservation, and the run blocked as `budget_denied` — the state
    the console already reads as "Budget refused" (factory_view) — in the cap's words."""
    with _transaction(engine) as c:
        _release(budget, c, project_id, reservation_id, "stopped by the budget cap",
                 run_id=run_id)
        emit_event(c, project_id, "code_task.budget_stopped", "boss", "code_task_run",
                   run_id, {"message": message[:400]})
        return _fail(c, run_id, project_id, f"The budget stopped this code task: {message}",
                     status="blocked", failure_class="needs_human",
                     reason_code="budget_denied")


#: Phases that deliver finished work and spend no agent money: the budget does not stop a
#: build at them (the money is spent; refusing the pull request only throws the work away).
_DELIVERY_PHASES = frozenset({"opening_pr", "waiting_for_ci"})


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
# A FAILING TEST IS A FACT, NOT AN OPINION. `written_tests_pass` and `nothing_broke` were outside
# this floor, so an advisory build whose own tests never passed shipped as "completed": the Notes
# Lite check (crun_3a39cd19) ran four rounds, never turned its tests green, and ended
# completed_no_pr with both gates marked advisory. A build whose tests fail is not done.
#: Named apart from _TEST_GATES (which decides when a repair re-authors tests): reusing the name
#: silently replaced that set at import and stopped weak-suite gates from re-running write_tests.
_FAIL_CLOSED_TEST_GATES = ("written_tests_pass", "nothing_broke", "coder_did_not_edit_tests")
_ADVISORY_HARD_FLOOR = ("code_written", "app_serves", "template_tests", "no_committed_secrets",
                        *_FAIL_CLOSED_TEST_GATES)

# Gates no autonomy setting may ship past. `app_serves` is not a reviewer's opinion: BOSS
# started the build's own run command and nothing answered HTTP. The Lead Triage Inbox build
# (crun_3e370c11) completed and opened PR #218 while its app could not start at all — an
# ImportError on its first line — and the operator found out from a preview timeout. A build
# whose app does not run is not done, so exhausting the repairs BLOCKS with the app's own
# error (and the partial PR rescue keeps the work), rather than shipping with dissent.
_NEVER_SHIP_PAST = frozenset({"app_serves", "template_tests", "no_committed_secrets", *_FAIL_CLOSED_TEST_GATES})
_TEMPLATE_GATE_SET = frozenset({"template_tests", "no_committed_secrets"})

# THE TEMPLATE HOLD (F2 §2.3, S2.1). A build that applies a template carries two mechanical
# gates (`_template_gates`): its tests ran and passed, and its diff holds no secret. Neither is a
# reviewer's opinion, so no autonomy setting ships past them (both are in _NEVER_SHIP_PAST and the
# advisory floor above), and a human waiver cannot either (factory_view.UNWAIVABLE_GATES).
#
# Blocking the PR at the end is not enough: a build pushes its work to the branch as it goes
# (stage commits, checkpoints, the repair hand-off, the partial-PR rescue), and an apply builds
# onto the app's branch, whose PR is already open — so a committed secret was on the PR minutes
# before any gate looked at it. So for such a build EVERY push waits for these gates: nothing it
# wrote reaches the remote, the branch or a PR until both have passed. A held build that fails
# keeps its result on the run (gates, evidence) but no diff with a secret in it.
TEMPLATE_GATES = ("template_tests", "no_committed_secrets")


# A FAILED ATTEMPT'S WORKSPACE IS KEPT, LOCALLY. A template build whose gates fail pushes nothing
# (the hold above), so its sandbox — every file the test author and the implementer wrote — was
# thrown away at teardown, and each retry re-ran write_tests and implement from nothing (HelpDesk
# build 7: ~2 hours and ~$30 a retry, for 7 failing e2e tests). The changed files are saved on
# the run (finalize_ctx.saved_workspace) — in BOSS's own database, never pushed — and a retry in
# the same lineage starts its sandbox from base plus those files and goes straight to repair.
# Dependencies, git internals and build output are not the work and are not kept.
_WORKSPACE_SKIP_PARTS = frozenset({
    "node_modules", ".git", "dist", "build", ".next", "coverage", "test-results",
    "playwright-report", "__pycache__", ".vite", ".turbo", ".cache", ".pytest_cache",
})


#: A top-level folder an agent made as a COPY of the app to work around a permission it lacked
#: (helpdesk-ai-locked/, helpdesk-ai__orig_readonly/, helpdesk-ai-locked-orig/ ...): not the
#: work, never seeded, never counted, never in a pull request.
_WORKAROUND_COPY = re.compile(r"(?:[-_]+(?:locked|orig|original|readonly|read[-_]only|snapshot|"
                              r"backup|bak|copy|prev|old))+$", re.I)


def workspace_keepable(path: str) -> bool:
    """Whether a changed path is part of the work (kept), not dependencies, build output or a
    workaround copy of the app."""
    parts = [p for p in str(path or "").replace("\\", "/").split("/") if p]
    return (bool(parts) and not any(p in _WORKSPACE_SKIP_PARTS for p in parts)
            and not (len(parts) > 1 and _WORKAROUND_COPY.search(parts[0])))


def saved_workspace_files(run: Optional[dict]) -> list[dict]:
    """The files a failed attempt left to continue from: its saved workspace, else the diff a
    gate-blocked run preserved for an override (`pending_diff`, the same tree). [] when none."""
    fctx = (run or {}).get("finalize_ctx") or {}
    saved = fctx.get("saved_workspace") if isinstance(fctx.get("saved_workspace"), dict) else {}
    for files in (saved.get("files"), fctx.get("pending_diff")):
        if isinstance(files, list) and files:
            return [f for f in files if isinstance(f, dict) and workspace_keepable(f.get("path"))
                    and isinstance(f.get("content"), str)]
    return []


def layered_workspace_files(load: Callable[[str], Optional[dict]], run: Optional[dict],
                            depth: int = 12) -> list[dict]:
    """The WHOLE workspace an attempt ended with. An attempt seeded from an earlier one saves
    only what it changed (the seed is its starting point), so crun_fe2dbbc8, which fixed every
    gate but one, saved 11 files and its retry fell back to an older, worse attempt. The whole
    tree is its seed's whole tree with its own files laid on top, newest wins."""
    if run is None or depth <= 0:
        return []
    own = saved_workspace_files(run)
    src = str(((run.get("finalize_ctx") or {}).get("seed_workspace_from")) or "").strip()
    if not src or src == run.get("id"):
        return own
    base = {f["path"]: f for f in layered_workspace_files(load, load(src), depth - 1)}
    for f in own:
        base[f["path"]] = f
    return list(base.values())


def previous_attempt_paths(load: Callable[[str], Optional[dict]], fctx: Optional[dict]) -> list[str]:
    """The files the attempt this one repairs produced: its kept workspace (layered) and the
    implementation and test files its gate recorded. [] for a first attempt."""
    parent_id = str((fctx or {}).get("parent_run_id") or "").strip()
    parent = load(parent_id) if parent_id else None
    if parent is None:
        return []
    st = parent.get("state") or {}
    paths = {str(f.get("path")) for f in layered_workspace_files(load, parent)}
    for key in ("impl_files", "test_files"):
        if isinstance(st.get(key), list):
            paths |= {str(p) for p in st[key] if isinstance(p, str)}
    return sorted(p for p in paths if workspace_keepable(p))


def repair_lost_the_work(expected: list[str], root: Path, *,
                         scope: Optional[str] = None) -> Optional[str]:
    """A HARNESS VERDICT, before review and the gates: this repair's tree holds NONE of the files
    the attempt it repairs produced (`expected`; within the app's folder when the brief names
    one). None when it holds any of them, or when nothing is expected.

    crun_3933d79d "repaired" crun_ced396a4 on the bare base branch: the reviewers spent a round
    finding "no helpdesk-ai application code whatsoever", four gates failed, and an override would
    have opened an empty PR. Reviewing an empty change judges nothing; this says so at once."""
    from .verification_target import in_app_scope

    wanted = [p for p in expected if in_app_scope(p, scope)]
    if not wanted:
        return None
    base = Path(root)
    if any((base / p).exists() for p in wanted):
        return None
    shown = ", ".join(wanted[:3]) + (f" and {len(wanted) - 3} more" if len(wanted) > 3 else "")
    return ("Stopped before review: this attempt repairs earlier work, but none of the "
            f"{len(wanted)} file(s) the previous attempt produced ({shown}) is in its working "
            "tree, so there is nothing to review or gate. This is BOSS's fault, not the code's: "
            "Retry starts from the work the build kept, or builds it in full when none was kept.")


def seed_workspace(root: Path, files: list[dict]) -> int:
    """Write a saved workspace into a freshly provisioned sandbox. Paths are confined to `root`
    (they were written by an agent); returns how many files were written."""
    import os as _os
    import re as _re

    base = Path(root).resolve()
    written = 0
    for f in files:
        rel = str(f["path"])
        # A COPY THE AGENT MADE TO WORK AROUND A READ-ONLY SEED is not the work: seeded files
        # used to land BOSS-owned 0644, the agent could not edit them, so it copied the app to
        # `<app>-locked/`, `<app>__orig_readonly/`, `<app>__prev_readonly_snapshot/`, and every
        # retry carried the copies forward (HelpDesk: 147 → 219 → 291 files).
        top = rel.split("/", 1)[0]
        if _re.search(r"(?:[-_]+(?:locked|orig|original|readonly|read[-_]only|snapshot|backup|"
                      r"bak|copy|prev|old))+$", top, _re.I):
            continue
        target = (base / rel).resolve()
        if base != target and base not in target.parents:
            continue
        # Directories and files the run's uid can write. The sandbox uid runs with NO
        # supplementary groups (boss-sandbox-exec), so group-writable is still read-only to it:
        # it kept copying the app (helpdesk-ai-locked-orig/) to edit it. World-writable is safe
        # here: the run directory is <uid>:boss 2770, no other uid can enter it.
        missing = [d for d in [target.parent, *target.parent.parents]
                   if base in d.parents or d == base]
        target.parent.mkdir(parents=True, exist_ok=True)
        for d in missing:
            if d != base:
                try:
                    _os.chmod(d, 0o2777)
                except OSError:
                    pass
        _ws_write(target, f["content"])
        try:
            _os.chmod(target, 0o666)
        except OSError:
            pass
        written += 1
    return written


def template_push_hold(applies_template: bool, gates: Optional[dict]) -> Optional[str]:
    """Why a push of this build's work must not happen now, or None when it may.

    `applies_template` is True also when BOSS could not tell (fail closed). `gates` is None until
    the gates ran."""
    if not applies_template:
        return None
    if gates is None:
        return "this build applies a template: nothing is pushed before its gates pass"
    failing = [g for g in TEMPLATE_GATES if gates.get(g) is not True]
    if failing:
        return f"this build applies a template and its gates failed ({', '.join(failing)}): nothing is pushed"
    return None


def _applies_template(engine, run: dict) -> bool:
    """Does this build apply a template? An error reading it counts as yes (fail closed)."""
    from ..templates import gate as template_gate

    try:
        with _transaction(engine) as c:
            return template_gate.applied_template(c, run) is not None
    except Exception:  # noqa: BLE001 — unknown is held, never pushed
        _log_caught(logger)
        return True


def _app_id_hint(spec: dict, fctx: dict, layout) -> Optional[str]:
    """Where the app lives, for the layout prompt: the named app, else the app name the brief
    gives as a directory (`helpdesk-ai/`), else None (the prompt says "the app directory")."""
    named = verification_target.official_app_id(spec, fctx)
    if named:
        return named
    text = str((spec or {}).get("instructions") or "")
    m = re.search(r"\b([a-z0-9][a-z0-9_-]{1,60})/\s*\((?:" + re.escape(layout.backend_dir)
                  + r"|" + re.escape(layout.frontend_dir) + r")/", text)
    return m.group(1) if m else None


class StackUnsupportedError(RuntimeError):
    """The build asks for a stack BOSS cannot build here (services.stacks). Raised before the
    sandbox is provisioned, so the refusal costs nothing."""


def _stack_context(engine, run: dict, title: str, instructions: str) -> dict:
    """What the build is meant to be, before any file exists (services.stacks): the applied
    template's stack and test command, the full-stack layout the brief or the approved
    architecture asks for (None for a single-service app: today's behaviour), and the verdict of
    the last guard before money is spent."""
    from . import stacks
    from ..templates import gate as template_gate

    tpl: dict = {}
    try:
        with _transaction(engine) as c:
            tpl = template_gate.applied_template(c, run) or {}
    except Exception:  # noqa: BLE001 — the template gates fail closed on their own
        _log_caught(logger)
        tpl = {}
    t_stack = str(tpl.get("stack") or "") or None
    t_fw = str(tpl.get("framework") or "") or None
    manifest = tpl.get("manifest") if isinstance(tpl.get("manifest"), dict) else {}
    tests = (manifest.get("tests") or {}).get(t_stack or "") if isinstance(manifest, dict) else None
    t_cmd = str((tests or {}).get("command") or "") or None if isinstance(tests, dict) else None
    layout = stacks.build_layout(instructions, title, template_stack=t_stack,
                                 template_framework=t_fw)
    verdict = stacks.check_build(instructions, template_stack=t_stack)
    return {"template_stack": t_stack, "template_framework": t_fw, "template_command": t_cmd,
            "layout": layout, "verdict": verdict}


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


def blocked_gate_messages(qg_id: str, failing: list, app_serves_record: Optional[dict],
                          template_tests_record: Optional[dict] = None,
                          install_failure: Optional[dict] = None,
                          blockers: Optional[list] = None) -> tuple[str, str]:
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
    from ..templates.gate import _derived_from_app_serves, install_reason

    install = install_reason(install_failure)
    if "app_serves" in (failing or []):
        from .preview_service import app_serves_headline

        sentence = _app_serves_sentence(app_serves_record)
        # Led by the gate that failed and its first error line (crun_247435e1); a failed install
        # leads instead (below): nothing could start without it.
        lead = "" if install else f"Blocked: {app_serves_headline(app_serves_record)}. "
        error = lead + error + f" The app does not serve HTTP. {sentence[:700]}"
        reason = "the app does not serve HTTP, so the build is not done"
    # The template gate's own words (e.g. "cannot run on the micro-VM build substrate: …"), so a
    # blocked apply says WHY and what to do, not only the gate's name — unless it is only the
    # consequence of the app not starting.
    why = str((template_tests_record or {}).get("error") or "")
    if ("template_tests" in (failing or []) and why
            and not ("app_serves" in (failing or []) and _derived_from_app_serves(template_tests_record))):
        error += f" Template tests: {why[:700]}"
        if "app_serves" not in (failing or []):
            reason = "the applied template's tests did not pass, so the build is not done"
    # BOSS's own install failed, so no test ran: that is the reason, in npm's words.
    if install:
        error = f"Blocked: {install}. " + error
        reason = f"{install}, so the build is not done"
    needs = list((app_serves_record or {}).get("needs_secret") or [])
    if "app_serves" in (failing or []) and needs and not install:
        # A PERSON'S ACT, not a waiver and not a repair: what to set, and where.
        from ..templates.run_env import needs_secret_action
        from .preview_service import app_serves_headline

        error = (f"Blocked: {app_serves_headline(app_serves_record)}. "
                 f"{needs_secret_action(needs)}")
        reason = f"the app needs {', '.join(needs)}, which only a person can set"
    elif blockers and not install and "app_serves" not in (failing or []):
        # A BLOCKER STILL OPEN AFTER ITS FIX ROUND (unresolved_blockers): not done, named.
        names = ", ".join(_BLOCKER_WORDS.get(g, g) for g in blockers)
        error = (f"Not done: a BLOCKER finding is still open after its fix round ({names}). "
                 + error)
        reason = "a blocker finding is still open after its fix round, so the build is not done"
    return error, reason


_BLOCKER_WORDS = {"code_review_passed": "code review", "security_clean": "security review",
                  "consistent": "consistency check"}


#: THE JUDGMENT SEATS. A blocking finding from any of them gets ONE warm fix round on the
#: implementer's (or, for a weak suite, the test author's) session and a scoped re-review of the
#: failed seats, before the gates are judged.
_JUDGMENT_GATES = frozenset({"code_review_passed", "security_clean", "consistent",
                             "tests_strong_enough"})
#: What the in-run warm round can work on. Anything else failing (a template gate, an IaC gate,
#: tests written after the code) is not a finding a warm round fixes: the legacy paths judge it.
_CONVERGEABLE = _JUDGMENT_GATES | frozenset({"written_tests_pass", "nothing_broke",
                                             "web_package_honest", "app_serves"})


#: What a template gate's `because` names as the gate it is a consequence of.
_TEMPLATE_CAUSES = {"app_serves": frozenset({"app_serves"}),
                    "tests": frozenset({"written_tests_pass", "nothing_broke"})}


def unconvergeable_failing(failing, *, template_evidence: Optional[dict] = None,
                           coder_owned_secrets: bool = False) -> set:
    """The failing gates a warm round cannot work on.

    A TEMPLATE GATE THAT FAILED AS A CONSEQUENCE IS NOT ITS OWN FAILURE. When the app did not
    start, or the build's own tests failed, `template_tests` fails with `because` naming that
    gate (templates/gate.py): the template's tests were never run. Counting it made every such
    template build skip the warm round and go to a cold ~20-minute child run; the warm round on
    the CAUSE (app_serves, the tests) is what fixes it, and the template gates are re-judged
    after it. A secret the coder wrote (`coder_owned_secrets`: no finding in QA's files, which
    fixture_secret_repair hands back to QA) is the coder's to remove in the same warm session."""
    failing = set(failing or ())
    tt = dict((template_evidence or {}).get("template_tests") or {})
    cause = _TEMPLATE_CAUSES.get(str(tt.get("because") or ""))
    if "template_tests" in failing and cause and failing & cause:
        failing.discard("template_tests")
    if "no_committed_secrets" in failing and coder_owned_secrets:
        failing.discard("no_committed_secrets")
    return failing - _CONVERGEABLE


def warm_fix_rounds(failing, *, configured: int, repair_attempts: int, use_microvm: bool,
                    template_evidence: Optional[dict] = None,
                    coder_owned_secrets: bool = False) -> int:
    """How many warm fix rounds the in-run loop gives these failing gates.

    WHY THIS IS ONE FUNCTION. The loop's entry test was "every failing gate is convergeable",
    and `tests_strong_enough` was not in the set. So a build whose mutation probe survived
    skipped the round for EVERY seat: the Onboarding Path Builder build (crun_2d097414, PR
    paramg/boss-sandbox#227) had a reviewer BLOCKER, a HIGH security finding and consistency
    drift, got no fix turn at all ("Round 1 of 1 — first review") and shipped all four as
    advisory. Now a blocking finding from any judgment seat always gets its round.

    `configured` is the Review rounds knob (effective_review_rounds, >= 1). Failing tests after
    a repair get no further round (they are not satisfied by a third try), unless a judgment seat
    is also failing: its one round is owed regardless. The micro-VM substrate has no warm
    session in this process; it gets the cold repair instead (resolve_gate_failure_action)."""
    failing = set(failing or ())
    if use_microvm or not failing or unconvergeable_failing(
            failing, template_evidence=template_evidence,
            coder_owned_secrets=coder_owned_secrets):
        return 0
    rounds = max(0, int(configured))
    if failing & set(_FAIL_CLOSED_TEST_GATES):
        rounds = min(rounds, 0 if repair_attempts >= 1 else 1)
    if failing & _JUDGMENT_GATES:
        rounds = max(rounds, 1)
    return rounds


def secret_repair_prompt(findings) -> str:
    """The implementer's secret repair turn: WHERE the scan matched (file, line, rule) and what
    to do instead. Never the matched text: the scan's report must not become the leak."""
    where = "\n".join(f"- {f.get('path')}:{f.get('line')} ({f.get('rule')})"
                      for f in list(findings or [])[:30] if isinstance(f, dict))
    return ("--- SECRET SCAN: ONE REPAIR TURN ---\n"
            "BOSS's secret scan found a committed secret literal at these places in your change. "
            "The build cannot ship while any remains:\n"
            f"{where}\n\n"
            "Fix each one: " + SECRETS_FROM_ENV_RULE + " Replace the literal with a read of the "
            "environment variable (declare it where the app declares its settings), and in an "
            "example or a document write a placeholder such as ${NAME} or <your value>. Change "
            "nothing else, do NOT touch any test file, run the tests to confirm they still pass, "
            "then end with a one-line summary of what you changed.\n")


def repair_committed_secrets(findings, *, run_fix: Callable[[str], dict],
                             rejudge: Callable[[], dict]) -> dict:
    """ONE REPAIR TURN FOR A COMMITTED SECRET, before the build is blocked for it.

    crun_ced396a4 ran 26 minutes, its tests passed and its app served, and then the secret scan
    blocked it for a literal at auth/demo.mjs:58 that the implementer was never shown: its one
    warm round had carried the earlier findings (lines 52-53) among a dozen review comments, and
    the line its fix left was judged after the last round. Now the implementer gets the scan's
    findings in a turn of their own (`secret_repair_prompt`), the gates re-run exactly as strict
    (`rejudge`: tests, then the scan), and the build blocks only if a secret is still there.

    `run_fix(prompt)` runs the turn and returns its result; `rejudge()` returns the secret
    scan's evidence after it ({"passed": bool, "findings": [...]})."""
    shown = [{k: f.get(k) for k in ("path", "line", "rule")}
             for f in list(findings or [])[:30] if isinstance(f, dict)]
    record: dict = {"attempted": True, "findings": shown}
    result = run_fix(secret_repair_prompt(shown)) or {}
    record["cost_usd_micros"] = int(result.get("cost_usd_micros") or 0)
    after = rejudge() or {}
    record["findings_after"] = [{k: f.get(k) for k in ("path", "line", "rule")}
                                for f in list(after.get("findings") or [])[:30]
                                if isinstance(f, dict)]
    record["cleared"] = bool(after.get("passed"))
    return record


def unresolved_blockers(failing, *, review_summary: str = "", security_verdict: str = "",
                        consistency_summary: str = "", hollow: Optional[dict] = None) -> list[str]:
    """The judgment gates still failing on a BLOCKER-level finding (after the fix round).

    Advisory means a reviewer's OPINION cannot withhold the PR; a blocker is not an opinion
    about polish. BLOCKER-level is: a review finding graded BLOCKER ("data loss, security hole,
    or the feature does not work"); a security verdict of ISSUES (the seat says ISSUES only for
    an exploitable CRITICAL/HIGH finding, the review's "security hole"); a consistency finding
    graded BLOCKER. A weak suite (`tests_strong_enough`) is never blocker-level. A hollow seat
    (it never saw the change) judged nothing, so it is the inconclusive path's, not this one's."""
    failing = set(failing or ())
    hollow = hollow or {}

    def _graded_blocker(summary: str) -> bool:
        return any(f.get("severity") == "blocker" for f in _parse_findings(summary or ""))

    out = []
    if ("code_review_passed" in failing and not hollow.get("review_code")
            and _graded_blocker(review_summary)):
        out.append("code_review_passed")
    if ("security_clean" in failing and not hollow.get("security_check")
            and str(security_verdict or "") == "issues"):
        out.append("security_clean")
    if ("consistent" in failing and not hollow.get("consistency_check")
            and _graded_blocker(consistency_summary)):
        out.append("consistent")
    return out


def review_repair_rounds(settings: Any) -> int:
    """BOSS_CODE_REVIEW_REPAIR_MAX_ROUNDS. A missing/blank value must never SILENTLY disable the
    moat (2), but an explicit 0 is the documented "block on the first failure" and is honoured:
    `int(x or 2)` turned it into 2."""
    value = getattr(settings, "code_review_repair_max_rounds", 2)
    return max(0, int(2 if value in (None, "") else value))


def resolve_gate_failure_action(
    *,
    failing: list[str],
    advisory_gate: bool,
    rounds_used: int,
    max_repair_rounds: int,
    open_pr: bool = True,
    never_block: bool = True,
    faults: Optional[dict[str, str]] = None,
    template_repair_max: Optional[int] = None,
    blockers: Optional[list[str]] = None,
    fix_round_ran: bool = True,
    open_pr_when_failing: bool = False,
) -> str:
    """Decide what to do after a non-passing quality gate (`_gate_failure_action`), then apply
    the operator's rule (checks_failing.py): with ``open_pr_when_failing`` (the project setting
    `open_pr_when_checks_fail`, on by default), a build that would stop for a waiver once its
    repair rounds are spent opens its pull request instead — ``"ship_failing"``: a draft,
    labelled, every failing gate and finding on it, a follow-up build for them, and a merge only
    a person decides. Its gates are judged exactly as before; only the stop moves. A build that
    needs a person's act (a secret only they can set), one held by an applied template's gate,
    and one that opens no PR (``open_pr`` false) still stop."""
    action = _gate_failure_action(
        failing=failing, advisory_gate=advisory_gate, rounds_used=rounds_used,
        max_repair_rounds=max_repair_rounds, open_pr=open_pr, never_block=never_block,
        faults=faults, template_repair_max=template_repair_max, blockers=blockers,
        fix_round_ran=fix_round_ran)
    if action == "block" and open_pr_when_failing:
        from .checks_failing import ACTION, may_open

        if may_open(failing, faults=faults, open_pr=open_pr):
            return ACTION
    return action


def _gate_failure_action(
    *,
    failing: list[str],
    advisory_gate: bool,
    rounds_used: int,
    max_repair_rounds: int,
    open_pr: bool = True,
    never_block: bool = True,
    faults: Optional[dict[str, str]] = None,
    template_repair_max: Optional[int] = None,
    blockers: Optional[list[str]] = None,
    fix_round_ran: bool = True,
) -> str:
    """Decide what to do after a non-passing quality gate.

    ``blockers`` (unresolved_blockers) are judgment gates still failing on a BLOCKER-level
    finding. After the warm fix round (``fix_round_ran``) they BLOCK the build as not done, on
    every profile: advisory never ships a blocker. A build that could not have a warm round (the
    micro-VM substrate) gets the cold repair first, within its budget.

    ``template_repair_max`` is the build's own repair budget (``repair_max``: the run's
    ``repair_retries``, else BOSS_CODE_REPAIR_MAX_RETRIES). A failed TEMPLATE gate is mechanical
    and unwaivable, so it is repaired only within that budget: at 0 the build goes straight to
    ``block`` (never `repairing`), and once the budget is spent it blocks too.

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
    # A PERSON MUST ACT (a secret only they can set, classify_gate_faults "PERSON"): the build
    # cannot pass until they do, so no repair round goes to the coder and nothing ships. Blocked,
    # naming what to set and where; their Retry runs the build again.
    if faults and any(faults.get(g) == "PERSON" for g in failing):
        return "block"
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
    # A BLOCKER STILL OPEN AFTER ITS FIX ROUND IS NOT DONE — checked before advisory, which
    # would otherwise ship it as a reviewer's opinion (crun_2d097414 shipped four).
    live_blockers = [g for g in (blockers or []) if g in failing
                     and (faults or {}).get(g) != "HARNESS"]
    # The cold repair it may buy is still a repair: a template build spends only its own repair
    # budget (template_repair_max), as below. At 0 it blocked here only after spawning a cold
    # ~20-minute child run.
    template_budget_spent = (template_repair_max is not None
                             and rounds_used >= max(0, int(template_repair_max))
                             and any(g in _TEMPLATE_GATE_SET for g in failing))
    if live_blockers:
        if (not fix_round_ran and rounds_used < max_repair_rounds and open_pr
                and not template_budget_spent):
            return "repair"
        return "block"
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
    if template_budget_spent and any(g in _TEMPLATE_GATE_SET for g in failing):
        return "block"
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


AUTO_INFRA_RETRY_KEY = "auto_infra_retry"

#: finalize_ctx marker on the attempt a CI failure started: a CI repair is never repaired again.
CI_REPAIR_KEY = "ci_repair"


def repair_after_ci_failure(engine, settings, jobs, run_id: str, project_id: str,
                            fctx: dict, ci_out: dict) -> Optional[str]:
    """ONE BOUNDED REPAIR ROUND WHEN THE PULL REQUEST'S CI FAILS (BOSSAPP-960).

    A CI failure used to be recorded and then waited for a person: the merge and deploy
    approvals refused (ci_gate.approval_hold), but nothing fed the failure back to an
    implementer — the only loop that existed (the ambient Chief of Staff) is off by default.
    Now the failure starts ONE new attempt in the same lineage, carrying the failing checks as
    structured findings; the attempt commits onto the same branch, so the same pull request
    re-runs the same CI. Bounded exactly like the infra retry: an attempt that IS the CI repair
    is not repaired again (CI_REPAIR_KEY), whatever its own CI says. Never raises; returns the
    new attempt's run id, or None when no repair starts."""
    if str((ci_out or {}).get("status") or "") != "failed":
        return None
    if (fctx or {}).get(CI_REPAIR_KEY):
        with _transaction(engine) as c:
            emit_event(c, project_id, "code_task.ci_repair_withheld", "boss", "code_task_run",
                       run_id, {"why": "this attempt already was the CI repair; a second "
                                       "failure waits for a person"})
        return None
    from .code_task_service import RetryNeedsPerson, retry_code_task

    summary = str((ci_out or {}).get("summary") or "")
    failing = re.findall(r"'([^']+)' failed", summary)
    findings = repair_findings.from_ci(failing, summary=summary,
                                       url=(ci_out or {}).get("failing_url"))
    note = ("Autonomous CI repair: the pull request opened and its CI then FAILED. "
            "Reproduce the failing check locally, fix the cause, and keep the change as small "
            "as the failure requires. The findings:\n"
            + repair_findings.render(findings))
    try:
        retried = retry_code_task(engine, settings, jobs, run_id, automatic=True, fix_note=note)
        new_id = str(retried.get("run_id") or "")
        if not new_id or retried.get("delivery_only"):
            return None
        with _transaction(engine) as c:
            c.execute(sql("UPDATE code_task_runs SET finalize_ctx = COALESCE(finalize_ctx,'{}'::jsonb)"
                          " || jsonb_build_object(CAST(:k AS text), true) WHERE id=:id"),
                      {"k": CI_REPAIR_KEY, "id": new_id})
            emit_event(c, project_id, "code_task.ci_repair_started", "boss", "code_task_run",
                       run_id, {"retry_run_id": new_id, "failing": failing[:6],
                                "why": "the pull request's CI failed; one bounded repair round"})
        return new_id
    except RetryNeedsPerson as withheld:
        try:
            with _transaction(engine) as c:
                emit_event(c, project_id, "code_task.ci_repair_withheld", "boss",
                           "code_task_run", run_id, {"why": str(withheld)[:300]})
        except Exception:  # noqa: BLE001
            _log_caught(logger)
        return None
    except Exception:  # noqa: BLE001 — the recorded CI failure stands; a person still decides
        _log_caught(logger)
        return None


def delivery_failure_words(detail: str) -> str:
    """Why delivering a finished build failed, naming the operation and what Retry will do."""
    import re

    text = str(detail or "")
    code = re.search(r"\b(5\d\d)\b", text)
    if code:
        why = f"GitHub returned {code.group(1)}"
    elif re.search(r"timed? ?out|timeout", text, re.I):
        why = "GitHub did not answer in time"
    elif re.search(r"connect|reset|resolve|network", text, re.I):
        why = "BOSS could not reach GitHub"
    else:
        why = f"GitHub refused it ({text.strip()[:160] or 'no detail'})"
    return (f"{why} while pushing the work and opening the pull request. The build, its tests and "
            "its review are done; Retry pushes and opens the pull request only.")


def _perm_words(perms: Any) -> str:
    if not isinstance(perms, dict) or not perms:
        return "nothing recorded"
    return ", ".join(f"{k}: {v}" for k, v in sorted(perms.items()))


def delivery_credential_facts(connection, run_id: str) -> Optional[dict]:
    """What the push credential for this run's repository asks GitHub for, and what the Setup
    App's installation was last verified to grant. None when there is no Setup App. Never
    raises: a failure explanation without these facts is still an explanation."""
    try:
        from ..identity import github_setup

        point = connection.begin_nested()
        try:
            spec = connection.execute(sql("SELECT spec FROM code_task_runs WHERE id = :i"),
                                      {"i": run_id}).scalar() or {}
            if isinstance(spec, str):
                spec = json.loads(spec)
            full = f"{spec.get('owner') or ''}/{spec.get('repo') or ''}".lower()
            row = connection.execute(sql(
                "SELECT verified FROM identity_github_app LIMIT 1")).first()
        finally:
            point.rollback()
        if row is None or full == "/":
            return None
        verified = row[0] if isinstance(row[0], dict) else json.loads(row[0] or "{}")
        grants = {p.get("name"): p.get("granted") for p in (verified or {}).get("permissions") or []
                  if p.get("granted")}
        return {"repository": full,
                "token_asks": github_setup.control_plane_permissions(connection, full),
                "installation_grants": grants}
    except Exception:  # noqa: BLE001
        _log_caught(logger)
        return None


def hold_delivery(connection, run_id: str, project_id: str, *, files: list[dict], summary: str,
                  quality_gate_id: Optional[str], branch: str, detail: str) -> dict:
    """A gated build whose push / pull request failed: keep what to deliver on the run
    (finalize_ctx.pending_delivery) and fail it `recoverable`, saying which operation failed.
    code_task_service.retry_code_task sees `pending_delivery` and redelivers only."""
    words = delivery_failure_words(detail)
    # WHAT THE CREDENTIAL COULD DO, NEXT TO WHAT FAILED. A 403 alone sent the recovery agent to
    # ask a person to grant `workflows` at GitHub — already granted on the installation, while
    # the token BOSS minted had never asked for it (crun_4eec9642, D2). Facts, not a verdict:
    # the agent reading the failure compares them itself.
    credential = delivery_credential_facts(connection, run_id)
    if credential:
        words = (f"{words} BOSS's push token asked GitHub for "
                 f"{_perm_words(credential.get('token_asks'))}; the App installation grants "
                 f"{_perm_words(credential.get('installation_grants'))}.")
    connection.execute(sql(
        "UPDATE code_task_runs SET finalize_ctx = COALESCE(finalize_ctx,'{}'::jsonb) "
        "|| jsonb_build_object('pending_delivery', CAST(:d AS JSONB)) WHERE id = :id"),
        {"id": run_id, "d": json.dumps({
            "files": files, "summary": summary, "quality_gate_id": quality_gate_id,
            "branch": branch, "failed_operation": "push_and_pull_request",
            "error": str(detail or "")[:500], "credential": credential})})
    emit_event(connection, project_id, "code_task.delivery_held", "boss", "code_task_run", run_id,
               {"failed_operation": "push_and_pull_request", "files": len(files),
                "quality_gate_id": quality_gate_id, "error": str(detail or "")[:300],
                "credential": credential})
    return {**_fail(connection, run_id, project_id, words, failure_class="recoverable",
                    reason_code="delivery_failed"), "delivery_held": True}


def _current_phase_name(engine, run_id: str) -> Optional[str]:
    """The phase a run is in (the stage a harness failure stopped). Never raises."""
    try:
        with _transaction(engine) as c:
            return c.execute(sql("SELECT phase FROM code_task_runs WHERE id=:id"),
                             {"id": run_id}).scalar()
    except Exception:  # noqa: BLE001
        _log_caught(logger)
        return None


def _retry_after_infra_failure(engine, settings, jobs, run_id: str, project_id: str,
                               fctx: dict, result: dict) -> dict:
    """ONE AUTOMATIC RETRY FOR A BUILD THAT DIED OF BOSS, NOT OF ITS CODE.

    crun_966aa44e ran an hour and then failed `infra` on a permission error in BOSS's own
    bookkeeping; the operator's only way on was to notice and press Retry. A failure_class=infra
    run now starts one new attempt in the same lineage at once (retry_code_task: the branch's
    work, the approved design and the completed stages are carried), and the workflow step that
    waited on it follows the new attempt. An attempt that is itself the automatic retry is not
    retried again: the second infra failure stays failed, with Retry offered and a plain reason.
    Never raises; the failure it was given is returned unchanged when no retry starts."""
    if (fctx or {}).get(AUTO_INFRA_RETRY_KEY):
        return {**result, "auto_retry": "not_again",
                "next_step": ("BOSS already retried this build once after a platform error. "
                              "Fix the cause shown above, then press Retry.")}
    from .code_task_service import RetryNeedsPerson, retry_code_task

    try:
        # AS BOSS, UNDER THE PERSON'S GRANT, OR NOT AT ALL (retry_code_task(automatic=True)):
        # crun_da5f6f72 was started bare and refused at once, "No one's authority covers this
        # build", because the worker's own key was the caller. Now the attempt carries the grant
        # the person gave the failed build, or is not started and the build stays failed with
        # Retry for a person.
        retried = retry_code_task(engine, settings, jobs, run_id, automatic=True)
        if retried.get("delivery_only"):
            return {**result, **retried}               # the same run, delivered (or not) in place
        new_id = str(retried.get("run_id") or "")
        with _transaction(engine) as c:
            c.execute(sql("UPDATE code_task_runs SET finalize_ctx = COALESCE(finalize_ctx,'{}'::jsonb)"
                          " || jsonb_build_object(CAST(:k AS text), true) WHERE id=:id"),
                      {"k": AUTO_INFRA_RETRY_KEY, "id": new_id})
            emit_event(c, project_id, "code_task.auto_retried", "boss", "code_task_run", run_id,
                       {"retry_run_id": new_id, "why": "the build stopped on a BOSS platform "
                        "error, not on its code; retrying once automatically"})
        return {**result, "auto_retry_run_id": new_id}
    except RetryNeedsPerson as withheld:
        try:
            with _transaction(engine) as c:
                emit_event(c, project_id, "code_task.auto_retry_withheld", "boss",
                           "code_task_run", run_id, {"why": str(withheld)[:300]})
        except Exception:  # noqa: BLE001
            _log_caught(logger)
        return {**result, "auto_retry": "needs_person", "next_step": str(withheld)}
    except Exception as exc:  # noqa: BLE001 — the original failure stands
        _log_caught(logger)
        try:
            with _transaction(engine) as c:
                emit_event(c, project_id, "code_task.auto_retry_failed", "boss",
                           "code_task_run", run_id,
                           {"error": f"{type(exc).__name__}: {exc}"[:300]})
        except Exception:  # noqa: BLE001
            _log_caught(logger)
            pass
        return {**result, "auto_retry": "refused",
                "next_step": "BOSS could not retry this build automatically; press Retry."}


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


def _applied_connector(engine, run: dict):
    """The manifest of the CONNECTOR this build applies, or None (no template, or not a
    connector, or it cannot be read)."""
    from ..templates import gate as template_gate
    from ..templates.manifest import validate_manifest

    try:
        with _transaction(engine) as c:
            applied = template_gate.applied_template(c, run)
        if not applied or applied.get("error"):
            return None
        manifest = validate_manifest(dict(applied.get("manifest") or {}))
        return manifest if manifest.item_kind == "connector" else None
    except Exception:  # noqa: BLE001 — not known as a connector: the build runs as stored
        _log_caught(logger)
        return None


def _connector_files(engine, run: dict) -> list[str]:
    """The paths of the applied CONNECTOR's own files ([] otherwise): pre-tested in the
    connector's tier, so never a mutation target (mutation_probe.targets)."""
    from ..templates import gate as template_gate

    if _applied_connector(engine, run) is None:
        return []
    try:
        with _transaction(engine) as c:
            applied = template_gate.applied_template(c, run) or {}
        return sorted(str(p) for p in (applied.get("files") or {}))
    except Exception:  # noqa: BLE001 — unknown files: every source file stays a target
        _log_caught(logger)
        return []


def _connector_probe_paths(engine, run: dict) -> list[str]:
    """The GET routes the serve proof probes for an applied CONNECTOR ([] otherwise)."""
    from ..templates import gate as template_gate

    manifest = _applied_connector(engine, run)
    return template_gate.probe_paths(manifest) if manifest is not None else []


def connector_build_input(engine, run: dict, instructions: str, criteria) -> tuple[str, list]:
    """THE ONE CONNECTOR POLICY at run time (templates.connector_policy): a build that applies a
    connector runs with the policy in its brief and wiring criteria, never "the template's tests
    pass", whatever its stored spec says (a retry of a build started before the policy). Every
    seat reads these two through _ctx_block, so the implementer, QA and every reviewer judge
    against the same thing. Anything else is returned unchanged."""
    from ..templates import connector_policy

    manifest = _applied_connector(engine, run)
    if manifest is None:
        return instructions, list(criteria or [])
    return connector_policy.normalize(instructions, criteria, manifest.routes)


def _app_run_declarations(engine, run: dict) -> tuple[dict, dict]:
    """(what the app's templates declare, the values a person bound for its `build` environment)
    for the app-start proof (templates.run_env). The proof reads the app's own files itself.
    Never raises: with nothing known the proof still reads boss.deploy.yaml and .env.example."""
    from ..templates import gate as template_gate
    from ..templates.run_env import app_needs
    from ..templates.secrets import values_for_env

    try:
        with _transaction(engine) as c:
            applied = template_gate.applied_template(c, run)
            if applied and not applied.get("error"):
                app_key = applied.get("app_key")
                needs = app_needs(c, project_id=run["project_id"], app_key=app_key,
                                  manifests=[applied.get("manifest") or {}])
            else:
                # No template: the app's own files declare its names (the proof reads them), and
                # a value a person set for one of them (secrets.deploy_value_spec allows build
                # for an app-declared name) is still this build's.
                from ..app_links.service import app_index

                app_key = app_index(c, run["project_id"])[0].get(str(run.get("id")))
                needs = {}
            org = c.execute(sql("SELECT w.org_id FROM projects p JOIN workspaces w ON w.id = p.workspace_id "
                                "WHERE p.id = :p"), {"p": run.get("project_id")}).scalar()
            bound: dict = {}
            if org and app_key:
                bound, _withheld = values_for_env(c, org_id=org, project_id=run["project_id"],
                                                  app_key=app_key, env="build")
            return needs, bound
    except Exception:  # noqa: BLE001
        _log_caught(logger)
        return {}, {}


def _precheck_app_env(engine, run: dict, root, changed: list) -> Optional[dict]:
    """Run the pre-check's app_env checks on the build's tree and record them as
    `code_task.precheck_app_env` (services.workflow_precheck). Never raises."""
    from pathlib import Path as _Path

    from ..templates import gate as template_gate
    from . import workflow_precheck
    from .preview_service import find_run_declaration

    try:
        base = _Path(str(root))
        declared = find_run_declaration(base, [str(c) for c in changed or []])
        if declared:
            app_dir = base if declared[0] in ("", ".") else base / declared[0]
        else:
            pkgs = sorted(str(c) for c in changed or [] if str(c).endswith("package.json"))
            app_dir = (base / pkgs[0]).parent if pkgs else base
        with _transaction(engine) as c:
            applied = template_gate.applied_template(c, run) or {}
            found = workflow_precheck.app_env_on_tree(
                c, None, project_id=run["project_id"],
                app_key=(applied.get("app_key") if not applied.get("error") else None),
                app_dir=app_dir)
            emit_event(c, run["project_id"], "code_task.precheck_app_env", "boss",
                       "code_task_run", run["id"], found)
        return found
    except Exception:  # noqa: BLE001 — a check that cannot run says nothing; the proof still runs
        _log_caught(logger)
        return None


def _template_gates(engine, run: dict, sandbox, *, root, cwd, changed_files: dict,
                    tests_passed: bool, use_microvm: bool, stack_plan=None,
                    serve: Optional[dict] = None) -> Optional[dict]:
    """THE TEMPLATE HOOK (APP_TEMPLATES_AND_SECURITY_LOOP.md §2.3, stream S2; the one function this
    file holds for F2, coordinated with S1's toolchain code).

    A build started by POST /v1/templates/{id}/apply carries `input.template` on its workflow
    run. For such a build this ADDS two mechanical gates to the ones every build runs (it never
    replaces one, S2.1): `template_tests` (the template's own test command for the stack, run in
    this sandbox, at least `min_tests` passing in its JUnit report, and its test files in the
    diff) and `no_committed_secrets` (the secret scan over the diff, including the values the
    app's build bindings hold). Returns {"gates": {...}, "evidence": {...}} or None for a build
    that applies no template. Fails CLOSED: any error reading the template is a failed gate."""
    from ..templates import gate as template_gate
    from . import workspace_perms as _wsp

    # The template's junit report is written by the run's uid; BOSS reads it next.
    _wsp.normalize(root)

    try:
        with _transaction(engine) as c:
            applied = template_gate.applied_template(c, run)
    except Exception as exc:  # noqa: BLE001 — never let a lookup error pass the gate
        _log_caught(logger)
        return {"gates": {"template_tests": False, "no_committed_secrets": False},
                "evidence": {"template_error": f"{type(exc).__name__}: {exc}"[:300]}}
    if applied is None:
        return None
    if applied.get("error"):
        return {"gates": {"template_tests": False, "no_committed_secrets": False},
                "evidence": {"template": {k: applied.get(k) for k in ("id", "key", "version", "digest")},
                             "template_error": applied["error"]}}
    bound: dict = {}
    try:
        from ..templates.secrets import values_for_env

        with _transaction(engine) as c:
            org = c.execute(sql("SELECT w.org_id FROM projects p JOIN workspaces w ON w.id = p.workspace_id "
                                "WHERE p.id = :p"), {"p": run.get("project_id")}).scalar()
            if org and applied.get("app_key"):
                bound, _withheld = values_for_env(c, org_id=org, project_id=run["project_id"],
                                                  app_key=applied["app_key"], env="build")
    except Exception:  # noqa: BLE001 — the scan still runs on every common format
        _log_caught(logger)
        bound = {}

    import contextlib as _contextlib

    session_env: dict = {}
    backend_note: dict = {}
    with _contextlib.ExitStack() as held:
        # A MULTI-SERVICE APP: the template's tests run in the service of the template's stack
        # (the FastAPI backend, not the app directory a Playwright harness sat in), with that
        # backend STARTED and its URL in BOSS_BACKEND_URL / BACKEND_URL.
        if stack_plan is not None and not use_microvm:
            service = stack_plan.service_for(str(applied.get("stack") or ""))
            if service is not None:
                cwd = stack_plan.dir_of(service)
            if tests_passed and stack_plan.backend is not None:
                from .stack_verify import BackendDidNotStart, backend_running

                _needs, _bound = _app_run_declarations(engine, run)
                try:
                    session = held.enter_context(backend_running(
                        stack_plan, needs=_needs, bound=_bound, path="template_gate"))
                    session_env = session.env()
                    backend_note = {"backend_url": session.url, "backend_command": session.command,
                                    "backend_start": (session.record.get("backend")
                                                      or {}).get("start")}
                except BackendDidNotStart as exc:
                    backend_note = {"backend_error": str(exc)[:1500], "backend_start": exc.record}

        def _run(command: str, where) -> object:
            if session_env:
                from .stack_verify import run_in

                return run_in(sandbox, command, Path(where), env=session_env)
            return sandbox.run(command, cwd=str(where))

        result = template_gate.evaluate(
            template=applied, files=applied.get("files") or {}, changed=changed_files,
            cwd=None if use_microvm else Path(cwd), run_command=None if use_microvm else _run,
            bound_values=bound, tests_passed=bool(tests_passed),
            substrate="microvm" if use_microvm else "local", serve=serve)
    if backend_note and isinstance(result.evidence.get("template_tests"), dict):
        result.evidence["template_tests"].update(backend_note)
    return {"gates": result.gates, "evidence": result.evidence}


def pg_json(value) -> str:
    """JSON Postgres will store: no NUL. A build's context can carry file text, and one NUL
    in it ("\\u0000 cannot be converted to text") crashed the Notes Lite check's final write,
    turning a clean fail-closed stop into a pipeline exception."""
    def _clean(v):
        if isinstance(v, str):
            return v.replace("\x00", "")
        if isinstance(v, dict):
            return {_clean(k): _clean(x) for k, x in v.items()}
        if isinstance(v, (list, tuple)):
            return [_clean(x) for x in v]
        return v

    return json.dumps(_clean(value), default=str)


#: Whether a task's context block carries the service standards, sticky per task once any
#: stage asked, so every stage of one build renders the SAME bytes (_ctx_block). Bounded: a
#: long-lived worker builds many tasks.
_CTX_SERVICE: dict = {}
_CTX_SERVICE_MAX = 256


def _ctx_block(title, instructions, criteria, *, service: bool = False) -> str:
    """Identical task-context preamble for EVERY pipeline stage — byte-identical on
    purpose. The model API's prompt cache is keyed on the request prefix, so stages
    launched back-to-back (within the cache TTL) re-READ this block from cache instead
    of cold-ingesting it in each fresh session. Keep all stage-specific text AFTER it.

    `service` used to make the block differ BY STAGE (QA and implement passed True for a
    served app, the reviewers False), so the shared prefix broke at the standards line. Once
    any stage of a task asks for the service standards, every later stage of the same task
    gets them too (code_agent.SHARED_CONTEXT_START puts the block in the system prompt)."""
    key = (str(title or ""), str(instructions or ""), tuple(str(c) for c in (criteria or [])))
    sticky = bool(service) or bool(_CTX_SERVICE.get(key))
    if sticky and not _CTX_SERVICE.get(key):
        if len(_CTX_SERVICE) >= _CTX_SERVICE_MAX:
            _CTX_SERVICE.pop(next(iter(_CTX_SERVICE)), None)
        _CTX_SERVICE[key] = True
    return _render_ctx_block(title, instructions, criteria, service=sticky)


def _render_ctx_block(title, instructions, criteria, *, service: bool = False) -> str:
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
    # HOW BOSS WORKS, for a build stage (agent_baseline.BUILD_PRIMER): static, and first after
    # the header, so it is the same bytes for every task's stages as well as one task's.
    from .agent_baseline import BUILD_PRIMER

    return (
        "=== TASK CONTEXT (shared verbatim across all pipeline stages) ===\n"
        f"{BUILD_PRIMER}"
        f"TASK: {title}\n\n{instructions}\n\nACCEPTANCE CRITERIA:\n{crit}\n"
        f"{standards}{references}\n"
        "=== END TASK CONTEXT ===\n\n"
    )


#: The test author writes no credential literal. BOSS's no_committed_secrets gate scans tests too,
#: and the coder may not edit them, so a password the QA stage typed into a fixture blocked every
#: repair round of the HelpDesk build (crun_b4613dcf: 8 findings, all in tests).
_NO_SECRETS_IN_TESTS = (
    "- NEVER write a password, token, API key or secret as a literal in a test or fixture. "
    "Generate it at run time (crypto.randomUUID(), secrets.token_urlsafe()) or read it from an "
    "environment variable the test harness sets for itself (e.g. process.env.DEMO_PASSWORD ?? "
    "a value generated once per run). BOSS blocks any committed secret-looking literal, in tests "
    "too, and the implementer is not allowed to edit your tests.\n"
)


#: The most test cases a build writes when the project sets no build_test_budget.
TEST_BUDGET_DEFAULT = 20

#: One test case, per language: JS/TS it()/test(), Python def test_, JVM @Test, Go func Test.
_TEST_CASE_RE = re.compile(
    r"^\s*(?:(?:it|test)(?:\.(?:only|skip|concurrent|each\s*\([^\n]*?\)))?\s*\(\s*[`'\"]"
    r"|(?:async\s+)?def\s+test_\w*\s*\("
    r"|@Test\b"
    r"|func\s+Test\w*\s*\()",
    re.MULTILINE)


def test_budget(criteria, budget: Optional[int] = None) -> int:
    """The most test cases this build writes: 1-3 per acceptance criterion and 3 edge cases,
    never more than the project's budget (build_test_budget, default 20), however long the brief.
    A HelpDesk-size brief's suite grew with the brief: 76 KB of tests and 27.6 minutes of
    write_tests, then 81 minutes of implement making them all pass."""
    from .build_scope import MORE_TESTS_BUDGET_FACTOR, more_tests_light, more_tests_pass

    n = max(1, len([c for c in (criteria or []) if str(c).strip()]))
    cap = int(budget or TEST_BUDGET_DEFAULT)
    if more_tests_pass():
        # Pass 2 adds coverage: twice pass 1's budget, not sized to the criteria. A light pass
        # (a hotfix's regression tests) gets pass 1's budget.
        return max(1, cap if more_tests_light() else cap * MORE_TESTS_BUDGET_FACTOR)
    return max(1, min(cap, 3 * n + 3))


def count_test_cases(root: Path, files) -> int:
    """Test cases in `files` (repo-relative), counted by BOSS, not reported by the model."""
    total = 0
    for rel in files or []:
        try:
            text = (Path(root) / rel).read_text(errors="ignore")
        except OSError:
            continue
        total += len(_TEST_CASE_RE.findall(text))
    return total


def budget_files(test_files, preexisting, *, placed=(), scope=None) -> list[str]:
    """The test files the TEST BUDGET counts: the ones THIS build wrote (not pre-existing in the
    tree, not a template's placed tests), inside the app's own directory (`scope`). The repo's
    existing tests, of this app or any other, are never counted and never trimmed."""
    from .verification_target import in_app_scope

    before, held = set(preexisting or ()), set(placed or ())
    return [p for p in dict.fromkeys(test_files or ())
            if p not in before and p not in held and in_app_scope(p, scope)]


def _test_budget_trim_prompt(written: int, cap: int, files=()) -> str:
    """The test author's one trim turn when its suite is over the budget (resumed session). It
    names the files it may trim — the ones this build wrote — and nothing else."""
    listing = "".join(f"  - {p}\n" for p in list(files)[:40])
    return (
        f"--- TEST BUDGET ---\nBOSS counted {written} test cases in the files you wrote"
        + (f":\n{listing}" if listing else "; ")
        + f"this build's hard limit is {cap}. Delete the lowest-priority cases until at most {cap} "
        "remain: keep one per acceptance criterion first, then the riskiest behaviour; drop "
        "duplicates, variations of one case and edge cases first. Edit ONLY the files listed; "
        "never edit or delete any other test file (the repository's existing tests are not "
        "yours to trim); do not add cases. End with the updated `TEST PLAN:`.\n")


def _test_scope_clause(criteria, budget: Optional[int] = None) -> str:
    """QA's tests are SIZED to what was asked, and start with a plan the implementer gets.

    The Notes Lite check (crun_3a39cd19) wrote 34,564 tokens of tests for a three-criterion
    notes app. Over-specified tests (exact wording, internals, extras nobody asked for) are what
    the implementer then failed to satisfy for four rounds. One to three tests per criterion and
    a few edge cases is a suite a build can pass; the plan makes the contract explicit up front.
    The cap (test_budget) never grows past the project's budget, and BOSS counts afterwards.
    """
    from .build_scope import more_tests_pass

    n = max(1, len([c for c in (criteria or []) if str(c).strip()]))
    cap = test_budget(criteria, budget or get_llm_context().get("test_budget"))
    if more_tests_pass():
        # PASS 2 (two_pass): the P0/P1 suite exists and passes; this pass is P2 coverage.
        scope = (
            f"- At most {cap} NEW test cases, all P2: edge cases, error paths, input validation, "
            "API contracts (status codes, response shapes), and browser tests only where the "
            "task asked for them. Pass 1's P0/P1 tests stay as they are; do not duplicate "
            "them.\n")
    else:
        # PASS 1: only the critical paths, labelled, so the More tests pass knows what is left.
        scope = (
            f"- At most {cap} test cases in total, ONLY the critical paths, each named with its "
            "priority: P0 = the app starts, sign-in/authorization, the core happy path; P1 = "
            "the main path of each requested feature. One per acceptance criterion "
            f"({n} given) FIRST. "
            "No P2 (edge cases, error paths, contract detail): a More tests pass on this branch "
            "adds those after the PR and preview.\n")
    return (
        "\nTEST SCOPE (mandatory; BOSS counts the cases and trims a suite that is over):\n"
        + scope +
        # A HelpDesk-lite suite passed for BOSS and failed for every reviewer: one test promoted
        # the seeded customer to agent and never put it back, so a second run (and any other
        # file order) failed 4 tests, and the implementer may not edit tests to fix it.
        "- Tests are independent of order and of each other: a test that changes shared state "
        "(a seeded user's role, a shared row) creates its own records or restores what it "
        "changed. The suite must pass when run twice in a row against the same database.\n"
        "- Test only behaviour the task or its criteria STATE, through public interfaces (HTTP "
        "responses, exported functions, visible DOM). Never exact copy text, CSS, internal names, "
        "timing, or anything the task did not ask for.\n"
        "- No test infrastructure the task did not ask for (no browser suite, metrics or extra "
        "services unless the task names them).\n"
        "- START your final reply with `TEST PLAN:` and one line per test: "
        "`<file>::<test name> -> <criterion>`. The implementer receives this plan.\n"
    )


def _test_plan_note(qa_summary: str) -> str:
    """The QA plan, handed to the implementer before it writes code."""
    text = str(qa_summary or "")
    at = text.find("TEST PLAN:")
    if at < 0:
        return ""
    plan = text[at:at + 3000].strip()
    return ("\n\nTHE TESTS YOU MUST MAKE PASS (QA's plan, agreed before any code):\n" + plan
            + "\nImplement exactly what these need, and nothing the task did not ask for.\n")


def _deferred_e2e_note() -> str:
    """Browser tests the brief asked for go to the More tests pass (build_scope)."""
    from .build_scope import deferred_note

    return deferred_note()


def _wants_e2e() -> bool:
    """Browser/e2e tests only when the task asked for them (build_scope)."""
    from .build_scope import wants_e2e

    return wants_e2e(True)


def _qa_prompt(title, instructions, criteria, test_cmd, *, iac=False, web=False, app_root=None,
               stack_note=""):
    service = bool(web or app_root)
    if stack_note:
        # A MULTI-SERVICE APP: each service's tests in its own toolchain, browser tests in the
        # front end — never a JS harness at the root of a Python app (crun_78d197f7614f40f38993).
        return (
            _ctx_block(title, instructions, criteria, service=True)
            + "You are a QA engineer for an app made of a backend and a web front end. Your ONLY "
            "job is to author the tests BOSS will run, BEFORE the implementation exists.\n\n"
            "RULES:\n"
            "- Backend tests in the backend's own test framework, inside the backend's "
            "directory. Browser (Playwright) e2e specs inside the front end's e2e/ directory, "
            "driving the real UI against the started backend.\n"
            "- You MAY create what a test runner needs to find the tests: the front end's "
            "package.json scripts and playwright.config, the backend's test dependencies.\n"
            "- Do NOT create the product implementation.\n"
            f"- Do NOT run anything and do NOT commit. (BOSS will run: {test_cmd}.)\n"
            "- End with a one-line summary of the cases you wrote.\n"
            + _metrics_tests_clause(title, instructions, service=True)
            + _test_scope_clause(criteria) + _deferred_e2e_note() + _KEEP_MONITORING
            + (_IAC_GUIDANCE + _IAC_QA_GUIDANCE if iac else "")
            + stack_note
        )
    if web:
        scope = f"`{app_root}/`" if app_root else "the app directory"
        return (
            _ctx_block(title, instructions, criteria, service=service)
            + "You are a QA engineer for a web application. Your ONLY job is to author tests "
            "that the authoritative `npm run verify` command will actually run.\n\n"
            "RULES:\n"
            + (f"- Write JS/TS API tests and a Playwright/e2e spec under {scope}. "
               "Prefer `tests/` and `e2e/` inside that app.\n" if _wants_e2e() else
               f"- Write JS/TS tests under {scope} (prefer `tests/`), with the test tools the "
               "task names. No browser/Playwright suite: the task did not ask for one.\n")
            + "- Do NOT write Python unittest files. Do NOT put tests at the monorepo root.\n"
            "- Do NOT edit sibling apps or repo-root tests that belong to other products.\n"
            f"- You MAY add `{app_root or '.'}/package.json` scripts "
            "(verify, test:api, test:e2e, typecheck, lint, build, healthcheck) if they are missing.\n"
            "- Do NOT create the product implementation.\n"
            f"- Do NOT run anything and do NOT commit. (BOSS will run `{test_cmd}` inside {scope}.)\n"
            "- End with a one-line summary of the cases you wrote.\n"
            + _NO_SECRETS_IN_TESTS + _metrics_tests_clause(title, instructions, service=service)
            + _test_scope_clause(criteria) + _deferred_e2e_note() + _KEEP_MONITORING
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
        + _NO_SECRETS_IN_TESTS + _metrics_tests_clause(title, instructions, service=service)
        + _test_scope_clause(criteria) + _deferred_e2e_note() + _KEEP_MONITORING
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
    from .build_scope import wants_ops
    from .platform_standards import METRICS_TEST_REQUIREMENT, applies_to

    if not wants_ops(bool(service or applies_to(f"{title}\n{instructions}"))):
        return ""
    return "\n\n" + METRICS_TEST_REQUIREMENT + "\n"


def _monitoring_removals(spec: Optional[dict]) -> Optional[list[str]]:
    """The monitoring entries a change ABOUT monitoring was asked to remove, as its spec names
    them (monitoring_guard.entries' names). Every other removal is undone at the PR."""
    got = (spec or {}).get("monitoring_removals") if isinstance(spec, dict) else None
    return [str(x) for x in got] if isinstance(got, list) else None


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


#: WHAT THE REVIEWERS CHECK, GIVEN TO THE IMPLEMENTER BEFORE IT HANDS OFF. A HelpDesk-lite
#: build (crun_4813f68c) spent 10.5 min in two fix rounds on findings any of these lines names:
#: async handlers whose rejected queries crashed the process, an unvalidated id reaching SQL, an
#: array in a login body. Getting the first pass right is cheaper than the round that fixes it.
#: THE SECRETS RULE, up front for every builder and again in the secret repair turn. A build the
#: secret scan blocks opens no pull request (crun_ced396a4: one literal at auth/demo.mjs:58).
SECRETS_FROM_ENV_RULE = (
    "Secrets come from the app's Secrets / environment (e.g. process.env.DEMO_PASSWORD); never a "
    "literal. No password, token, API key or secret in code or committed config: read it from the "
    "environment. BOSS's secret scan blocks a literal one, and a blocked build opens no pull "
    "request.")

FIRST_PASS_CHECKLIST = (
    "\nBEFORE YOU HAND OFF (the reviewers check exactly this; spend one minute on it and fix "
    "what fails):\n"
    "- Every async handler's errors reach the error handler (try/catch with next(err), or a "
    "wrapper): no rejected promise or thrown error can crash the process.\n"
    "- Every route checks who may call it (signed in, then role or ownership) before it reads "
    "or writes anything.\n"
    "- Every input is validated before use: required fields, types (a string is not an array or "
    "object), ids well-formed and in range. Bad input is a 4xx, never a 500.\n"
    "- " + SECRETS_FROM_ENV_RULE + "\n"
    "- A demo/seed password BOSS provides (DEMO_PASSWORD) is allowed in production: never refuse "
    "it. Gate demo seeding on an explicit opt-in flag (e.g. ALLOW_DEMO_SEED) instead.\n"
    "- Exactly what the criteria name: nothing unasked, no unused dependencies.\n"
    "- BOSS installs with a clean `npm install`; peer-dependency errors fail the build. Pick "
    "versions that agree (e.g. eslint 9 needs eslint-plugin-react-hooks >=5). Run BOSS's exact "
    f"install once to prove it: `{verification_target.NPM_INSTALL_CMD}`.\n"
    + "- " + verification_target.WEB_GATE_RULE + "\n"
    + "- " + _run_env.OPTIONAL_SETTINGS_RULE + "\n"
)


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
        f"- Run `{test_cmd}` from the app directory when a chunk of work is finished, and "
        "iterate until the tests pass. "
        "Create parent directories for SQLite/data files before opening them.\n"
        + _run_declaration_rule() + _KEEP_MONITORING
        + "- Do NOT commit. End with a one-line summary of what you changed.\n"
        + _deferred_e2e_note()
        + FIRST_PASS_CHECKLIST
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
    For a panel, the code review is the seat whose findings stand for it. When the panel sat
    as ONE session (BOSSAPP-1000, stage `review_panel`), that session's act signs every seat's
    findings that has no act of its own — the one agent really did write them."""
    out: dict = {}
    try:
        rows = c.execute(sql(
            "SELECT act_id, role_key, step_id, seat FROM agent_acts WHERE kind = 'stage' "
            "AND code_task_run_id = :r AND step_id IN ('review_code','security_check',"
            "'consistency_check','review_panel') ORDER BY started_at"),
            {"r": run_id}).mappings().all()
    except Exception:  # noqa: BLE001 — a signature is never worth losing the comments
        _log_caught(logger)
        return out
    for r in rows:
        stage = r["step_id"]
        if stage == "review_code" and review_seat and r["seat"] not in (None, review_seat):
            continue
        out[stage] = {"act_id": r["act_id"], "role_key": r["role_key"]}
    merged = out.pop("review_panel", None)
    if merged:
        for stage in ("review_code", "security_check", "consistency_check"):
            out.setdefault(stage, merged)
    return out


def _record_review_round(engine, run_id: str, project_id: str, *, round_i: int,
                         seats: dict, kind: str = "", reviewed: Optional[dict] = None) -> None:
    """One review round as it happened: each seat's verdict, its summary and whether it saw
    the change. The pull request gets one GitHub review per round (pr_review_post), so a person
    reads the review CONVERSATION, not only the last word. Re-recording a round replaces it."""
    from datetime import datetime as _dt, timezone as _tz

    entry = {"round": int(round_i), "at": _dt.now(_tz.utc).isoformat(),
             **({"kind": kind} if kind else {}),
             # THE CHANGE THIS ROUND JUDGED, as {path: content hash} (repair_review_scope.
             # snapshot): a later repair attempt re-reviews only what differs from it.
             **({"reviewed": reviewed} if isinstance(reviewed, dict) else {}),
             "seats": {stage: {"verdict": str((s or {}).get("verdict") or "not_run"),
                               "summary": str((s or {}).get("summary") or "")[:4000],
                               "hollow": (s or {}).get("hollow")}
                       for stage, s in (seats or {}).items()}}
    try:
        with _transaction(engine) as c:
            row = c.execute(sql(
                "SELECT finalize_ctx FROM code_task_runs WHERE id=:id AND project_id=:p"),
                {"id": run_id, "p": project_id}).mappings().first()
            rounds = [r for r in (((row or {}).get("finalize_ctx") or {}).get("review_rounds")
                                  or []) if isinstance(r, dict) and r.get("round") != round_i]
            rounds.append(entry)
            rounds.sort(key=lambda r: int(r.get("round") or 0))
            c.execute(sql(
                "UPDATE code_task_runs SET finalize_ctx = COALESCE(finalize_ctx,'{}'::jsonb) "
                "|| jsonb_build_object('review_rounds', CAST(:r AS JSONB)), updated_at=now() "
                "WHERE id=:id AND project_id=:p"),
                {"r": _json_dumps(rounds), "id": run_id, "p": project_id})
    except Exception as exc:  # noqa: BLE001 — a review must not die recording its own output
        _log_caught(logger)
        logger.warning("code_task.review_round_not_recorded run=%s %s: %s",
                       run_id, type(exc).__name__, exc)


def _record_comment_responses(engine, run_id: str, project_id: str, answered: list,
                              reply: str, *, round_i: int, changed_files: list) -> int:
    """The implementer's answer to each numbered comment it was shown (review_comments.
    implementer_order), stored on the comment so the pull request shows it as a reply."""
    responses = review_comments.parse_responses(reply)
    if not responses or not answered:
        return 0
    by_id: dict[str, tuple[str, str]] = {}
    for n, (kind, words) in responses.items():
        ref = getattr(answered[n - 1], "ref", None) if 1 <= n <= len(answered) else None
        if ref:
            by_id[ref] = (kind, words)
    if not by_id:
        return 0
    try:
        with _transaction(engine) as c:
            row = c.execute(sql(
                "SELECT finalize_ctx FROM code_task_runs WHERE id=:id AND project_id=:p"),
                {"id": run_id, "p": project_id}).mappings().first()
            stored = list(((row or {}).get("finalize_ctx") or {}).get("review_comments") or [])
            for entry in stored:
                hit = by_id.get((entry or {}).get("id"))
                if hit:
                    entry.update({"response_kind": hit[0], "response": hit[1],
                                  "response_round": int(round_i),
                                  "response_files": sorted(changed_files)[:20]})
            c.execute(sql(
                "UPDATE code_task_runs SET finalize_ctx = COALESCE(finalize_ctx,'{}'::jsonb) "
                "|| jsonb_build_object('review_comments', CAST(:e AS JSONB)), updated_at=now() "
                "WHERE id=:id AND project_id=:p"),
                {"e": _json_dumps(stored), "id": run_id, "p": project_id})
        # A local change's answers become thread replies when the change set is refreshed
        # (local_review.on_change_proposed), so each reply cites the commit that holds the fix.
        return len(by_id)
    except Exception as exc:  # noqa: BLE001
        _log_caught(logger)
        logger.warning("code_task.comment_responses_not_recorded run=%s %s: %s",
                       run_id, type(exc).__name__, exc)
        return 0


def _review_record(engine, run_id: str, project_id: str) -> tuple[list[dict], list[dict], dict]:
    """(rounds, comments, posted-state) as recorded on the run."""
    try:
        with _transaction(engine) as c:
            row = c.execute(sql(
                "SELECT finalize_ctx FROM code_task_runs WHERE id=:id AND project_id=:p"),
                {"id": run_id, "p": project_id}).mappings().first()
    except Exception:  # noqa: BLE001 — a description is never worth a failed PR
        _log_caught(logger)
        return [], [], {}
    fctx = (row or {}).get("finalize_ctx") or {}
    return ([r for r in fctx.get("review_rounds") or [] if isinstance(r, dict)],
            [c for c in fctx.get("review_comments") or [] if isinstance(c, dict)],
            fctx.get("pr_review_posts") if isinstance(fctx.get("pr_review_posts"), dict) else {})


def _generated_pr_summary(engine, settings, run_id: str, project_id: str, *, title: str,
                          evidence: dict, gates: dict, paths: list, qg_id: str) -> str:
    """The PR description from the run's records (pr_description) — never the implementer's
    closing chat, which is what PR #223's description was."""
    from . import pr_description

    rounds, comments, _ = _review_record(engine, run_id, project_id)
    if not rounds and evidence.get("review_summary"):
        from .pr_review_post import legacy_round

        rounds = [legacy_round(evidence, paths)]
    return pr_description.render(
        title=title, evidence=evidence, gates=gates, paths=paths,
        additions=evidence.get("additions"), deletions=evidence.get("deletions"),
        rounds=rounds, comments=comments, deferred=evidence.get("deferred_findings") or [],
        build_url=pr_description.build_url(settings, run_id, project_id), run_id=run_id,
        quality_gate_id=qg_id or "", baseline=evidence.get("baseline"))


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
                added.append({**comment.to_dict(), "id": make_id("rc"), "round": round_i,
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
        _log_caught(logger)
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
        _log_caught(logger)
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
            _log_caught(logger)
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
        _log_caught(logger)
        return []
    out = []
    for entry in (((row or {}).get("finalize_ctx") or {}).get("review_comments") or []):
        try:
            out.append(review_comments.ReviewComment.from_dict(entry))
        except Exception:  # noqa: BLE001
            _log_caught(logger)
            continue
    return out


def _post_review_comments_to_pr(sf, proposal: dict, evidence: dict, engine, project_id,
                                run_id: str) -> dict:
    """Put the review CONVERSATION on the pull request (pr_review_post): one GitHub review per
    round, findings at their lines, the implementer's answers as replies, deferred findings
    listed. PR #223 carried one review holding a summary that saw nothing; a person reading the
    PR could not tell what the agents had argued about or fixed.

    Idempotent through `finalize_ctx.pr_review_posts` (a re-run updates, never duplicates).
    Best effort, always: a review that cannot be posted never blocks a pull request that passed
    its gates. A connection that may not write reviews is said in words — on the build page
    (`evidence.review_comments_posted.reason`) and as an event — never skipped silently.
    """
    from . import pr_review_post

    number = proposal.get("pull_request_number")
    if not number:
        return {"posted": False, "reason": "no pull request number"}
    rounds, comments, state = _review_record(engine, run_id, project_id)
    if not comments:
        # A run recorded before comments were: parse what the reviewers wrote.
        for stage, key in (("review_code", "review_summary"),
                           ("security_check", "security_summary"),
                           ("consistency_check", "consistency_summary")):
            comments += [c.to_dict() for c in
                         review_comments.parse_findings(evidence.get(key) or "", stage=stage)]
    if not rounds:
        rounds = [pr_review_post.legacy_round(evidence)]
    if not comments and not any(str((s or {}).get("summary") or "").strip()
                                for r in rounds for s in (r.get("seats") or {}).values()):
        reason = "no reviewer produced findings or a summary"
        with _transaction(engine) as c:
            emit_event(c, project_id, "code_task.review_comments_absent", "boss",
                       "code_task_run", run_id,
                       {"pull_request_number": number, "reason": reason,
                        "stages": ["review_code", "security_check", "consistency_check"]})
        # THE PR SAYS SO. A pull request with no review conversation is not evidence that a
        # review happened; silence there read as "reviewed" (KG P6a PR #25). Once per PR.
        if not (state or {}).get("absent_notice"):
            seats = [f"- {name}: {(evidence.get(key) or 'not_run')}" for name, key in (
                ("Code review", "adversarial_verdict"), ("Security review", "security_verdict"),
                ("Consistency review", "consistency_verdict"))]
            body = ("### BOSS review — NOT DONE\n\nNo reviewer's findings or summary reached "
                    "this pull request, so there is no evidence it was reviewed. Do not treat "
                    "it as reviewed.\n\n" + "\n".join(seats) +
                    f"\n\nBuild: `{run_id}`")
            try:
                sf.github.create_review(int(number), body=body, event="COMMENT", comments=None)
                state = {**(state or {}), "absent_notice": True}
                with _transaction(engine) as c:
                    c.execute(sql(
                        "UPDATE code_task_runs SET finalize_ctx = COALESCE(finalize_ctx,'{}'::jsonb) "
                        "|| jsonb_build_object('pr_review_posts', CAST(:s AS jsonb)) WHERE id = :id"),
                        {"s": json.dumps(state), "id": run_id})
            except Exception:  # noqa: BLE001 — never block a PR on a comment
                _log_caught(logger)
        return {"posted": False, "reason": reason}
    repository = str(getattr(sf.github, "repository_full_name", "") or "")
    try:
        out = pr_review_post.post(sf.github, int(number), comments=comments, rounds=rounds,
                                  deferred=evidence.get("deferred_findings") or [],
                                  state=state, repository=repository)
    except Exception as exc:  # noqa: BLE001 — never block a passing PR on a comment
        _log_caught(logger)
        out = {"posted": False, "reason": f"{type(exc).__name__}: {exc}"[:200], "state": state}
    new_state = out.pop("state", state) or {}
    try:
        with _transaction(engine) as c:
            c.execute(sql(
                "UPDATE code_task_runs SET finalize_ctx = COALESCE(finalize_ctx,'{}'::jsonb) "
                "|| jsonb_build_object('pr_review_posts', CAST(:s AS JSONB), "
                "'pr_review_post_result', CAST(:r AS JSONB)), updated_at=now() "
                "WHERE id=:id AND project_id=:p"),
                {"s": _json_dumps(new_state), "r": _json_dumps(out), "id": run_id,
                 "p": project_id})
            emit_event(c, project_id,
                       "code_task.review_comments_posted" if out.get("posted")
                       else "code_task.review_comments_failed", "boss", "code_task_run", run_id,
                       {"pull_request_number": number, **{k: v for k, v in out.items()
                                                         if k != "review_url"}})
    except Exception:  # noqa: BLE001
        _log_caught(logger)
    return out


def _with_ci_static(rr: dict, files) -> dict:
    """A review result with BOSS's deterministic workflow findings appended (ci_gate), so the
    gate, the PR comments and the review in BOSS read them even from a scripted model."""
    try:
        block = ci_gate.findings_block(ci_gate.static_review(files))
    except Exception:  # noqa: BLE001
        _log_caught(logger)
        block = ""
    return {**(rr or {}), "summary": ((rr or {}).get("summary") or "") + block} if block else rr


def _wait_for_ci(engine, settings, sf, proposal: dict, project_id: str, run_id: str,
                 repository: str, *, sleep=time.sleep) -> dict:
    """Wait for the pull request head's checks (ci_gate). Never raises: the step's result is
    words — "CI passed: 3 checks", "CI failed: 'unit-tests' failed — <url>", "CI did not finish
    in 30 min" — and the gate on merge/deploy is the recorded wait, not this return value."""
    number = proposal.get("pull_request_number")
    url = proposal.get("pull_request_url")
    if not number:
        return {"status": "skipped", "summary": "No pull request to wait for CI on."}
    sha = ""
    try:
        reader = ci_gate.AdapterCiReader(sf.github)
        sha = sf.github.pull_request_snapshot(int(number)).get("head_sha") or ""
        if not (isinstance(sha, str) and re.fullmatch(r"[0-9a-f]{40}", sha)):
            sha = ""
            return _ci_unreadable(engine, project_id, run_id, repository, url, "",
                                  "the pull request's head commit is unknown")
        has_ci = ci_gate._safe_has_workflows(reader, sha)  # noqa: SLF001
        if not ci_gate.hosted_ci(reader):
            # BOSSAPP-946: a local repository has no hosted CI; BOSS's sandbox checks are its CI.
            out = {"status": "no_ci",
                   "summary": ci_gate.NO_HOSTED_CI_SUMMARY.format(repository=repository)}
            with _transaction(engine) as c:
                emit_event(c, project_id, "code_task.ci_waited", "boss", "code_task_run",
                           run_id, {"pull_request_url": url, **out})
            return out
        if has_ci is False and not ci_gate.workflow_paths(
                (proposal.get("files_changed") or [])):
            out = {"status": "no_ci", "summary": f"No CI is configured for {repository} (no "
                                                 ".github/workflows), so BOSS did not wait."}
            with _transaction(engine) as c:
                emit_event(c, project_id, "code_task.ci_waited", "boss", "code_task_run",
                           run_id, {"pull_request_url": url, **out})
            return out
        with _transaction(engine) as c:
            wait = ci_gate.start_wait(
                c, project_id=project_id, repository=repository, head_sha=sha,
                pull_request_url=url, code_task_run_id=run_id,
                # True: workflows found or added; None: unknown, never read as "has CI".
                has_ci=(True if has_ci or ci_gate.workflow_paths(
                    proposal.get("files_changed") or []) else None),
                timeout_seconds=int(settings.ci_wait_timeout_seconds))
        got = ci_gate.run_blocking(lambda: _transaction(engine), wait["id"], reader, sleep=sleep)
        out = {"id": got["id"], "status": got["status"], "summary": got["summary"],
               "failing_url": got.get("failing_url")}
    except Exception as exc:  # noqa: BLE001 — never lose the PR to a CI read; FAIL CLOSED
        _log_caught(logger)
        return _ci_unreadable(engine, project_id, run_id, repository, url,
                              sha if isinstance(sha, str) else "",
                              f"{type(exc).__name__}: {str(exc)[:200]}")
    with _transaction(engine) as c:
        emit_event(c, project_id, "code_task.ci_waited", "boss", "code_task_run", run_id,
                   {"pull_request_url": url, **out})
    return out


def _ci_unreadable(engine, project_id: str, run_id: str, repository: str, url, sha: str,
                   why: str) -> dict:
    """FAIL CLOSED: CI that could not be read is recorded against the PR as `unreadable`, so the
    merge and the deploy approval say "CI could not be read: <why>" until it is read again (or a
    person forces the merge with a note). Only an explicit no-CI repository passes unread."""
    out = {"status": "unreadable", "summary": ci_gate.unreadable_words(why)}
    try:
        with _transaction(engine) as c:
            wait = ci_gate.record_unreadable(c, project_id=project_id, repository=repository,
                                             why=why, head_sha=sha, pull_request_url=url,
                                             code_task_run_id=run_id)
            out["id"] = wait["id"]
            emit_event(c, project_id, "code_task.ci_waited", "boss", "code_task_run", run_id,
                       {"pull_request_url": url, **out})
    except Exception:  # noqa: BLE001 — recording must never sink the finished PR
        _log_caught(logger)
        logger.warning("could not record unreadable CI for %s", run_id, exc_info=True)
    return out


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


def carried_implement_session(fctx, *, agent, model, stage: str = "implement") -> Optional[str]:
    """The parent attempt's implement session id, when THIS attempt may resume it — or None,
    which is a cold start exactly as before (BOSSAPP-1001).

    A repair round used to pay a fresh CLI session per attempt: the ~34k preamble plus the whole
    repo re-onboarded, to patch work the parent's session already holds warm (audit BOSSAPP-997:
    ~78% of claude-sonnet-5 spend was cache re-read/write from cold starts). retry_code_task
    carries the parent's implement session (`resume_implement_session`, recorded on the run as
    `state.impl_session`), and the child's FIRST implement pass — which for a cross-run repair IS
    the repair — resumes it, sending only the findings as the next user turn.

    Never-when (each returns None, i.e. today's cold start):
      * any stage but the implementer's own ("implement"/"solo"): a reviewer or test author must
        NEVER inherit the builder's conversation — review independence is judged, not assumed;
      * the engine cannot resume (capabilities.resume False), or the id is one BOSS issued for a
        sessionless engine (SESSION_PREFIX): that names a process-local ledger entry a new worker
        does not hold, and "resuming" it would silently DROP the task context;
      * a different engine or model than the session ran on: a conversation is never switched to
        another model mid-flight (same rule as the in-run `_same_route` guard).

    A resume that then FAILS (expired session, engine error) falls back to the cold start at the
    call site — degradation only ever re-adds the old cost.
    """
    rec = (fctx or {}).get("resume_implement_session")
    if not isinstance(rec, dict):
        return None
    if stage not in ("implement", "solo"):
        return None
    sid = str(rec.get("session_id") or "").strip()
    if not sid or sid.startswith(SESSION_PREFIX):
        return None
    if not capabilities_of(agent).resume:
        return None
    if str(rec.get("engine") or "") != str(getattr(agent, "engine", "") or ""):
        return None
    if str(rec.get("model") or "") != str(model or ""):
        return None
    return sid


def _cross_run_resume_prompt(evidence: str, attempt: int) -> str:
    """The repair child's first turn when it RESUMES the parent attempt's implement session:
    only the findings travel — the task, the repo and the session's own work are warm cache
    reads. The tree note matters: the child runs in a FRESH checkout of the lineage branch
    (a new directory seeded with the session's work), so the session's memory of file contents
    may be stale even though the paths hold."""
    return (
        f"--- REPAIR ATTEMPT {attempt} (resuming your implement session) ---\n"
        "This build did NOT pass its quality gates, and you are resuming your own session to "
        "repair it. Your working directory is a FRESH checkout that already contains your "
        "earlier work: paths from earlier turns hold relative to the current working directory, "
        "but THE TREE IS THE TRUTH — re-read a file before editing it rather than trusting your "
        "memory of it (a checkpoint rewind or a seeded workspace may differ from your last "
        "turn). The tests are FROZEN and correct — do NOT edit, delete, or weaken any test "
        "file. Address EVERY finding below with the smallest correct change, run the tests, and "
        "end with a one-line summary of what you fixed.\n"
        f"The findings:\n{evidence}\n"
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
            + ("- Write JS/TS API and e2e tests, not Python unittest.\n" if _wants_e2e()
               else "- Write JS/TS tests, not Python unittest.\n" + _deferred_e2e_note())
            + f"- Run `{test_cmd}` from {scope} when a chunk of work is finished, and iterate "
            "until it passes. "
            "Create parent directories for SQLite/data files before opening them.\n"
            + _run_declaration_rule() + _KEEP_MONITORING
            + "- Do NOT commit. End with a one-line summary of what you built.\n"
            + FIRST_PASS_CHECKLIST
            + (_IAC_GUIDANCE + _IAC_QA_GUIDANCE if iac else "")
        )
    return (
        _ctx_block(title, instructions, criteria)
        + "You are a software engineer. Implement the task above AND write tests for it, then make "
        "the tests pass. This is a low-risk task, so one agent does both.\n\n"
        "RULES:\n"
        "- Create the implementation file(s) and stdlib `unittest` test file(s).\n"
        f"- Run `{test_cmd}` when a chunk of work is finished, and iterate until it passes. "
        "Do NOT commit.\n"
        + "- " + SECRETS_FROM_ENV_RULE + "\n"
        + _run_declaration_rule() + _KEEP_MONITORING
        + "- End with a one-line summary of what you built.\n"
        + (_IAC_GUIDANCE + _IAC_QA_GUIDANCE if iac else "")
    )


def _implementer_answers(engine, run_id: str, project_id: str) -> str:
    """THE OTHER SIDE OF THE CONVERSATION: the implementer's FIXED / DISPUTED answer to each
    review comment, for the reviewer's next round. Without it a re-review saw only its own
    findings, so a DISPUTED finding was never judged by anyone (CX PR #24: 7 disputed, 0
    settled). Empty when nothing was answered."""
    try:
        with _transaction(engine) as c:
            row = c.execute(sql(
                "SELECT finalize_ctx FROM code_task_runs WHERE id=:id AND project_id=:p"),
                {"id": run_id, "p": project_id}).mappings().first()
    except Exception:  # noqa: BLE001 — no record, no answers to show
        _log_caught(logger)
        return ""
    stored = ((row or {}).get("finalize_ctx") or {}).get("review_comments") or []
    lines = []
    for entry in stored:
        if not isinstance(entry, dict) or not entry.get("response_kind"):
            continue
        where = (f"{entry.get('path')}:{entry.get('line')}" if entry.get("path")
                 and entry.get("line") else "(no single line)")
        lines.append(f"- [{review_comments.priority_label(str(entry.get('severity') or ''))}] "
                     f"{where} — {str(entry.get('body') or '')[:400]}\n"
                     f"    IMPLEMENTER: {str(entry.get('response_kind')).upper()} — "
                     f"{str(entry.get('response') or '')[:600]}")
    if not lines:
        return ""
    return ("THE IMPLEMENTER'S ANSWERS to the review comments. For each DISPUTED one, judge their "
            "reason against the code: if it holds, say in one line `ACCEPTED: <path:line> — why`; "
            "if it does not, raise the finding again in the FINDING format with your "
            "counter-argument. A P1 is accepted only on evidence in the code, never on the "
            "implementer's say-so. For each FIXED one, confirm it in the code.\n"
            + "\n".join(lines)[:8000] + "\n\n")


def _fix_round_change(prior_findings: str, fixed: dict, answers: str = "") -> str:
    """WHAT A FAILED SEAT RE-REVIEWS AFTER A FIX ROUND: its own findings, and only the files
    the fix changed. A HelpDesk-lite round re-reviewed the whole change from scratch (review_code
    5 and 3 min per round) to confirm two fixes. Unchanged code was already judged; the seat
    confirms its findings are resolved and looks for a NEW blocking defect in the fixed lines.
    Findings still block: a finding the fix did not resolve is raised again."""
    files = review_input.build(dict(fixed)).block()
    return ("FIX ROUND RE-REVIEW. You reviewed this change before and raised the findings below. "
            "The implementer has since changed ONLY the files inlined after them. (1) For each "
            "of your blocking findings, confirm it is resolved; raise it again, in the same "
            "FINDING format, if it is not. (2) Check these changed files for a NEW blocking "
            "defect. Do not re-review unchanged code: it was judged already.\n\n"
            "YOUR PREVIOUS FINDINGS:\n" + str(prior_findings or "")[:6000] + "\n\n"
            + (answers or "")
            + "WHAT THE FIX CHANGED:\n" + files)


def _repair_review_scope_blocks(engine, *, run_id: str, project_id: str, fctx: dict,
                                change: "review_input.ChangeUnderReview", baseline: dict,
                                enabled: bool = True) -> dict:
    """{seat: scoped change block} for a repair attempt's first panel; {} = every seat reviews
    the whole change, as before. One `code_task.repair_review_scope` event says which mode
    each scopable seat ran and why, so the shortcut is never invisible.

    The parent's own record decides: its review rounds (what each seat judged, and its
    findings), its failing-test output and the operator's fix note (which files a repair may
    legitimately touch). Any doubt — no parent, no recorded review, an unreadable record — is
    the FULL review."""
    parent_id = str((fctx or {}).get("parent_run_id") or "").strip()
    if not parent_id or not enabled or change.empty:
        return {}
    parent = None
    try:
        with _transaction(engine) as c:
            parent = load_code_task_run(c, parent_id)
    except Exception:  # noqa: BLE001 — no parent record: the full review, as before
        _log_caught(logger)
    pf = (parent or {}).get("finalize_ctx") or {}
    ps = (parent or {}).get("state") or {}
    if isinstance(pf, str):
        try:
            pf = json.loads(pf)
        except ValueError:
            pf = {}
    if isinstance(ps, str):
        try:
            ps = json.loads(ps)
        except ValueError:
            ps = {}
    rounds = [r for r in (pf.get("review_rounds") or []) if isinstance(r, dict)]
    # WHAT NAMES A FILE THE REPAIR MAY TOUCH: every seat's recorded findings, the comments the
    # parent's review left, the failing test output, and the operator's fix note.
    named = [str((s or {}).get("summary") or "") for r in rounds
             for s in (r.get("seats") or {}).values()]
    named += [f"{c.get('path') or ''} {c.get('body') or ''}"
              for c in (pf.get("review_comments") or []) if isinstance(c, dict)]
    if set(pf.get("failing_gates") or []) & {"written_tests_pass", "nothing_broke"}:
        named.append(str(ps.get("last_verify_output") or "")[-8000:])
    named += [str((h or {}).get("fix_note") or "")
              for h in ((fctx or {}).get("feedback_history") or []) if isinstance(h, dict)]
    blocks, record = {}, {}
    for seat in repair_review_scope.SCOPABLE_SEATS:
        p = repair_review_scope.plan(seat, parent_run_id=parent_id if parent else "",
                                     parent_rounds=rounds, current=change, baseline=baseline,
                                     named_text="\n".join(named), is_test=_is_test_path)
        record[seat] = p.record()
        if p.scoped:
            blocks[seat] = repair_review_scope.render(p)
    try:
        with _transaction(engine) as c:
            emit_event(c, project_id, "code_task.repair_review_scope", "boss", "code_task_run",
                       run_id, {"parent_run_id": parent_id, "seats": record})
    except Exception:  # noqa: BLE001 — the record must not stop the review
        _log_caught(logger)
    return blocks


def _render_change_for_review(files, *, max_total: int = 48000, max_per_file: int = 12000) -> str:
    """The exact files this task added/modified, inlined into the review prompt so a reviewer
    JUDGES the change instead of spending dozens of agentic turns (git diff / ls / cat / grep) to
    REDISCOVER what changed. That rediscovery — not model quality or retries — was the dominant
    review latency. Capped so a large diff can't blow the context; the reviewer can still open any
    file in the read-only tree for more depth."""
    live = (dict(files) if isinstance(files, dict)
            else {fc.path: fc.content for fc in files})
    return review_input.render(review_input.build(live), max_total=max_total,
                               max_per_file=max_per_file)


def _change_clause(change: str) -> str:
    """The 'here is the change, judge THIS' preamble shared by every reviewer."""
    if not change:
        return ""
    return ("THE CHANGE UNDER REVIEW — the exact files this task added or modified is inlined "
            "below. Judge THIS change directly; you already have it, so do NOT spend turns running "
            "git diff or hunting the repo. Open another file in the read-only tree ONLY if you "
            f"genuinely need more context.\n{change}\n\n"
            + review_input.reviewed_clause())


def _operator_review_block(engine, run_id: str, project_id: str) -> str:
    """WHAT THE OPERATOR ASKED FOR, handed to every reviewer as P1 checks.

    Steers and fix notes reached only the implementer's prompt; the reviewers judged the
    original brief and passed PRs the operator had sent back (KG P6a PR #25: "passed — no
    blocking defect found" while 4 of the operator's 8 points were not done). One source: the
    steers the run applied (`applied_guidance`) and the latest fix note (`feedback_history`)."""
    try:
        with _transaction(engine) as c:
            row = c.execute(sql(
                "SELECT finalize_ctx FROM code_task_runs WHERE id=:id AND project_id=:p"),
                {"id": run_id, "p": project_id}).mappings().first()
    except Exception:  # noqa: BLE001 — no record, nothing extra to judge
        _log_caught(logger)
        return ""
    fctx = (row or {}).get("finalize_ctx") or {}
    if isinstance(fctx, str):
        try:
            fctx = json.loads(fctx)
        except ValueError:
            return ""
    points = []
    note = str(((fctx.get("feedback_history") or [{}])[-1] or {}).get("fix_note") or "").strip()
    if note:
        points.append(note)
    for item in fctx.get("applied_guidance") or []:
        text = str((item or {}).get("text") or "").strip() if isinstance(item, dict) else ""
        if text and text not in points:
            points.append(text)
    if not points:
        return ""
    body = "\n\n".join(f"[{i}] {p}" for i, p in enumerate(points, start=1))[:12000]
    return ("THE FEEDBACK THIS ATTEMPT MUST ADDRESS (the operator's review and any repair note "
            "on earlier attempts). Every point below is a P1: check each "
            "one against the code itself, and for any point that is not done, emit a FINDING: "
            "MAJOR (or BLOCKER) that names the point and where it is missing. A change that "
            "leaves one undone is REFUTED, whatever else it gets right.\n" + body + "\n\n")


def _review_prompt(title, criteria, *, instructions="", ui=False, change="", operator="",
                   ctx=True):
    # `ctx=False` for a merged-panel section (review_panel.combined_prompt): the task context
    # and the shared change are in the prompt ONCE, not once per seat.
    return (
        (_ctx_block(title, instructions, criteria) if ctx else "")
        + (operator or "")
        + "You are an adversarial code reviewer with FRESH context — you did NOT write this code or "
        "these tests. Try HARD to break the change: read the implementation and the tests and look "
        "for a REAL correctness or edge-case failure (invalid inputs, empty/boundary values, case "
        "sensitivity, error handling, off-by-one).\n\n"
        + _change_clause(change)
        + ci_gate.review_clause_for(change)
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
        "The pull request shows these as priorities: BLOCKER and MAJOR are P1 (must be fixed "
        "before merge), MINOR is P2 (should be fixed), NIT is P3 (optional).\n"
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
        _log_caught(logger)
        logger.exception("could not check for an existing follow-up on %s", parent_run_id)
        return False


def _findings_to_pass_two(engine, project_id: str, run_id: str, workflow_run_id: str,
                          findings: list[dict]) -> tuple[bool, str]:
    """(follow-up still wanted, why not). When this run's workflow will run its More tests pass
    on this branch next (workflow_capability_runner.pass_two_pending), the findings are put on
    this run (`pass_two_findings`, which that pass's brief carries) and no follow-up starts."""
    from .workflow_capability_runner import PASS_TWO_FINDINGS, pass_two_pending

    try:
        with _transaction(engine) as c:
            if not pass_two_pending(c, workflow_run_id):
                return True, ""
            c.execute(sql(
                "UPDATE code_task_runs SET finalize_ctx = COALESCE(finalize_ctx, '{}'::jsonb) "
                "|| jsonb_build_object(CAST(:k AS text), CAST(:f AS jsonb)) WHERE id = :id"),
                {"k": PASS_TWO_FINDINGS, "f": json.dumps(list(findings or [])[:20]),
                 "id": run_id})
            emit_event(c, project_id, "code_task.deferred_findings_to_pass_two", "boss",
                       "code_task_run", run_id,
                       {"workflow_run_id": workflow_run_id, "findings": list(findings)[:20]})
    except Exception:  # noqa: BLE001 — unknown: the ordinary follow-up (still one owner)
        _log_caught(logger)
        return True, ""
    return False, "handed to the workflow's More tests pass, which works on this branch next"


def _spawn_deferred_findings_task(
    engine, settings, jobs, *, project_id, task_id, spec, base_branch,
    findings: list[dict], parent_run_id: str, parent_title: str,
    parent_root: Optional[str] = None, parent_origin: Optional[dict] = None,
    checks_failing: Optional[dict] = None, parent_workflow_run_id: Optional[str] = None,
):
    """Queue sub-threshold review findings as their own code task on the shipped branch.

    Deferring a finding must never mean losing it: this is what makes "ship the phase over a
    cosmetic defect" safe, because the defect leaves as tracked work rather than as a note in a
    receipt nobody reads. Best-effort by construction — a PR that is already open must not be
    failed by bookkeeping.

    ``checks_failing`` (checks_failing.collect): the parent's PR opened with checks still
    failing after its repair rounds. The task is then those checks' fix — its failing gates
    and open findings come first in ``findings``, security first — and says so.
    """
    lines = [f"- [{f['severity'].upper()}] {f['text']}"
             for f in (findings or []) if f.get("text")]
    if not lines:
        return None
    from .code_task_service import start_code_task
    limit = 40 if checks_failing else 20
    body = "\n".join(lines[:limit])
    if checks_failing:
        from .checks_failing import headline

        title_lead = "Follow-up: checks failing"
        why = ("The parent change's pull request opened with these checks still FAILING after "
               f"its repair rounds ({headline(checks_failing)}), so it is a draft and is not "
               "merged until they pass or a person decides. Make every failing check pass and "
               "fix every open finding below on top of the shipped branch, security first.")
        heading = "FAILING CHECKS AND OPEN FINDINGS"
    else:
        title_lead = "Follow-up: deferred review findings"
        why = ("An adversarial review of the parent change graded these findings BELOW the "
               "blocking threshold, so the change shipped and they were deferred to this task. "
               "Fix them on top of the shipped branch.")
        heading = "DEFERRED FINDINGS"
    with _transaction(engine) as c:
        return start_code_task(
            c, project_id=project_id, task_id=task_id,
            title=f"{title_lead} — {parent_title}"[:160],
            instructions=(
                f"{why}\n\n"
                f"{heading} (from run {parent_run_id}):\n{body}\n\n"
                "Fix ONLY these. Do not refactor beyond them and do not weaken existing tests "
                "or checks."),
            acceptance_criteria=[f"{'Fixed' if checks_failing else 'Deferred finding addressed'}: "
                                 f"{ln[:180]}" for ln in lines[:limit]],
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
                          **({"origin": parent_origin} if parent_origin else {}),
                          # AND THE WORKFLOW RUN IT BELONGS TO. Without it the follow-up's merge
                          # approval was invisible on the run: helpdesk-ai's run showed "More
                          # tests: waiting" while its approval sat elsewhere, and the operator
                          # asked "how does the user know?" (2026-11-06). Retries carry it too.
                          **({"workflow_run_id": parent_workflow_run_id}
                             if parent_workflow_run_id else {})},
            jobs=jobs, profile=spec.get("profile"),
            # BOSS ITSELF continues this lineage: it acts under the person and grant the lineage
            # was started under (a standing grant, or the workflow run's), never the worker key's.
            continuation=True,
        )


# The verdict grammar lives in review_verdict.py so the decision eval (evals/task_rigor,
# family `verdict`) can measure it without importing this module. The old private names stay
# importable: pr_review_again and verifier_meta_eval read them from here.
from .review_verdict import (  # noqa: E402 — re-exported under their old names
    aggregate_panel as _aggregate_panel,
    parse_consistency as _parse_consistency_impl,
    parse_security as _parse_security_impl,
    parse_verdict as _parse_verdict,
    plan_verdict as _plan_verdict,
    refutation_is_substantiated as _refutation_is_substantiated,
)


def _security_prompt(title, *, instructions="", criteria=(), change="", ctx=True):
    return (
        (_ctx_block(title, instructions, criteria) if ctx else "")
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


_parse_security = _parse_security_impl


def _strengthen_tests_prompt(criteria, planted: str, round_i: int) -> str:
    """The test author's fix round for a weak suite (`tests_strong_enough`): the probe planted a
    one-line bug in the implementation and every test still passed."""
    crit = "\n".join(f"- {c}" for c in (criteria or [])) or "- (none specified)"
    return (
        f"FIX ROUND {round_i} — THE TESTS ARE TOO WEAK. A mutation probe changed ONE line of the "
        "implementation so it no longer meets the spec, and every test still passed. The bug it "
        "planted (already reverted):\n"
        f"{(planted or '(the probe did not say)')[:1500]}\n\n"
        f"ACCEPTANCE CRITERIA:\n{crit}\n\n"
        "Strengthen the tests so a change like that FAILS them: assert the behaviour the criteria "
        "require (values, status codes, boundaries), not only that something ran. Edit ONLY test "
        "files, never the implementation; keep every existing assertion; the suite must still pass "
        "on the current code. End with one line naming what you strengthened.")


def _qa_static_fix_prompt(findings) -> str:
    """QA's one turn on its own tests' typecheck/lint errors, before they are frozen."""
    rows = "\n".join(f"- {f}" for f in list(findings or [])[:60])
    return (
        "YOUR TESTS FAIL THE APP'S OWN TYPECHECK OR LINT. BOSS ran the app's `npm run typecheck` "
        "and `npm run lint` on the tree and these errors are in the test files you wrote:\n"
        f"{rows}\n\n"
        "Fix exactly these lines in the TEST files (types, unused variables, syntax, lint rules), "
        "keeping what every test asserts. Imports of implementation modules that do not exist yet "
        "are fine: the implementer writes them next. Do not edit any non-test file and do not add "
        "lint or type suppressions. After this turn the tests are frozen: nobody else may edit "
        "them. End with one line naming what you fixed.")


def _template_tests_home(spec: dict, fctx: dict, instructions: str) -> Optional[str]:
    """The app directory a template's tests belong in: the named app, else the folder the brief
    names ("in the `helpdesk-ai/` folder"), else None (BOSS then leaves the copy to the agent)."""
    named = verification_target.official_app_id(spec, fctx)
    if named:
        return named
    m = re.search(r"\bin (?:the )?`?([a-z0-9][a-z0-9._-]{0,60})/`?\s+(?:folder|directory)",
                  instructions or "", re.IGNORECASE)
    return m.group(1) if m else None


def place_template_tests(root: Path, home: Optional[str]) -> list[str]:
    """Copy the applied template's own test files into `home`/tests/, BEFORE any agent runs.

    BOSS holds them on disk (templates.run_ref). The brief used to ask the agent to copy them
    "as they are", and a HelpDesk AI build spent a 407-second stretch re-typing and diffing them
    against the reference. A file already in place (a repair, a resume) is left as it is.
    Returns the repo-relative paths placed. Never raises: a copy that fails leaves the old
    instruction to the agent."""
    from ..templates import run_ref

    if not home:
        return []
    if run_ref.current().get(run_ref.KIND_ENV) == "connector":
        # A CONNECTOR IS PRE-TESTED in its own test tier (templates.gate.connector_smoke): its
        # suite is not copied into the app, not frozen, and not run by the build.
        return []
    stack_dir = run_ref.current().get(run_ref.STACK_DIR_ENV)
    if not stack_dir:
        return []
    src = Path(stack_dir)
    placed: list[str] = []
    try:
        for f in sorted((src / "tests").rglob("*")):
            if not f.is_file():
                continue
            rel = f"{home.strip('/')}/{f.relative_to(src).as_posix()}"
            dest = Path(root) / rel
            if dest.exists():
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            for parent in [dest.parent, *dest.parent.parents]:
                if parent == Path(root) or Path(root) not in parent.parents:
                    break
                with contextlib.suppress(OSError):
                    os.chmod(parent, 0o2777)   # the run's uid writes beside them
            dest.write_bytes(f.read_bytes())
            os.chmod(dest, 0o666)
            placed.append(rel)
    except OSError:
        _log_caught(logger)
    return placed


def _placed_tests_note(placed: list[str]) -> str:
    if not placed:
        return ""
    return ("\n\nTHE TEMPLATE'S TESTS ARE ALREADY IN PLACE. BOSS copied the applied template's "
            "test files into the app, exactly as the template ships them:\n"
            + "\n".join(f"  - {p}" for p in placed[:60])
            + "\nThey are frozen: do NOT re-create, re-type, edit, move or diff them against "
            "$BOSS_TEMPLATE_STACK_DIR. Build the app so they pass.")


def _build_fan_out(engine, project_id, settings) -> int:
    """Parallel sub-agents for a writing stage (project_settings.build_parallelism), 0 for none.

    Never raises: a settings read that fails builds with one agent, exactly as before.
    """
    try:
        from .project_settings import build_parallelism

        with _transaction(engine) as c:
            n = int(build_parallelism(c, project_id, settings) or 0)
        # No more parts than the model limiter lets run AT ONCE (model_rate_limiter, one lane
        # per provider): a sixth part behind a limit of three only queues, it is not parallel.
        n = min(n, max(1, int(getattr(settings, "model_max_concurrency", 3) or 3)))
        return n if n > 1 else 0
    except Exception:  # noqa: BLE001 — speed is never worth failing a build over
        _log_caught(logger)
        return 0


def _record_effort(engine, project_id, run_id, settings) -> None:
    """EFFORT PER STAGE (code_agent.stage_effort): publish the project's writing effort to this
    thread's LLM context, where every CLI step reads its --effort, and record each stage's level
    on the run (state.effort) and as code_task.effort_chosen. Never raises."""
    from .code_agent import WRITING_EFFORT_DEFAULT, effort_map, stage_effort

    writing = WRITING_EFFORT_DEFAULT
    try:
        from .project_settings import effective

        with _transaction(engine) as c:
            writing = str(effective(c, project_id, settings, "build_effort_writing")
                          or WRITING_EFFORT_DEFAULT)
    except Exception:  # noqa: BLE001 — unreadable settings write at the default
        _log_caught(logger)
    writing = stage_effort("implement", writing) or WRITING_EFFORT_DEFAULT
    set_llm_context(effort_writing=writing)
    # THE TEST BUDGET (test_budget), read once with the effort: the QA prompt states it and
    # BOSS counts against it after the test author's stage.
    try:
        from .project_settings import effective

        with _transaction(engine) as c:
            budget = int(effective(c, project_id, settings, "build_test_budget")
                         or TEST_BUDGET_DEFAULT)
    except Exception:  # noqa: BLE001 — the default budget
        _log_caught(logger)
        budget = TEST_BUDGET_DEFAULT
    set_llm_context(test_budget=max(1, budget))
    try:
        efforts = effort_map(writing)
        with _transaction(engine) as c:
            c.execute(sql(
                "UPDATE code_task_runs SET state=jsonb_set(COALESCE(state,'{}'::jsonb), "
                "'{effort}', CAST(:e AS JSONB)), updated_at=now() WHERE id=:id"),
                {"e": json.dumps(efforts), "id": run_id})
            emit_event(c, project_id, "code_task.effort_chosen", "boss", "code_task_run",
                       run_id, {"writing": writing, "stages": efforts})
    except Exception:  # noqa: BLE001 — the record never fails the build
        _log_caught(logger)


#: Failing test names in a test run's output (node:test, vitest/jest, pytest, JUnit-ish).
_FAILED_TEST_RE = re.compile(
    r"^\s*(?:not ok \d+ - (?P<tap>.+)|(?:✗|×|✖|FAIL(?:ED)?)\s+(?P<name>\S.*?))\s*$", re.MULTILINE)


def failing_tests(output: str, limit: int = 20) -> list[str]:
    """The failing tests a test run printed, de-duplicated, at most `limit`."""
    seen: list[str] = []
    for m in _FAILED_TEST_RE.finditer(output or ""):
        name = (m.group("tap") or m.group("name") or "").strip()[:200]
        if name and name not in seen:
            seen.append(name)
        if len(seen) >= limit:
            break
    return seen


def _base_commit_sha(root: Path, base_branch: str) -> Optional[str]:
    """The base commit a build started from, as a sha — None when it cannot be resolved (no
    git, no such ref): a classification that cannot run never blocks the build."""
    from . import owned_process

    try:
        out = owned_process.boss_run(
            ["git", "-C", str(root), "rev-parse", str(base_branch)],
            capture_output=True, text=True, timeout=30)
        if out.returncode == 0:
            return out.stdout.strip() or None
    except Exception:  # noqa: BLE001
        _log_caught(logger)
    return None


def _test_cmd_without_paths(test_cmd: str) -> list[str]:
    """``test_cmd`` (the gate's already-expanded command) with every path-like / {tests}
    argument dropped, leaving just the runner and its flags — so a base rerun can append the
    failing ids in their place instead of the whole suite's file list."""
    import shlex

    from .verify_manifest import TESTS_PLACEHOLDER

    tokens = shlex.split(test_cmd or "")
    return [t for t in tokens if t != TESTS_PLACEHOLDER and "/" not in t]


def _rerun_test_cmd_on_base(root: Path, base_sha: str, failing_ids: Sequence[str],
                            test_cmd: str, cwd_rel: str = ".") -> str:
    """Only the currently-failing test ids, run once more against the base commit — in a
    worktree created under THIS run's own sandbox directory, dispatched through the same
    sandboxed runner (owned_process, the run's identity, no shell) the normal verify gate
    uses. The evidence that decides whether a failure is caused by this change or was already
    broken before it. Empty string ("unknown") on any failure to run it."""
    import shutil

    from . import owned_process, sandbox_identity

    if not failing_ids:
        return ""
    identity = sandbox_identity.current()
    worktree_dir = (identity.workspace("boss-baseline-") if identity is not None
                    else Path(tempfile.mkdtemp(prefix="boss-baseline-")))
    try:
        # AS THE RUN'S OWN SANDBOX UID (owned_process.run), never boss_run: after the agent has
        # worked, files in the checkout's .git belong to the run's uid/group (e.g. AUTO_MERGE,
        # 0660), and the control plane's git then says "not a git repository". Every base rerun
        # came back empty and L1 labelled every failure "caused" (boss-1, 2026-10-29).
        added = owned_process.run(
            ["git", "-C", str(root), "worktree", "add", "--detach", str(worktree_dir), base_sha],
            capture_output=True, text=True, timeout=120)
        if added.returncode != 0:
            logger.warning("baseline rerun skipped: git worktree add failed (%s): %s",
                           added.returncode, (added.stderr or "")[:300])
            return ""
        run_cwd = (worktree_dir / cwd_rel) if cwd_rel not in (".", "", None) else worktree_dir
        argv = _test_cmd_without_paths(test_cmd) + sorted(failing_ids)
        result = owned_process.run(argv, cwd=str(run_cwd), capture_output=True, text=True,
                                   timeout=900)
        return (result.stdout or "") + "\n" + (result.stderr or "")
    except Exception:  # noqa: BLE001 — a base rerun that cannot run never blocks the build
        _log_caught(logger)
        return ""
    finally:
        try:
            owned_process.run(
                ["git", "-C", str(root), "worktree", "remove", "--force", str(worktree_dir)],
                capture_output=True, text=True, timeout=60)
        except Exception:  # noqa: BLE001
            _log_caught(logger)
        shutil.rmtree(str(worktree_dir), ignore_errors=True)


def _baseline_cache_ddl(c) -> None:
    c.execute(sql(
        "CREATE TABLE IF NOT EXISTS code_task_baseline_cache_v2 ("
        "project_id TEXT NOT NULL, base_sha TEXT NOT NULL, test_id TEXT NOT NULL, "
        "outcome TEXT NOT NULL, updated_at TIMESTAMPTZ NOT NULL DEFAULT now(), "
        "PRIMARY KEY (project_id, base_sha, test_id))"))


def _load_baseline_cache(engine, project_id: str, base_sha: str) -> dict:
    """Every (test_id -> outcome) already known for this project's base commit, from the
    DB — so the cache survives a process restart and never leaks across projects or base
    commits (L1: baseline-aware verification, acceptance criterion 5)."""
    try:
        with _transaction(engine) as c:
            _baseline_cache_ddl(c)
            rows = c.execute(sql(
                "SELECT test_id, outcome FROM code_task_baseline_cache_v2 "
                "WHERE project_id = :p AND base_sha = :b"),
                {"p": project_id, "b": base_sha}).all()
        return {row[0]: row[1] for row in rows}
    except Exception:  # noqa: BLE001 — a cache that cannot load never blocks the build
        _log_caught(logger)
        return {}


def _save_baseline_cache(engine, project_id: str, base_sha: str, fresh: dict) -> None:
    if not fresh:
        return
    try:
        with _transaction(engine) as c:
            _baseline_cache_ddl(c)
            for test_id, outcome in fresh.items():
                c.execute(sql(
                    "INSERT INTO code_task_baseline_cache_v2 (project_id, base_sha, test_id, outcome) "
                    "VALUES (:p, :b, :t, :o) ON CONFLICT (project_id, base_sha, test_id) "
                    "DO UPDATE SET outcome = :o, updated_at = now()"),
                    {"p": project_id, "b": base_sha, "t": test_id, "o": outcome})
    except Exception:  # noqa: BLE001 — a cache that cannot save never blocks the build
        _log_caught(logger)


def _environment_signature_ddl(c) -> None:
    c.execute(sql(
        "CREATE TABLE IF NOT EXISTS code_task_environment_signatures ("
        "project_id TEXT NOT NULL, signature TEXT NOT NULL, seen_count INT NOT NULL DEFAULT 0, "
        "last_seen TIMESTAMPTZ NOT NULL DEFAULT now(), "
        "PRIMARY KEY (project_id, signature))"))


def _record_environment_signatures(engine, project_id: str, run_id: str,
                                   record: "baseline_verify.BaselineRecord") -> None:
    """Every `environment`-classified failure's signature is counted per project — the same
    infra problem (a missing driver, a collection error) recurring across different runs and
    test ids is a platform gap, not a one-off, so a count of 2+ is raised as its own event
    instead of staying buried inside each run's own `baseline_classified` record."""
    if engine is None or not record.environment:
        return
    try:
        for classified in record.environment:
            signature = baseline_verify.environment_signature(classified)
            with _transaction(engine) as c:
                _environment_signature_ddl(c)
                count = c.execute(sql(
                    "INSERT INTO code_task_environment_signatures "
                    "(project_id, signature, seen_count, last_seen) "
                    "VALUES (:p, :s, 1, now()) "
                    "ON CONFLICT (project_id, signature) DO UPDATE SET "
                    "seen_count = code_task_environment_signatures.seen_count + 1, "
                    "last_seen = now() "
                    "RETURNING seen_count"),
                    {"p": project_id, "s": signature}).scalar()
                if count and count >= 2:
                    emit_event(c, project_id, "code_task.environment_signature_recurring", "boss",
                               "code_task_run", run_id,
                               {"signature": signature, "seen_count": int(count)})
    except Exception:  # noqa: BLE001 — a signature count that cannot be saved never blocks a build
        _log_caught(logger)


def classify_against_base(*, project_id: str, root: Path, base_branch: str, test_cmd: str,
                           fail_output: str, cwd_rel: str = ".",
                           cache: Optional[dict] = None,
                           rerun: Optional[Callable[[str, Sequence[str]], str]] = None,
                           engine: Any = None,
                           ) -> Optional["baseline_verify.BaselineRecord"]:
    """L1 baseline-aware verification, wired to the real verify gate: the failing test ids the
    gate just saw, reclassified against the commit the build started from, so a repair round is
    never spent on a failure the change did not cause. `cache`/`rerun` are injectable for tests;
    production reruns only the failing ids through the sandbox runner and caches outcomes in
    `code_task_baseline_cache_v2`, keyed by (project_id, base_sha, test_id)."""
    failing_ids = baseline_verify.extract_test_ids(fail_output)
    if not failing_ids:
        return None
    base_sha = _base_commit_sha(root, base_branch)
    if not base_sha:
        return None
    rerun = rerun or (lambda sha, ids: _rerun_test_cmd_on_base(root, sha, ids, test_cmd, cwd_rel))
    persist = cache is None and engine is not None
    bucket = cache if cache is not None else (
        _load_baseline_cache(engine, project_id, base_sha) if engine is not None else {})
    uncached = [tid for tid in failing_ids if tid not in bucket]
    if uncached:
        base_output = rerun(base_sha, uncached)
        fresh = baseline_verify.base_outcomes_from_output(uncached, base_output)
        known = {tid: o for tid, o in fresh.items() if o in ("failed", "passed")}
        bucket.update(known)
        if persist and known:
            _save_baseline_cache(engine, project_id, base_sha, known)
    base_outcomes = {tid: bucket[tid] for tid in failing_ids if tid in bucket}
    return baseline_verify.classify(
        failing_ids, fail_output, base_outcomes,
        classify_environment=baseline_verify.default_classify_environment)


#: The slice of the implement budget kept for the same-model warm fix (pre-mortem finding 11).
WARM_FIX_RESERVE_SECONDS = 360


def implement_work_deadline(deadline: Optional[float], budget_s: int) -> Optional[float]:
    """When the attempts BEFORE the warm fix must stop: WARM_FIX_RESERVE_SECONDS before the
    implement deadline, so the warm fix always has its slice. A budget too small to split (under
    twice the reserve) is not split; no budget, no deadline."""
    if deadline is None or not budget_s:
        return deadline
    if budget_s < 2 * WARM_FIX_RESERVE_SECONDS:
        return deadline
    return deadline - WARM_FIX_RESERVE_SECONDS


def escalation_route_record(route: Optional[dict], last_result: Optional[dict]) -> dict:
    """{engine, model} for an escalation event: the route's, and when the route leaves the
    model to the engine's default (None), the model the last attempt reported running on."""
    route = dict(route or {})
    model = route.get("model") or (last_result or {}).get("model") or None
    return {"engine": route.get("engine") or None,
            "model": model or "(the engine's default model)"}


def _implement_budget_seconds(engine, project_id, settings) -> int:
    """build_implement_budget_minutes in seconds; 0 = no budget. Never raises."""
    try:
        from .project_settings import effective

        with _transaction(engine) as c:
            minutes = effective(c, project_id, settings, "build_implement_budget_minutes")
        minutes = int(25 if minutes is None else minutes)
    except Exception:  # noqa: BLE001 — the default budget
        _log_caught(logger)
        minutes = 25
    return max(0, minutes) * 60


def _record_split_off(engine, project_id, run_id, settings, stage: str) -> None:
    """Record on the stage that it runs as ONE session and why (build_streams.split_off), the
    same place and event a split records its stream count, so the choice is never invisible.
    Never raises."""
    try:
        from . import build_streams as _streams

        with _transaction(engine) as c:
            why = _streams.split_off(c, project_id, settings)
            if not why:
                return
            plan = {"n": 1, "why": why, "split": "off"}
            c.execute(sql(
                "UPDATE code_task_runs SET state=jsonb_set(COALESCE(state,'{}'::jsonb), "
                "'{streams}', COALESCE(state->'streams','{}'::jsonb) "
                "|| jsonb_build_object(CAST(:st AS text), CAST(:p AS JSONB))), "
                "updated_at=now() WHERE id=:id"),
                {"st": stage, "p": json.dumps(plan), "id": run_id})
            emit_event(c, project_id, "code_task.streams_chosen", "boss", "code_task_run",
                       run_id, {"stage": stage, **plan})
    except Exception:  # noqa: BLE001 — the record never fails the build
        _log_caught(logger)


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


def _plan_self_check_section() -> str:
    """The plan review as a SELF-CHECK section of the session that wrote the plan
    (BOSSAPP-1003, audit 997 §7.6). review_plan was its own fresh CLI session that re-read the
    repository to judge a plan it was handed inline — 88 calls, $42.80/month, nine times the
    plan stage itself. The session that just read the repository re-reads its own plan instead,
    under the SAME contract: the ROUTING: NOT_A_CODE_TASK check and a final PLAN: OK / REVISE
    marker, parsed by the same `review_verdict.plan_verdict`. A session that skips the section
    falls back to the separate review session (`_review_plan_prompt`), so the check never
    silently disappears. The human approve-plan gate is untouched."""
    return (
        "=== PLAN REVIEW ===\nNow change hats: review the plan you just wrote as a design "
        "reviewer would, against the task and the spec/standards above.\n"
        "FIRST decide whether this task ASKS FOR CODE at all. It is NOT a coding task ONLY "
        "when its deliverable is inherently a document, research, or a written specification — "
        "e.g. 'write a PRD', 'research the options', 'produce a discovery report' — where the "
        "task does not call for creating or modifying source code. A task that asks you to add "
        "or change code IS a coding task, EVEN IF the new code is a standalone/new module or "
        "the repository already contains unrelated or messy code. If and ONLY if the "
        "deliverable is not code at all, this section must be ONLY this single line:\n"
        "  ROUTING: NOT_A_CODE_TASK — <one-line reason>\n"
        "Otherwise, name the plan's gaps, missing edge cases, and risky choices — and fix the "
        "PLAN section's text for anything material you find, so the plan above is the plan to "
        "build. End this section with EXACTLY one final line:\n"
        "  PLAN: OK\n  PLAN: REVISE\n"
    )


def _plan_prompt(title, spec):
    return (
        "You are a software architect. Given the spec, write a short technical plan: the files to "
        "add/change, the function signatures, and the key edge cases to handle. Do NOT edit any "
        "files. If the app needs a relational database, end the plan with the line "
        "`database: postgres` (BOSS then gives every build its own PostgreSQL database, "
        "DATABASE_URL in the environment); otherwise end with `database: none`."
        f"\n\nTASK: {title}\n\nSPEC:\n{spec}\n\n"
        "Write the plan as a section starting with the heading line `=== PLAN ===`, then add "
        "this second section:\n\n" + _plan_self_check_section()
    )


_DESIGN_HEADS = (("standards", "STANDARDS"), ("spec", "SPEC"), ("plan", "PLAN"),
                 ("plan_review", "PLAN REVIEW"), ("packages", "WORK PACKAGES"))


def _design_prompt(title, instructions, criteria):
    """Standards, spec and plan from ONE read of the repository, in three marked sections."""
    return (
        _ctx_block(title, instructions, criteria)
        + "You are the design lead for this change. Read this repository ONCE, then write five "
        "sections, each starting with its own heading line exactly as shown. Do NOT edit any "
        "files.\n\n"
        "=== STANDARDS ===\nA short bulleted list of the coding standards a new change here "
        "should follow: style, naming, error handling, test conventions, and file structure.\n\n"
        "=== SPEC ===\nA clear, testable specification: a one-paragraph summary, a change list "
        "(added / changed / removed), and a clarified, testable list of acceptance criteria. "
        "Resolve any ambiguity explicitly.\n\n"
        "=== PLAN ===\nA short technical plan: the files to add or change, the function "
        "signatures, and the key edge cases to handle. If the app needs a relational database, "
        "end the plan with the line `database: postgres` (BOSS then gives every build its own "
        "PostgreSQL database, DATABASE_URL in the environment); otherwise end with "
        "`database: none`.\n\n"
        + _plan_self_check_section()
        + _parallel_build.design_prompt_section()
    )


def _split_design(text: str) -> dict:
    """The three sections of a joint design answer; a missing one is empty (and re-run)."""
    out = {key: "" for key, _ in _DESIGN_HEADS}
    marks = []
    for key, head in _DESIGN_HEADS:
        m = re.search(rf"^\s*=+\s*{head}\s*=+\s*$", text or "", re.MULTILINE)
        if m:
            marks.append((m.start(), m.end(), key))
    marks.sort()
    for i, (_start, end, key) in enumerate(marks):
        stop = marks[i + 1][0] if i + 1 < len(marks) else len(text)
        out[key] = text[end:stop].strip()
    return out


def _design_from_approved_prd(engine, project_id, run_id, fctx, *, title, instructions,
                              criteria, root, route) -> Optional[dict]:
    """The design from the APPROVED PRD this build came from, or None to run the full design.

    One read-only session under the plan's route, capped at PRD_DESIGN_MAX_TURNS, maps the PRD's
    solution onto the repo. Records `code_task.design_from_prd` (the short path ran) or
    `code_task.design_from_prd_declined` (why the full design runs instead) — the latter only
    when there WAS an approved PRD, so ordinary builds are not cluttered. Never raises."""
    try:
        with _transaction(engine) as c:
            prd = approved_prd_design.find_approved_prd(c, project_id, fctx or {})
        if prd is None:
            return None
        solution = approved_prd_design.solution_section(prd["markdown"])

        def _declined(why: str) -> None:
            with _transaction(engine) as c:
                emit_event(c, project_id, "code_task.design_from_prd_declined", "boss",
                           "code_task_run", run_id,
                           {"decision_id": prd["decision_id"], "why": why[:400],
                            "path": "full"})

        if not solution:
            _declined("the approved PRD has no solution / proposed-design section")
            return None
        set_llm_context(stage="design")
        agent, model = route("plan")
        started = time.monotonic()
        answer = agent.run_role(
            approved_prd_design.prompt(_ctx_block(title, instructions, criteria), solution),
            root, read_only=True, model=model,
            max_turns=approved_prd_design.PRD_DESIGN_MAX_TURNS)
        design, why = approved_prd_design.design_from_answer(
            prd, solution, str(answer.get("summary") or ""))
        if design is None:
            _declined(why)
            return None
        design["cost_usd_micros"] = int(answer.get("cost_usd_micros") or 0)
        with _transaction(engine) as c:
            emit_event(c, project_id, "code_task.design_from_prd", "boss", "code_task_run",
                       run_id, {"decision_id": prd["decision_id"], "path": "approved_prd",
                                "seconds": round(time.monotonic() - started, 1),
                                "solution_chars": len(solution),
                                "packages": len(_parallel_build.parse_packages(
                                    design.get("packages")))})
        return design
    except Exception:  # noqa: BLE001 — the full design is always there to fall back to
        _log_caught(logger)
        return None


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


def _consistency_prompt(title, criteria, design, *, instructions="", change="", ctx=True):
    spec = (design or {}).get("spec", "")
    extra = f"\n\nAGREED SPEC:\n{spec}" if spec else ""
    return (
        (_ctx_block(title, instructions, criteria) if ctx else "")
        + "You are a reviewer checking internal consistency. Read the implementation and the tests "
        "and confirm they agree with the task and its acceptance criteria: every criterion is "
        "implemented and tested, names and behaviour match the spec, and nothing contradicts. Do "
        "NOT edit any files. End with EXACTLY one final line:\n"
        "  CONSISTENT: YES\n  CONSISTENT: NO\n\n"
        + _change_clause(change)
        + extra
    )


_parse_consistency = _parse_consistency_impl


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


def _guest_billed(usage, guest_spec: dict):
    """A guest run's usage with who paid stamped on it: a claude_code guest on an Anthropic API
    key is money (included_spend.py), whatever its engine name would suggest."""
    if guest_spec.get("engine") == "claude_code" and guest_spec.get("anthropic_api_key"):
        from .included_spend import BILLING_METERED

        return {**(usage or {}), "billing": BILLING_METERED}
    return usage


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
        # The login or the console's Anthropic API key — one resolution (claude_credential),
        # never `provider="anthropic"`, which cannot tell the two apart.
        from .claude_credential import resolve_claude_credential

        cred = resolve_claude_credential(settings, connection=connection, project_id=project_id)
        return {"engine": "claude_code", **cred.guest_fields()}
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
        # sat at "Needs you · plan" for weeks on an existing install. No deadline: it waits for
        # the person, and every surface counts it (decisions.expire_overdue keeps it so).
        c.execute(sql("UPDATE decisions SET deadline_at = NULL WHERE id = :d"), {"d": dec_id})
        c.execute(sql("UPDATE code_task_runs SET status='blocked', phase='awaiting_plan_approval', "
                       "decision_id=:d, updated_at=now() WHERE id=:id"),
                  {"d": dec_id, "id": run_id})
        on_status_written(c, run_id)
    return {"status": "blocked", "done": True, "awaiting_plan_approval": True, "decision_id": dec_id}


def _provision_build_services(engine, settings: Settings, run_id: str, project_id: str,
                              pinned: Any, *, use_microvm: bool = False, spec: Any = None,
                              title: str = "", root: Any = None,
                              decided: Optional[dict[str, Any]] = None) -> Any:
    """The run's build-services lease, or None when it needs none (build_services.decide: the
    brief, a template, the plan or the app's own code says PostgreSQL; or the project's
    Always; never under Never). The decision is kept on the run (`state.build_database`) so the
    build page and the deploy plan say "Database: Postgres for this build", and why."""
    from . import build_services
    from .project_settings import effective as _effective_setting

    manifest = getattr(pinned, "manifest", None)
    declared = tuple(getattr(manifest, "services", ()) or ())
    try:
        with _transaction(engine) as c:
            setting = _effective_setting(c, project_id, settings, build_services.SETTING_KEY)
    except Exception:  # noqa: BLE001 — the repo's declaration still stands
        _log_caught(logger)
        setting = None
    design = None
    try:
        with _transaction(engine) as c:
            row = c.execute(sql("SELECT state FROM code_task_runs WHERE id = :id"), {"id": run_id}).first()
        state = (row[0] if row else None) or {}
        state = json.loads(state) if isinstance(state, str) else state
        design = (state or {}).get("design") if isinstance(state, dict) else None
    except Exception:  # noqa: BLE001 — no plan yet: the other signals decide
        _log_caught(logger)
        design = None
    decision = build_services.decide(declared, setting, spec=spec, title=title, root=root, design=design)
    wanted = decision["services"]
    if decided is not None:       # the caller keeps it in the run's final state too
        decided.update({"postgres": "postgres" in wanted, "mode": decision["mode"],
                        "why": decision["why"][:5]})
    try:
        with _transaction(engine) as c:
            c.execute(sql("UPDATE code_task_runs SET state = COALESCE(state, '{}'::jsonb) || "
                          "jsonb_build_object('build_database', CAST(:d AS jsonb)) WHERE id = :id"),
                      {"d": json.dumps({"postgres": "postgres" in wanted, "mode": decision["mode"],
                                        "why": decision["why"][:5]}), "id": run_id})
    except Exception:  # noqa: BLE001 — the record is for display; the build goes on
        _log_caught(logger)
        logger.debug("build_database decision not recorded", exc_info=True)
    if "postgres" not in wanted:
        return None
    if use_microvm:
        raise build_services.BuildServicesUnavailable(
            "this run executes in a microVM, which cannot reach the build-services server. "
            "Run it on the container sandbox, or remove services: [postgres]")
    if str(getattr(settings, "sandbox_command_runtime", "subprocess")).lower() == "docker":
        raise build_services.BuildServicesUnavailable(
            "the docker command runtime runs build commands with no network, so they cannot "
            "reach the run's database. Use the subprocess runtime, or remove services: [postgres]")
    install = ""
    try:
        from .resource_ledger import install_id

        with _transaction(engine) as c:
            install = install_id(c)
    except Exception:  # noqa: BLE001 — an unlabelled lease is still reaped by its run tag
        _log_caught(logger)
        install = ""
    lease = build_services.provision(run_id, install=install)
    with _transaction(engine) as c:
        emit_event(c, project_id, "code_task.build_services_ready", "boss", "code_task_run",
                   run_id, {"services": list(wanted), "database": lease.name,
                            "host": lease.host, "port": lease.port,
                            "env": list(build_services.ENV_NAMES)})
    return lease


# --------------------------------------------------------------------------- #
#  The governed job handler                                                   #
# --------------------------------------------------------------------------- #
def _authority_refused(engine, budget, project_id: str, reservation_id: Optional[str], run_id: str,
                       exc: BaseException):
    """The build was refused its authority before anything ran: its budget reservation goes back
    and the refusal (code_task_service.AuthorityRefused) is what the caller raises."""
    from .code_task_service import AuthorityRefused

    try:
        with _transaction(engine) as c:
            _release(budget, c, project_id, reservation_id, "authority refused", run_id=run_id)
    except Exception:  # noqa: BLE001 — the refusal is the answer either way
        _log_caught(logger)
        pass
    return AuthorityRefused(getattr(exc, "reason", None) or type(exc).__name__)


def _hold_for_github(engine, run_id: str, result: Any) -> Any:
    """A build that finished while GitHub was down (github_outage, BOSSAPP-922) ends "waiting
    for GitHub", in words, never failed: its commits are on this machine's mirror, its change set
    waits for the push, and a queued job pushes it and opens the pull request when GitHub
    answers. Every other build passes through unchanged."""
    try:
        from . import github_outage
        from .local_repo_host import is_local_pr_url

        with _transaction(engine) as c:
            row = c.execute(sql(
                "SELECT project_id, status, pull_request_url, finalize_ctx, spec FROM code_task_runs "
                "WHERE id = :id"), {"id": run_id}).mappings().first()
            if row is None:
                return result
            fctx = row["finalize_ctx"] or {}
            if not fctx.get("github_offline") or not is_local_pr_url(row["pull_request_url"]):
                return result
            if row["status"] in ("failed", "cancelled", github_outage.WAITING_STATUS):
                return result
            cs = c.execute(sql(
                "SELECT id, repository, branch, base_branch FROM local_change_sets "
                "WHERE run_id = :r OR branch = :b ORDER BY created_at DESC LIMIT 1"),
                {"r": run_id, "b": fctx.get("work_branch") or ""}).mappings().first()
            if cs is None:
                return result
            owner, _, repo = str(cs["repository"]).partition("/")
            pending_id = github_outage.record_pending(
                c, project_id=row["project_id"], owner=owner, repo=repo, branch=cs["branch"],
                base_branch=cs["base_branch"], run_id=run_id, change_set_id=cs["id"],
                reason=str(fctx.get("github_offline") or "GitHub did not answer"))
            github_outage.mark_run_waiting(c, run_id, reason=str(fctx.get("github_offline")),
                                           pending_id=pending_id)
            emit_event(c, row["project_id"], "code_task.waiting_for_github", "boss",
                       "code_task_run", run_id, {"pending_id": pending_id,
                                                 "branch": cs["branch"]})
        out = dict(result) if isinstance(result, dict) else {}
        out.update({"status": github_outage.WAITING_STATUS, "done": True,
                    "push_pending_id": pending_id,
                    "message": "Waiting for GitHub: the build finished and its commits are safe "
                               "on this machine; BOSS pushes them and opens the pull request as "
                               "soon as GitHub answers."})
        return out
    except Exception:  # noqa: BLE001 — never turn a finished build into a failure over this
        _log_caught(logger)
        return result


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
        # AND RUNS AS ITS OWN UID (sandbox_identity, IDENTITY.md §7.6): the agent, its commands
        # and its seats never run as the control plane's uid.
        from .sandbox_identity import bind as _sandbox_bind
        from ..app_links.run_env import for_run as _link_env
        from ..templates.run_ref import for_run as _template_files
        from .harness_failures import run_scope as _harness_scope

        # AND ITS ENGINE IS WATCHED AS ONE (harness_failures): a session that finds the engine
        # cannot run stops the whole build with that verdict, not just its own stage.
        with owned_process.owner_scope("code_run", run_id,
                                       project_id=(run or {}).get("project_id"), fresh=True), \
                _harness_scope(run_id), \
                _sandbox_bind(run_id, project_id=(run or {}).get("project_id")) as _sbx, \
                _link_env(engine, run_id, (run or {}).get("project_id")), \
                _template_files(engine, run, _sbx):
            if mcp is not None and _sbx is not None:
                mcp.move_into(_sbx)
            result = _run_governed_code_task(engine, settings, jobs, run_id,
                                             fencing_token=fencing_token, mcp=mcp)
        return _hold_for_github(engine, run_id, result)
    finally:
        if mcp is not None:
            mcp.cleanup()
        # Whatever is still running under this build's name ends with it (a background child
        # an exception unwound past, a seat the panel deadline gave up on).
        owned_process.terminate_owner(run_id, reason="run_ended", grace=2.0)
        # Its throwaway database ends with it too (after its processes: nothing still writes).
        from .build_services import teardown as _services_teardown

        _services_teardown(run_id)


def _run_governed_code_task(engine, settings: Settings, jobs, run_id: str,
                            *, fencing_token: Optional[str] = None, mcp: Any = None) -> dict:
    from .run_fencing import RunFenced, assert_owner, claim_run, hash_token  # noqa: F401
    _fence = hash_token(fencing_token)
    _build_database: dict[str, Any] = {}      # filled by _provision_build_services
    run = None
    try:
        with _transaction(engine) as c:
            run = load_code_task_run(c, run_id)
            if run is not None and _fence:
                # The claim is decided by the lease's fence epoch (run_fencing.claim_run,
                # BOSSAPP-986): the control plane stamps a monotonically increasing epoch on
                # every claim and reclaim, so a stale execution — one whose lease has been
                # reclaimed — is refused HERE, before it does any work; its late writes match
                # 0 rows at the database boundary either way.
                claim_run(c, run_id, _fence)
    except RunFenced as exc:
        # A duplicate execution stands down without touching the run: the owner continues.
        try:
            with _transaction(engine) as c:
                emit_event(c, (run or {}).get("project_id") or "boss-dogfood",
                           "code_task.fenced_exit", "boss", "code_task_run", run_id,
                           {"detail": str(exc)[:200],
                            "note": "stale claim refused; the current lease's execution "
                                    "continues"})
        except Exception:  # noqa: BLE001 — standing down must not fail over its own event
            _log_caught(logger)
        return {"status": "superseded", "done": True, "reason": str(exc)}
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
    _record_effort(engine, project_id, run_id, settings)
    # Rules a ROLE mandated, loaded once for the whole run so every stage's shared preamble is
    # byte-identical (the prompt cache depends on that) and so a rule added mid-build cannot
    # change the instructions between stages of the same task.
    from .platform_standards import load_mandated_standards
    from .platform_standards import load_reference_links
    with engine.begin() as _c:
        _n_mandated = load_mandated_standards(_c, project_id)
    # WHAT THIS BUILD OWES BEYOND ITS BRIEF (build_scope): ops endpoints and browser e2e only
    # when the brief asks, or the workflow deploys what it builds. Set once, read everywhere.
    from . import build_scope as _build_scope
    try:
        with engine.begin() as _c:
            _deploys = _build_scope.workflow_deploys(_c, fctx.get("workflow_run_id"))
    except Exception:  # noqa: BLE001
        _log_caught(logger)
        _deploys = False
    _scope = _build_scope.decide(
        str((spec or {}).get("title") or ""), str((spec or {}).get("instructions") or ""),
        (spec or {}).get("acceptance_criteria") or (), workflow_deploys=_deploys,
        has_manifest=bool(fctx.get("deploy_manifest")))
    _build_scope.set_scope(_scope)
    logger.info("code_task.build_scope run=%s scope=%s", run_id, _scope)
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
            if plane.get("github_plane") == "local" and (spec.get("owner"), spec.get("repo")) != (
                    _owner, _repo):
                # The run names its LOCAL repository from here on, so its preview, its deploy
                # and its merge read the bare repository, never the install's GitHub default.
                c.execute(sql(
                    "UPDATE code_task_runs SET spec = COALESCE(spec, '{}'::jsonb) || "
                    "jsonb_build_object('owner', CAST(:o AS text), 'repo', CAST(:r AS text), "
                    "'repo_host', 'local'), updated_at = now() WHERE id = :id"),
                    {"o": _owner, "r": _repo, "id": run_id})
                spec = {**spec, "owner": _owner, "repo": _repo, "repo_host": "local"}
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
        _log_caught(logger)
        pass
    # GITHUB IS DOWN, THE BUILD GOES ON (github_outage, BOSSAPP-922): a GitHub repository's
    # adapter falls back to the repository's local mirror once GitHub stops answering after the
    # retries; the build then ends "waiting for GitHub" and a queued job pushes it later.
    from . import github_outage as _gho
    from .local_repo import is_local_owner as _is_local_owner_early

    _outage: Optional[_gho.ResilientGitHub] = None
    if (not _is_local_owner_early(_owner) and _owner and _repo
            and getattr(settings, "github_outage_mirror", True)):
        def _record_offline(reason: str) -> None:
            from .local_repo_host import LocalSandboxPipeline

            sf.pipeline = LocalSandboxPipeline(settings, _outage.mirror())
            with _transaction(engine) as _c:
                _c.execute(sql(
                    "UPDATE code_task_runs SET finalize_ctx = COALESCE(finalize_ctx, '{}'::jsonb) "
                    "|| jsonb_build_object('github_offline', CAST(:why AS text)), updated_at = now() "
                    "WHERE id = :id"), {"why": reason[:300], "id": run_id})
                emit_event(_c, project_id, "code_task.github_offline", "boss", "code_task_run",
                           run_id, {"reason": reason[:300],
                                    "words": "GitHub is not answering; the build continues on "
                                             "this machine's copy of the repository."})

        _outage = _gho.ResilientGitHub(sf.github, settings=settings, project_id=project_id,
                                       run_id=run_id, engine=engine, on_switch=_record_offline)
        sf.github = _outage
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
    # A CONNECTOR IS PRE-TESTED, on every entry path and every retry (connector_build_input).
    instructions, criteria = connector_build_input(engine, run, instructions, criteria)
    # WHAT EARLIER RUNS LEARNED, AND THE RULES THAT GOVERN THIS CODE (knowledge spec §4.2), once.
    instructions = learning_context.for_build(engine, project_id=project_id, run_id=run_id,
                                              title=title, instructions=instructions)
    # Infrastructure persona (spec 05 §3.2 / T4): IaC tasks get Terraform-aware role
    # guidance appended to the qa/impl/solo prompts; stage mechanics are unchanged.
    iac_task = is_iac_task(spec, title)
    # APP-2 (#92): UI-bearing tasks get the design axis appended to the impl + review prompts.
    ui_task = design_prompts.is_ui_bearing(spec, title)
    # Cost/rigor dial: resolve the profile (per-task override > project default > edition
    # default) and infer the risk tier, clamped to the profile's range. Scales effort to work.
    with _transaction(engine) as c:
        profile = resolve_for_run(c, project_id, spec.get("profile"), settings)
    # A TYPED DECISION, WITH THE KEYWORD RULE AS ITS FALLBACK (BOSSAPP-960). The tier was
    # decided by substring markers (SENSITIVE_MARKERS), measured on evals/task_rigor missing 14
    # of 22 sensitive tasks; `model_risk_tier` (typed options, refused-not-coerced, the same
    # decisions backend every other typed decision rides) existed and nothing called it. Its
    # own fallback keeps the keyword answer when the model cannot answer, an explicit spec tier
    # still wins inside it, and any failure here degrades to the old call outright.
    try:
        from .pipeline_profiles import model_risk_tier
        from .typed_decisions import make_decider

        _rt = model_risk_tier({**spec, "title": title}, profile, make_decider(settings))
        risk_tier = int(_rt["tier"])
        with _transaction(engine) as c:
            emit_event(c, project_id, "code_task.risk_tier_decided", "boss", "code_task_run",
                       run_id, {"tier": risk_tier, "sensitive": bool(_rt.get("sensitive")),
                                "by": "model" if _rt.get("ok") else "keyword_fallback"})
    except Exception:  # noqa: BLE001 — the keyword tier is the floor this never falls below
        _log_caught(logger)
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
        # A RE-ENTRY (lease reclaim, a deploy's hand-back, a resume) settles the earlier
        # attempt's hold first: what it spent is the build's, and a hold left to lapse would
        # ask a person to reconcile it.
        if run.get("reservation_id"):
            _release(budget, c, project_id, run["reservation_id"],
                     "re-entry: the earlier attempt's hold settled", run_id=run_id)
        # THE CALL-CLASS GATE (BOSSAPP-999): a project that denies 'build_stage' calls, or whose
        # daily class volume is spent, refuses the build HERE — before any budget is held, with
        # the policy's words on the blocked run, through the same refused-start reading below.
        try:
            from .call_classes import CallClassRefused as _ClassRefused
            from .call_classes import authorize as _authorize_class

            _authorize_class(c, settings, project_id, call_class="build_stage")
        except _ClassRefused as refusal:
            reservation = {"status": "refused", "message": str(refusal), "transient": False,
                           "refused_by": "call_class"}
        else:
            # THE BUILD IS UNDER THE PROJECT'S CAPS (build_budget): its estimate at marginal
            # cost ($0 on a subscription — a gate, refused once the cap's money is spent). A
            # cap with no room refuses the start here, in the governor's words
            # (refused_reservation).
            reservation = budget.reserve(
                c, project_id, category="code_task",
                amount_usd_micros=_build_budget.reservation_estimate(c, settings, project_id),
                reason=f"governed code task: {title[:64]}",
                expires_in_minutes=_build_budget.RESERVATION_MINUTES, job_id=job_id,
                risk_tier=spec.get("risk_tier"))
    if reservation.get("status") != "reserved":
        return refused_reservation(engine, jobs, run_id=run_id, project_id=project_id,
                                   reservation=reservation, fencing_token=fencing_token)
    reservation_id = reservation["reservation_id"]
    with _transaction(engine) as c:
        _build_budget.bind(c, run_id, reservation_id)

    owner, repo = _owner, _repo
    # ONE BASE FOR THE CLONE AND THE PR (build_base_branch): the task's explicit base, else
    # the project's bound branch for this repo, else the host's default. It used to be
    # `spec.base_branch or "main"` for the clone and the GitHub default for the PR, so a
    # project bound to rel/rc10.10-kg built on main and opened PRs against main.
    from . import build_base_branch as _bbb

    with _transaction(engine) as c:
        _bases = _bbb.resolve_build_base(
            c, project_id=project_id, owner=owner, repo=repo,
            explicit=spec.get("base_branch"),
            host_default=lambda: _bbb.host_default_branch(sf.github), run_id=run_id)
        emit_event(c, project_id, "code_task.base_branch_resolved", "boss", "code_task_run",
                   run_id, _bases.as_event())
    base_branch = _bases.clone     # may advance to work_branch below (a repair, a release)
    repo_base_branch = _bases.pr   # the PR base
    # Feedback-iteration: attempts after the first share ONE branch derived from the root
    # run and clone the prior attempt's code, so the model builds ON prior work instead of
    # regenerating from base. The PR on that branch is reused (see create_pull_request).
    root_run = fctx.get("root_run_id") or run_id
    work_branch = f"boss/code/{root_run}"
    # A REPAIR continues earlier work of this lineage. A retry whose earlier attempts produced
    # nothing (code_task_service.retry_code_task marks it `fresh_build`: crun_2f76585e died at
    # the clone) is a FRESH build of the same brief: its tree holds no work of this app, so the
    # repair paths (augment the existing suite, cross-run repair prompts) have nothing to act on.
    _is_repair = root_run != run_id and not fctx.get("fresh_build")
    # A FIX THAT TARGETS A RELEASE IS BUILT ON ITS OWN BRANCH, cut from the serving commit —
    # never on the lineage branch, whose open PR may carry other runs' unreleased (even
    # rejected) commits (release_scope, the D2 case). Its PR is opened against a base at the
    # serving commit, so it shows only its own diff. No fall back to the shared branch.
    from . import release_scope as _release_scope

    # NOT `_release`: that name is the budget's release (imported above), which every exit of
    # this function calls — shadowing it made every release-scoped fix build crash with
    # "'dict' object is not callable" (Order API release 1.2 fix, crun_a3074b0f).
    _release_target = _release_scope.of_run(fctx)
    from .local_repo import is_local_owner as _is_local_owner_name

    _pushed_branch = str(fctx.get("local_change_branch") or "")
    if _release_target is not None:
        try:
            work_branch = _release_scope.prepare_branch(sf.github, _release_target)
        except _release_scope.ReleaseBranchError as exc:
            raise RuntimeError(f"release branch: {exc}") from exc
        base_branch = work_branch
    elif _pushed_branch and _is_local_owner_name(owner) and sf.github.branch_exists(_pushed_branch):
        # A FIX ROUND ON A PUSHED LOCAL CHANGE (BOSSAPP-948, local_review.start_fix_round): the
        # build works on the branch the person pushed, so its change set is the one refreshed.
        work_branch = _pushed_branch
        base_branch = work_branch
    elif root_run != run_id and sf.github.branch_exists(work_branch):
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
            _log_caught(logger)
            token = None
    from .local_repo import is_local_owner as _is_local_owner

    if not token and not _is_local_owner(owner):
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
    # The pinned engine (model_config default), the project's choices layered over the
    # deployment's, and the build/review lane engines: ONE function (model_router.stages
    # .lane_engines), which the pre-check's `sandbox.engine` asks too.
    from .model_router.stages import lane_engines

    with _transaction(engine) as _c:
        settings, build_engine, review_engine = lane_engines(_c, settings, project_id, spec)
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

    _agents_lock = threading.RLock()

    def _agent_for(engine_name: str):
        with _agents_lock:
            return _agent_for_locked(engine_name)

    def _session_budget_ceiling():
        """What is LEFT of this build's per-task budget, in dollars, asked before each session.

        The engine contract passes it to any engine that can stop itself at a ceiling
        (`--max-budget-usd`); the setting and the CLI flag both existed, but no run_role call in
        this pipeline ever connected them, so a runaway session was only stopped at the next
        stage boundary. None when the install sets no per-task budget."""
        cap = int(getattr(settings, "code_task_budget_usd_micros", 0) or 0)
        if cap <= 0:
            return None
        try:
            with _transaction(engine) as c:
                spent = _build_budget.unsettled_money(c, project_id, run_id)
        except Exception:  # noqa: BLE001 — an unreadable ledger must not unbound the session
            spent = 0
        return max(cap - spent, 0) / 1e6

    def _agent_for_locked(engine_name: str):
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
            # EVERY SESSION OF EVERY STAGE RUNS UNDER THE BUILD'S REMAINING BUDGET
            # (engine_contract._default_ceiling): set once here, enforced by the engine itself
            # where it can be (budget_limit), at the stage boundary where it cannot.
            a.budget_ceiling = _session_budget_ceiling
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
    except Exception:  # noqa: BLE001 — an unreadable budget never fails the build: route as green
        _record_swallowed(logger, "budget_state", project_id=project_id, run_id=run_id,
                          transaction=lambda: _transaction(engine))
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
        if decision.refused:
            # No source may take this stage under the policy — in words, never a silent
            # fallback (docs/product/MODEL_ROUTER.md).
            from .model_router.failover import RouteRefused

            raise RouteRefused(f"{step}: {decision.refusal}")
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

    def _await_route(step: str, **kw):
        """The stage's route — and NEVER STRANDED BY A TEMPORARY OUTAGE: a refusal only because
        its sources are out for now waits (bounded backoff, a half-open trial each time) and
        asks again, saying so in words, instead of failing the stage (Decision 21)."""
        from .model_router.failover import wait_for_route, waiting_words

        def on_wait(decision, pause: float, waited: float) -> None:
            words = waiting_words(decision, pause, step)
            logger.info("run %s: %s", run_id, words)
            with _transaction(engine) as c:
                emit_event(c, project_id, "code_task.stage_waiting", "boss", "code_task_run",
                           run_id, {"step": step, "why": words, "retry_in_s": round(pause),
                                    "waited_s": round(waited)})

        return wait_for_route(lambda: stage_router.route(step, **kw), on_wait=on_wait)

    def _routed_agent(step: str, decision):
        """(agent, model) for a decision. Smart mode wraps the agent so a limit or a
        transient failure at call time retries the SAME stage on the next source at once
        (model_router.failover); Fixed mode is exactly as before."""
        eng, model = _take(step, decision)
        agent = _agent_for(eng)
        if decision.mode != "smart":
            return agent, model
        from .model_router import store as _route_store
        from .model_router.failover import FailoverAgent

        def reroute():
            _route_store.invalidate(project_id)
            nxt = _await_route(step)
            if nxt.refused:
                return None, None, nxt
            e2, m2 = _take(step, nxt)
            return _agent_for(e2), m2, nxt

        def on_hop(failed: str, nxt, kind: str) -> None:
            try:
                with _transaction(engine) as c:
                    emit_event(c, project_id, "code_task.stage_rerouted", "boss",
                               "code_task_run", run_id,
                               {"step": step, "from": failed, "to": nxt.engine,
                                "model": nxt.model, "why": kind})
            except Exception:  # noqa: BLE001 — the record of a reroute is not the reroute
                _log_caught(logger)
                pass

        return FailoverAgent(agent, model, eng, reroute, on_hop), model

    def _route(step: str):
        """Resolve a step to (agent, model) through the router."""
        return _routed_agent(step, _await_route(step))

    def _route_escalated(step: str):
        """The step again, one boundary later, on the evidence that its route produced no
        usable output (write_tests) or that this is the final attempt (implement). Fixed mode:
        the hard-problems lane, as before; Smart mode: the escalation rules."""
        signal = "no_output" if step == "write_tests" else "final_attempt"
        return _routed_agent(step, _await_route(step, signal=signal,
                                                previous=stage_routes.get(step)))

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
        return _take(step, _await_route(step, budget=False))

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
    guest_egress: dict[str, Any] = {}     # the egress clone access, once the identity run starts
    identity_run = None
    guest_vm_ids: list[str] = []          # for release-on-success / retain-on-failure
    accumulated: dict[str, str] = {}      # path -> content committed so far (the PR diff)
    _impl_ran = [False]                   # implement started (or the tree was seeded): save on failure
    guest_base = base_branch              # advances to work_branch once commits exist
    # THE TEMPLATE HOLD (template_push_hold): every push below asks _push_held first.
    _tpl_applies = _applies_template(engine, run)
    _tpl_gates: Optional[dict] = None     # the template gates' verdicts, once they ran
    _template_record: Optional[dict] = None   # their receipt (the secret scan's findings)
    # A MULTI-SERVICE APP (services.stacks): the layout the brief asks for, so the test author
    # and the coder are told the per-service layout and commands, and the gate checks for it.
    _stack_ctx = _stack_context(engine, run, title, instructions)
    _stack_layout = _stack_ctx["layout"]
    _stack_note = ""
    if _stack_layout is not None:
        from . import stacks as _stacks

        _stack_note = _stacks.layout_prompt(
            _stack_layout, _app_id_hint(spec, fctx, _stack_layout),
            template_command=_stack_ctx["template_command"],
            template_stack=_stack_ctx["template_stack"])
    _last_stack_plan: list = [None]      # the newest verify's StackPlan, for the later gates
    _stack_record: dict = {}

    def _push_held(where: str) -> bool:
        reason = template_push_hold(_tpl_applies, _tpl_gates)
        if reason is None:
            return False
        try:
            with _transaction(engine) as c:
                emit_event(c, project_id, "code_task.push_held", "boss", "code_task_run", run_id,
                           {"where": where, "reason": reason})
        except Exception:  # noqa: BLE001 — the hold holds whether or not it was recorded
            _log_caught(logger)
            pass
        return True
    _app_id = verification_target.official_app_id(spec, fctx)
    # THE APP'S OWN DIRECTORY (the pinned app, else the folder the brief names: `helpdesk-ai/`).
    # A build's tests are its app's tests: the budget counts only those, and a repair's
    # "existing suite" oracle is only those — never other apps' suites at the repo root.
    _app_scope = verification_target.app_scope(spec, fctx)

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
        if guest_egress.get("spec"):
            # The guest's shim holds the per-run egress credential; no GitHub token is sent.
            guest_spec["egress"] = guest_egress["spec"]
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
        record_llm_call(engine_name=f"guest:{eng_name}",
                        usage=_guest_billed(res.usage, guest_spec),
                        cost_usd_micros=int(res.cost_usd_micros or 0),
                        turns=int(res.turns or 0))
        return res

    def _commit_stage(files, message: str) -> None:
        """Commit a stage's returned files to the work branch (control-plane-only git;
        requires the task repo to be the deployment's configured target repo)."""
        nonlocal guest_base
        if not files:
            return
        if _push_held("stage_commit"):
            return
        sf.github.ensure_branch(work_branch, base_branch)
        # One commit per stage where the host can (software_factory.commit_change).
        commit_change(sf.github, work_branch, [(fc.path, fc.content) for fc in files], message)
        for fc in files:
            accumulated[fc.path] = fc.content
        guest_base = work_branch
        _record_work_branch("stage_commit")

    def _record_work_branch(label: str) -> None:
        """Record on the run that its work is on the lineage branch: how a retry knows there is
        work to build onto (code_task_service.lineage_kept_work). Best-effort, never fatal."""
        try:
            with _transaction(engine) as c:
                c.execute(sql(
                    "UPDATE code_task_runs SET finalize_ctx=COALESCE(finalize_ctx,'{}'::jsonb) "
                    "|| jsonb_build_object('work_branch', CAST(:b AS text), "
                    "'checkpointed_after', CAST(:s AS text)), updated_at=now() WHERE id=:id"),
                    {"b": work_branch, "s": label, "id": run_id})
        except Exception:  # noqa: BLE001 — the push happened whether or not it was recorded
            _log_caught(logger)

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
            if _push_held("declare_preview_app"):
                return
            sf.github.upsert_file(
                work_branch, DECLARED_APP_FILE, entry + "\n",
                f"governed: declare preview app ({entry})")
            accumulated[DECLARED_APP_FILE] = entry + "\n"
            with _transaction(engine) as c:
                emit_event(c, project_id, "code_task.preview_app_declared", "boss",
                           "code_task_run", run_id, {"entry": entry})
        except Exception as exc:  # noqa: BLE001 — a declaration must never sink a finished build
            _log_caught(logger)
            logger.warning("could not declare the preview app for %s: %s", run_id, exc)

    def _commit_approved_prd() -> None:
        """THE PRD LANDS WITH THE PR (BOSSAPP-929): the approved PRD this build came from, and
        its meeting minutes, committed to the work branch as docs/prd/<slug>.md through the same
        repo host the stages commit through (GitHub, or a local-first project's own repository).
        Never raises and never blocks the PR: a build without its PRD file is the old delivery."""
        try:
            from . import prd_record

            with _transaction(engine) as c:
                prd = approved_prd_design.find_approved_prd(c, project_id, fctx or {})
            if not prd or _push_held("prd_record"):
                return
            with _transaction(engine) as c:
                record = prd_record.commit_to_branch(c, sf.github, project_id=project_id,
                                                     decision_id=prd["decision_id"],
                                                     branch=work_branch)
                if record:
                    emit_event(c, project_id, "code_task.prd_committed", "boss", "code_task_run",
                               run_id, record)
            if record:
                accumulated[record["path"]] = prd["markdown"] + "\n"
        except Exception as exc:  # noqa: BLE001 — the PRD file must never sink a finished build
            _log_caught(logger)
            logger.warning("could not commit the approved PRD for %s: %s", run_id, exc)

    _lease_owner_at_start: dict[str, Optional[str]] = {"v": None}

    def _lease_owner_now() -> Optional[str]:
        try:
            with _transaction(engine) as c:
                return c.execute(sql(
                    "SELECT lease_owner FROM jobs WHERE id=:j"), {"j": job_id}).scalar()
        except Exception:  # noqa: BLE001
            _log_caught(logger)
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
            _log_caught(logger)
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
            _log_caught(logger)
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
        _abort_if_cancelled(f"phase->{name}", spends=name not in _DELIVERY_PHASES)
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
            _log_caught(logger)
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

    def _abort_if_cancelled(where: str, spends: bool = True) -> None:
        """Stop a run the operator cancelled. Cancellation was a DB update nobody read: one
        cancelled run kept executing for 40 minutes and $6.73 afterwards, and cancelled runs
        later overwrote their own status. Checked at stage boundaries so it costs one cheap
        query per stage and never interrupts an agent mid-call.

        `spends`: the next stage spends model money, so the budget is checked too
        (_stop_if_cap_reached). Delivery of finished work (the PR) passes False."""
        # THE ENGINE COULD NOT RUN (harness_failures): a stage that swallowed the failure does
        # not get to start the next one — the build ends here with the harness verdict.
        _stop_on_harness_failure(where)
        learning_writeback.observe(engine, project_id=project_id, hook="build_stage", run_id=run_id,
                                   where=where)
        try:
            with _transaction(engine) as c:
                st = c.execute(sql("SELECT status FROM code_task_runs WHERE id=:id"),
                               {"id": run_id}).scalar()
        except Exception:  # noqa: BLE001 — never let the check itself break a run
            _log_caught(logger)
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
            _log_caught(logger)
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
        if spends:
            _stop_if_cap_reached(where)

    def _stop_if_cap_reached(where: str) -> None:
        """THE CAPS STOP A BUILD AT A STAGE BOUNDARY (build_budget.cap_reached). The hold is
        renewed for the next stage first, so a long build keeps it; then, if the money settled
        this period plus what running builds have spent reaches a cap, the build stops here,
        before the next stage spends. Its own transaction: it never waits on the row `c` holds.
        A budget that cannot be read never stops a build."""
        try:
            with _transaction(engine) as c:
                _build_budget.renew(budget, c, project_id, reservation_id)
                reached = _build_budget.cap_reached(budget, c, project_id)
        except Exception:  # noqa: BLE001 — never let the check itself break a run
            _log_caught(logger)
            return
        if reached:
            raise BuildBudgetStopped(
                f"{reached['message']} The build stopped before {where.removeprefix('phase->')}"
                "; its work so far is kept, and Retry continues it.")

    def _stop_on_harness_failure(where: str) -> None:
        """Raise the run's harness failure, if a session of this run found its engine cannot
        run. Every agent call after it already refuses (no spend); this stops the pipeline at
        the next boundary — before review, repair or a quality gate judges code written blind."""
        from .harness_failures import HarnessFailure, tripped

        verdict = tripped(run_id)
        if verdict is not None:
            raise HarnessFailure(verdict)

    def _save_workspace(where: str) -> int:
        """Keep this attempt's changed files on the run so a retry continues from them
        (`saved_workspace_files`). Local only — nothing is pushed, so the template hold stands.

        A BLOCKED BUILD KEEPS ITS WORK, NEVER ITS SECRET. When the secret gate failed, the tree
        is kept with each flagged value redacted in place (secret_scan.without_secrets; a file
        that cannot be cleaned is dropped). It used to keep nothing: crun_ced396a4 was blocked
        for one literal, its sandbox was thrown away, and the Retry "repaired" an empty tree.
        Pushing instead was rejected: a template build's pushes wait for its gates (the hold),
        and the lineage branch may already carry an open PR.
        Best-effort, never fatal; returns how many files were saved."""
        if not _impl_ran[0]:
            return 0
        try:
            files = (list(accumulated.items()) if use_microvm
                     else [(fc.path, fc.content) for fc in sandbox.diff()])
            kept = [{"path": p, "content": c} for p, c in files if workspace_keepable(p)]
            redacted: list[str] = []
            dropped: list[str] = []
            if (_tpl_gates or {}).get("no_committed_secrets") is False:
                from ..templates.secret_scan import without_secrets

                _found = (((_template_record or {}).get("evidence") or {})
                          .get("secret_scan") or {}).get("findings") or []
                clean, redacted, dropped = without_secrets(
                    {f["path"]: f["content"] for f in kept if isinstance(f["content"], str)},
                    _found)
                kept = [{"path": p, "content": c} for p, c in clean.items()]
            if not kept:
                return 0
            with _transaction(engine) as c:
                c.execute(sql(
                    "UPDATE code_task_runs SET finalize_ctx=COALESCE(finalize_ctx,'{}'::jsonb) "
                    "|| jsonb_build_object('saved_workspace', CAST(:w AS JSONB)), "
                    "updated_at=now() WHERE id=:id"),
                    {"w": json.dumps({"files": kept, "after": where[:120],
                                      "file_count": len(kept),
                                      **({"secrets_redacted_in": redacted} if redacted else {}),
                                      **({"dropped_for_secrets": dropped} if dropped else {})}),
                     "id": run_id})
                emit_event(c, project_id, "code_task.workspace_saved", "boss", "code_task_run",
                           run_id, {"after": where[:120], "files": len(kept),
                                    **({"secrets_redacted_in": redacted} if redacted else {}),
                                    **({"dropped_for_secrets": dropped} if dropped else {})})
            return len(kept)
        except Exception:  # noqa: BLE001 — saving the work must never mask the real exit
            _log_caught(logger)
            return 0

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
        # EVERY EXIT KEEPS THE WORK LOCALLY FIRST, held or not, so a retry can continue from it.
        _save_workspace(f"exit: {reason}")
        try:
            if _push_held("partial_pr"):
                return None
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
                    created_by_job_id=job_id, actual_usd_micros=cost,
                    pr_base=(_release_target["pr_base"] if _release_target else repo_base_branch),
                    monitoring_removals=_monitoring_removals(spec))
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
            _log_caught(logger)
            try:
                with _transaction(engine) as c:
                    emit_event(c, project_id, "code_task.partial_pr_failed", "boss",
                               "code_task_run", run_id,
                               {"error": f"{type(exc).__name__}: {exc}"[:200]})
            except Exception:  # noqa: BLE001
                _log_caught(logger)
                pass
        return None

    def _mark_stage_done(stage: str) -> None:
        mark_stage_done(engine, project_id, run_id, stage)

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
        if _push_held(f"checkpoint:{label}"):
            return 0
        try:
            files = sandbox.diff()
        except Exception:  # noqa: BLE001 — a dead sandbox must not mask the real failure
            _log_caught(logger)
            return 0
        if not files:
            return 0
        try:
            sf.github.ensure_branch(work_branch, base_branch)
            # One commit per stage where the host can (software_factory.commit_change).
            commit_change(sf.github, work_branch, [(fc.path, fc.content) for fc in files],
                          f"governed: checkpoint after {label} — {title[:50]}")
            for fc in files:
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
            _log_caught(logger)
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

    def _honour_budget_stop(exc):
        # A cap was reached: stop like a Stop (work kept, money so far settled), but the build
        # ends blocked as `budget_denied` — Budget refused, in the cap's words — not cancelled.
        _finish_vms(success=False)
        _save_workspace("budget_stopped")
        try:
            _checkpoint("budget_stopped")
        except Exception:  # noqa: BLE001
            _log_caught(logger)
        return stop_build_for_budget(engine, budget, run_id=run_id, project_id=project_id,
                                     reservation_id=reservation_id, message=str(exc))

    def _honour_cancel(exc):
        if isinstance(exc, BuildBudgetStopped):
            return _honour_budget_stop(exc)
        # Operator intent, not a failure: stop, keep the work on the branch, release budget.
        _finish_vms(success=False)
        _save_workspace("cancelled")
        try:
            _checkpoint("cancelled")
        except Exception:  # noqa: BLE001
            _log_caught(logger)
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
            _log_caught(logger)
            pass
        return {"status": "cancelled", "done": True, "reason": str(exc)}

    sandbox = None
    review_sandbox = None
    command_container = None  # BOSS-410, only when the docker command runtime is selected
    cost = 0
    # IDENTITY (docs/product/IDENTITY.md §7.9-§7.10), the same rule as the ungoverned path: with
    # the egress proxy on and a verified principal + grant on this build, every clone (the work
    # sandbox, the review clone, each guest stage) goes through boss-egress and holds NO GitHub
    # token. A refused authority fails the build; it never falls back to the shared token.
    from boss_engine.identity import runs as identity_runs

    from . import local_repo as _local_repo

    _local_code = _local_repo.is_local_owner(owner)
    try:
        # A LOCAL REPOSITORY (BOSSAPP-918) is on this machine: nothing outside is reached, so
        # there is no GitHub authority to delegate and no egress to route through.
        if not _local_code:
            identity_run = identity_runs.start_code_task_run(
                engine, task_run_id=run_id, project_id=project_id, owner=owner, repo=repo,
                role_key=identity_runs.code_task_builder(spec, fctx), job_id=job_id)
    except Exception as exc:  # noqa: BLE001 — refused authority fails the build, never widens
        _log_caught(logger)
        raise _authority_refused(engine, budget, project_id, reservation_id, run_id, exc) from exc
    egress_access = identity_run[1] if identity_run is not None else None
    if _local_code:
        token = ""
        if use_microvm:
            raise RuntimeError(
                "This project's code is local, and a micro-VM stage cannot reach a repository on "
                "this machine. Build it on the local sandbox (Project settings › Sandbox), or "
                "publish it to GitHub first.")
        # The sandbox clones the bare repository over file:// — the same provision call, the
        # clone URL is the only difference.
        egress_access = SimpleNamespace(
            clone_url=_local_repo.file_url(_local_repo.bare_path(owner, repo, settings)),
            git_config=[], own_git=None)
    if identity_run is not None:
        token = ""
        guest_egress["spec"] = {"clone_url": egress_access.clone_url,
                                "git_config": [list(kv) for kv in egress_access.git_config
                                               if kv[0] != "credential.helper"],
                                "credential": identity_run[0].egress_credential}
    try:
        # THE LAST GUARD BEFORE ANYTHING IS SPENT (services.stacks): a build that asks for a
        # stack BOSS cannot build — Go, or Java on an install without the toolchain — is refused
        # here, in words, before a sandbox, an agent or a model call. Start, the panel's prompt
        # and the architecture approval are the earlier, cheaper places this is caught.
        #
        # AN EXISTING REPOSITORY DECIDES ITS OWN STACK. The brief's words are only a request when
        # there is no code yet: a change to BOSS's own (Python) repository whose brief describes a
        # bank's Java codebase was refused "needs Java" three times (KG P7d, 2026-10-31). When the
        # brief names a stack this install cannot build, the repository is cloned (no model, no
        # agent) and judged by the languages it actually uses; an empty one keeps the refusal.
        _brief_refused = not _stack_ctx["verdict"].ok
        sandbox = provider.acquire(task_id=run_id, tenant_id=project_id)
        with _transaction(engine) as c:
            _phase(c, "provisioning")
        if _outage is not None and not use_microvm:
            # THE MIRROR IS FETCHED AT BUILD START (all branches and tags), so an outage later in
            # the build — or one that already began — has this repository to build on.
            _murl, _menv = _gho.access_for(egress_access, owner, repo, token)
            _mirror = _gho.refresh_mirror(settings, owner, repo, url=_murl, env=_menv)
            with _transaction(engine) as c:
                emit_event(c, project_id, "code_task.github_mirror", "boss", "code_task_run",
                           run_id, {"ok": _mirror["ok"],
                                    **({} if _mirror["ok"] else {"error": _mirror["error"][-200:]})})
        try:
            sandbox.provision(owner, repo, base_branch=base_branch,
                              work_branch=work_branch, token=token, egress=egress_access)
        except Exception as _clone_error:  # noqa: BLE001 — re-raised unless GitHub is down
            if (_outage is None or use_microvm or not _gho.is_outage(_clone_error)
                    or not _gho.mirror_ready(owner, repo, settings)):
                raise
            _outage.go_offline(f"clone: {_clone_error}")
            try:
                sandbox.teardown()
            except Exception:  # noqa: BLE001
                _log_caught(logger)
            from . import local_repo as _lr

            sandbox = provider.acquire(task_id=run_id, tenant_id=project_id)
            sandbox.provision(owner, repo, base_branch=base_branch, work_branch=work_branch,
                              token="", egress=SimpleNamespace(
                                  clone_url=_lr.file_url(_lr.mirror_path(owner, repo, settings)),
                                  git_config=[], own_git=None))
        if _brief_refused:
            _repo_verdict = repository_stack_verdict(getattr(sandbox, "path", None))
            if _repo_verdict is None or not _repo_verdict.ok:
                raise StackUnsupportedError(
                    (_repo_verdict.message if _repo_verdict is not None
                     else _stack_ctx["verdict"].message))
            with _transaction(engine) as c:
                emit_event(c, project_id, "code_task.stack_from_repository", "boss",
                           "code_task_run", run_id,
                           {"brief_said": _stack_ctx["verdict"].message[:300],
                            "repository_ok": True})
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
        # THE APP'S IDENTITY for every gate planner (app_root.app_dir): the named app (_app_id,
        # else the brief's folder, resolved inside resolve_gate_plan), this run's changed paths,
        # and the files the app's lineage wrote before. A repair whose own diff is two files
        # still plans in the lineage's app folder, never at a shared repository root.
        # THE APP'S OWN RECORD FIRST (app_root.recorded_app_dir: its code component, else the
        # folder an earlier run of the app recorded), then every file the app ever wrote
        # (app_root.identity_paths: the lineage and every run of the same app). One attempt's
        # diff is never the app's identity: crun_cd48617a was seeded with the root package.json
        # an earlier retry rewrote, and planned the shared root as "the app".
        _lineage_paths: list[str] = []
        _recorded_app: Optional[str] = None
        try:
            from . import app_root as _app_root

            _lineage_paths[:] = [str(p) for p in (_app_root.identity_paths(engine, run) or [])]
            with _transaction(engine) as _c:
                _recorded_app = _app_root.recorded_app_dir(_c, run)
        except Exception:  # noqa: BLE001 — the named app and the change list still decide
            _log_caught(logger)
        _gate_plan0 = verification_target.resolve_gate_plan(
            spec, title, root, app_id=_app_id, pinned=_pinned_manifest, layout=_stack_layout,
            lineage=_lineage_paths, recorded=_recorded_app)
        # RECORD THE APP'S FOLDER once BOSS knows it, so every later attempt of this app reads it
        # (app_root.recorded_app_dir) instead of re-deriving it from its own diff.
        try:
            from . import app_root as _app_root

            _known = _app_root.resolve(root, app_id=_app_id or _app_scope, recorded=_recorded_app,
                                       lineage=_lineage_paths)
            if _known.path is not None and _known.path != Path(root):
                with _transaction(engine) as _c:
                    _app_root.remember_app_dir(_c, run_id, _known.path.relative_to(root).as_posix())
        except Exception:  # noqa: BLE001 — recording is a convenience; the plan above stands
            _log_caught(logger)
        # CONTINUE FROM THE FAILED ATTEMPT'S WORKSPACE (saved_workspace_files). Seeded after the
        # verify manifest is pinned, exactly as the parent's agents wrote after it. A seeded run
        # skips the design chain, write_tests and the first implement: its first pass is the
        # repair, fed the parent's failing gates, verify output and the operator's note.
        _seeded = 0
        _seeded_paths: list[str] = []
        #: The parent's workspace text as seeded ({path: text}): what a repair's reviewers are
        #: shown a diff against, when it is exactly the text the parent's seat reviewed.
        _seed_baseline: dict[str, str] = {}
        _seed_from = str(fctx.get("seed_workspace_from") or "").strip()
        if _seed_from:
            def _load(rid: str) -> Optional[dict]:
                with _transaction(engine) as c:
                    return load_code_task_run(c, rid)
            _seed_files = layered_workspace_files(_load, _load(_seed_from))
            if use_microvm:
                _seed_files = []  # each guest stage clones the branch; nothing to seed into
            _seeded = seed_workspace(root, _seed_files) if _seed_files else 0
            _seeded_paths[:] = [str(f["path"]) for f in _seed_files if _seeded]
            _seed_baseline.update({str(f["path"]): f["content"] for f in _seed_files
                                   if isinstance(f.get("content"), str)})
            with _transaction(engine) as c:
                emit_event(c, project_id,
                           "code_task.workspace_seeded" if _seeded
                           else "code_task.workspace_seed_missing", "boss", "code_task_run",
                           run_id, {"from_run": _seed_from, "files": _seeded,
                                    **({} if _seeded else {
                                        "why": "no saved workspace to continue from (micro-VM "
                                               "substrate, or the attempt saved none) — this "
                                               "attempt runs the full pipeline"})})
            if _seeded:
                _impl_ran[0] = True
                # The seeded files are this lineage's app: plan from them, as every later
                # verify will (_changed_paths), not from the base branch alone.
                _gate_plan0 = verification_target.resolve_gate_plan(
                    spec, title, root, app_id=_app_id, changed=list(_seeded_paths),
                    pinned=_pinned_manifest, layout=_stack_layout, lineage=_lineage_paths, recorded=_recorded_app)
        # BUILD SERVICES (build_services): the run's own throwaway Postgres when the repo
        # declares `services: [postgres]` or the project's setting asks for it. Provisioned
        # before any agent, so the agents and every verify command see DATABASE_URL; a run that
        # asked and cannot have it fails here, saying why — its tests never run without it.
        _services_lease = _provision_build_services(
            engine, settings, run_id, project_id, _pinned_manifest, use_microvm=use_microvm,
            spec=spec, title=title, root=root, decided=_build_database)
        if _services_lease is not None:
            from .build_services import brief as _services_brief

            instructions = f"{instructions}\n\n{_services_brief(_services_lease)}"
        if _gate_plan0.source != "task":
            verify_commands = list(_gate_plan0.commands)
            test_cmd = ((verification_target.manifest_display(root, _pinned_manifest)
                         if _gate_plan0.from_manifest else _gate_plan0.description
                         if _gate_plan0.runs_steps else " && ".join(verify_commands))
                        or test_cmd)
        if _stack_layout is not None and _gate_plan0.source in ("stack", "stack_missing"):
            # What the prompts say BOSS runs: each service's own steps, then the e2e against the
            # started backend — not the "missing services" sentence a still-empty tree gets.
            test_cmd = (_gate_plan0.description if _gate_plan0.source == "stack" else
                        f"each service's install, build and unit tests in its own directory, "
                        f"then the backend started from {_stack_layout.backend_dir}/"
                        f"boss.preview.command and `npm run test:e2e` in "
                        f"{_stack_layout.frontend_dir}/ against it")
        # THE REPO'S boss.verify.yaml IS THE AUTHORITY when it declares one: never the greenfield
        # web contract nor the default npm command at the root (crun_de5f077a, a BOSS change,
        # was gated on a root package.json "missing vite").
        _manifest_governs = verification_target.manifest_governs(spec, _pinned_manifest, _app_id)
        if _manifest_governs and not _gate_plan0.from_manifest:
            test_cmd = verification_target.manifest_display(root, _pinned_manifest) or test_cmd
        _web_verify = (
            verification_target.detect_kind(spec, title) == "web"
            or str(test_cmd or "").startswith("npm ")
            or bool(_app_id)
        ) and not _gate_plan0.runs_steps and _stack_layout is None and not _manifest_governs
        last_verify_cwd = None
        last_verify_output = ""

        def _changed_paths() -> list[str]:
            """Every path this run has changed: what is checkpointed plus the live tree."""
            paths = set(accumulated)
            # A REPAIR SEEDED FROM A SAVED WORKSPACE: the seeded files are this lineage's work
            # against the base branch, even when the live diff reports nothing (crun_cc365333
            # planned its tests at the repository root: changed was empty, so the app folder
            # was never found and npm ran on another app's package.json).
            paths.update(_seeded_paths)
            if not use_microvm:
                try:
                    paths.update(sandbox.diff_summary().paths)
                except Exception:  # noqa: BLE001 — scope falls back to the whole suite
                    _log_caught(logger)
                    pass
            return sorted(paths)

        def _gate_app_dir(changed=None) -> Path:
            """THE APP'S DIRECTORY for a gate that runs before (or without) a recorded verify
            directory — the template gates, the web package check. The same identity every
            plan uses (app_root.app_dir): the named app, this run's paths, the lineage's files.
            Never the shared repository root for an app in a subfolder (crun_7768e91e)."""
            from . import app_root

            # None = BOSS cannot place the app in a shared repository: the gate plan already
            # carries that refusal (source "app_unknown") and blocks the build with it, so a
            # template gate here only needs a directory to report against, never to test.
            return app_root.gate_dir(root, app_id=_app_id or _app_scope,
                                     changed=sorted(set(list(changed or []) + _changed_paths())),
                                     lineage=_lineage_paths, recorded=_recorded_app) or Path(root)

        def _review_change_now() -> review_input.ChangeUnderReview:
            """THE change every reviewer seat judges (review_input.build): the live diff, plus
            every path this lineage changed that the live diff does not carry (a seeded file
            nobody touched again), read from the tree. Never the working tree against HEAD."""
            live = (dict(accumulated) if use_microvm
                    else {fc.path: fc.content for fc in sandbox.diff()})
            reader = None
            if not use_microvm:
                files_reader = getattr(sandbox, "_repo_files", None)

                def reader(p: str):
                    if files_reader is not None:
                        return files_reader.read_text(p, errors="strict")
                    target = root / p
                    return target.read_text(errors="strict") if target.is_file() else None
            return review_input.build(live, _changed_paths(), read=reader,
                                      keep=workspace_keepable)

        def _change_block_with_integrity(change: review_input.ChangeUnderReview) -> str:
            """L4: hand every reviewer seat the deterministic reachability/integrity evidence
            alongside the diff (pr_integrity_review) — never a verdict on its own, the seat
            judges each item. Evidence computation never blocks a review: a repo-walk failure
            just means the seat sees the diff without it."""
            block = change.block()
            try:
                existing = pr_integrity_review.gather_existing_files(root, change.paths)
                evidence = pr_integrity_review.compute_integrity_evidence(change.files, existing)
                block += pr_integrity_review.render_evidence_clause(evidence)
            except Exception:  # noqa: BLE001 — evidence is an addition, never a blocker itself
                _log_caught(logger)
            return block

        def _judge_seat(stage: str, ask) -> tuple[dict, Optional[str]]:
            """Run one reviewer seat, and ask it again once if its review did not see the change.

            `ask(preface)` runs the seat with `preface` in front of its prompt. A second hollow
            answer is returned with its reason: the caller records the seat as not having
            reviewed (never a pass), and the gate fails as review_input.HOLLOW_GATE_REASON."""
            rr = ask("") or {}
            reason = review_input.hollow_reason(rr.get("summary", ""), _review_change.paths)
            if not reason:
                return rr, None
            with _transaction(engine) as c:
                emit_event(c, project_id, "code_task.review_hollow", "boss", "code_task_run",
                           run_id, {"stage": stage, "reason": reason, "retrying": True,
                                    "changed_files": len(_review_change.paths)})
            again = ask(review_input.retry_preface(reason, _review_change)) or {}
            again = {**again, "cost_usd_micros": int(again.get("cost_usd_micros") or 0)
                     + int(rr.get("cost_usd_micros") or 0)}
            reason = review_input.hollow_reason(again.get("summary", ""), _review_change.paths)
            if reason:
                with _transaction(engine) as c:
                    emit_event(c, project_id, "code_task.review_hollow", "boss",
                               "code_task_run", run_id,
                               {"stage": stage, "reason": reason, "retrying": False,
                                "changed_files": len(_review_change.paths)})
            return again, reason

        _review_change = review_input.ChangeUnderReview()
        _hollow_seats: dict[str, str] = {}
        # BOSSAPP-1000: filled by the merged panel session when one sat (for the evidence).
        _merged_record: dict = {}
        _stack_declarations: list = []

        def _run_manifest_verify(plan, *, record: bool):
            """boss.verify.yaml: the affected suites, from the repo root, no scaffold repair."""
            nonlocal last_verify_output
            from .sandbox import CommandResult

            if plan.source == "stack" and not plan.error:
                from .stack_verify import run_stack_plan

                # The backend starts with what the app DECLARES — the serve proof's own
                # declarations (templates.run_env), read once per build.
                if not _stack_declarations:
                    _stack_declarations.append(_app_run_declarations(engine, run))
                _sneeds, _sbound = _stack_declarations[0]
                results, record_ = run_stack_plan(sandbox, plan.stack,
                                                  timeout=plan.timeout_seconds,
                                                  needs=_sneeds, bound=_sbound)
                _stack_record.clear()
                _stack_record.update({**plan.stack.as_dict(root), **record_})
                result = results[-1] if results else CommandResult(
                    command="", argv=[], exit_code=0, duration_ms=0, timed_out=False,
                    stdout=plan.description + "\n", stderr="")
                output = verification_target.verify_log(plan.description, results,
                                                         error=plan.error)
                if record:
                    last_verify_output = output
                return result
            if plan.error or not plan.commands:
                # A manifest that does not validate fails closed with its own message. A plan
                # that runs nothing passes only when nothing it leaves out is code (a docs-only
                # change); changed code no test covers fails below.
                failed = bool(plan.error)
                result = CommandResult(
                    command="", argv=[], exit_code=1 if failed else 0, duration_ms=0,
                    timed_out=False, stdout="" if failed else plan.description + "\n",
                    # The ERROR, not the description: a refused stack plan has no description.
                    stderr=(plan.error or plan.description) if failed else "")
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
            output = verification_target.verify_log(plan.description, results, error=plan.error)
            if record:
                last_verify_output = output
            return result

        def _prepare_app(plan, cwd):
            """BOSS's preparation of the app before its install: the verify script and the
            package.json hardening (prepare_web_verify_env). (app_cwd, prep, prep_note)."""
            # The command runs where it was written for (the root, for `npm --prefix app test`);
            # BOSS's own preparation — install, browsers, the verify script — happens in the app
            # directory that command names.
            _named = next((d for d in (verification_target.named_directory(c)
                                       for c in plan.commands) if d), None)
            app_cwd = (Path(root) / _named) if _named and (Path(root) / _named).is_dir() else cwd
            try:
                verification_target.ensure_verify_script(app_cwd)
            except Exception:  # noqa: BLE001 — never fail a run on script bookkeeping
                _log_caught(logger)
                pass
            prep_note = ""
            try:
                prep = verification_target.prepare_web_verify_env(
                    app_cwd,
                    scenarios=(fctx.get("qa_benchmark") or {}).get("acceptance_scenarios"),
                    app_id=_app_id,
                )
            except Exception as exc:  # noqa: BLE001
                _log_caught(logger)
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
            return app_cwd, prep, prep_note

        def _install_timeout() -> int:
            return max(300, int(getattr(settings, "sandbox_validation_timeout_seconds", 120) or 120))

        def _boss_install(app_cwd, prep):
            """BOSS's one install (NPM_INSTALL_CMD), the command every gate runs."""
            install = sandbox.run(
                str(prep.get("install_command") or verification_target.NPM_INSTALL_CMD),
                cwd=str(app_cwd),
                timeout=_install_timeout(),
            )
            _emit_cmd("install dependencies", install)
            return install

        def _qa_static_check(files) -> list[str]:
            """The plan's typecheck and lint, run on the tree right after QA wrote its tests, and
            the error lines about QA's own files (verification_target.qa_static_findings). []
            when clean, or when the plan has neither check, or BOSS cannot install the app yet.

            Pre-mortem finding 7: the implementer may not edit tests, tsc and eslint cover tests/,
            and the arbiter only ran inside warm rounds: a frozen test that failed typecheck or
            lint could never be fixed by anyone."""
            if use_microvm or not files or not str(test_cmd or "").startswith("npm "):
                return []
            try:
                plan = verification_target.resolve_gate_plan(
                    spec, title, root, app_id=_app_id, changed=_changed_paths(),
                    pinned=_pinned_manifest, layout=_stack_layout, lineage=_lineage_paths, recorded=_recorded_app)
                if plan.runs_steps or plan.source != "default" or plan.error:
                    return []
                wanted = [c for c in plan.commands
                          if c in {f"npm run {n}" for n in verification_target.QA_STATIC_SCRIPTS}]
                if not wanted:
                    return []
                app_cwd, prep, _note = _prepare_app(plan, plan.cwd)
                if not (Path(app_cwd) / "node_modules").is_dir():
                    if _boss_install(app_cwd, prep).exit_code != 0:
                        return []  # the install is the implementer's to fix, later
                out = []
                for command in wanted:
                    res = sandbox.run(command, cwd=str(app_cwd), timeout=_install_timeout())
                    _emit_cmd(f"check QA's tests: {command}", res)
                    if not res.ok:
                        out.append((res.stdout or "") + "\n" + (res.stderr or ""))
                try:
                    app_rel = str(Path(app_cwd).relative_to(root))
                except ValueError:
                    app_rel = ""
                return verification_target.qa_static_findings(
                    "\n".join(out), files, app_rel=app_rel)
            except Exception:  # noqa: BLE001 — a check BOSS could not run never fails the build
                _log_caught(logger)
                return []

        def _install_check():
            """BOSS's install exactly as its gate runs it — the same preparation, the same
            command, the same directory — on the tree as it now stands. (failure, output) when
            it fails, else None. Only for the npm gate (a manifest or stack plan installs in its
            own steps, and _run_verify names a failure there)."""
            if use_microvm or not str(test_cmd or "").startswith("npm "):
                return None
            if verification_target.verify_manifest_tampered(_pinned_manifest, root):
                return None
            plan = verification_target.resolve_gate_plan(
                spec, title, root, app_id=_app_id, changed=_changed_paths(),
                pinned=_pinned_manifest, layout=_stack_layout, lineage=_lineage_paths, recorded=_recorded_app)
            if plan.runs_steps:
                return None
            app_cwd, prep, _note = _prepare_app(plan, plan.cwd)
            install = _boss_install(app_cwd, prep)
            failure = verification_target.install_failure(install)
            if failure is None:
                return None
            failure["boss_added"] = [str(c).split(":", 1)[1] for c in (prep.get("changed") or [])
                                     if str(c).startswith("add:")]
            try:
                failure["cwd"] = str(Path(app_cwd).relative_to(root)) or "."
            except ValueError:
                failure["cwd"] = str(app_cwd)
            return failure, (install.stdout or "") + "\n" + (install.stderr or "")

        # The last recorded verify's failed dependency install (verification_target.install_failure),
        # or None: a fix round is told it was the install, and a run that ends on it says so.
        _install_fail: list = [None]
        # The last recorded verify's failing check (verification_target.failed_check): what ran
        # and its first error line, so a blocked build names it (checks_headline).
        _failed_check: list = [None]

        def _run_verify(*, record: bool = True):
            """Authoritative test command, scoped to the official app directory. BOSS's own
            install (NPM_INSTALL_CMD, the one every gate runs) is its first step."""
            result = _run_verify_once(record=record)
            env = None
            if record and not result.ok:
                # COULD THE SUITE RUN AT ALL? (suite_environment) When its failures share one
                # environmental cause — the run's test database refused the login because the
                # build-services server restarted empty (crun_de96bf12) — they are not the app's:
                # BOSS makes the database usable again and runs the suite once more; if it still
                # cannot, the build says "BOSS couldn't run the full suite", never "N failing".
                from . import suite_environment

                env = suite_environment.after_verify(
                    engine, settings, project_id=project_id, run_id=run_id,
                    output=last_verify_output, lease=_services_lease)
                if env and env.get("restored") == "restored":
                    result = _run_verify_once(record=record)
                    env = (suite_environment.classify(last_verify_output,
                                                      boss_service=_services_lease is not None)
                           if not result.ok else None)
            if record:
                _install_fail[0] = verification_target.install_failure(result)
                _failed_check[0] = verification_target.failed_check(result)
                if env:
                    from .suite_environment import failed_check as _env_check

                    _failed_check[0] = _env_check(env, str(getattr(result, "command", "") or ""))
            return result

        def _run_verify_once(*, record: bool = True):
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
                    _ws_write(_manifest_path, _pinned_manifest.text)
                if record:
                    last_verify_output = f"[boss] {tampered_manifest}\n"
                return CommandResult(command="", argv=[], exit_code=1, duration_ms=0,
                                     timed_out=False, stdout="", stderr=tampered_manifest)
            plan = verification_target.resolve_gate_plan(
                spec, title, root, app_id=_app_id, changed=_changed_paths(),
                pinned=_pinned_manifest, layout=_stack_layout, lineage=_lineage_paths, recorded=_recorded_app)
            _last_stack_plan[0] = plan.stack if plan.source == "stack" else None
            cwd = plan.cwd
            try:
                last_verify_cwd = str(cwd.relative_to(root)) if cwd != root else "."
            except ValueError:
                last_verify_cwd = str(cwd)
            if plan.runs_steps:
                return _run_manifest_verify(plan, record=record)
            app_cwd, prep, prep_note = _prepare_app(plan, cwd)
            if str(test_cmd or "").startswith("npm "):
                install_timeout = _install_timeout()
                install = _boss_install(app_cwd, prep)
                if install.exit_code != 0:
                    last_verify_output = verification_target.verify_log(
                        prep_note.removeprefix("[boss] ").rstrip("\n"), [install])
                    return install
                # BROWSERS ARE BOSS'S, ONCE PER INSTALL (playwright_cache): the first build that
                # needs a Playwright version downloads its Chromium into the shared cache, every
                # later build finds it there and skips the install. Only an app whose version
                # cannot be read falls back to installing inside its own sandbox.
                from . import playwright_cache
                from .sandbox import CommandResult as _PwResult

                _pw = playwright_cache.ensure(playwright_cache.project_version(app_cwd))
                if _pw.get("ok"):
                    _emit_cmd("install browsers", _PwResult(
                        command=f"boss: {_pw.get('detail')}", argv=[], exit_code=0,
                        duration_ms=int(float(_pw.get("seconds") or 0) * 1000),
                        timed_out=False, stdout="", stderr=""))
                else:
                    prep_note += f"[boss] shared browser cache: {_pw.get('detail')}\n"
                    _emit_cmd("install browsers", sandbox.run(
                        str(
                            prep.get("browser_install_command")
                            or verification_target.PLAYWRIGHT_INSTALL_CMD
                        ),
                        cwd=str(app_cwd),
                        timeout=max(install_timeout, playwright_cache.install_timeout()),
                    ))
            # npm install / the implementer can recreate leftover Playwright
            # configs. Force the official HookWatch tree again immediately
            # before the authoritative command so e2e cannot start itty-router
            # or a metrics stub.
            try:
                forced = verification_target.prepare_web_verify_env(
                    app_cwd,
                    scenarios=(fctx.get("qa_benchmark") or {}).get("acceptance_scenarios"),
                    app_id=_app_id,
                )
                prep_note += (
                    f"[boss] pre-verify restore restored="
                    f"{bool((forced.get('restored') or {}).get('restored'))} "
                    f"changed={(forced.get('changed') or [])[:16]}\n"
                )
            except Exception as exc:  # noqa: BLE001
                _log_caught(logger)
                prep_note += f"[boss] pre-verify restore failed: {type(exc).__name__}: {exc}\n"
            # What the app's package.json defines (verification_target.web_gate_for), and its
            # finding when it defines no test script.
            results = verification_target.run_web_gate(
                lambda command: sandbox.run(command, cwd=str(cwd)), plan)
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
            output = verification_target.verify_log(
                prep_note.removeprefix("[boss] ").rstrip("\n"), results or [result])
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
                _log_caught(logger)
                pass
        elif _app_id:
            try:
                verification_target.ensure_verify_script(root / _app_id)
            except Exception:  # noqa: BLE001
                _log_caught(logger)
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
        # The pre-existing tests OF THIS APP (its directory): the only suite a repair may treat
        # as its oracle. crun_37f4ccef took a repo's 37 test files of OTHER apps (735 cases)
        # as "the existing suite" of a new app. `preexisting_tests` stays repo-wide for the
        # regression floor: no pre-existing test anywhere may be removed or weakened.
        _app_tests = [p for p in preexisting_tests
                      if verification_target.in_app_scope(p, _app_scope)]
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

        # WHO OWNS THE TESTS. With a separate test author the implementer may not edit the
        # suite, so a secret-gate finding in a test file is QA's to repair (fixture_secret_repair),
        # including on a repair/retry that skips write_tests and reuses the lineage's suite.
        _qa_authors_tests = do_separate_tests
        # FIX LANE (triage, acceptance criterion #1): a small, evidenced bug fix — or an
        # operator/Shepherd `forced_lane` — never runs the design chain or the full review
        # panel below. Decided once, here, from the caller's size estimate; re-checked
        # against the REAL diff once the fix lane has written one (see the escalation check
        # near the review panel).
        from . import fix_lane_runner, fix_lane_triage
        lane = fix_lane_triage.classify(
            spec, diff_estimate=spec.get("diff_estimate"),
            forced_lane=spec.get("forced_lane") or fctx.get("forced_lane"),
        ).lane
        fctx["lane"] = lane
        # DESIGN PHASE (tier >= 2): standards -> spec -> plan -> review plan, BEFORE any code.
        # Spec-driven: the refined spec + plan are fed into the downstream coding steps. (The human
        # approve-plan gate is Wire-Phase 2b; for now review plan records a PLAN: OK/REVISE note.)
        design: dict = {}
        do_design = risk_tier >= 2 and not is_web_app and not _seeded
        if do_design and fix_lane_runner.skips_design(lane):
            do_design = False
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
            # AN APPROVED PRD IS THE DESIGN (approved_prd_design). HelpDesk AI's SLA-timers build
            # (crun_c8e2a5f8) spent 572 s here re-planning a PRD whose "Proposed Solution" the
            # owner had just approved (dec_13a8cd75). When the origin carries one, a short,
            # turn-bounded read maps that solution onto the repo and yields every field below
            # (standards/spec/plan/plan_review/plan_ok/packages). Any doubt — no solution
            # section, an incomplete answer, the repo contradicting the PRD — runs the full
            # design exactly as before, and the event says which path ran and why.
            design = _design_from_approved_prd(
                engine, project_id, run_id, fctx, title=title, instructions=instructions,
                criteria=criteria, root=root, route=_route)
            if design is not None:
                cost += int(design.pop("cost_usd_micros", 0) or 0)
                fctx["design_path"] = "approved_prd"
            else:
                fctx["design_path"] = "full"
            if design is None:
                # ONE SESSION FOR STANDARDS, SPEC AND PLAN. They were three cold sessions, each
                # re-reading the same repository (a HelpDesk AI build: 0.6 + 2.2 + 5.3 minutes).
                # One read produces all three, under the plan's route (the strongest of the
                # three). A part the session did not deliver is written by its own session as
                # before, so a short answer never becomes a thin design.
                # THE PLAN CHECKS ITSELF IN THE SAME SESSION (BOSSAPP-1003, audit 997 §7.6):
                # review_plan was its own fresh CLI session re-reading the repository to judge
                # a plan it was handed inline — $42.80/month, 9x the plan stage itself. The
                # session that wrote the plan now closes with the PLAN REVIEW section under the
                # same contract (NOT_A_CODE_TASK routing + a final PLAN: OK/REVISE marker,
                # review_verdict.plan_verdict). A session that skips the section still gets the
                # separate review session below, so the check never silently disappears.
                set_llm_context(stage="design")
                a, m = _route("plan")
                joint = a.run_role(_design_prompt(title, instructions, criteria), root,
                                   read_only=True, model=m)
                parts = _split_design(joint.get("summary", ""))
                d1 = {"summary": parts["standards"], "cost_usd_micros": joint.get("cost_usd_micros")}
                d2 = {"summary": parts["spec"], "cost_usd_micros": 0}
                d3 = {"summary": parts["plan"], "cost_usd_micros": 0}
                d4 = {"summary": parts["plan_review"], "cost_usd_micros": 0}
                if not parts["standards"]:
                    set_llm_context(stage="standards")
                    a, m = _route("standards")
                    d1 = a.run_role(_standards_prompt(), root, read_only=True, model=m)
                    d1["cost_usd_micros"] = int(d1.get("cost_usd_micros") or 0) + int(
                        joint.get("cost_usd_micros") or 0)
                if not parts["spec"]:
                    set_llm_context(stage="spec")
                    a, m = _route("spec")
                    d2 = a.run_role(_spec_prompt(title, instructions, criteria), root,
                                    read_only=True, model=m)
                if not parts["plan"]:
                    # The plan's own session self-checks too; its sections replace the joint
                    # answer's (which had no plan to review).
                    set_llm_context(stage="plan")
                    a, m = _route("plan")
                    d3 = a.run_role(_plan_prompt(title, d2.get("summary", "")), root,
                                    read_only=True, model=m)
                    plan_parts = _split_design(d3.get("summary", ""))
                    if plan_parts["plan"]:
                        d3 = {**d3, "summary": plan_parts["plan"]}
                    d4 = {"summary": plan_parts["plan_review"], "cost_usd_micros": 0}
                if not str(d4.get("summary") or "").strip():
                    # No self-check came back: the separate review session, exactly as before.
                    set_llm_context(stage="review_plan")
                    a, m = _route("review_plan")
                    d4 = a.run_role(_review_plan_prompt(title, d3.get("summary", "")), root,
                                    read_only=True, model=m)
                for d in (d1, d2, d3, d4):
                    cost += int(d.get("cost_usd_micros") or 0)
                # THE MARKER, NOT THE WORD (BOSSAPP-960). This was `"REVISE" not in summary`,
                # so "no need to revise anything" — or a hedge quoting the marker mid-prose —
                # read as a failed plan; measured false on EVERY run, including crun_12ae7cbf
                # which shipped PR #60 clean. The reviewer is ordered to end with `PLAN: OK` or
                # `PLAN: REVISE`; read that (last marker wins), and a review with no marker at
                # all counts as ok, which is what the old parse did for marker-free text.
                plan_ok = _plan_verdict(d4.get("summary", "")) != "revise"
                design = {"standards": d1.get("summary", ""), "spec": d2.get("summary", ""),
                          "plan": d3.get("summary", ""), "plan_review": d4.get("summary", ""),
                          "plan_ok": plan_ok,
                          # The partition BOSS runs the writing stages in parallel from
                          # (parallel_build); empty when the change is too small to split.
                          "packages": parts.get("packages", "")}
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
            if design.get("source") == "approved_prd":
                instructions = f"{instructions}{approved_prd_design.brief_addendum(instructions, design)}"
            else:
                instructions = (f"{instructions}\n\nREFINED SPEC:\n{design.get('spec', '')}"
                                f"\n\nTECHNICAL PLAN:\n{design.get('plan', '')}")
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
        if do_separate_tests and _seeded:
            # The seeded tree carries the QA-authored suite; it is the frozen oracle (and, being
            # pre-existing now, the existing-tests floor keeps the implementer off it).
            do_separate_tests = False
            with _transaction(engine) as c:
                emit_event(c, project_id, "code_task.tests_resumed", "boss",
                           "code_task_run", run_id,
                           {"skipped_stage": "write_tests", "seeded_from": _seed_from,
                            "existing_tests": len(preexisting_tests),
                            "why": "continuing from the previous attempt's saved workspace"})
        if (do_separate_tests and (_own_tests_done or (_is_repair and _app_tests))
                and (_own_tests_done or _lineage_wrote_tests)
                and ("write_tests" in _resume_done
                     or not _repair_needs_write_tests(
                         fctx.get("prior_failing_gates"),
                         prior_error=fctx.get("prior_error"),
                         tests_on_branch=bool(_app_tests)))):
            do_separate_tests = False
            with _transaction(engine) as c:
                emit_event(c, project_id, "code_task.tests_resumed", "boss",
                           "code_task_run", run_id,
                           {"skipped_stage": "write_tests",
                            "prior_failing_gates": list(fctx.get("prior_failing_gates") or []),
                            "existing_tests": len(preexisting_tests),
                            "why": ("no test-related gate failed, so the existing suite is the "
                                    "oracle for this repair")})

        # A LIGHT PLAN FOR A BUILD WITH NO DESIGN STAGE. The parallel split runs from the
        # design's WORK PACKAGES, and a direct build (tier < 2, no design chain) had none, so the
        # Notes Lite check ran as one session whatever the host could carry. One short read-only
        # call now produces just the partition, and only when the host allows 2+ streams.
        if (do_separate_tests and not use_microvm and not _seeded
                and not str((design or {}).get("packages") or "").strip()
                and _build_fan_out(engine, project_id, settings) >= 2):
            try:
                with _transaction(engine) as c:
                    _phase(c, "design")
                set_llm_context(stage="design")
                a_lp, m_lp = _route("plan")
                lp = a_lp.run_role(
                    _ctx_block(title, instructions, criteria)
                    + "You are planning how separate agents will write this change IN PARALLEL. "
                    "Read only what you need of the repository (a few files at most); do NOT "
                    "edit anything. Answer with the section below and nothing else."
                    + _parallel_build.design_prompt_section(),
                    root, read_only=True, model=m_lp, max_turns=8)
                cost += int(lp.get("cost_usd_micros") or 0)
                _lp_parts = _split_design(lp.get("summary", ""))
                _pk = _lp_parts.get("packages") or lp.get("summary", "")
                if _parallel_build.parse_packages(_pk):
                    design = {**(design or {}), "packages": _pk, "packages_source": "light_plan"}
                with _transaction(engine) as c:
                    emit_event(c, project_id, "code_task.light_plan", "boss", "code_task_run",
                               run_id, {"packages": len(_parallel_build.parse_packages(_pk))})
            except Exception:  # noqa: BLE001 — no partition means one session, never a failure
                _log_caught(logger)

        # THE TEMPLATE'S TESTS, PLACED BY BOSS (not typed by a model): before any agent runs.
        _placed_tpl_tests: list[str] = []
        if not use_microvm:
            _placed_tpl_tests = place_template_tests(
                Path(root), _template_tests_home(spec, fctx, instructions))
            if _placed_tpl_tests:
                with _transaction(engine) as c:
                    emit_event(c, project_id, "code_task.template_tests_placed", "boss",
                               "code_task_run", run_id,
                               {"count": len(_placed_tpl_tests), "paths": _placed_tpl_tests[:40]})

        def _run_split(agent, model, stage_prompt: str, fan: int, *, tests: bool, protect=None):
            """The writing stage split across the design's WORK PACKAGES, run in parallel CLI
            sessions (parallel_build), or None to run today's single session. Never raises:
            a split that cannot run falls back, it never fails the build."""
            packages = _parallel_build.parse_packages((design or {}).get("packages"))
            if not packages:
                return None

            def _event(kind: str, data: dict) -> None:
                try:
                    with _transaction(engine) as c:
                        emit_event(c, project_id, kind, "boss", "code_task_run", run_id, data)
                except Exception:  # noqa: BLE001 — the record never fails the build
                    _log_caught(logger)

            # SIZED AT THE STAGE'S START, FROM THE HOST AS IT IS NOW (build_streams): free
            # cores after load and other builds, free memory (cgroup, else /proc), the
            # project's min/max and the plan's part count. The choice and why are recorded on
            # the stage, so the console shows it and anyone can check it.
            stage_name = "write_tests" if tests else "implement"
            n_parts = len([p for p in packages if p.name != _parallel_build.SCAFFOLD
                           and (p.tests if tests else p.owns)])
            try:
                from . import build_streams as _streams

                with _transaction(engine) as c:
                    plan = _streams.for_stage(c, project_id, settings, packages=n_parts,
                                              exclude_run=run_id)
                    c.execute(sql(
                        "UPDATE code_task_runs SET state=jsonb_set(COALESCE(state,'{}'::jsonb), "
                        "'{streams}', COALESCE(state->'streams','{}'::jsonb) "
                        "|| jsonb_build_object(CAST(:st AS text), CAST(:p AS JSONB))), "
                        "updated_at=now() WHERE id=:id"),
                        {"st": stage_name, "p": json.dumps(plan.as_dict()), "id": run_id})
                    emit_event(c, project_id, "code_task.streams_chosen", "boss",
                               "code_task_run", run_id, {"stage": stage_name, **plan.as_dict()})
                fan = int(plan.n)
            except Exception:  # noqa: BLE001 — a sizing that fails keeps the caller's number
                _log_caught(logger)

            def _one(part_prompt: str) -> dict:
                # Each part is ONE agent: the parallelism is BOSS's, not a sub-agent's.
                return agent.run_role(part_prompt, root, model=model, parallel_agents=0)

            try:
                return _parallel_build.run_split(
                    run_role=_one, root=Path(root), base_prompt=stage_prompt, packages=packages,
                    parallel=fan, test_cmd=test_cmd, tests=tests, on_event=_event,
                    protect=protect)
            except Exception:  # noqa: BLE001 — fall back to the single session
                _log_caught(logger)
                _event("code_task.split_fell_back", {"why": "the split raised", "tests": tests})
                return None

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
                    iac=iac_task, web=_web_verify, app_root=_app_id, stack_note=_stack_note,
                ) + _placed_tests_note(_placed_tpl_tests)
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
                if _is_repair or _resumed:
                    existing = sorted(
                        str(p.relative_to(root)) for p in root.rglob("*")
                        if p.is_file() and _is_test_path(str(p.relative_to(root)))
                        and ".git/" not in str(p.relative_to(root))
                        and verification_target.in_app_scope(str(p.relative_to(root)), _app_scope))
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
                fan = _build_fan_out(engine, project_id, settings)
                # SPLIT BY BOSS, NOT LEFT TO THE MODEL (parallel_build): a first, from-scratch
                # pass with a design partition writes each part's suite in its own session, at
                # the same time. A repair or a resume augments an existing suite: one session.
                if (fan and not escalate and not _is_repair
                        and not getattr(sandbox, "resumed_from_checkpoint", False)):
                    split = _run_split(a, m, prompt, fan, tests=True)
                    if split is not None:
                        r = split.as_role_result()
                        cost += int(r.get("cost_usd_micros") or 0)
                        return r, [fc.path for fc in sandbox.diff() if _is_test_path(fc.path)
                                   and fc.path not in _placed_tpl_tests], None
                if not fan:
                    _record_split_off(engine, project_id, run_id, settings, "write_tests")
                r = a.run_role(prompt, root, model=m, parallel_agents=fan)
                cost += int(r.get("cost_usd_micros") or 0)
                return r, [fc.path for fc in sandbox.diff() if _is_test_path(fc.path)
                           and fc.path not in _placed_tpl_tests], None

            r1, test_files, test_changes = _attempt_write_tests(escalate=False)
            # On a REPAIR the tree ALREADY holds a passing suite, so "wrote no new test files"
            # is a legitimate outcome — the augment pass may judge coverage sufficient. Escalating
            # there is actively harmful: it re-issues the FROM-SCRATCH prompt on the frontier
            # model and re-authors a suite that already exists. Measured: augment pass 7 turns /
            # 23s / $0.16, then the escalation spent 31 turns / 356s / $1.84 on opus — worse than
            # the original from-scratch run it was meant to avoid. Only escalate when the tree has
            # NO tests at all.
            _repair_with_tests = _is_repair and bool(_app_tests)
            if not test_files and _repair_with_tests:
                test_files = list(_app_tests)
                with _transaction(engine) as c:
                    emit_event(c, project_id, "code_task.tests_reused", "boss",
                               "code_task_run", run_id,
                               {"step": "write_tests", "reason": "repair_augment_added_no_files",
                                "existing_tests": len(_app_tests),
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
            # THE TEST BUDGET, COUNTED BY BOSS (test_budget). The prompt states the cap; a suite
            # that is still over gets ONE trim turn on the test author's own warm session, and
            # what remains over is recorded on the build, never passed silently.
            if test_files and not use_microvm:
                _cap = test_budget(criteria, get_llm_context().get("test_budget"))
                # ONLY WHAT THIS BUILD WROTE, IN ITS APP (budget_files): crun_37f4ccef counted the
                # repo's 735 pre-existing cases of other apps against a budget of 20, and the
                # trim turn was told to delete them.
                _own_tests = budget_files(test_files, preexisting_tests,
                                          placed=_placed_tpl_tests, scope=_app_scope)
                _written = count_test_cases(root, _own_tests)
                _tb: dict = {"budget": _cap, "written": _written}
                if _written > _cap and (r1 or {}).get("session_id"):
                    try:
                        set_llm_context(stage="write_tests")
                        _a_tb, _m_tb = _route("write_tests")
                        _rt = _a_tb.run_role(_test_budget_trim_prompt(_written, _cap, _own_tests),
                                             root, model=(r1 or {}).get("model") or _m_tb,
                                             resume_session_id=r1["session_id"])
                        cost += int(_rt.get("cost_usd_micros") or 0)
                        if "TEST PLAN:" in str(_rt.get("summary") or ""):
                            r1 = {**r1, "summary": _rt["summary"]}
                        _kept = [p for p in test_files if p not in _own_tests]
                        _own_tests = budget_files(
                            [fc.path for fc in sandbox.diff() if _is_test_path(fc.path)],
                            preexisting_tests, placed=_placed_tpl_tests, scope=_app_scope)
                        test_files = _kept + [p for p in _own_tests if p not in _kept]
                        _tb["after_trim"] = count_test_cases(root, _own_tests)
                    except Exception:  # noqa: BLE001 — an untrimmed suite is recorded below
                        _log_caught(logger)
                _tb["over"] = int(_tb.get("after_trim", _written)) > _cap
                with _transaction(engine) as c:
                    c.execute(sql(
                        "UPDATE code_task_runs SET state=jsonb_set(COALESCE(state,'{}'::jsonb), "
                        "'{test_budget}', CAST(:t AS JSONB)), updated_at=now() WHERE id=:id"),
                        {"t": json.dumps(_tb), "id": run_id})
                    emit_event(c, project_id, "code_task.test_budget", "boss", "code_task_run",
                               run_id, _tb)
            # QA'S TESTS PASS THE APP'S OWN TYPECHECK AND LINT BEFORE THEY ARE FROZEN: one QA
            # turn on exactly the lines that fail, while QA may still edit them.
            _qa_static = _qa_static_check([p for p in test_files if p not in _placed_tpl_tests])
            if _qa_static:
                with _transaction(engine) as c:
                    _phase(c, "write_tests")
                    emit_event(c, project_id, "code_task.qa_static_fix", "boss", "code_task_run",
                               run_id, {"findings": _qa_static[:20], "count": len(_qa_static)})
                try:
                    set_llm_context(stage="write_tests")
                    _a_qs, _m_qs = _route("write_tests")
                    _rq = _a_qs.run_role(
                        _qa_static_fix_prompt(_qa_static), root,
                        model=(r1 or {}).get("model") or _m_qs,
                        resume_session_id=(r1 or {}).get("session_id"))
                    cost += int(_rq.get("cost_usd_micros") or 0)
                    test_files = list(dict.fromkeys(
                        list(test_files) + [fc.path for fc in sandbox.diff()
                                            if _is_test_path(fc.path)
                                            and fc.path not in _placed_tpl_tests]))
                    _left = _qa_static_check(
                        [p for p in test_files if p not in _placed_tpl_tests])
                    with _transaction(engine) as c:
                        emit_event(c, project_id, "code_task.qa_static_fixed", "boss",
                                   "code_task_run", run_id,
                                   {"before": len(_qa_static), "after": len(_left),
                                    "left": _left[:10]})
                except Exception:  # noqa: BLE001 — the gate still judges what QA left
                    _log_caught(logger)
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
                # The placed template tests are frozen with QA's own.
                test_files = list(test_files) + [p for p in _placed_tpl_tests
                                                 if p not in test_files]
                tests_before = _hash_files(root, test_files)
                # Snapshot the frozen tests so a tamper-then-retry starts from clean tests
                # (the repair loop restores them before re-invoking the implementer).
                tests_orig = {p: (root / p).read_text(errors="ignore") for p in test_files
                              if (root / p).exists()}
                # RED FIRST (red_first): the tree is the faulty code plus QA's new tests, and
                # nothing of the fix exists yet. A brief that says the regression test FAILS
                # on the faulty release is proven here, never taken on trust.
                if (red_first.demanded(criteria) and not _is_repair
                        and not getattr(sandbox, "resumed_from_checkpoint", False)):
                    try:
                        _rf = red_first.record(_run_verify(record=False),
                                               [p for p in test_files
                                                if p not in _placed_tpl_tests])
                        with _transaction(engine) as c:
                            c.execute(sql(
                                "UPDATE code_task_runs SET state=jsonb_set(COALESCE(state,"
                                "'{}'::jsonb), '{red_first}', CAST(:t AS JSONB)), "
                                "updated_at=now() WHERE id=:id"),
                                {"t": json.dumps(_rf), "id": run_id})
                            emit_event(c, project_id, "code_task.red_first", "boss",
                                       "code_task_run", run_id,
                                       {k: _rf[k] for k in ("failed", "reproduces", "failing",
                                                            "word", "exit_code")})
                    except Exception:  # noqa: BLE001 — unproven, never a failed build
                        _log_caught(logger)

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
                                iac=iac_task, web=_web_verify, app_root=_app_id)) + _stack_note \
            + _placed_tests_note(_placed_tpl_tests) \
            + (_test_plan_note((locals().get("r1") or {}).get("summary", ""))
               if do_separate_tests else "")
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
        # A "FRONTIER RETRY" ON THE SAME MODEL IS JUST ANOTHER COLD PASS. The Notes Lite check
        # (crun_3a39cd19) escalated implement from claude-sonnet-5 to claude-sonnet-5: six cold
        # minutes that changed nothing the warm fix round below could not. Escalate only when it
        # reaches a different engine or model.
        _esc_route: list = []   # the escalated route, resolved once when it is first needed
        _warm_fix = [False]     # the final attempt is a warm fix on the same model, not a "frontier" one
        # FAIL FAST: THE IMPLEMENT STAGE HAS A WALL-TIME BUDGET (build_implement_budget_minutes,
        # default 25), fix round and continuations included. Past it no new session starts, a
        # running one is ended (code_agent caps its timeout at the deadline), and the build fails
        # with its failing tests listed. The job's own timeout stays as the backstop.
        _impl_budget_s = _implement_budget_seconds(engine, project_id, settings)
        _impl_deadline = (time.time() + _impl_budget_s) if _impl_budget_s else None
        # THE WARM FIX HAS ITS OWN SLICE (implement_work_deadline): the attempts before it stop
        # WARM_FIX_RESERVE_SECONDS early, so the budget can no longer cut off the one turn
        # that fixes the failing tests on a warm session.
        _impl_work_deadline = implement_work_deadline(_impl_deadline, _impl_budget_s)
        _warm_fix_ran = [False]
        set_llm_context(stage_deadline=_impl_work_deadline)

        def _impl_over_budget(final: bool = False) -> bool:
            dl = _impl_deadline if (final or _warm_fix_ran[0]) else _impl_work_deadline
            # BOSSAPP-814 seam: time parked at the MCP gateway for a person is not spent.
            from . import stage_clock
            return dl is not None and time.time() >= stage_clock.extended(
                run_id, dl, since=_impl_deadline - _impl_budget_s)

        def _within_budget(call, model):
            """A session the deadline ended is a finished attempt (its tests then run), not a
            pipeline error."""
            try:
                return call()
            except RuntimeError as exc:
                if not (_impl_over_budget() and "timed out" in str(exc)):
                    raise
                _log_caught(logger)
                return {"summary": "(the implement stage reached its time limit)",
                        "cost_usd_micros": 0, "usage": {}, "model": model, "session_id": None}

        _cost_extra = [0]

        def _install_until_it_passes(agent, model, session_id, stage: str):
            """EDIT -> BOSS'S EXACT INSTALL -> REPEAT, IN THE SAME WARM SESSION, BEFORE ANY GATE.
            A failed install goes straight back to the session that wrote the tree, with npm's
            error and one instruction, and BOSS installs again after every reply, until it passes,
            the rounds run out or the implement budget ends. So the agent cannot hand off a
            workspace BOSS cannot install (HelpDesk AI: crun_6eaa478dc33a42c6bdb7 and its retry
            crun_1fdfea1c9c904a8083dd each fixed one conflict after BOSS's last check). Returns
            the session id to continue with."""
            sid = [session_id]

            def _emit(kind, data):
                with _transaction(engine) as c:
                    emit_event(c, project_id, kind, "boss", "code_task_run", run_id,
                               {"stage": stage, **data})

            def _ask(prompt) -> bool:
                if not sid[0]:
                    return False
                try:
                    rc = agent.run_role(prompt, root, model=model, resume_session_id=sid[0])
                except Exception:  # noqa: BLE001 — the verify that follows names the failure
                    _log_caught(logger)
                    return False
                _cost_extra[0] += int(rc.get("cost_usd_micros") or 0)
                sid[0] = rc.get("session_id") or sid[0]
                return True

            verification_target.install_rounds(_install_check, _ask,
                                               over_budget=_impl_over_budget, emit=_emit)
            return sid[0]

        def _remember_impl_session(agent, model) -> None:
            """The implement session that wrote this run's tree, on the run row
            (state.impl_session): what a repair child of this lineage resumes
            (carried_implement_session) instead of cold-starting — BOSSAPP-1001. Recorded with
            the engine and model the session ran on, so the child resumes only the SAME route.
            Best-effort bookkeeping, never fatal; reads the enclosing impl_session_id."""
            if not impl_session_id:
                return
            try:
                rec = {"session_id": str(impl_session_id),
                       "engine": str(getattr(agent, "engine", "") or ""),
                       "model": str(model or ""),
                       "native": not str(impl_session_id).startswith(SESSION_PREFIX)}
                with _transaction(engine) as c:
                    c.execute(sql(
                        "UPDATE code_task_runs SET state=jsonb_set(COALESCE(state,'{}'::jsonb), "
                        "'{impl_session}', CAST(:s AS JSONB)), updated_at=now() WHERE id=:id"),
                        {"s": json.dumps(rec), "id": run_id})
            except Exception:  # noqa: BLE001 — bookkeeping never fails a run
                _log_caught(logger)

        test_arbitrations: list[dict] = []  # CODE_WRONG/TEST_WRONG rulings on failing tests

        def _arbitrate_tests(test_output: str, round_i) -> str:
            """TEST ARBITRATION: before an implementer chases the oracle, establish that the
            oracle is right. An over-specified test is unsatisfiable, and every round spent
            against it is wasted. A `test_wrong` ruling goes to the test-author lane ONLY (never
            the implementer's session, so `coder_did_not_edit_tests` keeps meaning). Returns the
            ruling. One function for the warm round and the same-model warm fix of implement."""
            nonlocal cost
            with _transaction(engine) as c:
                _phase(c, "test_arbitration")
            # The arbiter is ledgered under its OWN stage (BOSSAPP-998): booked as review_code
            # it overstated review spend and made arbiter spend invisible. It still RUNS on the
            # review_code route (same reviewer lane), and _route stamps that route_id on the
            # context, so the row keeps its route.
            set_llm_context(routes=_route_ids(), db_engine=engine, project_id=project_id,
                            run_id=run_id, stage="test_arbitration")
            a_arb, m_arb = _route("review_code")
            r_arb = a_arb.run_role(
                _test_arbiter_prompt(
                    title, criteria, instructions=instructions, test_output=test_output,
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
                with _transaction(engine) as c:
                    _phase(c, "write_tests")
                set_llm_context(routes=_route_ids(), db_engine=engine, project_id=project_id,
                                run_id=run_id, stage="write_tests")
                a_ta, m_ta = _route("write_tests")
                r_ta = a_ta.run_role(
                    _test_amend_prompt(criteria, target, r_arb.get("summary", "")),
                    root, model=m_ta)
                cost += int(r_ta.get("cost_usd_micros") or 0)
            return ruling

        _budget_exhausted = [False]
        baseline_record = None
        for attempt in range(max_repair + 2):
            _impl_ran[0] = True
            baseline_record = None
            if _warm_fix[0] and attempt:
                # This attempt IS the warm fix: it runs in the reserved slice.
                _warm_fix_ran[0] = True
                set_llm_context(stage_deadline=_impl_deadline)
            escalate_impl = attempt == max_repair + 1 and not _warm_fix[0]
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
            _cross_run_repair = _is_repair and attempt == 0
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
                    # THE WHOLE ITERATION FEEDBACK, not only the note: the person's note, the
                    # previous attempt's failing gates and verify output tail
                    # (code_task_service.previous_failure_block) and the reviewers' findings.
                    _hist = fctx.get("feedback_history") or [{}]
                    _ev = ((instructions.split("=== ITERATION FEEDBACK", 1)[-1]
                            .split("\n", 1)[-1].strip()
                            if "=== ITERATION FEEDBACK" in instructions else "")
                           or _hist[-1].get("fix_note")
                           or "(see the iteration feedback in the task instructions)")
                impl_prompt = _repair_prompt(base_impl_prompt, _ev, max(1, attempt))
                _repair_evidence = _ev
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
                    _log_caught(logger)
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
                a, m = ((tuple(_esc_route) if _esc_route else _route_escalated("implement"))
                        if escalate_impl else _route("implement"))
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
                # A CROSS-RUN REPAIR RESUMES THE PARENT ATTEMPT'S SESSION (BOSSAPP-1001). The
                # child's first pass IS the repair, and the parent's implement session already
                # holds the task, the repo and its own work warm — a cold start here re-paid the
                # CLI preamble plus full repo re-onboarding every round (audit BOSSAPP-997).
                # Same role only, same engine+model only, native sessions only
                # (carried_implement_session); anything else keeps today's cold start.
                _carried_resume = False
                if resume_sid is None and _cross_run_repair and not escalate_impl:
                    resume_sid = carried_implement_session(
                        fctx, agent=a, model=m,
                        stage="implement" if do_separate_tests else "solo")
                    _carried_resume = bool(resume_sid)
                if resume_sid:
                    try:
                        r2 = a.run_role(
                            (_cross_run_resume_prompt(_repair_evidence, max(1, attempt))
                             + (_guidance_block(_steer) if _steer else "")
                             if _carried_resume
                             else _repair_resume_prompt(evidence_text, attempt)), root,
                            model=m, resume_session_id=resume_sid)
                        if _carried_resume:
                            _from = (fctx.get("resume_implement_session") or {}).get("from_run")
                            with _transaction(engine) as c:
                                emit_event(c, project_id, "code_task.implement_session_resumed",
                                           "boss", "code_task_run", run_id,
                                           {"from_run": _from, "session_id": resume_sid,
                                            # The engine's word, not BOSS's: a resume an engine
                                            # quietly answered with a NEW session shows here and
                                            # in the ledger's session ids.
                                            "reused": r2.get("session_id") == resume_sid})
                    except Exception:
                        _log_caught(logger)
                        if _carried_resume:
                            # HONESTY RULE: a failed resume (expired session, engine error)
                            # falls back to the cold start it does today — the degradation
                            # re-adds only the old cost, and says so.
                            try:
                                with _transaction(engine) as c:
                                    emit_event(c, project_id,
                                               "code_task.implement_resume_fell_back", "boss",
                                               "code_task_run", run_id,
                                               {"from_run": (fctx.get("resume_implement_session")
                                                             or {}).get("from_run"),
                                                "why": "the engine could not resume the parent "
                                                       "attempt's session; cold start instead"})
                            except Exception:  # noqa: BLE001 — the fallback still runs
                                _log_caught(logger)
                        if _impl_over_budget():
                            r2 = {"summary": "(the implement stage reached its time limit)",
                                  "cost_usd_micros": 0, "usage": {}, "model": m,
                                  "session_id": None}
                        else:
                            r2 = a.run_role(impl_prompt, root, model=m,
                                            parallel_agents=_build_fan_out(engine, project_id,
                                                                           settings))
                else:
                    # The first implement fans out (build_speed); a repair resumed on its own
                    # session stays one agent: it is a focused fix, not parallel volume.
                    fan = _build_fan_out(engine, project_id, settings)
                    split = None
                    if not fan and attempt == 0:
                        _record_split_off(engine, project_id, run_id, settings, "implement")
                    if fan and attempt == 0 and not escalate_impl:
                        # SPLIT BY BOSS (parallel_build), after the tamper snapshot: no part
                        # may touch QA's frozen tests even inside the paths it owns.
                        _frozen = set(test_files or []) if do_separate_tests else set()
                        split = _run_split(a, m, impl_prompt, fan, tests=False,
                                           protect=(lambda rel: rel in _frozen))
                    r2 = (split.as_role_result() if split is not None
                          else _within_budget(lambda: a.run_role(impl_prompt, root, model=m,
                                                                 parallel_agents=fan), m))
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
                       and _cont < _max_cont and impl_session_id and not _impl_over_budget()):
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
                        _log_caught(logger)
                        break
                    cost += int(rc.get("cost_usd_micros") or 0)
                    impl_session_id = rc.get("session_id") or impl_session_id
                    r2 = {**r2, "summary": (rc.get("summary") or r2.get("summary", ""))}
                # BOSS's own install passes before the hand-off is judged (or the rounds end).
                impl_session_id = _install_until_it_passes(a, m, impl_session_id, "implement")
                _remember_impl_session(a, m)
                cost += _cost_extra[0]
                _cost_extra[0] = 0
                # A FIX ROUND'S REQUIRED INPUTS (BOSSAPP-948, local_review.fix_brief): the brief
                # numbered a person's line comments; the implementer's RESPONSE lines become its
                # reply on each thread (Fixed with the commit, or Disputed with a reason).
                _required_human = [rc_ for rc_ in _open_operator_comments(engine, run_id, project_id)
                                   if str(getattr(rc_, "ref", "") or "").startswith("lcc_")]
                if _required_human:
                    _record_comment_responses(
                        engine, run_id, project_id,
                        review_comments.implementer_order(_required_human),
                        r2.get("summary") or "", round_i=attempt,
                        changed_files=[fc.path for fc in sandbox.diff()])
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
            # L1 baseline-aware verification: a failing test that also fails on the base commit
            # (pre_existing) or errors before any assertion ran (environment) is never the
            # change's fault — the gate decides on caused failures only, and a round is never
            # spent chasing a failure the change did not introduce.
            if not tests_passed and impl_files and tests_untouched:
                try:
                    baseline_record = classify_against_base(
                        project_id=project_id, root=root, base_branch=base_branch,
                        test_cmd=test_cmd, fail_output=fail_output,
                        cwd_rel=last_verify_cwd or ".", engine=engine)
                except Exception:  # noqa: BLE001 — baseline classification never blocks a build
                    _log_caught(logger)
                    baseline_record = None
                if baseline_record is not None:
                    with _transaction(engine) as c:
                        c.execute(sql(
                            "UPDATE code_task_runs SET state=jsonb_set(COALESCE(state,'{}'::jsonb), "
                            "'{baseline}', CAST(:b AS JSONB)), updated_at=now() WHERE id=:id"),
                            {"b": json.dumps(baseline_record.as_dict()), "id": run_id})
                        emit_event(c, project_id, "code_task.baseline_classified", "boss",
                                   "code_task_run", run_id,
                                   {"attempt": attempt, "caused": len(baseline_record.caused),
                                    "pre_existing": len(baseline_record.pre_existing),
                                    "environment": len(baseline_record.environment)})
                    _record_environment_signatures(engine, project_id, run_id, baseline_record)
            gate_tests_passed = tests_passed or bool(
                baseline_record is not None and not baseline_record.caused)
            ok = gate_tests_passed and tests_untouched and bool(impl_files)
            repair_log.append({"attempt": attempt, "tests_passed": tests_passed,
                               "tests_untouched": tests_untouched,
                               "impl_files": len(impl_files),
                               "cost_usd_micros": int(r2.get("cost_usd_micros") or 0)})
            learning_writeback.observe(engine, project_id=project_id, hook="repair_checkpoint",
                                       run_id=run_id, attempt=attempt, passed=ok,
                                       tests_passed=tests_passed, output=fail_output,
                                       command=test_cmd, failing=failing_tests(fail_output),
                                       files=impl_files, summary=r2.get("summary"))
            if escalate_impl and ok:
                escalated_recovered = True
                with _transaction(engine) as c:
                    emit_event(c, project_id, "code_task.step_recovered", "boss",
                               "code_task_run", run_id,
                               {"step": "implement", "recovered_by": "frontier_retry",
                                "attempt": attempt})
            if ok or attempt == max_repair + 1 or _warm_fix_ran[0]:
                break
            _can_warm_fix = (not use_microvm and bool(impl_session_id)
                             and not _impl_over_budget(final=True))
            if _impl_over_budget() and _can_warm_fix and not _warm_fix[0]:
                # PAST THE WORK SLICE, INSIDE THE RESERVE: no further cold attempt, one warm fix.
                _warm_fix[0] = True
                with _transaction(engine) as c:
                    emit_event(c, project_id, "code_task.warm_fix_reserved", "boss",
                               "code_task_run", run_id,
                               {"stage": "implement", "attempt": attempt,
                                "reserve_seconds": WARM_FIX_RESERVE_SECONDS,
                                "budget_minutes": _impl_budget_s // 60})
            elif _impl_over_budget():
                # No fix round past the budget: fail now, with the failing tests named.
                _budget_exhausted[0] = True
                with _transaction(engine) as c:
                    emit_event(c, project_id, "code_task.stage_budget_exhausted", "boss",
                               "code_task_run", run_id,
                               {"stage": "implement", "budget_minutes": _impl_budget_s // 60,
                                "attempt": attempt, "failing_tests": failing_tests(fail_output)})
                break
            if attempt == max_repair and not use_microvm and not _warm_fix[0]:
                _before = dict(stage_routes.get("implement") or {})
                try:
                    _esc_route[:] = list(_route_escalated("implement"))
                    _after = stage_routes.get("implement") or {}
                    _same = (_after.get("engine"), _after.get("model")) == (
                        _before.get("engine"), _before.get("model"))
                except Exception:  # noqa: BLE001 — unknown routes keep the escalation
                    _log_caught(logger)
                    _same = False
                if _same:
                    # THE FIX ROUND HAPPENS HERE, BEFORE ANY REVIEW: the same session, warm, with
                    # the failing tests as its evidence. A cold "frontier" pass on the same model
                    # bought nothing, and reviewing code whose tests fail cost the Notes Lite
                    # check 12 minutes of panel time on a build that then failed anyway.
                    _warm_fix[0] = True
                    # THE ENGINE AND THE MODEL THAT RAN, never null: a route that leaves the
                    # model to the engine's default reports what the last attempt ran on.
                    with _transaction(engine) as c:
                        emit_event(c, project_id, "code_task.escalation_skipped", "boss",
                                   "code_task_run", run_id,
                                   {"step": "implement", "reason": "same_model",
                                    **escalation_route_record(_before, r2),
                                    "instead": "warm_fix_round"})
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
                        _ws_write(root / p, content)
            elif _install_fail[0] and not use_microvm:
                # BOSS's clean install failed: nothing else ran. Said as that, with npm's error
                # and what to do, not as failing tests to fix "in the implementation only".
                evidence_text = verification_target.install_repair_evidence(
                    _install_fail[0], fail_output)
                with _transaction(engine) as c:
                    emit_event(c, project_id, "code_task.install_failed", "boss",
                               "code_task_run", run_id,
                               {"stage": "implement", "attempt": attempt,
                                "command": _install_fail[0].get("command"),
                                "summary": _install_fail[0].get("summary")})
            elif baseline_record is not None and baseline_record.caused:
                # Only the failures the change actually caused go to the repair prompt — a
                # pre_existing or environment failure in the same run is never the implementer's
                # to chase.
                _caused_text = "\n\n".join(
                    f"$ {test_cmd} (exit {test_exit_code}) — {c.test_id}\n{c.evidence[-3000:]}"
                    for c in baseline_record.caused)
                evidence_text = verification_target.attach_failing_sources(_caused_text, root)
            else:
                # STRUCTURED FINDINGS, NOT A STDOUT TAIL (BOSSAPP-960): which test failed, on
                # which assertion, in which file — parsed once here (repair_findings), rendered
                # in the review channel's FINDING grammar. Falls back to the legacy tail when
                # nothing parses, so unparseable evidence still arrives.
                evidence_text = verification_target.attach_failing_sources(
                    repair_findings.evidence_block(
                        fail_output, command=test_cmd, exit_code=test_exit_code),
                    root,
                )
                if (_warm_fix[0] and do_separate_tests and not use_microvm
                        and getattr(settings, "code_test_arbiter_enabled", True)):
                    # THE ARBITER BEFORE THE SAME-MODEL WARM FIX: the implementer may not edit
                    # tests, so a wrong test (one that fails typecheck, or asks for what the
                    # criteria never said) is the test author's to amend, never a fix to chase.
                    try:
                        if _arbitrate_tests(fail_output[-6000:], f"implement-{attempt}") == "test_wrong":
                            # The oracle was amended by its author: it is the frozen spec now.
                            tests_before = _hash_files(root, test_files)
                            tests_orig = {p: (root / p).read_text(errors="ignore")
                                          for p in test_files if (root / p).exists()}
                            evidence_text = (
                                "The test author amended the tests the arbiter ruled wrong. Run "
                                "the tests again and fix what still fails, in the "
                                "implementation only.\n" + evidence_text)
                        else:
                            evidence_text = ("The arbiter confirmed these tests are CORRECT: fix "
                                             "the implementation, do NOT touch the tests.\n"
                                             + evidence_text)
                    except Exception:  # noqa: BLE001 — the warm fix runs with the plain evidence
                        _log_caught(logger)

        # The implement budget ends with the stage: later writing turns are not cut by it.
        set_llm_context(stage_deadline=None)
        if _budget_exhausted[0] or (_impl_over_budget(final=True) and not tests_passed):
            with _transaction(engine) as c:
                c.execute(sql(
                    "UPDATE code_task_runs SET state=jsonb_set(COALESCE(state,'{}'::jsonb), "
                    "'{stage_budget}', CAST(:b AS JSONB)), updated_at=now() WHERE id=:id"),
                    {"b": json.dumps({"stage": "implement", "exhausted": True,
                                      "budget_minutes": _impl_budget_s // 60,
                                      "failing_tests": failing_tests(fail_output)}),
                     "id": run_id})
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
            _log_caught(logger)
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
                _log_caught(logger)
                q_ok = False
            reverted = False
            if not q_ok and snapshot:
                for _qp, _qc in snapshot.items():
                    try:
                        _ws_write(root / _qp, _qc)
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
                _log_caught(logger)
                pass

        # A REPAIR IS NEVER REVIEWED WITHOUT THE WORK IT REPAIRS (repair_lost_the_work): a
        # harness verdict now, not review rounds and gates spent on an empty change.
        if _is_repair and not use_microvm:
            def _load_prev(rid: str) -> Optional[dict]:
                with _transaction(engine) as c:
                    return load_code_task_run(c, rid)
            _lost = repair_lost_the_work(previous_attempt_paths(_load_prev, fctx), root,
                                         scope=_app_scope)
            if _lost:
                with _transaction(engine) as c:
                    _release(budget, c, project_id, reservation_id,
                             "repair without the work it repairs", run_id=run_id)
                    emit_event(c, project_id, "code_task.repair_lost_the_work", "boss",
                               "code_task_run", run_id,
                               {"parent_run_id": fctx.get("parent_run_id"),
                                "app_scope": _app_scope})
                    return _fail(c, run_id, project_id, _lost, failure_class="infra",
                                 reason_code="repair_without_the_work")

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
        panel_size = 1
        review_seat = None  # the panel seat whose findings stand for the review (§6.4)
        review_cost = 0
        mutation_caught = None
        # The probe's mutant, kept so a fix round re-probes the SAME bug (mutation_probe).
        _mutant: dict = {}
        # PASS 1 DOES NOT OWE TEST STRENGTH (mutation_probe.strength_is_advisory): its tests
        # are told no P2, so a surviving mutant is information, never a failed gate or a round.
        from .build_scope import more_tests_pass as _more_tests_pass
        from . import mutation_probe
        _strength_advisory = mutation_probe.strength_is_advisory(
            more_tests_pass=_more_tests_pass())
        regressions_ok = None
        panel_parallel = False
        # A CHANGE TO CI IS ALWAYS REVIEWED, whatever the risk tier (services/ci_gate.py): the
        # adversarial reviewer gets the workflow checklist, the security seat sits, and BOSS's own
        # deterministic workflow checks are added to the review's findings.
        try:
            _ci_files = ci_gate.workflow_changes(
                accumulated if use_microvm else {fc.path: fc.content for fc in sandbox.diff()})
        except Exception:  # noqa: BLE001 — an unreadable diff is reviewed as tiered
            _log_caught(logger)
            _ci_files = {}
        _ci_static = ci_gate.findings_block(ci_gate.static_review(_ci_files)) if _ci_files else ""
        if _ci_files and not do_review:
            do_review = True
            with _transaction(engine) as c:
                emit_event(c, project_id, "code_task.ci_change_reviewed", "boss", "code_task_run",
                           run_id, {"files": list(_ci_files), "risk_tier": risk_tier,
                                    "reason": "a change to .github/workflows/ is always reviewed"})
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
                                         work_branch=work_branch, token=token, egress=egress_access)
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
            # THE WHOLE CHANGE (review_input.build): the live diff plus every path the lineage
            # changed, so a seeded attempt's reviewer is not handed "(no file changes detected)".
            _review_change = _review_change_now()
            change_block = _change_block_with_integrity(_review_change)

            # AUTO-ESCALATION (acceptance criterion #3): the pre-flight estimate can be
            # wrong — re-check the SAME caps against the real diff now that the fix lane
            # has actually written one, and hand off to the full pipeline's review panel
            # if it blew the budget, carrying the fix lane's red test forward.
            if lane == fix_lane_triage.LANE_FIX:
                _esc = fix_lane_runner.check_escalation(
                    spec, files=len(_review_change.paths),
                    lines=sum(c.count("\n") + 1 for c in _review_change.files.values()),
                    touched_paths=_review_change.paths,
                    carried_test=fctx.get("red_first"))
                if _esc.escalated:
                    lane = fix_lane_triage.LANE_FULL
                    fctx["lane"] = lane
                    fctx["lane_escalated"] = True
                    fctx["lane_escalation_reason"] = _esc.reason
                    fctx["carried_failing_test"] = _esc.carried_test
                    with _transaction(engine) as c:
                        emit_event(c, project_id, "code_task.fix_lane_escalated", "boss",
                                   "code_task_run", run_id,
                                   {"reason": _esc.reason, "files": len(_review_change.paths)})

            # A REPAIR RE-REVIEWS WHAT IT CHANGED (repair_review_scope). crun_b32b4a26, the
            # repair of HelpDesk's SLA timers, changed four files and its first panel re-read the
            # whole 21-file change: review_code and consistency_check ~7 min each, to confirm
            # fixes. Each scopable seat now judges the repair's delta against the change the
            # parent's seat last reviewed, with its own findings to confirm — unless a guardrail
            # (no prior review, a big repair, files nobody named) sends it to the full review.
            # Same seats, same model routing. security_check scopes too (BOSSAPP-1000): it
            # always re-read the FULL change on repairs, the single costliest habit the audit
            # measured in the panel; the same guardrails send a doubtful repair to the full
            # security review.
            _scope_blocks = _repair_review_scope_blocks(
                engine, run_id=run_id, project_id=project_id, fctx=fctx,
                change=_review_change, baseline=_seed_baseline,
                enabled=(lane != fix_lane_triage.LANE_FIX))

            def _make_review(seat, seat_agent, seat_model):
                def _run():
                    # Each seat of a real panel signs its own act (factory §6.4); a panel of
                    # one is just the review.
                    set_llm_context(routes=_route_ids(), db_engine=engine, project_id=project_id, run_id=run_id,
                                    stage="review_code",
                                    seat=(seat + 1) if panel_size > 1 else None)
                    _seat_prompt = _review_prompt(title, criteria, instructions=instructions,
                                                  ui=ui_task,
                                                  change=_scope_blocks.get("review_code")
                                                  or change_block,
                                                  operator=_operator_review_block(
                                                      engine, run_id, project_id))
                    if lane == fix_lane_triage.LANE_FIX:
                        # ONE combined correctness+security reviewer (acceptance criterion
                        # #2) instead of separate review_code/security_check seats.
                        _seat_prompt = fix_lane_runner.combined_review_prompt(
                            _seat_prompt, _security_prompt(title, instructions=instructions,
                                                           criteria=criteria, change=change_block))
                    rr, hollow = _judge_seat("review_code", lambda pre: seat_agent.run_role(
                        pre + _seat_prompt, review_root, read_only=True, model=seat_model))
                    with lock:
                        panel_cost["v"] += int(rr.get("cost_usd_micros") or 0)
                        if hollow:
                            _hollow_seats["review_code"] = hollow
                    # A review that never saw the change casts no vote: never `survived`.
                    panel["review_votes"][seat] = (
                        rr, "not_run" if hollow else _parse_verdict(rr.get("summary", "")))
                return _run

            review_checks = [_make_review(i, a, m) for i, (a, m) in enumerate(review_members)]

            def _do_security():
                set_llm_context(routes=_route_ids(), db_engine=engine, project_id=project_id, run_id=run_id,
                                stage="security_check")
                rr, hollow = _judge_seat("security_check", lambda pre: security_agent.run_role(
                    pre + _security_prompt(title, instructions=instructions, criteria=criteria,
                                           change=_scope_blocks.get("security_check")
                                           or change_block),
                    review_root, read_only=True, model=security_model))
                with lock:
                    panel_cost["v"] += int(rr.get("cost_usd_micros") or 0)
                    if hollow:
                        _hollow_seats["security_check"] = hollow
                # A security check that never saw the change is not `clean`.
                panel["security"] = (rr, "hollow" if hollow
                                     else _parse_security(rr.get("summary", "")))

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
                rr, hollow = _judge_seat("consistency_check", lambda pre: consistency_agent.run_role(
                    pre + _consistency_prompt(title, criteria, design, instructions=instructions,
                                              change=_scope_blocks.get("consistency_check")
                                              or change_block),
                    review_root, read_only=True, model=consistency_model))
                with lock:
                    panel_cost["v"] += int(rr.get("cost_usd_micros") or 0)
                    if hollow:
                        _hollow_seats["consistency_check"] = hollow
                panel["consistency"] = (rr, "hollow" if hollow
                                        else _parse_consistency(rr.get("summary", "")))

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
            enabled_seats = review_panel_seats(lane, risk_tier, profile, ci_files=bool(_ci_files))

            # ONE SESSION SITS EVERY LLM SEAT (BOSSAPP-1000, audit 997 §7.3). The three seats
            # were three separate full CLI sessions over the SAME inline change — $375.98/month
            # of the panel's $389.91 — each paying its own repo onboarding to judge a text the
            # others had already read. One session (review_panel.combined_prompt) now reads the
            # change once and casts every enabled seat's verdict in its own marked section,
            # parsed by the SAME review_verdict grammar; a section the session does not deliver
            # is judged by that seat's own session, exactly as before. A refute panel of k > 1
            # keeps its independent review sessions (the votes must not share a context), and
            # the fix lane keeps its own combined reviewer.
            _merged_seats = [s for s in review_panel.SEATS if s in enabled_seats]
            _merge_panel = (panel_size == 1 and lane != fix_lane_triage.LANE_FIX
                            and len(_merged_seats) >= 2)

            def _do_merged_panel():
                set_llm_context(routes=_route_ids(), db_engine=engine, project_id=project_id,
                                run_id=run_id, stage=review_panel.STAGE)
                sections: list = []
                if "review_code" in _merged_seats:
                    _rc_scoped = _scope_blocks.get("review_code")
                    sections.append(("review_code", _review_prompt(
                        title, criteria, instructions=instructions, ui=ui_task,
                        change=_rc_scoped or "", ctx=False,
                        operator=_operator_review_block(engine, run_id, project_id))
                        + ("" if _rc_scoped else ci_gate.review_clause_for(change_block))))
                if "security_check" in _merged_seats:
                    sections.append(("security_check", _security_prompt(
                        title, instructions=instructions, criteria=criteria,
                        change=_scope_blocks.get("security_check") or "", ctx=False)))
                if "consistency_check" in _merged_seats:
                    sections.append(("consistency_check", _consistency_prompt(
                        title, criteria, design, instructions=instructions,
                        change=_scope_blocks.get("consistency_check") or "", ctx=False)))
                # The task context and the change are in the prompt ONCE (the whole point);
                # a repair-scoped seat carries its own delta block inside its section instead.
                _shared = [s for s, _ in sections if not _scope_blocks.get(s)]
                common = (_ctx_block(title, instructions, criteria)
                          + (_change_clause(change_block) if _shared else ""))
                rr, hollow = _judge_seat(review_panel.STAGE, lambda pre: review_agent.run_role(
                    pre + review_panel.combined_prompt(sections, common=common),
                    review_root, read_only=True, model=review_model))
                with lock:
                    panel_cost["v"] += int(rr.get("cost_usd_micros") or 0)
                parts = review_panel.split(rr.get("summary", ""), [s for s, _ in sections])
                fallback: list = []
                for seat in [s for s, _ in sections]:
                    section = (parts.get(seat) or "").strip()
                    if hollow:
                        # One session, one sight of the change: a session that never saw it is
                        # hollow for EVERY seat it sat — recorded as not reviewed, never a pass.
                        with lock:
                            _hollow_seats[seat] = hollow
                        section = rr.get("summary", "")
                    elif not section:
                        fallback.append(seat)
                        continue
                    if seat == "review_code":
                        # The merged session's cost rides the review seat's result, so the
                        # evidence books one session once (the other seats record 0).
                        panel["review_votes"][0] = (
                            {**rr, "summary": section},
                            "not_run" if hollow else _parse_verdict(section))
                    elif seat == "security_check":
                        panel["security"] = (
                            {"summary": section, "cost_usd_micros": 0,
                             "model": rr.get("model")},
                            "hollow" if hollow else _parse_security(section))
                    else:
                        panel["consistency"] = (
                            {"summary": section, "cost_usd_micros": 0,
                             "model": rr.get("model")},
                            "hollow" if hollow else _parse_consistency(section))
                for seat in fallback:  # a missing section: that seat's own session, as before
                    if seat == "review_code":
                        review_checks[0]()
                    elif seat == "security_check":
                        _do_security()
                    else:
                        _do_consistency()
                _merged_record.update(
                    {"seats": [s for s, _ in sections], "fallback": fallback,
                     "hollow": bool(hollow),
                     "cost_usd_micros": int(rr.get("cost_usd_micros") or 0)})
                try:
                    with _transaction(engine) as c:
                        emit_event(c, project_id, "code_task.review_panel_merged", "boss",
                                   "code_task_run", run_id, dict(_merged_record))
                except Exception:  # noqa: BLE001 — the record must not sink the panel
                    _log_caught(logger)

            # ONLY THE SEATS THAT ARE ENABLED RUN, and the pool is sized from them. Every seat
            # used to be handed in regardless, while the pool was sized from the enabled
            # checks: after review_skipped_tests_failing the "skipped" seats ran one after
            # another for up to 480 s (crun_decc93: skip 22:49:14, review_degraded 22:57:15).
            guarded = panel_seats(enabled_seats, review_checks, security=_do_security,
                                  regression=_do_full, consistency=_do_consistency,
                                  merged=_do_merged_panel if _merge_panel else None)
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
            max_workers = effective_review_lanes(_lanes, panel_size=len(guarded))
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
                _log_caught(logger)
                pass
            # Safe defaults so a check that times out / errors DEGRADES instead of crashing or
            # auto-passing: security 'not_run' still BLOCKS (security_clean requires 'clean');
            # adversarial/consistency 'not_run' proceed (they block only on refuted/drift).
            panel.setdefault("security", (None, "not_run"))
            panel.setdefault("consistency", (None, "not_run"))
            panel.setdefault("full", True)
            review_errors: list[str] = []

            max_workers = max(1, min(max_workers, len(guarded) or 1))
            panel_parallel = max_workers > 1
            # A DEADLINE PER SEAT, started when that seat starts (review_seats.run_seats): a
            # seat queued behind a slow one is not charged for the wait. A seat past it has its
            # own processes ended (it is a `claude -p` subprocess; Future.cancel() never stopped
            # a running one) and its slot freed. Unfinished checks keep their safe defaults.
            # The merged panel seat keeps the SAME deadline: it reads the change once and
            # writes three short verdicts (the slowest single seat measured 336-348 s against
            # a 900 s default), and its hollow/not_run degradation is the same as any seat's.
            review_deadline = int(getattr(settings, "code_agent_review_timeout_seconds", 900) or 900)
            _seat_run = _run_seats(
                guarded, max_workers=max_workers, seat_deadline_s=review_deadline,
                owner_id=lambda label: f"{run_id}:{label}",
                kill_strays=lambda: _kill_stray_review_processes(root, review_root))
            review_errors.extend(_seat_run.errors.values())
            if _seat_run.timed_out:
                review_errors.append(
                    f"{len(_seat_run.timed_out)} review check(s) exceeded {review_deadline}s "
                    f"({', '.join(_seat_run.timed_out)}) — degraded")
                review_errors.append(
                    f"terminated {_seat_run.killed} stuck review process(es) past the deadline"
                    if _seat_run.killed else
                    "no stuck review process found to terminate (already exited)")
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
            if _ci_static:
                r3 = {**r3, "summary": (r3.get("summary") or "") + _ci_static}
            r4 = r4 or _empty
            r6 = r6 or _empty
            regressions_ok = panel["full"]

            # check tests (mutation) — plant one bug, expect the tests to FAIL. THE PLANT IS
            # CODE, NOT A MODEL (BOSSAPP-1004, audit 997 §7.7): a write-capable model session
            # used to flip the one operator (87 calls, $13.93/month); the re-probe already
            # replanted deterministically, so the first plant now does too
            # (mutation_probe.plant_first) — no model call, no write-capable session in the
            # panel. In the micro-VM substrate the suite runs against the mutant in ITS OWN VM
            # (echo engine: clone + apply the mutant + run the suite) and the mutant is simply
            # never committed — nothing to restore.
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
                _mu_read = ((lambda p: accumulated.get(p)) if use_microvm
                            else (lambda p: (root / p).read_text(errors="ignore")
                                  if (root / p).exists() else None))
                # ONLY FILES THE CRITERIA COVER, NEVER A CONNECTOR'S (mutation_probe.targets).
                _mu_targets = mutation_probe.targets(
                    impl_files, criteria, connector_files=_connector_files(engine, run),
                    read=_mu_read)
                _planted = (mutation_probe.plant_first(_mu_targets, read=_mu_read)
                            if _mu_targets else None)
                if _planted is None:
                    # Nothing the plant knows how to flip: the probe did not run — recorded
                    # as inconclusive, never guessed and never a model fallback.
                    mutation_caught = None
                    r5 = {"cost_usd_micros": 0, "model": None,
                          "summary": "mutation probe not run: no deterministic mutation site "
                                     "in the targeted implementation files"}
                elif use_microvm:
                    res5 = _guest_stage(
                        "check_tests", "check_tests", "Run the full test suite.",
                        test_cmds=[mutation_probe.apply_command(_planted)]
                        + list(verify_commands),
                        stage_title=f"Mutation probe — {title}", engine_override="echo")
                    r5 = {"cost_usd_micros": res5.cost_usd_micros,
                          "summary": _planted.description, "model": None}
                    # A real suite FAILS on the planted bug. Only meaningful if the apply
                    # step actually changed an implementation file in the guest.
                    mutated = any(not _is_test_path(fc.path) for fc in res5.diff)
                    mutation_caught = (not bool(res5.tests_passed)) if mutated else None
                else:
                    # serial: it edits the workdir, then restores it so the PR diff is
                    # unchanged. Every implementation file is snapshotted (and restored).
                    orig = {p: (root / p).read_text(errors="ignore")
                            for p in impl_files if (root / p).exists()}
                    if orig and _planted.path in orig:
                        r5 = {"cost_usd_micros": 0, "summary": _planted.description,
                              "model": None}
                        _ws_write(root / _planted.path, _planted.mutated)
                        _mutant = mutation_probe.capture(
                            orig, {**orig, _planted.path: _planted.mutated})
                        mut = _run_verify(record=False)
                        mutation_caught = not bool(mut.ok)
                        for p, content in orig.items():
                            # The mutation probe's restore: crun_966aa44e died HERE on a
                            # file the coder had copied in read-only (workspace_perms).
                            _ws_write(root / p, content)
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
        _is_repair_iteration = _is_repair
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
        if _manifest_governs:
            # The repo defines its own verification (boss.verify.yaml): its suites ran above.
            _web_contract = verification_target.manifest_web_contract()
        elif _last_stack_plan[0] is not None:
            # A MULTI-SERVICE APP: the contract is the FRONT END's package.json, not the app
            # directory's (which, laid out right, has none). No front end, no web package.
            _sp = _last_stack_plan[0]
            if _sp.frontend is not None:
                try:
                    _web_contract = verification_target.evaluate_web_package_contract(
                        _sp.dir_of(_sp.frontend),
                        runners=(verification_target.NEXT_RUNNERS
                                 if _sp.frontend.framework == "next"
                                 else __import__("boss_engine.services.build_scope",
                                                 fromlist=["web_runners"]).web_runners()))
                except Exception:  # noqa: BLE001
                    _log_caught(logger)
                    _web_contract = {"ok": False, "stubs": ["unreadable"], "missing_runners": []}
                gates["web_package_honest"] = bool(_web_contract.get("ok"))
        elif _web_verify:
            try:
                # THE DIRECTORY THE TESTS RAN IN. With no app id this resolved to the repository
                # root, and a shared repo's root package.json "lacked" vite and @playwright/test
                # that helpdesk-ai/package.json declares (crun_cb6e15f4).
                _ran_in = (root / last_verify_cwd) if last_verify_cwd and last_verify_cwd != "." \
                    and not Path(last_verify_cwd).is_absolute() else None
                from .build_scope import web_runners

                _web_contract = verification_target.evaluate_web_package_contract(
                    _ran_in if _ran_in is not None and _ran_in.is_dir()
                    else _gate_app_dir(),
                    runners=web_runners(),
                )
            except Exception:  # noqa: BLE001
                _log_caught(logger)
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
            # BOSS copies dist/ and node_modules/ to prove the app serves: everything the run
            # made must be readable to it first (crun_8e9b3b0e: Permission denied on both).
            from . import workspace_perms as _wsp
            _wsp.normalize(root)

            with _transaction(engine) as c:
                _phase(c, "serve_check")
            # THE LINEAGE, NOT THIS DIFF ALONE. A follow-up of a declared app whose diff does
            # not contain the boss.preview.command proved `python3 main.py` instead of the
            # declared command the preview runs (second review of f16b5d4). The proof is given
            # the same file list the preview uses (lineage_impl_files) plus this diff.
            try:
                lineage = lineage_impl_files(engine, run)
            except Exception as exc:  # noqa: BLE001 — a fact about BOSS, not about the code
                _log_caught(logger)
                record = {"ok": False, "harness": True, "command": None, "source": "none",
                          "error": f"could not read this app's lineage to check it: "
                                   f"{type(exc).__name__}: {exc}"[:600]}
            else:
                # Held to serving only when the lineage DECLARED a run command or the run
                # builds a named full-stack app — never inferred from the code (third review
                # of b63717c), never `web_app_build`, which every build-on-existing/extend
                # carries, CLIs and libraries included.
                _run_needs, _run_bound = _app_run_declarations(engine, run)
                # THE PRE-CHECK'S APP-ENVIRONMENT HALF, ON THIS TREE, BEFORE THE PROOF: what the
                # app requires in build / preview / deploy and whether anyone may give it, so
                # the run learns "DEMO_PASSWORD cannot be bound for a build" before the proof
                # dies on it (workflow_precheck.app_env_on_tree). Recorded, never a gate here.
                _precheck_app_env(engine, run, root, list(files.keys()) + list(lineage or []))
                record = prove_app_serves(
                    root, list(files.keys()), lineage=lineage,
                    known_service=serve_proof_known_service(spec, fctx),
                    timeout=float(getattr(settings, "code_task_serve_check_timeout_seconds",
                                          180) or 180),
                    probe_paths=_connector_probe_paths(engine, run) or None,
                    # WHAT THE APP DECLARES, so a session key BOSS generates is there (fresh,
                    # throwaway) and a secret only a person sets is named, not crashed on.
                    needs=_run_needs, bound=_run_bound,
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
        # F2: an applied template's two gates, ADDED to the above (stream S2, §2.3).
        _template_record = _template_gates(
            engine, run, sandbox, root=root,
            cwd=(root / last_verify_cwd) if last_verify_cwd and not Path(str(last_verify_cwd)).is_absolute()
            else _gate_app_dir(changed_files),
            changed_files=changed_files, tests_passed=bool(tests_passed), use_microvm=use_microvm,
            stack_plan=_last_stack_plan[0], serve=app_serves_record)
        if _template_record:
            gates.update(_template_record["gates"])
        # A SECRET IN QA'S FIXTURE GOES BACK TO QA. The coder may not edit tests and QA was never
        # re-run, so this finding used to survive every repair round (crun_b4613dcf,
        # crun_ce7e8841). One narrow test-author turn changes only the flagged lines, then the
        # gates re-run unchanged. Findings in implementation files stay the coder's.
        fixture_repair_record: Optional[dict] = None
        if _template_record and gates.get("no_committed_secrets") is False:
            from . import fixture_secret_repair as _fsr

            _scan = ((_template_record.get("evidence") or {}).get("secret_scan") or {})
            _qa_f, _coder_f = _fsr.split_secret_findings(
                _scan.get("findings") or [],
                qa_owns=lambda p: _qa_authors_tests and _is_test_path(p))
            if _qa_f:
                _retry_of_secret_fail = _fsr.parent_failed_on_test_secrets(fctx)
                if use_microvm:
                    fixture_repair_record = {
                        "attempted": False, "findings": _qa_f,
                        "skipped": "the micro-VM lane has no in-place QA turn; findings stand"}
                else:
                    with _transaction(engine) as c:
                        _phase(c, "fixture_repair")
                        emit_event(c, project_id, "code_task.fixture_repair_started", "boss",
                                   "code_task_run", run_id,
                                   {"findings": _qa_f[:20], "coder_findings": len(_coder_f),
                                    "retry_of_secret_failure": _retry_of_secret_fail})

                    def _fixture_qa_turn(prompt: str) -> dict:
                        nonlocal cost
                        set_llm_context(stage="write_tests")
                        _a, _m = _route("write_tests")  # the TEST AUTHOR's role, not the coder's
                        _r = _a.run_role(prompt, root, model=_m)
                        cost += int(_r.get("cost_usd_micros") or 0)
                        return _r

                    from ..templates.secret_scan import scan_files as _scan_files

                    fixture_repair_record = _fsr.repair_fixture_secrets(
                        root=root, findings=_qa_f, run_qa=_fixture_qa_turn,
                        rerun_tests=lambda: bool(_run_verify(record=False).ok),
                        tests_passed_before=bool(tests_passed), test_cmd=test_cmd,
                        rescan=lambda files: _scan_files(files))
                fixture_repair_record["retry_of_secret_failure"] = _retry_of_secret_fail
                fixture_repair_record["coder_findings"] = len(_coder_f)
                if fixture_repair_record.get("attempted") and not fixture_repair_record.get("reverted"):
                    # THE GATES RE-RUN, exactly as strict, on the repaired tree.
                    changed_files = {fc.path: fc.content for fc in sandbox.diff()}
                    _template_record = _template_gates(
                        engine, run, sandbox, root=root,
                        cwd=(root / last_verify_cwd) if last_verify_cwd and not Path(str(last_verify_cwd)).is_absolute()
                        else _gate_app_dir(changed_files),
                        changed_files=changed_files, tests_passed=bool(tests_passed),
                        use_microvm=use_microvm, stack_plan=_last_stack_plan[0],
                        serve=app_serves_record)
                    if _template_record:
                        gates.update(_template_record["gates"])
                    fixture_repair_record["gate_after"] = bool(gates.get("no_committed_secrets"))
                with _transaction(engine) as c:
                    emit_event(c, project_id, "code_task.fixture_repair_finished", "boss",
                               "code_task_run", run_id,
                               {k: fixture_repair_record.get(k) for k in
                                ("repaired", "reverted", "reasons", "gate_after", "skipped",
                                 "changed_files", "cost_usd_micros")})
        _tpl_gates = dict(gates) if _template_record else ({} if _tpl_applies else None)

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
            if mutation_caught is not None and not _strength_advisory:
                gates["tests_strong_enough"] = mutation_caught
            if regressions_ok is not None:
                gates["nothing_broke"] = regressions_ok
            # block on explicit drift, and on a check that never saw the change (review_input)
            gates["consistent"] = cons_verdict not in ("drift", "hollow")
        if iac_result is not None:
            gates.update(_iac_gates(iac_result))

        steps_run = ((["prd_design"] if (design or {}).get("source") == "approved_prd"
                      else ["standards", "spec", "plan", "review_plan"]) if do_design else []) \
            + (["write_tests"] if do_separate_tests else []) + ["implement"] \
            + (["review_code", "security_check", "check_tests", "run_all_tests", "consistency_check"]
               if do_review else []) \
            + (["iac_verify"] if iac_result is not None else []) \
            + (["serve_check"] if "app_serves" in gates else [])
        workspace_evidence = bind_test_outcomes(
            workspace_evidence or {}, tests_passed=bool(tests_passed),
        )
        from .independence import assess_independence, independence_gate as _ig
        _reviewer_independence = {}
        try:
            _ria = assess_independence(
                (build_engine, r2.get('model') or None),
                [(review_engine, r3.get('model') or None)])
            _rig = _ig(_ria, required_level=str(getattr(
                settings, 'independence_required_level', 'family') or 'family'))
            _reviewer_independence = {'passed': bool(_rig.get('passed')),
                                       'level': _ria.get('min_level'),
                                       'required': _rig.get('required_level'),
                                       'reasons': _rig.get('reasons') or []}
        except Exception:  # noqa: BLE001 — a failed measurement reads as not independent
            _log_caught(logger)
            _reviewer_independence = {'passed': False, 'level': 'unknown',
                                       'required': 'family', 'reasons': ['not measured']}

        evidence = {
            "risk_tier": risk_tier,
            "profile": profile["name"],
            **({"web_package_contract": _web_contract} if _web_contract is not None else {}),
            # A multi-service app: its services, the backend BOSS started and what ran against it.
            **({"stack_verify": dict(_stack_record)} if _stack_record else {}),
            # Named here (not written earlier) because `evidence` does not exist until this
            # point — writing to it above was a latent crash that would have fired precisely
            # when a run DID weaken an existing test, i.e. when the gate matters most.
            **({"weakened_existing_tests": _weakened[:20]} if _weakened else {}),
            # What BOSS started, and whether it answered — the receipt for the app_serves gate.
            "app_serves": app_serves_record,
            # F2: the applied template's gates' receipt (which template, its tests, the scan).
            **({"template": _template_record["evidence"]} if _template_record else {}),
            # The QA fixture repair of a secret-gate finding in its own tests, when one ran.
            **({"fixture_repair": fixture_repair_record} if fixture_repair_record else {}),
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
            # Seats whose review did not see the change (review_input), and why. Their gates
            # fail as HOLLOW_GATE_REASON and the check is re-run, never the coder.
            "review_hollow": dict(_hollow_seats),
            # BOSSAPP-1000: the LLM seats sat ONE session (which seats, and any that fell
            # back to their own); absent = the pre-merge shape (refute panel / fix lane).
            **({"review_panel_merged": dict(_merged_record)} if _merged_record else {}),
            "review_change_files": len(_review_change.paths),
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
            # Pass 1: the probe's verdict is information; the More tests pass owns strength.
            "tests_strength": ({"advisory": True, "mutation_caught": mutation_caught,
                                "why": "pass 1 tests cover the critical paths only (no P2); "
                                       "the More tests pass owns test strength"}
                               if _strength_advisory and mutation_caught is not None else None),
            "regressions_ok": regressions_ok,
            # L1: the final baseline classification (caused/pre_existing/environment), so a
            # failure never caused by this change is named as such on the PR, not hidden.
            **({"baseline": baseline_record.as_dict()} if baseline_record is not None else {}),
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
            # The build's database decision (build_services.decide), shown on the build page.
            **({"build_database": dict(_build_database)} if _build_database else {}),
            "model": r2.get("model") or r2.get("usage", {}).get("model"),
            "review_model": (r3.get("model") or r3.get("usage", {}).get("model")
                             or r4.get("model") or r6.get("model")),
            "substrate": "microvm" if use_microvm else "local",
            "sandbox_task_ids": guest_vm_ids,
            "test_author_independent": do_separate_tests,
            # MEASURED, NOT ASSERTED (BOSSAPP-960): this flag used to be `do_review` — "a
            # review ran" — which read as independence on gates and dashboards while the
            # reviewer was the builder's own model. It now says what is true: a review ran AND
            # its lane is separated from the builder's (independence.separation). The full
            # assessment rides beside it for the gate pages.
            "reviewer_independent": do_review and bool(_reviewer_independence.get("passed")),
            "reviewer_independence": _reviewer_independence,
            "tests_passed": official_tests_passed, "adversarial_verdict": verdict,
            "test_files": test_files, "impl_files": impl_files,
            # What the repository already declares about how its apps run (a boss.deploy.yaml
            # the build did not write): preview and deploy read it (app_manifest).
            "repo_manifests": _deploy_manifests_in(root),
            "test_command": test_cmd, "test_exit_code": official_test_exit,
            "verify_cwd": last_verify_cwd,
            "last_verify_output": official_verify_output or last_verify_output,
            "tests": (
                [{
                    "command": test_cmd,
                    "exit_code": test_exit_code,
                    "timed_out": False,
                    "ok": bool(tests_passed),
                    **({"failed_check": _failed_check[0]}
                       if _failed_check[0] and not tests_passed else {}),
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

        # No gate judges a build whose engine could not run: its verdict is the harness's.
        _stop_on_harness_failure("quality gate")
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
        if do_review:
            _record_review_round(engine, run_id, project_id, round_i=0, seats={
                "review_code": {"verdict": verdict, "summary": r3.get("summary", ""),
                                "hollow": _hollow_seats.get("review_code")},
                "security_check": {"verdict": sec_verdict, "summary": r4.get("summary", ""),
                                   "hollow": _hollow_seats.get("security_check")},
                "consistency_check": {"verdict": cons_verdict, "summary": r6.get("summary", ""),
                                      "hollow": _hollow_seats.get("consistency_check")}},
                reviewed=repair_review_scope.snapshot(_review_change))

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
            # ONE FIX ROUND FOR FAILING TESTS (a test two warm rounds could not satisfy is not
            # satisfied by a third: Notes Lite spent 16 minutes on rounds 2-3), and ALWAYS ONE
            # for a blocking finding from any judgment seat: review, security, consistency or a
            # weak suite. Test gates and security findings are convergeable for the reasons in
            # warm_fix_rounds; so is `tests_strong_enough`, whose absence from this set skipped
            # the round for every seat on crun_2d097414 ("Round 1 of 1"). With no warm session
            # (a streamed implement), the round starts a fresh one with the same findings.
            _convergeable = _CONVERGEABLE

            def _tpl_evidence() -> dict:
                return dict((_template_record or {}).get("evidence") or {})

            def _coder_owned_secrets() -> bool:
                # Every remaining secret finding is in a file the coder wrote (QA's go back to
                # QA first, fixture_secret_repair): the implementer's warm session removes it.
                _fs = (_tpl_evidence().get("secret_scan") or {}).get("findings") or []
                return bool(_fs) and not any(
                    _qa_authors_tests and _is_test_path(str(f.get("path") or "")) for f in _fs)

            inrun_max = warm_fix_rounds(failing, configured=inrun_max,
                                        repair_attempts=repair_attempts, use_microvm=use_microvm,
                                        template_evidence=_tpl_evidence(),
                                        coder_owned_secrets=_coder_owned_secrets())
            _fix_rounds_run = 0
            if inrun_max:
              try:  # a convergence failure must DEGRADE to the legacy paths, never kill the run
                for round_i in range(1, inrun_max + 1):
                    _fix_rounds_run = round_i
                    finding_bits = []
                    # TEST ARBITRATION: before making the implementer chase the oracle, establish
                    # that the oracle is right. An over-specified test is unsatisfiable, and every
                    # round spent against it is wasted.
                    if ("written_tests_pass" in failing
                            and getattr(settings, "code_test_arbiter_enabled", True)):
                        _tr = _run_verify()
                        if not _tr.ok and _install_fail[0]:
                            # BOSS's install failed, so no test ran: nothing for an arbiter to
                            # judge. The warm session gets npm's error and what to do.
                            finding_bits.append(verification_target.install_repair_evidence(
                                _install_fail[0], (_tr.stdout or "") + (_tr.stderr or "")))
                        elif not _tr.ok:
                            # A test_wrong ruling amends only the oracle (_arbitrate_tests).
                            if _arbitrate_tests(_tr.stdout, round_i) != "test_wrong":
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
                    if "security_clean" in failing:
                        finding_bits.append(_anchored_findings(
                            r4.get("summary") or "", root, stage="security_check",
                            header="SECURITY FINDINGS:"))
                    if "app_serves" in failing:
                        # The app's OWN error, not "preview timed out": this is what lets the
                        # implementer fix an ImportError instead of guessing at a port.
                        finding_bits.append(
                            "THE APP DOES NOT SERVE HTTP. "
                            + _app_serves_sentence(app_serves_record)[:2600]
                            + "\nFix the app (or its boss.preview.command) so that command "
                            "serves HTTP on $PORT.")
                    if "no_committed_secrets" in failing and _coder_owned_secrets():
                        # Path, line and rule only: the scan's report never repeats the value.
                        _sf = (_tpl_evidence().get("secret_scan") or {}).get("findings") or []
                        finding_bits.append(
                            "COMMITTED SECRET LITERALS (the secret scan; remove each one — read "
                            "the value from the environment (process.env / os.environ) and put "
                            "a placeholder such as ${NAME} or <your value> in examples and docs):\n"
                            + "\n".join(f"- {f.get('path')}:{f.get('line')} ({f.get('rule')})"
                                        for f in _sf[:30]))
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
                        # The comments, numbered as shown, so the implementer's RESPONSE lines
                        # can be matched back and posted beside each one on the pull request.
                        _answered = review_comments.implementer_order(_open) if _open else []
                        rfix = a_fix.run_role(
                            _targeted_fix_prompt("\n\n".join(finding_bits), round_i)
                            + ("\n" + review_comments.response_clause() if _answered else ""),
                            root, model=m_fix, resume_session_id=impl_session_id)
                        impl_session_id = rfix.get("session_id") or impl_session_id
                        cost += int(rfix.get("cost_usd_micros") or 0)
                        impl_session_id = _install_until_it_passes(
                            a_fix, m_fix, impl_session_id, "fix_round")
                        _remember_impl_session(a_fix, m_fix)
                        cost += _cost_extra[0]
                        _cost_extra[0] = 0
                        _after = {fc.path: fc.content for fc in sandbox.diff()}
                        if _answered:
                            _record_comment_responses(
                                engine, run_id, project_id, _answered, rfix.get("summary") or "",
                                round_i=round_i,
                                changed_files=[p for p in set(_before) | set(_after)
                                               if _before.get(p) != _after.get(p)])
                        if _after == _before:
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
                    if "tests_strong_enough" in failing:
                        # A WEAK SUITE IS THE TEST AUTHOR'S TO FIX, never the implementer's (it
                        # may not edit tests): the lane that wrote them strengthens them against
                        # the bug the probe planted, and the probe re-runs below.
                        with _transaction(engine) as c:
                            _phase(c, "write_tests")
                        set_llm_context(routes=_route_ids(), db_engine=engine,
                                        project_id=project_id, run_id=run_id, stage="write_tests")
                        a_st, m_st = _route("write_tests")
                        r_st = a_st.run_role(
                            _strengthen_tests_prompt(criteria, (r5 or {}).get("summary") or "",
                                                     round_i),
                            root, model=m_st)
                        cost += int(r_st.get("cost_usd_micros") or 0)
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
                    if _template_record and set(failing) & _TEMPLATE_GATE_SET:
                        # THE TEMPLATE GATES ARE RE-JUDGED on the code as it now stands, exactly
                        # as strict: a consequence failure clears only if its cause did.
                        _tpl_changed = {fc.path: fc.content for fc in sandbox.diff()}
                        _template_record = _template_gates(
                            engine, run, sandbox, root=root,
                            cwd=(root / last_verify_cwd) if last_verify_cwd and not Path(str(last_verify_cwd)).is_absolute()
                            else _gate_app_dir(_tpl_changed),
                            changed_files=_tpl_changed, tests_passed=bool(tests_passed),
                            use_microvm=use_microvm, stack_plan=_last_stack_plan[0],
                            serve=app_serves_record) or _template_record
                        gates.update(_template_record["gates"])
                        _tpl_gates = dict(gates)
                    if not tests_passed:
                        # Still red is no longer a dead end: re-arbitrate next round (the fix may
                        # be partial, or an amended test still over-specified). Only exhausting
                        # the rounds drops through to the legacy repair/block paths.
                        failing = [k for k, v in gates.items() if not v]
                        if round_i >= inrun_max or unconvergeable_failing(
                                failing, template_evidence=_tpl_evidence(),
                                coder_owned_secrets=_coder_owned_secrets()):
                            break
                        continue
                    _review_change = _review_change_now()
                    change_block = _change_block_with_integrity(_review_change)
                    impl_files = [fc.path for fc in sandbox.diff()
                                  if not _is_test_path(fc.path)] or impl_files
                    # SCOPED TO WHAT THE FIX CHANGED (_fix_round_change). With nothing recorded
                    # as changed (a test-only round), the seat judges the whole change as before.
                    _fixed = ({p: c for p, c in _after.items() if _before.get(p) != c}
                              if finding_bits else {})

                    _answers = _implementer_answers(engine, run_id, project_id)

                    def _seat_change(prior: dict) -> str:
                        if not _fixed:
                            return _answers + change_block
                        return _fix_round_change((prior or {}).get("summary") or "", _fixed,
                                                 _answers)
                    _rc_prior, _r6_prior, _r4_prior = dict(r3), dict(r6), dict(r4)
                    # THE FAILED SEATS RE-REVIEW SIDE BY SIDE, as the first panel does. They read
                    # the same delta and do not depend on each other; one after another, a
                    # HelpDesk-lite round spent 6.5 min (07:31 -> 07:37) where the slowest seat
                    # alone took 2.7. Each thread publishes its own run identity and stage.
                    def _reseat_review():
                        set_llm_context(routes=_route_ids(), db_engine=engine, project_id=project_id,
                                        run_id=run_id, stage="review_code")
                        rr, _hollow = _judge_seat("review_code", lambda pre: review_agent.run_role(
                            pre + _review_prompt(title, criteria, instructions=instructions,
                                                 ui=ui_task, change=_seat_change(_rc_prior),
                                                 operator=_operator_review_block(
                                                     engine, run_id, project_id)),
                            review_root, read_only=True, model=review_model))
                        return rr, _hollow

                    def _reseat_consistency():
                        set_llm_context(routes=_route_ids(), db_engine=engine, project_id=project_id,
                                        run_id=run_id, stage="consistency_check")
                        rc2, _hollow = _judge_seat(
                            "consistency_check", lambda pre: consistency_agent.run_role(
                                pre + _consistency_prompt(title, criteria, design,
                                                          instructions=instructions,
                                                          change=_seat_change(_r6_prior)),
                                review_root, read_only=True, model=consistency_model))
                        return rc2, _hollow

                    def _reseat_security():
                        set_llm_context(routes=_route_ids(), db_engine=engine, project_id=project_id,
                                        run_id=run_id, stage="security_check")
                        rs2, _hollow = _judge_seat(
                            "security_check", lambda pre: security_agent.run_role(
                                pre + _security_prompt(title, instructions=instructions,
                                                       criteria=criteria,
                                                       change=_seat_change(_r4_prior)),
                                review_root, read_only=True, model=security_model))
                        return rs2, _hollow

                    _seat_fns = {"code_review_passed": ("review_code", _reseat_review),
                                 "consistent": ("consistency_check", _reseat_consistency),
                                 "security_clean": ("security_check", _reseat_security)}
                    _wanted = [_seat_fns[gname] for gname in failing if gname in _seat_fns]
                    if _wanted:
                        with _transaction(engine) as c:
                            _phase(c, _wanted[0][0] if len(_wanted) == 1 else "review_panel")
                    # THE SAME DEADLINE AND OWNED-PROCESS KILL AS THE FIRST PANEL
                    # (review_seats.run_seats). A re-review used to wait with no timeout at all.
                    # A seat that did not finish (deadline or error) counts as HOLLOW: recorded
                    # as not having reviewed, never as a pass.
                    _rs_deadline = int(getattr(settings, "code_agent_review_timeout_seconds", 900)
                                       or 900)
                    _rs_run = _run_seats(
                        list(_wanted), max_workers=max(1, len(_wanted)),
                        seat_deadline_s=_rs_deadline,
                        owner_id=lambda label: f"{run_id}:reseat{round_i}:{label}",
                        kill_strays=lambda: _kill_stray_review_processes(root, review_root))
                    _reseat = {}
                    for stage_key, _fn in _wanted:
                        if _rs_run.finished(stage_key) and stage_key not in _rs_run.errors:
                            _reseat[stage_key] = _rs_run.results[stage_key]
                        else:
                            _why = (f"the re-review did not finish within {_rs_deadline}s"
                                    if stage_key in _rs_run.timed_out
                                    else _rs_run.errors.get(stage_key) or "the re-review failed")
                            _reseat[stage_key] = ({"summary": "", "cost_usd_micros": 0}, _why)
                    if _rs_run.timed_out or _rs_run.errors:
                        with _transaction(engine) as c:
                            emit_event(c, project_id, "code_task.review_degraded", "boss",
                                       "code_task_run", run_id,
                                       {"round": round_i, "deadline_s": _rs_deadline,
                                        "timed_out": list(_rs_run.timed_out),
                                        "errors": list(_rs_run.errors.values())[:8],
                                        "killed": _rs_run.killed})
                    if "code_review_passed" in failing:
                        rr, _hollow = _reseat["review_code"]
                        cost += int(rr.get("cost_usd_micros") or 0)
                        rr = _with_ci_static(rr, accumulated if use_microvm else sandbox.diff())
                        if _hollow:
                            _hollow_seats["review_code"] = _hollow
                        else:
                            _hollow_seats.pop("review_code", None)
                        verdict = ("not_run" if _hollow
                                   else _parse_verdict(rr.get("summary", "")))
                        r3 = rr
                        review_confidence = 1.0 if verdict == "survived" else 0.0
                        _rev_pass, _blocking, _deferred = _review_gate_from_findings(
                            rr.get("summary", ""), verdict,
                            getattr(settings, "code_review_block_severity", "major"))
                        deferred_findings = _deferred
                        # Materialise the panel's findings as comments NOW, before any fix
                        # round runs, so the implementer and the pull request read the
                        # same artifact instead of two renderings of the same prose. THIS
                        # round's summary: `evidence` still holds the previous one until the
                        # round passes, which is how a re-review's findings were never recorded.
                        _record_review_comments(
                            engine, run_id, project_id,
                            {**evidence, "review_summary": rr.get("summary", "")},
                            round_i=round_i)
                        _record_review_round(engine, run_id, project_id, round_i=round_i, seats={
                            "review_code": {"verdict": verdict, "summary": rr.get("summary", ""),
                                            "hollow": _hollow}},
                            reviewed=repair_review_scope.snapshot(_review_change))
                        gates["code_review_passed"] = _rev_pass
                    if "consistent" in failing:
                        rc2, _hollow = _reseat["consistency_check"]
                        cost += int(rc2.get("cost_usd_micros") or 0)
                        if _hollow:
                            _hollow_seats["consistency_check"] = _hollow
                        else:
                            _hollow_seats.pop("consistency_check", None)
                        cons_verdict = ("hollow" if _hollow
                                        else _parse_consistency(rc2.get("summary", "")))
                        r6 = rc2
                        gates["consistent"] = cons_verdict not in ("drift", "hollow")
                    if "security_clean" in failing:
                        rs2, _hollow = _reseat["security_check"]
                        cost += int(rs2.get("cost_usd_micros") or 0)
                        if _hollow:
                            _hollow_seats["security_check"] = _hollow
                        else:
                            _hollow_seats.pop("security_check", None)
                        sec_verdict = ("hollow" if _hollow
                                       else _parse_security(rs2.get("summary", "")))
                        r4 = rs2
                        _record_review_round(engine, run_id, project_id, round_i=round_i, seats={
                            "security_check": {"verdict": sec_verdict,
                                               "summary": rs2.get("summary", ""),
                                               "hollow": _hollow}},
                            reviewed=repair_review_scope.snapshot(_review_change))
                        gates["security_clean"] = sec_verdict == "clean"
                    if "tests_strong_enough" in failing:
                        # THE WEAK-SUITE SEAT RE-JUDGES: plant one bug again and expect the
                        # strengthened suite to catch it. Serial, after the read-only seats: it
                        # edits the tree, then restores it, as the first probe does.
                        with _transaction(engine) as c:
                            _phase(c, "check_tests")
                        _keep = (last_verify_output, tests_passed, test_exit_code, last_verify_cwd)
                        _orig = {p: (root / p).read_text(errors="ignore")
                                 for p in impl_files if (root / p).exists()}
                        _caught = None
                        # THE SAME MUTANT the suite was strengthened against, planted again
                        # without a model call; a new one only when the code moved under it.
                        _replant = ({p: mutation_probe.reapply(_orig[p], b)
                                     for p, b in _mutant.items() if p in _orig}
                                    if _mutant and set(_mutant) <= set(_orig) else {})
                        _same_mutant = bool(_replant) and all(_replant.values())
                        # The code moved under the old mutant: a NEW deterministic plant
                        # (BOSSAPP-1004) — the same no-model mechanism as the first probe.
                        _newplant = None if _same_mutant else mutation_probe.plant_first(
                            mutation_probe.targets(
                                list(_orig), criteria,
                                connector_files=_connector_files(engine, run),
                                read=_orig.get) or list(_orig), read=_orig.get)
                        if _orig and (_same_mutant or _newplant is not None):
                            set_llm_context(routes=_route_ids(), db_engine=engine,
                                            project_id=project_id, run_id=run_id,
                                            stage="check_tests")
                            try:
                                if _same_mutant:
                                    for _mp, _mc in _replant.items():
                                        _ws_write(root / _mp, _mc)
                                else:
                                    r5 = {"cost_usd_micros": 0, "model": None,
                                          "summary": _newplant.description}
                                    _ws_write(root / _newplant.path, _newplant.mutated)
                                    _mutant = mutation_probe.capture(
                                        _orig, {**_orig, _newplant.path: _newplant.mutated})
                                _caught = not bool(_run_verify(record=False).ok)
                            finally:
                                for _mp, _mc in _orig.items():
                                    _ws_write(root / _mp, _mc)
                        last_verify_output, tests_passed, test_exit_code, last_verify_cwd = _keep
                        mutation_caught = _caught
                        evidence["mutation_caught"] = _caught
                        gates["tests_strong_enough"] = _caught is not False
                        _record_review_round(engine, run_id, project_id, round_i=round_i, seats={
                            "check_tests": {"verdict": ("caught" if _caught else
                                                        "survived" if _caught is False
                                                        else "not_run"),
                                            "same_mutant": _same_mutant,
                                            "summary": (r5 or {}).get("summary", ""),
                                            "hollow": None}})
                    failing = [k for k, v in gates.items() if not v]
                    evidence["review_hollow"] = dict(_hollow_seats)
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
                    if set(failing) - {"code_review_passed", "consistent", "app_serves",
                                       "security_clean", "tests_strong_enough"}:
                        break  # a non-judgment gate regressed — legacy handling below
              except Exception as exc:  # noqa: BLE001
                _log_caught(logger)
                with _transaction(engine) as c:
                    emit_event(c, project_id, "code_task.inrun_convergence_degraded", "boss",
                               "code_task_run", run_id,
                               {"error": f"{type(exc).__name__}: {exc}"[:300]})
                failing = [k for k, v in gates.items() if not v]

            # ONE SECRET REPAIR TURN BEFORE A SECRET BLOCKS THE BUILD (repair_committed_secrets):
            # the scan's findings (file, line, rule — never the value) go to the implementer's
            # warm session in a turn of their own, then the tests, the serve proof and the
            # template gates re-run exactly as strict. Blocked below only if a secret remains.
            if (_template_record and gates.get("no_committed_secrets") is False
                    and not use_microvm and _coder_owned_secrets()):
                _rj: dict = {}

                def _secret_fix(prompt: str) -> dict:
                    nonlocal impl_session_id, cost
                    with _transaction(engine) as c:
                        _phase(c, "implement")
                    set_llm_context(routes=_route_ids(), db_engine=engine, project_id=project_id,
                                    run_id=run_id, stage="implement")
                    a_sec, m_sec = _route("implement")
                    r_sec = a_sec.run_role(prompt, root, model=m_sec,
                                           resume_session_id=impl_session_id)
                    impl_session_id = r_sec.get("session_id") or impl_session_id
                    cost += int(r_sec.get("cost_usd_micros") or 0)
                    impl_session_id = _install_until_it_passes(
                        a_sec, m_sec, impl_session_id, "secret_repair")
                    _remember_impl_session(a_sec, m_sec)
                    cost += _cost_extra[0]
                    _cost_extra[0] = 0
                    return r_sec

                def _secret_rejudge() -> dict:
                    _post = _run_verify()
                    _rj["post"] = _post
                    _serve = app_serves_record
                    if bool(_post.ok) and "app_serves" in gates:
                        _serve = _check_app_serves({fc.path: fc.content for fc in sandbox.diff()})
                        _rj["serve"] = _serve
                    _changed = {fc.path: fc.content for fc in sandbox.diff()}
                    _rec = _template_gates(
                        engine, run, sandbox, root=root,
                        cwd=(root / last_verify_cwd) if last_verify_cwd and not Path(str(last_verify_cwd)).is_absolute()
                        else _gate_app_dir(_changed),
                        changed_files=_changed, tests_passed=bool(_post.ok),
                        use_microvm=use_microvm, stack_plan=_last_stack_plan[0], serve=_serve)
                    _rj["record"] = _rec
                    _scan = ((_rec or {}).get("evidence") or {}).get("secret_scan") or {}
                    return {"passed": bool(((_rec or {}).get("gates") or {}).get("no_committed_secrets")),
                            "findings": _scan.get("findings") or []}

                _sf0 = (_tpl_evidence().get("secret_scan") or {}).get("findings") or []
                try:
                    with _transaction(engine) as c:
                        emit_event(c, project_id, "code_task.secret_repair_started", "boss",
                                   "code_task_run", run_id,
                                   {"findings": [{k: f.get(k) for k in ("path", "line", "rule")}
                                                 for f in _sf0[:20] if isinstance(f, dict)]})
                    secret_repair_record = repair_committed_secrets(
                        _sf0, run_fix=_secret_fix, rejudge=_secret_rejudge)
                except Exception as exc:  # noqa: BLE001 — the gate's verdict stands as it was
                    _log_caught(logger)
                    secret_repair_record = {"attempted": True, "cleared": False,
                                            "error": f"{type(exc).__name__}: {exc}"[:300]}
                _post = _rj.get("post")
                if _post is not None:
                    tests_passed = bool(_post.ok)
                    official_tests_passed = tests_passed
                    official_test_exit = _post.exit_code
                    official_verify_output = last_verify_output
                    if "written_tests_pass" in gates:
                        gates["written_tests_pass"] = tests_passed
                    if "nothing_broke" in gates:
                        gates["nothing_broke"] = tests_passed
                if "serve" in _rj and "app_serves" in gates:
                    app_serves_record = _rj["serve"]
                    gates["app_serves"] = bool(app_serves_record.get("ok"))
                    evidence["app_serves"] = app_serves_record
                if _rj.get("record"):
                    _template_record = _rj["record"]
                    gates.update(_template_record["gates"])
                    _tpl_gates = dict(gates)
                    evidence["template"] = _template_record["evidence"]
                evidence["secret_repair"] = secret_repair_record
                failing = [k for k, v in gates.items() if not v]
                with _transaction(engine) as c:
                    emit_event(c, project_id, "code_task.secret_repair_finished", "boss",
                               "code_task_run", run_id,
                               {k: secret_repair_record.get(k) for k in
                                ("cleared", "findings_after", "cost_usd_micros", "error")})
                    if not failing:
                        c.execute(sql(
                            "UPDATE code_task_runs SET state=CAST(:s AS JSONB), updated_at=now() "
                            "WHERE id=:id"), {"s": json.dumps(evidence), "id": run_id})
                        qg_id, qg_status = _record_quality_gate(
                            c, run_id=run_id, project_id=project_id, task_id=task_id,
                            job_id=job_id, evidence=evidence, gates=gates)

            rounds_used = max(0, int(fctx.get("attempt", 1)) - 1)
            # Fallback 2 (not 0): a missing/blank config field must never SILENTLY disable the moat.
            max_repair_rounds = review_repair_rounds(settings)
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
            # WHAT IS STILL A BLOCKER after the warm round: it blocks on every profile.
            _blockers = unresolved_blockers(
                failing, review_summary=r3.get("summary", ""), security_verdict=sec_verdict,
                consistency_summary=r6.get("summary", ""), hollow=_hollow_seats)
            if _blockers:
                evidence["unresolved_blockers"] = _blockers
            # THE OPERATOR'S RULE (checks_failing.py): past the repair rounds a failing check
            # opens the PR (draft, labelled, listed) instead of parking for a waiver.
            from . import checks_failing as _checks_failing

            try:
                with _transaction(engine) as c:
                    _open_when_failing = _checks_failing.enabled(c, settings, project_id)
            except Exception:  # noqa: BLE001 — the default is the rule
                _log_caught(logger)
                _open_when_failing = _checks_failing.enabled(None, settings, project_id)
            # L2a run supervisor: watch this repair loop for stalled progress (same failing
            # gate signature across rounds, environment/refusal-only failures, the 3-round
            # cap) and, if tripped or invariant-forced, record a governed decision. Never
            # allowed to raise or block the existing flow — a supervisor failure degrades to
            # a no-op here, same as any other best-effort telemetry.
            _sup_decision = None
            try:
                from datetime import datetime as _datetime, timezone as _timezone

                from . import run_supervisor as _supervisor
                with _transaction(engine) as _c:
                    _signal_on = _supervisor.signal_enabled(_c, settings, project_id)
                # A round that failed nothing has nothing to supervise: judging it parked a
                # build that had just converged (crun_73e64e45, 2026-10-31).
                if _signal_on and gate_faults:
                    _history = list(fctx.get("supervisor_history") or [])
                    # This repair loop's own start marker: the first round to reach this hook
                    # stamps it, so `stall_minutes` measures wall-clock time stuck in THIS loop.
                    _started_at = fctx.get("supervisor_started_at")
                    if not _started_at:
                        _started_at = _datetime.now(_timezone.utc).isoformat()
                        fctx["supervisor_started_at"] = _started_at
                    _signal = _supervisor.evaluate_signal(
                        faults=gate_faults,
                        rounds_used=rounds_used,
                        max_repair_rounds=max_repair_rounds,
                        history=_history,
                        spend_usd_micros=int(fctx.get("spend_usd_micros") or 0),
                        started_at=_started_at,
                    )
                    if _signal.tripped or _signal.forced:
                        with _transaction(engine) as _c:
                            _judgement_on = _supervisor.judgement_enabled(_c, settings, project_id)
                        if _judgement_on:
                            # adapter=None routes through the real governed model call
                            # (run_supervisor._model_adapter / AgentService.decide); tests
                            # inject a stub adapter instead.
                            # A CONNECTION, not the engine: AgentService.decide executes on
                            # it. Passing `engine` failed every judgement ("'Engine' object has
                            # no attribute 'execute'") into a fail-closed stop_report, so the
                            # supervisor never consulted a model or Jev (boss-1, crun_73e64e45).
                            with _transaction(engine) as _jc:
                                _decision = _supervisor.decide(
                                    _jc, settings, project_id, run_id=run_id,
                                    signal=_signal, history=_history, adapter=None,
                                )
                        else:
                            _decision = {"action": _supervisor.STOP_REPORT,
                                         "reason": "judgement disabled by setting"}
                        with _transaction(engine) as _c:
                            _supervisor.record_decision(
                                _c, run_id=run_id, project_id=project_id,
                                signal=_signal, decision=_decision)
                            _supervisor.write_lesson(
                                _c, project_id=project_id, run_id=run_id,
                                signal=_signal, decision=_decision)
                        with _transaction(engine) as _c:
                            _action_on = _supervisor.action_enabled(_c, settings, project_id)
                        if _action_on:
                            _sup_decision = _decision
                    fctx["supervisor_history"] = _history + [
                        {"round": rounds_used, "faults": dict(gate_faults)}]
            except Exception:  # noqa: BLE001 — telemetry never blocks the repair loop
                _log_caught(logger)
                _sup_decision = None

            # THE DECISION IS ENFORCED, NOT JUST RECORDED: a round with zero caused failures
            # never proceeds unredirected (PRD 5.3 / AC2). stop_report/degraded short-circuit
            # straight to "block" (resolve_gate_failure_action never runs); reach_out also
            # short-circuits to "block", carrying its question; steer/change_approach/
            # platform_fix stash guidance for the next round's prompt but let the gates'
            # own action stand — the supervisor redirects the NEXT attempt, it does not
            # pre-empt THIS round's gate verdict.
            from . import run_supervisor_types

            _sup_action = (_sup_decision or {}).get("action")
            if _sup_action in (run_supervisor_types.STOP_REPORT, run_supervisor_types.DEGRADED):
                action = "block"
                fctx["supervisor_decision"] = _sup_decision
            elif _sup_action == run_supervisor_types.REACH_OUT:
                # The supervisor decided a person is needed -- pause and ask (L2b) instead of
                # falling into the generic quality-gate "block" path below, which assumes a
                # failing gate to explain and would have nothing to say for a round with zero
                # caused failures (PRD 5.3 / AC2).
                from . import person_wait

                fctx["supervisor_decision"] = _sup_decision
                question = (_sup_decision.get("question")
                            or "This build needs something only a person can provide.")
                fctx["supervisor_question"] = question
                _budget_adapter = SimpleNamespace(
                    release=lambda **_: _release(
                        budget, _c, project_id, reservation_id, question, run_id=run_id,
                    ),
                )
                with _transaction(engine) as _c:
                    person_wait.enter_waiting_on_person(
                        _c, _budget_adapter, run_id=run_id, project_id=project_id,
                        task_id=task_id, reason="supervisor_reach_out", question=question,
                    )
                return {"status": "waiting_on_person", "done": True,
                        "reason": "supervisor_reach_out", "message": question}
            else:
                if _sup_action in (run_supervisor_types.STEER,
                                    run_supervisor_types.CHANGE_APPROACH,
                                    run_supervisor_types.PLATFORM_FIX):
                    fctx["supervisor_steer"] = _sup_decision.get("note")
                action = resolve_gate_failure_action(
                    failing=failing,
                    advisory_gate=advisory_gate,
                    rounds_used=rounds_used,
                    max_repair_rounds=max_repair_rounds,
                    open_pr=_can_ship_pr,
                    never_block=bool(getattr(settings, "code_gate_never_block", True)),
                    faults=gate_faults,
                    template_repair_max=max_repair,
                    blockers=_blockers,
                    fix_round_ran=_fix_rounds_run > 0,
                    open_pr_when_failing=_open_when_failing,
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
                        _review_change = _review_change_now()
                        change_block = _change_block_with_integrity(_review_change)
                        rr, _hollow = _judge_seat("review_code", lambda pre: review_agent.run_role(
                            pre + _review_prompt(title, criteria, instructions=instructions,
                                                 ui=ui_task, change=change_block,
                                                 operator=_operator_review_block(
                                                     engine, run_id, project_id)),
                            review_root, read_only=True, model=review_model,
                            max_turns=escalated))
                        cost += int(rr.get("cost_usd_micros") or 0)
                        rr = _with_ci_static(rr, accumulated if use_microvm else sandbox.diff())
                        if _hollow:
                            _hollow_seats["review_code"] = _hollow
                        else:
                            _hollow_seats.pop("review_code", None)
                        evidence["review_hollow"] = dict(_hollow_seats)
                        verdict = ("not_run" if _hollow
                                   else _parse_verdict(rr.get("summary", "")))
                        r3 = rr
                        _rev_pass, _blocking, _deferred = _review_gate_from_findings(
                            rr.get("summary", ""), verdict,
                            getattr(settings, "code_review_block_severity", "major"))
                        deferred_findings = _deferred
                        # Materialise the panel's findings as comments NOW, before any fix
                        # round runs, so the implementer and the pull request read the
                        # same artifact instead of two renderings of the same prose.
                        _esc_round = 100 + escalations   # escalated checks after the rounds
                        _record_review_comments(
                            engine, run_id, project_id,
                            {**evidence, "review_summary": rr.get("summary", "")},
                            round_i=_esc_round)
                        _record_review_round(engine, run_id, project_id, round_i=_esc_round,
                                             seats={"review_code": {
                                                 "verdict": verdict,
                                                 "summary": rr.get("summary", ""),
                                                 "hollow": _hollow}})
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
                    _blockers = unresolved_blockers(
                        failing, review_summary=r3.get("summary", ""),
                        security_verdict=sec_verdict, consistency_summary=r6.get("summary", ""),
                        hollow=_hollow_seats)
                    action = resolve_gate_failure_action(
                        failing=failing, advisory_gate=advisory_gate,
                        rounds_used=rounds_used, max_repair_rounds=max_repair_rounds,
                        open_pr=_can_ship_pr,
                        never_block=bool(getattr(settings, "code_gate_never_block", True)),
                        faults=gate_faults, template_repair_max=max_repair,
                        blockers=_blockers, fix_round_ran=_fix_rounds_run > 0,
                        open_pr_when_failing=_open_when_failing)
                if action == "inconclusive":
                    # Escalations exhausted: ship, recording that the code was NOT reviewed.
                    # Still respect _can_ship_pr — a non-final slice / open_pr=false run must
                    # not ship_with_dissent and advance broken mid-chain work.
                    action = ("ship_with_dissent"
                              if (bool(getattr(settings, "code_gate_never_block", True))
                                  and _can_ship_pr)
                              else _checks_failing.ACTION
                              if (_open_when_failing and _checks_failing.may_open(
                                  failing, faults=gate_faults, open_pr=_can_ship_pr))
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
            elif action in ("ship_with_dissent", _checks_failing.ACTION):
                # AUTONOMY TERMINAL: convergence and repair are spent and the panel is still
                # unhappy. Ship, but make the dissent impossible to miss — the failing gates and
                # the reviewer's reasoning land on the run AND become their own task, so a human
                # can reverse this without having to discover it first.
                #
                # CHECKS FAILING (checks_failing.py): the same PR, made impossible to merge by
                # accident — a draft labelled `checks failing`, its description opening with
                # every failing gate and finding word for word (security first), and a merge
                # card a person answers with a reason. Both terminals record it: a PR with
                # failing checks is one thing, whichever rule let it open.
                _details = {}
                if "app_serves" in failing:
                    _details["app_serves"] = _app_serves_sentence(app_serves_record)[:700]
                if _blockers:
                    for _g in _blockers:
                        _details.setdefault(_g, "a BLOCKER-level finding is still open after "
                                                "its fix round")
                checks_record = {
                    **_checks_failing.collect(failing, evidence, details=_details),
                    "hard": action == _checks_failing.ACTION,
                    "rounds_used": rounds_used, "quality_gate_id": qg_id,
                }
                evidence[_checks_failing.KEY] = checks_record
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
                # Carry the failing gates and their open findings (verbatim, security first)
                # into the follow-up task, ahead of the deferred findings.
                deferred_findings = (_checks_failing.followup_findings(checks_record)
                                     + list(deferred_findings))
                with _transaction(engine) as c:
                    c.execute(sql(
                        "UPDATE code_task_runs SET state=CAST(:s AS JSONB), "
                        "finalize_ctx=COALESCE(finalize_ctx,'{}'::jsonb) "
                        "|| jsonb_build_object('unresolved_dissent', CAST(:d AS JSONB), "
                        "'checks_failing', CAST(:cf AS JSONB)), "
                        "updated_at=now() WHERE id=:id"),
                        {"s": json.dumps(evidence), "d": json.dumps(dissent),
                         "cf": json.dumps(checks_record), "id": run_id})
                    emit_event(c, project_id, "code_task.shipped_with_dissent", "boss",
                               "code_task_run", run_id, dissent)
                    if action == _checks_failing.ACTION:
                        emit_event(c, project_id, _checks_failing.OPENED_EVENT, "boss",
                                   "code_task_run", run_id,
                                   {"failing_gates": _checks_failing.gate_names(checks_record),
                                    "security": checks_record.get("security"),
                                    "findings": len(checks_record.get("findings") or []),
                                    "why": "the repair rounds are spent; the PR opens with its "
                                           "checks failing instead of waiting for a waiver"})
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
                    if _files and not _push_held("repair_handoff"):
                        sf.github.ensure_branch(work_branch, base_branch)
                        for _path, _content in _files:
                            sf.github.upsert_file(
                                work_branch, _path, _content,
                                f"wip({run_id}): persist attempt for repair")
                        _record_work_branch("repair_handoff")
                        with _transaction(engine) as c:
                            emit_event(c, project_id, "code_task.work_persisted", "boss",
                                       "code_task_run", run_id,
                                       {"branch": work_branch, "files": len(_files),
                                        "why": "so the repair patches this tree instead of "
                                               "rebuilding it from nothing"})
                except Exception as exc:  # never let persistence failure block the repair
                    _log_caught(logger)
                    with _transaction(engine) as c:
                        emit_event(c, project_id, "code_task.work_persist_failed", "boss",
                                   "code_task_run", run_id,
                                   {"branch": work_branch,
                                    "error": f"{type(exc).__name__}: {exc}"[:200]})
                _save_workspace("repair_handoff")
                from .code_task_service import RetryNeedsPerson, retry_code_task
                try:
                    _repair_note = ("Autonomous review-repair: the quality gate blocked on "
                              f"{', '.join(failing)}. Address EVERY review finding below so ALL "
                              "gates pass, and do NOT regress the checks that already passed. If "
                              "'tests_strong_enough' failed, STRENGTHEN the tests so a one-line "
                              "mutation to the logic is caught."
                              # The app's own startup error, so the repair fixes THAT rather
                              # than rediscovering it (crun_3e370c11's ImportError).
                              + (" 'app_serves' failed: "
                                 + _app_serves_sentence(app_serves_record)[:1600]
                                 if "app_serves" in failing else ""))
                    # BOSS's own decision, so under the person's grant or not at all
                    # (retry_code_task(automatic=True)).
                    nxt = retry_code_task(engine, settings, jobs, run_id, fix_note=_repair_note,
                                          automatic=True)
                except RetryNeedsPerson as withheld:
                    with _transaction(engine) as c:
                        emit_event(c, project_id, "code_task.auto_retry_withheld", "boss",
                                   "code_task_run", run_id,
                                   {"why": str(withheld)[:300], "failing_gates": failing})
                        return _fail(c, run_id, project_id,
                                     f"The quality gate blocked on {', '.join(failing)}, and "
                                     f"{str(withheld)[0].lower()}{str(withheld)[1:]}",
                                     failure_class="needs_human", reason_code="repair_needs_person")
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
                if not _install_fail[0]:
                    from ..templates.gate import install_failure_of as _tpl_install_failure

                    _install_fail[0] = _tpl_install_failure(
                        ((_template_record or {}).get("evidence") or {}).get("template_tests"))
                _blocked_error, _blocked_reason = blocked_gate_messages(
                    qg_id, failing, app_serves_record,
                    ((_template_record or {}).get("evidence") or {}).get("template_tests"),
                    _install_fail[0], blockers=_blockers)
                # Human escape hatch: preserve the produced diff so an operator can WAIVE the failing
                # gate(s) and open the PR without a re-run. Governance is kept — gates the human does
                # NOT waive still block, and the PR still carries its own merge-gate Decision.
                pending = ([{"path": p, "content": content} for p, content in accumulated.items()]
                           if use_microvm else
                           [{"path": fc.path, "content": fc.content} for fc in sandbox.diff()])
                if "no_committed_secrets" in failing:
                    # A diff with a secret in it is not kept anywhere a person or a waiver could
                    # ship it from (the gate is unwaivable): the run keeps the gate's findings.
                    pending = []
                with _transaction(engine) as c:
                    _release(budget, c, project_id, reservation_id, f"quality gate failed: {qg_id}", run_id=run_id)
                    ctx = {**fctx, "pending_diff": pending, "failing_gates": failing,
                           "quality_gate_id": qg_id, "pr_summary": r2.get("summary", "")[:600],
                           **({"sandbox_task_id": guest_vm_ids[-1]} if guest_vm_ids else {}),
                           **({"install_failure": _install_fail[0]} if _install_fail[0] else {})}
                    # A TEMPLATE GATE CANNOT BE WAIVED (factory_view.UNWAIVABLE_GATES), so a build
                    # it stopped is not waiting on anybody's waiver: blocked and done, saying why
                    # and what to do. Parked at awaiting_gate_override, the workflow above it
                    # waited for a waiver nobody could give.
                    from ..templates.gate import hold_outcome as _tpl_hold_outcome

                    _held = _tpl_hold_outcome(
                        failing, _install_fail[0], app_serves=app_serves_record,
                        template_tests=((_template_record or {}).get("evidence") or {}).get("template_tests"),
                        secret_scan=((_template_record or {}).get("evidence") or {}).get("secret_scan"),
                        tests={"failed_check": _failed_check[0]} if _failed_check[0] else None)
                    if _held is not None:
                        ctx["template_hold"] = _held
                    c.execute(sql(
                        "UPDATE code_task_runs SET status='blocked', phase=:ph, "
                        "error=:e, finalize_ctx=CAST(:fc AS JSONB), updated_at=now() WHERE id=:id"),
                        {"e": _held["error"] if _held else _blocked_error,
                         "ph": "done" if _held else "awaiting_gate_override",
                         "fc": pg_json(ctx), "id": run_id})
                    on_status_written(c, run_id)
                _rescue_pr(_blocked_reason)
                return {"status": "blocked", "done": True, "awaiting_gate_override": _held is None,
                        "quality_gate_id": qg_id, "failing_gates": failing}

        # GATE PASSED — open the PR via the existing software-factory path.
        if _push_held("pull_request"):
            # Unreachable by design (a failing template gate never ships), kept as the last
            # door: a template build whose gates did not pass opens no PR, whatever led here.
            with _transaction(engine) as c:
                _release(budget, c, project_id, reservation_id, "template gates did not pass",
                         run_id=run_id)
                from ..templates.gate import hold_outcome as _tpl_hold_outcome

                _held = _tpl_hold_outcome([g for g in TEMPLATE_GATES
                                           if (_tpl_gates or {}).get(g) is not True],
                                          _install_fail[0],
                                          secret_scan=((_template_record or {}).get("evidence")
                                                       or {}).get("secret_scan"),
                                          template_tests=((_template_record or {}).get("evidence")
                                                          or {}).get("template_tests"),
                                          tests=({"failed_check": _failed_check[0]}
                                                 if _failed_check[0] else None))
                c.execute(sql("UPDATE code_task_runs SET status='blocked', phase='done', error=:e, "
                              "updated_at=now() WHERE id=:id"),
                          {"e": (_held or {}).get("error") or template_push_hold(_tpl_applies, _tpl_gates),
                           "id": run_id})
                on_status_written(c, run_id)
            return {"status": "blocked", "done": True, "failing_gates": [
                g for g in TEMPLATE_GATES if (_tpl_gates or {}).get(g) is not True]}
        # A FIX ROUND DOES NOT ASK FOR THE MERGE WITH A PERSON'S COMMENT UNANSWERED (BOSSAPP-948):
        # every required line comment gets Fixed (with the commit) or Disputed (with a reason).
        # One more turn of the implementer's own session, asking only for the missing answers.
        try:
            with _transaction(engine) as c:
                _frow = c.execute(sql("SELECT finalize_ctx FROM code_task_runs WHERE id=:id"),
                                  {"id": run_id}).mappings().first()
            from . import local_review as _local_review

            _missing = _local_review.unanswered_required((_frow or {}).get("finalize_ctx") or {})
            if _missing and impl_session_id:
                _req = [rc_ for rc_ in _open_operator_comments(engine, run_id, project_id)
                        if str(getattr(rc_, "ref", "") or "").startswith("lcc_")]
                _ord = review_comments.implementer_order(_req)
                _want = {str(e.get("id")) for e in _missing}
                _lines = [f"{i}. {rc_.location()} — {rc_.body}" for i, rc_ in enumerate(_ord, 1)
                          if getattr(rc_, "ref", None) in _want]
                if _lines:
                    a_ans, m_ans = _route("implement")
                    r_ans = a_ans.run_role(
                        "These line comments from a PERSON still have no answer. Answer each one "
                        "now (fix it first if it is not fixed yet):\n" + "\n".join(_lines) + "\n"
                        + review_comments.response_clause(), root, model=m_ans,
                        resume_session_id=impl_session_id)
                    cost += int(r_ans.get("cost_usd_micros") or 0)
                    _record_comment_responses(engine, run_id, project_id, _ord,
                                              r_ans.get("summary") or "", round_i=99,
                                              changed_files=[])
        except Exception:  # noqa: BLE001 — Merge stays held by the open comments (ci_gate)
            _log_caught(logger)
        _finish_vms(success=True)
        _declare_preview_app()
        _commit_approved_prd()
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
                        amount_usd_micros=_build_budget.reservation_estimate(
                            c, settings, project_id),
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
                _log_caught(logger)
                emit_event(c, project_id, "code_task.reservation_renew_failed", "boss",
                           "code_task_run", run_id, {"error": f"{type(exc).__name__}: {exc}"[:200]})
            # THE STOP WINS WHEN IT CAN (factory walk D1). The PR step is one long transaction
            # with no phase change inside, so a Stop pressed during it was only seen after the
            # PR existed. Checked right before the first GitHub write (the push and the PR).
            # CHECKS FAILING (checks_failing.py): the description OPENS with them, and the PR
            # is a draft with the label, so nobody reads it as a passing change.
            from .checks_failing import KEY as _CF_KEY, pr_section as _cf_section

            _checks = evidence.get(_CF_KEY) if isinstance(evidence.get(_CF_KEY), dict) else None
            # PAST THE LAST STAGE BOUNDARY. The final `take_pending_guidance` read is behind us;
            # from here to the terminal write no stage reads the queue again, so a steer queued
            # now would be the silent no-op this task exists to end (crun_e470d6dc). Mark it, so
            # `steer_code_task` answers in words / lands it on a follow-up instead of dropping it.
            c.execute(sql(
                "UPDATE code_task_runs SET finalize_ctx = "
                "COALESCE(finalize_ctx,'{}'::jsonb) "
                "|| jsonb_build_object('past_last_stage_boundary', true), updated_at=now() "
                "WHERE id=:id"), {"id": run_id})
            _abort_if_cancelled("before opening the PR", spends=False)
            _pr_files = ([FileChange(path=p, content=content)
                          for p, content in accumulated.items()]
                         if use_microvm else sandbox.diff())
            _pr_summary = _cf_section(_checks) + _generated_pr_summary(
                engine, settings, run_id, project_id, title=title, evidence=evidence,
                gates=gates, paths=[f.path for f in _pr_files], qg_id=qg_id)
            proposal = sf.propose_pull_request(
                c, project_id, task_id, reservation_id=reservation_id,
                dedupe_key=f"code:task:{run_id}", title=title,
                branch_name=work_branch,
                summary=_pr_summary + agent_acts.pr_trailers(c, run_id),
                files=_pr_files,
                created_by_job_id=job_id, actual_usd_micros=cost,
                evidence=evidence, quality_gate_id=qg_id, run_id=run_id,
                pr_base=(_release_target["pr_base"] if _release_target else repo_base_branch),
                **({"checks_failing": _checks} if _checks else {}),
                monitoring_removals=_monitoring_removals(spec))

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
                            amount_usd_micros=_build_budget.reservation_estimate(
                                c, settings, project_id),
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
                    _log_caught(logger)
                    emit_event(
                        c, project_id, "code_task.reservation_renew_failed", "boss",
                        "code_task_run", run_id,
                        {"attempt": _pr_attempt, "error": f"{type(exc).__name__}: {exc}"[:200]},
                    )
                _abort_if_cancelled("before retrying the PR", spends=False)
                try:
                    proposal = sf.propose_pull_request(
                        c, project_id, task_id, reservation_id=reservation_id,
                        dedupe_key=f"code:task:{run_id}", title=title,
                        branch_name=work_branch,
                        summary=_pr_summary + agent_acts.pr_trailers(c, run_id),
                        files=_pr_files,
                        created_by_job_id=job_id, actual_usd_micros=cost,
                        evidence=evidence, quality_gate_id=qg_id, run_id=run_id,
                        pr_base=(_release_target["pr_base"] if _release_target else repo_base_branch),
                        **({"checks_failing": _checks} if _checks else {}),
                        monitoring_removals=_monitoring_removals(spec))
                    status = proposal.get("status")
                except Exception as exc:  # noqa: BLE001
                    _log_caught(logger)
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
        if status not in ("created", "succeeded", "blocked", "reused"):
            # THE BUILD IS DONE; ONLY THE DELIVERY FAILED (GitHub/network, after its in-place
            # retries). Its diff, summary and gate verdict are kept on the run, and Retry runs
            # ONLY the push and the pull request (code_task_service.redeliver_code_task) — no new
            # design, tests, implementation or review. It used to block, or crash into a full
            # automatic rebuild, after every gate had passed. A `blocked` answer (it waits on a
            # person's decision) and a `reused` effect keep their own handling below.
            _save_workspace("delivery_failed")
            with _transaction(engine) as c:
                _release(budget, c, project_id, reservation_id,
                         "the build passed its gates; delivering it to GitHub failed", run_id=run_id)
                return hold_delivery(
                    c, run_id, project_id,
                    files=[{"path": f.path, "content": f.content} for f in _pr_files],
                    summary=_pr_summary, quality_gate_id=qg_id, branch=work_branch,
                    detail=str(proposal.get("message") or proposal.get("error") or status))
        if status not in ("created", "succeeded") and not _reused_pr:
            rescued = _rescue_pr(f"the pull-request step failed 3 times ({status})")
            with _transaction(engine) as c:
                c.execute(sql("UPDATE code_task_runs SET status='blocked', phase='done', "
                               "decision_id=:d, error=:e, updated_at=now() WHERE id=:id"),
                          {"d": proposal.get("decision_id"),
                           "e": (f"PR not created after 3 attempts: {status}"
                                 + (f" — partial work shipped as {rescued}" if rescued else "")),
                           "id": run_id})
                on_status_written(c, run_id)
            return {"status": "blocked", "done": True, "quality_gate_id": qg_id, **proposal}

        # The findings go ON the pull request, at the lines they are about.
        evidence["review_comments_posted"] = _post_review_comments_to_pr(
            sf, proposal, evidence, engine, project_id, run_id)

        # WAIT FOR CI before the deploy approval (services/ci_gate.py). Blocking and bounded
        # (BOSS_CI_WAIT_TIMEOUT_SECONDS), each look in its own short transaction. A failure or a
        # timeout is recorded against the pull request, and the merge and deploy approvals
        # refuse until CI passes; a repository with no CI is said so and not waited on.
        if int(getattr(settings, "ci_wait_timeout_seconds", 0) or 0) > 0:
            with _transaction(engine) as c:
                _phase(c, "waiting_for_ci")
            evidence["ci_wait"] = _wait_for_ci(engine, settings, sf, proposal, project_id,
                                               run_id, f"{owner}/{repo}")

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
        # CI FAILED AFTER THE PR OPENED: one bounded repair round in the same lineage
        # (repair_after_ci_failure). After the terminal write, so the attempt chains off a
        # settled run; the merge/deploy approvals stay refused meanwhile (ci_gate).
        ci_repair_run = repair_after_ci_failure(engine, settings, jobs, run_id, project_id,
                                                fctx, evidence.get("ci_wait") or {})
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
            _log_caught(logger)
            logger.warning("retrospective not recorded for %s", run_id, exc_info=True)

        # AUTO-PREVIEW FOR APPS — in the background, never on this job (auto_preview_after_pr).
        if not fctx.get("is_followup"):
            auto_preview_after_pr(engine, settings, project_id=project_id, run_id=run_id,
                                  accumulated=accumulated)
        # DEFERRED FINDINGS → their own task on the shipped branch. After the PR exists, so a
        # bookkeeping failure can never cost the delivery; guarded so follow-ups don't recurse.
        followup_run_id = None
        # Never a follow-up on a PR the operator already merged or closed by hand — three
        # spawned in a row against one (CX PR #24) before this existed.
        _followup_ok, _followup_skip_reason = True, ""
        _pr_number = proposal.get("pull_request_number")
        if _pr_number:
            try:
                _followup_ok, _followup_skip_reason = should_create_followup_for_pr(
                    sf.github, pull_request_number=_pr_number)
            except Exception:  # noqa: BLE001 — a check that cannot run must not block the PR
                _log_caught(logger)
                _followup_ok, _followup_skip_reason = True, ""
        if not _followup_ok:
            with _transaction(engine) as c:
                emit_event(c, project_id, "code_task.followup_skipped", "boss",
                           "code_task_run", run_id, {"reason": _followup_skip_reason})
        # ONE OWNER PER PULL REQUEST (code_task_service.lineage_branch_owner). A workflow whose
        # More tests pass has not started yet hands the deferred findings to THAT pass — it
        # extends this same branch and PR next — instead of starting a second run on the branch
        # beside it; and a follow-up never starts while another run of the lineage works there.
        if (deferred_findings and _followup_ok and not _checks
                and not fctx.get("is_followup") and fctx.get("workflow_run_id")):
            _followup_ok, _followup_skip_reason = _findings_to_pass_two(
                engine, project_id, run_id, str(fctx.get("workflow_run_id")),
                deferred_findings)
        if deferred_findings and _followup_ok and not fctx.get("is_followup"):
            from .code_task_service import lineage_branch_owner

            with _transaction(engine) as c:
                _owner = lineage_branch_owner(c, project_id, str(fctx.get("root_run_id")
                                                                 or run_id),
                                              exclude=(run_id,))
            if _owner:
                _followup_ok = False
                _followup_skip_reason = (f"{_owner['id']} is already working on this branch; "
                                         "one run owns a pull request at a time")
                with _transaction(engine) as c:
                    emit_event(c, project_id, "code_task.followup_skipped", "boss",
                               "code_task_run", run_id,
                               {"reason": _followup_skip_reason, "branch_owner": _owner["id"],
                                "findings": deferred_findings[:20]})
        # A PR that opened with its checks failing always gets its follow-up: it is the fix.
        if (deferred_findings
                and (getattr(settings, "code_review_followup_tasks", True) or _checks)
                and not fctx.get("is_followup")
                and _followup_ok
                and not _already_spawned_followup(engine, run_id)):
            try:
                followup_run_id = _spawn_deferred_findings_task(
                    engine, settings, jobs, project_id=project_id, task_id=task_id,
                    spec=spec, base_branch=work_branch, findings=deferred_findings,
                    parent_run_id=run_id, parent_title=title,
                    parent_root=(fctx.get("root_run_id") or run_id),
                    parent_origin=fctx.get("origin"),
                    checks_failing=_checks,
                    parent_workflow_run_id=fctx.get("workflow_run_id"))
                with _transaction(engine) as c:
                    emit_event(c, project_id, "code_task.deferred_findings_task", "boss",
                               "code_task_run", run_id,
                               {"followup_run_id": followup_run_id,
                                "findings": deferred_findings[:20],
                                **({"checks_failing": True} if _checks else {})})
                    if _checks and followup_run_id:
                        # Where its fix is being built: the build page and the merge card
                        # name the follow-up next to the failing checks.
                        c.execute(sql(
                            "UPDATE code_task_runs SET finalize_ctx = jsonb_set("
                            "finalize_ctx, '{checks_failing,followup_run_id}', "
                            "to_jsonb(CAST(:f AS text))) WHERE id = :id "
                            "AND finalize_ctx ? 'checks_failing'"),
                            {"f": str(followup_run_id), "id": run_id})
            except Exception as exc:  # noqa: BLE001 — never fail a shipped PR on follow-up bookkeeping
                _log_caught(logger)
                with _transaction(engine) as c:
                    emit_event(c, project_id, "code_task.deferred_findings_task_failed", "boss",
                               "code_task_run", run_id,
                               {"error": f"{type(exc).__name__}: {exc}"[:300],
                                "findings": deferred_findings[:20]})
        return {"status": "completed", "done": True, "quality_gate_id": qg_id,
                "deferred_findings": deferred_findings,
                "followup_run_id": followup_run_id,
                **({"ci_repair_run_id": ci_repair_run} if ci_repair_run else {}),
                **({"checks_failing": _checks} if _checks else {}), **proposal}

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
            _log_caught(logger)
            pass
        return {"status": "superseded", "done": True, "reason": str(exc)}
    except CodeTaskCancelled as exc:
        return _honour_cancel(exc)
    except Exception as exc:  # noqa: BLE001 — fail the run gracefully on any agent/model error
        # WHERE IT DIED, NOT ONLY WHAT IT SAID (crash_frames): crun_944194c4 failed with a bare
        # "I/O operation on closed file." and no traceback anywhere. The log gets the whole
        # (redacted) traceback; the run keeps its last frames — file, line, function, no values.
        _log_caught(logger, f"run {run_id} failed")
        try:
            from .crash_frames import record as _crash_record

            with _transaction(engine) as c:
                c.execute(sql("UPDATE code_task_runs SET state = COALESCE(state, '{}'::jsonb) || "
                              "jsonb_build_object('crash_frames', CAST(:f AS jsonb)) WHERE id = :id"),
                          {"f": json.dumps(_crash_record(exc)), "id": run_id})
        except Exception:  # noqa: BLE001 — the record must never mask the failure itself
            _log_caught(logger, "storing crash_frames")
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
        # THE ENGINE COULD NOT RUN (harness_failures) — whatever exception carried the run here
        # (a stage may have swallowed the HarnessFailure and died of what followed), the verdict
        # is the harness's: "BOSS could not run <engine>: <message>; fix: <fix>.", needs_human,
        # Retry after the fix. No automatic retry (it meets the same engine), no failover.
        from .harness_failures import failure_of as _harness_failure_of
        harness = _harness_failure_of(exc, run_id)
        # A blank reason makes every retry blind — always record at least the exception type
        # and the substrate the run was on (that's the first question when root-causing).
        reason = (harness.sentence() if harness is not None
                  else (str(exc).strip() or f"{type(exc).__name__} with no message"))
        reason = f"{reason} [substrate={execution_substrate}]"
        from .code_task_service import classify_pipeline_exception
        from .spend_cap_failover import apply_spend_cap_failover
        fclass, rcode = classify_pipeline_exception(reason)
        if harness is not None:
            fclass, rcode = "needs_human", "harness_unavailable"
            try:
                with _transaction(engine) as c:
                    emit_event(c, project_id, "code_task.harness_failed", "boss",
                               "code_task_run", run_id,
                               {**harness.as_dict(), "phase": _current_phase_name(engine, run_id),
                                "retryable_after_fix": True})
            except Exception:  # noqa: BLE001 — the record must never mask the failure itself
                _log_caught(logger)
        if isinstance(exc, StackUnsupportedError):
            # A stack refusal is a decision about the design, not a platform fault: it must not
            # read as `infra`, which starts an automatic retry that is refused all over again.
            fclass, rcode = "needs_human", "stack_unsupported"
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
            failover = None if harness is not None else apply_spend_cap_failover(
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
            # Not gated on an existing decision_id: a later capacity failure still
            # opens a card when the run already holds a plan-approval decision.
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
                    _log_caught(logger)
                    pass
            elif (
                not (run or {}).get("decision_id")
                and fclass == "needs_human"
                and rcode == "rules_forbidden"
            ):
                # A rules 403 is not retried. Only this path skips a second card
                # when the run already holds a decision.
                try:
                    decision_id = create_blocking_decision(
                        c, project_id, task_id,
                        (
                            "Egress or git refused this build with a rules 403 "
                            "(the allow-list or the installation). A rules 403 is not "
                            "a 5xx or a timeout, so this build is not retried. Allow "
                            "the repository, or point the build at one the rules will clone."
                        ),
                        {
                            "action": "allow_repository_or_change_clone_target",
                            "run_id": run_id,
                            "reason_code": rcode,
                            "failure_class": fclass,
                            "failure_reason": reason[:500],
                        },
                        {},
                        "rules_forbidden",
                    )
                    c.execute(
                        sql("UPDATE code_task_runs SET decision_id=:d, updated_at=now() "
                            "WHERE id=:id AND decision_id IS NULL"),
                        {"d": decision_id, "id": run_id},
                    )
                    emit_event(
                        c, project_id, "code_task.rules_forbidden_escalated", "boss",
                        "code_task_run", run_id,
                        {"decision_id": decision_id, "reason_code": rcode},
                    )
                except Exception:  # noqa: BLE001 — never mask the original failure
                    _log_caught(logger)
                    pass
            result = _fail(c, run_id, project_id, reason,
                           failure_class=fclass, reason_code=rcode)
            if decision_id:
                result = {**result, "decision_id": decision_id}
        if fclass == "infra":
            # Committed first: the retry reads this run as failed, and the workflow step that
            # waits on it follows the successor (workflow_capability_runner._successor_attempt).
            result = _retry_after_infra_failure(engine, settings, jobs, run_id, project_id,
                                                fctx, result)
        return result
    finally:
        try:
            if identity_run is not None:
                # Run end, FIRST: its tokens, egress credential, "this build only" grant and GitHub
                # installation tokens all end with it — before anything below that could raise
                # (a teardown that fails must never leave the run's credentials alive).
                from .code_task_service import _identity_org

                identity_runs.finish_code_task_run(engine, identity_run[0], org_id=_identity_org(engine, project_id))
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
            try:
                if sandbox is not None:
                    sandbox.teardown()
            finally:
                if review_sandbox is not None:
                    review_sandbox.teardown()


def _deploy_manifests_in(root: Any) -> list[str]:
    """The boss.deploy.yaml files in the build's checkout; [] when it cannot be read."""
    try:
        from .app_deploy_manifest import manifests_in

        return manifests_in(root)
    except Exception:  # noqa: BLE001 — a record of the tree must never fail a build
        logger.warning("could not list the deploy manifests in %s", root, exc_info=True)
        return []


def repository_stack_verdict(path: Any):
    """The stack verdict of the code a repository already holds, or None when it holds none
    (a new app, where the brief decides). Unsupported or switched-off languages it USES refuse."""
    from pathlib import Path

    from . import stacks

    if not path or not Path(str(path)).is_dir():
        return None
    services = stacks.detect_services(Path(str(path)))
    if not services:
        return None
    unsupported = [s.language for s in services if not s.supported]
    off = [s.language for s in services if s.supported and not stacks.language_ready(s.language)]
    return stacks.verdict(unsupported, off, where="This repository")
