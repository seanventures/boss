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
import uuid as _uuid
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
        "App previews with their open URLs AND, for the failed ones, why they failed: "
        "`failure_kind` (port_timeout / clone_failed / command_missing / boot_crash) and the "
        "error, which usually names the real fault — a build error, a missing entry file, a "
        "port the app did not honour. Read `read_preview_log` for the full startup log, then "
        "repair: an app-side fault is fixed with extend_code_task or steer_code_task on the "
        "run, and start_preview tries again.",
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
                    # WHY IT FAILED, WHICH THIS TOOL USED TO THROW AWAY.
                    #
                    # The row carries `error` and `failure_kind`; the tool selected neither. So
                    # asked "the preview is failing, please fix it", BOSS could see the word
                    # "failed" and nothing else — no reason, no log, nothing to act on — and
                    # answered with advice. The evidence was one column away the whole time:
                    # prev_52cc502f's error reads "Could not resolve entry module index.html",
                    # which names the fault exactly.
                    **({"failure_kind": p.get("failure_kind"), "error": p.get("error")}
                       if p.get("status") == "failed" else {}),
                }
                for p in previews
            ],
        }

    @reg.tool(
        "read_preview_log",
        "The startup log of one preview (prev_…) — the build output and the app's own stdout, "
        "which is where the real reason a preview did not come up is written. Use it whenever a "
        "preview is failed or stuck: `status` alone cannot be acted on. The fault is usually in "
        "the APP, not in BOSS — a build error, a missing entry file, an app that ignores $PORT "
        "or binds 127.0.0.1 — and the repair is extend_code_task or steer_code_task on the "
        "run_id, then start_preview again.",
        _obj({"preview_id": {"type": "string", "description": "prev_…"},
              "project_id": {"type": "string"}},
             ["preview_id"]),
    )
    def read_preview_log(ctx, *, project_id, actor, args):
        from ..services.preview_service import preview_log_snapshot

        pid = args.get("project_id") or project_id
        preview_id = str(args.get("preview_id") or "").strip()
        # THE TENANT FENCE. A preview id is guessable-shaped, and the snapshot is keyed by id
        # alone — so the project is checked here, as every other by-id tool does.
        with ctx.begin() as c:
            owner = c.execute(
                _text("SELECT project_id, run_id FROM app_previews WHERE id = :i"),
                {"i": preview_id},
            ).mappings().first()
        if not owner or owner["project_id"] != pid:
            return {"ok": False, "error": f"no preview {preview_id!r} in this project"}
        try:
            snapshot = preview_log_snapshot(ctx.engine, preview_id)
        except ValueError as error:
            return {"ok": False, "error": str(error)}
        # THE RUN IS THE HANDLE ON THE FIX. Without it the agent has a diagnosis and no way to
        # act on it: every repair tool for an app takes a crun_, not a prev_.
        snapshot["run_id"] = owner["run_id"]
        if not (snapshot.get("log") or "").strip():
            snapshot["note"] = (
                "No log body is readable from here — the file lives on the preview host. "
                "`error` above is what was captured at the time and is usually enough to name "
                "the fault."
            )
        return snapshot

    @reg.tool(
        "get_task_preview",
        "The live preview URL(s) for one code-task run (crun_…), if any preview is "
        "running — and for the failed attempts, why each failed. Answers 'preview this task / "
        "give me the link' and 'why will this app not come up'.",
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
                    # Same reason as list_previews: "no live preview" is not an answer to "why
                    # is the preview failing", and this is the tool that question lands on.
                    **({"failure_kind": p.get("failure_kind"), "error": p.get("error")}
                       if p.get("status") == "failed" else {}),
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
        try:
            with ctx.begin() as c:
                rows = c.execute(_text(sql)).mappings().all()
        except Exception as error:  # noqa: BLE001 — the schema belongs in the reply
            # A WRONG COLUMN NAME MUST NOT COST A WHOLE TURN.
            #
            # Postgres says `column "run_id" does not exist` and stops. That is true and
            # useless: it does not say what the column IS. So the agent guessed again, and
            # again — one real thread spent SEVEN of its eighteen tool calls on
            # `FROM workflows`, `work_class`, `workflow_key`, `graph`, `run_id`,
            # `priority_band`, none of which exist — and answered "what is the status?" with
            # an apology about its own SQL.
            #
            # Reported as: "it is going into a rabbit hole and never coming out of the loop."
            #
            # The schema is RIGHT THERE, one catalogue query away. Handing it back with the
            # failure turns an unbounded guessing game into a single correction.
            return {"ok": False, "error": f"{type(error).__name__}: {str(error)[:400]}",
                    **_schema_hint_for(ctx, sql)}
        capped = rows[:max_rows]
        return {
            "row_count": len(capped),
            "truncated": len(rows) > max_rows,
            "rows": [_json_safe(dict(r)) for r in capped],
        }

    # -----------------------------------------------------------------------
    # THE MACHINES BOSS DEPLOYS ONTO.
    #
    # The chat agent could describe every build and every preview and had no idea what a HOST
    # was — so "is hetzner-ci-01 healthy?" or "where would this deploy?" were unanswerable, and
    # the Deployment Manager role existed on paper with nothing to read.
    #
    # Read-only, deliberately. The role's own contract says it must not deploy directly and
    # must escalate staging and production; these tools let it diagnose and recommend, and the
    # governed deploy path remains the only way anything actually ships.
    # -----------------------------------------------------------------------
    @reg.tool(
        "list_deployment_hosts",
        "The machines this project can deploy onto: name, address, architecture, status "
        "(registered/verified/degraded/retired), port budget and when each was last checked. "
        "Use for 'what hosts do we have', 'is <host> healthy', 'where can this ship'.",
        _obj({"project_id": {"type": "string"}, "limit": _LIMIT}),
    )
    def list_deployment_hosts(ctx, *, project_id, actor, args):
        pid = args.get("project_id") or project_id
        limit = int(args.get("limit") or 25)
        with ctx.begin() as c:
            rows = c.execute(
                _text(
                    "SELECT id, name, address, ssh_user, ssh_port, arch, status, labels, "
                    "       port_min, port_max, max_apps, last_preflight_at "
                    "FROM deployment_hosts WHERE project_id=:p "
                    "ORDER BY name LIMIT :l"
                ),
                {"p": pid, "l": limit},
            ).mappings().all()
        return {"project_id": pid, "hosts": [_json_safe(dict(r)) for r in rows]}

    @reg.tool(
        "describe_deployment_host",
        "One host in full: its last preflight result — reachable, architecture detected, "
        "findings and problems — plus the apps currently deployed onto it. Accepts the host "
        "id, its name, or its IP address. Use before recommending anything be shipped there.",
        _obj({"host": {"type": "string", "description": "host id, name or address"},
              "project_id": {"type": "string"}}, ["host"]),
    )
    def describe_deployment_host(ctx, *, project_id, actor, args):
        pid = args.get("project_id") or project_id
        needle = str(args.get("host") or "").strip()
        if not needle:
            return {"error": "host is required (id, name or address)"}
        with ctx.begin() as c:
            # Matched three ways because an operator says "2.29.2.202" and the console says
            # "hetzner-ci-01"; forcing them to know the id is a lookup they should not have to do.
            row = c.execute(
                _text(
                    "SELECT * FROM deployment_hosts "
                    "WHERE project_id=:p AND (id=:n OR name=:n OR address=:n) LIMIT 1"
                ),
                {"p": pid, "n": needle},
            ).mappings().first()
            if not row:
                return {"error": f"no host matching {needle!r} in this project"}
            apps = c.execute(
                _text(
                    # THREE COLUMNS THAT DO NOT EXIST. `deployment_targets` has app_name,
                    # domains and no app_port at all — so "what is running on this host" raised
                    # `column "name" does not exist` every single time it was called.
                    #
                    # Found by BOSS itself, mid-answer, and reported the way this whole effort
                    # is about: it named the tool, quoted the error, said the defect was in the
                    # framework rather than the operator's app, gave its confidence without it,
                    # and named the next check. The tool it could not use was the one for
                    # "is the host healthy".
                    "SELECT id, app_name, environment, status, domains, updated_at "
                    "FROM deployment_targets WHERE project_id=:p AND host_id=:h "
                    "ORDER BY updated_at DESC LIMIT 50"
                ),
                {"p": pid, "h": row["id"]},
            ).mappings().all()
        host = _json_safe(dict(row))
        # Never hand the model a credential reference it has no use for.
        host.pop("key_secret_ref", None)
        return {"host": host, "apps_on_this_host": [_json_safe(dict(a)) for a in apps]}

    @reg.tool(
        "list_deployments",
        "Recent deployment runs: app, environment, target, commit, status and when. "
        "Use for 'what shipped', 'did the last deploy succeed', 'what is on <host> now'.",
        _obj({"project_id": {"type": "string"}, "limit": _LIMIT}),
    )
    def list_deployments(ctx, *, project_id, actor, args):
        pid = args.get("project_id") or project_id
        limit = int(args.get("limit") or 25)
        with ctx.begin() as c:
            rows = c.execute(
                _text(
                    "SELECT id, app_name, environment, target_kind, target_ref, commit_ref, "
                    "       status, deployment_target_id, decision_id, created_at "
                    "FROM deployment_runs WHERE project_id=:p "
                    "ORDER BY created_at DESC LIMIT :l"
                ),
                {"p": pid, "l": limit},
            ).mappings().all()
        return {"project_id": pid, "deployments": [_json_safe(dict(r)) for r in rows]}


# ===========================================================================
# WRITE TOOLS — create workflow tasks
# ===========================================================================
def _register_write_tools(reg: ToolRegistry) -> None:
    # Read from the service rather than restated here. The enum goes into the tool schema the
    # model sees, so a behaviour added there and forgotten here would be a behaviour the agent
    # is told does not exist.
    _SKILL_BEHAVIOURS = services_module("skill_definitions").BEHAVIOURS
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
                "reference_links": {
                    "type": "array", "items": {"type": "string"},
                    "description": "URLs the builder must read before writing code — an API "
                                   "doc, a repo to follow the conventions of, a design. Given "
                                   "to every stage as required reading. NOT indexed or "
                                   "searched: an agent opens them, so they must be reachable "
                                   "without a login.",
                },
                "workflow_run_id": {
                    "type": "string",
                    "description": "the outer workflow run this task belongs to, when a "
                                   "workflow step is creating it. Names the run that already "
                                   "governs the work so a second one is not started for it.",
                },
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
        reversibility="costs_money",
    )
    def create_code_task(ctx, *, project_id, actor, args):
        pid = args.get("project_id") or project_id
        cts = services_module("code_task_service")
        task_id = args.get("task_id")
        instructions = args.get("instructions") or ""
        base_branch = args.get("base_branch") or "main"
        # ALREADY GOVERNED. When a workflow step creates this task, the run that governs it
        # exists — naming it here stops start_code_task opening a SECOND one, which would put
        # a whole nine-step pipeline inside a step of the pipeline that created it.
        finalize_ctx: dict = ({"workflow_run_id": str(args["workflow_run_id"])}
                              if args.get("workflow_run_id") else {})
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
                reference_links=args.get("reference_links") or [],
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
        reversibility="costs_money",
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
        reversibility="costs_money",
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
        reversibility="costs_money",
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
        "draft_workflow",
        "Create a new delivery workflow from a described sequence of steps, or REPLACE the "
        "steps of one that exists. Pass steps as a list of {id, capability} in order — use "
        "list_capabilities for the available kinds and what each produces, and name a custom "
        "step's skill with skill=<skill_key>. The draft is validated against the same rules "
        "the visual builder obeys and published as version 1 (or the next version), so a "
        "workflow authored here and one drawn by hand are the same object. Nothing runs: this "
        "authors a workflow, it does not start one.",
        _obj(
            {
                "workflow_key": {"type": "string",
                                 "description": "lower_snake_case identifier, stable"},
                "name": {"type": "string"},
                "description": {"type": "string"},
                "work_class": {"type": "string",
                               "description": "the class of work this path is for, e.g. "
                                              "project.new; omit to leave it unmapped"},
                "steps": {
                    "type": "array",
                    "description": "in order: [{id, capability, skill?, captain?, approval?}]. "
                                   "`approval` is 'manual' or 'auto' — a capability at or above "
                                   "the risk floor is forced manual whatever you ask for. You "
                                   "do NOT wire inputs: a step's required input is connected "
                                   "to the nearest earlier step that produces it.",
                    "items": {"type": "object"},
                },
                "inputs": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "What the OPERATOR supplies when starting a run, e.g. "
                                   "['media_artifact_id', 'title']. Needed only for inputs no "
                                   "step produces — typically the first step's. Anything "
                                   "neither produced upstream nor listed here is refused, "
                                   "because a workflow that publishes and then dies on a "
                                   "missing value is worse than one that will not publish.",
                },
                "project_id": {"type": "string"},
            },
            ["workflow_key", "name", "steps"],
        ),
        kind="write",
        reversibility="safe",
    )
    def draft_workflow(ctx, *, project_id, actor, args):
        wf = services_module("workflows")
        caps = services_module("workflow_capabilities")
        pid = args.get("project_id") or project_id
        key = re.sub(r"[^a-z0-9_]+", "_", str(args.get("workflow_key") or "").strip().lower())
        if not re.fullmatch(r"[a-z][a-z0-9_]{2,60}", key or ""):
            return {"ok": False, "error": "workflow_key must be lower_snake_case, 3-61 chars"}
        spec = _spec_from_steps(args.get("steps") or [], args)
        if isinstance(spec, dict) and spec.get("ok") is False:
            return spec
        with ctx.begin() as c:
            # VALIDATE BEFORE CREATING ANYTHING. A definition row for a spec that cannot publish
            # leaves a workflow in the catalogue that does not work, and the operator has to go
            # and delete it.
            try:
                caps_by_key = {x["key"]: x for x in caps.palette()}
                # A STEP THAT NAMES A REAL SKILL IS NOT A MISTAKE TO REFUSE.
                #
                # `skill: "video.bookend"` becomes binding.custom_step, and a custom step is a
                # DIFFERENT object from a skill — a workflow_custom_steps row that binds one.
                # So every draft naming a skill failed validation with "this project does not
                # have that skill", the definition stayed empty, and the agent proposed the
                # same thing again. That is the loop an operator reported as "it just keeps
                # asking the same question and the action button never does anything".
                #
                # The model was right and the plumbing was not: naming the skill IS what the
                # operator means. Defining the step that binds it is a safe write the agent is
                # already trusted with (draft_custom_step is on the write allowlist), so it is
                # done here rather than handed back as an error nobody can act on.
                made = _ensure_steps_for_skills(c, pid, spec, actor)
                problems = _draft_problems(c, pid, spec, caps_by_key, caps)
                if problems:
                    return {"ok": False, "error": "; ".join(problems[:4])}
                existing = c.execute(
                    _text("SELECT id FROM workflow_definitions "
                          "WHERE project_id=:p AND workflow_key=:k LIMIT 1"),
                    {"p": pid, "k": key},
                ).scalar()
                if existing:
                    definition_id = str(existing)
                else:
                    definition_id = wf.create_definition(
                        c, project_id=pid, workflow_key=key,
                        name=str(args.get("name") or key), actor=actor,
                        description=str(args.get("description") or "") or None,
                        namespace="product",
                    )["id"]
                published = wf.publish_version(
                    c, project_id=pid, definition_id=definition_id, spec=spec, actor=actor)
            except Exception as error:  # noqa: BLE001 — the reason belongs in the reply
                return {"ok": False, "error": f"{type(error).__name__}: {str(error)[:300]}"}
            work_class = str(args.get("work_class") or "").strip()
            mapped = None
            if work_class:
                try:
                    mapped = wf.create_mapping(
                        c, project_id=pid, workflow_version_id=published["id"],
                        selector_kind="work_class", selector_value=work_class,
                        # Project scope, not org: a workflow the agent drafted for THIS project
                        # must not silently become the path every project follows.
                        scope_type="project", scope_id=pid, priority=100, actor=actor)
                except Exception as error:  # noqa: BLE001
                    mapped = {"note": f"published, but not mapped: {str(error)[:160]}"}
        return {"status": "published", "workflow_key": key, "definition_id": definition_id,
                "version": published.get("version"), "steps": len(spec["steps"]),
                # Named, because a step BOSS defined on the operator's behalf is something they
                # should be able to find and change afterwards.
                **({"defined_steps": made} if made else {}),
                **({"mapping": mapped} if mapped else {})}

    @reg.tool(
        "delete_workflow",
        "Retire a delivery workflow this project authored. Refuses while runs are still using "
        "it, and refuses a catalogue workflow — those are BOSS's own and are forked, not "
        "deleted.",
        _obj({"workflow_key": {"type": "string"}, "project_id": {"type": "string"}},
             ["workflow_key"]),
        kind="write",
        reversibility="destroys_work",
    )
    def delete_workflow(ctx, *, project_id, actor, args):
        pid = args.get("project_id") or project_id
        key = str(args.get("workflow_key") or "").strip().lower()
        with ctx.begin() as c:
            row = c.execute(
                _text("SELECT id, is_catalogue, name FROM workflow_definitions "
                      "WHERE project_id=:p AND workflow_key=:k LIMIT 1"),
                {"p": pid, "k": key},
            ).mappings().first()
            if not row:
                return {"ok": False, "error": f"no workflow {key!r} in this project"}
            if row["is_catalogue"]:
                return {"ok": False,
                        "error": f"{key} is one of BOSS's catalogue workflows. Fork it and edit "
                                 "the fork; deleting it would remove it for every project."}
            live = c.execute(
                _text("SELECT count(*) FROM workflow_runs r "
                      "JOIN workflow_versions v ON v.id = r.workflow_version_id "
                      "WHERE v.workflow_definition_id = :d AND r.status IN "
                      "('running','waiting','validating')"),
                {"d": row["id"]},
            ).scalar() or 0
            if live:
                return {"ok": False,
                        "error": f"{live} run(s) are still on {key}. Let them finish or cancel "
                                 "them first — deleting the path underneath a live run leaves "
                                 "it unable to report what it was following."}
            c.execute(_text("DELETE FROM workflow_definitions WHERE id = :d"), {"d": row["id"]})
        return {"status": "deleted", "workflow_key": key, "name": row["name"]}

    @reg.tool(
        "draft_custom_step",
        "Define a reusable workflow step backed by a skill this project has authored. Use it "
        "when no standard capability covers the work — publishing to a third-party account, "
        "running a media or document tool. The skill must exist first (list_skills); this step "
        "is what makes it placeable in a workflow.",
        _obj(
            {
                "step_key": {"type": "string", "description": "lower_snake_case, stable"},
                "label": {"type": "string"},
                "summary": {"type": "string"},
                "skill": {"type": "string", "description": "the skill_key this step runs"},
                "knowledge": {"type": "string",
                              "description": "the standard this step holds to — house style, "
                                             "the format your team uses. Carried into every "
                                             "execution."},
                "inputs": {"type": "array", "items": {"type": "object"}},
                "produces": {"type": "array", "items": {"type": "object"}},
                "project_id": {"type": "string"},
            },
            ["step_key", "label", "skill"],
        ),
        kind="write",
        reversibility="safe",
    )
    def draft_custom_step(ctx, *, project_id, actor, args):
        steps = services_module("workflow_custom_steps")
        skills = services_module("skill_definitions")
        pid = args.get("project_id") or project_id
        skill_key = str(args.get("skill") or "").strip().lower()
        with ctx.begin() as c:
            if skills.get_skill(c, pid, skill_key) is None:
                # NAMING WHAT EXISTS beats "not found": the model picked a skill and the useful
                # correction is the list it should have picked from.
                available = [s["skill_key"] for s in skills.list_skills(c, pid)]
                return {"ok": False, "error": f"no skill {skill_key!r} in this project",
                        "available": available}
            try:
                created = steps.create(
                    c, project_id=pid, actor=actor,
                    step_key=str(args.get("step_key") or "").strip().lower(),
                    label=str(args.get("label") or ""),
                    summary=str(args.get("summary") or ""),
                    binding_kind="skill", skill_id=skill_key,
                    inputs=args.get("inputs") or [], produces=args.get("produces") or [],
                    knowledge=str(args.get("knowledge") or ""))
            except Exception as error:  # noqa: BLE001
                return {"ok": False, "error": f"{type(error).__name__}: {str(error)[:300]}"}
        return {"status": "defined", "step_key": created.get("step_key"), "skill": skill_key}

    @reg.tool(
        "draft_skill",
        "Create a skill from what the operator has told you. A skill is the reusable unit of "
        "doing — trimming a video, posting to a channel, calling one tool on an MCP server — "
        "and it is what a custom workflow step runs and what an agent can be given. Ask for "
        "whatever you still need (what it does, what it takes in, what it hands back, and how "
        "it runs) BEFORE calling this: the operator approves the finished skill with one press, "
        "so the draft you propose is the thing that gets created.",
        _obj(
            {
                "skill_key": {"type": "string",
                              "description": "lower_snake_case, 3-49 chars. Stable: workflow "
                                             "steps point at it by this name."},
                "label": {"type": "string", "description": "what a person calls it"},
                "summary": {"type": "string", "description": "one line, what it does"},
                "behaviour_kind": {
                    "type": "string",
                    "enum": list(_SKILL_BEHAVIOURS),
                    "description": "builtin = a capability BOSS already has; mcp_tool = one "
                                   "named tool on a registered MCP server, which is how you "
                                   "reach a third-party account; sandboxed_command = a pinned "
                                   "container image run in the sandbox.",
                },
                "knowledge": {"type": "string",
                              "description": "the standard it holds to — house style, the "
                                             "format your team uses. Carried into every run."},
                "inputs": {"type": "array", "items": {"type": "object"},
                           "description": "[{name, required, description}]"},
                "produces": {"type": "array", "items": {"type": "object"}},
                "config": {"type": "object",
                           "description": "behaviour-specific. mcp_tool needs server and tool; "
                                          "sandboxed_command needs a digest-pinned image and a "
                                          "command template whose placeholders are all declared "
                                          "inputs."},
                "risk_tier": {"type": "integer",
                              "description": "0-3. Anything that spends money, publishes, or "
                                             "touches an outside account is not tier 0."},
                "project_id": {"type": "string"},
            },
            ["skill_key", "label", "behaviour_kind"],
        ),
        kind="write",
        reversibility="safe",
    )
    def draft_skill(ctx, *, project_id, actor, args):
        skills = services_module("skill_definitions")
        pid = args.get("project_id") or project_id
        with ctx.begin() as c:
            try:
                created = skills.save_skill(
                    c,
                    project_id=pid,
                    actor=actor,
                    skill_key=str(args.get("skill_key") or "").strip().lower(),
                    label=str(args.get("label") or ""),
                    behaviour_kind=str(args.get("behaviour_kind") or ""),
                    summary=str(args.get("summary") or ""),
                    knowledge=str(args.get("knowledge") or ""),
                    inputs=args.get("inputs") or [],
                    produces=args.get("produces") or [],
                    config=args.get("config") or {},
                    risk_tier=int(args.get("risk_tier") or 1),
                )
            except Exception as error:  # noqa: BLE001
                # THE MESSAGE IS THE REPAIR INSTRUCTION. save_skill's errors say precisely what
                # is wrong (a placeholder that is not a declared input, an unpinned image), and
                # the model can fix the draft and re-propose from them. Flattening them to
                # "invalid" would cost the operator a round trip for nothing.
                return {"ok": False, "error": f"{type(error).__name__}: {str(error)[:300]}"}
        return {
            "status": "defined",
            "skill_key": created.get("skill_key"),
            "behaviour_kind": created.get("behaviour_kind"),
            "risk_tier": created.get("risk_tier"),
            # A skill on its own is not yet placeable. Saying so here is what stops the next
            # answer being "it's created" followed by an operator who cannot find it anywhere.
            "next": "draft_custom_step binds this skill to a workflow step so it can be placed.",
        }

    @reg.tool(
        "list_capabilities",
        "The kinds of step a workflow can be made of: what each one does, what it needs, what "
        "it produces, which agent answers for it, and whether it always requires a person. Read "
        "this before drafting a workflow — a step that asks for an input nothing produces is "
        "the most common way a draft fails to publish.",
        _obj({}),
    )
    def list_capabilities(ctx, *, project_id, actor, args):
        caps = services_module("workflow_capabilities")
        return {"capabilities": [
            {k: v for k, v in entry.items()
             if k in ("key", "label", "summary", "required_inputs", "produces", "risk_tier",
                      "is_gate", "captain", "approval_forced_manual")}
            for entry in caps.palette()
        ]}

    @reg.tool(
        "list_skills",
        "Skills this project has authored, with what each needs and produces and whether it "
        "forces an approval. A skill is what a custom workflow step runs.",
        _obj({"project_id": {"type": "string"}}),
    )
    def list_skills(ctx, *, project_id, actor, args):
        # EVERYTHING THIS PROJECT CAN RUN, not only what it wrote.
        #
        # This returned `skill_definitions` alone, so the agent's view of the world was the
        # handful of skills an operator had authored — and every skill BOSS SHIPS was invisible
        # to it. Asked to build a video workflow it reported "no such skill: video.bookend"
        # about a skill sitting in the catalogue with an executor behind it, then designed
        # around the gap. An agent that cannot see a capability cannot use it, and will
        # confidently tell the operator it does not exist.
        skills = services_module("skill_definitions")
        pid = args.get("project_id") or project_id
        keep = ("skill_key", "label", "summary", "behaviour_kind", "inputs",
                "produces", "risk_tier", "trusted", "enabled", "requires_approval")
        with ctx.begin() as c:
            authored = [
                {k: v for k, v in s.items() if k in keep}
                for s in skills.list_skills(c, pid)
            ]
            shipped: list[dict[str, Any]] = []
            try:
                from ..config import get_settings
                from .._compat import services_module as _svc

                broker = _svc("entitlements").EntitlementBrokerService(get_settings())
                for item in broker.list_skills(pid, connection=c):
                    shipped.append({
                        "skill_key": item.get("id"),
                        "label": item.get("id"),
                        "summary": item.get("description"),
                        "behaviour_kind": item.get("kind"),
                        "risk_tier": item.get("risk_tier"),
                        "enabled": item.get("enabled", True),
                        "trusted": bool((item.get("provenance") or {}).get("trusted")),
                        "ships_with_boss": True,
                    })
            except Exception:  # noqa: BLE001 — authored skills are still worth returning
                pass
        return {"skills": authored + shipped,
                "authored": len(authored), "ships_with_boss": len(shipped)}

    @reg.tool(
        "start_workflow",
        "THE way to start new work. Runs a whole delivery workflow — research, the PRD panel, "
        "the approval gates and the written record are STEPS of it — so the operator watches "
        "one run in the Workflow area and reads one outcome at the end. Pick the workflow_key "
        "whose objective matches the ask: research_led_discovery to find out whether something "
        "is worth building at all (ends in a decision and a document, not code), "
        "standard_product_delivery / major_feature / minor_feature / fast_fix to build it, "
        "ux_certification, security_hardening, backend_service_setup, data_migration, "
        "compliance_release, third_party_integration, deprecation_sunset for the rest. "
        "Never start a bare research run or a bare panel: those produce an artefact that "
        "belongs to no run, which nothing in the console links to.",
        _obj(
            {
                "directive": {"type": "string",
                              "description": "the work, in the operator's terms"},
                "workflow_key": {"type": "string",
                                 "description": "which workflow to run, by key"},
                "title": {"type": "string",
                          "description": "short name for the run and its document"},
                "from_discovery": {
                    "type": "string",
                    "description": "a COMPLETED discovery run (drun_…) whose dossier this run "
                                   "should build on. The research step adopts it instead of "
                                   "commissioning more — use this whenever the operator says "
                                   "they do not want the research repeated. Look the id up "
                                   "with list_discovery; never guess one.",
                },
                "project_id": {"type": "string"},
            },
            ["directive", "workflow_key"],
        ),
        kind="write",
        reversibility="costs_money",
    )
    def start_workflow(ctx, *, project_id, actor, args):
        from uuid import uuid4

        pid = args.get("project_id") or project_id
        directive = (args.get("directive") or "").strip()
        key = (args.get("workflow_key") or "").strip()
        title = (args.get("title") or directive[:120] or "New work").strip()
        wf = services_module("workflows")
        with ctx.begin() as c:
            version_id = c.execute(
                _text(
                    "SELECT v.id FROM workflow_versions v "
                    "JOIN workflow_definitions d ON d.id = v.workflow_definition_id "
                    "WHERE d.project_id=:p AND d.workflow_key=:k "
                    "AND COALESCE(d.namespace,'system')='product' "
                    "ORDER BY v.version DESC LIMIT 1"
                ),
                {"p": pid, "k": key},
            ).scalar()
            if not version_id:
                # NAMING WHAT EXISTS beats "not found": the model picked a key and the only
                # useful correction is the list it should have picked from.
                available = [
                    row[0] for row in c.execute(
                        _text("SELECT DISTINCT workflow_key FROM workflow_definitions "
                             "WHERE project_id=:p AND COALESCE(namespace,'system')='product' "
                             "ORDER BY workflow_key"),
                        {"p": pid},
                    ).all()
                ]
                return {"ok": False,
                        "error": f"no workflow {key!r} in this project",
                        "available": available}
            # CARRY FINISHED RESEARCH FORWARD. Checked here rather than trusted: the adopt key
            # says "this work already happened", and a model must not be able to assert that.
            adopted = (args.get("from_discovery") or "").strip()
            run_input = {"directive": directive, "title": title}
            if adopted:
                found = c.execute(
                    _text("SELECT project_id, status FROM discovery_runs WHERE id = :d"),
                    {"d": adopted},
                ).mappings().first()
                if not found or found["project_id"] != pid:
                    return {"ok": False, "error": f"no research run {adopted!r} in this project"}
                if str(found["status"] or "") != "completed":
                    return {"ok": False,
                            "error": f"that research is {found['status']}, not completed — "
                                     "there is nothing finished to build on yet"}
                run_input["adopt_discovery_run_id"] = adopted
            try:
                run = wf.start_run(
                    c, project_id=pid, actor=actor,
                    input_data=run_input,
                    dedupe_key=f"agent:{key}:{uuid4().hex[:12]}",
                    workflow_version_id=version_id,
                )
            except Exception as error:  # noqa: BLE001 — the reason belongs in the reply
                return {"ok": False, "error": f"{type(error).__name__}: {str(error)[:300]}"}
        return {"status": "running", "run_id": run.get("id"), "workflow_key": key,
                "project_id": pid, "title": title,
                **({"built_on_research": adopted} if adopted else {})}

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
                "research": {
                    "type": "string",
                    "description": "findings the panel should argue FROM rather than "
                                   "re-derive — a research step's dossier, typically.",
                },
                "objective": {
                    "type": "string",
                    "description": "what the WORKFLOW this panel sits in has to end with — "
                                   "a shipped app, a decision and a document, a security "
                                   "sign-off. Shapes what the PRD is for.",
                },
                "workflow_name": {
                    "type": "string",
                    "description": "the workflow this panel is part of, by name.",
                },
            },
            ["directive"],
        ),
        kind="write",
        reversibility="costs_money",
    )
    def request_meeting(ctx, *, project_id, actor, args):
        pid = args.get("project_id") or project_id
        ms = services_module("meeting_service")
        # RESEARCH THE PANEL CAN READ.
        #
        # The directive IS the panel's prompt, so evidence handed over as a separate field
        # would have to be threaded through task creation, the agenda and every seat's context
        # to reach the argument. Appended here it reaches all of them, because they all read
        # the directive. Clearly delimited so nobody mistakes the findings for the ask.
        directive = (args.get("directive") or "").strip()
        # WHAT THIS PANEL IS FOR, put where every seat will read it.
        #
        # Same mechanism as the research block below, for the same reason: the directive IS the
        # panel's prompt, so anything not in it never reaches the agenda or the seats. It goes
        # FIRST because it changes what the rest of the text is asking for — a panel told
        # afterwards that none of this will be built has already written a build plan.
        objective = (args.get("objective") or "").strip()
        workflow_name = (args.get("workflow_name") or "").strip()
        if objective:
            directive = (
                f"--- WHAT THIS PANEL IS FOR ---\n"
                f"You are the panel inside BOSS's "
                f"{workflow_name or 'delivery'} workflow. That workflow's objective:\n\n"
                f"{objective}\n\n"
                f"Write for THAT outcome. A PRD that suits a different workflow is the wrong "
                f"document however good it is.\n"
                f"--- WHAT YOU ARE BEING ASKED ABOUT ---\n\n{directive}"
            )
        research = (args.get("research") or "").strip()
        if research:
            directive = (
                f"{directive}\n\n"
                f"--- RESEARCH ALREADY DONE FOR THIS ---\n"
                f"BOSS researched this before convening you. Argue from it: agree, disagree, "
                f"or say what it missed — do not re-derive it from scratch.\n\n{research}"
            )
        with ctx.begin() as c:
            result = ms.convene_prd_meeting_core(
                c,
                settings=ctx.settings,
                budget=ctx.budget,
                intake=ctx.intake,
                jobs=ctx.jobs,
                project_id=pid,
                directive=directive,
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
        reversibility="safe",
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


#: Arguments whose VALUE IS PROSE, exempt from the marker scan.
#:
#: The markers are substrings, and they are substrings of ordinary English. A real directive —
#: "…the rule fires and records its inputs before any LLM… never 'buy' as an instruction… a
#: margin that survives realistic commission" — was refused as invented because it contains
#: "..." and would equally have been refused for "<", "bar" or "some-". The operator wrote a
#: careful brief, pressed the button, and BOSS told them their own words looked made up.
#:
#: The check is for FABRICATED IDENTIFIERS: a run_id the model did not look up, a repository
#: called owner/name, a domain at example.com. Those are id-shaped and short. Prose is neither,
#: and a long directive is exactly what a well-specified run needs.
_FREE_TEXT_ARGS = frozenset({
    "directive", "instructions", "reason", "note", "fix_note", "message", "why", "seed",
    "context", "title", "label", "hint", "question", "summary", "description", "brief",
    "purpose", "body", "content", "feedback", "agenda", "objective",
})


def _placeholder_argument(args: dict) -> str:
    """The first argument that looks invented, described for the model, or ''."""
    for key, value in (args or {}).items():
        if not isinstance(value, str):
            continue
        if str(key).lower() in _FREE_TEXT_ARGS:
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
        reversibility="destroys_work",
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
        "NOT for deployment targets: use create_deployment_target, which knows the host, the "
        "free ports and the health check. Composing that INSERT by hand failed on a guessed "
        "table name and would have written a row missing half its columns. "
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
        reversibility="destroys_work",
    )
    def write_db(ctx, *, project_id, actor, args):
        from ..ports import sql as _s
        from ..services.core import emit_event

        statement = (args.get("sql") or "").strip().rstrip(";")
        ok, why = _is_single_write(statement, whole_table=bool(args.get("whole_table")))
        if not ok:
            return {"ok": False, "error": f"rejected: {why}"}
        # THE SAME GUARD THE OFFER PATH HAS. `propose_action` refuses to put a fabricated value
        # behind a button; write_db would execute one. So the check that stopped a bad BUTTON
        # did not stop a bad WRITE, and the row BOSS tried to insert carried
        # target_ref='container_ref_placeholder' — a value invented to fill a column it did not
        # understand, headed for a table that decides where an app gets deployed.
        invented = _placeholder_argument({"sql": statement})
        if invented:
            return {"ok": False,
                    "error": ("this statement contains an invented value. Look the real one up "
                              "first, or use the tool that knows it — create_deployment_target "
                              "derives target_ref from the host record rather than guessing at "
                              "it. A row written with a placeholder is worse than no row: it "
                              "looks configured and fails later, somewhere else.")}
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
        reversibility="safe",
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
        reversibility="safe",
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
        "create_deployment_target",
        "Give an app somewhere to deploy TO. Use this when a deploy refused with 'no target for "
        "this app' or 'has no deployment target of its own' — that is not an ambiguity to "
        "resolve by picking another app's target, it is a missing destination, and this creates "
        "it. Picks a genuinely free port on the project's host (nothing listening AND nothing "
        "else assigned it), declares a health check with the right scheme, and derives the "
        "connection details from the host record. An app that already has a target for that "
        "environment adopts it instead of growing a second one. It creates the DESTINATION and "
        "ships nothing — deploy with deploy_app afterwards. Never write this row with SQL: the "
        "table has columns this sets that a hand-written INSERT does not know about.",
        _obj({"app_name": {"type": "string",
                           "description": "the app's NAME, e.g. 'Change Calendar' — not the "
                                          "directive it was built from"},
              "environment": {"type": "string", "description": "staging | production"},
              "host_id": {"type": "string",
                          "description": "optional dh_ — only needed when the project deploys "
                                         "to more than one host"},
              "port": {"type": "integer",
                       "description": "optional; leave unset and a free one is chosen"}},
             required=["app_name"]),
        kind="write",
        reversibility="safe",
    )
    def create_deployment_target(ctx, *, project_id, actor, args):
        from ..services.deployment_fleet import create_deployment_target as _create

        with ctx.begin() as c:
            return _create(
                c, project_id=project_id, app_name=str(args.get("app_name") or ""),
                environment=str(args.get("environment") or "staging"),
                host_id=(str(args["host_id"]) if args.get("host_id") else None),
                port=(int(args["port"]) if args.get("port") else None),
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
        "test_github_access",
        "RE-RUN the GitHub access check for this project and return what GitHub said. Use "
        "this after an operator changes a token's repository access or permissions — it is "
        "the action that turns 'I fixed the token' into a verified connection, and it needs "
        "no credential from anybody. Also use it to confirm a repair you suggested actually "
        "worked, rather than telling the operator to go and press a button.",
        _obj({"connection_id": {"type": "string",
                                "description": "optional; the project's connection by default"},
              "repository": {"type": "string",
                             "description": "optional; owner/name, the primary binding by "
                                            "default"}}),
        kind="write",
        # A READ OF GITHUB THAT RECORDS WHAT IT SAW. It asks GitHub one question and updates
        # the connection's test state; running it again simply asks again, which is the whole
        # point of it existing.
        reversibility="safe",
    )
    def test_github_access(ctx, *, project_id, actor, args):
        # WHY THIS IS A TOOL. Asked why a connection failed, the agent diagnosed it correctly
        # and then said "open BOSS's GitHub view, select the repository, and click Test
        # access" — an instruction to do by hand the one step it could have done itself.
        # "Just explaining the problem would not help anyone."
        from ..services.github_connections import GitHubConnectionService

        pid = args.get("project_id") or project_id
        svc = GitHubConnectionService(ctx.settings)
        with ctx.begin() as c:
            conn_id = str(args.get("connection_id") or "").strip()
            if not conn_id:
                row = c.execute(
                    _text("SELECT id FROM github_connections WHERE project_id = :p "
                          " ORDER BY created_at DESC LIMIT 1"), {"p": pid}).first()
                if row is None:
                    return {"ok": False,
                            "error": f"{pid} has no GitHub connection to test — connect one "
                                     f"in Setup first."}
                conn_id = row[0]
            wanted = str(args.get("repository") or "").strip()
            binding = c.execute(
                _text("SELECT id, full_name FROM github_repository_bindings "
                      " WHERE project_id = :p AND status = 'active' "
                      "   AND (:w = '' OR lower(full_name) = lower(:w)) "
                      " ORDER BY (role = 'primary') DESC LIMIT 1"),
                {"p": pid, "w": wanted}).mappings().first()
            if binding is None:
                return {"ok": False,
                        "error": (f"no active repository binding on {pid}"
                                  + (f" for {wanted}" if wanted else "")
                                  + " — bind one in Setup first.")}
            try:
                # A NEW dedupe key each time: this is deliberately repeatable, because the
                # whole point is to re-ask a question whose answer has changed.
                result = svc.test_connection(
                    c, project_id=pid, connection_id=conn_id,
                    binding_id=binding["id"],
                    dedupe_key=f"agent-retest-{_uuid.uuid4().hex[:12]}",
                    actor=actor)
            except Exception as exc:  # noqa: BLE001 — the reason is the answer
                return {"ok": False, "repository": binding["full_name"],
                        "error": f"{type(exc).__name__}: {exc}"[:400]}
        detail = result.get("detail") or {}
        passed = result.get("status") == "passed"
        return {"ok": passed, "status": result.get("status"),
                "repository": binding["full_name"],
                "granted_permissions": detail.get("granted_permissions"),
                "missing_permissions": detail.get("missing_permissions"),
                "message": detail.get("message"),
                "next": ("nothing — this connection works" if passed else
                         "the message above is what GitHub said; fix that and test again")}

    @reg.tool(
        "set_project_setting",
        "Change one of this project's settings — which model implements, reviews, handles "
        "hard or routine work, or answers in chat; how many agents run at once; the turn and "
        "review-round budgets. Takes effect on the next run, no restart. Use it to APPLY a "
        "change you have recommended once the operator agrees, instead of describing where "
        "they could click. Send an empty value to hand a setting back to the deployment "
        "default. Refuses a key or a value that is not allowed, and says what is.",
        _obj({"key": {"type": "string", "description": "the setting, e.g. code_review_engine"},
              "value": {"type": "string",
                        "description": "the new value; empty to use the deployment default"}},
             required=["key"]),
        kind="write",
        # UNDONE BY SETTING IT BACK, or by sending an empty value to return to the deployment
        # default. It starts nothing and spends nothing; the next run reads the new value.
        reversibility="safe",
    )
    def set_project_setting(ctx, *, project_id, actor, args):
        from ..services import project_settings as ps

        pid = args.get("project_id") or project_id
        key = str(args.get("key") or "").strip()
        value = args.get("value")
        with ctx.begin() as c:
            if key not in ps.KNOBS:
                return {"ok": False, "error": f"{key!r} is not a setting BOSS can change",
                        "available": sorted(ps.KNOBS)}
            spec = ps.KNOBS[key]
            if value in (None, ""):
                ps.clear_one(c, pid, key)
                return {"ok": True, "key": key, "value": None,
                        "note": (f"{spec['label']} now follows the deployment default "
                                 f"({getattr(ctx.settings, spec['attr'], None)})")}
            try:
                ps.set_one(c, pid, key, value, actor=actor)
            except Exception as exc:  # noqa: BLE001 — the bound's own message is the answer
                return {"ok": False, "key": key, "error": str(exc)[:300],
                        "offered": ps.offered_choices(c, ctx.settings, pid, spec)}
            return {"ok": True, "key": key, "value": value,
                    "note": f"{spec['label']} is now {value} for {pid}; applies on the next "
                            f"run, nothing to restart"}

    @reg.tool(
        "diagnose_setup",
        "EVERYTHING about whether this install is set up, in one call: which steps are done, "
        "the GitHub connection and the REASON its last test failed, which coding agents are "
        "installed/signed in/connected, which model answers chat and which builds, where "
        "secrets are stored, and what to do next. Use this FIRST for any question about "
        "setup, connecting a repository or a model, a token being rejected, or 'the check "
        "failed' — it is the whole picture and it names the next action. Read-only; no "
        "credential value is ever returned.",
        _obj({}),
        kind="read",
    )
    def diagnose_setup(ctx, *, project_id, actor, args):
        # WHY THIS EXISTS. Asked why a GitHub connection failed, the agent had to assemble the
        # answer from `query_db` over events, source inspection and three tools that do not
        # work in a container — and still could not say why, because the reason had been
        # discarded. Setup is the one thing every customer touches first; answering it should
        # not be an investigation.
        pid = args.get("project_id") or project_id
        from ..services.setup_service import (
            build_engine_for,
            chat_engine_for,
            connected_engines,
            list_coding_agents,
            setup_status,
            with_recorded_connections,
        )

        out: dict = {"project_id": pid}
        with ctx.begin() as c:
            try:
                out["steps"] = setup_status(c, ctx.settings, pid)
            except Exception as exc:  # noqa: BLE001 — a partial picture beats none
                out["steps_error"] = f"{type(exc).__name__}: {exc}"[:200]
            # THE REASON, not just the verdict. `last_error` is where the redacted message
            # lives, and reading it is the difference between "the check failed" and "GitHub
            # has no paramg/boss-sandbox visible to this token".
            try:
                out["github_connections"] = [dict(r) for r in c.execute(
                    _text("SELECT id, connection_label, auth_method, status, "
                          "       last_test_status, last_tested_at, last_error "
                          "  FROM github_connections WHERE project_id = :p"),
                    {"p": pid}).mappings().all()]
                out["repositories"] = [dict(r) for r in c.execute(
                    _text("SELECT full_name, role, status FROM github_repository_bindings "
                          " WHERE project_id = :p"), {"p": pid}).mappings().all()]
            except Exception as exc:  # noqa: BLE001
                out["github_error"] = f"{type(exc).__name__}: {exc}"[:200]
            try:
                agents = with_recorded_connections(
                    list_coding_agents(ctx.settings), c, pid)
                out["coding_agents"] = [
                    {k: a.get(k) for k in
                     ("name", "installed", "version", "authenticated", "auth_detail",
                      "ready", "connected", "connected_at", "next_step", "credential_kind")}
                    for a in agents]
                out["connected_engines"] = sorted(
                    connected_engines(c, ctx.settings, pid))
                # THE PROJECT'S CHOICES FIRST, or this reports the deployment default and
                # contradicts the console: `build_engine_for` reads settings.code_agent_engine,
                # which the pipeline layers before calling it.
                from ..services.project_settings import applied as _applied

                effective = _applied(c, pid, ctx.settings)
                out["chat_model"] = chat_engine_for(c, effective, pid) or "(the API key)"
                out["build_model"] = build_engine_for(c, effective, pid)
                out["role_models"] = {
                    "implementer": effective.code_agent_engine,
                    "adversarial_reviewer": effective.code_review_engine or "(the implementer)",
                    "hard_problems": effective.code_agent_strong_engine or "(the implementer)",
                    "routine_work": effective.code_agent_low_cost_engine or "(the implementer)",
                }
            except Exception as exc:  # noqa: BLE001
                out["agents_error"] = f"{type(exc).__name__}: {exc}"[:200]
        try:
            from ..services.secret_backends import default_secret_backend

            out["secret_store"] = default_secret_backend(ctx.settings)
        except Exception as exc:  # noqa: BLE001
            out["secret_store"] = f"unavailable ({type(exc).__name__})"

        # WHAT TO DO NEXT, in order, so the answer is an instruction rather than a dump.
        next_steps: list[str] = []
        for step in (out.get("steps") or {}).get("steps", []):
            if not step.get("done"):
                next_steps.append(f"{step['title']}: {step['detail']}")
        for conn in out.get("github_connections", []):
            if conn.get("last_test_status") == "failed":
                reason = (conn.get("last_error") or {}).get("message") or "no reason recorded"
                next_steps.append(
                    f"the GitHub connection {conn['id']} last failed its test: {reason}")
        # ONLY THE ONES SOMEBODY IS TRYING TO USE. Listing every CLI that could be installed
        # turned this into a to-do list of four vendors — "cursor: curl … | bash" is not a
        # next step for an operator who has never wanted Cursor. An agent that is installed
        # but not connected IS half-done and worth naming; so is having nothing at all.
        any_connected = bool(out.get("connected_engines"))
        for agent in out.get("coding_agents", []):
            if agent.get("connected") or not agent.get("next_step"):
                continue
            if agent.get("installed") or not any_connected:
                next_steps.append(f"{agent['name']}: {agent['next_step']}")
        out["next_steps"] = next_steps or ["nothing outstanding — this install is set up."]
        return out

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
        reversibility="safe",
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
        reversibility="safe",
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
        reversibility="safe",
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
        reversibility="irreversible",
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
        reversibility="destroys_work",
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
        reversibility="destroys_work",
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
        reversibility="safe",
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
        reversibility="safe",
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
        reversibility="safe",
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
        "detect_coding_agents",
        "Which coding-agent CLIs (Codex, Cursor, Aider, Claude Code) are installed on this "
        "machine and signed in. A subscription has no credential to paste — the CLI holds its "
        "own OAuth session — so this is how BOSS finds one. Reports the exact next command "
        "for anything not ready yet.",
        _obj({}, []),
    )
    def detect_coding_agents(ctx, *, project_id, actor, args):
        from ..services.setup_service import list_coding_agents

        agents = list_coding_agents(ctx.settings)
        return {"ok": True, "agents": agents,
                "ready": [a["name"] for a in agents if a["ready"]],
                "not_ready": [{"name": a["name"], "next_step": a["next_step"]}
                              for a in agents if not a["ready"]]}

    @reg.tool(
        "connect_coding_agent",
        "Import a coding-agent subscription the operator already has (e.g. Codex via their "
        "ChatGPT login) into BOSS: confirm the CLI is signed in, make it complete one real "
        "turn, and record the entitlement. Refuses rather than half-succeeds — signed in is "
        "not the same as working.",
        _obj({"provider": {"type": "string",
                           "description": "codex | cursor | aider"},
              "prove": {"type": "boolean", "default": True,
                        "description": "run one real turn before recording it (recommended)"}},
             ["provider"]),
        kind="write",
        reversibility="safe",
    )
    def connect_coding_agent(ctx, *, project_id, actor, args):
        from ..services.setup_service import SetupError, connect_coding_agent as _connect

        with ctx.begin() as c:
            try:
                return _connect(c, ctx.settings, project_id,
                                str(args.get("provider") or "").strip().lower(),
                                actor=actor, prove=bool(args.get("prove", True)))
            except SetupError as exc:
                # The next step IS the answer here, so it travels as a result an agent can
                # read and act on rather than an exception that loses it.
                return {"ok": False, "error": str(exc)}

    @reg.tool(
        "provider_audit",
        "Which model providers this project ACTUALLY called, from the llm_calls ledger "
        "rather than from configuration. Reports transport (the endpoint BOSS called) and "
        "vendor (who trained the weights) separately, because routing Claude through "
        "OpenRouter still sends your prompt to Anthropic. Pass `expect` to get a yes/no: "
        "'did anything reach a provider other than this one?'",
        _obj({"since_hours": {"type": "integer", "minimum": 1,
                              "description": "only calls in the last N hours"},
              "run_id": {"type": "string", "description": "scope to one code task run"},
              "expect": {"type": "string",
                         "description": "the only provider that should appear, e.g. 'openai'"},
              "dimension": {"type": "string", "enum": ["vendor", "transport"],
                            "default": "vendor",
                            "description": "vendor = whose model; transport = whose endpoint"}},
             []),
    )
    def provider_audit(ctx, *, project_id, actor, args):
        from ..services.provider_audit import audit_project

        with ctx.begin() as c:
            return audit_project(
                c, project_id,
                since_hours=args.get("since_hours"),
                run_id=args.get("run_id"),
                expect=args.get("expect"),
                dimension=(args.get("dimension") or "vendor"),
            )

    @reg.tool(
        "list_harnesses",
        "Every coding harness BOSS can drive — API lanes and CLI agents — with what each "
        "needs and whether a governed code task has actually completed on it. `verified: "
        "false` means wired but unproven, not broken.",
        _obj({}, []),
    )
    def list_harnesses(ctx, *, project_id, actor, args):
        from ..services.harness_registry import available_harnesses

        harnesses = available_harnesses()
        return {"ok": True, "count": len(harnesses), "harnesses": harnesses,
                "verified": [h["name"] for h in harnesses if h["verified"]],
                "unverified": [h["name"] for h in harnesses if not h["verified"]]}

    @reg.tool(
        "add_review_comment",
        "Leave a review comment on ONE line of a code task's change, the way you would on a "
        "pull request. The implementer gets your words AND the code at that line, so it does "
        "not have to work out where you meant. Use this instead of steer_code_task whenever "
        "the problem is somewhere specific — 'the error handling is weak' costs a repair round "
        "in re-reading; 'pricing/rules.py:14 returns 1.0 for an unknown SKU' does not.",
        _obj({"run_id": {"type": "string"},
              "path": {"type": "string", "description": "file the comment is about"},
              "line": {"type": "integer", "minimum": 1},
              "body": {"type": "string", "description": "what is wrong here"},
              "end_line": {"type": "integer", "minimum": 1,
                           "description": "for a comment about a span"},
              "severity": {"type": "string", "enum": ["blocker", "major", "minor", "nit"],
                           "default": "major",
                           "description": "blocker/major block the gate; minor/nit become "
                                          "follow-up work"}},
             ["run_id", "path", "line", "body"]),
        kind="write",
        reversibility="safe",
    )
    def add_review_comment(ctx, *, project_id, actor, args):
        import json as _json
        from ..services.core import make_id
        from ..services.review_comments import ReviewComment, AUTHOR_HUMAN

        body = (args.get("body") or "").strip()
        if not body:
            return {"ok": False, "error": "body is required"}
        comment = ReviewComment(
            severity=(args.get("severity") or "major").lower(),
            body=body[:500],
            path=(args.get("path") or "").strip() or None,
            line=int(args["line"]),
            end_line=int(args["end_line"]) if args.get("end_line") else None,
            author=actor,
            author_kind=AUTHOR_HUMAN,
            stage="operator",
        )
        entry = {"id": make_id("rc"), **comment.to_dict()}
        with ctx.begin() as c:
            # Same project fence as steering: a comment redirects an agent, so it must not
            # cross a tenant boundary.
            row = _code_task_in_project(c, run_id := args["run_id"], project_id)
            if not row:
                return {"ok": False, "error": f"no code task run {run_id}"}
            c.execute(_text(
                "UPDATE code_task_runs SET finalize_ctx = COALESCE(finalize_ctx,'{}'::jsonb) "
                "|| jsonb_build_object('review_comments', "
                "COALESCE(finalize_ctx->'review_comments','[]'::jsonb) || CAST(:e AS JSONB)), "
                "updated_at=now() WHERE id=:id AND project_id=:p"),
                {"e": _json.dumps([entry]), "id": run_id, "p": project_id})
        return {"ok": True, "comment_id": entry["id"], "run_id": run_id,
                "at": comment.location(),
                "applies": "at the next repair round; retry_code_task if the run has finished"}

    @reg.tool(
        "list_review_comments",
        "Every review comment on a code task — from the panel and from people — with its "
        "file, line, severity and whether it has been dealt with.",
        _obj({"run_id": {"type": "string"},
              "status": {"type": "string", "enum": ["open", "addressed", "dismissed", "all"],
                         "default": "all"}},
             ["run_id"]),
    )
    def list_review_comments(ctx, *, project_id, actor, args):
        from ..services.review_comments import parse_findings

        want = (args.get("status") or "all").lower()
        with ctx.begin() as c:
            row = _code_task_in_project(c, run_id := args["run_id"], project_id)
            if not row:
                return {"ok": False, "error": f"no code task run {run_id}"}
        fctx = row.get("finalize_ctx") or {}
        stored = list(fctx.get("review_comments") or [])
        # The panel's findings live in the run's evidence rather than as stored comments, so
        # they are parsed here. A reviewer's finding and an operator's comment are the same
        # kind of thing and an operator should not have to look in two places for them.
        evidence = (fctx.get("evidence") or {})
        for stage, key in (("review_code", "review_summary"),
                           ("security_check", "security_summary"),
                           ("consistency_check", "consistency_summary")):
            for c_ in parse_findings(evidence.get(key) or "", stage=stage):
                stored.append({"id": None, **c_.to_dict()})
        if want != "all":
            stored = [c_ for c_ in stored if (c_.get("status") or "open") == want]
        return {"ok": True, "run_id": run_id, "count": len(stored), "comments": stored}

    @reg.tool(
        "resolve_review_comment",
        "Mark a review comment addressed or dismissed. Dismissing records that it was a "
        "judgement call rather than making it disappear — a comment that vanishes is "
        "indistinguishable from one nobody read.",
        _obj({"run_id": {"type": "string"},
              "comment_id": {"type": "string"},
              "status": {"type": "string", "enum": ["addressed", "dismissed"]},
              "note": {"type": "string", "description": "why, for a dismissal"}},
             ["run_id", "comment_id", "status"]),
        kind="write",
        reversibility="safe",
    )
    def resolve_review_comment(ctx, *, project_id, actor, args):
        import json as _json

        status = (args.get("status") or "").lower()
        if status not in ("addressed", "dismissed"):
            return {"ok": False, "error": "status must be addressed or dismissed"}
        with ctx.begin() as c:
            row = _code_task_in_project(c, run_id := args["run_id"], project_id)
            if not row:
                return {"ok": False, "error": f"no code task run {run_id}"}
            fctx = row.get("finalize_ctx") or {}
            comments = list(fctx.get("review_comments") or [])
            target = args["comment_id"]
            hit = False
            for entry in comments:
                if entry.get("id") == target:
                    entry["status"] = status
                    entry["resolved_by"] = actor
                    if args.get("note"):
                        entry["resolution_note"] = str(args["note"])[:500]
                    hit = True
            if not hit:
                return {"ok": False, "error": f"no review comment {target} on {run_id}"}
            c.execute(_text(
                "UPDATE code_task_runs SET finalize_ctx = COALESCE(finalize_ctx,'{}'::jsonb) "
                "|| jsonb_build_object('review_comments', CAST(:e AS JSONB)), "
                "updated_at=now() WHERE id=:id AND project_id=:p"),
                {"e": _json.dumps(comments), "id": run_id, "p": project_id})
        return {"ok": True, "run_id": run_id, "comment_id": target, "status": status}

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
        reversibility="safe",
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


def _spec_from_steps(steps: Any, args: dict) -> Any:
    """A workflow spec from the plain list a model can actually produce.

    The runtime's spec format carries `next`, `on_failure`, binding snapshots and per-step
    approval blocks. Asking a model to emit that by hand is asking it to get a graph right in
    JSON, and the failures are silent — a mistyped `next` makes a workflow that stops after
    step one. So the model describes a SEQUENCE and this builds the graph, which is the part
    that can be got right once.
    """
    if not isinstance(steps, list) or not steps:
        return {"ok": False, "error": "steps must be a non-empty list"}
    if len(steps) > 24:
        return {"ok": False, "error": "24 steps is the most a workflow may have"}
    built: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(steps):
        if isinstance(raw, str):
            raw = {"capability": raw}
        if not isinstance(raw, dict):
            return {"ok": False, "error": f"step {index + 1} is not an object"}
        capability = str(raw.get("capability") or raw.get("kind") or "").strip().lower()
        if not capability:
            return {"ok": False, "error": f"step {index + 1} has no capability"}
        step_id = re.sub(r"[^a-z0-9_]+", "_",
                         str(raw.get("id") or capability).strip().lower()) or f"step_{index}"
        # A repeated id silently overwrites a step in the graph, so it is renamed rather than
        # refused: the operator asked for two previews and should get two.
        base = step_id
        suffix = 2
        while step_id in seen:
            step_id = f"{base}_{suffix}"
            suffix += 1
        seen.add(step_id)
        # THE RUNTIME'S OWN SHAPE, not a plausible-looking one. Every step is
        # kind="capability" with what it runs inside `binding` — a custom step names the step
        # key it defined, a standard one names the capability. Writing `kind: "preview"` would
        # have been refused by `validate_workflow_spec`, which is the good outcome; writing a
        # shape it ACCEPTS but the runtime reads differently is the bad one.
        entry: dict[str, Any] = {"id": step_id, "kind": "capability", "next": []}
        if raw.get("skill") or capability == "custom":
            entry["binding"] = {"custom_step": str(raw.get("skill") or "").strip().lower(),
                                "inputs": raw.get("input") or {}}
            # The binding names the step, not the capability — but the capability is where
            # `required_inputs` and `produces` are declared, so the wiring pass needs it. Held
            # under a leading underscore and stripped before the spec is published.
            entry["_capability"] = capability
        else:
            entry["binding"] = {"capability": capability, "inputs": raw.get("input") or {}}
        if raw.get("captain"):
            entry["captain"] = str(raw["captain"]).strip().lower()
        mode = str(raw.get("approval") or "").strip().lower()
        if mode in ("manual", "auto"):
            entry["approval"] = {"mode": mode}
        built.append(entry)
    # THE CHAIN, BUILT NOT ASKED FOR. `next` is a LIST in the runtime's format; a bare string
    # is accepted by the validator and normalised, but emitting the real shape means the spec
    # the agent wrote and the spec the builder draws are byte-comparable.
    for index, entry in enumerate(built[:-1]):
        entry["next"] = [built[index + 1]["id"]]

    # AND THE DATA FLOW, FOR THE SAME REASON THE CHAIN IS.
    #
    # A step declares required inputs; `video_edit` needs `media_artifact_id`. Nothing wired
    # them, so a described sequence published as "step 'bookend' needs 'media_artifact_id',
    # which is not set" — and the model, told only that, had no way to know the answer was a
    # reference to the step above rather than a value it should invent. It looped.
    #
    # Asking a model to emit {"from_step": "import", "field": "media_artifact_id"} is asking it
    # to get data flow right in JSON, which is the same class of thing as getting `next` right,
    # and it is exactly as mechanical: an input is satisfied by the NEAREST EARLIER STEP that
    # produces a field of that name, and otherwise by the run's own input.
    # What the OPERATOR supplies when they start this workflow. Declared, never guessed: see
    # `_wire_inputs`.
    declared = {str(x).strip() for x in (args.get("inputs") or []) if str(x).strip()}
    _wire_inputs(built, declared)
    for entry in built:
        entry.pop("_capability", None)

    # WHAT THE OPERATOR MUST SUPPLY, WRITTEN DOWN — otherwise the start form is blank.
    #
    # `inputs` was used to decide wiring and then dropped, so a published version carried
    # `input_schema: {}` while its steps referenced `from_input`. The bindings resolved and
    # the workflow was startable only by a caller that already knew the two field names; the
    # console's start form, which renders FROM the schema, had nothing to draw.
    #
    # Found by the Program Manager agent reading its own published spec back out of the
    # database rather than trusting the tool's success reply — which is the check that was
    # missing from every earlier attempt.
    #
    # Taken from what the wiring actually BOUND, not from what was asked for: an input the
    # model listed but no step consumes is not something to put on a form.
    required: dict[str, Any] = {}
    for entry in built:
        for value in ((entry.get("binding") or {}).get("inputs") or {}).values():
            if isinstance(value, dict) and value.get("from_input"):
                required[str(value["from_input"])] = "string"
    spec: dict[str, Any] = {"entry_step": built[0]["id"], "steps": built}
    if required:
        spec["input_schema"] = required
    return spec


def _wire_inputs(built: list[dict[str, Any]], declared: set[str]) -> None:
    """Point each step's unset required inputs at whatever upstream produces them."""
    caps = services_module("workflow_capabilities")
    palette = {x["key"]: x for x in caps.palette()}

    produced_by: dict[str, str] = {}
    for entry in built:
        binding = entry.get("binding") or {}
        # A custom step still names its capability, and that is where `produces` is declared —
        # so this works for a skill-bound step exactly as for a standard one.
        cap = palette.get(str(binding.get("capability") or entry.get("_capability") or ""))
        inputs = dict(binding.get("inputs") or {})
        for name in (cap or {}).get("required_inputs", ()) if cap else ():
            if inputs.get(name) not in (None, ""):
                continue
            source = produced_by.get(name)
            if source:
                inputs[name] = {"from_step": source, "field": name}
            elif name in declared:
                # The operator supplies it when they start the workflow. The first step of a
                # chain has no upstream by definition, so this is its only truthful wiring.
                inputs[name] = {"from_input": name}
            # OTHERWISE LEFT UNSET, DELIBERATELY.
            #
            # Wiring every unmatched input `from_input` would satisfy the validator and produce
            # a workflow that publishes cleanly and dies at run time on a value nobody supplies
            # — `run_id`, say, which is PRODUCED by a build step and is not something an
            # operator types. A spec the validator accepts but the runtime reads differently is
            # the worst outcome available here; refusing with "needs 'run_id', which is not
            # set" names the real design error.
        if inputs:
            binding["inputs"] = inputs
            entry["binding"] = binding
        for field in ((cap or {}).get("produces", ()) if cap else ()):
            produced_by[field] = str(entry["id"])


def _ensure_steps_for_skills(connection, project_id: str, spec: dict, actor: str) -> list[str]:
    """Define a custom step for any binding that names a skill but has no step yet.

    THE TWO OBJECTS, AND WHY CONFUSING THEM LOOKED LIKE AN UNRESPONSIVE BUTTON.
    A SKILL is the unit of doing — `video.bookend`, `publish_youtube`. A CUSTOM STEP is a
    workflow-shaped wrapper around one, with a label, inputs and a risk tier, so a graph can
    place it. A model asked for a workflow names skills, because that is what the work IS; the
    step is bookkeeping it has no reason to know about.

    Both kinds of skill count: `skill_definitions` holds what this project authored, and the
    entitlements catalogue holds what BOSS ships. Only the first was ever checked, so every
    built-in — the whole media pipeline — read as "this project does not have that skill".

    Returns the step keys it created, so the caller can say what it did.
    """
    steps_service = services_module("workflow_custom_steps")
    skills = services_module("skill_definitions")
    created: list[str] = []
    for entry in spec.get("steps", []):
        binding = entry.get("binding") or {}
        wanted = str(binding.get("custom_step") or "").strip()
        if not wanted:
            continue
        existing = connection.execute(
            _text("SELECT step_key FROM workflow_custom_steps "
                  "WHERE project_id = :p AND step_key = :k LIMIT 1"),
            {"p": project_id, "k": wanted},
        ).first()
        if existing:
            continue
        # Not a step. Is it a skill — authored, or one BOSS ships?
        source = skills._skill_exists(connection, project_id, wanted)
        if source is None:
            # Leave it. `_draft_problems` says which skills this project has, which is a more
            # useful sentence than anything invented here.
            continue
        # A step key cannot hold a dot; a skill id routinely does (`video.bookend`).
        step_key = re.sub(r"[^a-z0-9_]+", "_", wanted.lower()).strip("_")[:48]
        label = wanted.replace(".", " ").replace("_", " ").strip().title()
        try:
            steps_service.create(
                connection, project_id=project_id, actor=actor,
                step_key=step_key, label=label,
                summary=f"Runs the {wanted} skill.",
                binding_kind="skill", skill_id=wanted,
                inputs=[], produces=[], knowledge="",
            )
        except Exception:  # noqa: BLE001 — a step that will not define is a problem
            continue                                   # _draft_problems reports it properly
        binding["custom_step"] = step_key
        created.append(step_key)
    return created


def _draft_problems(connection, project_id: str, spec: dict, caps_by_key: dict,
                    caps_module: Any) -> list[str]:
    """Everything wrong with a drafted spec, in sentences, before anything is written.

    `publish_version` validates too — this is not a second opinion but an EARLIER one, so the
    agent can say "that step needs an input nothing produces" instead of creating a definition
    and then failing to publish into it.
    """
    problems: list[str] = []
    skills = services_module("skill_definitions")
    for entry in spec.get("steps", []):
        binding = entry.get("binding") or {}
        if binding.get("custom_step"):
            step_key = str(binding.get("custom_step") or "")
            # THE STEP, THEN THE SKILL BEHIND IT.
            #
            # This looked the step key up in `skill_definitions` directly — so a step whose key
            # differs from its skill id (every one BOSS defines, since a skill id holds dots
            # and a step key cannot) reported "this project does not have that skill" about a
            # step sitting in the table. Resolve the step first; only fall back to treating the
            # name as a skill id for a spec written by hand.
            row = connection.execute(
                _text("SELECT skill_id, binding_kind FROM workflow_custom_steps "
                      "WHERE project_id = :p AND step_key = :k LIMIT 1"),
                {"p": project_id, "k": step_key},
            ).mappings().first()
            skill_key = str((row or {}).get("skill_id") or step_key)
            skill = skills.get_skill(connection, project_id, skill_key)
            if skill is None and skills._skill_exists(connection, project_id, skill_key):
                # A skill BOSS SHIPS. `get_skill` reads only what this project authored, so
                # every built-in — the entire media pipeline — validated as missing.
                skill = {"skill_key": skill_key, "risk_tier": 1, "enabled": True,
                         "requires_approval": False, "trusted": True}
            problems.extend(caps_module.validate_custom_step(
                {**entry, "binding": {"skill_id": skill_key}}, skill))
            continue
        kind = str(binding.get("capability") or "")
        if kind not in caps_by_key:
            near = ", ".join(sorted(caps_by_key)[:8])
            problems.append(
                f"step '{entry.get('id')}' asks for '{kind}', which is not a capability. "
                f"Available kinds include: {near}.")
            continue
        capability = caps_by_key[kind]
        if (capability.get("approval_forced_manual")
                and str((entry.get("approval") or {}).get("mode", "")).lower() == "auto"):
            problems.append(
                f"step '{entry.get('id')}' is {kind}, which always requires a person — it "
                "cannot be set to auto.")
    return problems


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


#: Tables named in a statement: FROM x, JOIN y, INTO z. Deliberately loose — this drives a
#: HINT, so over-collecting costs a little output and under-collecting costs another turn.
_SQL_TABLES = re.compile(
    r"\b(?:FROM|JOIN|INTO|UPDATE)\s+([a-zA-Z_][a-zA-Z0-9_]*)", re.IGNORECASE)

#: Keywords that follow FROM/JOIN and are not tables.
_NOT_TABLES = {"select", "lateral", "only", "unnest", "generate_series", "values"}


def _schema_hint_for(ctx, sql: str) -> dict[str, Any]:
    """What the failed query SHOULD have said: the real tables and columns.

    Returned alongside a query_db failure so a wrong identifier is corrected in one step
    rather than discovered by elimination. See the call site for the thread this came from.
    """
    wanted = {
        t.lower() for t in _SQL_TABLES.findall(sql or "")
        if t.lower() not in _NOT_TABLES
    }
    if not wanted:
        return {}
    hint: dict[str, Any] = {}
    try:
        with ctx.begin() as c:
            existing = c.execute(
                _text("SELECT table_name, column_name FROM information_schema.columns "
                      " WHERE table_schema = current_schema() AND table_name = ANY(:names) "
                      " ORDER BY table_name, ordinal_position"),
                {"names": sorted(wanted)},
            ).mappings().all()
            columns: dict[str, list[str]] = {}
            for row in existing:
                columns.setdefault(row["table_name"], []).append(row["column_name"])
            if columns:
                hint["columns"] = columns
            missing = sorted(wanted - set(columns))
            if missing:
                # A TABLE THAT DOES NOT EXIST USUALLY HAS A NEIGHBOUR THAT DOES. `workflows`
                # is not a table; `workflow_definitions`, `workflow_versions` and
                # `workflow_runs` all are, and naming them ends the search immediately.
                near = c.execute(
                    _text("SELECT table_name FROM information_schema.tables "
                          " WHERE table_schema = current_schema() "
                          "   AND (" + " OR ".join(
                              f"table_name LIKE :p{i}" for i in range(len(missing))) + ") "
                          " ORDER BY table_name LIMIT 40"),
                    {f"p{i}": f"%{name}%" for i, name in enumerate(missing)},
                ).scalars().all()
                hint["no_such_table"] = missing
                hint["did_you_mean"] = list(near)
    except Exception:  # noqa: BLE001 — a hint that fails must not replace the real error
        return {}
    if hint:
        hint["hint"] = ("These are the REAL tables and columns. Correct the query from this "
                        "and re-run it once — do not guess another name.")
    return hint


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


#: Words a person puts in front of a role's name in a sentence, and never in the table.
_ROLE_NOISE = ("the ", "a ", "an ", "our ", "role ", "agent ")


def _role_name_candidates(wanted: str) -> list[str]:
    """Every spelling of a role name that should find the same row, most literal first.

    "the Deployment Manager", "Deployment Manager" and "deployment_manager" are one role. A
    tool that only accepts the third makes the model guess, and the guess costs a turn — which
    is exactly what happened: the first call failed and the retry worked.
    """
    raw = (wanted or "").strip()
    if not raw:
        return []
    out = [raw]
    lowered = raw.lower()
    for noise in _ROLE_NOISE:
        if lowered.startswith(noise):
            lowered = lowered[len(noise):].strip()
    if lowered and lowered not in out:
        out.append(lowered)
    slug = re.sub(r"[^a-z0-9]+", "_", lowered).strip("_")
    if slug and slug not in out:
        out.append(slug)
    spaced = slug.replace("_", " ")
    if spaced and spaced not in out:
        out.append(spaced)
    return out


def _repo_root(settings: Any, repo: str,
               ref: _Optional[str] = None) -> tuple[_Optional[str], _Optional[str]]:
    """The local path for `repo` at `ref`, cloning it if we do not have it yet.

    `repo` is "boss" for BOSS's own source, or "owner/name" (or bare "name", which
    resolves against the configured target owner) for a customer's application. The
    honest answer to "why does my app do X" is in the app's code, and until now no
    tool could open it — Ask BOSS could describe BOSS and only guess about the thing
    BOSS had built.

    `ref` EXISTS BECAUSE THE DEFAULT BRANCH IS THE WRONG PLACE TO LOOK. This cloned
    origin/HEAD, and an app BOSS builds lives on `boss/code/<run_id>` behind an open PR —
    unmerged, by design, because a human gates the merge. So a search for the app's own code
    found NOTHING on main, and the agent, having drawn a blank in the only place it could look,
    reached for container logs and reported that it could not determine the cause. It was
    looking at an empty repository.

    Each ref is cached in its own directory: checkouts are cheap next to a re-clone, and two
    questions about two runs must not fight over one working copy.
    """
    name = (repo or "boss").strip()
    if name in ("boss", "self", "platform", ""):
        return _SOURCE_ROOT, None

    owner, _, short = name.rpartition("/")
    owner = owner or (getattr(settings, "target_github_owner", "") or "")
    short = short or (getattr(settings, "target_github_repo", "") or "")
    if not owner or not short:
        return None, f"cannot resolve repo {repo!r} — no owner configured"

    ref = (ref or "").strip()
    slug = re.sub(r"[^0-9A-Za-z._-]+", "_", ref)[:80]
    dest = _os.path.join(_REPO_CACHE, f"{owner}__{short}" + (f"__{slug}" if slug else ""))
    try:
        _os.makedirs(_REPO_CACHE, exist_ok=True)
        from ..services.repo_host import clone_url_for

        url = clone_url_for(getattr(settings, "target_repo_host", "github") or "github",
                            owner, short, getattr(settings, "github_token", "") or "")
        if _os.path.isdir(_os.path.join(dest, ".git")):
            # Refresh, but never fail the read because the network is down — a slightly
            # stale checkout answers most questions; no checkout answers none.
            _subprocess.run(["git", "-C", dest, "fetch", "--depth", "1", "origin"]
                            + ([ref] if ref else []),
                            capture_output=True, timeout=120)
            _subprocess.run(["git", "-C", dest, "reset", "--hard",
                             "FETCH_HEAD" if ref else "origin/HEAD"],
                            capture_output=True, timeout=60)
        else:
            out = _subprocess.run(
                ["git", "clone", "--depth", "1"] + (["--branch", ref] if ref else [])
                + [url, dest], capture_output=True, text=True, timeout=300)
            if out.returncode != 0:
                # A ref that is a SHA, or a tag, cannot be cloned with --branch. Fall back to a
                # plain clone and fetch it, so "ref" means any ref rather than "branch only".
                if ref:
                    plain = _subprocess.run(["git", "clone", "--depth", "1", url, dest],
                                            capture_output=True, text=True, timeout=300)
                    if plain.returncode != 0:
                        return None, f"clone failed: {(plain.stderr or '')[-200:]}"
                    got = _subprocess.run(["git", "-C", dest, "fetch", "--depth", "1",
                                           "origin", ref], capture_output=True, text=True,
                                          timeout=120)
                    if got.returncode != 0:
                        return None, (f"no such ref {ref!r} in {owner}/{short} "
                                      f"({(got.stderr or '').strip()[-160:]})")
                    _subprocess.run(["git", "-C", dest, "reset", "--hard", "FETCH_HEAD"],
                                    capture_output=True, timeout=60)
                else:
                    return None, f"clone failed: {(out.stderr or '')[-200:]}"
        return dest, None
    except FileNotFoundError:
        return None, "git is not installed in this environment"
    except Exception as exc:  # noqa: BLE001 — a read tool must not raise
        return None, f"{type(exc).__name__}: {exc}"[:200]


def _source_for_run(ctx: Any, run_id: str) -> tuple[_Optional[str], _Optional[str],
                                                    _Optional[str]]:
    """(repo, ref, error) — WHERE THE CODE FOR THIS RUN ACTUALLY IS.

    An operator says "this error, on run crun_abc". The agent should not have to know that the
    code is on a branch named after some OTHER run, or that the PR is still open. It has the
    run id; that must be enough.

    The candidates, in order, are all checked against the remote rather than guessed at:
      * `boss/code/<run_id>` — the branch a code task normally writes to;
      * the run's own recorded base branch — a FOLLOW-UP task commits onto the branch of the
        run it continues, so its code is under the earlier run's name. That case is exactly the
        one that looks like a bug when you assume the convention;
      * the repository default, so the answer degrades to "merged code" rather than nothing.
    """
    try:
        with ctx.begin() as connection:
            row = connection.execute(
                _text("SELECT spec FROM code_task_runs WHERE id = :id"), {"id": run_id},
            ).mappings().first()
    except Exception as exc:  # noqa: BLE001 — a read tool must not raise
        return None, None, f"{type(exc).__name__}: {exc}"[:200]
    if row is None:
        return None, None, f"no code task run {run_id!r}"
    spec = row["spec"] if isinstance(row["spec"], dict) else {}
    owner = str(spec.get("owner") or "").strip()
    short = str(spec.get("repo") or "").strip()
    if not short:
        return None, None, f"run {run_id} records no repository"
    repo = f"{owner}/{short}" if owner else short

    candidates = [f"boss/code/{run_id}", str(spec.get("base_branch") or "").strip()]
    settings = getattr(ctx, "settings", None)
    try:
        from ..services.repo_host import clone_url_for

        url = clone_url_for(getattr(settings, "target_repo_host", "github") or "github",
                            owner or (getattr(settings, "target_github_owner", "") or ""),
                            short, getattr(settings, "github_token", "") or "")
        for candidate in [c for c in candidates if c]:
            probe = _subprocess.run(["git", "ls-remote", "--heads", url, candidate],
                                    capture_output=True, text=True, timeout=60)
            if probe.returncode == 0 and (probe.stdout or "").strip():
                return repo, candidate, None
    except Exception:  # noqa: BLE001 — fall through to the default branch
        pass
    # Nothing matched: read the default branch and say so, rather than failing the question.
    return repo, None, None


def _deepen(root: str, depth: int = 200) -> None:
    """Give a shallow checkout enough history to answer a question about the past.

    Best-effort by design: history is an ENRICHMENT of an answer that a stale or shallow
    checkout can still mostly give, so a failure here must never turn into a failed read.
    """
    try:
        if not _os.path.isfile(_os.path.join(root, ".git", "shallow")):
            return
        _subprocess.run(["git", "-C", root, "fetch", f"--depth={depth}"],
                        capture_output=True, timeout=120)
    except Exception:  # noqa: BLE001
        pass


def _resolve_source(ctx: Any, args: dict) -> tuple[str, _Optional[str], _Optional[str]]:
    """(repo, ref, error) for a source tool's arguments.

    One place, so search_code and read_code cannot disagree about which tree they are on —
    finding a line in one repo and then reading a different file from another is a worse
    failure than finding nothing.
    """
    run_id = str(args.get("run_id") or "").strip()
    if run_id:
        repo, ref, err = _source_for_run(ctx, run_id)
        if err:
            return "", None, err
        return repo or "boss", (args.get("ref") or ref or None), None
    return (args.get("repo") or "boss"), (args.get("ref") or None), None


def _safe_path(rel: str, root: _Optional[str] = None) -> _Optional[str]:
    """Resolve `rel` under `root` (default: BOSS's own source), or None if it escapes.

    Escape check is on the RESOLVED path, so `../../etc/passwd` and a symlink out
    are both caught — a prefix check on the raw string is not the same test.
    """
    base = root or _SOURCE_ROOT
    candidate = _os.path.realpath(_os.path.join(base, (rel or "").lstrip("/")))
    real = _os.path.realpath(base)
    return candidate if candidate == real or candidate.startswith(real + _os.sep) else None


def _register_debug_session_tools(reg: ToolRegistry) -> None:
    """Hand the problem to something that can actually debug it.

    Every tool in this registry answers a question somebody anticipated. That is why BOSS kept
    producing correct diagnoses and no fixes: it could describe a broken deployment precisely
    and could not open a shell, read the app's code, test a theory, or try a second one.

    `investigate` opens a real Claude Code session — a shell, the repo checked out, read-only
    SQL and a way onto the fleet — with a large turn budget and a requirement to end with
    evidence. It is the one tool here that does not have a fixed answer shape.
    """

    @reg.tool(
        "investigate",
        "Hand a problem to a full debugging session when the tools here cannot settle it: a "
        "shell, the app's repo checked out, read-only SQL over BOSS's database, SSH onto the "
        "fleet, and dozens of turns to form and TEST hypotheses. Use it when you have "
        "diagnosed something twice without fixing it, when the cause is probably in the app's "
        "code or on the machine rather than in a row, or when the operator asks you to work "
        "something out properly. It runs for a few minutes and posts the result into this "
        "conversation — say that you have started it and answer with what you know so far; do "
        "NOT sit and wait, and do not open a second one while the first is running. "
        "Read-only by default; pass fix=true only when the operator has asked for the problem "
        "to be fixed on the machine, which they will be asked to confirm.",
        _obj({"question": {"type": "string",
                           "description": "what to find out — be specific, include the ids you "
                                          "already have and what you have already ruled out"},
              "run_id": {"type": "string",
                         "description": "crun_ this is about, so the repo can be checked out"},
              "fix": {"type": "boolean",
                      "description": "allow it to CHANGE the host, not just look"}},
             required=["question"]),
        kind="write",
        reversibility="safe",
    )
    def investigate(ctx, *, project_id, actor, args):
        from ..services.debug_session import open_investigation

        wants_fix = bool(args.get("fix"))
        # WHO DECIDED, NOT WHICH FLAG WAS SET.
        #
        # This required `_operator_confirmed` — the marker only a button click sets — so a
        # fixing investigation always needed a click even when the operator had just said "do
        # anything to fix it". That is the same fault as the look-only gate refusing an
        # explicit instruction, one layer up, and it makes "applying the fixes" unreachable by
        # asking for it.
        #
        # The authority is already established before this line runs. A write tool is only
        # reached when the turn granted write permission, and the turn grants it only on an
        # INSTRUCTION — a question comes back as a button, unchanged. So an operator who told
        # BOSS to fix something gets a fixing investigation, and one who asked a question gets
        # an offer.
        #
        # What a fix investigation still cannot do is bounded elsewhere and does not move:
        # cancelling work in flight, deleting data, DDL, and the irreversible shell verbs are
        # refused in every mode, whoever asked.
        licensed = bool(args.get("_operator_confirmed") or args.get("_writes_allowed"))
        if wants_fix and not licensed:
            return {"ok": False, "requires_confirmation": True,
                    "error": "a fixing investigation gets a writable shell on the host; that "
                             "needs the operator's confirmation.",
                    "do_this": ("call propose_action with tool='investigate', these exact args "
                                "and a label saying what it will try to fix. Or run it "
                                "read-only by leaving fix unset — that answers most questions."),
                    "label": "Investigate and fix on the host",
                    "why": ("opens a debugging session with a writable shell on the machine; "
                            "read-only otherwise"),
                    "proposed_args": {**{k: v for k, v in args.items()
                                         if k in ("question", "run_id")}, "fix": True}}
        with ctx.begin() as c:
            return open_investigation(
                c, project_id=project_id, question=str(args.get("question") or ""),
                actor=actor, thread_id=args.get("_thread_id"),
                run_id=(str(args["run_id"]) if args.get("run_id") else None),
                allow_fix=wants_fix)

    @reg.tool(
        "factory_briefing",
        "Everything in the whole factory that wants a person right now — unanswered decisions, "
        "failed workflow steps with what each failure MEANS, apps that are deployed and not "
        "answering, and builds that have gone quiet. Call this FIRST when asked an open "
        "question about the system — 'what changed', 'what is blocked', 'what should I do "
        "next', 'how are we doing' — instead of assembling the same picture from six separate "
        "lookups. Read-only.",
        _obj({}),
        kind="read",
    )
    def factory_briefing(ctx, *, project_id, actor, args):
        from ..services.factory_briefing import factory_briefing as _briefing

        with ctx.begin() as c:
            return _briefing(c, project_id)

    @reg.tool(
        "describe_workflow",
        "WHAT A WORKFLOW WILL DO, before anyone runs it: every step in order with what it "
        "does, which agent captains it, where each input comes from, what it produces, and "
        "which steps stop for approval. Also says what the operator must supply to start it. "
        "Use this for 'what does this workflow do', 'what will it produce', 'who runs it', "
        "and ALWAYS before offering to start one — so the operator agrees to a plan rather "
        "than to a name.",
        _obj({"workflow_key": {"type": "string"},
              "version": {"type": "integer",
                          "description": "omit for the active version"},
              "project_id": {"type": "string"}},
             ["workflow_key"]),
        kind="read",
    )
    def describe_workflow(ctx, *, project_id, actor, args):
        from ..services.workflow_explain import WorkflowExplainError, explain_version

        pid = args.get("project_id") or project_id
        raw = args.get("version")
        try:
            with ctx.begin() as c:
                return {"ok": True, **explain_version(
                    c, project_id=pid,
                    workflow_key=str(args.get("workflow_key") or "").strip(),
                    version=int(raw) if raw not in (None, "") else None)}
        except WorkflowExplainError as error:
            return {"ok": False, "error": str(error)}

    @reg.tool(
        "remember",
        "Record something worth carrying into LATER sessions: a correction the operator gave "
        "you (kind='lesson'), a fact about how they like to work (kind='preference'), or "
        "context about this project (kind='project'). Use it the moment someone corrects you "
        "or tells you how things are done here — that is the only moment you have it. A "
        "lesson is a RULE, so it is recorded as 'proposed' and does not bind until a human "
        "reviews it; say so rather than claiming it is now in force.",
        _obj({"kind": {"type": "string", "enum": ["lesson", "preference", "project"]},
              "body": {"type": "string",
                       "description": "one or two sentences, in the imperative for a lesson: "
                                      "'always run the frontend checks before calling a "
                                      "change done'"},
              "scope": {"type": "string", "enum": ["org", "project", "actor", "role"],
                        "description": "default 'project'; 'actor' or 'role' narrows it"},
              "scope_ref": {"type": "string",
                            "description": "required for scope 'actor' or 'role'"},
              "project_id": {"type": "string"}},
             ["kind", "body"]),
        kind="write",
        reversibility="safe",
    )
    def remember(ctx, *, project_id, actor, args):
        """The write half of the memory plane.

        THE MOMENT A CORRECTION IS GIVEN IS THE ONLY MOMENT IT EXISTS. Before this tool, an
        operator saying "always check X first" changed that one turn and nothing else: the
        next thread started cold and they had to say it again. This is the gap the Kiro Crew
        analysis put first, on their own evidence that it is the step that pays.
        """
        from ..services.agent_memory import MemoryError_, remember as _remember

        pid = args.get("project_id") or project_id
        try:
            with ctx.begin() as c:
                row = _remember(
                    c, project_id=pid, actor=actor,
                    kind=str(args.get("kind") or "").strip(),
                    body=str(args.get("body") or ""),
                    scope=str(args.get("scope") or "project").strip(),
                    scope_ref=str(args.get("scope_ref") or "").strip() or None,
                    source_thread_id=(ctx.ambient or {}).get("_thread_id"),
                )
        except MemoryError_ as error:
            return {"ok": False, "error": str(error)}
        note = ("Recorded and in force from now on."
                if row["status"] == "active" else
                "Recorded as PROPOSED. It does not bind anything until a human reviews it — "
                "tell the operator it is waiting for review rather than saying it is done.")
        return {"ok": True, **row, "note": note}

    @reg.tool(
        "recall",
        "What this project already knows: the rules, context and preferences recorded in "
        "earlier sessions. You normally do NOT need this — active memory is already in your "
        "prompt. Use it to answer 'what do you know about me/this project', to check whether "
        "something is already recorded before recording it again, or to see what is waiting "
        "for review (include_proposed).",
        _obj({"kinds": {"type": "array", "items": {"type": "string"},
                        "description": "filter: lesson, preference, project"},
              "include_proposed": {"type": "boolean",
                                   "description": "also show what is awaiting review"},
              "project_id": {"type": "string"}}),
        kind="read",
    )
    def recall(ctx, *, project_id, actor, args):
        from ..services.agent_memory import recall as _recall

        pid = args.get("project_id") or project_id
        kinds = tuple(str(k) for k in (args.get("kinds") or []) if str(k).strip())
        with ctx.begin() as c:
            rows = _recall(c, project_id=pid, actor=actor,
                           role=(ctx.ambient or {}).get("_role"),
                           kinds=kinds or None,
                           include_proposed=bool(args.get("include_proposed")))
        return {"ok": True, "count": len(rows), "memories": [_json_safe(r) for r in rows],
                "note": ("nothing recorded yet for this project" if not rows else
                         "these are already in your prompt; do not read them back verbatim")}

    @reg.tool(
        "forget",
        "Retire a memory that is wrong or no longer true, by id (from recall). Retired, not "
        "deleted — a rule that shaped past answers stays auditable.",
        _obj({"memory_id": {"type": "string"}, "project_id": {"type": "string"}},
             ["memory_id"]),
        kind="write",
        reversibility="safe",
    )
    def forget(ctx, *, project_id, actor, args):
        from ..services.agent_memory import MemoryError_, forget as _forget

        pid = args.get("project_id") or project_id
        try:
            with ctx.begin() as c:
                return {"ok": True,
                        **_forget(c, project_id=pid,
                                  memory_id=str(args.get("memory_id") or "").strip(),
                                  actor=actor)}
        except MemoryError_ as error:
            return {"ok": False, "error": str(error)}

    @reg.tool(
        "update_ledger",
        "Record where THIS long-running session has got to: its goal, the phase it has "
        "reached, the concrete next step, and any approach you tried that did NOT work. "
        "Write it as intent, not status — 'add the Windows branch, the test already fails' "
        "tells a cold resume what to do; 'in progress' does not. Use it when a session will "
        "outlive one turn: a long investigation, a multi-step build, anything you would have "
        "to reconstruct after a restart. `rejected` APPENDS, and is the most valuable field: "
        "it stops a resumed session repeating work that already failed.",
        _obj({"goal": {"type": "string",
                       "description": "required the first time; what this session is for"},
              "phase": {"type": "string"},
              "next_step": {"type": "string"},
              "rejected": {"type": "string",
                           "description": "one approach that was tried and did not work"},
              "artifact_id": {"type": "string"},
              "finished": {"type": "boolean",
                           "description": "true when the work is done or abandoned, so this "
                                          "stops being re-injected into later turns"},
              "project_id": {"type": "string"}}),
        kind="write",
        reversibility="safe",
    )
    def update_ledger(ctx, *, project_id, actor, args):
        """The session's own memory of itself.

        A long thread has its early turns compacted away and the objective is usually the
        first thing to fall out — so the session re-derives it from a transcript that no
        longer contains it. This is what it reads instead.
        """
        from ..services.agent_memory import (
            MemoryError_,
            finish_ledger,
            read_ledger,
            write_ledger,
        )

        pid = args.get("project_id") or project_id
        thread_id = str((ctx.ambient or {}).get("_thread_id") or "").strip()
        if not thread_id:
            return {"ok": False,
                    "error": "no thread on this turn, so there is nothing to keep a ledger for"}
        try:
            with ctx.begin() as c:
                if args.get("finished"):
                    finish_ledger(c, thread_id=thread_id, actor=actor)
                    return {"ok": True, "finished": True,
                            "note": "ledger closed; it will not be re-injected"}
                row = write_ledger(
                    c, project_id=pid, thread_id=thread_id, actor=actor,
                    goal=str(args.get("goal") or "").strip() or None,
                    phase=str(args.get("phase") or "").strip() or None,
                    next_step=str(args.get("next_step") or "").strip() or None,
                    rejected=str(args.get("rejected") or "").strip() or None,
                    artifact_id=str(args.get("artifact_id") or "").strip() or None)
                _ = read_ledger(c, thread_id=thread_id)
        except MemoryError_ as error:
            return {"ok": False, "error": str(error)}
        return {"ok": True, **_json_safe(row),
                "note": "recorded; this is re-read at the start of later turns in this thread"}

    @reg.tool(
        "retry_workflow_step",
        "Put ONE failed, dead-lettered or cancelled workflow step back in the queue, keeping "
        "everything the run already produced. This is the console's \"Try … again\" button. "
        "Use it after describe_workflow_run shows a step failed for a reason that has since "
        "been dealt with — a missing credential now configured, a service now up. "
        "`recheck_workflow_run` is a different thing: it only re-tests deployment-target "
        "blockers and cannot recover a failed meeting or panel step.",
        _obj({"run_id": {"type": "string", "description": "the wfr_... the step belongs to"},
              "step_id": {"type": "string",
                          "description": "the step's id, e.g. 'meeting' — NOT its label"},
              "project_id": {"type": "string"}},
             ["run_id", "step_id"]),
        kind="write",
        reversibility="safe",
    )
    def retry_workflow_step(ctx, *, project_id, actor, args):
        """The recovery the agent could diagnose and could not perform.

        Asked why a panel step failed, BOSS traced it correctly — a 500 from the meeting job,
        deadlettered, swept as "abandoned", root-caused to meetings ignoring the configured
        model — and then said "tell me to go ahead and I will run it", offering no button,
        because there was no tool for the one action that follows. The operator reported it as
        "the panel retry endpoint currently has no exposed BOSS tool", which was exactly right.

        A correct diagnosis with no way to act on it is still a dead end. The console has had
        this button the whole time; this is the same operation, reachable by the agent.
        """
        from ..services.core import emit_event

        pid = args.get("project_id") or project_id
        run_id = str(args.get("run_id") or "").strip()
        step_id = str(args.get("step_id") or "").strip()
        if not run_id or not step_id:
            return {"ok": False, "error": "run_id and step_id are required"}
        with ctx.begin() as c:
            row = c.execute(
                _text("SELECT id, status FROM workflow_step_runs "
                      " WHERE workflow_run_id=:r AND project_id=:p AND step_id=:s "
                      " ORDER BY created_at DESC LIMIT 1"),
                {"r": run_id, "p": pid, "s": step_id}).mappings().first()
            if not row:
                # NAME WHAT IS THERE. A step is addressed by id ('meeting'), and the console
                # shows its LABEL ('Convene a panel') — so the obvious guess is the wrong one,
                # and "step not found" sends the reader hunting for a typo they did not make.
                available = c.execute(
                    _text("SELECT step_id, status FROM workflow_step_runs "
                          " WHERE workflow_run_id=:r AND project_id=:p ORDER BY created_at"),
                    {"r": run_id, "p": pid}).mappings().all()
                return {"ok": False,
                        "error": f"no step {step_id!r} on {run_id}",
                        "steps_on_this_run": [
                            {"step_id": s["step_id"], "status": s["status"]}
                            for s in available]}
            if row["status"] not in ("failed", "deadlettered", "cancelled"):
                # Re-queueing a live step would run it twice.
                return {"ok": False,
                        "error": (f"'{step_id}' is {row['status']}, so there is nothing to "
                                  "retry.")}
            c.execute(
                _text("UPDATE workflow_step_runs SET status='ready', attempt=0, "
                      "       last_error=NULL, lease_owner=NULL, lease_expires_at=NULL, "
                      "       updated_at=now() WHERE id=:id"),
                {"id": row["id"]})
            # The RUN has to come back too, or a re-queued step sits ready under a dead run.
            c.execute(
                _text("UPDATE workflow_runs SET status='running', blocked_reason=NULL, "
                      "       error=NULL, updated_at=now() WHERE id=:r AND project_id=:p"),
                {"r": run_id, "p": pid})
            emit_event(c, pid, "workflow.step_retried", actor, "workflow_run", run_id,
                       {"step_id": step_id, "was": row["status"]})
        return {"ok": True, "run_id": run_id, "step_id": step_id, "status": "ready",
                "was": row["status"],
                "note": ("Re-queued. The worker picks it up on its next sweep; everything the "
                         "run already produced is kept. Check back with "
                         "describe_workflow_run.")}

    @reg.tool(
        "describe_workflow_run",
        "THE ONE CALL for 'what happened to this workflow run' (wfr_...): its status, the "
        "inputs it was started with, every step with attempts and its error, and — when it "
        "failed — WHAT TO DO ABOUT IT. Use this instead of querying workflow_runs or "
        "workflow_step_runs by hand. `diagnose_run` is for code-task runs (crun_) and does "
        "not accept a wfr_.",
        _obj({"run_id": {"type": "string", "description": "the wfr_... to describe"},
              "project_id": {"type": "string"}},
             ["run_id"]),
        kind="read",
    )
    def describe_workflow_run(ctx, *, project_id, actor, args):
        """A workflow run, and the way out of it.

        THERE WAS NO READ TOOL FOR A WORKFLOW RUN AT ALL.
        -------------------------------------------------
        `diagnose_run` takes a crun_. Asked "what is the status?" about a wfr_, the agent had
        nothing to call, so it wrote SQL — and spent seven tool calls in one turn discovering
        that `workflows`, `work_class`, `workflow_key`, `graph`, `run_id` and `priority_band`
        are not real. The operator got an apology about SQL instead of a status.

        And the half that matters more: a deadlettered run was reported as a fact and left
        there. "It is erroring out but there's no option or solution given to the user." A
        run that stopped because an input was never supplied has an obvious remedy — supply
        it — and nothing was computing that remedy, so nobody offered it.
        """
        run_id = str(args.get("run_id") or "").strip()
        pid = args.get("project_id") or project_id
        if not run_id:
            return {"ok": False, "error": "run_id is required"}
        if run_id.startswith("crun_"):
            return {"ok": False,
                    "error": f"{run_id} is a code-task run; call diagnose_run for those."}
        with ctx.begin() as c:
            run = c.execute(
                _text("SELECT r.id, r.status, r.actor, r.input, r.output, r.error, "
                      "       r.blocked_reason, r.created_at, r.completed_at, "
                      "       d.workflow_key, d.name, v.version, v.spec "
                      "  FROM workflow_runs r "
                      "  JOIN workflow_definitions d ON d.id = r.workflow_definition_id "
                      "  JOIN workflow_versions v ON v.id = r.workflow_version_id "
                      " WHERE r.id = :id AND r.project_id = :p"),
                {"id": run_id, "p": pid}).mappings().first()
            if run is None:
                return {"ok": False, "error": f"no workflow run {run_id!r} on {pid}"}
            steps = c.execute(
                # `last_error`, NOT `error` — workflow_runs has `error` and workflow_step_runs
                # has `last_error`, and this shipped with the wrong one. Caught in production
                # by the schema hint added in the same commit, which is the point of it.
                _text("SELECT step_id, step_kind, status, attempt, max_attempts, "
                      "       last_error, input, output, updated_at "
                      "  FROM workflow_step_runs WHERE workflow_run_id = :id "
                      " ORDER BY created_at, step_id"),
                {"id": run_id}).mappings().all()

        spec = run["spec"] if isinstance(run["spec"], dict) else {}
        declared = list((spec.get("input_schema") or {}).keys())
        supplied = list((run["input"] or {}).keys())
        missing = [k for k in declared if k not in supplied]
        stopped = [dict(s) for s in steps
                   if s["status"] in ("failed", "deadlettered", "cancelled")]

        # WHAT TO DO, COMPUTED — not left for the reader to infer from an error string.
        remedy: dict[str, Any] = {}
        if run["status"] in ("failed", "deadlettered", "cancelled"):
            if missing:
                remedy = {
                    "diagnosis": (
                        f"This run was started without {', '.join(repr(m) for m in missing)}, "
                        f"which {run['workflow_key']} v{run['version']} declares as required."),
                    "do_this": (
                        f"Start it again WITH those inputs: start_workflow with "
                        f"workflow_key='{run['workflow_key']}' and an `input` object "
                        f"containing {', '.join(missing)}. Offer that to the operator as a "
                        f"button, asking them for any value you do not have."),
                    "needs_from_operator": missing,
                    # Named so an agent does not reach for write_db to "fix" a dead run.
                    "do_not": "Do not edit workflow_runs directly; start a new run instead.",
                }
            elif stopped:
                remedy = {
                    "diagnosis": (
                        f"Step {stopped[0]['step_id']!r} stopped: "
                        f"{stopped[0].get('last_error') or 'no error recorded'}"),
                    "do_this": (
                        "If the cause has since been dealt with, recheck_workflow_run re-tests "
                        "the recorded blocker and re-queues only what can now proceed. If it "
                        "has not, say what the operator must change first."),
                }
        return {
            "ok": True,
            "run_id": run["id"],
            "workflow": {"key": run["workflow_key"], "name": run["name"],
                         "version": run["version"]},
            "status": run["status"],
            "error": run["error"],
            "blocked_reason": run["blocked_reason"],
            "started_by": run["actor"],
            "started_at": _json_safe(run["created_at"]),
            "completed_at": _json_safe(run["completed_at"]),
            "declared_inputs": declared,
            "supplied_inputs": supplied,
            "missing_inputs": missing,
            "steps": [_json_safe(dict(s)) for s in steps],
            **({"remedy": remedy} if remedy else {}),
        }

    @reg.tool(
        "recheck_workflow_run",
        "Re-test what stopped a workflow run and put back whatever can now proceed. Use it "
        "when the thing a step complained about has since been dealt with — a deployment "
        "target that did not exist and now does. A failed step's error is a record of a past "
        "attempt, not a statement about now, so this tests the recorded blocker against the "
        "world and re-queues only the steps whose blocker has actually cleared. It never "
        "re-runs a step whose reason still holds, and it says which is which.",
        _obj({"run_id": {"type": "string", "description": "the wfr_... to re-check"},
              "requeue": {"type": "boolean",
                          "description": "false to report without changing anything"}},
             required=["run_id"]),
        kind="write",
        reversibility="safe",
    )
    def recheck_workflow_run(ctx, *, project_id, actor, args):
        from ..services.workflow_recheck import recheck_run

        with ctx.begin() as c:
            return recheck_run(c, project_id=project_id, run_id=str(args["run_id"]),
                               requeue=bool(args.get("requeue", True)))

    @reg.tool(
        "get_investigation",
        "The result of an investigation: what it found, what it ran to prove it, the fix, and "
        "whether it applied one. Call this when an investigation was started earlier in the "
        "conversation — while it is still running, say so and answer with what you have.",
        _obj({"investigation_id": {"type": "string",
                                   "description": "optional — defaults to the latest"}}),
        kind="read",
    )
    def get_investigation(ctx, *, project_id, actor, args):
        from ..services.debug_session import get_investigation as _get

        with ctx.begin() as c:
            return _get(c, project_id=project_id,
                        investigation_id=str(args.get("investigation_id") or ""))


def _register_diagnosis_tools(reg: ToolRegistry) -> None:
    """The investigation, performed in code rather than chosen by the model each time.

    The ordering — what the work did, then the logs, then the machine — lived in prose in the
    system prompt, which is guidance. Asked why a build was slow, BOSS read the job queue and
    the container, found nothing starved, concluded the phase was stalled and cancelled it,
    while 101 recorded turns said the engineer had worked normally for nine minutes and then
    issued one command that never returned.

    A prompt can be skipped under pressure and a routine cannot. This runs the sequence, adds
    the comparison that makes "27 minutes" mean something, and says plainly what it could NOT
    check — so an answer built on it cannot quietly omit the step it never took.
    """
    @reg.tool(
        "diagnose_run",
        "THE ONE CALL for 'why is this stuck / slow / failing?'. Runs the whole investigation "
        "in the right order and returns it as evidence: what the work actually did (its "
        "turns), how long it has been silent, the last command that never returned, the phase "
        "breakdown, and — crucially — how this run's duration COMPARES to other builds on this "
        "project, so a normal build is not called a stall. Also reports what could not be "
        "checked. Prefer this over assembling the same picture yourself from four tools.",
        _obj({"run_id": {"type": "string", "description": "the code-task run (crun_...)"},
              "project_id": {"type": "string"}},
             ["run_id"]),
        kind="read",
    )
    def diagnose_run(ctx, *, project_id, actor, args):
        run_id = str(args.get("run_id") or "").strip()
        pid = args.get("project_id") or project_id
        if not run_id:
            return {"ok": False, "error": "run_id is required"}

        checked: list[str] = []
        could_not_check: list[str] = []
        findings: list[str] = []

        with ctx.begin() as c:
            run = c.execute(
                _text("SELECT id, project_id, title, status, phase, error, created_at, "
                      "updated_at, EXTRACT(EPOCH FROM (coalesce(updated_at, now()) "
                      "  - created_at))/60 AS minutes "
                      "FROM code_task_runs WHERE id = :r"),
                {"r": run_id}).mappings().first()
            if run is None:
                return {"ok": False, "error": f"no run {run_id} on this platform"}
            pid = run["project_id"] or pid
            checked.append("the run row")

            turns = c.execute(
                _text("SELECT count(*) AS n, max(created_at) AS last_turn, "
                      "min(created_at) AS first_turn, "
                      "count(*) FILTER (WHERE direction = 'tool_call') AS calls, "
                      "count(*) FILTER (WHERE direction = 'tool_result') AS results, "
                      "count(*) FILTER (WHERE ok = false) AS failures, "
                      "EXTRACT(EPOCH FROM (now() - max(created_at))) AS silent "
                      "FROM agent_turns WHERE code_task_run_id = :r"),
                {"r": run_id}).mappings().first()

            last_call = None
            if turns and turns["n"]:
                checked.append(f"{turns['n']} recorded turns")
                # THE COMMAND THAT NEVER CAME BACK. A tool_call with no tool_result after it is
                # where the work stopped, and it is the single most useful row in the table.
                last_call = c.execute(
                    _text("SELECT turn_index, tool_name, left(coalesce(content,''), 300) AS args, "
                          "created_at FROM agent_turns "
                          "WHERE code_task_run_id = :r AND direction = 'tool_call' "
                          "ORDER BY created_at DESC LIMIT 1"),
                    {"r": run_id}).mappings().first()
            else:
                could_not_check.append(
                    "the turn-by-turn record — nothing was captured for this run")

            # WHAT NORMAL LOOKS LIKE HERE. "27 minutes" is not a fact about a build, it is a
            # comparison, and BOSS called 28 minutes a stall on a project whose builds took
            # 13, 14, 22, 55, 56, 66 and 82.
            peers = c.execute(
                _text("SELECT EXTRACT(EPOCH FROM (updated_at - created_at))/60 AS minutes "
                      "FROM code_task_runs "
                      "WHERE project_id = :p AND id <> :r AND status = 'completed' "
                      "  AND updated_at IS NOT NULL "
                      "ORDER BY created_at DESC LIMIT 30"),
                {"p": pid, "r": run_id}).scalars().all()
            durations = sorted(float(m) for m in peers if m is not None)

        minutes = float(run["minutes"] or 0)
        baseline = None
        if durations:
            checked.append(f"{len(durations)} previous completed runs on this project")
            mid = durations[len(durations) // 2]
            p90 = durations[max(0, int(len(durations) * 0.9) - 1)]
            baseline = {"median_minutes": round(mid, 1), "p90_minutes": round(p90, 1),
                        "sample": len(durations),
                        "longest_minutes": round(durations[-1], 1)}
            if minutes <= p90:
                findings.append(
                    f"{minutes:.0f} minutes is NOT unusual here — the median is {mid:.0f} and "
                    f"the slowest completed run took {durations[-1]:.0f}. Do not call this a "
                    "stall on duration alone.")
            else:
                findings.append(
                    f"{minutes:.0f} minutes is beyond this project's p90 of {p90:.0f} "
                    f"(median {mid:.0f}, n={len(durations)}).")
        else:
            could_not_check.append(
                "how long this project's other builds take — none have completed yet, so there "
                "is no baseline and no duration here can be called abnormal")

        silent = int((turns or {}).get("silent") or 0)
        running = str(run["status"]) in ("running", "queued")
        if turns and turns["n"] and running and silent > 300:
            findings.append(
                f"NO TURN RECORDED FOR {silent // 60} MINUTES while the run is still "
                f"'{run['status']}'. This is not slow work — it has gone quiet.")
        calls, results = int((turns or {}).get("calls") or 0), int((turns or {}).get("results") or 0)
        if calls > results:
            findings.append(
                f"{calls - results} tool call(s) never produced a result. The last one is where "
                "it stopped.")
        if run["error"]:
            findings.append(f"the run records an error: {str(run['error'])[:200]}")

        # THE ANSWER ENDS WITH THE FIX, NOT WITH THE DIAGNOSIS.
        #
        # "Your build hung on npm run verify" is a good explanation and leaves the operator
        # exactly where they started. The resolution for this state is known here, with real
        # arguments, so it is handed over rather than left to be worked out — the model offers
        # it as a button and the operator is one click from done.
        resolution = None
        status = str(run["status"] or "")
        if status in ("cancelled", "failed", "blocked", "error"):
            resolution = {
                "tool": "retry_code_task",
                "args": {"run_id": run_id},
                "label": f"Retry {run['title'] or run_id}",
                "why": f"this run is '{status}'; retrying resumes the same lineage, reusing the "
                       "branch and the code already written rather than starting again",
            }
        elif running and silent > 300:
            resolution = {
                "tool": "cancel_code_task",
                "args": {"run_id": run_id},
                "label": f"Stop {run['title'] or run_id}",
                "why": "it has produced nothing for "
                       f"{silent // 60} minutes; stopping it frees the slot — but this throws "
                       "away the work done so far, so it is the operator's call",
            }

        return {
            "run_id": run_id, "project_id": pid, "title": run["title"],
            "status": run["status"], "phase": run["phase"],
            "resolution": resolution,
            "running_minutes": round(minutes, 1),
            "normal_for_this_project": baseline,
            "turns": int((turns or {}).get("n") or 0),
            "silent_for_seconds": silent,
            "last_tool_call": (
                {**dict(last_call), "created_at": last_call["created_at"].isoformat()}
                if last_call else None),
            "findings": findings,
            "checked": checked,
            "could_not_check": could_not_check,
            "next": (
                "If findings name a silent period or an unmatched tool call, the last_tool_call "
                "above is the cause — read it and say so. If nothing here explains it, call "
                "read_logs(service='worker') and explain_run BEFORE concluding anything, and "
                "say in your answer which of those you read. "
                "If `resolution` is set, END YOUR ANSWER by offering exactly that with "
                "propose_action, so the operator is one click from done — do not describe the "
                "fix in prose and leave them to ask for it."),
        }


def _register_port_tools(reg: ToolRegistry) -> None:
    """Choosing a port, without guessing.

    From the transcript: "the host already has port 8000 in use, so the container cannot bind.
    I verified the host's listening sockets and found port 8000 occupied, while port 8001 is
    free." Correct — and reached by reading raw socket output and eyeballing it. The next port
    up is a guess that happened to be right; the one after it might be a neighbour's.

    Two sources have to agree for a port to be genuinely free: nothing is listening on the host,
    and BOSS's own registry has not promised it to another app. Either alone produces a
    collision — a port that is free right now but assigned to an app that is between deploys is
    not free.
    """
    @reg.tool(
        "find_free_port",
        "A port on this host that is actually free — nothing listening AND not already assigned "
        "to another app in BOSS's registry. Use before retarget_app instead of picking the next "
        "number up: 'is 8001 free' needs both answers, and a port that is idle right now but "
        "promised to an app between deploys is not free.",
        _obj({"host": {"type": "string", "description": "address, e.g. 2.29.2.202"},
              "start": {"type": "integer", "description": "first port to consider (default 3000)"},
              "count": {"type": "integer", "description": "how many to report (default 3)"},
              "project_id": {"type": "string"}},
             ["host"]),
        kind="read",
    )
    def find_free_port(ctx, *, project_id, actor, args):
        import socket

        host = str(args.get("host") or "").strip()
        if not host:
            return {"ok": False, "error": "host is required"}
        start = max(1024, int(args.get("start") or 3000))
        want = max(1, min(int(args.get("count") or 3), 10))
        pid = args.get("project_id") or project_id

        with ctx.begin() as c:
            rows = c.execute(
                _text("SELECT app_name, domains FROM deployment_targets "
                      "WHERE status = 'active'"),
            ).mappings().all()
        # Every port BOSS has promised on this host, whatever project owns it: a collision with
        # another project's app is still a collision.
        assigned: dict[int, str] = {}
        for row in rows:
            for domain in (row["domains"] or []):
                head, _, tail = str(domain).rpartition(":")
                if tail.isdigit() and head == host:
                    assigned[int(tail)] = row["app_name"]

        free: list[int] = []
        checked = 0
        port = start
        while len(free) < want and checked < 200 and port < 65535:
            checked += 1
            if port in assigned:
                port += 1
                continue
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(2.0)
            try:
                sock.connect((host, port))
                listening = True          # something answered: not free
            except OSError:
                listening = False
            finally:
                sock.close()
            if not listening:
                free.append(port)
            port += 1

        return {
            "host": host, "project_id": pid,
            "free_ports": free,
            "assigned_on_this_host": [{"port": k, "app": v} for k, v in sorted(assigned.items())],
            "scanned": checked,
            "note": ("A port is reported free only when nothing answered AND BOSS has not "
                     "assigned it. If free_ports is empty, widen `start` rather than picking "
                     "one anyway."
                     if free else
                     "Nothing free in the range scanned — try a higher `start`."),
        }


def _register_run_turn_tools(reg: ToolRegistry) -> None:
    """The turn-by-turn record of what a build was actually doing.

    A build was reported "stuck for 27m". BOSS called get_code_task, find_starved_work and
    inspect_self, concluded the implement phase was stalled, and cancelled it. The answer was
    sitting in `agent_turns` — 101 rows for that run — and no tool read that table. Asked again
    afterwards it reached for `get_run_activity`, which reads `llm_calls`, which had zero rows
    for the run, and reported that no detail existed.

    So the richest record BOSS keeps was unreachable except by hand-written SQL, which is why a
    question about progress got answered from queue statistics instead of from what the engineer
    was doing. What it was doing: nine and a half minutes of ordinary work, then one command
    that never returned.
    """
    @reg.tool(
        "read_run_turns",
        "THE FIRST THING TO READ when asked about a build's progress, why it is slow, or "
        "whether it is stuck. The turn-by-turn record of what the coding agent actually did: "
        "each prompt, completion, tool call and tool result, with timings. Also reports "
        "SILENCE — how long since the last recorded turn — which is what distinguishes a run "
        "that is working from one that has hung. Read this BEFORE queue or host statistics: "
        "those describe the machine, this describes the work.",
        _obj({"run_id": {"type": "string", "description": "the code-task run (crun_...)"},
              "limit": {"type": "integer", "description": "most recent N turns (default 40)"},
              "stage": {"type": "string", "description": "only this stage, e.g. implement"},
              "project_id": {"type": "string"}},
             ["run_id"]),
        kind="read",
    )
    def read_run_turns(ctx, *, project_id, actor, args):
        run_id = str(args.get("run_id") or "").strip()
        if not run_id:
            return {"ok": False, "error": "run_id is required"}
        limit = max(1, min(int(args.get("limit") or 40), 200))
        stage = str(args.get("stage") or "").strip()
        with ctx.begin() as c:
            summary = c.execute(
                _text("SELECT count(*) AS turns, min(created_at) AS first_turn, "
                      "max(created_at) AS last_turn, "
                      "count(*) FILTER (WHERE direction = 'tool_call') AS tool_calls, "
                      "count(*) FILTER (WHERE direction = 'tool_result') AS tool_results, "
                      "count(*) FILTER (WHERE ok = false) AS failures, "
                      "EXTRACT(EPOCH FROM (now() - max(created_at))) AS silent_seconds "
                      "FROM agent_turns WHERE code_task_run_id = :r"),
                {"r": run_id}).mappings().first()
            run = c.execute(
                _text("SELECT status, phase FROM code_task_runs WHERE id = :r"),
                {"r": run_id}).mappings().first()
            rows = c.execute(
                _text("SELECT turn_index, direction, stage, role_key, tool_name, latency_ms, "
                      "ok, error, truncated, left(coalesce(content, ''), 400) AS content, "
                      "created_at FROM agent_turns WHERE code_task_run_id = :r "
                      "  AND (:stage = '' OR stage = :stage) "
                      "ORDER BY created_at DESC, turn_index DESC LIMIT :lim"),
                {"r": run_id, "stage": stage, "lim": limit}).mappings().all()

        if not summary or not summary["turns"]:
            return {"run_id": run_id, "turns": 0,
                    "note": "nothing captured for this run. It may not have started, or "
                            "capture may have been off. Try read_logs for the service."}

        calls = int(summary["tool_calls"] or 0)
        results = int(summary["tool_results"] or 0)
        silent = int(summary["silent_seconds"] or 0)
        running = str((run or {}).get("status") or "") in ("running", "queued")
        # THE ONE NUMBER THAT ANSWERS "IS IT STUCK?".
        #
        # A run that is working produces turns. Its last turn being 18 minutes old while its
        # status is still `running` is not a slow build — it is a build that stopped saying
        # anything, which is a different problem with a different cause. Stated as a finding
        # rather than left for the reader to subtract two timestamps.
        findings = []
        if running and silent > 300:
            findings.append(
                f"NO TURN RECORDED FOR {silent // 60}m while the run is still '{run['status']}' "
                f"in phase '{run.get('phase')}'. It is not working slowly; it has gone quiet. "
                "Look at the last tool_call below — a command that never returned is the usual "
                "cause.")
        if calls > results:
            findings.append(
                f"{calls - results} tool call(s) never produced a result. The last one is where "
                "it stopped; a command whose output pipe is held open by a process it started "
                "will hang past its own timeout.")
        if int(summary["failures"] or 0):
            findings.append(f"{summary['failures']} turn(s) recorded an error.")

        return {
            "run_id": run_id,
            "status": (run or {}).get("status"),
            "phase": (run or {}).get("phase"),
            "turns": int(summary["turns"]),
            "tool_calls": calls,
            "tool_results": results,
            "first_turn": summary["first_turn"].isoformat() if summary["first_turn"] else None,
            "last_turn": summary["last_turn"].isoformat() if summary["last_turn"] else None,
            "silent_for_seconds": silent,
            "findings": findings,
            "recent_turns": [
                {**{k: v for k, v in dict(row).items() if k != "created_at"},
                 "created_at": row["created_at"].isoformat() if row["created_at"] else None}
                for row in rows
            ],
            "note": "newest first. `direction` is prompt | completion | tool_call | tool_result "
                    "— a tool_call with no matching tool_result is a command that never came "
                    "back.",
        }


def _register_standard_tools(reg: ToolRegistry) -> None:
    """Let a role turn a conclusion into a rule that binds future builds.

    The Security role can sit in every panel, conclude that a port must be checked for public
    exposure before it is assigned, say so — and have it apply to nothing. The conclusion lived
    in one meeting's minutes; the next build never saw it, because standards were Python
    constants and only the platform could add one.

    A mandated standard is injected into every pipeline stage of every matching build, exactly
    like the hardcoded ones, and can be scoped to one project or to all of them.
    """
    @reg.tool(
        "list_standards",
        "The standards binding on builds here: the platform's own, plus any this workforce "
        "has mandated, with who mandated each and why. Read before proposing a new one — a "
        "second rule saying the same thing teaches agents that standards are noise.",
        _obj({"project_id": {"type": "string"}}),
        kind="read",
    )
    def list_standards(ctx, *, project_id, actor, args):
        pid = args.get("project_id") or project_id
        with ctx.begin() as c:
            rows = c.execute(
                _text("SELECT id, project_id, standard_key, title, scope, mandated_by_role, "
                      "rationale, status, created_at FROM engineering_standards "
                      "WHERE status = 'active' AND (project_id IS NULL OR project_id = :p) "
                      "ORDER BY project_id NULLS FIRST, created_at"),
                {"p": pid}).mappings().all()
        return {
            "project_id": pid,
            "platform_standards": ["observability (metrics, health, route cardinality)"],
            "mandated": [
                {**dict(r), "applies_to": ("every project" if r["project_id"] is None
                                           else "this project"),
                 "created_at": r["created_at"].isoformat() if r["created_at"] else None}
                for r in rows
            ],
        }

    @reg.tool(
        "mandate_standard",
        "Make a rule binding on future builds — the standing version of a recommendation. "
        "Use when a finding is not about ONE app but about how every app of a kind should be "
        "built. Scope 'all_projects' is for rules true everywhere; otherwise it binds this "
        "project only. Write the body as instructions to a builder, and say WHY: an agent that "
        "understands a constraint applies it to cases the text did not enumerate. "
        "WHAT THIS DOES, EXACTLY: the rule is injected into every pipeline stage of every "
        "matching build, with the same force as the platform's own standards. It instructs the "
        "builder; it is NOT a deploy-time gate and it cannot by itself block a release. Write "
        "'do X before Y', not 'block the deployment if X' — a rule that promises enforcement "
        "nobody performs is worse than none, because everyone downstream believes it is "
        "covered.",
        _obj({"key": {"type": "string",
                      "description": "short stable id, e.g. 'port_exposure_check'"},
              "role": {"type": "string",
                       "description": "the role whose judgement this is — 'Security', "
                                      "'Operations', 'QA Lead'. Recorded as the author, so an "
                                      "engineer reading the rule later knows who to argue "
                                      "with. Use the role you are answering as, not 'BOSS'."},
              "title": {"type": "string"},
              "body": {"type": "string",
                       "description": "what a builder must do, concretely"},
              "rationale": {"type": "string", "description": "why — what goes wrong without it"},
              "scope": {"type": "string",
                        "description": "'service' (anything serving traffic, the default) or "
                                       "'all' (also libraries and CLIs)"},
              "all_projects": {"type": "boolean",
                               "description": "true to bind every project, not just this one"},
              "project_id": {"type": "string"}},
             ["key", "title", "body", "rationale"]),
        kind="write",
        reversibility="safe",
    )
    def mandate_standard(ctx, *, project_id, actor, args):
        pid = args.get("project_id") or project_id
        key = str(args.get("key") or "").strip()
        title = str(args.get("title") or "").strip()
        body = str(args.get("body") or "").strip()
        why = str(args.get("rationale") or "").strip()
        # The ROLE, not the session identity. Every chat turn runs as the same actor
        # ("orchestrator"), so recording that would attribute every rule the workforce ever
        # makes to the same anonymous account — and "who mandated this" is the first question
        # anyone asks about a rule they have to follow.
        role = str(args.get("role") or "").strip() or actor
        if not (key and title and body and why):
            return {"ok": False,
                    "error": "key, title, body and rationale are all required — a rule with "
                             "no stated reason is followed narrowly and ignored at the edges"}
        scope = "all" if str(args.get("scope") or "").lower() == "all" else "service"
        everywhere = bool(args.get("all_projects"))
        target = None if everywhere else pid

        with ctx.begin() as c:
            existing = c.execute(
                _text("SELECT id FROM engineering_standards WHERE standard_key = :k "
                      "AND project_id IS NOT DISTINCT FROM :p"),
                {"k": key, "p": target}).scalar()
            if existing:
                c.execute(_text(
                    "UPDATE engineering_standards SET title=:t, body=:b, rationale=:w, "
                    "scope=:s, mandated_by_role=:r, status='active', updated_at=now() "
                    "WHERE id=:i"),
                    {"t": title, "b": body, "w": why, "s": scope, "r": role, "i": existing})
                sid = existing
            else:
                from ..services.core import make_id

                sid = make_id("std")
                c.execute(_text(
                    "INSERT INTO engineering_standards (id, project_id, standard_key, title, "
                    "body, scope, mandated_by_role, rationale, created_by) "
                    "VALUES (:i,:p,:k,:t,:b,:s,:r,:w,:by)"),
                    {"i": sid, "p": target, "k": key, "t": title, "b": body, "s": scope,
                     "r": role, "w": why, "by": actor})
        return {"ok": True, "standard_id": sid, "key": key, "mandated_by_role": role,
                "applies_to": "every project" if everywhere else pid,
                "scope": scope,
                "note": "From now on this is in the instructions for every matching build, "
                        "alongside the platform's own standards. It does NOT gate deploys — "
                        "tell the operator that plainly rather than implying releases will be "
                        "blocked."}

    @reg.tool(
        "check_port_exposure",
        "Is a port reachable from outside, and does anything else answer on it? Probes the "
        "host's public address and reports what came back. Use before assigning a port to an "
        "app, and when deciding whether a service is unintentionally public. Read-only: it "
        "makes requests, it changes nothing.",
        _obj({"host": {"type": "string", "description": "public address, e.g. 2.29.2.202"},
              "ports": {"type": "array", "items": {"type": "integer"},
                        "description": "ports to probe"},
              "project_id": {"type": "string"}},
             ["host", "ports"]),
        kind="read",
    )
    def check_port_exposure(ctx, *, project_id, actor, args):
        import socket

        host = str(args.get("host") or "").strip()
        ports = [int(p) for p in (args.get("ports") or []) if str(p).strip().isdigit()][:20]
        if not host or not ports:
            return {"ok": False, "error": "host and at least one port are required"}

        findings = []
        for port in ports:
            # A TCP connect is the honest test of "reachable from here": an HTTP probe cannot
            # tell a closed port from a service that speaks something other than HTTP.
            reachable, detail = False, ""
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(4.0)
            try:
                sock.connect((host, port))
                reachable = True
            except OSError as exc:
                detail = f"{type(exc).__name__}: {exc}"[:120]
            finally:
                sock.close()
            findings.append({
                "port": port,
                "reachable_from_boss": reachable,
                "detail": detail,
                # Stated rather than implied: BOSS reaching it says it is open TO BOSS, which
                # on a tailnet or inside a VPC is not the same as open to the internet.
                "means": ("something is listening and BOSS can reach it — whether that is "
                          "PUBLIC depends on where BOSS sits relative to this host"
                          if reachable else "nothing accepted a connection from BOSS"),
            })
        with ctx.begin() as c:
            taken = c.execute(
                _text("SELECT app_name, domains FROM deployment_targets "
                      "WHERE project_id = :p AND status = 'active'"),
                {"p": args.get("project_id") or project_id}).mappings().all()
        claimed = {}
        for row in taken:
            for domain in (row["domains"] or []):
                head, _, tail = str(domain).rpartition(":")
                if tail.isdigit() and head == host:
                    claimed[int(tail)] = row["app_name"]
        for f in findings:
            f["already_assigned_to"] = claimed.get(f["port"])
        return {"host": host, "findings": findings,
                "note": "already_assigned_to comes from BOSS's own registry; a reachable port "
                        "with no assignment is something BOSS did not put there"}


def _register_role_experience_tools(reg: ToolRegistry) -> None:
    """What a role has actually argued, on this project, in its own words.

    The Agents page listed a workforce and "Ask" opened the GENERAL agent with "@QA Lead: "
    typed into the box — the name was prose in a prompt, not an identity. So an operator asking
    the QA Lead a question got BOSS, answering as itself, with no idea what the QA Lead had
    ever said.

    The experience is already recorded and nobody reads it back. `meeting_runs.state` holds
    each round as an object KEYED BY ROLE TITLE:

        {"round1": {"UX Designer": "…the whole argument…", "Engineer / Architect": "…"}}

    So every position every role took, on every panel, in full prose, attributed. That is what
    makes "advice from the experience it built here" a retrieval problem rather than a claim,
    and it is why this is a read tool over existing data instead of a new capture mechanism
    nobody would remember to write to.
    """
    @reg.tool(
        "recall_role_experience",
        "What a role has argued on THIS project's past panels, in its own words — the "
        "positions it took, on which brief, and when. Use it before answering as a role: an "
        "SME's value is the specifics it has seen, and a generic answer from a named expert "
        "is worse than no expert. Also useful for 'what has our QA lead been worried about'.",
        _obj({"role": {"type": "string",
                       "description": "role title or key, e.g. 'QA / Test' or 'qa'"},
              "limit": _LIMIT,
              "project_id": {"type": "string"}},
             ["role"]),
        kind="read",
    )
    def recall_role_experience(ctx, *, project_id, actor, args):
        pid = args.get("project_id") or project_id
        wanted = str(args.get("role") or "").strip()
        limit = int(args.get("limit") or 12)
        if not wanted:
            return {"ok": False, "error": "role is required"}

        with ctx.begin() as c:
            # Titles are what the rounds are keyed by; a key like "qa" has to be resolved to
            # "QA / Test" first or nothing matches.
            titles = [r["title"] for r in c.execute(
                _text("SELECT title FROM meeting_roles WHERE project_id = :p "
                      "AND (lower(role_key) = lower(:w) OR lower(title) = lower(:w))"),
                {"p": pid, "w": wanted}).mappings()]
            if not titles:
                titles = [wanted]
            rows = c.execute(
                _text("SELECT id, directive, state, created_at FROM meeting_runs "
                      "WHERE project_id = :p AND state != '{}'::jsonb "
                      "ORDER BY created_at DESC LIMIT 200"),
                {"p": pid}).mappings().all()

        seen = []
        for row in rows:
            state = row["state"] if isinstance(row["state"], dict) else {}
            for round_name in ("round1", "round2", "synthesis"):
                block = state.get(round_name)
                if not isinstance(block, dict):
                    continue
                for title in titles:
                    said = block.get(title)
                    if not isinstance(said, str) or not said.strip():
                        continue
                    seen.append({
                        "meeting_run_id": row["id"],
                        "brief": str(row["directive"] or "")[:160],
                        "round": round_name,
                        "when": row["created_at"].isoformat() if row["created_at"] else None,
                        # Enough to be specific, not so much that a dozen of them flood the
                        # context this is meant to inform.
                        "said": said.strip()[:1200],
                    })
                    if len(seen) >= limit:
                        break
                if len(seen) >= limit:
                    break
            if len(seen) >= limit:
                break

        return {"project_id": pid, "role": wanted, "matched_titles": titles,
                "contributions": seen, "count": len(seen),
                # AN EMPTY MEMORY IS NOT A TOPIC. The old note told the model to SAY that its
                # answer was not from experience here — so every reply opened by disclosing an
                # absence the operator had not asked about, and a question about a failed step
                # became a paragraph about the role's own history. Answer from expertise; only
                # mention the empty record if past positions were actually being asked for.
                "note": ("no past contributions recorded for this role on this project. "
                         "Answer the question from expertise. Do NOT make this absence the "
                         "subject — mention it only if the operator asked what this role has "
                         "argued before."
                         if not seen else "these are this role's own past words")}

    @reg.tool(
        "describe_role",
        "Who a role is on this project: the lens it argues from, and the rules and skills its "
        "agent definition carries. Read this before answering as a role, so the answer comes "
        "from that role's actual remit rather than a guess at what the title implies.",
        _obj({"role": {"type": "string"}, "project_id": {"type": "string"}}, ["role"]),
        kind="read",
    )
    def describe_role(ctx, *, project_id, actor, args):
        pid = args.get("project_id") or project_id
        wanted = str(args.get("role") or "").strip()
        # HOW A PERSON SAYS IT vs HOW THE TABLE SPELLS IT. Measured: an agent asked about
        # "the Deployment Manager" — the name the console had just shown it — got "no role
        # 'the Deployment Manager' on boss-dogfood" and had to guess `deployment_manager` on
        # the retry. The display name, the slug, and the way anyone writes either in a
        # sentence should all find the same row.
        candidates = _role_name_candidates(wanted)
        with ctx.begin() as c:
            # TWO TABLES HOLD ROLES, AND THIS ONLY LOOKED IN ONE.
            #
            # `meeting_roles` is panel SEATS — who sits in a PRD debate. `roles` is the
            # WORKFORCE — deployment_manager, program_manager, media_publisher, the agents an
            # operator actually addresses. On this project meeting_roles is empty and `roles`
            # has 28 rows, so every single agent opened by announcing it did not exist:
            # "There is no Program Manager role defined on this project", said by the Program
            # Manager. Reported as giving a false impression, which is putting it kindly.
            #
            # The workforce is checked FIRST because that is what a named agent is. A panel
            # seat is the fallback, so asking about one still works.
            row = None
            for candidate in candidates:
                row = c.execute(
                    _text("SELECT role_key, display_name AS title, spec, "
                          "       COALESCE(autonomy_level, 1) AS risk_tier "
                          "  FROM roles WHERE project_id = :p "
                          "   AND (lower(role_key) = lower(:w) "
                          "        OR lower(display_name) = lower(:w) "
                          "        OR lower(replace(display_name, ' ', '_')) = lower(:w)) "
                          " LIMIT 1"),
                    {"p": pid, "w": candidate}).mappings().first()
                if row is not None:
                    break
            if row is None:
                for candidate in candidates:
                    row = c.execute(
                        _text("SELECT role_key, title, "
                              "       jsonb_build_object('lens', lens) AS spec, risk_tier "
                              "  FROM meeting_roles WHERE project_id = :p "
                              "   AND (lower(role_key) = lower(:w) "
                              "        OR lower(title) = lower(:w) "
                              "        OR lower(replace(title, ' ', '_')) = lower(:w)) "
                              " LIMIT 1"),
                        {"p": pid, "w": candidate}).mappings().first()
                    if row is not None:
                        break
            if row is None and candidates:
                # LAST RESORT: one unambiguous partial match. "deployment" finding exactly one
                # role is an answer; finding three is a question, and the list below asks it.
                near = c.execute(
                    _text("SELECT role_key, display_name AS title, spec, "
                          "       COALESCE(autonomy_level, 1) AS risk_tier "
                          "  FROM roles WHERE project_id = :p "
                          "   AND (role_key ILIKE :like OR display_name ILIKE :like) "
                          " ORDER BY length(role_key) LIMIT 2"),
                    {"p": pid, "like": f"%{candidates[-1]}%"}).mappings().all()
                if len(near) == 1:
                    row = near[0]
            if row is None:
                # NAME WHAT EXISTS. "No role X" sends the reader hunting for a typo; the list
                # says whether they are even asking the right project.
                available = c.execute(
                    _text("SELECT role_key FROM roles WHERE project_id = :p "
                          "ORDER BY role_key LIMIT 40"),
                    {"p": pid}).scalars().all()
                return {"ok": False,
                        "error": f"no role {wanted!r} on {pid}",
                        "tried": candidates,
                        "available": list(available)}
            # The agent DEFINITION, when one has been published for this role: its prompts,
            # rules and requested skills are the SME's remit, and they live apart from the
            # panel lens so an operator can extend one without editing the other.
            version = c.execute(
                _text("SELECT v.prompts, v.rules, v.requested_skills, v.requested_integrations "
                      "FROM agent_versions v "
                      "JOIN agent_definitions d ON d.active_version_id = v.id "
                      "WHERE d.project_id = :p AND lower(v.role_key) = lower(:k) LIMIT 1"),
                {"p": pid, "k": row["role_key"]}).mappings().first()
        # THE SPEC *IS* THE DEFINITION, AND THIS USED TO THROW IT AWAY.
        #
        # Every workforce role carries a full spec — mission, boundaries, decision rights,
        # comms contract — installed from the pack into `roles.spec`. The old code selected
        # `NULL AS lens` and reported `has_agent_definition: false` unless someone had
        # separately published an `agent_versions` row, which on this project nobody has for
        # any role. So a Program Manager with a thirty-line remit introduced itself by saying
        # it had no definition and no lens and could not argue from anything real.
        #
        # An `agent_definitions` row is an EXTENSION of a role, not the thing that brings it
        # into being. A role is defined when it has a spec, and it always does.
        spec = row["spec"] if isinstance(row["spec"], dict) else {}
        boundaries = spec.get("boundaries") or {}
        permissions = spec.get("permissions") or {}
        out = {
            "role_key": row["role_key"],
            "title": row["title"],
            # The mission is what a workforce role argues from; `lens` is the panel-seat word
            # for the same thing. Either way the agent needs ONE field to answer from.
            "lens": spec.get("lens") or spec.get("mission"),
            "mission": spec.get("mission"),
            "risk_tier": row["risk_tier"],
            "primary_outcomes": spec.get("primary_outcomes") or [],
            "must": boundaries.get("must") or [],
            "must_not": boundaries.get("must_not") or [],
            "decision_rights": spec.get("decision_rights") or {},
            "comms_contract": spec.get("comms_contract") or {},
            "skills": permissions.get("allowed_skills") or [],
            "forbidden_skills": permissions.get("forbidden_skills") or [],
            "has_agent_definition": True,
            "definition_source": "spec",
            # Say it in the payload, because the model reads this before it answers and will
            # otherwise report the absence of an optional extension as a defect in itself.
            # THE ROLE IS A LENS ON THE OPERATOR'S QUESTION, NOT THE SUBJECT OF THE ANSWER.
            #
            # This note used to say only "answer AS this role, from its mission and
            # boundaries", which fixed one failure and caused a worse one. Asked to run two
            # SQL queries and paste the rows, the Program Manager replied with several
            # hundred words about never having been seated on a panel. It was not wrong about
            # that, and it was not what anybody asked. Reported as "no recommendations, not
            # identifying the right issue, the Agent quality is very bad".
            #
            # A real specialist brings their lens TO your question. They do not answer a
            # question about a failing step with their own job description.
            "note": "ANSWER THE OPERATOR'S ACTUAL QUESTION FIRST, in full. This role is a "
                    "LENS you bring to it — its mission shapes what you notice and what you "
                    "warn about, it is not the subject. Do not open with who you are, what "
                    "you have or have not done on this project, or what is missing from your "
                    "own configuration, unless the operator asked about the role itself or it "
                    "directly changes the answer. If asked for data, give the data. The role "
                    "is defined; never say it is not.",
        }
        if version is not None:
            # A published version OVERRIDES the pack: it is what an operator edited by hand.
            out.update({
                "rules": version["rules"] or [],
                "skills": version["requested_skills"] or out["skills"],
                "integrations": version["requested_integrations"] or [],
                "prompts": version["prompts"] or {},
                "definition_source": "agent_version",
            })
        return out


def _register_deployment_target_tools(reg: ToolRegistry) -> None:
    """Read an app's address, and correct it — the two things BOSS could not do.

    HookWatch failed its health check four times running. Each time the recorded reason said
    "port 3020 is blocked by a firewall", each time the operator approved a fix, and each time
    BOSS answered with the same paragraph. It was not being obtuse: it had NO TOOL TO CHANGE A
    TARGET. It kept offering "tell me which port and I'll update the target" for an action it
    could not perform, which is the worst shape a limitation can take — indistinguishable, from
    the outside, from refusing to help.

    It also had no way to READ the target's binding side by side. The contradiction was two
    fields apart: `domains` said 2.29.2.202:8000 while `app_port` said 3020. The health check
    probed the first, the container published on the second, and nothing compared them. There
    was no firewall; a sibling app on that host answers on its own port from outside.

    So: one tool that lays the binding out and says where it disagrees with itself, and one
    that fixes it.
    """
    @reg.tool(
        "describe_deployment_target",
        "Everything about where an app ships: its host, the port the container publishes on, "
        "the address the health check probes, its status and its credential — AND whether "
        "those agree. Run this whenever a deployment is 'unreachable' or a health check "
        "fails: an address that names a different port from the one the app is published on "
        "fails identically to a blocked firewall, and the fixes are opposite.",
        _obj({"app_name": {"type": "string", "description": "the app, e.g. HookWatch"},
              "target_id": {"type": "string", "description": "or the target directly"},
              "project_id": {"type": "string"}}),
        kind="read",
    )
    def describe_deployment_target(ctx, *, project_id, actor, args):
        pid = args.get("project_id") or project_id
        where, params = "project_id = :p", {"p": pid}
        if args.get("target_id"):
            where += " AND id = :i"
            params["i"] = str(args["target_id"])
        elif args.get("app_name"):
            where += " AND app_name = :a"
            params["a"] = str(args["app_name"])
        with ctx.begin() as c:
            rows = c.execute(_text(
                "SELECT id, app_name, environment, status, host_id, target_ref, domains, "
                "required_resources, health_checks, secret_refs "
                f"FROM deployment_targets WHERE {where} ORDER BY app_name"), params,
            ).mappings().all()
            hosts = {r["id"]: dict(r) for r in c.execute(_text(
                "SELECT id, name, address, status, key_secret_ref FROM deployment_hosts "
                "WHERE project_id = :p"), {"p": pid}).mappings()}

        out = []
        for row in rows:
            resources = row["required_resources"] or []
            app_port = next((str(r.get("name")) for r in resources
                             if isinstance(r, dict) and r.get("kind") == "app_port"), "")
            domains = [str(d) for d in (row["domains"] or [])]
            domain_port = ""
            if domains and ":" in domains[0]:
                tail = domains[0].rsplit(":", 1)[1]
                domain_port = tail if tail.isdigit() else ""
            host = hosts.get(row["host_id"]) if row["host_id"] else None

            # THE CONTRADICTIONS, STATED. This is the whole point of the tool: the facts were
            # always readable and nobody compared them.
            problems = []
            if not row["host_id"]:
                problems.append("no host bound: the ssh credential resolves through host_id, "
                                "so every deploy will refuse before it connects")
            if app_port and domain_port and app_port != domain_port:
                problems.append(
                    f"THE ADDRESS NAMES THE WRONG PORT: health checks probe {domain_port} "
                    f"but the container publishes on {app_port}. Nothing listens on "
                    f"{domain_port}, so the check fails exactly as a blocked port would. "
                    f"Fix with retarget_app(port={app_port}), not with a firewall rule.")
            if app_port and not domains:
                problems.append(f"no address recorded, though the app publishes on {app_port}")
            if row["status"] != "active":
                problems.append(f"target is {row['status']}: the console lists only active "
                                "targets, so this app is invisible and cannot be shipped")
            out.append({
                "target_id": row["id"], "app_name": row["app_name"],
                "environment": row["environment"], "status": row["status"],
                "host": (host or {}).get("name"), "host_address": (host or {}).get("address"),
                "publishes_on_port": app_port or None,
                "health_check_address": domains[0] if domains else None,
                "agrees_with_itself": not problems,
                "problems": problems,
            })
        return {"project_id": pid, "targets": out,
                "note": "compare publishes_on_port with health_check_address before blaming "
                        "the network"}

    @reg.tool(
        "retarget_app",
        "Correct where an app is addressed: its port, or the host it is bound to. Use after "
        "describe_deployment_target reports the address and the published port disagree. "
        "Changes only the binding — it does not deploy, and it does not touch the app.",
        _obj({"target_id": {"type": "string"},
              "port": {"type": "integer",
                       "description": "the port the container actually publishes on"},
              "host_id": {"type": "string", "description": "rebind to a different host"},
              "activate": {"type": "boolean",
                           "description": "bring a retired target back into service"},
              "project_id": {"type": "string"}},
             ["target_id"]),
        kind="write",
        reversibility="safe",
    )
    def retarget_app(ctx, *, project_id, actor, args):
        pid = args.get("project_id") or project_id
        target_id = str(args["target_id"])
        changed = {}
        with ctx.begin() as c:
            row = c.execute(_text(
                "SELECT id, host_id, domains, required_resources, status FROM "
                "deployment_targets WHERE id = :i AND project_id = :p"),
                {"i": target_id, "p": pid}).mappings().first()
            if row is None:
                return {"ok": False, "error": f"no target {target_id} in {pid}"}

            host_id = str(args.get("host_id") or row["host_id"] or "")
            if not host_id:
                return {"ok": False, "error": "the target has no host and none was given"}
            host = c.execute(_text(
                "SELECT id, name, address, ssh_user, ssh_port FROM deployment_hosts "
                "WHERE id = :h AND project_id = :p"),
                {"h": host_id, "p": pid}).mappings().first()
            if host is None:
                return {"ok": False, "error": f"no host {host_id} in {pid}"}

            if args.get("host_id") and args["host_id"] != row["host_id"]:
                c.execute(_text(
                    "UPDATE deployment_targets SET host_id = :h, target_ref = :ref, "
                    "updated_at = now() WHERE id = :i"),
                    {"h": host_id, "i": target_id,
                     "ref": f"{host['ssh_user'] or 'root'}@{host['address']}:"
                            f"{host['ssh_port'] or 22}"})
                changed["host"] = host["name"]

            port = args.get("port")
            if port:
                resources = [r for r in (row["required_resources"] or [])
                             if not (isinstance(r, dict) and r.get("kind") == "app_port")]
                resources.append({"kind": "app_port", "name": str(int(port))})
                # BOTH RECORDS, IN ONE WRITE. They are two spellings of one fact, and letting
                # them drift apart is the entire bug this tool exists to repair.
                c.execute(_text(
                    "UPDATE deployment_targets SET required_resources = CAST(:r AS JSONB), "
                    "domains = CAST(:d AS JSONB), updated_at = now() WHERE id = :i"),
                    {"r": json.dumps(resources),
                     "d": json.dumps([f"{host['address']}:{int(port)}"]), "i": target_id})
                changed["port"] = int(port)
                changed["address"] = f"{host['address']}:{int(port)}"

            if args.get("activate") and row["status"] != "active":
                c.execute(_text("UPDATE deployment_targets SET status = 'active', "
                                "updated_at = now() WHERE id = :i"), {"i": target_id})
                changed["status"] = "active"

        if not changed:
            return {"ok": True, "changed": {}, "note": "nothing to change"}
        return {"ok": True, "target_id": target_id, "changed": changed,
                "note": "the binding is corrected; redeploy to verify it end to end"}


def _register_image_tools(reg: ToolRegistry) -> None:
    """Make pictures, because the apps BOSS writes need them.

    Every app BOSS ships that wants imagery has had exactly two options: a drawing built out of
    SVG, or a hotlinked stock photo. The first looks like a wireframe and the second breaks the
    day the CDN is unreachable — and on a tailnet-only console it is unreachable most days.

    No Claude model can do this: Claude READS images and cannot emit them. So this goes through
    the OpenAI-compatible endpoint the platform already points at (`OPENAI_BASE_URL`, currently
    a LiteLLM proxy), which is the same route every other model call takes. If the proxy has no
    image model configured, the tool says so and repeats the proxy's own message rather than
    inventing a reason.
    """
    @reg.tool(
        "generate_image",
        "Generate an image from a description and save it to a file — for an app BOSS is "
        "building that needs real artwork rather than a drawn placeholder. Costs money per "
        "image, so it is a write. Returns the path written and the bytes produced. Say what "
        "the picture should SHOW; the model has no other context.",
        _obj({"prompt": {"type": "string",
                         "description": "what the image shows, in a sentence or two"},
              "path": {"type": "string",
                       "description": "where to write it, e.g. public/hero.png. Relative paths "
                                      "land under the image output directory."},
              "size": {"type": "string",
                       "description": "1024x1024 (default), 1536x1024, or 1024x1536"},
              "model": {"type": "string",
                        "description": "override the image model; defaults to the configured "
                                       "one"}},
             ["prompt", "path"]),
        kind="write",
        reversibility="costs_money",
    )
    def generate_image(ctx, *, project_id, actor, args):
        import base64
        import urllib.error
        import urllib.request

        settings = ctx.settings
        base = str(getattr(settings, "openai_base_url", "") or "").rstrip("/")
        key = str(getattr(settings, "openai_api_key", "") or "")
        if not base or not key:
            return {"ok": False,
                    "error": "no image provider configured: OPENAI_BASE_URL and "
                             "OPENAI_API_KEY must both be set"}

        prompt = str(args.get("prompt") or "").strip()
        if not prompt:
            return {"ok": False, "error": "prompt is required"}
        model = str(args.get("model")
                    or getattr(settings, "image_model", "") or "gpt-image-1")
        size = str(args.get("size") or "1024x1024")

        rel = str(args.get("path") or "").strip().lstrip("/")
        if not rel:
            return {"ok": False, "error": "path is required"}
        root = _os.environ.get("BOSS_IMAGE_DIR") or _os.path.join(_REPO_CACHE, "images")
        dest = _safe_path(rel, root)
        if dest is None:
            return {"ok": False, "error": "path escapes the image output directory"}

        body = json.dumps({"model": model, "prompt": prompt, "size": size, "n": 1}).encode()
        request = urllib.request.Request(
            f"{base}/images/generations", data=body,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
        )
        try:
            with urllib.request.urlopen(request, timeout=180) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            # THE PROVIDER'S OWN WORDS. "no image model configured on this proxy" and "billing
            # limit reached" need completely different actions, and a generic failure hides
            # which one happened.
            detail = ""
            try:
                detail = exc.read().decode("utf-8")[:400]
            except Exception:  # noqa: BLE001
                pass
            return {"ok": False, "status": exc.code, "model": model,
                    "error": f"image endpoint refused: {detail or exc.reason}"}
        except Exception as exc:  # noqa: BLE001 — a tool must not raise
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:300]}

        item = ((payload.get("data") or [{}])[0]) or {}
        raw: bytes
        if item.get("b64_json"):
            raw = base64.b64decode(item["b64_json"])
        elif item.get("url"):
            try:
                with urllib.request.urlopen(item["url"], timeout=120) as fetched:
                    raw = fetched.read()
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "error": f"image URL could not be fetched: {exc}"[:200]}
        else:
            return {"ok": False, "error": "the provider returned neither b64_json nor a url",
                    "keys": sorted(item.keys())}

        try:
            _os.makedirs(_os.path.dirname(dest), exist_ok=True)
            with open(dest, "wb") as handle:
                handle.write(raw)
        except OSError as exc:
            return {"ok": False, "error": f"could not write {rel}: {exc}"[:200]}
        return {"ok": True, "path": dest, "bytes": len(raw), "model": model, "size": size}


def _register_app_identity_tools(reg: ToolRegistry) -> None:
    """Find and repair apps that were split into two identities.

    An operator opened the console and saw the same product twice: "Incident Timeline Builder."
    with 17 attempts and PR #164, and "Incident-Timeline Builder" with 2 attempts, PR #166 and a
    live deployment. One product, two cards, two PRs, two bills, and the deployed one is not the
    one carrying most of the work.

    `stable_app_id` hashes `identity_key(name)`, and identity_key was fixed to collapse
    punctuation precisely so a trailing full stop and a U+2010 hyphen could not fork one app.
    But app_id is STAMPED AT CREATION and never re-derived, so the fix only helps work started
    afterwards. Every app split before it stays split for good, and nothing in the product could
    put it back together — which makes it the customer's problem, permanently, for a bug that
    was ours.
    """
    @reg.tool(
        "find_split_apps",
        "Find apps recorded under more than one identity — the same product showing as two "
        "cards, with its attempts, PRs and spend divided between them. Compares what each "
        "run's title NORMALISES to, so it catches the splits punctuation caused ('X.' vs "
        "'X-Y'). Read-only. Use before merge_apps, and whenever a customer says they see "
        "the same app twice.",
        _obj({"project_id": {"type": "string"}}),
    )
    def find_split_apps(ctx, *, project_id, actor, args):
        from ..services.naming import identity_key, project_name

        pid = args.get("project_id") or project_id
        with ctx.begin() as c:
            rows = c.execute(
                _text("SELECT id, title, status, pull_request_number, finalize_ctx, created_at "
                      "FROM code_task_runs WHERE project_id=:p ORDER BY created_at"),
                {"p": pid},
            ).mappings().all()

        groups: dict[str, dict] = {}
        for row in rows:
            name, _blurb, _label = project_name(row["title"] or "")
            key = identity_key(name)
            if not key or name == "Untitled app":
                continue
            app_id = ((row["finalize_ctx"] or {}).get("app_id")) or ""
            g = groups.setdefault(key, {"identity": key, "identities": {}})
            ident = g["identities"].setdefault(
                app_id or "(unstamped)",
                {"app_id": app_id or None, "display_names": [], "runs": 0,
                 "pull_requests": [], "latest": None},
            )
            ident["runs"] += 1
            if name not in ident["display_names"]:
                ident["display_names"].append(name)
            if row["pull_request_number"] and row["pull_request_number"] not in ident["pull_requests"]:
                ident["pull_requests"].append(row["pull_request_number"])
            ident["latest"] = row["id"]

        split = []
        for key, g in groups.items():
            if len(g["identities"]) < 2:
                continue
            idents = sorted(g["identities"].values(), key=lambda i: -i["runs"])
            split.append({
                "identity": key,
                "identities": idents,
                # The one with the most work behind it is the natural survivor; the caller can
                # override, because "most runs" is not always "the one that is deployed".
                "suggested_keep": idents[0]["app_id"],
            })
        return {"project_id": pid, "split_apps": split, "count": len(split)}

    @reg.tool(
        "merge_apps",
        "Merge apps that are the same product into ONE identity, so the console shows one "
        "card with the whole lineage instead of two competing ones. Re-stamps app_id on every "
        "affected run and records what it was, so the merge can be undone. Does not touch "
        "code, PRs or running deployments — it corrects how BOSS grouped the work.",
        _obj({"keep_app_id": {"type": "string",
                              "description": "the identity to keep (from find_split_apps)"},
              "merge_app_ids": {"type": "array", "items": {"type": "string"},
                                "description": "identities to fold into it"},
              "project_id": {"type": "string"}},
             ["keep_app_id", "merge_app_ids"]),
        kind="write",
        reversibility="safe",
    )
    def merge_apps(ctx, *, project_id, actor, args):
        pid = args.get("project_id") or project_id
        keep = str(args.get("keep_app_id") or "").strip()
        merge = [str(a).strip() for a in (args.get("merge_app_ids") or []) if str(a).strip()]
        if not keep or not merge:
            return {"ok": False, "error": "keep_app_id and at least one merge_app_id required"}
        if keep in merge:
            return {"ok": False, "error": "keep_app_id cannot also be in merge_app_ids"}

        moved = []
        with ctx.begin() as c:
            for app_id in merge:
                rows = c.execute(
                    _text("SELECT id FROM code_task_runs "
                          "WHERE project_id=:p AND finalize_ctx->>'app_id' = :a"),
                    {"p": pid, "a": app_id},
                ).mappings().all()
                if not rows:
                    continue
                # `merged_from_app_id` is the undo. A grouping change with no record of what it
                # was before is a grouping change nobody can argue with afterwards.
                c.execute(
                    _text("UPDATE code_task_runs SET finalize_ctx = "
                          "COALESCE(finalize_ctx,'{}'::jsonb) || CAST(:v AS JSONB), "
                          "updated_at=now() "
                          "WHERE project_id=:p AND finalize_ctx->>'app_id' = :a"),
                    {"p": pid, "a": app_id,
                     "v": json.dumps({"app_id": keep, "merged_from_app_id": app_id,
                                      "merged_by": actor})},
                )
                moved.append({"app_id": app_id, "runs": len(rows)})
        return {"ok": True, "kept": keep, "merged": moved,
                "runs_moved": sum(m["runs"] for m in moved),
                "note": "the console groups by app_id, so these now show as one app"}


def _register_investigation_tools(reg: ToolRegistry) -> None:
    @reg.tool(
        "search_code",
        "Search SOURCE for a regex — BOSS's own platform code (repo='boss', the default, "
        "including infra/) or a customer application (repo='owner/name', or a bare repo "
        "name for the configured target). Answers 'where is this implemented', 'what "
        "raises this error', 'why does my app do that'. Returns file:line. Prefer this "
        "over guessing at behaviour.\n"
        "TO SEARCH AN APP BOSS BUILT, PASS run_id. Its code sits on an open PR branch, NOT "
        "on the default branch, so a search without run_id looks at a repo that does not "
        "contain the app and finds nothing. run_id resolves the repo and the branch for you.\n"
        "When an operator quotes an error message, search for that literal text FIRST — in "
        "the app (run_id) AND in repo='boss'. Whichever source contains the string is where "
        "it is raised, and it is often the platform rather than the app.",
        _obj({"pattern": {"type": "string", "description": "python regex"},
              "repo": {"type": "string",
                       "description": "'boss' for the platform, or 'owner/name' for an app"},
              "run_id": {"type": "string",
                         "description": "a code task run (crun_…): search THAT run's code, "
                                        "on its branch. Overrides repo."},
              "ref": {"type": "string",
                      "description": "explicit branch/tag/sha; defaults to the branch for "
                                     "run_id, else the repo default"},
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
        repo, ref, err = _resolve_source(ctx, args)
        if err:
            return {"ok": False, "error": err}
        root, err = _repo_root(ctx.settings, repo, ref)
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
                                    return {"matches": hits, "truncated": True, "root": root,
                                            "repo": repo, "ref": ref or "(default branch)"}
                except (OSError, UnicodeDecodeError):
                    continue
        # WHICH SOURCE WAS READ, always. "No matches" means nothing unless you know where it
        # looked — that ambiguity is what let a search of an empty default branch read as
        # "this code does not exist".
        return {"matches": hits, "truncated": False, "root": root,
                "repo": repo, "ref": ref or "(default branch)"}

    @reg.tool(
        "list_source",
        "List the files in a repo — BOSS's platform (repo='boss', default) or an app BOSS "
        "built (run_id). Answers 'what IS this app' before you guess at filenames. "
        "search_code needs a pattern and read_code needs an exact path; this is how you find "
        "out what is there in the first place. Pass run_id for an app: its files are on its "
        "PR branch, not the default branch.",
        _obj({"repo": {"type": "string", "description": "'boss', or 'owner/name' for an app"},
              "run_id": {"type": "string",
                         "description": "a code task run (crun_…): list THAT run's tree"},
              "ref": {"type": "string", "description": "explicit branch/tag/sha"},
              "path": {"type": "string", "description": "optional subdir, e.g. server/"},
              "glob": {"type": "string", "description": "optional filter, e.g. *.ts"},
              "limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 200}}),
    )
    def list_source(ctx, *, project_id, actor, args):
        """WHAT IS IN THIS APP — the question every investigation starts from.

        BOSS could grep a repo and read a named file, and had no way to ask what the repo
        CONTAINED. So an investigation opened with a guess at a filename, and a wrong guess is
        indistinguishable from a file that does not exist.
        """
        import fnmatch as _fn

        repo, ref, err = _resolve_source(ctx, args)
        if err:
            return {"ok": False, "error": err}
        root, err = _repo_root(ctx.settings, repo, ref)
        if root is None:
            return {"ok": False, "error": err}
        base = _safe_path(args.get("path") or "", root)
        if base is None or not _os.path.isdir(base):
            return {"ok": False, "error": f"no such directory: {args.get('path') or '/'}"}
        glob = args.get("glob")
        limit = int(args.get("limit") or 200)

        files: list[dict] = []
        truncated = False
        for dirpath, dirnames, filenames in _os.walk(base):
            dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_DIRS)
            for fname in sorted(filenames):
                if glob and not _fn.fnmatch(fname, glob):
                    continue
                full = _os.path.join(dirpath, fname)
                try:
                    size = _os.path.getsize(full)
                except OSError:
                    continue
                files.append({"file": _os.path.relpath(full, root), "bytes": size})
                if len(files) >= limit:
                    truncated = True
                    break
            if truncated:
                break
        return {"repo": repo, "ref": ref or "(default branch)", "root": root,
                "files": files, "truncated": truncated}

    @reg.tool(
        "read_code",
        "Read a file from source — BOSS's platform code (repo='boss', default) or a "
        "customer app (repo='owner/name' or run_id), optionally a line range. Use after "
        "search_code to read the actual implementation instead of describing what it "
        "probably does. Pass the SAME run_id you searched with: an app's code is on its PR "
        "branch, not on the default branch.",
        _obj({"file": {"type": "string", "description": "path relative to the repo root"},
              "repo": {"type": "string", "description": "'boss', or 'owner/name' for an app"},
              "run_id": {"type": "string",
                         "description": "a code task run (crun_…): read THAT run's code"},
              "ref": {"type": "string", "description": "explicit branch/tag/sha"},
              "start": {"type": "integer", "minimum": 1},
              "end": {"type": "integer", "minimum": 1}},
             ["file"]),
    )
    def read_code(ctx, *, project_id, actor, args):
        repo, ref, err = _resolve_source(ctx, args)
        if err:
            return {"ok": False, "error": err}
        root, err = _repo_root(ctx.settings, repo, ref)
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
        # NO FILE IS NOT NO LOG. In a container these services log to stdout, so the loop
        # above always fails there — and it used to end in a refusal that named other tools.
        # Measured live: the model reported "the tool cannot access container stdout" and
        # treated the logging question as closed, which is the right conclusion about FILES
        # and the wrong one about the log.
        #
        # `log_capture` has been writing the same records into `app_logs` all along — every
        # service, every deployment shape, no docker socket and no mounted volume. Reading the
        # table is not a fallback; on a containerised install it is the only place the log
        # exists, so it is where this tool looks when there is no file.
        try:
            sql = ("SELECT created_at, level, logger, message, project_id, run_id "
                   "FROM app_logs WHERE (:svc = '' OR service = :svc) ")
            params: dict = {"svc": "" if service in ("", "all") else service,
                            "lim": max(1, min(limit, 500))}
            if args.get("project_id") or project_id:
                sql += "AND (project_id IS NULL OR project_id = :pid) "
                params["pid"] = args.get("project_id") or project_id
            sql += "ORDER BY created_at DESC LIMIT :lim"
            with ctx.begin() as c:
                rows = c.execute(_text(sql), params).mappings().all()
        except Exception as exc:  # noqa: BLE001 — say which of the two places failed
            return {"ok": False, "service": service,
                    "error": f"no log file, and app_logs could not be read: "
                             f"{type(exc).__name__}"}
        entries = []
        for row in reversed(rows):          # oldest first, like a tail
            line = f"{row['created_at']} {row['level']:<7} {row['logger']} {row['message']}"
            if rx is not None and not rx.search(line):
                continue
            entries.append(line[:500])
        if entries:
            return {"service": service, "source": "app_logs (BOSS's own capture)",
                    "lines": len(entries), "log": entries}
        return {"ok": True, "service": service, "source": "app_logs (BOSS's own capture)",
                "lines": 0, "log": [],
                "note": ("the log is readable and has nothing matching. An empty answer here "
                         "means nothing was recorded, NOT that logging is unavailable — "
                         "capture writes one row at startup, so a truly empty table would "
                         "mean the service has not started. For a build, "
                         "get_run_activity(run_id=...) and read_run_turns(run_id=...) carry "
                         "more than the service log does."),
                "use_instead": ["get_run_activity", "read_run_turns", "diagnose_run"]}


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
        "read_pull_request",
        "What a pull request actually CONTAINS: state, branch, and the list of files it "
        "changed, optionally with the diff. Answers 'what did this task really ship' and "
        "'why does the preview fail to build this'. Accepts a PR number, a PR url, or a code "
        "task run id — the PR the task opened is looked up for you.",
        _obj({"pull_request": {"type": "string",
                               "description": "PR number, PR url, or a crun_ id"},
              "repo": {"type": "string", "description": "owner/repo; defaults to the target"},
              "with_diff": {"type": "boolean", "default": False},
              "project_id": {"type": "string"}},
             ["pull_request"]),
    )
    def read_pull_request(ctx, *, project_id, actor, args):
        """The agent could describe a task and not see what it shipped.

        A code task reported "completed" with a PR url, its preview failed with "Could not
        resolve entry module index.html", and the agent had no way to look inside: it has
        git_history and repo_activity, neither of which reads a pull request. So it could only
        offer to retry — while the answer was one listing away. The task had been told a Vite
        scaffold existed and had written a Python service instead; there was no index.html,
        and the PR's file list said so immediately.

        The operator diagnosed this with `gh pr view`. A tool the operator needs and the agent
        lacks is the definition of an agent that cannot do its job.
        """
        import json as _json
        import re as _re
        import urllib.request as _ureq

        pid = args.get("project_id") or project_id
        raw = str(args.get("pull_request") or "").strip()
        if not raw:
            return {"ok": False, "error": "pull_request is required (number, url, or crun_ id)"}

        repo_full = str(args.get("repo") or "").strip()
        number = ""

        if raw.startswith("crun_"):
            # Resolve the task's own PR, so "why did crun_x fail to preview" is one call.
            with ctx.begin() as c:
                row = c.execute(
                    _text("SELECT pull_request_url, pull_request_number, repo, owner "
                          "FROM code_task_runs WHERE id = :r AND project_id = :p"),
                    {"r": raw, "p": pid}).mappings().first()
            if not row:
                return {"ok": False, "error": f"no code task {raw!r} in this project"}
            if not (row.get("pull_request_url") or row.get("pull_request_number")):
                return {"ok": False, "error": f"{raw} has not opened a pull request",
                        "note": "a task with no PR has nothing to read"}
            number = str(row.get("pull_request_number") or "")
            if not repo_full and row.get("owner") and row.get("repo"):
                repo_full = f"{row['owner']}/{row['repo']}"
            if not number and row.get("pull_request_url"):
                raw = str(row["pull_request_url"])

        if not number:
            m = _re.search(r"github\.com/([^/]+)/([^/]+)/pull/(\d+)", raw)
            if m:
                repo_full = repo_full or f"{m.group(1)}/{m.group(2)}"
                number = m.group(3)
            elif raw.isdigit():
                number = raw
        if not number:
            return {"ok": False, "error": f"could not read a PR number out of {raw!r}"}

        if not repo_full:
            owner = getattr(ctx.settings, "target_github_owner", "") or ""
            name = getattr(ctx.settings, "target_github_repo", "") or ""
            repo_full = f"{owner}/{name}" if owner and name else ""
        if "/" not in repo_full:
            return {"ok": False, "error": "repo is required as owner/repo"}

        token = getattr(ctx.settings, "github_token", "") or ""

        def get(path: str):
            req = _ureq.Request(
                f"https://api.github.com{path}",
                headers={"Accept": "application/vnd.github+json",
                         **({"Authorization": f"Bearer {token}"} if token else {})})
            with _ureq.urlopen(req, timeout=25) as resp:
                return _json.loads(resp.read().decode() or "{}")

        try:
            pr = get(f"/repos/{repo_full}/pulls/{number}")
            files = get(f"/repos/{repo_full}/pulls/{number}/files?per_page=100")
        except Exception as exc:  # noqa: BLE001 — a dead network is an answer, not a crash
            return {"ok": False, "error": f"could not read {repo_full}#{number}: {exc}"[:250]}

        listed = [{"path": f.get("filename"), "status": f.get("status"),
                   "additions": f.get("additions"), "deletions": f.get("deletions"),
                   **({"patch": (f.get("patch") or "")[:4000]}
                      if args.get("with_diff") else {})}
                  for f in (files or [])]
        return {
            "repo": repo_full, "number": int(number),
            "title": pr.get("title"), "state": pr.get("state"),
            "merged": bool(pr.get("merged_at")), "branch": (pr.get("head") or {}).get("ref"),
            "additions": pr.get("additions"), "deletions": pr.get("deletions"),
            "changed_files": pr.get("changed_files"),
            "files": listed,
        }

    @reg.tool(
        "run_on_host",
        "Run ANY command on a registered deployment host over SSH — restart or delete "
        "containers, edit files, install packages, inspect anything. This changes the machine. "
        "Use inspect_host when you only need to look. Requires BOSS_AGENT_HOST_SHELL; without "
        "it this refuses and only the read-only verbs are available.",
        _obj({"host": {"type": "string", "description": "host name, id or address"},
              "command": {"type": "string", "description": "the shell command to run"},
              "timeout_s": {"type": "integer", "minimum": 5, "maximum": 600, "default": 120}},
             ["host", "command"]),
        kind="write",
        reversibility="irreversible",
    )
    def run_on_host(ctx, *, project_id, actor, args):
        """A shell on the operator's own machine, off by default and always recorded.

        `inspect_host` deliberately allows only read-only verbs, and that was the right default
        — but it is a floor, not a ceiling. An operator who wants their agent to clear a host
        and rebuild it is asking for something reasonable about hardware they own, and telling
        them to go and do it by hand is not a security control, it is a missing feature.

        So: the capability exists, it is off unless switched on, and every command lands in the
        event stream with the actor attached. What makes this survivable is not a filter on the
        command — a filter on shell input is a promise nobody can keep, which is why
        `inspect_host` allow-lists verbs instead — it is that the blast radius is one registered
        host, the switch is deliberate, and the audit trail names who asked.
        """
        from ..services.core import emit_event
        from ..services.deploy_service import (_discard_key_file, _materialise_key, _ssh,
                                               _ssh_key_file)
        from ..services.secret_backends import resolve_target_secret

        if not getattr(ctx.settings, "agent_host_shell", False):
            return {"ok": False,
                    "error": "host shell is disabled (BOSS_AGENT_HOST_SHELL is not set).",
                    "note": "inspect_host still works for read-only commands."}

        raw = (args.get("command") or "").strip()
        if not raw:
            return {"ok": False, "error": "command is required"}

        # A PERSON PRESSES THE BUTTON. Only /v1/engine/agent/action sets this, and it sets it
        # after an operator has seen the exact host and the exact command on a button and
        # clicked it. A model calling this tool directly gets sent back to propose_action,
        # which is what puts the command in front of the operator in the first place.
        #
        # Redundant since the registry enforces this for every tool declared irreversible, and
        # kept deliberately: this one carries the command verbatim, and defence in depth on the
        # single most dangerous tool BOSS owns is worth one duplicated check.
        if not args.get("_operator_confirmed"):
            return {
                "ok": False,
                "requires_confirmation": True,
                "error": "running a command on a host needs the operator's confirmation.",
                "do_this": (
                    "call propose_action with tool='run_on_host' and these exact args, plus a "
                    "label saying what it does and why. The console renders it as a button; "
                    "nothing runs until the operator clicks it."
                ),
                "proposed_args": {"host": args.get("host"), "command": raw},
            }
        want = (args.get("host") or "").strip()
        timeout = int(args.get("timeout_s") or 120)

        with ctx.begin() as c:
            row = c.execute(_text(
                "SELECT * FROM deployment_hosts "
                "WHERE project_id = :p AND (id = :h OR name = :h OR address = :h)"),
                {"p": project_id, "h": want}).mappings().first()
            if row is None:
                return {"ok": False, "error": f"no registered host {want!r} in this project"}
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
            # Recorded BEFORE it runs. A command that takes the box down must still be
            # answerable for afterwards, and a receipt written on the way out is not written
            # when the thing never comes back.
            emit_event(c, project_id, "host.command", actor, "deployment_host", host["id"],
                       {"host": host["name"], "address": host["address"], "command": raw})

        try:
            res = _ssh(key, host["address"], (host.get("ssh_user") or "root"),
                       int(host.get("ssh_port") or 22), raw, timeout=timeout)
        finally:
            if ephemeral:
                _discard_key_file(ephemeral)

        with ctx.begin() as c:
            emit_event(c, project_id, "host.command_result", actor,
                       "deployment_host", host["id"],
                       {"host": host["name"], "command": raw, "exit_code": res.returncode})
        return {"ok": res.returncode == 0, "host": host["name"], "command": raw,
                "exit_code": res.returncode,
                "stdout": (res.stdout or "")[-8000:], "stderr": (res.stderr or "")[-4000:]}

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
        "'was this deliberate', which no amount of reading the CURRENT code can. "
        "Pass run_id for an app BOSS built, so the log covers ITS branch rather than a "
        "default branch that may not contain the app at all.",
        _obj({"repo": {"type": "string", "description": "'boss' (default) or 'owner/name'"},
              "run_id": {"type": "string",
                         "description": "a code task run (crun_…): history of THAT run's "
                                        "branch"},
              "ref": {"type": "string", "description": "explicit branch/tag/sha"},
              "file": {"type": "string", "description": "optional path to scope the log"},
              "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
              "search": {"type": "string",
                         "description": "optional: commits touching this string"}}),
    )
    def git_history(ctx, *, project_id, actor, args):
        repo, ref, err = _resolve_source(ctx, args)
        if err:
            return {"ok": False, "error": err}
        root, err = _repo_root(ctx.settings, repo, ref)
        if root is None:
            return {"ok": False, "error": err}
        # AN IMAGE IS NOT A CHECKOUT. BOSS's own source is COPYed into the container without
        # .git, so this returned git's own words — "fatal: not a git repository" — to an agent
        # that had reasonably asked when a fix shipped. That reads as a broken tool; the true
        # answer is that this install has no history to read, and the thing the question wants
        # is which source it is running, which the build stamps.
        if not _os.path.isdir(_os.path.join(root, ".git")):
            sha = (_os.environ.get("BOSS_SOURCE_SHA") or "").strip()
            built = (_os.environ.get("BOSS_BUILT_AT") or "").strip()
            return {
                "ok": False,
                "error": (f"{repo} has no git history in this install — it runs from a built "
                          f"image, which carries the source but not the repository. This is "
                          f"permanent here, not a transient failure."),
                "running_source_sha": sha or "unknown (image built without a stamp)",
                "built_at": built or "unknown",
                "how_to_answer_anyway": (
                    "To check whether a change is in the RUNNING build, read the file with "
                    "read_code/search_code — the source in the image is what is executing. "
                    "For when it shipped, the commit above identifies the build; for a "
                    "customer repository, repo_activity and read_pull_request read GitHub "
                    "directly and do not need a checkout."),
                "use_instead": ["read_code", "search_code", "repo_activity",
                                "read_pull_request", "inspect_self"],
            }
        # A SHALLOW CLONE HAS NO HISTORY TO SHOW. Customer repos are cloned --depth 1, so
        # `git log` returned exactly one commit — and a one-entry history reads as "nothing
        # ever changed here", which is the opposite of what this tool is for.
        _deepen(root)
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
        return {"repo": repo, "ref": ref or "(default branch)", "commits": commits}

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
    _register_app_identity_tools(reg)
    _register_image_tools(reg)
    _register_deployment_target_tools(reg)
    _register_role_experience_tools(reg)
    _register_standard_tools(reg)
    _register_port_tools(reg)
    _register_run_turn_tools(reg)
    _register_diagnosis_tools(reg)
    _register_debug_session_tools(reg)
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
    # SETUP REPAIR, because a diagnosis with no matching capability is a loop — and setup is
    # where that loop hurts most, since it is the first thing a customer touches. Asked twice
    # about the same failed GitHub connection, BOSS explained it correctly and then told the
    # operator to go and press Test access itself: "just explaining the problem would not help
    # anyone." Both of these are reversible and neither takes a credential.
    "test_github_access",
    "set_project_setting",
    # The escape hatch from a registry of fixed questions: a real debugging session.
    "investigate",
    # Reconciling a run with a world that has moved on. Named by an investigation as the
    # operation whose absence forced it to write SQL by hand.
        # RETRYING ONE DEAD STEP IS THE ACTION THAT FOLLOWS A DIAGNOSIS.
    #
    # BOSS could explain exactly why a panel step failed and had no way to offer the retry,
    # so it ended with "tell me to go ahead" and no button. Reachable from chat means the
    # operator gets the button; it is still a write, so nothing runs until they press it.
    "retry_workflow_step",
    # MEMORY IS WORTHLESS IF THE AGENT CANNOT REACH IT FROM A CONVERSATION. The moment an
    # operator corrects you is the only moment that correction exists — it has to be writable
    # from the turn in which it is said, not from a separate admin path.
    "remember",
    "forget",
    "update_ledger",
"recheck_workflow_run",
    # Somewhere to deploy TO. Without this the only route to a first deployment was write_db
    # with hand-composed SQL — which BOSS took, against a schema it had to guess at.
    "create_deployment_target",
    # Queue and run state
    "cancel_job",
    "set_job_priority",
    "close_orphaned_runs",
    "link_run_origin",
    # Recovering work that exists
    "retry_code_task",
    "extend_code_task",
    "steer_code_task",
    # "CONNECT MY CODEX SUBSCRIPTION" IS A SENTENCE SOMEBODY SAYS. Importing an agent the
    # operator already pays for is setup, and setup is the part they are most likely to ask
    # for in words rather than go looking for a page. It stores no credential, proves the
    # session in a read-only sandbox, and records an entitlement row.
    "connect_coding_agent",
    "add_review_comment",
    "resolve_review_comment",
    "cancel_code_task",
    # Correcting where an app is addressed. BOSS diagnosed the same deployment four times
    # and could not act on any of it, because changing a target was not something it could do —
    # so it offered "tell me which port and I'll update the target" for an action it did not
    # have. A diagnosis with no matching capability is a loop.
    "retarget_app",
    # Making the artwork an app needs. Costs money per image, which is why it is a write and
    # not a read; it is reachable because an app that has to ship with pictures cannot be
    # finished by describing them.
    "generate_image",
    # Repairing how work is GROUPED. One product recorded under two identities shows as two
    # cards with the attempts, PRs and spend divided between them — a bug of ours that the
    # customer otherwise keeps for good, since app_id is stamped at creation and never
    # re-derived. Reversible: merge_apps records what each identity was.
    "merge_apps",
    # Turning a role's conclusion into a rule that binds future builds. Without this an agent
    # can conclude that every service must have its ports checked for public exposure, say it
    # in a meeting, and change nothing: standards were Python constants, so only the platform
    # could add one and the workforce could mandate precisely nothing. Reversible — a standard
    # is retired, not deleted, and carries who mandated it and why.
    "mandate_standard",
    # Writing the fix. A customer's bug is repaired with code.
    "create_code_task",
    # AUTHORING THE PATH, as opposed to starting work on it.
    #
    # These three change what workflows EXIST; none of them starts a run or spends a model
    # budget. `draft_workflow` and `draft_custom_step` therefore execute directly, because a
    # conversation that has to be confirmed after every sentence is not a conversation — and a
    # workflow that exists but is not mapped to a class of work changes no behaviour until
    # somebody chooses it. `delete_workflow` is in DESTRUCTIVE: it removes something a person
    # authored, and no sentence should be able to make the model press that itself.
    "draft_workflow",
    "draft_custom_step",
    "delete_workflow",
    # AUTHORING THE UNIT OF DOING. A skill is what a custom step runs, so it is the same kind
    # of act as the two above — it changes what CAN be built, not what is running — and it
    # spends nothing. It is how an operator gets a skill by describing one instead of filling
    # in a form they should not have to understand.
    "draft_skill",
    # STARTING WORK MEANS STARTING A WORKFLOW.
    #
    # `create_discovery` and `request_meeting` are deliberately NOT here. Research and the PRD
    # panel are steps of a workflow, and one started on its own belongs to no run: the operator
    # asked BOSS to research a stock-recommendation site, BOSS ran a bare discovery, and the
    # finished dossier was reachable from nothing — no run linked to it, and the console has no
    # standing page for one because nobody should be starting them individually. The workflow is
    # the unit of work and its document is the outcome.
    "start_workflow",
    # Delivery and environment
    # A shell on the operator's own host. Gated a second time by BOSS_AGENT_HOST_SHELL, so
    # admitting it here does not by itself hand anybody a shell.
    "run_on_host",
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

#: REGISTERED, AND DELIBERATELY NOT REACHABLE FROM CHAT.
#:
#: Every other write in the default registry is chat-reachable on purpose — owning a tool and
#: not being allowed to use it is how "BOSS only gives advice" happened, and a test enforces
#: it. These two are the stated exception: they are how the workflow's `research` and `meeting`
#: STEPS do their work (see workflow_capabilities), and they must keep working there. What they
#: must not be is a way for a conversation to start one on its own — the operator's rule is
#: that research and the panel only ever happen inside a workflow, so that the run is what you
#: open and its document is the outcome. `start_workflow` is the chat's way in.
_WORKFLOW_ONLY_TOOLS = frozenset({"create_discovery", "request_meeting"})


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
                # AND PRESERVE HOW HARD IT IS TO UNDO — this was dropped, which silently
                # DISARMED the confirmation gate on the only registry the chat agent uses.
                #
                # `reversibility` defaults to "safe", and `self_executable` is derived from it.
                # So every rebuilt spec came out self-executable: `write_db`, declared
                # "destroys_work" at its definition, could run arbitrary INSERT/UPDATE/DELETE
                # from a model's own tool call with nobody asked. It did — a hand-composed
                # INSERT into deployment_targets, against a table name it guessed wrong.
                #
                # The guard I wrote after the 2026-09-02 wipe was tested against
                # build_default_registry, which is not the registry this path uses. A safety
                # property that holds in the registry nobody calls is not a safety property.
                reversibility=spec.reversibility,
            )
        )
    return reg
