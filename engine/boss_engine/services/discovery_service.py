"""Discovery pipeline — research-driven outcome discovery.

Given a seed (a raw idea / problem / signal) plus optional operator context, runs a governed,
web-research pipeline and produces a cited **Discovery Dossier** with candidate outcomes:

    signal_sweep (web) -> synthesize -> adversarial vet (web) -> shape outcomes -> evidence gate

The moat, applied to research: every finding is cited, the thesis is adversarially vetted, and a
fail-closed evidence gate decides "pursue" vs "needs more research". See
docs/product/BOSS_DISCOVERY_PIPELINE_DESIGN.md.

D1 scope: web-only signals. In Community a discovery is a durable `discovery.run` job that a
worker leases, and every research stage is reserved, leased, receipted and settled on its own
(discovery_spend.py); a retry replays stages it already paid for instead of calling them again.
"""
from __future__ import annotations

import json
import threading
import traceback
from typing import Callable, Optional

from sqlalchemy import text

from ..config import Settings
from ..db import begin, get_engine
from .code_agent import make_code_agent
from .core import create_blocking_decision, emit_event, make_id

PHASES = ["queued", "signal_sweep", "synthesize", "vet", "shape", "gate", "done"]
#: Recorded on a run that was running on the old in-process thread when Community restarted.
COMMUNITY_DISCOVERY_BLOCK_REASON = (
    "This research was running in-process when BOSS restarted, before Discovery ran as a "
    "governed job; its model spend must be reconciled before it is retried."
)


def _research_runs_as_a_job(settings: Settings) -> bool:
    """Community runs Discovery as a governed worker job. Other editions keep the legacy
    in-process path until they are qualified separately (DISCOVERY_COMMUNITY_GA_BLOCKER.md)."""
    return str(settings.boss_edition).strip().lower() in {"community", "oss", "local"}


def community_research_paused(settings: Settings) -> bool:
    """Kept for the workflow research step until it is reconciled with the governed job."""
    return _research_runs_as_a_job(settings)


class DiscoveryResearchUnavailable(RuntimeError):
    """The research agent ran but gathered nothing — a broken tool, not a weak idea."""


def _record_stage(dossier: dict, stage: str, result: dict, *, parsed: dict) -> None:
    """Keep enough of each research call to diagnose one that came back empty.

    Every stage parsed the agent's reply and threw the reply away. When the sweep returned no
    findings there was nothing left to say WHY: no turn count, no cost, not a single character
    of what the agent actually said. The run looked like a considered "needs more research"
    verdict, and the evidence that it was a tooling failure had already been discarded.

    Cheap by design: counts and a short excerpt, only enough to tell "the agent never searched"
    from "the agent searched and found nothing" from "the reply did not parse".
    """
    summary = str(result.get("summary") or "")
    diagnostics = dossier.setdefault("_diagnostics", {})
    diagnostics[stage] = {
        "turns": result.get("turns"),
        "cost_usd_micros": result.get("cost_usd_micros"),
        "model": result.get("model"),
        "reply_chars": len(summary),
        "parsed_keys": sorted(parsed.keys()) if isinstance(parsed, dict) else [],
        "parse_ok": bool(parsed),
        # The head, not the tail: an agent that could not search says so in its opening line,
        # and a stack trace's closing frames are the least informative part of any failure.
        "reply_head": summary[:1200],
    }


# --------------------------------------------------------------------------- #
#  Public API                                                                 #
# --------------------------------------------------------------------------- #
def start_discovery(*, project_id: str, title: str, seed: str, context: str,
                    settings: Settings, deep: bool = False, parent_run_id: Optional[str] = None,
                    governance: Optional[dict] = None, actor: str = "ceo") -> str:
    """Create the durable run row and start the pipeline.

    deep=True runs a multi-source parallel scout sweep (competitor/pain/demand/timing) for a richer,
    more cited dossier — the deeper second pass for a promising idea.

    In Community the run is a durable `discovery.run` job: a worker leases it and every
    research stage spends only through its own reservation, lease and receipt. Nothing runs
    on a thread in this process, so a restart cannot orphan it or replay a paid call.
    """
    run_id = make_id("drun")
    engine = get_engine()
    as_job = _research_runs_as_a_job(settings)
    init_state = {"deep": deep}
    if governance:
        init_state["governance"] = governance
    if parent_run_id:
        init_state["parent_run_id"] = parent_run_id
    with begin(engine) as c:
        c.execute(
            text("""
                INSERT INTO discovery_runs (id, project_id, status, phase, title, seed, context, state)
                VALUES (:id, :p, 'running', 'queued', :t, :s, :ctx, CAST(:st AS JSONB))
            """),
            {"id": run_id, "p": project_id, "t": title[:300], "s": seed, "ctx": context or "",
             "st": json.dumps(init_state)},
        )
        if as_job:
            from .discovery_spend import AGENT_ID, JOB_KIND
            from .jobs import enqueue_job

            job = enqueue_job(
                c, project_id, JOB_KIND,
                {"discovery_run_id": run_id, "agent_id": AGENT_ID, "actor": actor},
                priority=40, max_attempts=3, dedupe_key=f"discovery:{run_id}",
                workload_class="owner_sandbox", data_boundary="internal",
            )
            c.execute(
                text("UPDATE discovery_runs SET state = state || CAST(:j AS JSONB) "
                     "WHERE id = :id"),
                {"id": run_id, "j": json.dumps({"job_id": job["id"]})},
            )
        emit_event(c, project_id, "discovery.started", "boss", "discovery_run", run_id,
                   {"title": title[:300], "deep": deep, "governed_job": as_job})
    if not as_job:
        threading.Thread(
            target=_run_discovery,
            args=(run_id, project_id, seed, context, settings, deep, governance),
            daemon=True,
        ).start()
    return run_id


def execute_discovery_job(settings: Settings, *, run_id: str, job_id: str, worker_id: str,
                          lease_token: str) -> dict:
    """Run (or resume) one discovery under the worker's lease of its `discovery.run` job.

    Stages already paid for replay from their receipts. Returns {"status", "retry"}: a stage
    that failed outright is retryable on the job's next attempt; a refused budget, a research
    reply with no evidence, or a call that needs reconciliation is not — retrying would either
    spend again for the same result or risk paying twice.
    """
    from . import discovery_spend
    from .discovery_spend import (
        DiscoveryReconciliationRequired,
        DiscoverySpendRefused,
        DiscoveryStageSpend,
    )

    engine = get_engine()
    with begin(engine) as c:
        row = c.execute(
            text("SELECT d.project_id, d.seed, d.context, d.state, d.status, "
                 "j.attempt, j.max_attempts, j.payload FROM discovery_runs d "
                 "JOIN jobs j ON j.id = :job WHERE d.id = :id"),
            {"id": run_id, "job": job_id},
        ).mappings().first()
    if row is None:
        return {"status": "failed", "retry": False, "error": f"no discovery run {run_id}"}
    state = row["state"] if isinstance(row["state"], dict) else {}
    if row["status"] == "failed" and state.get("failed_by_job"):
        # A REQUEUED DEADLETTER resumes the run it failed: the job, not the research, stopped
        # it, and the stages it already paid for replay from their receipts.
        with begin(engine) as c:
            c.execute(text(
                "UPDATE discovery_runs SET status = 'running', error = NULL, "
                "state = (state - 'failed_by_job' - 'job_error') || jsonb_build_object('job_id', CAST(:j AS text)), "
                "updated_at = now() WHERE id = :id AND status = 'failed'"),
                {"id": run_id, "j": job_id})
            emit_event(c, row["project_id"], "discovery.resumed", worker_id, "discovery_run",
                       run_id, {"reason": "its deadlettered job was requeued", "job_id": job_id})
        state = {k: v for k, v in state.items() if k not in ("failed_by_job", "job_error")}
        state["job_id"] = job_id
    elif row["status"] != "running":
        return {"status": row["status"], "retry": False}
    payload = row["payload"] if isinstance(row["payload"], dict) else {}
    spend = DiscoveryStageSpend.for_engine(
        settings, engine, project_id=row["project_id"], run_id=run_id, job_id=job_id,
        worker_id=worker_id, job_lease_token=lease_token,
        actor=str(payload.get("actor") or "ceo"),
    )
    agent = make_code_agent(settings, engine="claude_code")
    if not hasattr(agent, "run_research"):
        raise RuntimeError("Discovery requires the claude_code engine (web research).")

    def research(stage: str, prompt: str) -> dict:
        return spend.run(stage, lambda: agent.run_research(prompt),
                         source_id=discovery_spend.RESEARCH_SOURCE_ID)

    last_attempt = int(row["attempt"] or 1) >= int(row["max_attempts"] or 1)
    try:
        _run_discovery(run_id, row["project_id"], row["seed"] or "", row["context"] or "",
                       settings, bool(state.get("deep")), None, research=research,
                       sequential=True, raise_errors=True)
    except DiscoverySpendRefused as refused:
        waits = int(state.get("budget_waits") or 0)
        if refused.transient and waits < MAX_BUDGET_WAITS:
            # THE PER-MINUTE CAP CLEARS BY ITSELF, and waiting for it is not a failed attempt —
            # retrying the job spent one of its three attempts per wait, so a run needing more
            # than three waits failed. Queue a fresh job a minute out instead; it replays every
            # stage already paid for and carries on.
            _defer_run(engine, row["project_id"], run_id, payload, waits + 1, str(refused))
            return {"status": "deferred", "retry": False, "error": str(refused)[:1000]}
        _fail_run(engine, row["project_id"], run_id, refused)
        return {"status": "failed", "retry": False, "error": str(refused)[:1000]}
    except (DiscoveryReconciliationRequired, DiscoveryResearchUnavailable) as settled:
        _fail_run(engine, row["project_id"], run_id, settled)
        return {"status": "failed", "retry": False, "error": str(settled)[:1000]}
    except Exception as error:  # noqa: BLE001 — a stage failed outright; retry if attempts remain
        from .model_failures import explain, is_login_rejected

        if is_login_rejected(error):
            # A REJECTED LOGIN DOES NOT FIX ITSELF. Retrying spent three attempts saying the
            # same 401 while the run showed "queued"; stop now and say what to do.
            said = explain(error, source_id=discovery_spend.RESEARCH_SOURCE_ID)
            _fail_run(engine, row["project_id"], run_id,
                      RuntimeError(f"{said['headline']} {said['action']}"))
            return {"status": "failed", "retry": False, "error": said["headline"]}
        if last_attempt:
            _fail_run(engine, row["project_id"], run_id, error)
            return {"status": "failed", "retry": False, "error": str(error)[:1000]}
        with begin(engine) as c:
            c.execute(text("UPDATE discovery_runs SET error = :e, updated_at = now() "
                           "WHERE id = :id"), {"e": f"retrying: {error}"[:1000], "id": run_id})
        return {"status": "failed", "retry": True, "error": str(error)[:1000]}
    return {"status": "completed", "retry": False}


#: How many times one run may wait out the per-minute budget cap before it is failed.
MAX_BUDGET_WAITS = 30
BUDGET_WAIT_SECONDS = 65


def _defer_run(engine, project_id: str, run_id: str, payload: dict, waits: int,
               reason: str) -> None:
    from .discovery_spend import JOB_KIND
    from .jobs import enqueue_job

    with begin(engine) as c:
        job = enqueue_job(
            c, project_id, JOB_KIND, dict(payload), priority=40, max_attempts=3,
            run_after_seconds=BUDGET_WAIT_SECONDS, dedupe_key=f"discovery:{run_id}:wait:{waits}",
            workload_class="owner_sandbox", data_boundary="internal",
        )
        c.execute(
            text("UPDATE discovery_runs SET state = state || CAST(:s AS JSONB), error = :e, "
                 "updated_at = now() WHERE id = :id"),
            {"id": run_id, "e": f"waiting for the per-minute budget: {reason}"[:1000],
             "s": json.dumps({"job_id": job["id"], "budget_waits": waits})},
        )
        emit_event(c, project_id, "discovery.waiting_for_budget", "boss", "discovery_run",
                   run_id, {"job_id": job["id"], "waits": waits,
                            "retry_in_seconds": BUDGET_WAIT_SECONDS})


def _fail_run(engine, project_id: str, run_id: str, error: BaseException) -> None:
    with begin(engine) as c:
        c.execute(text("UPDATE discovery_runs SET status = 'failed', error = :e, "
                       "updated_at = now() WHERE id = :id AND status = 'running'"),
                  {"e": f"{error}"[:1000], "id": run_id})
        emit_event(c, project_id, "discovery.failed", "boss", "discovery_run", run_id,
                   {"error": f"{error}"[:500], "kind": type(error).__name__})


def resume_interrupted(settings: Settings) -> dict:
    """Reconcile Community research interrupted by process exit.

    `start_discovery` drives the pipeline on a DAEMON THREAD, so a discovery lives inside
    whichever process started it and dies when that process does. Every API restart — a deploy,
    a crash, an OOM — silently orphaned every discovery in flight: the row stayed `running`,
    nothing advanced it, and the Task Shepherd called it abandoned. From the operator's side
    that is research failing at random.

    Called at startup beside the orphaned-deployment sweep. Community marks
    interrupted runs failed and creates a spend-reconciliation decision without
    replaying model calls. Other editions retain the earlier restart behavior.
    """
    engine = get_engine()
    if _research_runs_as_a_job(settings):
        # A job-owned run is the job system's to resume: the worker's lease is reclaimed and
        # the run replays the stages it already paid for. Only a run left over from the old
        # in-process thread is failed here for reconciliation.
        blocked: list[str] = []
        with begin(engine) as c:
            rows = c.execute(
                text("SELECT id, project_id, state, usage FROM discovery_runs "
                     "WHERE status = 'running' ORDER BY created_at FOR UPDATE SKIP LOCKED")
            ).mappings().all()
            for row in rows:
                state = row["state"] if isinstance(row["state"], dict) else {}
                if state.get("job_id"):
                    continue
                changed = c.execute(
                    text("UPDATE discovery_runs SET status = 'failed', error = :reason, "
                         "updated_at = now() WHERE id = :id AND status = 'running'"),
                    {"id": row["id"], "reason": COMMUNITY_DISCOVERY_BLOCK_REASON},
                ).rowcount
                if not changed:
                    continue
                state = row["state"] if isinstance(row["state"], dict) else {}
                governance = state.get("governance") or {}
                if not isinstance(governance, dict):
                    governance = {}
                create_blocking_decision(
                    c, row["project_id"], None,
                    f"Reconcile interrupted Discovery research {row['id']}",
                    {"action": "review_model_spend_and_receipts_before_retry"},
                    {"run_id": row["id"], "recorded_usage": row["usage"],
                     "reservation_id": governance.get("reservation_id"),
                     "receipt_id": governance.get("receipt_id")},
                    "discovery_unverified_spend", default_action="hold",
                )
                emit_event(
                    c, row["project_id"], "discovery.blocked", "boss",
                    "discovery_run", row["id"],
                    {"reason": "community_discovery_governance_gate",
                     "spend_reconciliation_required": True},
                )
                blocked.append(row["id"])
        return {"resumed": 0, "run_ids": [], "blocked": len(blocked),
                "blocked_run_ids": blocked[:20]}
    resumed: list[str] = []
    with begin(engine) as c:
        rows = c.execute(
            text("SELECT id, project_id, seed, context, state FROM discovery_runs "
                 "WHERE status = 'running' ORDER BY created_at")
        ).mappings().all()
    for row in rows:
        state = row["state"] if isinstance(row["state"], dict) else {}
        try:
            with begin(get_engine()) as conn:
                emit_event(conn, row["project_id"], "discovery.resumed", "boss",
                           "discovery_run", row["id"],
                           {"reason": "the process running it restarted"})
            threading.Thread(
                target=_run_discovery,
                args=(row["id"], row["project_id"], row["seed"] or "", row["context"] or "",
                      settings, bool(state.get("deep")), state.get("governance")),
                daemon=True,
            ).start()
            resumed.append(row["id"])
        except Exception:  # noqa: BLE001 — one unresumable run must not stop the rest
            continue
    return {"resumed": len(resumed), "run_ids": resumed[:20]}


def list_discovery_runs(connection, project_id: str, limit: int = 50) -> list[dict]:
    rows = connection.execute(
        text("""SELECT d.id, d.project_id, d.status, d.phase, d.title, d.seed, d.context, d.state,
                       d.usage, d.error, d.created_at, d.updated_at, j.status AS job_status,
                       j.last_error AS job_error, j.lease_owner AS job_lease_owner,
                       j.run_after AS job_run_after, (now() - j.created_at) AS job_age
                FROM discovery_runs d LEFT JOIN jobs j ON j.id = d.state->>'job_id'
                WHERE d.project_id=:p ORDER BY d.created_at DESC LIMIT :n"""),
        {"p": project_id, "n": limit},
    ).mappings().all()
    return [_row_to_dict(r) for r in rows]


def load_discovery_run(connection, run_id: str) -> Optional[dict]:
    r = connection.execute(
        text("""SELECT d.id, d.project_id, d.status, d.phase, d.title, d.seed, d.context, d.state,
                       d.usage, d.error, d.created_at, d.updated_at, j.status AS job_status,
                       j.last_error AS job_error, j.lease_owner AS job_lease_owner,
                       j.run_after AS job_run_after, (now() - j.created_at) AS job_age
                FROM discovery_runs d LEFT JOIN jobs j ON j.id = d.state->>'job_id'
                WHERE d.id=:id"""),
        {"id": run_id},
    ).mappings().first()
    return _row_to_dict(r) if r else None


def build_promote_directive(run: dict, idx: int) -> tuple[str, str]:
    """Turn a chosen candidate outcome + its dossier evidence into a PRD-meeting directive."""
    state = run.get("state") or {}
    o = (state.get("candidate_outcomes") or [])[idx]
    crit = state.get("critique") or {}
    findings = [f for f in (state.get("signal_findings") or []) if (f or {}).get("finding")][:4]
    outcome = (o.get("outcome") or "").strip()
    title = f"Build: {outcome[:80]}" if outcome else f"Build outcome from {run['id']}"
    sig = "\n".join(f"- {f.get('finding')} [{f.get('citation_url', '')}]" for f in findings)
    directive = (
        "Produce a PRD for this evidence-backed opportunity, surfaced by BOSS Discovery research "
        f"on the seed: \"{(run.get('seed') or '')[:200]}\".\n\n"
        f"OUTCOME TO BUILD: {outcome}\n"
        f"WEDGE: {o.get('wedge', '(n/a)')}\n"
        f"TARGET PILOT: {o.get('target_pilot', '(n/a)')}\n"
        f"FIRST PROTOTYPE SLICE: {o.get('first_prototype_slice', '(n/a)')}\n"
        f"VALIDATION PLAN: {o.get('validation_plan', '(n/a)')}\n\n"
        f"BIGGEST RISK (from adversarial vetting): {crit.get('biggest_risk', '(n/a)')}\n"
        f"KEY MARKET SIGNALS:\n{sig or '- (none)'}\n\n"
        "Scope the PRD to the FIRST PROTOTYPE SLICE only — the smallest thing we can pilot with the "
        "target segment to validate real usage before investing further. Honor the validation plan "
        "(pilot -> measure usage -> then monetize)."
    )
    return directive, title


def _row_to_dict(r) -> dict:
    state = r["state"]
    state = json.loads(state) if isinstance(state, str) else (state or {})
    usage = r["usage"]
    usage = json.loads(usage) if isinstance(usage, str) else usage
    return {
        "id": r["id"], "project_id": r["project_id"], "status": r["status"], "phase": r["phase"],
        "title": r["title"], "seed": r["seed"], "context": r["context"],
        "state": state, "usage": usage, "error": r["error"],
        "created_at": r["created_at"].isoformat() if r["created_at"] else None,
        "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
        "attention": _attention(r, state),
    }


#: A queued job nobody has picked up for this long means no worker is taking work.
_UNCLAIMED_AFTER_SECONDS = 180


def _attention(r, state: dict) -> Optional[dict]:
    """Why this run is not moving, in words an operator can act on — or None if it is fine.

    Discovery used to say "queued" through a refused worker (409), a budget cap and a rejected
    Claude login: each reason was in a job's last_error, an event or this row's error, and none
    was where the operator looked. This reads the run AND its job, because some failures (the
    worker refused, the job deadlettered) never reach this row at all.
    """
    from .discovery_spend import RESEARCH_SOURCE_ID
    from .model_failures import explain

    keys = r.keys() if hasattr(r, "keys") else ()
    status, error = r["status"], r["error"] or ""
    job_status = r["job_status"] if "job_status" in keys else None
    job_error = r["job_error"] if "job_error" in keys else None
    if isinstance(job_error, str):
        try:
            job_error = json.loads(job_error)
        except ValueError:
            pass
    job_text = (job_error.get("message") if isinstance(job_error, dict) else job_error) or ""
    if status == "completed":
        return None
    if status == "cancelled":
        # The task shepherd closes a run that no job can advance, keeping its last error: the
        # reason it stopped is that error, not the cancellation.
        said = explain(error, source_id=RESEARCH_SOURCE_ID)
        return {**said, "state": "stopped",
                "headline": "Stopped — nothing could advance it. " + said["headline"]}
    if status == "failed":
        if state.get("failed_by_job"):
            # The job, not the research, stopped it: explain the job's own error.
            said = explain(state.get("job_error") or error, source_id=RESEARCH_SOURCE_ID)
            return {**said, "state": "stopped",
                    "headline": "Its worker job stopped. " + said["headline"]}
        return {**explain(error, source_id=RESEARCH_SOURCE_ID), "state": "stopped"}
    if status != "running":
        return None
    if state.get("job_id") and job_status in ("deadlettered", "failed", "cancelled"):
        said = explain(job_text or f"the job is {job_status}", source_id=RESEARCH_SOURCE_ID)
        return {**said, "state": "stopped",
                "headline": f"Not running — its worker job stopped ({job_status}). "
                            + said["headline"]}
    if error.startswith("waiting for the per-minute budget"):
        return {**explain("burst cap"), "state": "waiting"}
    if error.startswith("retrying:"):
        said = explain(error, source_id=RESEARCH_SOURCE_ID)
        return {**said, "state": "retrying",
                "headline": "Retrying after a failed attempt. " + said["headline"]}
    age = r["job_age"] if "job_age" in keys else None
    if (job_status == "queued" and not (r["job_lease_owner"] if "job_lease_owner" in keys else None)
            and age is not None and age.total_seconds() > _UNCLAIMED_AFTER_SECONDS):
        return {"kind": "no_worker", "state": "waiting",
                "headline": "Queued, but no worker has picked it up for a few minutes.",
                "action": "Check that the worker service is running (boss doctor, or "
                          "`docker compose ps` on the server).",
                "href": None, "retry": True}
    return None


# --------------------------------------------------------------------------- #
#  Pipeline                                                                    #
# --------------------------------------------------------------------------- #
def _still_working(run_id: str, engine, stop: threading.Event) -> None:
    """Touch the row once a minute while the pipeline is alive.

    A discovery has no JOB — it is a thread — so the Task Shepherd cannot ask "is a worker
    holding this?" the way it can for a code task. All it has is `updated_at`, and its rule is
    that a run untouched for twenty minutes has been abandoned. The rule is right; the problem
    is that one research phase can legitimately run longer than that without reaching the next
    `_phase` call, so the sweep cancelled work that was going perfectly well. That is what
    "research fails at random" was.

    Only while it is still running: moving a finished run's timestamp makes "when did this end"
    unanswerable.
    """
    while not stop.wait(60):
        try:
            with begin(engine) as c:
                c.execute(
                    text("UPDATE discovery_runs SET updated_at = now() "
                         "WHERE id = :id AND status = 'running'"),
                    {"id": run_id},
                )
        except Exception:  # noqa: BLE001 — a missed beat must not kill the research
            continue


def _run_discovery(run_id: str, project_id: str, seed: str, context: str,
                   settings: Settings, deep: bool = False,
                   governance: Optional[dict] = None, *,
                   research: Optional[Callable[[str, str], dict]] = None,
                   sequential: bool = False, raise_errors: bool = False) -> None:
    """The pipeline. `research(stage, prompt)` makes each model call: the governed job passes
    one that reserves, leases and receipts each stage (and replays a stage already paid for);
    the legacy path calls the agent directly. `raise_errors` hands a failure to the job, which
    decides between a retry and a failed run, instead of recording it here."""
    engine = get_engine()
    _hb_stop = threading.Event()
    threading.Thread(target=_still_working, args=(run_id, engine, _hb_stop),
                     name=f"drun-hb-{run_id}", daemon=True).start()
    try:
        if research is None:
            agent = make_code_agent(settings, engine="claude_code")
            if not hasattr(agent, "run_research"):
                raise RuntimeError("Discovery requires the claude_code engine (web research).")

            def research(stage: str, prompt: str) -> dict:  # noqa: F811 — the legacy path
                return agent.run_research(prompt)
        dossier: dict = {"deep": deep}
        if governance:
            dossier["governance"] = governance
        cost = 0
        model = None

        # P1 — Signal sweep (web research). Deep mode fans out to parallel focused scouts.
        _phase(engine, run_id, "signal_sweep")
        if deep:
            brief, findings, sweep_cost, model = _deep_sweep(research, seed, context,
                                                             sequential=sequential)
            dossier["research_brief"] = brief
            dossier["signal_findings"] = findings
            cost += sweep_cost
        else:
            r = research("signal_sweep", _sweep_prompt(seed, context))
            cost += r["cost_usd_micros"]
            model = r.get("model")
            sweep = _extract_json(r["summary"])
            dossier["research_brief"] = sweep.get("brief", {})
            dossier["signal_findings"] = sweep.get("findings", [])
            _record_stage(dossier, "signal_sweep", r, parsed=sweep)
        _save(engine, run_id, dossier, cost, model)

        # A SWEEP THAT FOUND NOTHING IS A FAILED RUN, NOT A CAUTIOUS ONE.
        #
        # Discovery used to carry an empty sweep through synthesize -> vet -> shape and finish
        # `completed`, having written a market analysis on top of zero evidence. The gate then
        # said "needs_more_research", which reads as "the idea is unproven" when what actually
        # happened is that research never ran. The operator is left to infer a broken tool from
        # a verdict about their idea. Stop here and say which it is.
        if not dossier.get("signal_findings"):
            # DISTINGUISH "RESEARCH DID NOT RUN" FROM "THE REPLY WAS CUT OFF".
            #
            # This said web research returned nothing and pointed at WebSearch/WebFetch. In
            # the three failures that prompted the fix, research had run fine — the brief
            # parsed, with real sources_consulted — and the reply was simply truncated before
            # the findings array. Sending the operator to debug a working search tool is worse
            # than saying nothing, so name the two cases apart using what actually came back.
            brief = dossier.get("research_brief") or {}
            consulted = brief.get("sources_consulted") or []
            if consulted or brief:
                raise DiscoveryResearchUnavailable(
                    f"The research reply was cut off before it produced any findings, after "
                    f"consulting {len(consulted)} source(s). Web search itself WORKED — this is "
                    "a truncated response, not a missing tool, so retrying is reasonable. "
                    "See state._diagnostics.signal_sweep.reply_head for where it stopped.")
            raise DiscoveryResearchUnavailable(
                "Web research returned no findings, so there is nothing to analyse. This is a "
                "TOOLING failure, not a verdict on the idea: the research agent completed "
                "without gathering any cited source. Check that WebSearch/WebFetch work for "
                "the agent in this environment. See state._diagnostics for what it returned.")

        # P2 — Synthesize
        _phase(engine, run_id, "synthesize")
        r = research("synthesize", _synth_prompt(seed, context, dossier["signal_findings"]))
        cost += r["cost_usd_micros"]
        parsed = _extract_json(r["summary"])
        dossier["market_analysis"] = parsed.get("analysis", {})
        _record_stage(dossier, "synthesize", r, parsed=parsed)
        _save(engine, run_id, dossier, cost, model)

        # P3 — Adversarial vetting (web research for disconfirming evidence)
        _phase(engine, run_id, "vet")
        r = research("vet", _vet_prompt(seed, dossier))
        cost += r["cost_usd_micros"]
        parsed = _extract_json(r["summary"])
        dossier["critique"] = parsed.get("critique", {})
        _record_stage(dossier, "vet", r, parsed=parsed)
        _save(engine, run_id, dossier, cost, model)

        # P4 — Shape outcomes
        _phase(engine, run_id, "shape")
        r = research("shape", _shape_prompt(seed, context, dossier))
        cost += r["cost_usd_micros"]
        parsed = _extract_json(r["summary"])
        dossier["candidate_outcomes"] = parsed.get("outcomes", [])
        _record_stage(dossier, "shape", r, parsed=parsed)
        _save(engine, run_id, dossier, cost, model)

        # P5 — Evidence gate (deterministic, fail-closed)
        _phase(engine, run_id, "gate")
        dossier["gate"] = _evaluate_gate(dossier)

        with begin(engine) as c:
            c.execute(
                text("""UPDATE discovery_runs SET status='completed', phase='done',
                        state=CAST(:s AS JSONB) || jsonb_strip_nulls(jsonb_build_object('job_id', state->'job_id')),
                        usage=jsonb_build_object('cost_usd_micros', CAST(:cost AS bigint),
                                                 'model', CAST(:m AS text)),
                        updated_at=now() WHERE id=:id"""),
                {"s": json.dumps(dossier), "cost": cost, "m": model, "id": run_id},
            )
            emit_event(c, project_id, "discovery.completed", "boss", "discovery_run", run_id,
                       {"verdict": dossier["gate"]["verdict"],
                        "outcomes": len(dossier.get("candidate_outcomes", []))})
            if governance:
                from .budget import BudgetGovernor
                from .receipts import ReceiptService

                ReceiptService().mark_succeeded(
                    c,
                    governance["receipt_id"],
                    model,
                    {"run_id": run_id, "verdict": dossier["gate"]["verdict"]},
                )
                BudgetGovernor(settings).settle(
                    c,
                    project_id,
                    governance["reservation_id"],
                    governance["receipt_id"],
                    "model",
                    model or "discovery",
                    int(governance["estimated_usd_micros"]),
                    int(cost),
                )
    except Exception as exc:  # noqa: BLE001 — record the failure durably, never crash the thread
        if raise_errors:
            raise
        detail = f"{exc}"[:1000]
        try:
            with begin(engine) as c:
                c.execute(text("UPDATE discovery_runs SET status='failed', error=:e, updated_at=now() WHERE id=:id"),
                          {"e": detail, "id": run_id})
                if governance:
                    from .budget import BudgetGovernor
                    from .receipts import ReceiptService

                    ReceiptService().mark_failed(
                        c, governance["receipt_id"], {"message": detail}
                    )
                    BudgetGovernor(settings).release(
                        c,
                        project_id,
                        governance["reservation_id"],
                        "Discovery failed before a verified outcome.",
                    )
        except Exception:
            traceback.print_exc()


# --------------------------------------------------------------------------- #
#  Persistence helpers                                                         #
# --------------------------------------------------------------------------- #
    finally:
        # Stop the beat whatever happened. A heartbeat outliving its pipeline would keep a dead
        # run looking alive, which is precisely the state the shepherd exists to catch.
        _hb_stop.set()

def _phase(engine, run_id: str, phase: str) -> None:
    with begin(engine) as c:
        c.execute(text("UPDATE discovery_runs SET phase=:ph, updated_at=now() WHERE id=:id"),
                  {"ph": phase, "id": run_id})


def _save(engine, run_id: str, dossier: dict, cost: int, model: Optional[str]) -> None:
    # The dossier REPLACES the state but the job that owns the run is kept: dropping it made a
    # governed run look like an orphan of the old in-process path to startup recovery.
    with begin(engine) as c:
        c.execute(
            text("""UPDATE discovery_runs SET state=CAST(:s AS JSONB) || jsonb_strip_nulls(jsonb_build_object('job_id', state->'job_id')),
                    usage=jsonb_build_object('cost_usd_micros', CAST(:cost AS bigint),
                                             'model', CAST(:m AS text)),
                    updated_at=now() WHERE id=:id"""),
            {"s": json.dumps(dossier), "cost": cost, "m": model, "id": run_id},
        )


# --------------------------------------------------------------------------- #
#  Evidence gate                                                              #
# --------------------------------------------------------------------------- #
def _f(v) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


# Evidence-gate thresholds (tunable).
PURSUE_CONFIDENCE = 0.60      # strong, build-ready
PROMISING_CONFIDENCE = 0.35   # weak literal idea but a viable pivot worth a pilot
MIN_CITED_SOURCES = 4


def _evaluate_gate(dossier: dict) -> dict:
    """Three-tier, fail-closed evidence gate.

    Confidence blends the skeptic's verdict on the *original thesis* with the *best shaped
    outcome's* confidence — so a weak basic idea that research pivots into a strong wedge can
    still rank as 'promising' rather than being binary-rejected.
      pursue              — build-ready: strong confidence + full structural evidence
      promising           — a viable, cited outcome worth a pilot, but not yet a slam dunk
      needs_more_research — thin evidence, or no viable outcome
    """
    findings = dossier.get("signal_findings") or []
    cited = [f for f in findings if (f or {}).get("citation_url")]
    crit = dossier.get("critique") or {}
    outcomes = dossier.get("candidate_outcomes") or []

    thesis_conf = _f(crit.get("overall_confidence_0_1"))
    outcome_confs = [_f(o.get("confidence_0_1")) for o in outcomes if (o or {}).get("confidence_0_1") is not None]
    best_outcome_conf = max(outcome_confs) if outcome_confs else 0.0
    confidence = max(thesis_conf, best_outcome_conf)

    checks = {
        "enough_cited_sources": len(cited) >= MIN_CITED_SOURCES,
        "has_outcome": len(outcomes) >= 1,
        "outcome_has_pilot": any((o or {}).get("target_pilot") for o in outcomes),
        "disconfirming_considered": len(crit.get("disconfirming_evidence") or []) >= 1,
        "confidence_strong": confidence >= PURSUE_CONFIDENCE,
    }
    structural_ok = checks["enough_cited_sources"] and checks["has_outcome"] and checks["disconfirming_considered"]

    if structural_ok and checks["outcome_has_pilot"] and confidence >= PURSUE_CONFIDENCE:
        verdict = "pursue"
    elif structural_ok and confidence >= PROMISING_CONFIDENCE:
        verdict = "promising"
    else:
        verdict = "needs_more_research"

    return {
        "verdict": verdict,
        "checks": checks,
        "confidence": round(confidence, 2),
        "thesis_confidence": round(thesis_conf, 2),
        "best_outcome_confidence": round(best_outcome_conf, 2),
        "cited_sources": len(cited),
    }


# --------------------------------------------------------------------------- #
#  Prompts                                                                     #
# --------------------------------------------------------------------------- #
_JSON_ONLY = "Return ONLY a single JSON object — no markdown fences, no prose before or after."


def _sweep_prompt(seed: str, context: str) -> str:
    return (
        "You are a rigorous market research analyst doing PRIMARY research with web search.\n"
        f"IDEA / SEED:\n{seed}\n\n"
        f"OPERATOR CONTEXT: {context or '(none provided)'}\n\n"
        "Use WebSearch and WebFetch to gather REAL, CITED market signals. Investigate:\n"
        "1. Competitors / existing alternatives — who, positioning, pricing.\n"
        "2. Evidence of real demand — communities, forums, complaints, search interest; who has "
        "this pain and how often.\n"
        "3. Market size & trends.\n"
        "4. Timing — what changed that makes now relevant.\n"
        "Be skeptical and evidence-driven; prefer primary sources you actually open.\n\n"
        f"{_JSON_ONLY}\n"
        # FINDINGS FIRST, DELIBERATELY.
        #
        # This asked for `brief` first and `findings` last. Three runs in a row failed with
        # "web research returned no findings" while having actually done the research: the
        # reply was cut off partway through, after the brief and its sources_consulted and
        # BEFORE the findings array existed. The evidence — the only part the next phase needs
        # — was the part always lost, and each failure still cost about $0.50.
        #
        # A truncated reply now loses the summary rather than the substance.
        '{"findings": [{"angle": "competitor|demand|market|timing", "finding": "concise fact", '
        '"evidence": "what the source shows", "citation_url": "https://..."}], '
        '"brief": {"hypothesis": "...", "questions": ["..."], "sources_consulted": ["url"]}}\n'
        "Emit `findings` FIRST and completely; 6-12 of them, each with a real citation_url you "
        "visited. Keep every field short — one or two sentences. If you are running long, cut "
        "the brief, never the findings."
    )


_SCOUTS = {
    "competitor": "competitors and existing alternatives — who they are, positioning, pricing, and where the gaps/unmet needs are",
    "pain": "real user pain — communities, forums, reviews, and complaints; how FREQUENT and how SEVERE the pain is, with quotes",
    "demand": "demand and interest signals — search trends, keyword interest, job posts, willingness-to-pay indicators; is anyone actively looking",
    "timing": "why now — recent news, regulation, technology, funding, or behavior shifts that make this timely",
}


def _scout_prompt(angle: str, focus: str, seed: str, context: str) -> str:
    return (
        f"You are a market research scout focused ONLY on: {focus}.\n"
        f"IDEA / SEED:\n{seed}\n\nOPERATOR CONTEXT: {context or '(none)'}\n\n"
        "Use WebSearch and WebFetch to gather REAL, CITED evidence on your focus area only. Open "
        "primary sources.\n\n"
        f"{_JSON_ONLY}\n"
        '{"findings": [{"angle": "' + angle + '", "finding": "concise fact", "evidence": '
        '"what the source shows", "citation_url": "https://..."}]}\n'
        "Return 3-6 findings, each with a real citation_url you visited."
    )


def _deep_sweep(research, seed: str, context: str, *, sequential: bool = False):
    """Run the focused scouts and merge their cited findings — the deeper pass.

    In parallel on the legacy path. One at a time when governed: each scout is its own leased
    stage, and a subscription source holds one model lease at a time.
    """
    from concurrent.futures import ThreadPoolExecutor

    def run_scout(item):
        angle, focus = item
        try:
            r = research(f"scout_{angle}", _scout_prompt(angle, focus, seed, context))
            return r, _extract_json(r["summary"]).get("findings", [])
        except Exception:  # noqa: BLE001 — a dead scout shouldn't sink the sweep
            if sequential:
                raise   # governed: the job decides retry vs reconcile, never a silent zero
            return {"cost_usd_micros": 0, "model": None}, []

    findings: list = []
    cost = 0
    model = None
    with ThreadPoolExecutor(max_workers=1 if sequential else len(_SCOUTS)) as pool:
        for r, fs in pool.map(run_scout, _SCOUTS.items()):
            cost += r.get("cost_usd_micros", 0)
            model = model or r.get("model")
            findings.extend(fs)
    brief = {"hypothesis": seed[:300],
             "questions": ["Is the pain real, frequent, and severe?", "Who owns the market today?",
                           "Is there demonstrated demand / willingness to pay?", "Why now?"],
             "sources_consulted": [f.get("citation_url") for f in findings if f.get("citation_url")][:12]}
    return brief, findings, cost, model


def _synth_prompt(seed: str, context: str, findings: list) -> str:
    return (
        "You are a market analyst. Using ONLY the researched findings below, synthesize a "
        "structured market analysis. Every claim must rest on the findings.\n"
        f"IDEA: {seed}\nCONTEXT: {context or '(none)'}\n"
        f"FINDINGS (JSON): {json.dumps(findings)[:6000]}\n\n"
        f"{_JSON_ONLY}\n"
        '{"analysis": {"problem": {"statement": "...", "is_real": true, "frequency": '
        '"rare|occasional|frequent", "severity_0_5": 3}, "target_segment": "...", '
        '"demand_evidence": "...", "competition": {"landscape": "...", "our_wedge": "..."}, '
        '"market_size": "...", "timing": "...", "risks": ["..."]}}'
    )


def _vet_prompt(seed: str, dossier: dict) -> str:
    return (
        "You are a sharp, skeptical venture investor. Challenge this analysis HARD. Use WebSearch "
        "to find DISCONFIRMING evidence. For each key claim decide if it is evidence-backed or "
        "wishful thinking. Default to skepticism.\n"
        f"IDEA: {seed}\n"
        f"ANALYSIS (JSON): {json.dumps(dossier.get('market_analysis', {}))[:5000]}\n"
        f"FINDINGS (JSON): {json.dumps(dossier.get('signal_findings', []))[:4000]}\n\n"
        f"{_JSON_ONLY}\n"
        '{"critique": {"claims": [{"claim": "...", "verdict": "supported|weak|unsupported", '
        '"reason": "..."}], "disconfirming_evidence": ["... (include a url)"], '
        '"overall_confidence_0_1": 0.5, "biggest_risk": "..."}}'
    )


def _shape_prompt(seed: str, context: str, dossier: dict) -> str:
    return (
        "You are a product strategist. Turn the VETTED research into concrete candidate outcomes "
        "to build — but ONLY those the evidence supports. Validation model: prototype -> pilot "
        "with customers who genuinely need it -> measure real usage -> THEN monetize (never "
        "pay-upfront-on-an-idea).\n"
        f"IDEA: {seed}\nCONTEXT: {context or '(none)'}\n"
        f"ANALYSIS: {json.dumps(dossier.get('market_analysis', {}))[:4000]}\n"
        f"CRITIQUE: {json.dumps(dossier.get('critique', {}))[:3000]}\n\n"
        f"{_JSON_ONLY}\n"
        '{"outcomes": [{"outcome": "what to build, scoped", "wedge": "...", "target_pilot": '
        '"who to pilot with", "first_prototype_slice": "smallest prototype to pilot", '
        '"validation_plan": "pilot -> measure usage -> monetize", "rationale": "...", '
        '"confidence_0_1": 0.6, "supporting_signals": ["finding/citation refs"]}]}\n'
        "Give 1-3 outcomes, best first. If the evidence does NOT support building, "
        'return {"outcomes": []}.'
    )


# --------------------------------------------------------------------------- #
#  JSON extraction                                                            #
# --------------------------------------------------------------------------- #
def _extract_json(text_out: str) -> dict:
    """Best-effort parse of a JSON object from an agent's text output.

    Tolerant of markdown fences and — importantly — of a response truncated mid-object (the
    web-research sweep often returns a long JSON whose final closing brace is cut off). When a
    direct parse fails we balance any unclosed strings/brackets and retry."""
    s = (text_out or "").strip()
    if not s:
        return {}
    if s.startswith("```"):
        inner = s.split("```")
        if len(inner) >= 2:
            s = inner[1]
            if s.lstrip().lower().startswith("json"):
                s = s.lstrip()[4:]
            s = s.strip()
    i = s.find("{")
    if i < 0:
        return {}
    s = s[i:]
    try:
        return json.loads(s)
    except Exception:
        pass
    # Repair: walk the text tracking string/escape state + bracket depth, then close whatever
    # is still open (handles a response cut off before its final braces/brackets).
    instr = esc = False
    stack: list[str] = []
    for ch in s:
        if instr:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                instr = False
            continue
        if ch == '"':
            instr = True
        elif ch in "{[":
            stack.append(ch)
        elif ch in "}]" and stack:
            stack.pop()
    repaired = s + ('"' if instr else "")
    for op in reversed(stack):
        repaired += "}" if op == "{" else "]"
    try:
        return json.loads(repaired)
    except Exception:
        pass

    # LAST RESORT: KEEP WHAT COMPLETED, DROP WHAT DID NOT.
    #
    # Balancing brackets cannot fix a reply cut off mid-KEY or mid-ELEMENT — it yields things
    # like {"brief": {"hyp"} or a half-written finding, so the whole object was discarded
    # INCLUDING the findings that had already parsed. That is how a sweep carrying eleven good
    # findings reported "no findings".
    #
    # Walk back through the completed boundaries — the end of any value, or the comma before
    # the next one — and at each, close whatever is still open and try again. The first prefix
    # that parses is the most complete valid dossier the reply contained. A partial dossier is
    # worth far more than an empty one, and the caller can see which keys survived.
    def _close(prefix: str) -> str:
        instr2 = esc2 = False
        stack2: list[str] = []
        for ch2 in prefix:
            if instr2:
                if esc2:
                    esc2 = False
                elif ch2 == "\\":
                    esc2 = True
                elif ch2 == '"':
                    instr2 = False
                continue
            if ch2 == '"':
                instr2 = True
            elif ch2 in "{[":
                stack2.append(ch2)
            elif ch2 in "}]" and stack2:
                stack2.pop()
        out = prefix.rstrip().rstrip(",")
        for op2 in reversed(stack2):
            out += "}" if op2 == "{" else "]"
        return out

    depth = 0
    instr = esc = False
    cuts: list[int] = []
    for n, ch in enumerate(s):
        if instr:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                instr = False
            continue
        if ch == '"':
            instr = True
        elif ch in "{[":
            depth += 1
        elif ch in "}]":
            depth -= 1
            if depth >= 1:
                cuts.append(n + 1)       # a value just completed, at any nesting level
        elif ch == "," and depth >= 1:
            cuts.append(n)               # just before whatever came next
    for cut in reversed(cuts):
        try:
            parsed = json.loads(_close(s[:cut]))
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            continue
    return {}
