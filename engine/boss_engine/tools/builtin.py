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
import os as _os
import re
import subprocess as _subprocess
from typing import Any, Optional as _Optional

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

#: Statement kinds that are CRUD on ROWS. DDL is not CRUD: dropping or altering a table is a
#: migration — not reversible from a chat turn — and no operator asking BOSS to "fix the stuck
#: row" means "restructure the schema".
_WRITE_VERBS = ("insert", "update", "delete")
_DDL_VERBS = ("drop", "alter", "truncate", "create", "grant", "revoke")


def _is_single_write(statement: str, *, whole_table: bool = False) -> tuple[bool, str]:
    """Validate a single DML statement.

    The WHERE requirement is NOT a permission boundary — BOSS is trusted with the database. It
    is a foot-gun guard: `UPDATE code_task_runs SET status='failed'` is valid SQL that silently
    rewrites every row in the table, and a model that omits a WHERE has made a typing mistake,
    not a decision. `whole_table=true` is how it says the omission was deliberate.
    """
    text = " ".join((statement or "").split())
    lowered = text.lower()
    if not lowered:
        return False, "empty statement"
    if ";" in text.rstrip(";"):
        return False, "multiple statements are not allowed; send one at a time"
    verb = lowered.split(" ", 1)[0]
    if verb in _DDL_VERBS:
        return False, (
            f"{verb.upper()} is a schema change, not CRUD. Schema changes belong in a migration "
            "so they are reviewed and reversible.")
    if verb not in _WRITE_VERBS:
        return False, (
            f"expected INSERT, UPDATE or DELETE; got {verb.upper()}. Use query_db for reads.")
    if verb in ("update", "delete") and " where " not in lowered and not whole_table:
        return False, (
            f"{verb.upper()} without a WHERE clause would change EVERY row. Add a WHERE, or "
            "pass whole_table=true if that is genuinely intended.")
    return True, ""



#: Values that mean "I did not look this up". A proposal carrying one produces a button that
#: fails the moment the operator trusts it.
_PLACEHOLDER_MARKERS = (
    "placeholder", "example.com", "your-", "todo", "tbd", "xxx", "<", "...",
    "changeme", "some-", "my-app", "foo", "bar", "abc123",
    # Seen live in a proposal's args: repository="owner/name".
    "owner/name", "owner/repo", "user/repo", "org/repo",
)


def _placeholder_argument(args: dict) -> str:
    """The first argument that looks invented, described for the model, or ''."""
    for key, value in (args or {}).items():
        if not isinstance(value, str):
            continue
        low = value.strip().lower()
        if not low:
            continue
        if any(marker in low for marker in _PLACEHOLDER_MARKERS):
            return f"{key}={value!r}"
    return ""

def _register_operations_tools(reg: ToolRegistry) -> None:
    """Operational writes: BOSS running BOSS.

    Everything Ask BOSS could do was READ. So when a PRD meeting sat queued for two hours
    behind benchmark jobs boosted to priority_score 9000, BOSS could describe the problem in
    full detail and could not touch it — an operator had to reach in from outside and run the
    fix by hand. A control plane that can only narrate its own queue is not operating anything.

    The boundary is deliberate and narrow: these change OPERATIONAL STATE — job priority, a
    wedged job, an orphaned run. They do not change BOSS's own code. That still goes through
    the governed coding pipeline, with a PR and a review, because a system editing its own
    source without a diff is a different and much larger promise.

    Every one of these is a targeted UPDATE by id, and each records who asked and why.
    """

    @reg.tool(
        "cancel_job",
        "Cancel a queued job so the worker skips it. Pass force=true to stop one that is "
        "already RUNNING — use that when a wedged job is holding a worker slot and blocking "
        "work someone is waiting on. Cancelling a job also closes its code-task run, so "
        "nothing is left reported as in-flight. Prefer set_job_priority when the job is not "
        "wedged and only needs to yield.",
        _obj({"job_id": {"type": "string"},
              "reason": {"type": "string", "description": "why — recorded on the job"},
              "force": {"type": "boolean", "description": "stop it even if running"}},
             required=["job_id", "reason"]),
        kind="write",
    )
    def cancel_job(ctx, *, project_id, actor, args):
        from ..services.jobs import JobLeaseService
        with ctx.begin() as c:
            return JobLeaseService(ctx.settings).cancel_queued_job(
                c, str(args["job_id"]), reason=str(args["reason"]),
                actor=actor, force=bool(args.get("force")))

    @reg.tool(
        "write_db",
        "Create, update or delete ONE row when no dedicated tool covers it. This is the reason "
        "you almost never need to tell an operator that something 'requires a human' or is a "
        "'custom admin action'. If a record is missing or has the wrong value and you can name "
        "the table, you can fix it YOURSELF with this. Examples that have no dedicated tool and "
        "ARE done with write_db: creating a production deployment_targets row for an app that "
        "only has staging, clearing a paused flag, correcting a wrong domain or URL, fixing a "
        "status that is stuck. Before you say a change needs someone else, check whether it is "
        "one row in one table -- if it is, do it. One statement, always WHERE-qualified unless "
        "whole_table is set. Never invent a value you write: read it first, or ask. "
        "PREFER a structured tool when one exists: raw SQL emits no domain events and updates "
        "no related rows, so cancel_job is better than UPDATE jobs SET status. This is the "
        "fallback for what those tools do not cover, not a shortcut around them.",
        _obj({"sql": {"type": "string",
                      "description": "a single INSERT, UPDATE or DELETE statement"},
              "reason": {"type": "string", "description": "why — recorded in the event stream"},
              "whole_table": {"type": "boolean",
                              "description": "confirm an intentionally unqualified UPDATE/DELETE"}},
             required=["sql", "reason"]),
        kind="write",
    )
    def write_db(ctx, *, project_id, actor, args):
        from ..ports import sql as _s
        from ..services.core import emit_event

        statement = (args.get("sql") or "").strip().rstrip(";")
        ok, why = _is_single_write(statement, whole_table=bool(args.get("whole_table")))
        if not ok:
            return {"ok": False, "error": f"rejected: {why}"}
        with ctx.begin() as c:
            result = c.execute(_s(statement))
            changed = getattr(result, "rowcount", None)
            emit_event(c, project_id, "db.write", actor, "database", "sql",
                       {"sql": statement[:2000], "rows": changed,
                        "reason": str(args.get("reason") or "")[:500]})
        return {"ok": True, "rows_affected": changed, "sql": statement[:2000],
                "note": ("A raw write emits no domain events and updates no related rows. "
                         "Check whether this change needs a companion update.")}

    @reg.tool(
        "redeploy",
        "Ship the SAME build to its target again, for an app whose deployment failed. Use this "
        "when the operator says fix/retry the deployment: transfer and docker steps are "
        "idempotent, so re-running is safe. Takes the code-task run id (crun_...). Reports what "
        "the health check returned and how long it waited, so a second failure is diagnosable "
        "rather than a repeat of the same sentence. This does NOT rebuild the app or spend "
        "model budget — it re-ships a build that already exists.",
        _obj({"run_id": {"type": "string", "description": "the crun_... whose deployment failed"},
              "reason": {"type": "string", "description": "why — recorded on the deployment"}},
             required=["run_id"]),
        kind="write",
    )
    def redeploy(ctx, *, project_id, actor, args):
        from ..services.shepherd_service import _act_on_deployment
        outcome = _act_on_deployment(
            ctx.engine, ctx.settings, str(args["run_id"]), "redeploy_deployment", actor)
        return {"ok": not outcome.startswith(("no_action", "failed")),
                "run_id": args["run_id"], "outcome": outcome,
                "reason": args.get("reason") or "operator asked BOSS to retry the deployment"}

    @reg.tool(
        "deploy_app",
        "Deploy a SUCCESSFUL build to an environment for the FIRST time. This is the tool to "
        "use when an operator says 'deploy it', 'ship it', or 'deploy to staging/live' and the "
        "app has never been deployed — redeploy only re-ships a deployment that already "
        "exists, so it cannot help there. Takes the crun_ of a completed run and the "
        "environment name. It will REFUSE if the run did not succeed, or if no active target "
        "exists for that environment, and it tells you which environments DO exist so you can "
        "offer the operator a real choice. It never creates infrastructure.",
        _obj({"run_id": {"type": "string", "description": "crun_ of a COMPLETED build"},
              "environment": {"type": "string",
                              "description": "staging | production — must already have a target"},
              "target_id": {"type": "string", "description": "optional exact deptgt_ to use"}},
             required=["run_id", "environment"]),
        kind="write",
    )
    def deploy_app(ctx, *, project_id, actor, args):
        from ..services.delivery_pipeline import deploy_build
        return deploy_build(
            ctx.engine, ctx.settings,
            project_id=project_id,
            run_id=str(args["run_id"]),
            environment=str(args.get("environment") or "staging"),
            target_id=(str(args["target_id"]) if args.get("target_id") else None),
            actor=actor,
        )

    @reg.tool(
        "preflight_deployment_target",
        "Check the machine an app deploys to is reachable and healthy, WITHOUT shipping "
        "anything. Read-only. Run this first when a deployment failed with a connection or "
        "health error, so a redeploy is not aimed at a host that is down.",
        _obj({"run_id": {"type": "string"}}, required=["run_id"]),
        kind="read",
    )
    def preflight_deployment_target(ctx, *, project_id, actor, args):
        from ..services.shepherd_service import _act_on_deployment
        return {"run_id": args["run_id"],
                "result": _act_on_deployment(
                    ctx.engine, ctx.settings, str(args["run_id"]), "preflight_target", actor)}

    @reg.tool(
        "propose_action",
        "Offer the operator an action as a BUTTON instead of performing it. Nothing runs until "
        "they click. "
        "Use this when the next step COSTS something or is hard to undo — deploying, starting a "
        "build, writing to the database — and they have not explicitly asked for that exact "
        "thing. Use it ALSO to offer a cheaper alternative beside an expensive one: 'redeploy' "
        "and 'just clear the stale URL' are both valid answers to 'this link is wrong', and the "
        "operator should get to pick rather than be handed the expensive one. "
        "Do NOT use it to avoid work they DID ask for: 'redeploy it' means call redeploy. "
        "Propose several when several are reasonable; each becomes its own button.",
        _obj({"tool": {"type": "string",
                       "description": "the tool to run when they click, e.g. 'redeploy'"},
              "args": {"type": "object",
                       "description": "exact arguments for that tool"},
              "label": {"type": "string",
                        "description": "the button text, imperative and short, e.g. "
                                       "'Redeploy Living Colors'"},
              "why": {"type": "string",
                      "description": "one line: what it will do and what it costs"}},
             required=["tool", "label"]),
        kind="read",
    )
    def propose_action(ctx, *, project_id, actor, args):
        """Records an offer. Executing it is the operator's click, not this call."""
        name = ToolRegistry.canonical_tool_name(str(args.get("tool") or ""))
        if name not in _DISCUSS_OPERATIONS_TOOLS:
            return {"ok": False,
                    "error": f"cannot propose {name!r}: not an action BOSS can run",
                    "available": sorted(_DISCUSS_OPERATIONS_TOOLS)}
        call_args = args.get("args") or {}
        placeholder = _placeholder_argument(call_args)
        if placeholder:
            # A BUTTON WITH AN INVENTED ARGUMENT IS A BUTTON THAT FAILS ON CLICK.
            #
            # Observed: proposing redeploy with run_id="crun_placeholder". The offer looked
            # right, and the operator would have discovered the fabrication by pressing it.
            # Refusing here sends the model back to look the value up, which is the work it
            # skipped.
            return {"ok": False,
                    "error": (f"{placeholder} looks invented. Look up the real value first — "
                              "query_db or list_code_tasks — then propose with it. An action "
                              "the operator cannot actually run is worse than no offer."),
                    "proposed": False}
        return {"ok": True, "proposed": True, "tool": name,
                "args": call_args, "label": str(args.get("label") or name),
                "why": str(args.get("why") or ""),
                "note": "Offered to the operator as a button. NOT executed."}

    @reg.tool(
        "inspect_self",
        "Look at the container BOSS is running in: CPU load and throttling, memory use and "
        "OOM-kill count, free disk, how much space preview workdirs are holding, and the "
        "heaviest processes. Use this FIRST whenever a process died without an explanation "
        "(`exited -9`, `exited 137`, `SIGKILL`) — the OOM count settles in one number what "
        "otherwise takes an hour of guessing — and whenever the API is slow while a build is "
        "running, because load-per-core says whether anything is actually contending. "
        "Read-only; it touches no docker socket.",
        _obj({}),
        kind="read",
    )
    def inspect_self(ctx, *, project_id, actor, args):
        from ..services.self_observability import self_snapshot
        return self_snapshot()

    @reg.tool(
        "set_job_priority",
        "Move a job between priority bands. LOWER runs sooner. Bands: 'urgent' (delay costs "
        "money or safety), 'interactive' (a person is watching), 'requested' (a person asked "
        "and walked away), 'background' (the system decided this itself), 'bulk' (benchmarks "
        "and canaries — yields to everything). Also clears priority_score, which overrides "
        "the bands entirely and is how benchmark work once outranked every meeting in the "
        "system.",
        _obj({"job_id": {"type": "string"},
              "band": {"type": "string",
                       "enum": ["urgent", "interactive", "requested", "background", "bulk"]},
              "reason": {"type": "string"}},
             required=["job_id", "band", "reason"]),
        kind="write",
    )
    def set_job_priority(ctx, *, project_id, actor, args):
        from ..ports import sql as _s
        from ..services.jobs import (
            JOB_PRIORITY_BACKGROUND, JOB_PRIORITY_BULK, JOB_PRIORITY_INTERACTIVE,
            JOB_PRIORITY_REQUESTED, JOB_PRIORITY_URGENT,
        )
        bands = {"urgent": JOB_PRIORITY_URGENT, "interactive": JOB_PRIORITY_INTERACTIVE,
                 "requested": JOB_PRIORITY_REQUESTED, "background": JOB_PRIORITY_BACKGROUND,
                 "bulk": JOB_PRIORITY_BULK}
        band = str(args["band"])
        with ctx.begin() as c:
            n = c.execute(_s(
                "UPDATE jobs SET priority = :p, priority_score = NULL, updated_at = now() "
                "WHERE id = :i AND project_id = :proj"),
                {"p": bands[band], "i": str(args["job_id"]), "proj": project_id}).rowcount
        if not n:
            return {"ok": False, "error": "no such job in this project"}
        return {"ok": True, "job_id": args["job_id"], "band": band,
                "priority": bands[band], "priority_score": None,
                "reason": str(args["reason"]), "actor": actor}

    @reg.tool(
        "find_starved_work",
        "Find work that is QUEUED and not progressing, and what is ahead of it. This is the "
        "question no failure-triggered check can answer: starvation is the ABSENCE of "
        "progress, not an error, so nothing shows up as failed. Returns each waiting job with "
        "how long it has waited, plus whatever is currently holding a worker slot.",
        _obj({"minutes": {"type": "integer",
                          "description": "how long queued counts as starved (default 10)"}}),
    )
    def find_starved_work(ctx, *, project_id, actor, args):
        from ..ports import sql as _s
        threshold = int(args.get("minutes") or 10)
        with ctx.begin() as c:
            waiting = c.execute(_s(
                "SELECT id, kind, priority, priority_score, task_id, "
                "       EXTRACT(EPOCH FROM (now() - created_at))/60 AS waited_min "
                "FROM jobs WHERE project_id = :p AND status = 'queued' "
                "  AND run_after <= now() "
                "  AND created_at < now() - (:m || ' minutes')::interval "
                "ORDER BY priority ASC, created_at ASC LIMIT 25"),
                {"p": project_id, "m": threshold}).mappings().all()
            holding = c.execute(_s(
                "SELECT id, kind, priority, priority_score, "
                "       EXTRACT(EPOCH FROM (now() - updated_at))/60 AS running_min "
                "FROM jobs WHERE project_id = :p AND status IN ('running','leased') "
                "ORDER BY priority ASC LIMIT 25"), {"p": project_id}).mappings().all()
        return {
            "threshold_minutes": threshold,
            "starved": [{**dict(r), "waited_min": round(float(r["waited_min"]), 1)}
                        for r in waiting],
            "holding_a_slot": [{**dict(r), "running_min": round(float(r["running_min"]), 1)}
                               for r in holding],
            "how_to_read": (
                "claim_job orders by priority_score DESC, then priority ASC, then created_at. "
                "A job with a priority_score outranks EVERY band. If something in "
                "holding_a_slot outranks something in starved, that is the reason it waits."),
        }

    @reg.tool(
        "link_run_origin",
        "Record where a run came from, so the console can show its Research/Spec history. A "
        "run's origin is written when it is created and there was no way to repair it "
        "afterwards — a build restarted by hand loses the PRD that specified it, and the card "
        "then reports 'Spec: none' about work that has a spec sitting right there.",
        _obj({"run_id": {"type": "string"},
              "kind": {"type": "string", "enum": ["meeting", "discovery"]},
              "meeting_run_id": {"type": "string", "description": "the mrun_/drun_ to link to"},
              "task_id": {"type": "string"}},
             required=["run_id", "kind"]),
        kind="write",
    )
    def link_run_origin(ctx, *, project_id, actor, args):
        import json as _json

        from ..ports import sql as _s
        origin = {"kind": str(args["kind"])}
        for field in ("meeting_run_id", "task_id"):
            if args.get(field):
                origin[field] = str(args[field])
        with ctx.begin() as c:
            n = c.execute(_s(
                "UPDATE code_task_runs "
                "SET finalize_ctx = COALESCE(finalize_ctx, '{}'::jsonb) "
                "    || jsonb_build_object('origin', CAST(:o AS JSONB)), "
                "    updated_at = now() "
                "WHERE id = :i AND project_id = :p"),
                {"o": _json.dumps(origin), "i": str(args["run_id"]), "p": project_id}).rowcount
        if not n:
            return {"ok": False, "error": "no such run in this project"}
        return {"ok": True, "run_id": args["run_id"], "origin": origin, "actor": actor}

    @reg.tool(
        "consistency_report",
        "Every 'this should never be true' check in one pass, read-only. Counts rows that "
        "violate an invariant: runs claiming to work with no job that could advance them, "
        "previews stuck starting, runs whose phase says done but whose status does not, jobs "
        "queued far longer than anything is running, decisions pending past their deadline. "
        "Use this to answer 'what is inconsistent right now' instead of waiting for someone "
        "to notice a stuck card.",
        _obj({}),
    )
    def consistency_report(ctx, *, project_id, actor, args):
        from ..ports import sql as _s
        live_job = ("NOT EXISTS (SELECT 1 FROM jobs j WHERE j.payload->>'run_id' = r.id "
                    "AND j.status IN ('queued','running','leased'))")
        checks = {
            "code_task_runs_working_with_no_job": (
                f"SELECT count(*) FROM code_task_runs r WHERE r.project_id = :p "
                f"AND r.status IN ('running','repairing','queued') AND {live_job}"),
            "meeting_runs_working_with_no_job": (
                f"SELECT count(*) FROM meeting_runs r WHERE r.project_id = :p "
                f"AND r.status IN ('running','queued') AND {live_job}"),
            "discovery_runs_working_with_no_job": (
                f"SELECT count(*) FROM discovery_runs r WHERE r.project_id = :p "
                f"AND r.status = 'running' AND {live_job}"),
            "previews_stuck_starting": (
                "SELECT count(*) FROM app_previews WHERE project_id = :p "
                "AND status = 'starting' AND created_at < now() - interval '25 minutes'"),
            # "not terminal" is too wide: a run at phase='done' with status='blocked' is
            # waiting on a human decision, which is a legitimate resting state. Counting those
            # as inconsistent reported 12 violations that were nothing wrong — and a report
            # full of false positives is a report nobody reads. The real contradiction is a run
            # that claims to be WORKING while its phases are finished.
            "runs_done_but_still_claiming_to_work": (
                "SELECT count(*) FROM code_task_runs WHERE project_id = :p "
                "AND phase = 'done' AND status IN ('running','repairing','queued')"),
            "runs_done_and_blocked_on_a_decision": (
                "SELECT count(*) FROM code_task_runs WHERE project_id = :p "
                "AND phase = 'done' AND status = 'blocked'"),
            "jobs_queued_over_30_min": (
                "SELECT count(*) FROM jobs WHERE project_id = :p AND status = 'queued' "
                "AND run_after <= now() AND created_at < now() - interval '30 minutes'"),
            "decisions_pending_past_deadline": (
                "SELECT count(*) FROM decisions WHERE project_id = :p AND status = 'pending' "
                "AND deadline_at IS NOT NULL AND deadline_at < now()"),
            "jobs_with_a_priority_score_override": (
                "SELECT count(*) FROM jobs WHERE project_id = :p "
                "AND status IN ('queued','running','leased') AND priority_score IS NOT NULL"),
        }
        # Informational counts are reported but never mark the system unhealthy.
        informational = {"runs_done_and_blocked_on_a_decision"}
        out, failing = {}, []
        with ctx.begin() as c:
            for name, q in checks.items():
                try:
                    n = int(c.execute(_s(q), {"p": project_id}).scalar() or 0)
                except Exception as exc:  # noqa: BLE001 — a missing table is data, not a crash
                    out[name] = f"unavailable ({type(exc).__name__})"
                    continue
                out[name] = n
                if n and name not in informational:
                    failing.append(name)
        return {"violations": out, "failing_checks": failing, "clean": not failing,
                "how_to_read": ("every count should be 0. A non-zero value is a row the system "
                                "believes is in progress when nothing can advance it, which is "
                                "what a stuck card in the console actually is.")}

    @reg.tool(
        "close_orphaned_runs",
        "Close code-task runs that claim to be running but have no live job — work that will "
        "never start and is reported forever as in flight. Reports what it would close when "
        "dry_run is true.",
        _obj({"dry_run": {"type": "boolean", "description": "default true"}}),
        kind="write",
    )
    def close_orphaned_runs(ctx, *, project_id, actor, args):
        from ..ports import sql as _s
        dry = args.get("dry_run")
        dry = True if dry is None else bool(dry)
        find = (
            "SELECT r.id, r.title, r.phase FROM code_task_runs r "
            "WHERE r.project_id = :p AND r.status IN ('running','repairing') "
            "  AND NOT EXISTS (SELECT 1 FROM jobs j "
            "                  WHERE j.payload->>'run_id' = r.id "
            "                    AND j.status IN ('queued','running','leased')) "
            "LIMIT 200")
        with ctx.begin() as c:
            rows = c.execute(_s(find), {"p": project_id}).mappings().all()
            found = [dict(r) for r in rows]
            if not dry and found:
                c.execute(_s(
                    "UPDATE code_task_runs SET status = 'cancelled', "
                    "error = COALESCE(error, :e), updated_at = now() "
                    "WHERE id = ANY(:ids)"),
                    {"ids": [r["id"] for r in found],
                     "e": f"closed as orphaned by {actor}: no live job"})
        return {"dry_run": dry, "count": len(found), "runs": found[:50],
                "closed": 0 if dry else len(found)}


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
        "read_spec",
        # DESCRIBE THE TOOL. DO NOT INSTRUCT THE ASSISTANT.
        #
        # This description was first written as directives — "Call it FIRST", "read the PRD",
        # "there is no other way". The CLI backend renders every tool description into one
        # prompt, so the model read those as orders addressed to it and reached for its own
        # (blocked) Read tool, which killed the turn: every tool-using question in the console
        # started failing the moment this tool was registered. Steering belongs in the system
        # prompt; a description says what the tool returns.
        "Returns an app's specification and how it was delivered: the PRD or brief, the "
        "acceptance criteria, the test commands, the implementation and review summaries, the "
        "files changed and the pull request. Covers questions about an app's intent, scope, "
        "coverage gaps, or what to build next. Accepts a code task run id, a meeting run id, "
        "or an app name.",
        _obj({"run_id": {"type": "string",
                         "description": "code task run id (crun_...) or meeting run id (mrun_...); "
                                        "omit to use the run this conversation is scoped to"},
              "app": {"type": "string",
                      "description": "app/run title instead of an id, e.g. 'UnitFlip'"}}),
    )
    def read_spec(ctx, *, project_id, actor, args):
        """The spec, from wherever this app's spec actually landed.

        BOSS could read its own source, its logs, its hosts and arbitrary SQL, and could not
        read the one document that says what an app was FOR. Asked "what should we build next
        for UnitFlip", it chained tools for two minutes and answered that "the PR description,
        diff, and any related PRD documents are not present in the searchable data" — while the
        PRD sat in `meeting_runs.state`, openable in the console, two clicks from the question.

        A spec reaches an app by three different routes and no single table holds all three:
          * built from a meeting  -> meeting_runs.state->>'prd_markdown', joined on task_id
          * published as a doc    -> artifacts, joined on task_id
          * a direct code task    -> code_task_runs.spec.instructions (for a meeting-born task
                                     this IS the PRD, because the directive is set from it)
        So look in all three and say which one answered. Returning "not found" when the
        document exists on a route you did not check is the failure being fixed here.
        """
        run_id = (args.get("run_id") or "").strip()
        app = (args.get("app") or "").strip()
        with ctx.begin() as c:
            row = None
            if run_id.startswith("mrun_"):
                m = c.execute(
                    _text("SELECT id, task_id, directive, state FROM meeting_runs "
                          "WHERE id = :r AND project_id = :p"),
                    {"r": run_id, "p": project_id},
                ).mappings().first()
                if m is None:
                    return {"ok": False, "error": f"no meeting run {run_id}"}
                st = m["state"] or {}
                if isinstance(st, str):
                    st = json.loads(st or "{}")
                return _json_safe({
                    "ok": True, "source": "meeting_runs.state.prd_markdown",
                    "meeting_run_id": m["id"], "task_id": m["task_id"],
                    "directive": m["directive"],
                    "prd_markdown": st.get("prd_markdown"),
                    "synthesis": st.get("synthesis"),
                })
            if run_id:
                row = c.execute(
                    _text("SELECT id, task_id, title, status, spec, state, pull_request_url "
                          "FROM code_task_runs WHERE id = :r AND project_id = :p"),
                    {"r": run_id, "p": project_id},
                ).mappings().first()
                if row is None:
                    return {"ok": False, "error": f"no code task run {run_id}"}
            else:
                # By name, newest first. An app is usually rebuilt several times and the
                # operator means the latest one.
                like = f"%{app}%" if app else "%"
                row = c.execute(
                    _text("SELECT id, task_id, title, status, spec, state, pull_request_url "
                          "FROM code_task_runs WHERE project_id = :p AND title ILIKE :t "
                          "ORDER BY created_at DESC LIMIT 1"),
                    {"p": project_id, "t": like},
                ).mappings().first()
                if row is None:
                    return {"ok": False, "error": f"no app matching {app!r}"}

            run = dict(row)
            spec = run.get("spec") or {}
            state = run.get("state") or {}
            if isinstance(spec, str):
                spec = json.loads(spec or "{}")
            if isinstance(state, str):
                state = json.loads(state or "{}")

            prd, source = None, "code_task_runs.spec.instructions"
            if run.get("task_id"):
                m = c.execute(
                    _text("SELECT id, state FROM meeting_runs WHERE task_id = :t "
                          "AND project_id = :p ORDER BY created_at DESC LIMIT 1"),
                    {"t": run["task_id"], "p": project_id},
                ).mappings().first()
                if m is not None:
                    st = m["state"] or {}
                    if isinstance(st, str):
                        st = json.loads(st or "{}")
                    if st.get("prd_markdown"):
                        prd = st["prd_markdown"]
                        source = f"meeting_runs.state.prd_markdown ({m['id']})"
                if prd is None:
                    a = c.execute(
                        _text("SELECT title, content FROM artifacts WHERE task_id = :t "
                              "AND project_id = :p AND title ILIKE 'PRD%' "
                              "ORDER BY created_at DESC LIMIT 1"),
                        {"t": run["task_id"], "p": project_id},
                    ).mappings().first()
                    if a is not None:
                        content = a["content"] or {}
                        if isinstance(content, str):
                            content = json.loads(content or "{}")
                        prd = content.get("markdown") or content.get("body")
                        if prd:
                            source = f"artifacts ({a['title']})"
        if prd is None:
            prd = spec.get("instructions")

        return _json_safe({
            "ok": True,
            "source": source,
            "run_id": run["id"],
            "app": run["title"],
            "status": run["status"],
            "task_id": run.get("task_id"),
            "pull_request_url": run.get("pull_request_url"),
            # The spec, in the words it was written in.
            "prd_or_brief": prd,
            "acceptance_criteria": spec.get("acceptance_criteria"),
            "test_commands": spec.get("test_commands"),
            # What was actually built against it. The gap between these two IS the answer to
            # "what did we miss" and "what should we build next".
            "implementation_summary": state.get("impl_summary") or state.get("summary"),
            "review_summary": state.get("review_summary"),
            "files_changed": state.get("impl_files") or state.get("diff_summary"),
            "tests_passed": state.get("tests_passed"),
        })

    @reg.tool(
        "explain_run",
        "WHY a code-task run took as long as it did, cost what it cost, or ended how it "
        "ended. Returns the wall-clock breakdown per phase (the phase that dominated is "
        "almost always the answer), cost per stage, repair attempts, turns and the test "
        "verdict. Use this for 'why did this take 152m', 'what went wrong', 'why so "
        "expensive'.",
        _obj({"run_id": {"type": "string"}}, ["run_id"]),
    )
    def explain_run(ctx, *, project_id, actor, args):
        """The wall-clock answer, which `get_run_activity` cannot give.

        `get_run_activity` reads `llm_calls` — per-call latency, tokens and cost. That is the
        right source for "which model call was slow", but a run's DURATION is not the sum of
        its LLM calls: it also contains sandbox provisioning, test execution, and retries of a
        whole phase. The engine already records exactly this in `finalize_ctx.phase_timings`,
        and nothing surfaced it — so an operator asking "why did this take 152 minutes" got
        either a guess or a spend-cap error, while the run itself held the answer
        (write_tests: 127.5 min, 84% of the total) the whole time.
        """
        run_id = args["run_id"]
        with ctx.begin() as c:
            row = c.execute(
                _text("SELECT id, status, phase, title, error, state, finalize_ctx, "
                      "       created_at, updated_at "
                      "FROM code_task_runs WHERE id = :rid AND project_id = :p"),
                {"rid": run_id, "p": project_id},
            ).mappings().first()
        if row is None:
            return {"ok": False, "error": f"no code task run {run_id}"}

        run = dict(row)
        state = run.get("state") or {}
        fctx = run.get("finalize_ctx") or {}
        if isinstance(state, str):
            state = json.loads(state or "{}")
        if isinstance(fctx, str):
            fctx = json.loads(fctx or "{}")

        # Aggregate per phase: a phase that ran twice (a restart) is the thing worth seeing,
        # so keep the run count rather than only the total.
        timings = fctx.get("phase_timings") or []
        by_phase: dict = {}
        for item in timings:
            if not isinstance(item, dict):
                continue
            name = str(item.get("phase") or "unknown")
            entry = by_phase.setdefault(name, {"ms": 0, "runs": 0})
            entry["ms"] += int(item.get("ms") or 0)
            entry["runs"] += 1
        total_ms = sum(v["ms"] for v in by_phase.values())
        phases = [
            {"phase": k, "minutes": round(v["ms"] / 60000, 1), "runs": v["runs"],
             "share_pct": round(100 * v["ms"] / total_ms, 1) if total_ms else 0.0}
            for k, v in sorted(by_phase.items(), key=lambda kv: -kv[1]["ms"])
        ]

        stage_costs = state.get("stage_costs") or {}
        costs = [{"stage": k, "usd": round(int(v or 0) / 1_000_000, 4)}
                 for k, v in sorted(stage_costs.items(), key=lambda kv: -int(kv[1] or 0))]

        tests = state.get("tests") or []
        failed_tests = [t for t in tests if isinstance(t, dict) and not t.get("ok")]

        # State the conclusion rather than leaving the reader to compute it from the table.
        dominant = phases[0] if phases else None
        headline = None
        if dominant and total_ms:
            headline = (f"{dominant['phase']} took {dominant['minutes']} min "
                        f"({dominant['share_pct']}% of {round(total_ms/60000)} min total)"
                        + (f", and ran {dominant['runs']} times" if dominant["runs"] > 1 else ""))

        return {
            "run_id": run_id,
            "status": run.get("status"),
            "phase": run.get("phase"),
            "error": run.get("error"),
            "headline": headline,
            "total_minutes": round(total_ms / 60000, 1) if total_ms else None,
            "phases": phases,
            "total_cost_usd": round(int(state.get("cost_usd_micros") or 0) / 1_000_000, 4),
            "cost_by_stage": costs,
            "turns": state.get("turns"),
            "repair_attempts": state.get("repair_attempts"),
            "repair_log": state.get("repair_log") or [],
            "steps_run": state.get("steps_run") or [],
            "convergence_rounds": state.get("inrun_convergence_rounds"),
            "tests_passed": state.get("tests_passed"),
            "test_exit_code": state.get("test_exit_code"),
            "failed_tests": failed_tests,
            "note": ("Wall clock per phase, which is NOT the sum of LLM-call latency — it "
                     "includes provisioning, test execution and whole-phase retries. Use "
                     "get_run_activity for per-model-call detail."),
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
            # THE TOTAL IS NOT THE SUM OF THE PAGE.
            #
            # total_cost_usd used to be sum(runs), and runs is LIMIT-ed to the most recent 25.
            # So a project with more than 25 recent runs reported "total spend" that was really
            # "spend of the last 25 runs" -- silently, with no marker. Asked five times in a row
            # this answered $258.44, $19.56, 0.9141 and 40.2165, because the model also picks
            # `days` freely. A money figure that is not reproducible is worse than no figure.
            #
            # The total is now its own aggregate over the whole window, independent of limit.
            totals = c.execute(
                _text(
                    "SELECT COALESCE(sum(cost_usd_micros), 0) AS micros, "
                    "       COALESCE(sum(total_tokens), 0)   AS tokens, "
                    "       count(*)                          AS calls, "
                    "       count(DISTINCT code_task_run_id)  AS runs "
                    "FROM llm_calls "
                    "WHERE project_id = :p "
                    "  AND created_at > now() - make_interval(days => :d)"
                ),
                {"p": pid, "d": days},
            ).mappings().first() or {}
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
        total_runs = int(totals.get("runs") or 0)
        return {
            "project_id": pid,
            "window_days": days,
            # Say the window in words too: the model was labelling a 7-day figure "this month".
            "window_description": f"the last {days} days (not a calendar month)",
            "total_cost_usd": round(int(totals.get("micros") or 0) / 1_000_000, 4),
            "total_tokens": int(totals.get("tokens") or 0),
            "total_llm_calls": int(totals.get("calls") or 0),
            "runs_in_window": total_runs,
            # Explicit, so a truncated list cannot be mistaken for the whole ledger.
            "runs_shown": len(runs),
            "runs_truncated": total_runs > len(runs),
            "note": (
                "total_cost_usd is the WHOLE window and is authoritative. `runs` is only the "
                f"{len(runs)} most recent of {total_runs} — never sum `runs` and call it the "
                "total. Report the window: this is the last "
                f"{days} days, which is not the same as a calendar month."
            ),
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
# ── Investigation: read the source and the logs, not just the database ────────
#
# Ask BOSS is meant to work like an operator with console access, not a menu of
# canned reports. A fixed tool list can only answer questions somebody predicted:
# every gap becomes a new tool, and the list never catches up with what an
# operator actually wants to ask. These three plus `query_db` are the primitives
# that make the difference — read the schema-free truth, read the code, read what
# the process actually logged.
#
# SCOPE: single-tenant. `query_db` is unrestricted SELECT and these read the
# deployed source tree. If BOSS ever serves more than one tenant from one
# process, all four need a fence (Postgres RLS keyed on the authorised project is
# the mechanism the schema is already shaped for — 233 of 244 tables carry
# project_id). Stated here so that decision is made deliberately rather than
# discovered.

# Where the deployed source lives. /app in the container, the repo root in
# development — resolved once so a tool cannot wander outside it.
_SOURCE_ROOT = _os.environ.get("BOSS_SOURCE_ROOT") or (
    "/app" if _os.path.isdir("/app/engine") else
    _os.path.abspath(_os.path.join(_os.path.dirname(__file__), "..", "..", ".."))
)
# Never worth reading, always huge, and they bury the answer.
_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".next", ".venv", "dist",
              "build", ".mypy_cache", ".pytest_cache", "browsers", "pw-browsers"}
_MAX_FILE_BYTES = 400_000


# Where cloned customer repos are cached. Read-only working copies, refreshed on
# demand — BOSS already clones this repo to build and to deploy; this reuses the
# same credential path rather than inventing a second way to reach the code.
_REPO_CACHE = _os.environ.get("BOSS_REPO_CACHE") or "/tmp/boss-repo-cache"


def _repo_root(settings: Any, repo: str) -> tuple[_Optional[str], _Optional[str]]:
    """The local path for `repo`, cloning it if we do not have it yet.

    `repo` is "boss" for BOSS's own source, or "owner/name" (or bare "name", which
    resolves against the configured target owner) for a customer's application. The
    honest answer to "why does my app do X" is in the app's code, and until now no
    tool could open it — Ask BOSS could describe BOSS and only guess about the thing
    BOSS had built.
    """
    name = (repo or "boss").strip()
    if name in ("boss", "self", "platform", ""):
        return _SOURCE_ROOT, None

    owner, _, short = name.rpartition("/")
    owner = owner or (getattr(settings, "target_github_owner", "") or "")
    short = short or (getattr(settings, "target_github_repo", "") or "")
    if not owner or not short:
        return None, f"cannot resolve repo {repo!r} — no owner configured"

    dest = _os.path.join(_REPO_CACHE, f"{owner}__{short}")
    try:
        _os.makedirs(_REPO_CACHE, exist_ok=True)
        from ..services.repo_host import clone_url_for

        url = clone_url_for(getattr(settings, "target_repo_host", "github") or "github",
                            owner, short, getattr(settings, "github_token", "") or "")
        if _os.path.isdir(_os.path.join(dest, ".git")):
            # Refresh, but never fail the read because the network is down — a slightly
            # stale checkout answers most questions; no checkout answers none.
            _subprocess.run(["git", "-C", dest, "fetch", "--depth", "1", "origin"],
                            capture_output=True, timeout=120)
            _subprocess.run(["git", "-C", dest, "reset", "--hard", "origin/HEAD"],
                            capture_output=True, timeout=60)
        else:
            out = _subprocess.run(["git", "clone", "--depth", "1", url, dest],
                                  capture_output=True, text=True, timeout=300)
            if out.returncode != 0:
                return None, f"clone failed: {(out.stderr or '')[-200:]}"
        return dest, None
    except FileNotFoundError:
        return None, "git is not installed in this environment"
    except Exception as exc:  # noqa: BLE001 — a read tool must not raise
        return None, f"{type(exc).__name__}: {exc}"[:200]


def _safe_path(rel: str, root: _Optional[str] = None) -> _Optional[str]:
    """Resolve `rel` under `root` (default: BOSS's own source), or None if it escapes.

    Escape check is on the RESOLVED path, so `../../etc/passwd` and a symlink out
    are both caught — a prefix check on the raw string is not the same test.
    """
    base = root or _SOURCE_ROOT
    candidate = _os.path.realpath(_os.path.join(base, (rel or "").lstrip("/")))
    real = _os.path.realpath(base)
    return candidate if candidate == real or candidate.startswith(real + _os.sep) else None


def _register_investigation_tools(reg: ToolRegistry) -> None:
    @reg.tool(
        "search_code",
        "Search SOURCE for a regex — BOSS's own platform code (repo='boss', the default, "
        "including infra/) or a customer application (repo='owner/name', or a bare repo "
        "name for the configured target). Answers 'where is this implemented', 'what "
        "raises this error', 'why does my app do that'. Returns file:line. Prefer this "
        "over guessing at behaviour.",
        _obj({"pattern": {"type": "string", "description": "python regex"},
              "repo": {"type": "string",
                       "description": "'boss' for the platform, or 'owner/name' for an app"},
              "path": {"type": "string", "description": "optional subdir, e.g. engine/"},
              "glob": {"type": "string", "description": "optional filename filter, e.g. *.py"},
              "limit": _LIMIT},
             ["pattern"]),
    )
    def search_code(ctx, *, project_id, actor, args):
        import fnmatch as _fn

        try:
            rx = re.compile(args["pattern"])
        except re.error as exc:
            return {"ok": False, "error": f"bad regex: {exc}"}
        root, err = _repo_root(ctx.settings, args.get("repo") or "boss")
        if root is None:
            return {"ok": False, "error": err}
        base = _safe_path(args.get("path") or "", root)
        if base is None:
            return {"ok": False, "error": "path escapes the source root"}
        glob = args.get("glob")
        limit = int(args.get("limit") or 40)

        hits: list[dict] = []
        for dirpath, dirnames, filenames in _os.walk(base):
            dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
            for fname in filenames:
                if glob and not _fn.fnmatch(fname, glob):
                    continue
                full = _os.path.join(dirpath, fname)
                try:
                    if _os.path.getsize(full) > _MAX_FILE_BYTES:
                        continue
                    with open(full, "r", encoding="utf-8", errors="ignore") as fh:
                        for n, line in enumerate(fh, 1):
                            if rx.search(line):
                                hits.append({
                                    "file": _os.path.relpath(full, root),
                                    "line": n, "text": line.rstrip()[:300],
                                })
                                if len(hits) >= limit:
                                    return {"matches": hits, "truncated": True, "root": root}
                except (OSError, UnicodeDecodeError):
                    continue
        return {"matches": hits, "truncated": False, "root": root}

    @reg.tool(
        "read_code",
        "Read a file from source — BOSS's platform code (repo='boss', default) or a "
        "customer app (repo='owner/name'), optionally a line range. Use after "
        "search_code to read the actual implementation instead of describing what it "
        "probably does.",
        _obj({"file": {"type": "string", "description": "path relative to the repo root"},
              "repo": {"type": "string", "description": "'boss', or 'owner/name' for an app"},
              "start": {"type": "integer", "minimum": 1},
              "end": {"type": "integer", "minimum": 1}},
             ["file"]),
    )
    def read_code(ctx, *, project_id, actor, args):
        root, err = _repo_root(ctx.settings, args.get("repo") or "boss")
        if root is None:
            return {"ok": False, "error": err}
        path = _safe_path(args["file"], root)
        if path is None or not _os.path.isfile(path):
            return {"ok": False, "error": f"no such file under the source root: {args['file']}"}
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as fh:
                lines = fh.readlines()
        except OSError as exc:
            return {"ok": False, "error": str(exc)}
        total = len(lines)
        start = max(1, int(args.get("start") or 1))
        end = min(total, int(args.get("end") or min(total, start + 399)))
        body = "".join(lines[start - 1:end])
        return {"file": args["file"], "total_lines": total, "start": start, "end": end,
                "truncated": end < total, "content": body[:_MAX_FILE_BYTES]}

    @reg.tool(
        "read_logs",
        "Recent log lines from a BOSS service (api | worker | web), newest last, "
        "optionally filtered by a regex. The answer to 'what actually happened' when "
        "the database only records the outcome.",
        _obj({"service": {"type": "string", "enum": ["api", "worker", "web"]},
              "pattern": {"type": "string", "description": "optional regex filter"},
              "limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 120}}),
    )
    def read_logs(ctx, *, project_id, actor, args):
        service = (args.get("service") or "api").lower()
        limit = int(args.get("limit") or 120)
        rx = None
        if args.get("pattern"):
            try:
                rx = re.compile(args["pattern"])
            except re.error as exc:
                return {"ok": False, "error": f"bad regex: {exc}"}

        # Docker keeps container stdout on disk; a local run writes .local-logs/.
        # Try both rather than assuming one deployment shape.
        candidates = [
            _os.environ.get(f"BOSS_{service.upper()}_LOG"),
            _os.path.join(_SOURCE_ROOT, ".local-logs", f"{service}.log"),
            f"/var/log/boss/{service}.log",
        ]
        for path in [c for c in candidates if c]:
            if not _os.path.isfile(path):
                continue
            try:
                with open(path, "r", encoding="utf-8", errors="ignore") as fh:
                    lines = fh.readlines()
            except OSError:
                continue
            if rx is not None:
                lines = [ln for ln in lines if rx.search(ln)]
            tail = [ln.rstrip()[:500] for ln in lines[-limit:]]
            return {"service": service, "source": path, "lines": len(tail), "log": tail}
        return {"ok": False, "service": service,
                "error": ("no log file readable from this process. In a container the service "
                          "logs to stdout — read them with `docker logs`, or set "
                          f"BOSS_{service.upper()}_LOG to a path.")}


    @reg.tool(
        "describe_schema",
        "The database's own shape: tables, columns and types. query_db can read ANY "
        "table, but only if you know it exists — 244 tables and 68k events are useless "
        "to someone guessing names. Call this first when a question has no dedicated "
        "tool, then write the SELECT.",
        _obj({"table": {"type": "string", "description": "optional: one table's columns"},
              "like": {"type": "string", "description": "optional name filter, e.g. deployment"}}),
    )
    def describe_schema(ctx, *, project_id, actor, args):
        table = (args.get("table") or "").strip()
        like = (args.get("like") or "").strip()
        with ctx.begin() as c:
            if table:
                rows = c.execute(_text(
                    "SELECT column_name, data_type, is_nullable "
                    "FROM information_schema.columns "
                    "WHERE table_schema='public' AND table_name = :t "
                    "ORDER BY ordinal_position"), {"t": table}).mappings().all()
                if not rows:
                    return {"ok": False, "error": f"no table {table!r}"}
                return {"table": table,
                        "columns": [f"{r['column_name']} {r['data_type']}"
                                    f"{'' if r['is_nullable'] == 'YES' else ' NOT NULL'}"
                                    for r in rows]}
            clause = " AND table_name ILIKE :like" if like else ""
            rows = c.execute(_text(
                "SELECT t.table_name, "
                "  (SELECT count(*) FROM information_schema.columns c "
                "     WHERE c.table_schema='public' AND c.table_name=t.table_name) AS cols "
                "FROM information_schema.tables t "
                "WHERE t.table_schema='public' AND t.table_type='BASE TABLE'" + clause + " "
                "ORDER BY t.table_name"),
                ({"like": f"%{like}%"} if like else {})).mappings().all()
        return {"tables": [f"{r['table_name']} ({r['cols']} cols)" for r in rows],
                "count": len(rows),
                "hint": "call describe_schema with table=<name> for columns, then query_db"}

    @reg.tool(
        "inspect_host",
        "Run a READ-ONLY command on a registered deployment host over SSH — what is "
        "running there, disk, a deployed app's own container logs. The database records "
        "what BOSS INTENDED; this shows what the machine is actually doing. Refuses "
        "anything that could change the host.",
        _obj({"host": {"type": "string", "description": "host name or id from the fleet"},
              "command": {"type": "string",
                          "description": "e.g. 'docker ps', 'df -h', 'docker logs --tail 50 app'"}},
             ["host", "command"]),
    )
    def inspect_host(ctx, *, project_id, actor, args):
        # An allow-list, not a deny-list. A deny-list on shell input is a promise nobody
        # can keep: `docker` alone can start, stop and delete. Naming the safe verbs is
        # the only version of this that stays true as the tool is used in ways nobody
        # anticipated.
        raw = (args.get("command") or "").strip()
        allowed = ("docker ps", "docker logs", "docker inspect", "docker stats --no-stream",
                   "docker images", "df", "free", "uptime", "uname", "ls", "cat /etc/os-release",
                   "systemctl status", "journalctl", "ss -", "netstat", "ps aux", "whoami")
        if not any(raw.startswith(a) for a in allowed):
            return {"ok": False,
                    "error": f"only read-only commands are allowed here: {', '.join(allowed)}",
                    "note": "this tool inspects; it never changes a host."}
        if any(ch in raw for ch in (";", "&&", "||", "|", "`", "$(", ">")):
            return {"ok": False, "error": "shell chaining/redirection is not permitted"}

        from ..services.deploy_service import (_discard_key_file, _materialise_key, _ssh,
                                               _ssh_key_file)
        from ..services.secret_backends import resolve_target_secret

        want = (args.get("host") or "").strip()
        with ctx.begin() as c:
            row = c.execute(_text(
                "SELECT * FROM deployment_hosts "
                "WHERE project_id = :p AND (id = :h OR name = :h)"),
                {"p": project_id, "h": want}).mappings().first()
            if row is None:
                return {"ok": False, "error": f"no registered host {want!r}"}
            host = dict(row)
            probe = {"secret_refs": ([{"name": "ssh_key",
                                       "secret_ref_id": host["key_secret_ref"]}]
                                     if host.get("key_secret_ref") else []),
                     "host_id": host["id"]}
            ephemeral = None
            try:
                material = resolve_target_secret(c, probe, "ssh_key", ctx.settings)
                key = ephemeral = (_materialise_key(material) if material
                                   else _ssh_key_file(ctx.settings))
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "error": f"no usable credential for this host: {exc}"[:200]}
        try:
            res = _ssh(key, host["address"], (host.get("ssh_user") or "root"),
                       int(host.get("ssh_port") or 22), raw, timeout=45)
        finally:
            if ephemeral:
                _discard_key_file(ephemeral)
        return {"host": host["name"], "command": raw, "exit_code": res.returncode,
                "stdout": (res.stdout or "")[-6000:], "stderr": (res.stderr or "")[-2000:]}

    @reg.tool(
        "http_probe",
        "GET a URL and report status, timing and a body excerpt. Answers 'is the app "
        "actually up RIGHT NOW' — health samples say what was true when last sampled, "
        "which is not the same question.",
        _obj({"url": {"type": "string"},
              "timeout_s": {"type": "integer", "minimum": 1, "maximum": 30, "default": 10}},
             ["url"]),
    )
    def http_probe(ctx, *, project_id, actor, args):
        import time as _time
        import urllib.error as _uerr
        import urllib.request as _ureq

        url = (args.get("url") or "").strip()
        if not url.startswith(("http://", "https://")):
            return {"ok": False, "error": "url must start with http:// or https://"}
        started = _time.time()
        try:
            with _ureq.urlopen(url, timeout=int(args.get("timeout_s") or 10)) as resp:
                body = resp.read(4000).decode("utf-8", errors="replace")
                return {"url": url, "status": resp.status,
                        "ms": round((_time.time() - started) * 1000),
                        "content_type": resp.headers.get("content-type"),
                        "body_excerpt": body}
        except _uerr.HTTPError as exc:
            return {"url": url, "status": exc.code,
                    "ms": round((_time.time() - started) * 1000),
                    "body_excerpt": exc.read(2000).decode("utf-8", errors="replace")}
        except Exception as exc:  # noqa: BLE001 — unreachable is a RESULT, not an error
            return {"url": url, "status": None,
                    "ms": round((_time.time() - started) * 1000),
                    "error": f"{type(exc).__name__}: {exc}"[:300]}

    @reg.tool(
        "git_history",
        "Recent commits for a repo or a single file — when something changed, who "
        "changed it, and the message explaining why. Answers 'when did this break' and "
        "'was this deliberate', which no amount of reading the CURRENT code can.",
        _obj({"repo": {"type": "string", "description": "'boss' (default) or 'owner/name'"},
              "file": {"type": "string", "description": "optional path to scope the log"},
              "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
              "search": {"type": "string",
                         "description": "optional: commits touching this string"}}),
    )
    def git_history(ctx, *, project_id, actor, args):
        root, err = _repo_root(ctx.settings, args.get("repo") or "boss")
        if root is None:
            return {"ok": False, "error": err}
        cmd = ["git", "-C", root, "log", f"-{int(args.get('limit') or 20)}",
               "--date=short", "--pretty=%h|%ad|%an|%s"]
        if args.get("search"):
            cmd += ["-S", str(args["search"])]
        if args.get("file"):
            safe = _safe_path(str(args["file"]), root)
            if safe is None:
                return {"ok": False, "error": "path escapes the repo root"}
            cmd += ["--", safe]
        try:
            out = _subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        except FileNotFoundError:
            return {"ok": False, "error": "git is not installed in this environment"}
        if out.returncode != 0:
            return {"ok": False, "error": (out.stderr or "")[-300:]}
        commits = []
        for line in (out.stdout or "").splitlines():
            parts = line.split("|", 3)
            if len(parts) == 4:
                commits.append({"sha": parts[0], "date": parts[1],
                                "author": parts[2], "subject": parts[3]})
        return {"repo": args.get("repo") or "boss", "commits": commits}

    @reg.tool(
        "get_config",
        "BOSS's EFFECTIVE runtime configuration — feature flags, model routing, engine "
        "selection, limits. Answers 'why did it behave that way' when the code branches "
        "on a setting. Secret VALUES are never returned, only whether each is set.",
        _obj({"like": {"type": "string", "description": "optional name filter, e.g. sandbox"}}),
    )
    def get_config(ctx, *, project_id, actor, args):
        like = (args.get("like") or "").lower().strip()
        # A name-based rule, so a setting added later is redacted BY DEFAULT rather than
        # leaking until someone remembers to add it to a list.
        secretish = ("token", "key", "secret", "password", "credential", "dsn", "url")
        out: dict = {}
        settings = ctx.settings
        for name in sorted(dir(settings)):
            if name.startswith("_"):
                continue
            if like and like not in name.lower():
                continue
            try:
                value = getattr(settings, name)
            except Exception:  # noqa: BLE001
                continue
            if callable(value):
                continue
            if any(mark in name.lower() for mark in secretish):
                out[name] = "<set>" if value else "<unset>"
            elif isinstance(value, (str, int, float, bool, type(None))):
                out[name] = value
        return {"settings": out, "count": len(out),
                "note": "secret-shaped names report only whether a value is present"}


# NOTHING is withheld from the discuss path.
#
# This held `query_db` back, so Ask BOSS answered from ~17 canned reports while the
# capability to answer anything sat one registry away. That is the wrong trade for a
# single-tenant, self-hosted product: every unanswerable question became a request for
# another tool, and the list never catches up with what an operator wants to ask.
#
# The exclusion existed for cross-tenant isolation. There is one tenant in this version,
# so the fence it bought is not currently buying anything — and when multi-tenant
# arrives, the fence belongs in the DATABASE (Postgres RLS on the authorised project),
# not in a hand-maintained deny-list that also has to be right about 20 other tools.
_DISCUSS_EXCLUDED_READ_TOOLS: frozenset[str] = frozenset()


def build_default_registry() -> ToolRegistry:
    """The full BOSS engine tool set (read + write)."""
    reg = ToolRegistry()
    _register_read_tools(reg)
    _register_investigation_tools(reg)
    _register_write_tools(reg)
    _register_operations_tools(reg)
    _register_governance_tools(reg)
    return reg


#: Write tools Ask BOSS may call — BOSS operating BOSS.
#:
#: Ask BOSS could only READ. When a PRD meeting sat queued for two hours behind benchmark jobs
#: boosted past every priority band, BOSS diagnosed it precisely and could not touch it; an
#: operator had to reach in from outside and run the fix by hand. Describing your own stuck
#: queue is not operating anything.
#:
#: An ALLOW-LIST, not `kind == "write"`. The distinction that matters is not read-vs-write, it
#: is OPERATIONAL STATE vs CREATING WORK. These change the state of work that already exists —
#: a job's priority, a wedged job, an orphaned run. `create_code_task` and `request_meeting`
#: start new work and spend money, so they stay on the deliberate path where a human asks for
#: them. Changing BOSS's own code stays in the governed pipeline behind a PR and a review.
# ASKED TO FIX IT, BOSS FIXES IT.
#
# This held FIVE tools while the registry carried EIGHTEEN, so every request an operator
# actually makes — "retry the run", "cancel this stuck task", "start the preview", "approve that
# gate", "fix and retry the deployment" — reached a tool BOSS owned and was forbidden to call.
# It replied with advice because advice was all that remained, and the operator reasonably
# concluded the agent could not do anything. That is not a guardrail; it is the product failing
# at its entire premise.
#
# THERE IS EXACTLY ONE LINE, AND IT IS NOT ABOUT MONEY OR RISK.
#
# I drew two boundaries the product owner never asked for — first "don't spend money", then
# "recover but don't create" — and each one left BOSS unable to do the thing it had just been
# asked to do. Both were wrong, and the reason is the same: a control plane that answers
# "shall I?" is not autonomous, it is a chatbot with a database.
#
#   A CUSTOMER'S app, deployment, preview, observability, queue -> BOSS FIXES IT. Including
#   when the fix is new work: a bug in a customer app is repaired by writing code, so
#   create_code_task IS a fix tool, not a spending risk. No excuses, no "let me know if".
#
#   BOSS'S OWN FOUNDATION (this engine, the API, the console) -> BOSS does NOT patch itself.
#   It says plainly that the BOSS framework needs the fix, names the file and the cause, and a
#   human changes it through the governed pipeline and redeploys. That is not a permission
#   BOSS lacks; it is a different SUBJECT, and pretending otherwise is how an agent edits the
#   floor it is standing on.
#
# So this list is every write the registry has. If a tool should not be reachable, remove it
# from the registry — do not silently disarm it here, which is how BOSS spent this session
# owning eighteen tools and being allowed five.
_DISCUSS_OPERATIONS_TOOLS: frozenset[str] = frozenset({
    "deploy_app",
    # Queue and run state
    "cancel_job",
    "set_job_priority",
    "close_orphaned_runs",
    "link_run_origin",
    # Recovering work that exists
    "retry_code_task",
    "extend_code_task",
    "steer_code_task",
    "cancel_code_task",
    # Writing the fix. A customer's bug is repaired with code.
    "create_code_task",
    "create_discovery",
    "request_meeting",
    # Delivery and environment
    "redeploy",
    "start_preview",
    "stop_preview",
    "register_repository",
    # The operator's own authority, exercised on their instruction
    "decide",
    "waive_gates",
    "send_feedback",
    # Full CRUD. `query_db` already reads every table; state that no structured tool models
    # still has to be fixable, or "BOSS can see the problem and cannot touch it" comes back in
    # a narrower form.
    "write_db",
})


def build_discuss_read_registry() -> ToolRegistry:
    """Project-pinned tools for IntentRouter's tool-grounded discuss path.

    Every read tool, plus the operational writes in ``_DISCUSS_OPERATIONS_TOOLS``.
    ``project_id`` is stripped from schemas/args so the model cannot reach past the project
    the API already authorised — which applies to the writes especially.
    """
    source = build_default_registry()
    reg = ToolRegistry()
    admitted = [
        spec for spec in source.specs()
        if spec.kind == "read" or spec.name in _DISCUSS_OPERATIONS_TOOLS
    ]
    for spec in admitted:
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
                # Preserve the real kind. Stamping every admitted spec "read" made the
                # operational writes look like reads to anything that inspects the registry —
                # an audit, a gate, a future policy. A tool that changes state must say so.
                kind=spec.kind,
            )
        )
    return reg
