"""The built-in BOSS engine tools.

This is the concrete answer to "the BOSS Agent should provide all sorts of info,
not just create tasks" — every read/write capability the console + API expose is
here as an in-process tool. Read tools give the agent an overall view of the
workflow engine (what's running, previews, discovery, meetings, the microVM
fleet, and governed Postgres reads); write tools create workflow tasks.

Handlers share one signature:  handler(ctx, *, project_id, actor, args) -> dict
They call engine helpers through `EngineContext`; they never touch HTTP.
"""
from __future__ import annotations

import json
import re
from typing import Any

from .._compat import services_module
from .registry import ToolRegistry


# ---------------------------------------------------------------------------
# schema fragments
# ---------------------------------------------------------------------------
def _obj(props: dict, required: list[str] | None = None) -> dict:
    return {
        "type": "object",
        "properties": props,
        "required": required or [],
        "additionalProperties": False,
    }


_LIMIT = {"type": "integer", "minimum": 1, "maximum": 200, "default": 25}


# ===========================================================================
# READ TOOLS — the "overall view of the workflow engine"
# ===========================================================================
def _register_read_tools(reg: ToolRegistry) -> None:
    @reg.tool(
        "project_status",
        "Snapshot of everything happening in a project: counts of running/queued "
        "code tasks, discovery runs, meetings, background jobs and live previews. "
        "The go-to answer for 'what's running / how busy is BOSS right now?'.",
        _obj({"project_id": {"type": "string", "description": "override the default project"}}),
    )
    def project_status(ctx, *, project_id, actor, args):
        pid = args.get("project_id") or project_id
        with ctx.begin() as c:
            def n(sql: str) -> int:
                return int(c.execute(_text(sql), {"p": pid}).scalar() or 0)

            running_code = n(
                "SELECT count(*) FROM code_task_runs WHERE project_id=:p AND status='running'"
            )
            running_disc = n(
                "SELECT count(*) FROM discovery_runs WHERE project_id=:p AND status='running'"
            )
            running_meet = n(
                "SELECT count(*) FROM meeting_runs WHERE project_id=:p AND status='running'"
            )
            live_prev = n(
                "SELECT count(*) FROM app_previews WHERE project_id=:p AND status='running'"
            )
            active_jobs = ctx.jobs.list_jobs(
                c, pid, include_terminal=False, limit=200
            )
        return {
            "project_id": pid,
            "running_code_tasks": running_code,
            "running_discovery": running_disc,
            "running_meetings": running_meet,
            "active_jobs": len(active_jobs),
            "live_previews": live_prev,
            "total_running": running_code + running_disc + running_meet,
        }

    @reg.tool(
        "list_code_tasks",
        "Recent coding-agent task runs (newest first) with status, phase, title and "
        "PR link. Use to answer 'show me the builds / what happened to task X'.",
        _obj({"project_id": {"type": "string"}, "limit": _LIMIT}),
    )
    def list_code_tasks(ctx, *, project_id, actor, args):
        pid = args.get("project_id") or project_id
        limit = int(args.get("limit") or 25)
        with ctx.begin() as c:
            runs = ctx.list_code_task_runs(c, pid, limit)
        return {"count": len(runs), "runs": [_slim_run(r) for r in runs]}

    @reg.tool(
        "get_code_task",
        "Full detail for one code-task run by id (crun_…): status, phase, PR url, "
        "gates, and the failure reason if it failed.",
        _obj({"run_id": {"type": "string"}}, ["run_id"]),
    )
    def get_code_task(ctx, *, project_id, actor, args):
        run_id = args["run_id"]
        with ctx.begin() as c:
            row = c.execute(
                _text("SELECT * FROM code_task_runs WHERE id=:id"), {"id": run_id}
            ).mappings().first()
        if not row:
            return {"ok": False, "error": f"no code task run {run_id}"}
        return {"run": _json_safe(dict(row))}

    @reg.tool(
        "list_running_work",
        "Everything actively running in the background right now — running code "
        "tasks and non-terminal jobs — with how long each has been going. Answers "
        "'how many tasks are running in the background?'.",
        _obj({"project_id": {"type": "string"}}),
    )
    def list_running_work(ctx, *, project_id, actor, args):
        pid = args.get("project_id") or project_id
        with ctx.begin() as c:
            runs = c.execute(
                _text(
                    "SELECT id,title,phase,created_at FROM code_task_runs "
                    "WHERE project_id=:p AND status='running' ORDER BY created_at DESC"
                ),
                {"p": pid},
            ).mappings().all()
            jobs = ctx.jobs.list_jobs(c, pid, include_terminal=False, limit=200)
        return {
            "running_code_tasks": [_json_safe(dict(r)) for r in runs],
            "active_jobs": [
                {"id": j.get("id"), "kind": j.get("kind"), "status": j.get("status")}
                for j in jobs
            ],
            "count": len(runs) + len(jobs),
        }

    @reg.tool(
        "list_discovery",
        "Recent research/discovery runs (outcome discovery dossiers) with status.",
        _obj({"project_id": {"type": "string"}, "limit": _LIMIT}),
    )
    def list_discovery(ctx, *, project_id, actor, args):
        pid = args.get("project_id") or project_id
        with ctx.begin() as c:
            runs = ctx.list_discovery_runs(c, pid, int(args.get("limit") or 25))
        return {"count": len(runs), "runs": [_json_safe(r) for r in runs]}

    @reg.tool(
        "list_meetings",
        "Recent multi-agent PRD meeting runs with status and directive.",
        _obj({"project_id": {"type": "string"}, "limit": _LIMIT}),
    )
    def list_meetings(ctx, *, project_id, actor, args):
        pid = args.get("project_id") or project_id
        limit = int(args.get("limit") or 25)
        with ctx.begin() as c:
            rows = c.execute(
                _text(
                    "SELECT id,status,phase,directive,created_at FROM meeting_runs "
                    "WHERE project_id=:p ORDER BY created_at DESC LIMIT :l"
                ),
                {"p": pid, "l": limit},
            ).mappings().all()
        return {"count": len(rows), "meetings": [_json_safe(dict(r)) for r in rows]}

    @reg.tool(
        "list_previews",
        "Live app previews (running 1-box apps) with their open URLs, so the agent "
        "can hand a customer a link to preview a task's built app.",
        _obj({"project_id": {"type": "string"}, "limit": _LIMIT}),
    )
    def list_previews(ctx, *, project_id, actor, args):
        pid = args.get("project_id") or project_id
        previews = ctx.list_previews(pid, int(args.get("limit") or 25))
        return {
            "count": len(previews),
            "previews": [
                {
                    "id": p.get("id"),
                    "run_id": p.get("run_id"),
                    "status": p.get("status"),
                    "url": p.get("url"),
                    "substrate": p.get("substrate"),
                }
                for p in previews
            ],
        }

    @reg.tool(
        "get_task_preview",
        "The live preview URL(s) for one code-task run (crun_…), if any preview is "
        "running. Answers 'preview this task / give me the link'.",
        _obj({"run_id": {"type": "string"}, "project_id": {"type": "string"}}, ["run_id"]),
    )
    def get_task_preview(ctx, *, project_id, actor, args):
        pid = args.get("project_id") or project_id
        run_id = args["run_id"]
        previews = [p for p in ctx.list_previews(pid, 100) if p.get("run_id") == run_id]
        live = [p for p in previews if p.get("status") == "running"]
        return {
            "run_id": run_id,
            "has_live_preview": bool(live),
            "urls": [p.get("url") for p in live if p.get("url")],
            "all": [{"id": p.get("id"), "status": p.get("status"), "url": p.get("url")} for p in previews],
        }

    @reg.tool(
        "microvm_status",
        "State of the sandbox/microVM execution fleet: the configured build "
        "substrate, the sandbox agent URL and its reachability, plus any held "
        "sandboxes still attached to running previews/tasks.",
        _obj({}),
    )
    def microvm_status(ctx, *, project_id, actor, args):
        s = ctx.settings
        provider = getattr(s, "governed_sandbox_provider", None)
        agent_url = getattr(s, "sandbox_agent_url", None)
        reachable = None
        detail = None
        if agent_url:
            reachable, detail = _http_probe(agent_url.rstrip("/") + "/health")
        # Held-sandbox listing is best-effort: on an older schema the column may
        # be absent — report the fleet config regardless rather than failing.
        held: list[dict] = []
        held_note = None
        try:
            with ctx.begin() as c:
                rows = c.execute(
                    _text(
                        "SELECT id,run_id,sandbox_task_id,status FROM app_previews "
                        "WHERE sandbox_task_id IS NOT NULL AND status='running'"
                    )
                ).mappings().all()
            held = [_json_safe(dict(h)) for h in rows]
        except Exception as exc:
            held_note = f"held-sandbox query unavailable: {type(exc).__name__}"
        return {
            "substrate": provider,
            "uses_microvm": str(provider).lower() in ("microvm", "firecracker"),
            "sandbox_agent_url": agent_url,
            "sandbox_agent_reachable": reachable,
            "sandbox_agent_detail": detail,
            "held_sandboxes": held,
            "held_sandboxes_note": held_note,
        }

    @reg.tool(
        "query_db",
        "Run a READ-ONLY SQL query (a single SELECT/WITH) against the engine's "
        "Postgres and return rows. For questions the structured tools don't cover. "
        "Writes and multi-statements are rejected; results are capped.",
        _obj(
            {
                "sql": {"type": "string", "description": "a single SELECT or WITH…SELECT statement"},
                "max_rows": {"type": "integer", "minimum": 1, "maximum": 500, "default": 100},
            },
            ["sql"],
        ),
    )
    def query_db(ctx, *, project_id, actor, args):
        sql = (args.get("sql") or "").strip().rstrip(";")
        ok, why = _is_read_only(sql)
        if not ok:
            return {"ok": False, "error": f"rejected: {why}"}
        max_rows = int(args.get("max_rows") or 100)
        with ctx.begin() as c:
            rows = c.execute(_text(sql)).mappings().all()
        capped = rows[:max_rows]
        return {
            "row_count": len(capped),
            "truncated": len(rows) > max_rows,
            "rows": [_json_safe(dict(r)) for r in capped],
        }


# ===========================================================================
# WRITE TOOLS — create workflow tasks
# ===========================================================================
def _register_write_tools(reg: ToolRegistry) -> None:
    @reg.tool(
        "create_code_task",
        "Start a governed coding-agent task: BOSS provisions a sandbox, implements "
        "the change, runs tests and opens a PR. Use ONLY for an already-specified "
        "engineering change (clear scope, known acceptance criteria) — it skips the "
        "PRD stage. For a NEW product/feature idea, or any 'kick off / spec / plan / "
        "PRD' request, call request_meeting instead: the meeting produces the PRD in "
        "BOSS and auto-starts this code task once the PRD is approved.",
        _obj(
            {
                "title": {"type": "string"},
                "instructions": {"type": "string", "description": "what to build/change"},
                "acceptance_criteria": {"type": "array", "items": {"type": "string"}},
                "test_commands": {"type": "array", "items": {"type": "string"}},
                "task_id": {"type": "string", "description": "attach to an existing task"},
                "base_branch": {"type": "string", "description": "branch to build on (default main)"},
                "risk_tier": {"type": "integer", "minimum": 0, "maximum": 3},
                "project_id": {"type": "string"},
            },
            ["title", "instructions"],
        ),
        kind="write",
    )
    def create_code_task(ctx, *, project_id, actor, args):
        pid = args.get("project_id") or project_id
        cts = services_module("code_task_service")
        task_id = args.get("task_id")
        instructions = args.get("instructions") or ""
        base_branch = args.get("base_branch") or "main"
        finalize_ctx: dict = {}
        risk_tier = args.get("risk_tier")
        extended_from = None
        with ctx.begin() as c:
            # Build-on-existing: if this task already has a built app (a completed
            # code task with a PR), EXTEND that app's branch instead of a fresh
            # build on main. Mirrors intent_router._existing_app_for_task.
            if task_id and not args.get("base_branch"):
                branch, root = _existing_app_branch(c, pid, task_id)
                if branch:
                    base_branch = branch
                    extended_from = root
                    finalize_ctx = {"root_run_id": root, "web_app_build": True}
                    risk_tier = 0 if risk_tier is None else risk_tier
                    instructions = (
                        "EXTEND the existing app on this branch — do NOT rebuild from scratch. "
                        "Make the smallest change that satisfies the request, preserving all "
                        "current features.\n\n" + instructions
                    )
            run_id = cts.start_code_task(
                c,
                project_id=pid,
                task_id=task_id,
                title=args.get("title") or "Code task",
                instructions=instructions,
                acceptance_criteria=args.get("acceptance_criteria") or [],
                test_commands=args.get("test_commands") or [],
                owner=ctx.settings.target_github_owner,
                repo=ctx.settings.target_github_repo,
                base_branch=base_branch,
                finalize_ctx=finalize_ctx,
                jobs=ctx.jobs,
                risk_tier=risk_tier,
            )
        return {
            "status": "queued",
            "run_id": run_id,
            "project_id": pid,
            "base_branch": base_branch,
            "extended_existing_app": bool(extended_from),
            "extended_from_root": extended_from,
        }

    @reg.tool(
        "retry_code_task",
        "Resume a failed/blocked code-task run as a new attempt in the same "
        "lineage, carrying prior findings forward (optionally with a fix note).",
        _obj(
            {"run_id": {"type": "string"}, "fix_note": {"type": "string"}},
            ["run_id"],
        ),
        kind="write",
    )
    def retry_code_task(ctx, *, project_id, actor, args):
        cts = services_module("code_task_service")
        result = cts.retry_code_task(
            ctx.engine, ctx.settings, ctx.jobs, args["run_id"],
            fix_note=(args.get("fix_note") or ""),
        )
        return {"status": "retried", "result": _json_safe(result)}

    @reg.tool(
        "create_discovery",
        "Kick off a research/discovery run: BOSS studies the idea/problem on the "
        "web and produces a cited dossier scored against an evidence gate.",
        _obj(
            {
                "seed": {"type": "string", "description": "the idea/problem to research"},
                "title": {"type": "string"},
                "context": {"type": "string"},
                "deep": {"type": "boolean", "description": "deeper multi-source sweep"},
                "project_id": {"type": "string"},
            },
            ["seed"],
        ),
        kind="write",
    )
    def create_discovery(ctx, *, project_id, actor, args):
        pid = args.get("project_id") or project_id
        ds = services_module("discovery_service")
        seed = (args.get("seed") or "").strip()
        run_id = ds.start_discovery(
            project_id=pid,
            title=(args.get("title") or seed[:120] or "Discovery").strip(),
            seed=seed,
            context=(args.get("context") or "").strip(),
            settings=ctx.settings,
            deep=bool(args.get("deep")),
        )
        return {"status": "running", "run_id": run_id, "project_id": pid}

    @reg.tool(
        "request_meeting",
        "THE way to kick off new work in BOSS: convene a governed multi-agent PRD "
        "meeting. A cross-functional agent panel debates the directive inside BOSS "
        "(visible in the Meetings area) and produces the meeting minutes and the PRD; "
        "once the PRD is approved, BOSS auto-starts the governed code task. Use for "
        "any 'kick off / spec / plan / design X' or new product/feature idea. Do NOT "
        "write agenda/minutes/PRD documents yourself and do NOT jump to "
        "create_code_task — this tool owns that pipeline.",
        _obj(
            {
                "directive": {"type": "string", "description": "what to design/plan"},
                "url": {"type": "string", "description": "a reference site to study"},
                "task_id": {"type": "string"},
                "project_id": {"type": "string"},
            },
            ["directive"],
        ),
        kind="write",
    )
    def request_meeting(ctx, *, project_id, actor, args):
        pid = args.get("project_id") or project_id
        ms = services_module("meeting_service")
        with ctx.begin() as c:
            result = ms.convene_prd_meeting_core(
                c,
                settings=ctx.settings,
                budget=ctx.budget,
                intake=ctx.intake,
                jobs=ctx.jobs,
                project_id=pid,
                directive=(args.get("directive") or "").strip(),
                url=(args.get("url") or "").strip(),
                task_id=args.get("task_id"),
            )
        return {"status": "running", "meeting": _json_safe(result), "project_id": pid}

    @reg.tool(
        "start_preview",
        "Start (or replace) a live preview for a code-task run so the app can be "
        "opened in a browser. Returns the preview record; the URL follows shortly.",
        _obj(
            {"run_id": {"type": "string"}, "ttl_minutes": {"type": "integer", "default": 60}},
            ["run_id"],
        ),
        kind="write",
    )
    def start_preview(ctx, *, project_id, actor, args):
        ps = services_module("preview_service")
        rec = ps.start_preview(
            ctx.engine, ctx.settings, args["run_id"], ttl_minutes=int(args.get("ttl_minutes") or 60)
        )
        return {"preview": _json_safe(rec)}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
# ===========================================================================
# GOVERNANCE / CONTROL TOOLS
#
# Without these an external agent (Copilot, Claude) can START governed work but
# cannot FINISH it: BOSS's own flow is "meeting -> human PRD approval -> code
# task", and approval lived only in the console. These close that loop and add
# the operations an operator actually needs mid-flight -- decide, cancel, waive,
# steer with feedback, and see cost/activity.
#
# `decide` is deliberately ONE tool with an action enum rather than five tools
# (approve/reject/revise/delegate/execute): same power, a fifth of the schema
# tokens every client pays on every request.
# ===========================================================================
_DECISION_ACTIONS = ("approve", "reject", "revise", "delegate", "execute")


def _register_governance_tools(reg: ToolRegistry) -> None:
    @reg.tool(
        "list_decisions",
        "Decisions waiting on a human: PRD approvals, plan gates and merge gates. "
        "The answer to 'what is blocked on me?'. Pair with `decide` to unblock.",
        _obj({
            "status": {"type": "string", "enum": ["pending", "approved", "rejected", "all"],
                       "default": "pending"},
            "limit": _LIMIT,
            "project_id": {"type": "string"},
        }),
    )
    def list_decisions(ctx, *, project_id, actor, args):
        pid = args.get("project_id") or project_id
        status = args.get("status") or "pending"
        limit = int(args.get("limit") or 25)
        clause = "" if status == "all" else "AND status = :st"
        with ctx.begin() as c:
            rows = c.execute(
                _text(
                    "SELECT id, task_id, question, status, risk_tier, default_action, "
                    "       approved_by, approved_at, resolution_note, deadline_at, created_at "
                    f"FROM decisions WHERE project_id = :p {clause} "
                    "ORDER BY created_at DESC LIMIT :lim"
                ),
                {"p": pid, "st": status, "lim": limit},
            ).mappings().all()
        return {"count": len(rows), "decisions": [_json_safe(dict(r)) for r in rows]}

    @reg.tool(
        "decide",
        "Act on a decision (PRD approval, plan gate, merge gate): approve, reject, "
        "revise, delegate or execute it. This is how governed work gets unblocked "
        "from chat. `note` is REQUIRED and is recorded on the decision — if no human "
        "actually reviewed it, say so in the note.",
        _obj(
            {
                "decision_id": {"type": "string"},
                "action": {"type": "string", "enum": list(_DECISION_ACTIONS)},
                "note": {"type": "string", "description": "why — recorded as provenance"},
                "actor": {"type": "string", "description": "who is deciding (default: caller)"},
                "delegate_to": {"type": "string", "description": "for action=delegate"},
            },
            ["decision_id", "action", "note"],
        ),
        kind="write",
    )
    def decide(ctx, *, project_id, actor, args):
        action = args["action"]
        if action not in _DECISION_ACTIONS:
            return {"ok": False, "error": f"unsupported action: {action}"}
        who = args.get("actor") or actor
        note = (args.get("note") or "").strip()
        if not note:
            return {"ok": False, "error": "note is required — it is the provenance record"}
        dec_mod = services_module("decisions")
        svc = dec_mod.DecisionService(ctx.settings)
        try:
            with ctx.begin() as c:
                row = svc.transition(
                    c, args["decision_id"], action=action, actor=who, note=note,
                    delegate_to=args.get("delegate_to"),
                )
        except Exception as exc:  # surface engine errors as data, never a stack trace
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        return {"ok": True, "action": action, "actor": who, "decision": _json_safe(dict(row))}

    @reg.tool(
        "cancel_code_task",
        "Stop a running/queued code-task run. Cancels the run AND its queued job "
        "together — cancelling only one leaves a zombie that shows as running "
        "forever with nothing driving it.",
        _obj({"run_id": {"type": "string"}, "reason": {"type": "string"}}, ["run_id"]),
        kind="write",
    )
    def cancel_code_task(ctx, *, project_id, actor, args):
        run_id = args["run_id"]
        reason = args.get("reason") or "cancelled via MCP"
        from ..services.core import emit_event
        with ctx.begin() as c:
            run = c.execute(
                _text("SELECT id, project_id, status FROM code_task_runs WHERE id=:id"),
                {"id": run_id},
            ).mappings().first()
            if run is None:
                return {"ok": False, "error": f"no code task run {run_id}"}
            if run["status"] in ("completed", "failed", "cancelled"):
                return {"ok": False, "error": f"run is already terminal: {run['status']}"}
            c.execute(
                _text("UPDATE code_task_runs SET status='cancelled', error=:e, updated_at=now() "
                      "WHERE id=:id"),
                {"id": run_id, "e": reason},
            )
            jobs = c.execute(
                _text("SELECT id FROM jobs WHERE payload::text LIKE :pat "
                      "AND status NOT IN ('succeeded','cancelled','deadlettered','failed')"),
                {"pat": f"%{run_id}%"},
            ).mappings().all()
            for j in jobs:
                c.execute(
                    _text("UPDATE jobs SET status='cancelled', updated_at=now() WHERE id=:id"),
                    {"id": j["id"]},
                )
            emit_event(c, run["project_id"], "code_task.cancelled", actor, "code_task_run",
                       run_id, {"reason": reason, "jobs_cancelled": [j["id"] for j in jobs]})
        return {"ok": True, "run_id": run_id, "jobs_cancelled": [j["id"] for j in jobs],
                "reason": reason}

    @reg.tool(
        "waive_gates",
        "Ship a blocked code task by waiving named failed gate(s): opens the PR from "
        "the diff preserved at gate-failure time, with NO re-run. Gates you do not "
        "waive still block. Use get_code_task first to see which gates failed.",
        _obj(
            {
                "run_id": {"type": "string"},
                "waive_gates": {"type": "array", "items": {"type": "string"}},
                "reason": {"type": "string"},
            },
            ["run_id", "waive_gates"],
        ),
        kind="write",
    )
    def waive_gates(ctx, *, project_id, actor, args):
        cts = services_module("code_task_service")
        try:
            result = cts.override_code_task(
                ctx.engine, ctx.settings, ctx.jobs, args["run_id"],
                waive_gates=args.get("waive_gates") or [],
                actor=actor, reason=args.get("reason") or "waived via MCP",
            )
        except Exception as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        return {"ok": True, "result": _json_safe(result)}

    @reg.tool(
        "send_feedback",
        "Give a running/finished code task human feedback ('the win banner is hidden') "
        "so BOSS folds it into the work instead of you filing a new task.",
        _obj(
            {
                "run_id": {"type": "string"},
                "message": {"type": "string"},
                "expected": {"type": "string", "description": "what you expected instead"},
                "severity": {"type": "string", "enum": ["low", "normal", "high"],
                             "default": "normal"},
            },
            ["run_id", "message"],
        ),
        kind="write",
    )
    def send_feedback(ctx, *, project_id, actor, args):
        fa = services_module("feedback_agent")
        try:
            result = fa.submit_feedback(
                ctx.engine, args["run_id"], message=args["message"],
                expected=args.get("expected") or "",
                severity=args.get("severity") or "normal",
            )
        except Exception as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        return {"ok": True, "feedback": _json_safe(result)}

    @reg.tool(
        "stop_preview",
        "Stop a running app preview and free its port/container. The counterpart to "
        "start_preview.",
        _obj({"preview_id": {"type": "string"}}, ["preview_id"]),
        kind="write",
    )
    def stop_preview(ctx, *, project_id, actor, args):
        ps = services_module("preview_service")
        try:
            result = ps.stop_preview(ctx.engine, args["preview_id"], ctx.settings)
        except Exception as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        return {"ok": True, "preview": _json_safe(result)}

    @reg.tool(
        "get_run_activity",
        "Per-LLM-call activity for a code-task run: stage, model, tokens, cost, "
        "latency, retries, turns and errors. The answer to 'WHY did this phase fail "
        "or take so long / what did it cost?'.",
        _obj({"run_id": {"type": "string"}, "limit": _LIMIT}, ["run_id"]),
    )
    def get_run_activity(ctx, *, project_id, actor, args):
        limit = int(args.get("limit") or 100)
        with ctx.begin() as c:
            rows = c.execute(
                _text(
                    "SELECT stage, engine, model, total_tokens, cost_usd_micros, latency_ms, "
                    "       retries, turns, ok, error, created_at "
                    "FROM llm_calls WHERE code_task_run_id = :rid "
                    "ORDER BY created_at, id LIMIT :lim"
                ),
                {"rid": args["run_id"], "lim": limit},
            ).mappings().all()
        calls = [_json_safe(dict(r)) for r in rows]
        total = sum(int(r["cost_usd_micros"] or 0) for r in rows)
        return {
            "run_id": args["run_id"], "calls": len(calls),
            "total_cost_usd": round(total / 1_000_000, 4),
            "total_tokens": sum(int(r["total_tokens"] or 0) for r in rows),
            "failed_calls": sum(1 for r in rows if not r["ok"]),
            "activity": calls,
        }

    @reg.tool(
        "get_costs",
        "What BOSS has spent: total and per-run USD over a recent window, newest "
        "first. Use before launching expensive work, or to explain a spend spike.",
        _obj({"days": {"type": "integer", "minimum": 1, "maximum": 90, "default": 7},
              "limit": _LIMIT, "project_id": {"type": "string"}}),
    )
    def get_costs(ctx, *, project_id, actor, args):
        days = int(args.get("days") or 7)
        limit = int(args.get("limit") or 25)
        with ctx.begin() as c:
            rows = c.execute(
                _text(
                    "SELECT l.code_task_run_id AS run_id, r.title, r.status, "
                    "       sum(l.cost_usd_micros) AS micros, sum(l.total_tokens) AS tokens, "
                    "       count(*) AS calls, max(l.created_at) AS last_call "
                    "FROM llm_calls l LEFT JOIN code_task_runs r ON r.id = l.code_task_run_id "
                    "WHERE l.created_at > now() - make_interval(days => :d) "
                    "GROUP BY l.code_task_run_id, r.title, r.status "
                    "ORDER BY max(l.created_at) DESC LIMIT :lim"
                ),
                {"d": days, "lim": limit},
            ).mappings().all()
        runs = []
        for r in rows:
            d = _json_safe(dict(r))
            d["cost_usd"] = round(int(r["micros"] or 0) / 1_000_000, 4)
            d.pop("micros", None)
            runs.append(d)
        return {
            "window_days": days,
            "total_cost_usd": round(sum(r["cost_usd"] for r in runs), 4),
            "runs": runs,
        }

    @reg.tool(
        "get_meeting",
        "Full detail for one PRD meeting run (mrun_…): status, phase, directive, the "
        "panel roles and the linked decision. Read this BEFORE approving its PRD.",
        _obj({"meeting_id": {"type": "string"}}, ["meeting_id"]),
    )
    def get_meeting(ctx, *, project_id, actor, args):
        with ctx.begin() as c:
            row = c.execute(
                _text("SELECT * FROM meeting_runs WHERE id=:id OR meeting_id=:id"),
                {"id": args["meeting_id"]},
            ).mappings().first()
            if row is None:
                return {"ok": False, "error": f"no meeting {args['meeting_id']}"}
            dec = None
            if row.get("decision_id"):
                dec = c.execute(
                    _text("SELECT id, question, status, approved_by, approved_at "
                          "FROM decisions WHERE id=:d"),
                    {"d": row["decision_id"]},
                ).mappings().first()
        return {"meeting": _json_safe(dict(row)),
                "decision": _json_safe(dict(dec)) if dec else None}


def _text(sql: str):
    from sqlalchemy import text

    return text(sql)


def _existing_app_branch(conn, project_id: str, task_id: str) -> tuple:
    """The app branch + lineage root for a task that already has a built app
    (latest completed code-task run with a PR). Mirrors intent_router so chat
    enhancements extend the existing PR instead of rebuilding on main."""
    row = conn.execute(
        _text(
            "SELECT finalize_ctx FROM code_task_runs "
            "WHERE project_id = :p AND task_id = :t AND status = 'completed' "
            "AND pull_request_url IS NOT NULL ORDER BY created_at DESC LIMIT 1"
        ),
        {"p": project_id, "t": task_id},
    ).mappings().first()
    if not row:
        return None, None
    fc = row["finalize_ctx"]
    if isinstance(fc, str):
        try:
            fc = json.loads(fc)
        except (ValueError, TypeError):
            fc = {}
    root = (fc or {}).get("root_run_id")
    return (f"boss/code/{root}", root) if root else (None, None)


def _slim_run(r: dict) -> dict:
    keys = ("id", "status", "phase", "title", "pr_url", "created_at", "task_id")
    out = {k: r.get(k) for k in keys if k in r}
    return _json_safe(out)


def _json_safe(obj: Any) -> Any:
    """Make DB rows JSON-serializable (datetimes -> iso, JSON strings -> parsed)."""
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    if hasattr(obj, "isoformat"):
        return obj.isoformat()
    return obj


_WRITE_KEYWORDS = re.compile(
    r"\b(insert|update|delete|drop|alter|create|grant|revoke|truncate|copy|"
    r"merge|call|do|vacuum|analyze|reindex|comment|lock)\b",
    re.IGNORECASE,
)


def _is_read_only(sql: str) -> tuple[bool, str]:
    if not sql:
        return False, "empty query"
    if ";" in sql:
        return False, "multiple statements are not allowed"
    head = sql.lstrip().split(None, 1)[0].lower() if sql.strip() else ""
    if head not in ("select", "with"):
        return False, "only SELECT / WITH queries are allowed"
    if _WRITE_KEYWORDS.search(sql):
        return False, "write/DDL keyword detected"
    return True, ""


def _http_probe(url: str) -> tuple[bool, str]:
    import urllib.request

    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=3) as resp:  # noqa: S310 (trusted internal URL)
            return (200 <= resp.status < 400), f"HTTP {resp.status}"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


# ---------------------------------------------------------------------------
# assembly
# ---------------------------------------------------------------------------
def build_default_registry() -> ToolRegistry:
    """The full BOSS engine tool set (read + write)."""
    reg = ToolRegistry()
    _register_read_tools(reg)
    _register_write_tools(reg)
    _register_governance_tools(reg)
    return reg
