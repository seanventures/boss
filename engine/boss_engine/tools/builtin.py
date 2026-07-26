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
        "the change, runs tests and opens a PR. Use for 'build/implement/fix X'.",
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
        with ctx.begin() as c:
            run_id = cts.start_code_task(
                c,
                project_id=pid,
                task_id=args.get("task_id"),
                title=args.get("title") or "Code task",
                instructions=args.get("instructions") or "",
                acceptance_criteria=args.get("acceptance_criteria") or [],
                test_commands=args.get("test_commands") or [],
                owner=ctx.settings.target_github_owner,
                repo=ctx.settings.target_github_repo,
                base_branch=args.get("base_branch") or "main",
                finalize_ctx={},
                jobs=ctx.jobs,
                risk_tier=args.get("risk_tier"),
            )
        return {"status": "queued", "run_id": run_id, "project_id": pid}

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
        "Convene a governed multi-agent PRD meeting: a panel debates the directive "
        "and produces meeting minutes / a PRD. Use for 'let's spec / plan X'.",
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
def _text(sql: str):
    from sqlalchemy import text

    return text(sql)


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
    return reg
