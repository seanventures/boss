"""Durable coding-agent task — the pipeline that turns a task spec into a real,
tested, gated pull request.

It composes the pieces built for this feature with the *existing* software-factory
back half:

    reserve budget
      -> EphemeralSandbox.provision (clone the repo)
      -> CodeAgent.run            (edit files + iterate against tests in the sandbox)
      -> BOSS re-runs the tests   (authoritative pass/fail)
      -> propose_pull_request     (commits the diff, opens a receipted PR,
                                   creates the review_pull_request merge gate,
                                   settles the budget)            <-- reused
      -> teardown sandbox

Durability model (see code_task_runs DDL): the sandbox working tree is
filesystem-ephemeral, so the whole pipeline runs inside ONE `code.task` job.
Crash-resumability is provided by the job lease-reclaim (a crash re-runs the
attempt) plus the PR receipt dedupe (dedupe_key=code:task:<run_id>), which makes
re-opening the PR a no-op. If the run already recorded a PR url, the handler is a
no-op (idempotent).
"""
from __future__ import annotations

import json
from typing import Optional

from sqlalchemy import text

from ..config import Settings
from ..db import begin
from .code_agent import CodeTaskSpec, RepoContext, make_code_agent
from .core import emit_event, make_id
from .github_app_adapter import GitHubAppAdapter
from .sandbox import SandboxError
from .sandbox_provider import make_sandbox_provider
from .software_factory import SoftwareFactoryService


# --------------------------------------------------------------------------- #
#  Persistence helpers                                                        #
# --------------------------------------------------------------------------- #
def _row_to_run(r) -> dict:
    def _j(v):
        if isinstance(v, str):
            try:
                return json.loads(v)
            except Exception:
                return {}
        return v or {}

    return {
        "id": r["id"], "project_id": r["project_id"], "task_id": r["task_id"],
        "status": r["status"], "phase": r["phase"], "title": r["title"],
        "spec": _j(r["spec"]), "state": _j(r["state"]), "usage": _j(r["usage"]),
        "finalize_ctx": _j(r["finalize_ctx"]), "reservation_id": r["reservation_id"],
        "pull_request_url": r["pull_request_url"], "pull_request_number": r["pull_request_number"],
        "decision_id": r["decision_id"], "error": r["error"],
    }


def load_code_task_run(connection, run_id: str) -> Optional[dict]:
    r = connection.execute(
        text("SELECT id, project_id, task_id, status, phase, title, spec, state, usage, "
             "finalize_ctx, reservation_id, pull_request_url, pull_request_number, decision_id, error "
             "FROM code_task_runs WHERE id = :id"),
        {"id": run_id},
    ).mappings().first()
    return _row_to_run(r) if r else None


def list_code_task_runs(connection, project_id: str, limit: int = 50) -> list[dict]:
    rows = connection.execute(
        text("SELECT id, project_id, task_id, status, phase, title, spec, state, usage, "
             "finalize_ctx, reservation_id, pull_request_url, pull_request_number, decision_id, error "
             "FROM code_task_runs WHERE project_id = :p ORDER BY created_at DESC LIMIT :lim"),
        {"p": project_id, "lim": int(limit)},
    ).mappings().all()
    return [_row_to_run(r) for r in rows]


_DEPLOYABILITY_NOTE = (
    "\n\nDEPLOYABILITY (required for any network service): bind the host from the HOST env var "
    "(default 0.0.0.0) and the port from the PORT env var, with sensible fallbacks. Do NOT hardcode "
    "127.0.0.1 or a fixed port for the running server — it must run in a container / behind a proxy. "
    "(Unit tests may still bind loopback + an ephemeral port.) Binding 0.0.0.0 here is expected for a "
    "containerized internal tool behind the platform proxy — it is not a vulnerability."
)


def start_code_task(
    connection, *, project_id: str, task_id: Optional[str], title: str,
    instructions: str, acceptance_criteria: list[str], test_commands: list[str],
    owner: str, repo: str, base_branch: str, finalize_ctx: Optional[dict],
    jobs, profile: Optional[str] = None, risk_tier: Optional[int] = None,
) -> str:
    """Create the durable run row and enqueue the single `code.task` job."""
    run_id = make_id("crun")
    spec = {
        "instructions": instructions + _DEPLOYABILITY_NOTE,
        "acceptance_criteria": acceptance_criteria or [],
        "test_commands": test_commands or [],
        "owner": owner, "repo": repo, "base_branch": base_branch,
    }
    if profile:  # per-task cost/rigor override (else the project setting applies)
        spec["profile"] = profile
    if isinstance(risk_tier, int):
        spec["risk_tier"] = risk_tier
    connection.execute(
        text("""
            INSERT INTO code_task_runs (id, project_id, task_id, status, phase, title, spec, finalize_ctx)
            VALUES (:id, :p, :t, 'running', 'queued', :title, CAST(:spec AS JSONB), CAST(:fc AS JSONB))
        """),
        {"id": run_id, "p": project_id, "t": task_id, "title": title,
         "spec": json.dumps(spec), "fc": json.dumps(finalize_ctx or {})},
    )
    jobs.create_job(connection, project_id=project_id, task_id=task_id,
                    kind="code.task", priority=46, payload={"run_id": run_id})
    emit_event(connection, project_id, "code_task.started", "boss", "code_task_run", run_id,
               {"task_id": task_id, "title": title, "repo": f"{owner}/{repo}"})
    return run_id


def retry_code_task(engine, settings: Settings, jobs, run_id: str, *, fix_note: str = "") -> dict:
    """Resume a failed/blocked code task as a NEW attempt in the same lineage — carrying the prior
    attempt's findings forward as explicit feedback so the agent fixes the identified issues instead
    of regenerating blind. History is preserved: each attempt links to its parent + root run, and
    accumulates the findings it must address."""
    with begin(engine) as c:
        prev = load_code_task_run(c, run_id)
    if prev is None:
        raise ValueError(f"code task run not found: {run_id}")
    spec = prev.get("spec") or {}
    state = prev.get("state") or {}
    fctx_prev = prev.get("finalize_ctx") or {}
    attempt = int(fctx_prev.get("attempt", 1)) + 1
    root = fctx_prev.get("root_run_id") or run_id

    parts = []
    if prev.get("error"):
        parts.append(f"Outcome: {prev['error']}")
    for label, key in (("Security review", "security_summary"),
                       ("Code review", "review_summary"),
                       ("Consistency review", "consistency_summary")):
        if state.get(key):
            parts.append(f"## {label} findings (previous attempt)\n{state[key]}")
    feedback = "\n\n".join(parts) or "(no detailed findings were recorded on the previous attempt)"

    augmented = (
        (spec.get("instructions") or "")
        + f"\n\n=== PREVIOUS ATTEMPT {attempt - 1} FEEDBACK — fix ONLY these issues; preserve everything that already passed ===\n"
        + feedback
        + (f"\n\nOperator note: {fix_note}" if fix_note else "")
    )
    base_title = (prev.get("title") or "Code task").split(" · retry")[0]
    title = f"{base_title} · retry {attempt - 1}"

    with begin(engine) as c:
        new_id = start_code_task(
            c, project_id=prev["project_id"], task_id=prev.get("task_id"), title=title,
            instructions=augmented,
            acceptance_criteria=spec.get("acceptance_criteria") or [],
            test_commands=spec.get("test_commands") or [],
            owner=spec.get("owner") or settings.target_github_owner,
            repo=spec.get("repo") or settings.target_github_repo,
            base_branch=spec.get("base_branch") or "main",
            finalize_ctx={**fctx_prev, "parent_run_id": run_id, "root_run_id": root, "attempt": attempt},
            jobs=jobs, profile=spec.get("profile"), risk_tier=spec.get("risk_tier"),
        )
    return {"status": "queued", "run_id": new_id, "attempt": attempt,
            "parent_run_id": run_id, "root_run_id": root}


def override_code_task(engine, settings: Settings, jobs, run_id: str, *,
                       waive_gates: list[str], actor: str = "operator", reason: str = "") -> dict:
    """Human gate override: waive the named failed gate(s) and open the PR from the diff that was
    preserved at gate-failure time — NO re-run. Gates the human does NOT waive still block, and the
    opened PR keeps its own merge-gate Decision (this opens, it does not merge)."""
    from ..schemas import FileChange
    with begin(engine) as c:
        run = load_code_task_run(c, run_id)
    if run is None:
        raise ValueError(f"code task run not found: {run_id}")
    if run.get("pull_request_url"):
        return {"status": "completed", "pull_request_url": run["pull_request_url"], "already_open": True}
    fctx = run.get("finalize_ctx") or {}
    failing = fctx.get("failing_gates") or []
    pending = fctx.get("pending_diff")
    if not pending:
        raise ValueError("no preserved diff — only runs blocked at 'awaiting_gate_override' can be overridden.")
    waive = list(waive_gates or [])
    if "*" in waive or "all" in waive:
        waive = list(failing)
    not_waived = [g for g in failing if g not in waive]
    if not_waived:
        raise ValueError(f"refused: failing gates not waived: {not_waived}. "
                         "Waive them explicitly to proceed (or leave them to keep blocking).")
    waive_gates = waive
    files = [FileChange(path=f["path"], content=f["content"]) for f in pending]
    sf = SoftwareFactoryService(settings)
    project_id = run["project_id"]
    task_id = run.get("task_id")
    qg = fctx.get("quality_gate_id")
    with begin(engine) as c:
        reservation = sf.budget.reserve(c, project_id, category="code_task",
            amount_usd_micros=200_000, reason=f"gate-override PR {run_id[:12]}", expires_in_minutes=15)
        _set_phase(c, run_id, "opening_pr")
        proposal = sf.propose_pull_request(
            c, project_id, task_id, reservation_id=reservation.get("reservation_id"),
            dedupe_key=f"code:task:{run_id}", title=run["title"],
            branch_name=f"boss/code/{run_id}",
            summary=(f"⚠️ GATE OVERRIDE by {actor}: waived {waive_gates} (QualityGate {qg}).\n"
                     f"Reason: {reason or '(none given)'}\n\n{fctx.get('pr_summary', '')}"),
            files=files, created_by_job_id=fctx.get("created_by_job_id"))
    status = proposal.get("status")
    if status not in ("created", "succeeded"):
        return {"status": "error", "detail": f"PR not created: {status}", **proposal}
    with begin(engine) as c:
        c.execute(text("UPDATE code_task_runs SET status='completed', phase='done', "
                       "pull_request_url=:u, pull_request_number=:n, decision_id=:d, error=:e, "
                       "updated_at=now() WHERE id=:id"),
                  {"u": proposal.get("pull_request_url"), "n": proposal.get("pull_request_number"),
                   "d": proposal.get("decision_id"),
                   "e": f"Gate {qg} OVERRIDDEN by {actor}: waived {waive_gates}.", "id": run_id})
        emit_event(c, project_id, "code_task.gate_overridden", actor, "code_task_run", run_id,
                   {"waived_gates": waive_gates, "reason": reason, "quality_gate_id": qg,
                    "pull_request_url": proposal.get("pull_request_url")})
    return {"status": "completed", "run_id": run_id, "waived_gates": waive_gates,
            "pull_request_url": proposal.get("pull_request_url"),
            "pull_request_number": proposal.get("pull_request_number"),
            "decision_id": proposal.get("decision_id")}


def _release(sf, connection, project_id: str, reservation_id: Optional[str], reason: str) -> None:
    if not reservation_id:
        return
    try:
        sf.budget.release(connection, project_id, reservation_id, reason)
    except Exception:
        pass


def _set_phase(connection, run_id: str, phase: str) -> None:
    connection.execute(
        text("UPDATE code_task_runs SET phase = :ph, updated_at = now() WHERE id = :id"),
        {"ph": phase, "id": run_id},
    )


def _fail(connection, run_id: str, project_id: str, reason: str, *, status: str = "failed") -> dict:
    connection.execute(
        text("UPDATE code_task_runs SET status=:st, phase='done', error=:e, updated_at=now() WHERE id=:id"),
        {"st": status, "e": reason[:2000], "id": run_id},
    )
    emit_event(connection, project_id, "code_task.failed", "boss", "code_task_run", run_id,
               {"reason": reason[:300], "status": status})
    return {"status": status, "done": True, "error": reason}


# --------------------------------------------------------------------------- #
#  The job handler                                                            #
# --------------------------------------------------------------------------- #
def run_code_task(engine, settings: Settings, jobs, run_id: str) -> dict:
    """Execute one full code-task attempt. Safe to re-run after a crash."""
    with begin(engine) as c:
        run = load_code_task_run(c, run_id)
    if run is None:
        return {"status": "not_found", "done": True}
    if run["status"] != "running" or run["pull_request_url"]:
        # already terminal or PR already opened — idempotent no-op
        return {"status": run["status"], "done": True,
                "pull_request_url": run["pull_request_url"], "decision_id": run["decision_id"]}

    if not settings.enable_code_task:
        with begin(engine) as c:
            return _fail(c, run_id, run["project_id"],
                         "Code tasks are disabled (BOSS_ENABLE_CODE_TASK=false).", status="blocked")

    # Default for all coding tasks: route through the governed, role-separated pipeline.
    if settings.governed_coding_pipeline:
        from .governed_code_task_service import run_governed_code_task
        return run_governed_code_task(engine, settings, jobs, run_id)

    project_id = run["project_id"]; task_id = run["task_id"]; spec = run["spec"]
    fctx = run["finalize_ctx"] or {}
    sf = SoftwareFactoryService(settings)

    # 1) Reserve budget for this code task's model spend.
    with begin(engine) as c:
        reservation = sf.budget.reserve(
            c, project_id, category="code_task",
            amount_usd_micros=int(settings.code_task_budget_usd_micros),
            reason=f"code task: {run['title'][:80]}",
            expires_in_minutes=30, job_id=fctx.get("created_by_job_id"),
        )
    if reservation.get("status") != "reserved":
        with begin(engine) as c:
            return _fail(c, run_id, project_id,
                         f"Budget reservation denied: {reservation.get('status')}", status="blocked")
    reservation_id = reservation["reservation_id"]
    with begin(engine) as c:
        c.execute(text("UPDATE code_task_runs SET reservation_id=:r WHERE id=:id"),
                  {"r": reservation_id, "id": run_id})

    # 2) Resolve a clone token: prefer a short-lived GitHub-App installation token
    #    (Phase 2 impersonation); fall back to the PAT (Phase 0).
    owner = spec.get("owner") or settings.target_github_owner
    repo = spec.get("repo") or settings.target_github_repo
    base_branch = spec.get("base_branch") or "main"
    app = GitHubAppAdapter(settings)
    token = app.token_for_repo(owner, repo) if app.configured else settings.github_token

    # The provider decides the substrate + WHERE the agent runs.
    #  - microVM/container provider: the whole pipeline runs guest-side; we send a
    #    spec and collect the result (run_code_task).
    #  - local provider: the control plane drives the agent against a host sandbox.
    provider = make_sandbox_provider(settings)
    sandbox = None
    try:
        if hasattr(provider, "run_code_task"):
            with begin(engine) as c:
                _set_phase(c, run_id, "coding")
            guest_spec = {
                "owner": owner, "repo": repo, "base_branch": base_branch,
                "title": run["title"], "instructions": spec.get("instructions", ""),
                "acceptance_criteria": spec.get("acceptance_criteria", []),
                "test_commands": spec.get("test_commands", []),
                "engine": settings.sandbox_guest_engine,
                "openai_api_key": settings.openai_api_key, "model": settings.openai_model,
                # provider-agnostic: base_url selects OpenAI/Grok/Together/etc.; claude token for claude_code
                "openai_base_url": settings.openai_base_url,
                "claude_oauth_token": settings.claude_code_oauth_token,
                "azure_endpoint": settings.azure_openai_endpoint,
                "azure_deployment": settings.azure_openai_deployment,
                "azure_api_version": settings.azure_openai_api_version,
                "azure_key": settings.azure_openai_key,
                "token": token, "timeout": settings.code_agent_timeout_seconds,
            }
            result = provider.run_code_task(guest_spec, tenant_id=project_id)
        else:
            sandbox = provider.acquire(task_id=run_id, tenant_id=project_id)
            with begin(engine) as c:
                _set_phase(c, run_id, "provisioning")
            sandbox.provision(owner, repo, base_branch=base_branch,
                              work_branch=f"boss/code/{run_id}", token=token)
            with begin(engine) as c:
                _set_phase(c, run_id, "coding")
            agent = make_code_agent(settings)
            code_spec = CodeTaskSpec(
                task_id=run_id, title=run["title"],
                instructions=spec.get("instructions", ""),
                acceptance_criteria=spec.get("acceptance_criteria", []),
                test_commands=spec.get("test_commands", []),
            )
            result = agent.run(code_spec, RepoContext(owner=owner, repo=repo, base_branch=base_branch), sandbox)

        # 5) Record agent outcome (governance metadata only — never the patch).
        with begin(engine) as c:
            _set_phase(c, run_id, "validating")
            c.execute(
                text("UPDATE code_task_runs SET state=CAST(:s AS JSONB), "
                     "usage=jsonb_build_object('cost_usd_micros', CAST(:cost AS bigint), 'model', CAST(:m AS text)), "
                     "updated_at=now() WHERE id=:id"),
                {"s": json.dumps(result.to_metadata()), "cost": int(result.cost_usd_micros or 0),
                 "m": result.usage.get("model"), "id": run_id},
            )

        if not result.diff:
            with begin(engine) as c:
                _release(sf, c, project_id, reservation_id, "no changes produced")
                return _fail(c, run_id, project_id, "Agent produced no file changes.")
        if not result.tests_passed:
            with begin(engine) as c:
                _release(sf, c, project_id, reservation_id, "tests did not pass")
                return _fail(c, run_id, project_id,
                             "Tests did not pass; not opening a PR. See run state for details.")

        # 6) Open the PR via the EXISTING software-factory path (receipt dedupe,
        #    CI wait, merge-gate Decision, budget settle all handled there).
        with begin(engine) as c:
            _set_phase(c, run_id, "opening_pr")
            proposal = sf.propose_pull_request(
                c, project_id, task_id,
                reservation_id=reservation_id,
                dedupe_key=f"code:task:{run_id}",
                title=f"{run['title']}",
                branch_name=f"boss/code/{run_id}",
                summary=result.summary,
                files=result.diff,
                created_by_job_id=fctx.get("created_by_job_id"),
            )

        status = proposal.get("status")
        if status not in ("created", "succeeded"):
            # blocked by data-boundary / routing / missing reservation, etc.
            with begin(engine) as c:
                c.execute(
                    text("UPDATE code_task_runs SET status='blocked', phase='done', "
                         "decision_id=:d, error=:e, updated_at=now() WHERE id=:id"),
                    {"d": proposal.get("decision_id"), "e": f"PR not created: {status}", "id": run_id},
                )
            return {"status": "blocked", "done": True, **proposal}

        with begin(engine) as c:
            c.execute(
                text("UPDATE code_task_runs SET status='completed', phase='done', "
                     "pull_request_url=:u, pull_request_number=:n, decision_id=:d, updated_at=now() WHERE id=:id"),
                {"u": proposal.get("pull_request_url"), "n": proposal.get("pull_request_number"),
                 "d": proposal.get("decision_id"), "id": run_id},
            )
            emit_event(c, project_id, "code_task.pr_opened", "boss", "code_task_run", run_id,
                       {"pull_request_url": proposal.get("pull_request_url"),
                        "decision_id": proposal.get("decision_id")})
        return {"status": "completed", "done": True, **proposal}

    except (SandboxError, RuntimeError) as exc:
        with begin(engine) as c:
            _release(sf, c, project_id, reservation_id, f"code task error: {exc}")
            return _fail(c, run_id, project_id, str(exc))
    finally:
        if sandbox is not None:
            sandbox.teardown()
