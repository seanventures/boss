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
from .registry import ToolRegistry, ToolSpec


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
                _text(
                    "SELECT * FROM code_task_runs WHERE id=:id AND project_id=:p"
                ),
                {"id": run_id, "p": project_id},
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
        # list_previews already publicizes: url/open_url are gateway paths, not loopback.
        previews = ctx.list_previews(pid, int(args.get("limit") or 25))
        return {
            "count": len(previews),
            "previews": [
                {
                    "id": p.get("id"),
                    "run_id": p.get("run_id"),
                    "status": p.get("status"),
                    "url": p.get("url"),
                    "open_url": p.get("open_url") or p.get("url"),
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
        # list_previews already publicizes; never hand agents the sandbox loopback address.
        previews = [p for p in ctx.list_previews(pid, 100) if p.get("run_id") == run_id]
        live = [p for p in previews if p.get("status") == "running"]
        return {
            "run_id": run_id,
            "has_live_preview": bool(live),
            "urls": [p.get("open_url") or p.get("url") for p in live if (p.get("open_url") or p.get("url"))],
            "all": [
                {
                    "id": p.get("id"),
                    "status": p.get("status"),
                    "url": p.get("open_url") or p.get("url"),
                    "open_url": p.get("open_url") or p.get("url"),
                }
                for p in previews
            ],
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
                        "WHERE project_id=:p AND sandbox_task_id IS NOT NULL "
                        "AND status='running'"
                    ),
                    {"p": project_id},
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
                "repository": {
                    "type": "string",
                    "description": "which registered repo to build in (owner/name, repo "
                                   "name, or binding id). Omit to use the project's "
                                   "primary. See list_repositories.",
                },
                "base_branch": {"type": "string", "description": "branch to build on (default main)"},
                "risk_tier": {"type": "integer", "minimum": 0, "maximum": 3},
                "profile": {
                    "type": "string",
                    "enum": ["lean", "balanced", "strict", "vag"],
                    "description": "Cost/rigor dial. Use 'lean' for small, low-risk "
                                   "changes (copy, CSS, a flag): it caps the risk tier "
                                   "at 1 and runs every stage on the cheap model. The "
                                   "project default may otherwise force a full "
                                   "frontier-model pipeline — a one-line change then "
                                   "costs the same ~9 agent sessions as a whole feature.",
                },
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
        rr = services_module("repo_registry")
        with ctx.begin() as c:
            # Resolve the target repo through the project's registry instead of the single
            # globally-configured repo. Falls back to the legacy settings when a project
            # has no bindings yet, so existing single-repo setups are unaffected.
            try:
                target = rr.resolve_repository_target(
                    c, ctx.settings, pid, selector=args.get("repository"))
            except rr.RepositoryResolutionError as exc:
                return {"ok": False, "error": str(exc)}
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
                owner=target["owner"],
                repo=target["repo"],
                base_branch=base_branch,
                finalize_ctx=finalize_ctx,
                jobs=ctx.jobs,
                risk_tier=risk_tier,
                profile=args.get("profile"),
            )
        return {
            "status": "queued",
            "run_id": run_id,
            "project_id": pid,
            "repository": target["full_name"],
            "repository_source": target["source"],
            "binding_id": target["binding_id"],
            "base_branch": base_branch,
            "extended_existing_app": bool(extended_from),
            "extended_from_root": extended_from,
        }

    @reg.tool(
        "extend_code_task",
        "Build ON TOP OF an app a previous code task already produced — new features, real "
        "API integration, auth, anything additive. USE THIS whenever the work continues an "
        "existing app, INCLUDING when that task COMPLETED successfully. It reuses the same "
        "branch, the code already written and the open PR, so the agent edits the real app "
        "instead of starting again. "
        "Do NOT use create_code_task for this: a new task branches from the default branch, "
        "and an earlier app whose PR is still open does not exist there — the agent finds "
        "nothing to build on and writes a SECOND, parallel app. Use list_code_tasks to find "
        "the run whose app you mean.",
        _obj(
            {
                "run_id": {"type": "string",
                           "description": "the run whose app to extend (crun_...)"},
                "instructions": {"type": "string",
                                 "description": "what to add or change, in plain language"},
                "profile": {"type": "string", "enum": ["lean", "balanced", "strict", "vag"]},
            },
            ["run_id", "instructions"],
        ),
        kind="write",
    )
    def extend_code_task(ctx, *, project_id, actor, args):
        cts = services_module("code_task_service")
        result = cts.retry_code_task(
            ctx.engine, ctx.settings, ctx.jobs, args["run_id"],
            fix_note=(args.get("instructions") or ""),
            profile=(args.get("profile") or None),
            allow_test_retirement=bool(args.get("allow_test_retirement")),
        )
        return {"status": "extending", "result": _json_safe(result),
                "note": ("continues the existing lineage: same branch, same PR, "
                         "the previous app is already in the working tree")}

    @reg.tool(
        "retry_code_task",
        "Re-run a code-task run that FAILED or is BLOCKED, as a new attempt in the same "
        "lineage, carrying prior findings forward (optionally with a fix note). "
        "Can also SWITCH THE PROFILE for the new attempt (e.g. profile='lean' to re-run "
        "the same work faster). Prefer this over create_code_task whenever the work "
        "already has a run: it reuses the branch, the existing code and the approved plan. "
        "To ADD to an app that completed successfully, use extend_code_task instead.",
        _obj(
            {
                "run_id": {"type": "string"},
                "fix_note": {"type": "string"},
                "profile": {
                    "type": "string",
                    "enum": ["lean", "balanced", "strict", "vag"],
                    "description": "Switch the cost/rigor dial for THIS attempt. 'lean' keeps "
                                   "test-first discipline and the regression suite but convenes "
                                   "no adversarial reviewer (~3 agent sessions instead of ~6). "
                                   "Omit to keep the profile the previous attempt used. Retrying "
                                   "is ALWAYS preferable to creating a new task for the same "
                                   "work: a retry reuses the branch, the code already written, "
                                   "and the approved plan; a new task rebuilds from scratch.",
                },
                "repair_retries": {"type": "integer", "minimum": 0, "maximum": 5},
                "allow_test_retirement": {
                    "type": "boolean",
                    "description": "Permit REMOVING existing tests that this change makes "
                                   "obsolete — e.g. consolidating two apps, where tests "
                                   "asserting the retired app still exists cannot pass. Every "
                                   "removal is recorded on the PR for a human to reverse. "
                                   "Without this, such a change is impossible rather than "
                                   "merely gated: the agent cannot satisfy contradictory "
                                   "tests and will fail repeatedly.",
                },
            },
            ["run_id"],
        ),
        kind="write",
    )
    def retry_code_task(ctx, *, project_id, actor, args):
        run_id = args["run_id"]
        with ctx.begin() as c:
            if _code_task_in_project(c, run_id, project_id) is None:
                return {"ok": False, "error": f"no code task run {run_id}"}
        cts = services_module("code_task_service")
        rr = args.get("repair_retries")
        result = cts.retry_code_task(
            ctx.engine, ctx.settings, ctx.jobs, run_id,
            fix_note=(args.get("fix_note") or ""),
            profile=(args.get("profile") or None),
            repair_retries=(int(rr) if isinstance(rr, int) else None),
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
        "opened in a browser. Returns the preview record; the URL follows shortly. "
        "REQUIRES the run to have opened a pull request — a run still building, parked on "
        "a plan-approval decision, or failed has no completed change to preview, and this "
        "returns {preview: null, error: 'preview_unavailable_no_pr'} with the run's status.",
        _obj(
            {"run_id": {"type": "string"}, "ttl_minutes": {"type": "integer", "default": 60}},
            ["run_id"],
        ),
        kind="write",
    )
    def start_preview(ctx, *, project_id, actor, args):
        run_id = args["run_id"]
        with ctx.begin() as c:
            if _code_task_in_project(c, run_id, project_id) is None:
                return {"ok": False, "error": f"no code task run {run_id}"}
        ps = services_module("preview_service")
        try:
            rec = ps.start_preview(
                ctx.engine, ctx.settings, run_id,
                ttl_minutes=int(args.get("ttl_minutes") or 60),
            )
        except ps.AmbiguousAppError as exc:
            return {"preview": None, "error": "preview_ambiguous_app",
                    "message": str(exc), "candidates": exc.candidates}
        except ps.PreviewNotReady as exc:
            # Return the reason as DATA, not a raised tool error: the calling agent can act on
            # "this run has no PR yet" (wait, approve the plan gate, retry) but can do nothing
            # with a stack trace. Mirrors the console, which hides the button until a PR exists.
            return {"preview": None, **exc.as_detail()}
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
        # Session actor is authoritative — never honor a model-supplied actor.
        who = actor
        note = (args.get("note") or "").strip()
        if not note:
            return {"ok": False, "error": "note is required — it is the provenance record"}
        decision_id = args["decision_id"]
        dec_mod = services_module("decisions")
        svc = dec_mod.DecisionService(ctx.settings)
        try:
            with ctx.begin() as c:
                if _decision_in_project(c, decision_id, project_id) is None:
                    return {"ok": False, "error": f"no decision {decision_id}"}
                row = svc.transition(
                    c, decision_id, action=action, actor=who, note=note,
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
            run = _code_task_in_project(c, run_id, project_id)
            if run is None:
                return {"ok": False, "error": f"no code task run {run_id}"}
            if run["status"] in ("completed", "failed", "cancelled"):
                return {"ok": False, "error": f"run is already terminal: {run['status']}"}
            c.execute(
                _text("UPDATE code_task_runs SET status='cancelled', error=:e, updated_at=now() "
                      "WHERE id=:id AND project_id=:p"),
                {"id": run_id, "e": reason, "p": project_id},
            )
            jobs = c.execute(
                _text("SELECT id FROM jobs WHERE project_id=:p AND payload::text LIKE :pat "
                      "AND status NOT IN ('succeeded','cancelled','deadlettered','failed')"),
                {"p": project_id, "pat": f"%{run_id}%"},
            ).mappings().all()
            for j in jobs:
                c.execute(
                    _text("UPDATE jobs SET status='cancelled', updated_at=now() "
                          "WHERE id=:id AND project_id=:p"),
                    {"id": j["id"], "p": project_id},
                )
            emit_event(c, project_id, "code_task.cancelled", actor, "code_task_run",
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
        run_id = args["run_id"]
        with ctx.begin() as c:
            if _code_task_in_project(c, run_id, project_id) is None:
                return {"ok": False, "error": f"no code task run {run_id}"}
        cts = services_module("code_task_service")
        try:
            result = cts.override_code_task(
                ctx.engine, ctx.settings, ctx.jobs, run_id,
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
        run_id = args["run_id"]
        with ctx.begin() as c:
            if _code_task_in_project(c, run_id, project_id) is None:
                return {"ok": False, "error": f"no code task run {run_id}"}
        fa = services_module("feedback_agent")
        try:
            result = fa.submit_feedback(
                ctx.engine, run_id, message=args["message"],
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
        preview_id = args["preview_id"]
        with ctx.begin() as c:
            if _preview_in_project(c, preview_id, project_id) is None:
                return {"ok": False, "error": f"no preview {preview_id}"}
        ps = services_module("preview_service")
        try:
            result = ps.stop_preview(ctx.engine, preview_id, ctx.settings)
        except Exception as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        return {"ok": True, "preview": _json_safe(result)}

    @reg.tool(
        "steer_code_task",
        "Redirect a RUNNING task without killing it. The guidance is applied at the next "
        "stage boundary and OVERRIDES the original instructions where they conflict. Use it "
        "the moment watch_code_task shows the agent going the wrong way — otherwise the only "
        "option is cancel-and-restart, which throws away everything done so far.",
        _obj({"run_id": {"type": "string"},
              "guidance": {"type": "string",
                           "description": "what to do differently, in plain terms"}},
             ["run_id", "guidance"]),
        kind="write",
    )
    def steer_code_task(ctx, *, project_id, actor, args):
        import json as _json
        from datetime import datetime, timezone
        guidance = (args.get("guidance") or "").strip()
        if not guidance:
            return {"ok": False, "error": "guidance is required"}
        run_id = args["run_id"]
        entry = {"text": guidance[:4000], "actor": actor,
                 "at": datetime.now(timezone.utc).isoformat()}
        with ctx.begin() as c:
            # Session project fence — without this, project A can redirect project B's
            # running agent (OPERATOR STEERING overrides the original prompt).
            row = _code_task_in_project(c, run_id, project_id)
            if not row:
                return {"ok": False, "error": f"no code task run {run_id}"}
            if row["status"] in ("completed", "failed", "cancelled"):
                return {"ok": False,
                        "error": f"run is {row['status']}; steering applies to running tasks"}
            c.execute(_text(
                "UPDATE code_task_runs SET finalize_ctx = COALESCE(finalize_ctx,'{}'::jsonb) "
                "|| jsonb_build_object('pending_guidance', "
                "COALESCE(finalize_ctx->'pending_guidance','[]'::jsonb) || CAST(:e AS JSONB)), "
                "updated_at=now() WHERE id=:id AND project_id=:p"),
                {"e": _json.dumps([entry]), "id": run_id, "p": project_id})
        return {"ok": True, "run_id": run_id,
                "applies": "at the next stage boundary"}

    @reg.tool(
        "watch_code_task",
        "LIVE tool-level feed of what the coding agent is doing RIGHT NOW: each file it "
        "reads or edits, lines written so far, and whether it is exploring or editing. Use "
        "this while a task is running — get_run_activity only reports a stage AFTER it "
        "finishes, so a 20-minute `implement` is otherwise a black box. Answers 'is it "
        "patching or rebuilding?' before the money is spent.",
        _obj({"run_id": {"type": "string"},
              "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 40},
              "as_text": {"type": "boolean", "default": True,
                          "description": "terminal-style lines (default) or structured JSON"}},
             ["run_id"]),
    )
    def watch_code_task(ctx, *, project_id, actor, args):
        rid = args["run_id"]
        with ctx.begin() as c:
            if _code_task_in_project(c, rid, project_id) is None:
                return {"ok": False, "error": f"no code task run {rid}"}
        la = services_module("live_activity")
        limit = int(args.get("limit") or 40)
        if args.get("as_text", True):
            return {"run_id": rid, "feed": la.live_activity_text(rid, limit=limit)}
        return _json_safe(la.live_activity(rid, limit=limit))

    @reg.tool(
        "get_run_activity",
        "Per-LLM-call activity for a code-task run: stage, model, tokens, cost, "
        "latency, retries, turns and errors. The answer to 'WHY did this phase fail "
        "or take so long / what did it cost?'.",
        _obj({"run_id": {"type": "string"}, "limit": _LIMIT}, ["run_id"]),
    )
    def get_run_activity(ctx, *, project_id, actor, args):
        run_id = args["run_id"]
        limit = int(args.get("limit") or 100)
        with ctx.begin() as c:
            if _code_task_in_project(c, run_id, project_id) is None:
                return {"ok": False, "error": f"no code task run {run_id}"}
            rows = c.execute(
                _text(
                    "SELECT stage, engine, model, total_tokens, cost_usd_micros, latency_ms, "
                    "       retries, turns, ok, error, created_at "
                    "FROM llm_calls WHERE code_task_run_id = :rid AND project_id = :p "
                    "ORDER BY created_at, id LIMIT :lim"
                ),
                {"rid": run_id, "p": project_id, "lim": limit},
            ).mappings().all()
        calls = [_json_safe(dict(r)) for r in rows]
        total = sum(int(r["cost_usd_micros"] or 0) for r in rows)
        return {
            "run_id": run_id, "calls": len(calls),
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
        # Session project is the fence. args.project_id is stripped by MCP HTTP and
        # the discuss-path pin; never fall back to a model-supplied override here
        # because the previous query had NO project filter at all and leaked every
        # tenant's spend ledger.
        pid = project_id
        days = int(args.get("days") or 7)
        limit = int(args.get("limit") or 25)
        with ctx.begin() as c:
            rows = c.execute(
                _text(
                    "SELECT l.code_task_run_id AS run_id, r.title, r.status, "
                    "       sum(l.cost_usd_micros) AS micros, sum(l.total_tokens) AS tokens, "
                    "       count(*) AS calls, max(l.created_at) AS last_call "
                    "FROM llm_calls l LEFT JOIN code_task_runs r ON r.id = l.code_task_run_id "
                    "WHERE l.project_id = :p "
                    "  AND l.created_at > now() - make_interval(days => :d) "
                    "GROUP BY l.code_task_run_id, r.title, r.status "
                    "ORDER BY max(l.created_at) DESC LIMIT :lim"
                ),
                {"p": pid, "d": days, "lim": limit},
            ).mappings().all()
        runs = []
        for r in rows:
            d = _json_safe(dict(r))
            d["cost_usd"] = round(int(r["micros"] or 0) / 1_000_000, 4)
            d.pop("micros", None)
            runs.append(d)
        return {
            "project_id": pid,
            "window_days": days,
            "total_cost_usd": round(sum(r["cost_usd"] for r in runs), 4),
            "runs": runs,
        }

    @reg.tool(
        "list_repositories",
        "Every repository registered to this project, with the health of the connection "
        "that owns it. Use before starting work to pick the right repo — BOSS is "
        "multi-repo; do NOT assume one global default.",
        _obj({"project_id": {"type": "string"}, "limit": _LIMIT}),
    )
    def list_repositories(ctx, *, project_id, actor, args):
        rr = services_module("repo_registry")
        pid = args.get("project_id") or project_id
        with ctx.begin() as c:
            repos = rr.list_repositories(c, pid, limit=int(args.get("limit") or 50))
            try:
                target = rr.resolve_repository_target(c, ctx.settings, pid)
            except Exception as exc:  # no registry yet, or ambiguous — report, don't raise
                target = {"error": str(exc)}
        return {"project_id": pid, "count": len(repos), "repositories": repos,
                "default_target": target}

    @reg.tool(
        "repo_activity",
        "What BOSS actually DID in each repo: runs, PRs opened/merged, success and "
        "first-pass rates, median time-to-PR, spend and $/PR, plus the recent runs. "
        "The answer to 'how is BOSS performing in my repository?'.",
        _obj({"days": {"type": "integer", "minimum": 1, "maximum": 365, "default": 30},
              "project_id": {"type": "string"}}),
    )
    def repo_activity(ctx, *, project_id, actor, args):
        rr = services_module("repo_registry")
        pid = args.get("project_id") or project_id
        with ctx.begin() as c:
            acts = rr.repository_activity(c, pid, days=int(args.get("days") or 30))
        try:
            rr.fill_merged_counts(acts, token=getattr(ctx.settings, "github_token", "") or "")
        except Exception:
            pass
        return {"project_id": pid, "repositories": _json_safe(acts)}

    @reg.tool(
        "register_repository",
        "Register a repository against an existing GitHub connection so BOSS can build "
        "in it. Credentials come from the CONNECTION (a stored SecretRef) — never pass a "
        "token here. Use list_github_connections to find connection_id.",
        _obj(
            {
                "connection_id": {"type": "string"},
                "repository": {"type": "string", "description": "owner/name"},
                "role": {"type": "string", "enum": ["primary", "source", "upstream", "mirror"],
                         "default": "source",
                         "description": "primary = the project's default build target"},
                "default_branch": {"type": "string"},
                "project_id": {"type": "string"},
            },
            ["connection_id", "repository"],
        ),
        kind="write",
    )
    def register_repository(ctx, *, project_id, actor, args):
        gc = services_module("github_connections")
        svc = gc.GitHubConnectionService(ctx.settings)
        pid = args.get("project_id") or project_id
        try:
            with ctx.begin() as c:
                row = svc.create_repository_binding(
                    c, project_id=pid, connection_id=args["connection_id"],
                    repository=args["repository"], role=args.get("role") or "secondary",
                    default_branch=args.get("default_branch"), metadata={}, actor=actor,
                )
        except Exception as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        return {"ok": True, "binding": _json_safe(dict(row))}

    @reg.tool(
        "list_github_connections",
        "GitHub connections for this project: the credentialed accounts/installations "
        "repositories are bound to, with status and any missing permissions. Secrets are "
        "never returned — only the reference and its health.",
        _obj({"project_id": {"type": "string"}, "limit": _LIMIT}),
    )
    def list_github_connections(ctx, *, project_id, actor, args):
        pid = args.get("project_id") or project_id
        with ctx.begin() as c:
            rows = c.execute(
                _text(
                    "SELECT id, connection_label, auth_method, status, account_login, "
                    "       app_id, installation_id, api_base_url, last_test_status, "
                    "       last_tested_at, missing_permissions, created_at "
                    "FROM github_connections WHERE project_id=:p "
                    "ORDER BY created_at DESC LIMIT :lim"
                ),
                {"p": pid, "lim": int(args.get("limit") or 50)},
            ).mappings().all()
        return {"project_id": pid, "count": len(rows),
                "connections": [_json_safe(dict(r)) for r in rows]}

    @reg.tool(
        "get_meeting",
        "Full detail for one PRD meeting run (mrun_…): status, phase, directive, the "
        "panel roles and the linked decision. Read this BEFORE approving its PRD.",
        _obj({"meeting_id": {"type": "string"}}, ["meeting_id"]),
    )
    def get_meeting(ctx, *, project_id, actor, args):
        with ctx.begin() as c:
            row = c.execute(
                _text(
                    "SELECT * FROM meeting_runs "
                    "WHERE project_id=:p AND (id=:id OR meeting_id=:id)"
                ),
                {"id": args["meeting_id"], "p": project_id},
            ).mappings().first()
            if row is None:
                return {"ok": False, "error": f"no meeting {args['meeting_id']}"}
            dec = None
            if row.get("decision_id"):
                dec = c.execute(
                    _text("SELECT id, question, status, approved_by, approved_at "
                          "FROM decisions WHERE id=:d AND project_id=:p"),
                    {"d": row["decision_id"], "p": project_id},
                ).mappings().first()
        return {"meeting": _json_safe(dict(row)),
                "decision": _json_safe(dict(dec)) if dec else None}


def _text(sql: str):
    from sqlalchemy import text

    return text(sql)


def _code_task_in_project(connection, run_id: str, project_id: str):
    """Load a code-task run only when it belongs to the session project.

    Governance mutators historically keyed only on run_id, so an MCP/agent
    session bound to project A could cancel, waive, retry, or preview a run
    from project B. Session project_id is the tenant fence.
    """
    return connection.execute(
        _text(
            "SELECT id, project_id, status FROM code_task_runs "
            "WHERE id=:id AND project_id=:p"
        ),
        {"id": run_id, "p": project_id},
    ).mappings().first()


def _preview_in_project(connection, preview_id: str, project_id: str):
    """Load a preview only when it belongs to the session project."""
    return connection.execute(
        _text(
            "SELECT id, project_id, status FROM app_previews "
            "WHERE id=:id AND project_id=:p"
        ),
        {"id": preview_id, "p": project_id},
    ).mappings().first()


def _decision_in_project(connection, decision_id: str, project_id: str):
    """Load a decision only when it belongs to the session project."""
    return connection.execute(
        _text("SELECT id, project_id, status FROM decisions WHERE id=:id AND project_id=:p"),
        {"id": decision_id, "p": project_id},
    ).mappings().first()


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
    # "pr_url" is not a key on a run — the column is pull_request_url. So the PR link this
    # tool's own description promises ("with status, phase, title and PR link") was ALWAYS
    # dropped, and an agent asked to act on a PR could not find one. Include both: the real
    # key, plus the short alias callers may already expect.
    keys = ("id", "status", "phase", "title", "pull_request_url", "pull_request_number",
            "created_at", "task_id")
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
# Unrestricted SQL is fine for a trusted local MCP/CLI operator; it is NOT safe
# on the multi-tenant chat discuss path where the model chooses the query.
_DISCUSS_EXCLUDED_READ_TOOLS = frozenset({"query_db"})


def build_default_registry() -> ToolRegistry:
    """The full BOSS engine tool set (read + write)."""
    reg = ToolRegistry()
    _register_read_tools(reg)
    _register_write_tools(reg)
    _register_governance_tools(reg)
    return reg


def build_discuss_read_registry() -> ToolRegistry:
    """Project-pinned read tools for IntentRouter's tool-grounded discuss path.

    Excludes ``query_db`` (arbitrary SELECT across the whole DB) and strips
    ``project_id`` from schemas/args so the model cannot override into another
    tenant after the API already authorized a single project.
    """
    source = build_default_registry()
    reg = ToolRegistry()
    for spec in source.specs(kind="read"):
        if spec.name in _DISCUSS_EXCLUDED_READ_TOOLS:
            continue
        properties = dict((spec.parameters or {}).get("properties") or {})
        properties.pop("project_id", None)
        parameters = {**(spec.parameters or {}), "properties": properties}
        required = [
            item
            for item in list((spec.parameters or {}).get("required") or [])
            if item != "project_id"
        ]
        if "required" in parameters:
            parameters["required"] = required

        def _pin(inner: Any = spec.handler) -> Any:
            def handler(ctx, *, project_id, actor, args):
                safe_args = dict(args or {})
                safe_args.pop("project_id", None)
                return inner(
                    ctx, project_id=project_id, actor=actor, args=safe_args
                )

            return handler

        reg.register(
            ToolSpec(
                name=spec.name,
                description=spec.description,
                parameters=parameters,
                handler=_pin(),
                kind="read",
            )
        )
    return reg
