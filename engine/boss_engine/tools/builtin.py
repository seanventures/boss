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
import shlex
import subprocess as _subprocess
from typing import Any, Optional as _Optional

from .._compat import services_module
from ..services.workflow_step_order import in_declared_order as _in_declared_order
# Drafting a workflow from a described sequence is shared with the console's Describe (the
# `/v1/delivery-workflows/draft` endpoint), so it lives in a service and the tool imports it.
from ..services.workflow_drafting import (
    _ensure_steps_for_mcp_tools,
    _ensure_steps_for_skills,
    _resolve_subworkflows,
    _spec_from_steps,
)
from .registry import PROJECT_ARG, PROJECT_ARG_SCHEMA, ToolRegistry, ToolSpec


# ---------------------------------------------------------------------------
# schema fragments
# ---------------------------------------------------------------------------
def _project_settings_catalogue() -> str:
    from ..services import project_settings

    return project_settings.catalogue_said()


def _obj(props: dict, required: list[str] | None = None) -> dict:
    return {
        "type": "object",
        "properties": props,
        "required": required or [],
        "additionalProperties": False,
    }


_LIMIT = {"type": "integer", "minimum": 1, "maximum": 200, "default": 25}
_PAID_EFFECT_EDITIONS = frozenset({
    "cloud", "enterprise", "enterprise_cloud", "enterprise_self_hosted",
})


_INSPECT_HOST_VERBS = (
    "docker ps", "docker logs", "docker inspect", "docker stats --no-stream",
    "docker images", "df", "free", "uptime", "uname", "ls", "cat /etc/os-release",
    "systemctl status", "journalctl", "ss -", "netstat", "ps aux", "whoami",
)
_INSPECT_HOST_SHELL_META = frozenset("&;|<>`$\\")
_JOURNALCTL_FLAGS = frozenset({
    "-b", "--boot", "-k", "--dmesg", "-q", "--quiet", "-r", "--reverse",
    "-x", "--catalog", "--no-pager", "--utc",
})
_JOURNALCTL_VALUE_FLAGS = frozenset({
    "-u", "--unit", "-n", "--lines", "-p", "--priority", "-o", "--output",
    "--since", "--until",
})


def app_logs_shell(*, compose_project: str | None, container: str, tail: int) -> str:
    """The one read command that prints an app's container logs: `docker ps` to find them,
    `docker logs` for each. Every name is BOSS's own and quoted; nothing else runs."""
    import shlex as _shlex

    tail = max(1, min(int(tail or 80), 400))
    if compose_project:
        filt = _shlex.quote(f"label=com.docker.compose.project={compose_project}")
    else:
        filt = _shlex.quote(f"name=^{container}$")
    return (f"for c in $(docker ps -a --filter {filt} --format '{{{{.Names}}}}' | head -8); "
            f"do echo \"===== $c\"; docker logs --tail {tail} \"$c\" 2>&1; done")


def _app_logs_command(ctx: Any, project_id: str, args: dict) -> tuple[Any, Any]:
    """(command, (target, host_id, where the names came from)), or (None, the reason)."""
    import json as _json

    from sqlalchemy import text as _text

    from ..services.deploy_service import _app_name

    want = str(args.get("app") or "").strip()
    env = str(args.get("environment") or "").strip()
    if not want:
        return None, "name the app or its deployment target"
    with ctx.begin() as c:
        rows = c.execute(_text(
            "SELECT id, app_name, repo, environment, host_id FROM deployment_targets "
            "WHERE project_id = :p AND (id = :w OR lower(app_name) = lower(:w)) "
            "AND (:e = '' OR environment = :e) ORDER BY updated_at DESC NULLS LAST"),
            {"p": project_id, "w": want, "e": env}).mappings().all()
        if not rows:
            return None, f"no deployment target for {want!r}" + (f" in {env}" if env else "")
        target = dict(rows[0])
        stack = c.execute(_text(
            "SELECT manifest, host_id FROM stack_deployments WHERE project_id = :p "
            "AND target_id = :t ORDER BY created_at DESC LIMIT 1"),
            {"p": project_id, "t": target["id"]}).mappings().first()
    host_id = (stack or {}).get("host_id") or target.get("host_id")
    if not host_id:
        return None, f"{target['app_name']} has no host recorded"
    manifest = (stack or {}).get("manifest") or {}
    if isinstance(manifest, str):
        manifest = _json.loads(manifest or "{}")
    project = str(manifest.get("compose_project") or (
        manifest.get("name") if manifest.get("kind") == "compose" else "") or "").strip()
    single = _app_name(target.get("repo") or "", target.get("app_name"))
    command = app_logs_shell(compose_project=project or None, container=single,
                             tail=int(args.get("tail") or 80))
    source = f"compose project {project}" if project else f"container {single}"
    return command, (target, host_id, source)


def _inspect_host_command(raw_value: object) -> tuple[str | None, str | None]:
    """Return one shell-safe read command, or a reason before any credential/SSH access.

    SSH transports a *single string* interpreted by a remote shell. A permitted prefix such
    as ``ls`` does not make ``ls & touch /tmp/x`` safe. Reject shell syntax before tokenizing,
    verify the complete verb and its read-only options, then quote every token for that shell.
    """
    if not isinstance(raw_value, str):
        return None, "a read-only command is required"
    if any(ord(ch) < 32 or ord(ch) == 127 or ch in _INSPECT_HOST_SHELL_META
           for ch in raw_value):
        return None, "shell metacharacters and control characters are not permitted"
    try:
        words = shlex.split(raw_value.strip(), posix=True)
    except ValueError:
        return None, "unbalanced command quoting is not permitted"
    if not words:
        return None, "a read-only command is required"

    verb = words[0]
    if verb == "docker":
        allowed = len(words) >= 2 and words[1] in {"ps", "logs", "inspect", "images"}
        allowed = allowed or (len(words) >= 3 and words[1:3] == ["stats", "--no-stream"])
    elif verb == "cat":
        allowed = words == ["cat", "/etc/os-release"]
    elif verb == "systemctl":
        allowed = (len(words) >= 2 and words[1] == "status"
                   and all(re.fullmatch(r"[A-Za-z0-9_.@:-]+", unit)
                           for unit in words[2:]))
    elif verb == "journalctl":
        allowed = True
        index = 1
        while index < len(words):
            option = words[index]
            if option in _JOURNALCTL_FLAGS:
                index += 1
            elif option in _JOURNALCTL_VALUE_FLAGS and index + 1 < len(words):
                value = words[index + 1]
                if not value or value.startswith("-"):
                    allowed = False
                    break
                index += 2
            elif any(option.startswith(flag + "=") and option[len(flag) + 1:]
                     for flag in _JOURNALCTL_VALUE_FLAGS if flag.startswith("--")):
                index += 1
            else:
                allowed = False
                break
    elif verb == "ss":
        # `ss -K`/`ss --kill` terminates sockets; only listing switches belong here.
        allowed = len(words) >= 2 and all(
            re.fullmatch(r"-[tulnpaHs46]+", option)
            or option in {"--tcp", "--udp", "--listening", "--numeric",
                          "--processes", "--all", "--summary", "--no-header"}
            for option in words[1:]
        )
    elif verb == "ps":
        allowed = words == ["ps", "aux"]
    elif verb in {"df", "free", "uptime", "uname", "ls", "netstat", "whoami"}:
        allowed = True
    else:
        allowed = False
    if not allowed:
        return None, "only read-only commands are allowed here: " + ", ".join(
            _INSPECT_HOST_VERBS
        )
    return shlex.join(words), None


def _community_restricted(settings: Any) -> bool:
    """Unknown editions get the conservative Community read boundary."""
    edition = str(getattr(settings, "boss_edition", "community") or "community")
    return edition.strip().lower() not in _PAID_EFFECT_EDITIONS


def _community_raw_sql_disabled(settings: Any) -> bool:
    """Raw SQL has no tenant fence or domain policy and is unsafe in Community."""
    return _community_restricted(settings)


def _raw_sql_admin_authorized(ctx: Any, actor: str) -> bool:
    """Only an explicit local CLI call can use unscoped SQL in a paid edition."""
    return actor == "cli" and getattr(ctx, "raw_sql_admin_authorized", False) is True


# ===========================================================================
# READ TOOLS — the "overall view of the workflow engine"
# ===========================================================================
def _register_read_tools(reg: ToolRegistry) -> None:
    @reg.tool(
        "project_status",
        "Snapshot of everything happening in a project: counts of running/queued "
        "code tasks, discovery runs, meetings, background jobs and live previews. "
        "The go-to answer for 'what's running / how busy is BOSS right now?'.",
        _obj({"project_id": {"type": "string", "description": "override the default project"}}), permission="project:read")
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
        "list_projects",
        "Every project in this organization the caller may read: id, slug, name, how many "
        "repositories each has registered. The answer to 'which projects exist / does project "
        "X exist?' — never create a project to find out whether it exists.",
        _obj({}), permission="project:read")
    def list_projects(ctx, *, project_id, actor, args):
        from .registry import CURRENT_TOOL_AUTH

        auth = CURRENT_TOOL_AUTH.get()
        org = str(getattr(getattr(auth, "principal", None), "org_id", "")
                  or getattr(ctx.settings, "default_org_id", "") or "")
        with ctx.begin() as c:
            rows = c.execute(_text(
                "SELECT p.id, p.slug, p.name, p.description, "
                "       (SELECT count(*) FROM github_repository_bindings b "
                "         WHERE b.project_id = p.id AND b.status = 'active') AS repositories "
                "  FROM projects p JOIN workspaces w ON w.id = p.workspace_id "
                " WHERE (:org = '' OR w.org_id = :org) ORDER BY p.slug"),
                {"org": org}).mappings().all()
        readable = [dict(r) for r in rows if _may_read_project(auth, str(r["id"]))]
        return {"count": len(readable), "projects": _json_safe(readable),
                "current_project": project_id,
                "note": "Every project tool takes `project` (an id or slug from this list) to "
                        "read or act in that project."}

    @reg.tool(
        "describe_project",
        "One project's real state in one call: whether it exists, its name and description, "
        "the repositories registered to it (with role, status and default branch), and EVERY "
        "project setting with its stored value, its effective value and the values it accepts. "
        "Pass `project` for a project other than this conversation's. Read this before "
        "reporting on or changing a project's repositories or settings.",
        _obj({}), permission="project:read")
    def describe_project(ctx, *, project_id, actor, args):
        from ..services import project_settings as ps

        with ctx.begin() as c:
            row = c.execute(_text(
                "SELECT id, slug, name, description, created_at FROM projects WHERE id = :p"),
                {"p": project_id}).mappings().first()
            if row is None:
                return {"ok": False, "error": "project_not_found", "project": project_id}
            repos = c.execute(_text(
                "SELECT full_name, role, status, default_branch, connection_id, created_at "
                "  FROM github_repository_bindings WHERE project_id = :p "
                " ORDER BY (role = 'primary') DESC, full_name"),
                {"p": project_id}).mappings().all()
            stored = ps.get_all(c, project_id)
        settings_out = []
        for key, spec in ps.KNOBS.items():
            default = getattr(ctx.settings, spec["attr"], None)
            settings_out.append({
                "key": key, "label": spec.get("label"),
                "stored": stored.get(key),
                "effective": stored[key] if key in stored else default,
                "source": "project" if key in stored else "deployment default",
                "allowed": ps.allowed_values_said(spec)})
        return {"ok": True, "project": _json_safe(dict(row)),
                "repositories": _json_safe([dict(r) for r in repos]),
                "repository_count": len(repos),
                "settings": _json_safe(settings_out),
                "settings_set_on_this_project": sorted(stored),
                "note": ("No repository is in this project yet. The org's GitHub connection "
                         "serves every project: list_org_repositories shows what it reaches, "
                         "allow_repository allows one, register_repository adds it here."
                         if not repos else "")}

    @reg.tool(
        "list_code_tasks",
        "Recent coding-agent task runs (newest first) with status, phase, title and "
        "PR link. Use to answer 'show me the builds / what happened to task X'.",
        _obj({"project_id": {"type": "string"}, "limit": _LIMIT}), permission="code_task:read")
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
        _obj({"run_id": {"type": "string"}}, ["run_id"]), permission="code_task:read")
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
        out = {"run": _json_safe(dict(row))}
        with ctx.begin() as c:
            step = _workflow_step_of(c, project_id, run_id)
        if step:
            out["part_of_workflow_run"] = step
        # The one state every surface says (factory spec §4.4): the Floor, the cockpit and
        # this tool read the same derivation.
        try:
            fv = services_module("factory_view")
            with ctx.begin() as c:
                out["build_state"] = _json_safe(fv.state_of(c, project_id, run_id))
        except Exception:  # noqa: BLE001 — the run is the answer; its state word is extra
            pass
        return out

    # ── BOSS fixes itself (BOSSAPP-571): read the self-fixes and the autonomy metric ─────────
    # Read only. No tool starts, approves, installs, removes or sends a self-fix: those are a
    # person's acts in the console (and approval is the decisions API), never an agent's.
    @reg.tool(
        "list_self_fixes",
        "BOSS's self-fixes (newest first): bugs in BOSS's own code that BOSS diagnosed, fixed "
        "with a regression test and its reviewers, and installed after a person approved — "
        "with status chip, cause, area, run, upstream state and the one-line progress.",
        _obj({"project_id": {"type": "string"}, "status": {"type": "string"},
              "limit": _LIMIT}), permission="improvement:read")
    def list_self_fixes(ctx, *, project_id, actor, args):
        sf = services_module("self_fix")
        pid = args.get("project_id") or project_id
        with ctx.begin() as c:
            items = sf.list_self_fixes(c, project_id=pid, status=args.get("status") or None,
                                       limit=int(args.get("limit") or 25))
        keep = ("id", "status", "chip", "stage", "area", "signature", "class_signature",
                "run_id", "step_id", "confidence", "tier", "refusal", "upstream_state",
                "upstream_url", "created_at", "updated_at")
        return {"count": len(items), "self_fixes": [
            {**{k: _json_safe(i.get(k)) for k in keep},
             "title": (i.get("pr") or {}).get("title"), "line": sf.step_line(i)} for i in items]}

    @reg.tool(
        "get_self_fix",
        "One BOSS self-fix by id (sfx_…) with its lineage: problem → diagnosis (and the "
        "rejected hypothesis) → fix → tests (red then green) → review → approval → install → "
        "resumed step → upstream, its events, and the approval card while it waits for one.",
        _obj({"self_fix_id": {"type": "string"}}, ["self_fix_id"]),
        permission="improvement:read")
    def get_self_fix(ctx, *, project_id, actor, args):
        sf = services_module("self_fix")
        si = services_module("self_fix_install")
        sfx_id = str(args["self_fix_id"])
        with ctx.begin() as c:
            found = sf.get(c, sfx_id)
            if found is None or found.get("project_id") != project_id:
                return {"ok": False, "error": f"no self-fix {sfx_id} in this project"}
            out = sf.lineage(c, sfx_id) or {}
            if found.get("status") == sf.AWAITING_APPROVAL:
                out["card"] = si.card(found)
        (out.get("self_fix") or {}).pop("bundle", None)
        out["line"] = sf.step_line(found)
        return _json_safe(out)

    @reg.tool(
        "autonomy_report",
        "The autonomy metric: of the problems runs hit, how many BOSS's agents solved end to "
        "end, how many a person solved, how many an outside coding agent solved, how many are "
        "open — per run (run_id) or for the project over a window (7d, 30d, 90d) with a "
        "weekly trend and the BOSS-side cut.",
        _obj({"project_id": {"type": "string"}, "run_id": {"type": "string"},
              "window": {"type": "string", "enum": ["7d", "30d", "90d"]}}),
        permission="improvement:read")
    def autonomy_report(ctx, *, project_id, actor, args):
        am = services_module("autonomy_metric")
        pid = args.get("project_id") or project_id
        with ctx.begin() as c:
            if args.get("run_id"):
                return _json_safe(am.run_report(c, project_id=pid, run_id=str(args["run_id"])))
            return _json_safe(am.report(c, project_id=pid, window=args.get("window") or "30d"))

    @reg.tool(
        "list_running_work",
        "Everything actively running in the background right now — running code "
        "tasks and non-terminal jobs — with how long each has been going. Answers "
        "'how many tasks are running in the background?'.",
        _obj({"project_id": {"type": "string"}}), permission="job:read")
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
        _obj({"project_id": {"type": "string"}, "limit": _LIMIT}), permission="discovery:read")
    def list_discovery(ctx, *, project_id, actor, args):
        pid = args.get("project_id") or project_id
        with ctx.begin() as c:
            runs = ctx.list_discovery_runs(c, pid, int(args.get("limit") or 25))
        return {"count": len(runs), "runs": [_json_safe(r) for r in runs]}

    @reg.tool(
        "list_meetings",
        "Recent multi-agent PRD meeting runs with status and directive.",
        _obj({"project_id": {"type": "string"}, "limit": _LIMIT}), permission="meeting:read")
    def list_meetings(ctx, *, project_id, actor, args):
        pid = args.get("project_id") or project_id
        limit = int(args.get("limit") or 25)
        with ctx.begin() as c:
            rows = c.execute(
                _text(
                    "SELECT id,status,phase,title,directive,created_at FROM meeting_runs "
                    "WHERE project_id=:p ORDER BY created_at DESC LIMIT :l"
                ),
                {"p": pid, "l": limit},
            ).mappings().all()
        from ..services.meeting_service import meeting_title

        # The title first, so an agent reading the list sees what each meeting is ABOUT; the
        # directive stays for the full brief.
        meetings = [
            {**_json_safe(dict(r)), "title": meeting_title(r["title"], r["directive"])}
            for r in rows
        ]
        return {"count": len(rows), "meetings": meetings}

    @reg.tool(
        "list_previews",
        "App previews with their open URLs AND, for the failed ones, why they failed: "
        "`failure_kind` (port_timeout / no_page_at_root / no_health_route / clone_failed / command_missing / "
        "boot_crash) and the error, which usually names the real fault — a build error, a "
        "missing entry file, a port the app did not honour, or an app that started correctly "
        "and serves no page at '/' because the build produced only an API. Read "
        "`read_preview_log` for the full startup log, then "
        "repair: an app-side fault is fixed with extend_code_task or steer_code_task on the "
        "run, and start_preview tries again.",
        _obj({"project_id": {"type": "string"}, "limit": _LIMIT}), permission="preview:read")
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
             ["preview_id"]), permission="preview:read")
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
        _obj({"run_id": {"type": "string"}, "project_id": {"type": "string"}}, ["run_id"]), permission="preview:read")
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
        _obj({}), permission="sandbox:read")
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
        "Local admin CLI only: run a single read-only SELECT/WITH query against the "
        "engine's Postgres. This is unscoped SQL; use project-scoped read tools in "
        "chat, MCP, workflows and API sessions. Results are capped.",
        _obj(
            {
                "sql": {"type": "string", "description": "a single SELECT or WITH…SELECT statement"},
                "max_rows": {"type": "integer", "minimum": 1, "maximum": 500, "default": 100},
            },
            ["sql"],
        ), permission="host:exec")
    def query_db(ctx, *, project_id, actor, args):
        if _community_raw_sql_disabled(ctx.settings):
            return {"ok": False, "error": "Unrestricted SQL reads are unavailable in Community. "
                    "Use the project-scoped read tools."}
        if not _raw_sql_admin_authorized(ctx, actor):
            return {"ok": False, "error": "Unrestricted SQL reads require an explicit local admin CLI call. "
                    "Use project-scoped read tools here."}
        sql = (args.get("sql") or "").strip().rstrip(";")
        ok, why = _is_read_only(sql)
        if not ok:
            return {"ok": False, "error": f"rejected: {why}"}
        max_rows = int(args.get("max_rows") or 100)
        try:
            with ctx.begin() as c:
                _bound_statement(c)
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
        # REDACTED like every other read an agent or a person sees here: credential columns
        # dropped, secret-shaped values scrubbed (agent_baseline.redact_for_agent).
        from ..services.agent_baseline import redact_for_agent

        return {
            "row_count": len(capped),
            "truncated": len(rows) > max_rows,
            "rows": redact_for_agent([_json_safe(dict(r)) for r in capped]),
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
        _obj({"project_id": {"type": "string"}, "limit": _LIMIT}), permission="host:read")
    def list_deployment_hosts(ctx, *, project_id, actor, args):
        pid = args.get("project_id") or project_id
        limit = int(args.get("limit") or 25)
        with ctx.begin() as c:
            rows = c.execute(
                _text(
                    "SELECT id, name, address, ssh_user, ssh_port, arch, status, labels, "
                    "       port_min, port_max, max_apps, last_preflight_at, last_preflight, "
                    "       key_secret_ref, key_public, admin_identity_file "
                    "FROM deployment_hosts WHERE project_id=:p "
                    "ORDER BY name LIMIT :l"
                ),
                {"p": pid, "l": limit},
            ).mappings().all()
        return {"project_id": pid, "hosts": [_host_said(dict(r)) for r in rows]}

    @reg.tool(
        "describe_deployment_host",
        "One host in full: its last preflight result — reachable, architecture detected, "
        "findings and problems — plus the apps currently deployed onto it. Accepts the host "
        "id, its name, or its IP address. Use before recommending anything be shipped there.",
        _obj({"host": {"type": "string", "description": "host id, name or address"},
              "project_id": {"type": "string"}}, ["host"]), permission="host:read")
    def describe_deployment_host(ctx, *, project_id, actor, args):
        pid = args.get("project_id") or project_id
        needle = str(args.get("host") or "").strip()
        if not needle:
            return {"error": "host is required (id, name or address)"}
        with ctx.begin() as c:
            # Matched three ways because an operator says "192.0.2.202" and the console says
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
        host = _host_said(dict(row), full=True)
        return {"host": host, "apps_on_this_host": [_json_safe(dict(a)) for a in apps]}

    def _host_said(row: dict, *, full: bool = False) -> dict:
        """A host as the console says it: the one state word and the setup stage
        (infra_view.host_word / host_stage), so the agent and the Fleet never disagree."""
        from ..services import infra_view

        ready = infra_view._host_ready(row)
        state = infra_view.host_word(ready)
        stage = infra_view.host_stage(ready)
        out = _json_safe(dict(row))
        # Never hand the model a credential reference it has no use for.
        out.pop("key_secret_ref", None)
        if not full:
            for key in ("last_preflight", "key_public", "admin_identity_file"):
                out.pop(key, None)
        out["state"] = state["word"]
        out["stage"] = stage["label"]
        return out

    @reg.tool(
        "list_deployments",
        "Recent deployments — one-container deployment runs and stack deployments "
        "(boss.deploy.yaml, sdep_…): app, environment, target, commit, status, why it failed "
        "and when. Pass deployment_id (an sdep_… id) for ONE stack deployment in full: its "
        "steps and their states, the failing step and command, the redacted log tail, what the "
        "host has, and the manifest. Use for 'what shipped', 'why did the deploy fail', "
        "'what is on <host> now'.",
        _obj({"project_id": {"type": "string"}, "limit": _LIMIT,
              "deployment_id": {
                  "type": "string",
                  "description": "a stack deployment id (sdep_…) to read in full"}}),
        permission="deployment:read")
    def list_deployments(ctx, *, project_id, actor, args):
        pid = args.get("project_id") or project_id
        limit = int(args.get("limit") or 25)
        wanted = str(args.get("deployment_id") or "").strip()
        from ..services.agent_baseline import redact_for_agent

        with ctx.begin() as c:
            if wanted:
                # READ ONLY, and redacted: the log tail was redacted by value when stored, and
                # is scrubbed again here by shape before any model sees it.
                from ..services import deploy_repair

                owned = c.execute(_text("SELECT 1 FROM stack_deployments WHERE id=:i "
                                        "AND project_id=:p"), {"i": wanted, "p": pid}).first()
                failure = deploy_repair.failure_of_deployment(c, wanted) if owned else None
                if not failure:
                    return {"error": f"no stack deployment {wanted!r} in this project"}
                failure["host_facts_said"] = deploy_repair.facts_sentence(
                    failure.get("host_facts"))
                return {"project_id": pid, "deployment": redact_for_agent(_json_safe(failure))}
            rows = c.execute(
                _text(
                    "SELECT id, app_name, environment, target_kind, target_ref, commit_ref, "
                    "       status, deployment_target_id, decision_id, created_at "
                    "FROM deployment_runs WHERE project_id=:p "
                    "ORDER BY created_at DESC LIMIT :l"
                ),
                {"p": pid, "l": limit},
            ).mappings().all()
            stacks = c.execute(
                _text(
                    "SELECT d.id, t.app_name, t.environment, d.target_id, d.host_id, "
                    "       d.commit_sha, d.status, d.failure_reason, d.workflow_run_id, "
                    "       d.code_task_run_id, d.created_at, d.finished_at "
                    "FROM stack_deployments d "
                    "LEFT JOIN deployment_targets t ON t.id = d.target_id "
                    "WHERE d.project_id=:p ORDER BY d.created_at DESC LIMIT :l"
                ),
                {"p": pid, "l": limit},
            ).mappings().all()
        return {"project_id": pid, "deployments": [_json_safe(dict(r)) for r in rows],
                "stack_deployments": redact_for_agent([_json_safe(dict(r)) for r in stacks])}


def _build_onto_existing_app(ctx, cts, pid: str, actor: str, args: dict, extends: str,
                             finalize_ctx: dict) -> dict:
    """create_code_task for a feature on an app that already exists: the app's next build.

    A fresh task branches from the default branch and scaffolds; an app whose PR is still open
    does not exist there, so the builder wrote a second, parallel app and the Factory showed it
    as a new one. The follow-up path already knows how to continue an app — its branch, its
    repository, its lineage and app id — so this hands it the feature rather than re-deriving
    any of that. Only the workflow's own bookkeeping (the governing run, who started it, which
    agent builds) travels with it.
    """
    with ctx.begin() as c:
        parent = _code_task_in_project(c, extends, pid)
        if parent is None:
            return {"ok": False, "error": f"no app build {extends} in this project"}
        # ONE BUILD ON A BRANCH AT A TIME. A build of this app still on the line (the launch
        # check's repair, a retry) writes the same branch; starting the feature beside it races
        # it — and start_code_task's same-app guard would hand back THAT run instead, so the
        # step would wait on someone else's work as if it were the feature. Wait for it.
        busy = _app_build_in_flight(c, pid, extends)
        if busy:
            said = (f"Waiting for build {busy} of this app to finish: a feature is built on the "
                    "app's branch, one build at a time.")
            if finalize_ctx.get("workflow_run_id"):
                from ..services.workflow_capability_runner import CapabilityWaitError

                raise CapabilityWaitError(said, wait_seconds=60, kind="app_busy")
            return {"ok": False, "error": said}
        agent = _building_agent(c, pid, actor)
        instructions = args.get("instructions") or ""
        preview_id = str(args.get("preview_id") or "").strip()
        if preview_id:
            if _preview_in_project(c, preview_id, pid) is None:
                return {"ok": False, "error": f"no preview {preview_id} in this project"}
            # Said the way the quick change's feedback says it (its anchor), so the builder
            # knows WHICH running app the operator was looking at when they asked.
            instructions = (f"{instructions}\n\nThe operator asked for this while looking at "
                            f"the running preview {preview_id} "
                            f"(anchor: /v1/previews/{preview_id}/web/).").strip()
        try:
            started = cts.start_followup_task(
                c,
                parent_run_id=extends,
                instructions=instructions,
                jobs=ctx.jobs,
                title=_titled_for_the_app(c, cts, extends, args.get("title")),
                acceptance_criteria=cts.with_existing_tests_criterion(
                    args.get("acceptance_criteria")),
                test_commands=args.get("test_commands") or None,
                profile=args.get("profile"),
                actor=actor,
                reference_links=args.get("reference_links") or None,
                workflow_run_id=finalize_ctx.get("workflow_run_id"),
                finalize_extra={"origin": finalize_ctx.get("origin"), "agent": agent},
                release_base_sha=str(args.get("release_base_sha") or "").strip() or None,
            )
        except ValueError as exc:  # a simulated or vanished parent: say so, start nothing
            return {"ok": False, "error": str(exc)}
    owner, repo = started.get("owner"), started.get("repo")
    return {
        "status": "queued",
        "run_id": started["run_id"],
        "project_id": pid,
        "repository": f"{owner}/{repo}" if owner and repo else None,
        "repository_source": "extended_app",
        "binding_id": None,
        "base_branch": started["base_branch"],
        "extended_existing_app": True,
        "extended_from_root": started["root_run_id"],
    }


def _app_build_in_flight(connection, project_id: str, run_id: str):
    """A build of the same app (its lineage or its app id) that is still running, or None."""
    from sqlalchemy import text as _text

    return connection.execute(_text(
        "WITH me AS (SELECT COALESCE(finalize_ctx->>'root_run_id', id) AS root, "
        "                   finalize_ctx->>'app_id' AS app FROM code_task_runs WHERE id = :r) "
        "SELECT c.id FROM code_task_runs c, me WHERE c.project_id = :p "
        "  AND c.status IN ('running', 'queued') "
        "  AND (COALESCE(c.finalize_ctx->>'root_run_id', c.id) = me.root "
        "       OR (me.app IS NOT NULL AND c.finalize_ctx->>'app_id' = me.app)) "
        "ORDER BY c.created_at LIMIT 1"), {"r": run_id, "p": project_id}).scalar()


def _multirepo_app_key(c, pid: str, args: dict, extends: str) -> tuple[str, str]:
    """(app_key, app_name) of the app this build is for: the composer's own name for it (the
    workflow run's `repo_layout`), else the app the build extends, else the title."""
    from sqlalchemy import text as _text

    from ..multirepo import components as _components

    wf = str(args.get("workflow_run_id") or "").strip()
    if wf:
        row = c.execute(_text("SELECT input FROM workflow_runs WHERE id = :w"), {"w": wf}).scalar()
        layout = (row or {}).get("repo_layout") if isinstance(row, dict) else None
        if isinstance(layout, dict) and layout.get("app_name"):
            name = str(layout["app_name"])
            return _components.app_key_of(name), name
    if extends:
        parent = c.execute(_text("SELECT id, title, spec, finalize_ctx FROM code_task_runs WHERE id = :r "
                                 "AND project_id = :p"), {"r": extends, "p": pid}).mappings().first()
        if parent:
            name = services_module("factory_view").app_name(dict(parent))
            return _components.app_key_of(name), name
    title = str(args.get("title") or "")
    from ..services.naming import project_name

    name = project_name(title)[0]
    return _components.app_key_of(title), name


def _multirepo_change(ctx, pid: str, actor: str, args: dict, extends: str, finalize_ctx: dict) -> dict | None:
    """Start a multi-repo change set when this build's app has several components, else None."""
    from ..multirepo import change_sets as _change_sets
    from ..multirepo import components as _components
    from ..multirepo.common import MultiRepoError

    with ctx.begin() as c:
        try:
            key, name = _multirepo_app_key(c, pid, args, extends)
        except Exception:  # noqa: BLE001 — no app to resolve: the ordinary path decides
            return None
        if not key or not _components.is_multi(c, pid, key):
            return None
        title = str(args.get("title") or "Change")
        if name and not title.lower().startswith(name.lower()):
            title = f"{name}: {title}"
        try:
            started = _change_sets.start(
                c, ctx.settings, ctx.jobs, project_id=pid, app_key=key, title=title,
                instructions=str(args.get("instructions") or title),
                acceptance_criteria=args.get("acceptance_criteria") or [],
                test_commands=args.get("test_commands") or [], actor=actor,
                workflow_run_id=str(args.get("workflow_run_id") or "") or None, task_id=args.get("task_id"),
                finalize_extra={k: v for k, v in finalize_ctx.items() if k in ("origin", "agent")})
        except MultiRepoError as refused:
            return {"ok": False, "status": "failed", "error": refused.message, "reason": refused.message}
    return {"status": "queued", "change_set_id": started["id"], "project_id": pid,
            "repositories": [lane["full_name"] for lane in started["lanes"]], "multirepo": True}


def _titled_for_the_app(connection, cts, run_id: str, title) -> str:
    """"<App>: <feature>". The Factory names an app from its newest build's title, so a build
    titled only for the feature would rename the app on screen to the feature it gained."""
    feature = str(title or "").strip()
    parent = cts.load_code_task_run(connection, run_id) or {}
    app = services_module("factory_view").app_name(parent) if parent else ""
    if not app or app == "Untitled app":
        return feature or "Change"
    if feature.lower().startswith(app.lower()):
        return feature
    return f"{app}: {feature or 'a change'}"


def _building_agent(connection, project_id: str, actor: str) -> dict | None:
    """{role_key, agent_id} when `actor` is an agent on this project (not a person), else None."""
    from sqlalchemy import text as _text

    key = str(actor or "").strip().lower()
    if not key or key in ("human", "workflow_runner"):
        return None
    try:
        with connection.begin_nested():
            row = connection.execute(
                _text("SELECT r.role_key, r.agent_id FROM roles r WHERE r.project_id = :p "
                      "AND r.role_key = :k AND NOT EXISTS (SELECT 1 FROM project_members m "
                      "WHERE m.project_id = :p AND m.member_id = :k "
                      "AND m.member_type = 'human')"), {"p": project_id, "k": key},
            ).mappings().first()
    except Exception:  # noqa: BLE001 — attribution only: never stop the build over it
        return None
    if row is None:
        return None
    return {"role_key": str(row["role_key"]),
            **({"agent_id": str(row["agent_id"])} if row["agent_id"] else {})}


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
                "extends_run_id": {
                    "type": "string",
                    "description": "a build (crun_...) of an EXISTING app to build this onto. "
                                   "The task starts from that app's branch and code, in its "
                                   "repository, and is recorded as the app's next build; its "
                                   "existing tests must still pass. `repository` and "
                                   "`base_branch` are ignored — the app decides both.",
                },
                "release_base_sha": {
                    "type": "string",
                    "description": "with extends_run_id: this change targets a release (an "
                                   "incident's fix). It is built on a branch of its own cut "
                                   "from this serving commit, never on the app's shared "
                                   "branch, and its PR shows only its own diff.",
                },
                "preview_id": {
                    "type": "string",
                    "description": "with extends_run_id: the running preview (prev_...) the "
                                   "operator was looking at when they asked for this. The "
                                   "builder is told which one.",
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
        reversibility="costs_money", permission="code_task:create")
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
        # WHO STARTED IT: the workflow step or chat turn calling this tool, and the person
        # accountable (agent_acts.origin). Its stages are signed under that work.
        started_by = services_module("agent_acts").current_origin(actor)
        if started_by:
            finalize_ctx["origin"] = started_by
        extends = str(args.get("extends_run_id") or "").strip()
        # AN APP BUILT FROM SEVERAL REPOSITORIES gets one change set — a lane per repository, in
        # dependency order, linked PRs, one approval — instead of a single build in one repo.
        multi = _multirepo_change(ctx, pid, actor, args, extends, finalize_ctx)
        if multi is not None:
            return multi
        if extends:
            return _build_onto_existing_app(ctx, cts, pid, actor, args, extends, finalize_ctx)
        release_base = str(args.get("release_base_sha") or "").strip()
        if release_base:
            # A RELEASE FIX WITH NO BUILD LINEAGE is still cut from the serving commit, on a
            # branch of its own (release_scope) — never from the default branch's head.
            rs = services_module("release_scope")
            try:
                rb = rs.release_branch(str(finalize_ctx.get("workflow_run_id")
                                           or services_module("core").make_id("rel")),
                                       release_base)
            except rs.ReleaseBranchError as exc:
                return {"ok": False, "error": str(exc)}
            finalize_ctx = {**finalize_ctx, rs.FCTX_KEY: rb}
            base_branch = rb["branch"]
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
                    finalize_ctx = {"root_run_id": root, "web_app_build": True,
                                    **({"origin": finalize_ctx["origin"]}
                                       if finalize_ctx.get("origin") else {})}
                    risk_tier = 0 if risk_tier is None else risk_tier
                    instructions = (
                        "EXTEND the existing app on this branch — do NOT rebuild from scratch. "
                        "Make the smallest change that satisfies the request, preserving all "
                        "current features.\n\n" + instructions
                    )
            # WHO BUILDS IT. A workflow step that names an agent runs this tool AS that agent
            # (step_agents.resolve → `actor`), so a build step naming the Frontend Engineer
            # hands the code task to the Frontend Engineer, recorded where the run's stages
            # read their context. Only an agent this project has — a person or the workflow
            # runner starting a task records nothing — and never from `args`, which a model
            # writes: who a task is for is decided by who was named, not by what was claimed.
            agent = _building_agent(c, pid, actor)
            if agent:
                finalize_ctx = {**finalize_ctx, "agent": agent}
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
        reversibility="costs_money", permission="code_task:retry")
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
        "To ADD to an app that completed successfully, use extend_code_task instead. If the "
        "run is a STEP of a workflow run (get_code_task says part_of_workflow_run), retry the "
        "step with retry_workflow_step instead, or the run never follows the new attempt.",
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
        reversibility="costs_money", permission="code_task:retry")
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
        reversibility="costs_money", permission="discovery:create")
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
            actor=actor,
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
                               "description": "optional label for a class of work other than "
                                              "the default Build path. project.new / any are "
                                              "refused here — apply those in the Workflows "
                                              "area so a person chooses what Build follows"},
                "steps": {
                    "type": "array",
                    "description": "in order. Each is one of: "
                                   "{id, capability, captain?, approval?} a built-in step; "
                                   "{id, skill: <skill or custom step key>, captain?} runs one "
                                   "skill; {id, mcp_tool: {server, tool}, captain?} runs one MCP "
                                   "tool; {id, agent: true, instructions?, captain?, skills?: "
                                   "[...], mcp?: [server names], produces?: [...]} lets an "
                                   "agent work it out (instructions default to the run's "
                                   "'instructions' input; leave captain out and BOSS picks the "
                                   "agent holding the skills); {id, gate: {question, guidance}} "
                                   "stops for a person; {id, wait_seconds} pauses. `captain` "
                                   "is one of the project's agents. `approval` is 'manual' or "
                                   "'auto' — a capability at or above the risk floor is forced "
                                   "manual whatever you ask for. You do NOT wire inputs: a "
                                   "step's required input is connected to the nearest earlier "
                                   "step that produces it.",
                    "items": {"type": "object"},
                },
                "inputs": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "What the OPERATOR supplies when starting a run, e.g. "
                                   "['media_artifact_id', 'title'] (describe each in "
                                   "input_details). Needed only for inputs no "
                                   "step produces — typically the first step's. Anything "
                                   "neither produced upstream nor listed here is refused, "
                                   "because a workflow that publishes and then dies on a "
                                   "missing value is worse than one that will not publish.",
                },
                "input_details": {
                    "type": "object",
                    "description": "Optional, per input name: {title, description, required} "
                                   "— the label and hint the start form shows. required "
                                   "defaults to true.",
                },
                "project_id": {"type": "string"},
            },
            ["workflow_key", "name", "steps"],
        ),
        kind="write",
        reversibility="safe", permission="workflow:publish")
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
                made = _ensure_steps_for_mcp_tools(c, pid, spec, actor)
                made += _ensure_steps_for_skills(c, pid, spec, actor)
                problems = _resolve_subworkflows(c, pid, spec)
                # One catalogue build for the whole draft, not one per step (round-2 review).
                with services_module("skill_catalogue").one_pass(c):
                    problems += _draft_problems(c, pid, spec, caps_by_key, caps)
                    # AND EVERYTHING PUBLISH WILL REFUSE, from the collector publish raises
                    # from — so a draft that passes here publishes, and the definition below is
                    # never created for a spec that then fails (a gate with no question, say).
                    _, shape, blocked = wf.spec_problems(c, pid, spec,
                                                         namespace=wf.PRODUCT_NAMESPACE)
                    problems += [p["detail"] for p in shape + blocked
                                 if p["detail"] not in problems]
                if problems:
                    return {"ok": False, "error": "; ".join(problems[:4])}
                existing = c.execute(
                    _text("SELECT id, COALESCE(is_catalogue, false) AS is_catalogue "
                          "FROM workflow_definitions "
                          "WHERE project_id=:p AND workflow_key=:k LIMIT 1"),
                    {"p": pid, "k": key},
                ).mappings().first()
                if existing:
                    # THE TWELVE BOSS SHIPS ARE READ-ONLY. The visual builder already
                    # refuses this (`require_editable`). start_workflow and the unmapped
                    # default both take the latest version of this key, so publishing
                    # here would silently replace the gated path every new Build follows.
                    if existing["is_catalogue"]:
                        return {"ok": False,
                                "error": f"{key} is one of BOSS's catalogue workflows. "
                                         "Fork it and edit the fork; editing it would "
                                         "change the path every new Build follows."}
                    definition_id = str(existing["id"])
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
            # BUILD POSTS ONLY work_class=project.new. Mapping that slot — or the
            # catalogue wildcard `any` — from a self-executing tool is how a drafted
            # shortcut becomes the next Build. Apply in the Workflows area is the
            # press that chooses the default path.
            if work_class.lower() in {"project.new", "any", "*"}:
                mapped = {"note": "published, not mapped: project.new/any is the default "
                                  "Build path. Apply this workflow in the Workflows area "
                                  "so a person chooses it."}
            elif work_class:
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
        "Delete a delivery workflow this project authored — or retire it, when it has runs whose "
        "history must stay readable. Refuses while runs are still using it, and refuses a "
        "standard workflow — those are BOSS's own; save one as a copy to change it.",
        _obj({"workflow_key": {"type": "string"}, "project_id": {"type": "string"}},
             ["workflow_key"]),
        kind="write",
        reversibility="destroys_work", permission="workflow:delete")
    def delete_workflow(ctx, *, project_id, actor, args):
        # THE CONSOLE'S DELETE, not a second one. This deleted the definition row outright,
        # which fails on any workflow that has ever run (runs have no cascade) and would have
        # taken the specs finished runs are read from. `delete_definition` deletes a workflow
        # that never ran and RETIRES one that did, and refuses a standard, BOSS's own, and one
        # with a run still going — the same answers the Library gives.
        wf = services_module("workflows")
        pid = args.get("project_id") or project_id
        key = str(args.get("workflow_key") or "").strip().lower()
        with ctx.begin() as c:
            row = c.execute(
                _text("SELECT id FROM workflow_definitions "
                      "WHERE project_id=:p AND workflow_key=:k LIMIT 1"),
                {"p": pid, "k": key},
            ).mappings().first()
            if not row:
                return {"ok": False, "error": f"no workflow {key!r} in this project"}
            try:
                outcome = wf.delete_definition(c, pid, str(row["id"]), actor)
            except wf.WorkflowNamespaceError as error:
                return {"ok": False, "error": str(error)}
            except wf.WorkflowConflictError as error:
                return {"ok": False, "error": str(error)}
        return {"status": outcome["outcome"], "workflow_key": key, "name": outcome["name"],
                "runs": outcome["runs"]}

    @reg.tool(
        "draft_custom_step",
        "Define a reusable workflow step that runs ONE skill, or calls ONE tool on a registered "
        "MCP server. Use it when no standard capability covers the work — publishing to a "
        "third-party account, running a media or document tool. Give `skill` (any skill in "
        "list_skills: authored here or shipped with BOSS) OR `mcp_server` plus `mcp_tool` "
        "(see list_mcp_servers). This step is what makes either placeable in a workflow.",
        _obj(
            {
                "step_key": {"type": "string", "description": "lower_snake_case, stable"},
                "label": {"type": "string"},
                "summary": {"type": "string"},
                "skill": {"type": "string",
                          "description": "the skill id this step runs (list_skills)"},
                "mcp_server": {"type": "string",
                               "description": "instead of a skill: the MCP server's name or id "
                                              "(list_mcp_servers)"},
                "mcp_tool": {"type": "string",
                             "description": "with mcp_server: the tool on it this step calls"},
                "knowledge": {"type": "string",
                              "description": "the standard this step holds to — house style, "
                                             "the format your team uses. Carried into every "
                                             "execution."},
                "inputs": {"type": "array", "items": {"type": "object"}},
                "produces": {"type": "array", "items": {"type": "object"}},
                "project_id": {"type": "string"},
            },
            ["step_key", "label"],
        ),
        kind="write",
        reversibility="safe", permission="workflow:create")
    def draft_custom_step(ctx, *, project_id, actor, args):
        # BOTH BINDINGS, AND EVERY SKILL. This accepted only a skill the project had AUTHORED,
        # so the chat could not place a skill BOSS ships, and could not bind an MCP tool at all
        # — the console builder could, so the conversation was the weaker of the two doors.
        steps = services_module("workflow_custom_steps")
        catalogue = services_module("skill_catalogue")
        registry = services_module("mcp_registry")
        pid = args.get("project_id") or project_id
        skill_key = str(args.get("skill") or "").strip()
        server_ref = str(args.get("mcp_server") or "").strip()
        tool_name = str(args.get("mcp_tool") or "").strip()
        if bool(skill_key) == bool(server_ref):
            return {"ok": False,
                    "error": "give either `skill`, or `mcp_server` with `mcp_tool` — one step "
                             "runs one thing."}
        binding: dict[str, Any]
        if skill_key:
            with ctx.begin() as c:
                # Exact first, then lower-cased: this tool lower-cased every key before it read
                # the catalogue, and a model writing "Summarise_Notes" for summarise_notes
                # should still land. Exact first because a shipped id is matched as written.
                found = (catalogue.entry(c, pid, skill_key)
                         or catalogue.entry(c, pid, skill_key.lower()))
                if found is not None:
                    skill_key = found["skill_id"]
                if found is None:
                    # NAMING WHAT EXISTS beats "not found": the model picked a skill and the
                    # useful correction is the list it should have picked from.
                    available = sorted(catalogue.entries(c, pid))
                    return {"ok": False, "error": f"no skill {skill_key!r} in this project",
                            "available": available[:80]}
            binding = {"binding_kind": "skill", "skill_id": skill_key}
        else:
            if not tool_name:
                return {"ok": False, "error": "say which tool on the server (`mcp_tool`)."}
            with ctx.begin() as c:
                server = registry.find_mcp_server(c, pid, server_ref)
                known = [f"{x['name']} ({x['id']})" for x in registry.list_mcp_servers(c, pid)]
            if server is None:
                return {"ok": False, "error": f"no MCP server {server_ref!r} in this project",
                        "registered": known}
            # Asked now if the last answer is stale, so a tool name is checked against what the
            # server really offers rather than what it offered last week.
            fresh = registry.servers_with_tools(ctx.begin, ctx.settings, pid,
                                                server_id=server["id"])
            server = fresh[0] if fresh else server
            names = sorted(t["name"] for t in server.get("tools") or [] if t.get("name"))
            if server.get("reachable") is True and tool_name not in names:
                return {"ok": False,
                        "error": f"MCP server '{server['name']}' has no tool {tool_name!r}",
                        "tools": names}
            binding = {"binding_kind": "mcp_tool", "mcp_server_id": server["id"],
                       "mcp_tool_name": tool_name}
        with ctx.begin() as c:
            try:
                created = steps.create(
                    c, project_id=pid, actor=actor,
                    step_key=str(args.get("step_key") or "").strip().lower(),
                    label=str(args.get("label") or ""),
                    summary=str(args.get("summary") or ""),
                    inputs=args.get("inputs") or [], produces=args.get("produces") or [],
                    knowledge=str(args.get("knowledge") or ""), **binding)
            except Exception as error:  # noqa: BLE001
                return {"ok": False, "error": f"{type(error).__name__}: {str(error)[:300]}"}
        return {"status": "defined", "step_key": created.get("step_key"),
                **({"skill": skill_key} if skill_key else
                   {"mcp_server": binding["mcp_server_id"], "mcp_tool": tool_name}),
                "usable": created.get("usable"),
                **({"reason": created.get("reason")} if created.get("reason") else {})}

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
                           "description": "behaviour-specific. builtin: {executor_id} naming a "
                                          "skill BOSS ships. mcp_tool: {mcp_server_id, "
                                          "tool_name} — the server by id or name, see "
                                          "list_mcp_servers. sandboxed_command: a "
                                          "digest-pinned image and a command template whose "
                                          "placeholders are all declared inputs."},
                "mode": {"type": "string", "enum": ["create", "update"],
                         "description": "create (default) refuses a key that is taken; update "
                                        "changes the skill that has it."},
                "risk_tier": {"type": "integer",
                              "description": "0-3. Anything that spends money, publishes, or "
                                             "touches an outside account is not tier 0."},
                "project_id": {"type": "string"},
            },
            ["skill_key", "label", "behaviour_kind"],
        ),
        kind="write",
        reversibility="safe", permission="skill:manage")
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
                    # `or 1` turned an honest 0 into 1: tier 0 is falsy. Absent means 1.
                    risk_tier=1 if args.get("risk_tier") in (None, "") else args["risk_tier"],
                    mode=str(args.get("mode") or "create"),
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
        _obj({}), permission="workflow:read")
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
        "Every skill this project can use — the ones it authored and the ones BOSS ships — with "
        "what each needs, its risk tier, whether it forces an approval, and whether it can run "
        "right now (and why not). A skill is what an agent is given and what a custom workflow "
        "step runs.",
        _obj({"project_id": {"type": "string"}}), permission="skill:read")
    def list_skills(ctx, *, project_id, actor, args):
        # EVERYTHING THIS PROJECT CAN RUN, not only what it wrote — and from THE ONE CATALOGUE
        # (skill_catalogue), so the agent's answer is the same one the runtime and the builder
        # give.
        #
        # This once returned `skill_definitions` alone, so every skill BOSS SHIPS was invisible:
        # asked to build a video workflow it reported "no such skill: video.bookend" about a
        # skill sitting in the catalogue. It then merged the spec pack by hand — which, once
        # the spec pack started carrying authored skills too, listed each of those twice.
        catalogue = services_module("skill_catalogue")
        skills = services_module("skill_definitions")
        pid = args.get("project_id") or project_id
        with ctx.begin() as c:
            authored = {s["skill_key"]: s for s in skills.list_skills(c, pid)}
        try:
            with ctx.begin() as c:
                entries = catalogue.entries(c, pid)
        except Exception:  # noqa: BLE001 — a partial answer beats none
            # The authored skills are still returned if the spec pack cannot be read: they are
            # the ones an operator is most likely asking about.
            entries = {key: {"skill_id": key, "label": s["label"], "summary": s["summary"],
                             "source": "authored", "behaviour_kind": s["behaviour_kind"],
                             "kind": "", "risk_tier": s["risk_tier"], "trusted": s["trusted"],
                             "enabled": s["enabled"], "usable": s["enabled"],
                             "runs_through": "", "reason": None}
                       for key, s in authored.items()}
        out: list[dict[str, Any]] = []
        for item in sorted(entries.values(), key=lambda e: (e["source"] != "authored",
                                                            e["skill_id"])):
            row = {
                "skill_key": item["skill_id"],
                "label": item["label"],
                "summary": item["summary"],
                "source": item["source"],
                "behaviour_kind": item["behaviour_kind"] if item["source"] == "authored"
                                  else item["kind"],
                "risk_tier": item["risk_tier"],
                # The same answer the runtime gives: the floor, or a person whatever the tier
                # while the skill's MCP server is unreviewed (requires_person says why).
                "requires_approval": (item["risk_tier"] >= 2
                                      or bool(item.get("requires_person"))),
                **({"requires_person": item["requires_person"]}
                   if item.get("requires_person") else {}),
                "trusted": item["trusted"],
                "enabled": item["enabled"],
                "usable": item["usable"] and item["enabled"],
                **({"runs_through": item["runs_through"]} if item["runs_through"] else {}),
                **({"reason": item["reason"]} if item["reason"] else {}),
                "ships_with_boss": item["source"] != "authored",
            }
            mine = authored.get(item["skill_id"])
            if mine is not None:
                row["inputs"] = mine.get("inputs")
                row["produces"] = mine.get("produces")
            out.append(row)
        return {"skills": out,
                "authored": sum(1 for r in out if not r["ships_with_boss"]),
                "ships_with_boss": sum(1 for r in out if r["ships_with_boss"])}

    @reg.tool(
        "list_mcp_servers",
        "The MCP servers registered in this project: id, name, URL, risk tier, whether a person "
        "has reviewed (trusted) it, whether it is turned on, whether BOSS could reach it at the "
        "last check (and what to do if not), and the tools it offers. Read this before writing "
        "an mcp_tool skill or binding an MCP tool to a workflow step.",
        _obj({"check": {"type": "boolean",
                        "description": "ask each server again now instead of using the last "
                                       "answer (up to a minute old)"},
              "project_id": {"type": "string"}}), permission="mcp_server:read")
    def list_mcp_servers(ctx, *, project_id, actor, args):
        registry = services_module("mcp_registry")
        pid = args.get("project_id") or project_id
        servers = registry.servers_with_tools(
            ctx.begin, ctx.settings, pid,
            max_age_seconds=0 if args.get("check") else registry.REACHABILITY_FRESH_SECONDS)
        return {"servers": [
            {"id": s["id"], "name": s["name"], "url": s["url"], "risk_tier": s["risk_tier"],
             "trusted": s["trusted"], "enabled": s["enabled"], "reachable": s["reachable"],
             "last_checked_at": s["last_checked_at"],
             **({"status": s["status_note"]} if s["status_note"] else {}),
             "tools": [{"name": t["name"], "description": t.get("description", "")[:200],
                        "risk_tier": registry.tool_tier(s, t["name"])}
                       for t in s["tools"]]}
            for s in servers]}

    @reg.tool(
        "register_mcp_server",
        "Register an MCP server (JSON-RPC over HTTP) so its tools can be bound to workflow steps "
        "or wrapped as mcp_tool skills. Ask the operator for the URL — one the BOSS API "
        "container can reach (127.0.0.1 inside the container is the container itself) — and a "
        "name. A server registered here lands SWITCHED OFF and UNREVIEWED, at tier 2 or "
        "above: nothing calls it, and BOSS does not contact it, until a person reviews it and "
        "turns it on on the Skills and MCP page. Tell the operator that.",
        _obj({"name": {"type": "string"},
              "url": {"type": "string", "description": "absolute http(s) URL of the endpoint"},
              "description": {"type": "string"},
              "risk_tier": {"type": "integer", "minimum": 2, "maximum": 3,
                            "description": "the blanket tier for its tools, 2 or 3. Default "
                                           "2: an outside account is not tier 0 or 1 until a "
                                           "person says so."},
              "project_id": {"type": "string"}},
             ["name", "url"]),
        kind="write",
        reversibility="safe", permission="mcp_server:register")
    def register_mcp_server(ctx, *, project_id, actor, args):
        registry = services_module("mcp_registry")
        pid = args.get("project_id") or project_id
        tier = args.get("risk_tier")
        try:
            # NEVER BELOW 2 FROM A CONVERSATION. A tier is a claim about what the server's tools
            # can do, and the model making it has not seen the server; a person lowers it after
            # reviewing it.
            tier = max(2, int(tier)) if tier not in (None, "") else 2
        except (TypeError, ValueError):
            tier = 2
        with ctx.begin() as c:
            try:
                server = registry.register_mcp_server(
                    c, pid, str(args.get("name") or ""), str(args.get("url") or ""),
                    risk_tier=tier,
                    description=str(args.get("description") or "") or None,
                    # NEVER TRUSTED FROM A CONVERSATION. Trust is a person's review of a
                    # server, and is what lets its higher-tier tools be placed at all; a model
                    # that could grant it could talk its way past the floor.
                    provenance={"source": "chat", "trusted": False},
                    # SWITCHED OFF UNTIL A PERSON LOOKS. Round-1 review: registered enabled and
                    # probed at once, a prompt-injected "register http://attacker/mcp" put
                    # attacker-written tool descriptions and callable tools into every chat.
                    # Off, nothing lists its tools, no agent is offered them, and BOSS sends
                    # it nothing — not even the probe — until the operator turns it on.
                    enabled=False,
                    actor=actor)
            except Exception as error:  # noqa: BLE001 — the sentence is the repair
                return {"ok": False, "error": str(error)[:400]}
        return {"status": "registered_switched_off", "id": server["id"],
                "name": server["name"], "enabled": False, "trusted": False,
                "risk_tier": server["risk_tier"],
                "tell_the_operator": (
                    f"I registered '{server['name']}' switched off and unreviewed. Open Skills "
                    "and MCP, check the server is the one you meant, mark it reviewed if you "
                    "vouch for it, and turn it on — BOSS checks it can reach it then. Until a "
                    "person reviews it, every call to one of its tools waits for approval."),
                "next": "once it is on, draft_skill with behaviour_kind mcp_tool wraps one of "
                        "its tools as a skill an agent can be given; draft_custom_step with "
                        "mcp_server and mcp_tool places one in a workflow."}

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
                "inputs": {
                    "type": "object",
                    "description": "Values this workflow's steps read with from_input — a "
                                   "video to fetch, a clip to trim, a channel to publish to. "
                                   "Check the workflow's input_schema for what it declares; "
                                   "a required one that is absent refuses the run rather than "
                                   "failing three steps in. Stable settings — a channel, an "
                                   "icon, a house style — belong in the skill or the agent's "
                                   "knowledge instead, not here: these are the values that "
                                   "differ every run.",
                },
                "start_anyway": {
                    "type": "array", "items": {"type": "string"},
                    "description": "Readiness keys (from a refusal's `missing`) the operator "
                                   "explicitly said to start without, e.g. "
                                   "['deployment_target']. Never fill this in on your own.",
                },
                "allow_unsupported_stack": {
                    "type": "boolean",
                    "description": "Start even though the refusal said code stack_unsupported "
                                   "(the brief asks for a language BOSS cannot build here). "
                                   "Only when the operator said so; never on your own.",
                },
                "project_id": {"type": "string"},
            },
            ["directive", "workflow_key"],
        ),
        kind="write",
        reversibility="costs_money", permission="workflow_run:start")
    def start_workflow(ctx, *, project_id, actor, args):
        from uuid import uuid4

        pid = args.get("project_id") or project_id
        directive = (args.get("directive") or "").strip()
        key = (args.get("workflow_key") or "").strip()
        title = (args.get("title") or directive[:120] or "New work").strip()
        wf = services_module("workflows")
        with ctx.begin() as c:
            # Active version, not the highest number. A revert reactivates an earlier
            # spec without minting a newer one; version DESC would start the one replaced.
            running = wf.running_version(c, pid, key)
            version_id = running["version_id"] if running else None
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
            # WHAT THE STEPS WILL READ. `from_input` resolves any field name, so a custom
            # workflow's steps can bind a video to fetch or a channel to publish to — and
            # until now nothing could pass one. Supplied values go in first so the two BOSS
            # always sets cannot be shadowed by a caller: a run whose "directive" was quietly
            # replaced would be governed under a description of work it is not doing.
            supplied = args.get("inputs")
            run_input = dict(supplied) if isinstance(supplied, dict) else {}
            run_input.update({"directive": directive, "title": title})
            # A MODEL DOES NOT GET TO SAY THE WORK ALREADY HAPPENED. `adopt_*` keys in `inputs`
            # were copied straight into the run, so an agent could make a run build on research
            # that failed — the boss-1 incident through the agent's door (review of 0fd6c45).
            # `start_run` now drops them for every caller; they are named here so the reply
            # says what was ignored rather than dropping it in silence.
            ignored = sorted(k for k in run_input if str(k).startswith(wf.ADOPT_PREFIX))
            adoption: dict = {}
            if adopted:
                # The same research rule the console's start uses: a COMPLETED dossier WITH
                # findings, in this project. A completed-but-empty one was accepted here.
                from ..services import workflow_run_actions as _run_actions
                try:
                    adoption = {"adopt_discovery_run_id": _run_actions.research_to_adopt(
                        c, pid, from_discovery=adopted)}
                except wf.WorkflowNotFoundError:
                    return {"ok": False, "error": f"no research run {adopted!r} in this project"}
                except _run_actions.RunActionRefused as error:
                    return {"ok": False, "error": error.reason}
            # THE SAME PREFLIGHT AS THE CONSOLE'S START. An agent asked to "run the newsletter"
            # on an install with no mail server started it, paid for the research, and failed
            # at the send after a person approved it. It now gets the list of what is missing
            # and the fix, to tell the operator — and starts anyway only for the keys the
            # operator said to start without.
            # CAN BOSS BUILD WHAT IT ASKS FOR (services.stacks) — the same refusal as the
            # console's start, with the supported list and how to turn a toolchain on.
            from ..services import stacks as _stacks

            try:
                _stacks.refuse_unsupported_start(
                    c, version_id=str(version_id), run_input=run_input,
                    allow=bool(args.get(_stacks.OVERRIDE_FIELD)))
            except _stacks.StackUnsupported as refused:
                return {"ok": False, "code": refused.code, "error": str(refused),
                        "supported": _stacks.supported_sentence(),
                        "next": "Tell the operator BOSS cannot build that stack and what it can "
                                "build. Start again with allow_unsupported_stack=true only if "
                                "they say to."}
            from ..services import workflow_readiness as _ready

            accepted = args.get("start_anyway")
            try:
                readiness = _ready.refuse_unless_ready(
                    c, getattr(ctx, "settings", None), pid, version_id=str(version_id),
                    input_data=run_input,
                    start_anyway=[str(k) for k in accepted] if isinstance(accepted, list) else [])
            except _ready.WorkflowNotReadyError as error:
                return {"ok": False, "error": str(error),
                        "missing": [{"key": m["key"], "what": m["label"], "detail": m["detail"],
                                     "fix": m["fix"],
                                     "needed_by": [n["label"] for n in m["needed_by"]]}
                                    for m in error.readiness.get("missing") or []],
                        "next": "Tell the operator what is missing and how to fix it. Start "
                                "again with start_anyway=[keys] only if they say to start "
                                "without them."}
            try:
                run = wf.start_run(
                    c, project_id=pid, actor=actor,
                    input_data=run_input,
                    dedupe_key=f"agent:{key}:{uuid4().hex[:12]}",
                    workflow_version_id=version_id,
                    adopted=adoption,
                )
            except Exception as error:  # noqa: BLE001 — the reason belongs in the reply
                return {"ok": False, "error": f"{type(error).__name__}: {str(error)[:300]}"}
            _ready.record_started_anyway(c, pid, actor, run.get("id") or "", readiness)
        return {"status": "running", "run_id": run.get("id"), "workflow_key": key,
                "project_id": pid, "title": title,
                **({"built_on_research": adopted} if adopted else {}),
                **({"ignored_input": ignored} if ignored else {}),
                **({"started_without": readiness.get("started_anyway")}
                   if readiness.get("started_anyway") else {})}

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
                "seats": {
                    "type": "array", "items": {"type": "string"},
                    "description": "meeting role keys to seat, chair first (a workflow's "
                                   "security review seats security, engineer, architect). "
                                   "Unknown or disabled keys are left out; none left = the "
                                   "project's default panel.",
                },
                "purpose": {
                    "type": "string", "enum": ["prd", "security_review"],
                    "description": "what the panel's document is: a PRD (the default; "
                                   "approving it publishes it) or a security review's "
                                   "recommendations (read by the workflow, never published "
                                   "as a PRD and never auto-built).",
                },
            },
            ["directive"],
        ),
        kind="write",
        reversibility="costs_money", permission="meeting:convene")
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
        # THE MEETING'S NAME IS THE ASK, taken before any framing is wrapped around it. The
        # framing below is for the panel's prompt; when it became the title too, every
        # workflow-convened meeting on boss-1 was called "PRD meeting: --- WHAT THIS PANEL IS
        # FOR ---" and the Meetings screen could not tell them apart.
        title = ms.meeting_subject(directive)
        brief = directive  # the ask in the caller's words, before any framing
        if objective:
            # The markers are meeting_service's, so directive_subject() can take them off
            # again for anything that NAMES the meeting (the task title, the console's cards).
            directive = (
                f"{ms.PANEL_PURPOSE_MARKER}\n"
                f"You are the panel inside BOSS's "
                f"{workflow_name or 'delivery'} workflow. That workflow's objective:\n\n"
                f"{objective}\n\n"
                f"Write for THAT outcome. A PRD that suits a different workflow is the wrong "
                f"document however good it is.\n"
                f"{ms.PANEL_ASK_MARKER}\n\n{directive}"
            )
        research = (args.get("research") or "").strip()
        if research:
            directive = (
                f"{directive}\n\n"
                f"{ms.PANEL_RESEARCH_MARKER}\n"
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
                title=title,
                brief=brief,
                # Who convened it (agent_acts.origin): its seats sit under that work.
                origin=services_module("agent_acts").current_origin(actor),
                role_keys=[str(k) for k in (args.get("seats") or []) if str(k).strip()][:8] or None,
                constraints=({"purpose": "security_review"}
                             if args.get("purpose") == "security_review" else None),
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
        reversibility="safe", permission="preview:manage")
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

    @reg.tool(
        "run_security_scan",
        "Scan one app of this project with the pinned security tools: Semgrep (its code), "
        "OSV-Scanner (its dependencies) and OWASP ZAP (the app started in the scan's own sandbox, "
        "or a running preview of the same app). Queues a worker job and returns its scan_id; "
        "findings land normalised and redacted (GET /v1/security/scans/{id}/findings). Never "
        "scans a URL: the target is `sandbox` or `preview:<id>`.",
        _obj(
            {
                "app_key": {"type": "string", "description": "the app id builds stamp on runs"},
                "ref": {"type": "string",
                        "description": "the app's branch (default) or a commit sha on it"},
                "tools": {"type": "array", "items": {"type": "string",
                                                      "enum": ["semgrep", "osv", "zap"]}},
                "zap_mode": {"type": "string", "enum": ["baseline", "api"],
                             "description": "api = active scan, only against BOSS's own targets"},
                "zap_target": {"type": "string",
                               "description": "`sandbox` (default) or `preview:<id>`"},
                "workflow_run_id": {"type": "string",
                                    "description": "the security_review run this scan belongs to"},
                "project_id": {"type": "string"},
            },
            ["app_key"],
        ),
        kind="write",
        reversibility="safe", permission="security_scan:run")
    def run_security_scan(ctx, *, project_id, actor, args):
        from ..security_scan import scope as scan_scope
        from ..security_scan import service as scan_service

        pid = args.get("project_id") or project_id
        zap = {k: v for k, v in (("mode", args.get("zap_mode")), ("target", args.get("zap_target")))
               if v}
        try:
            with ctx.begin() as c:
                scan = scan_service.request_scan(
                    c, project_id=pid, app_key=str(args.get("app_key") or ""),
                    ref=args.get("ref") or None, tools=args.get("tools") or None,
                    zap=zap or None, triggered_by=actor,
                    workflow_run_id=args.get("workflow_run_id") or None,
                    settings=getattr(ctx, "settings", None))
        except scan_scope.ScopeRefused as exc:
            return {"ok": False, "status": "refused", "code": exc.code, "error": f"{exc.code}: {exc}"}
        return {"status": "queued", "scan_id": scan["id"], "scan": _json_safe(scan)}


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
        reversibility="destroys_work", permission="job:control")
    def cancel_job(ctx, *, project_id, actor, args):
        from ..services.jobs import JobLeaseService
        with ctx.begin() as c:
            return JobLeaseService(ctx.settings).cancel_queued_job(
                c, str(args["job_id"]), reason=str(args["reason"]),
                actor=actor, force=bool(args.get("force")),
                project_id=project_id)

    @reg.tool(
        "write_db",
        "Local admin CLI only: create, update or delete one row with a reason. "
        "This is unscoped SQL and cannot run from chat, MCP, workflows or API sessions. "
        "PREFER a structured tool when one exists: raw SQL emits no domain events or "
        "related updates, so cancel_job is better than UPDATE jobs SET status. "
        "Use create_deployment_target rather than writing deployment_targets by hand. "
        "One statement, always WHERE-qualified unless whole_table is set.",
        _obj({"sql": {"type": "string",
                      "description": "a single INSERT, UPDATE or DELETE statement"},
              "reason": {"type": "string", "description": "why — recorded in the event stream"},
              "whole_table": {"type": "boolean",
                              "description": "confirm an intentionally unqualified UPDATE/DELETE"}},
             required=["sql", "reason"]),
        kind="write",
        reversibility="destroys_work", permission="host:exec")
    def write_db(ctx, *, project_id, actor, args):
        from ..ports import sql as _s
        from ..services.core import emit_event

        if _community_raw_sql_disabled(ctx.settings):
            return {"ok": False, "error": "Unrestricted SQL writes are unavailable in Community. "
                    "Use a governed project-scoped operation."}
        if not _raw_sql_admin_authorized(ctx, actor):
            return {"ok": False, "error": "Unrestricted SQL writes require an explicit local admin CLI call. "
                    "Use a governed project-scoped operation."}
        statement = (args.get("sql") or "").strip().rstrip(";")
        ok, why = _is_single_write(statement, whole_table=bool(args.get("whole_table")))
        if not ok:
            return {"ok": False, "error": f"rejected: {why}"}
        guarded = _writes_access_tables(statement)
        if guarded:
            # Raw SQL is host:exec (an Admin holds it); rewriting a role binding, a seat or a
            # legacy member row is Owner-level and must carry §6.11's limits and an audit event.
            return {"ok": False, "error": (
                f"rejected: {guarded} holds who may do what. Change access through the identity "
                "API (role bindings, groups, seats), which checks grant limits and audits it.")}
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
        reversibility="safe", permission="deployment:create")
    def redeploy(ctx, *, project_id, actor, args):
        from ..services.shepherd_service import _act_on_deployment, redeploy_latest_deployment
        reason = args.get("reason") or "operator asked BOSS to retry the deployment"
        result = redeploy_latest_deployment(ctx.engine, ctx.settings, str(args["run_id"]), actor)
        if result is None:
            # Never deployed: the explanation of what IS there comes from the shared remedy.
            outcome = _act_on_deployment(
                ctx.engine, ctx.settings, str(args["run_id"]), "redeploy_deployment", actor)
            return {"ok": False, "run_id": args["run_id"], "outcome": outcome, "reason": reason}
        # THE NEW DEPLOYMENT'S ID AND STATUS, not only "started". A workflow's redeploy step
        # waits on that deployment run; with only a word to go on it went green while the
        # deployment it had begun failed its health check. `blocked` is a refusal too — it used
        # to read "started".
        status = str(result.get("status") or "")
        refused = status in ("failed", "error", "blocked", "not_found")
        return {"ok": not refused, "run_id": args["run_id"],
                "outcome": f"failed: {status}" if refused else "started",
                "deployment_run_id": result.get("deployment_run_id"),
                "deployment_status": status,
                **({"error": str(result.get("reason") or result.get("error") or status)}
                   if refused else {}),
                "reason": reason}

    @reg.tool(
        "remove_app",
        "Remove a deployed app from its host: stop and remove its compose project or container "
        "and its folder under /opt/boss-apps. Governed like a deploy: a ChangeSet approved by a "
        "person (or the environment's policy), a leased job, a receipt. Its data is KEPT unless "
        "delete_data is true. Offer it as a button with propose_action; never run it unasked. "
        "Takes the app name or its deployment target id (deptgt_...).",
        _obj({"app_or_target": {"type": "string", "description": "app name or deptgt_... id"},
              "delete_data": {"type": "boolean", "default": False,
                              "description": "also delete its database volumes and data"},
              "reason": {"type": "string", "description": "why — recorded on the removal"}},
             required=["app_or_target"]),
        kind="write",
        reversibility="destroys_work", permission="deployment:create")
    def remove_app(ctx, *, project_id, actor, args):
        from sqlalchemy import text

        from ..services.app_removal import RemovalRefused, plan_removal
        from ..services.resource_plane import ResourcePlaneService

        key = str(args.get("app_or_target") or "").strip()
        with ctx.engine.begin() as c:
            row = c.execute(text(
                "SELECT * FROM deployment_targets WHERE project_id = :p AND (id = :k OR "
                "lower(app_name) = lower(:k)) ORDER BY (status = 'active') DESC, updated_at DESC "
                "LIMIT 1"), {"p": project_id, "k": key}).mappings().first()
            if not row:
                return {"ok": False, "error": f"no deployed app or target named {key!r}"}
            try:
                planned = plan_removal(ResourcePlaneService(ctx.settings), c, project_id=project_id,
                                       target=dict(row), actor=actor,
                                       delete_data=bool(args.get("delete_data")),
                                       reason=str(args.get("reason") or ""))
            except RemovalRefused as refused:
                return {"ok": False, "error": str(refused)}
        return {"ok": True, "deployment_run_id": planned.get("id"),
                "status": planned.get("status"), "decision_id": planned.get("decision_id"),
                "target_id": row["id"]}

    @reg.tool(
        "deploy_app",
        "Deploy a SUCCESSFUL build to an environment for the FIRST time. This is the tool to "
        "use when an operator says 'deploy it', 'ship it', or 'deploy to staging/live' and the "
        "app has never been deployed — redeploy only re-ships a deployment that already "
        "exists, so it cannot help there. Takes the crun_ of a completed run and the "
        "environment name. It will REFUSE if the run did not succeed, or if no active target "
        "exists for that environment, and it tells you which environments DO exist so you can "
        "offer the operator a real choice. An app's FIRST deploy creates its target (free port, "
        "URL and health from the host) on host_id, or the project's one verified host.",
        _obj({"run_id": {"type": "string", "description": "crun_ of a COMPLETED build"},
              "environment": {"type": "string",
                              "description": "staging | production"},
              "target_id": {"type": "string", "description": "optional exact deptgt_ to use"},
              "host_id": {"type": "string",
                          "description": "optional dh_: where a first deploy goes when the "
                                         "project has more than one verified host"},
              "approval_decision_id": {"type": "string",
                                       "description": "set by a workflow's Deploy step only; "
                                                      "leave empty"},
              "person_required": {"type": "boolean",
                                  "description": "set by a workflow's Deploy step only; "
                                                 "leave empty"}},
             required=["run_id", "environment"]),
        kind="write",
        reversibility="safe", permission="deployment:create")
    def deploy_app(ctx, *, project_id, actor, args):
        from ..services.delivery_pipeline import deploy_build
        return deploy_build(
            ctx.engine, ctx.settings,
            project_id=project_id,
            run_id=str(args["run_id"]),
            environment=str(args.get("environment") or "staging"),
            target_id=(str(args["target_id"]) if args.get("target_id") else None),
            host_id=(str(args["host_id"]) if args.get("host_id") else None),
            actor=actor,
            # Set by the workflow runner from the step's released gate, never by a model: a
            # named decision counts only when it is that deploy step's approved, unused gate
            # (deploy_policy.pre_approval).
            approval_decision_id=(str(args["approval_decision_id"])
                                  if args.get("approval_decision_id") else None),
            # A Deploy step that promised a person: no environment policy answers for it. Only
            # ever makes a deploy stricter, so a model setting it can only add a question.
            person_required=args.get("person_required") is True,
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
        reversibility="safe", permission="deployment_target:manage")
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
        kind="read", permission="deployment:read")
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
        kind="read", permission="decision:read")
    def propose_action(ctx, *, project_id, actor, args):
        """Records an offer. Executing it is the operator's click, not this call."""
        name = ToolRegistry.canonical_tool_name(str(args.get("tool") or ""))
        if name not in _DISCUSS_OPERATIONS_TOOLS:
            return {"ok": False,
                    "error": f"cannot propose {name!r}: not an action BOSS can run",
                    "available": sorted(_DISCUSS_OPERATIONS_TOOLS)}
        unavailable = action_unavailable(name, getattr(ctx, "settings", None))
        if unavailable:
            return {"ok": False,
                    "error": f"cannot propose {name!r} here: {unavailable} Offer what BOSS "
                             "can run instead (for a deploy: a retry or `redeploy`), and ask the "
                             "operator to act by hand only if nothing in BOSS can."}
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
                              "list_code_tasks or get_code_task — then propose with it. An action "
                              "the operator cannot actually run is worse than no offer."),
                    "proposed": False}
        return {"ok": True, "proposed": True, "tool": name,
                "args": call_args, "label": _offer_button_label(args.get("label"), name, call_args),
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
        reversibility="safe", permission="setup:manage")
    def test_github_access(ctx, *, project_id, actor, args):
        # WHY THIS IS A TOOL. Asked why a connection failed, the agent diagnosed it correctly
        # and then said "open BOSS's GitHub view, select the repository, and click Test
        # access" — an instruction to do by hand the one step it could have done itself.
        # "Just explaining the problem would not help anyone."
        # THE SAME THREE PROBES AS REPO AND MODELS' Test (setup_actions.check_repository):
        # read, write a blob, ask to open a pull request — never the account's role.
        from ..services import setup_actions

        pid = args.get("project_id") or project_id
        with ctx.begin() as c:
            wanted = str(args.get("repository") or "").strip()
            binding = c.execute(
                _text("SELECT id, full_name FROM github_repository_bindings "
                      " WHERE project_id = :p AND status = 'active' "
                      "   AND (:w = '' OR lower(full_name) = lower(:w)) "
                      " ORDER BY (role = 'primary') DESC LIMIT 1"),
                {"p": pid, "w": wanted}).mappings().first()
            target = binding["id"] if binding else None
            if target is None:
                from ..services.setup_view import install_repository

                if all(install_repository(ctx.settings)):
                    target = "install"
            if target is None:
                return {"ok": False,
                        "error": (f"no repository connected on {pid}"
                                  + (f" for {wanted}" if wanted else "")
                                  + " — connect one in Repo and models first."),
                        "href": "/?area=setup&focus=repository"}
            try:
                found = setup_actions.check_repository(c, ctx.settings, pid, target, actor=actor)
            except Exception as exc:  # noqa: BLE001 — the reason is the answer
                return {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:400]}
        from ..services.setup_pills import pill

        shown = pill("repository", found["state"], repo=(binding or {}).get("full_name") or "")
        return {"ok": found["state"] == "working", "state": shown["label"],
                "fix": shown["tip"], "checks": {k: v.get("ok") for k, v in
                                                (found.get("checks") or {}).items()},
                "repository": (binding or {}).get("full_name"),
                "href": "/?area=setup&focus=repository"}

    @reg.tool(
        "set_project_setting",
        "Change one of this project's settings — which model implements, reviews, handles "
        "hard or routine work, or answers in chat; how many agents run at once; the turn and "
        "review-round budgets. Takes effect on the next run, no restart. Use it to APPLY a "
        "change you have recommended once the operator agrees, instead of describing where "
        "they could click. Send an empty value to hand a setting back to the deployment "
        "default. Refuses a key or a value that is not allowed, and says what is. "
        # GENERATED FROM THE SETTINGS TABLE, so the agent reads the real names and values
        # instead of guessing one (`knowledge` for `knowledge_graph`).
        "Keys and allowed values: " + _project_settings_catalogue() + ".",
        _obj({"key": {"type": "string", "description": "the setting key, exactly as listed"},
              "value": {"type": "string",
                        "description": "the new value; empty to use the deployment default"}},
             required=["key"]),
        kind="write",
        # UNDONE BY SETTING IT BACK, or by sending an empty value to return to the deployment
        # default. It starts nothing and spends nothing; the next run reads the new value.
        reversibility="safe", permission="settings:update")
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
        "create_project",
        "Create a new BOSS PROJECT in this organization: a separate workspace with its own "
        "agents, skills, budget, settings, repositories and work, set up exactly like the first "
        "project. Use it when someone asks for a new project (\"create a project called "
        "northpeak-bank-a\"). NOT for a new app — an app is built with start_workflow or "
        "create_code_task inside a project. The slug is kebab-case and becomes the project's "
        "id; give it when they named one, else it is made from the name. A slug already in use "
        "comes back with the existing project's id. Never call it to check whether a project "
        "exists: list_projects and describe_project say that.",
        _obj({"name": {"type": "string", "description": "the project's name, as they said it"},
              "slug": {"type": "string",
                       "description": "kebab-case id, e.g. northpeak-bank-a (optional)"},
              "description": {"type": "string", "description": "what it is for (optional)"}},
             required=["name"]),
        kind="write",
        # A NEW, EMPTY PROJECT: it spends nothing and changes no existing work. The identity gate
        # decides project:create with the caller's own context (a person's fresh step-up; a
        # service account's key); an agent's run token never holds it (not delegable).
        reversibility="safe", permission="project:create", org_scope=True)
    def create_project(ctx, *, project_id, actor, args):
        from ..services import projects as project_service
        from .registry import CURRENT_TOOL_AUTH

        auth = CURRENT_TOOL_AUTH.get()
        try:
            with ctx.begin() as c:
                made = project_service.create(
                    c, ctx.settings, auth=auth, name=args.get("name"), slug=args.get("slug"),
                    description=args.get("description"), actor=actor)
        except project_service.ProjectExists as clash:
            return {"ok": False, "error": "project_exists", "slug": clash.slug,
                    "project_id": clash.project_id,
                    "message": (f"A project {clash.project_id} already exists." if clash.project_id
                                else f"The slug {clash.slug} is taken; choose another.")}
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "project_id": made["project_id"], "name": made["name"],
                "slug": made["slug"], "href": f"/?project_id={made['project_id']}",
                "note": f"Project {made['name']} ({made['project_id']}) is ready, set up like "
                        "the first project."}

    @reg.tool(
        "diagnose_setup",
        "EVERYTHING about whether this install is set up, in one call — the same view Repo "
        "and models renders: the repository and its read/write/pull-request checks, each "
        "model and its last check, which model does each job (Build, Review, Chat, "
        "Decisions), the budget, and what to fix first with a console link. Use this FIRST "
        "for any question about setup, connecting a repository or a model, a token being "
        "rejected, or 'the check failed'. Read-only; never calls GitHub or a provider and "
        "never returns a credential.",
        _obj({}),
        kind="read", permission="setup:read")
    def diagnose_setup(ctx, *, project_id, actor, args):
        # ONE SOURCE WITH THE PAGE. This assembled its own picture from setup_status, raw
        # github_connections rows and CLI detection, so the chat and Repo and models could
        # disagree about the same install. It now returns `setup_view.build` — the page's own
        # state — plus the last check of each row and the fixes in order.
        pid = args.get("project_id") or project_id
        from ..services import setup_view

        with ctx.begin() as c:
            view = setup_view.build(c, ctx.settings, pid)
        next_steps: list[dict] = []
        for repo in view["repository"]["repos"]:
            if repo["pill"]["tone"] in ("red", "amber"):
                next_steps.append({"row": "repository", "what": repo["full_name"],
                                   "state": repo["pill"]["label"], "fix": repo["pill"]["tip"],
                                   "href": "/?area=setup&focus=repository"})
        if not view["repository"]["repos"]:
            next_steps.append({"row": "repository", "state": "Not connected",
                               "fix": "Paste a GitHub token in Repo and models.",
                               "href": "/?area=setup&focus=repository"})
        for model in view["models"]:
            if model["pill"]["tone"] in ("red", "amber"):
                next_steps.append({"row": "models", "what": model["label"],
                                   "state": model["pill"]["label"], "fix": model["pill"]["tip"],
                                   "href": f"/?area=setup&focus=models&source={model['source_id']}"})
        if not view["models"]:
            next_steps.append({"row": "models", "state": "Not connected",
                               "fix": "Connect a subscription, an API key or a local model.",
                               "href": "/?area=setup&focus=models"})
        return {"project_id": pid, "view": view,
                "verdict": view["verdict"],
                "who_does_what": {j["job"]: (j["resolved"] or {}).get("label") or "nothing"
                                  for j in view["jobs"]},
                "next_steps": next_steps or [{"row": None, "state": "Ready",
                                              "fix": "Nothing outstanding."}]}

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
        kind="read", permission="system:read")
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
        reversibility="safe", permission="job:control")
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
                          "description": "how long queued counts as starved (default 10)"}}), permission="job:read")
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
        reversibility="safe", permission="code_task:steer")
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
        _obj({}), permission="system:read")
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
        reversibility="safe", permission="code_task:cancel")
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
                from ..services.run_terminal import on_status_written

                for r in found:
                    on_status_written(c, r["id"])
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
        }), permission="decision:read")
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
        reversibility="irreversible", permission="decision:approve",
        # the same operation, the same permission as over HTTP (/v1/decisions/{id}/{action})
        permission_by_arg=("action", (("delegate", "decision:delegate"),
                                      ("execute", "decision:execute"))))
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
        from ..identity.approvals import ApprovalPending, ApprovalRefused

        try:
            with ctx.begin() as c:
                if _decision_in_project(c, decision_id, project_id) is None:
                    return {"ok": False, "error": f"no decision {decision_id}"}
                try:
                    row = svc.transition(
                        c, decision_id, action=action, actor=who, note=note,
                        delegate_to=args.get("delegate_to"),
                    )
                except ApprovalPending as pending:     # recorded: this transaction commits it
                    return {"ok": True, "action": action, "actor": who, **pending.body()}
        except ApprovalRefused as refused:
            return {"ok": False, "status": refused.code, **refused.body()}
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
        reversibility="destroys_work", permission="code_task:cancel")
    def cancel_code_task(ctx, *, project_id, actor, args):
        run_id = args["run_id"]
        reason = args.get("reason") or "cancelled via MCP"
        cts = services_module("code_task_service")
        # One path with the console's Stop (code_task_service.cancel_run): the run, its job,
        # its open acts and who stopped it, together.
        try:
            with ctx.begin() as c:
                return cts.cancel_run(c, run_id, project_id, actor=actor, reason=reason)
        except LookupError:
            return {"ok": False, "error": f"no code task run {run_id}"}
        except cts.CancelRefused as exc:
            return {"ok": False, "error": str(exc)}

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
        reversibility="destroys_work", permission="code_task:override")
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
        reversibility="safe", permission="code_task:steer")
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
        reversibility="safe", permission="preview:manage")
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
        reversibility="safe", permission="code_task:steer")
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
        _obj({}, []), permission="setup:read")
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
        "ChatGPT login) into BOSS. Community can record an eligible CLI with prove=false; "
        "live proof is paused until model turns have budget, lease, and receipt tracking.",
        _obj({"provider": {"type": "string",
                           "description": "codex | cursor | aider"},
              "prove": {"type": "boolean", "default": True,
                        "description": "run one real turn before recording it (recommended)"}},
             ["provider"]),
        kind="write",
        reversibility="safe", permission="model_provider:manage")
    def connect_coding_agent(ctx, *, project_id, actor, args):
        from ..services.setup_service import SetupError, connect_coding_agent as _connect

        # Proved by default; in Community the one test turn is reserved and receipted
        # (setup_service.governed_proof).
        prove = args.get("prove") is not False

        with ctx.begin() as c:
            try:
                return _connect(c, ctx.settings, project_id,
                                str(args.get("provider") or "").strip().lower(),
                                actor=actor, prove=prove)
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
             []), permission="model_provider:read")
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
        _obj({}, []), permission="setup:read")
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
        reversibility="safe", permission="code_task:steer")
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
             ["run_id"]), permission="code_task:read")
    def list_review_comments(ctx, *, project_id, actor, args):
        cts = services_module("code_task_service")
        want = (args.get("status") or "all").lower()
        with ctx.begin() as c:
            row = _code_task_in_project(c, run_id := args["run_id"], project_id)
            if not row:
                return {"ok": False, "error": f"no code task run {run_id}"}
            # The whole run: the fence row carries no finalize_ctx or state, so this list was
            # always empty. A reviewer's finding and an operator's comment are one list.
            run = cts.load_code_task_run(c, run_id) or {}
        stored = cts.review_comments_of(run)
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
        reversibility="safe", permission="code_task:steer")
    def resolve_review_comment(ctx, *, project_id, actor, args):
        cts = services_module("code_task_service")
        run_id = args["run_id"]
        try:
            with ctx.begin() as c:
                return cts.resolve_review_comment(
                    c, run_id, project_id, args["comment_id"],
                    status=args.get("status") or "", actor=actor, note=args.get("note") or "")
        except (LookupError, ValueError) as exc:
            return {"ok": False, "error": str(exc).strip("'\"")}

    @reg.tool(
        "review_pull_request_again",
        "Run the adversarial review again on a build's open pull request — on the PR's REAL "
        "diff from GitHub, not the build's working tree — and post it on the PR as a new review "
        "round (findings at their lines, the description regenerated from BOSS's records). Use "
        "when a PR's review was hollow ('nothing to review') or a person wants it looked at "
        "again. Queued: returns at once; the build page shows the result.",
        _obj({"run_id": {"type": "string", "description": "the build (code task run)"},
              "pull_request_number": {"type": "integer",
                                      "description": "or the PR number, if the run id is unknown"}},
             []),
        kind="write",
        # A model review per call: reversible in state (a new round), not in spend.
        reversibility="costs_money", permission="code_task:steer")
    def review_pull_request_again(ctx, *, project_id, actor, args):
        pra = services_module("pr_review_again")
        if not args.get("run_id") and not args.get("pull_request_number"):
            return {"ok": False, "error": "give run_id or pull_request_number"}
        try:
            with ctx.begin() as c:
                out = pra.request(c, project_id=project_id, actor=actor,
                                  run_id=args.get("run_id") or None,
                                  pull_request_number=args.get("pull_request_number") or None)
        except pra.ReviewAgainRefused as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, **out}

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
             ["run_id"]), permission="code_task:read")
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
        _obj({"run_id": {"type": "string"}, "limit": _LIMIT}, ["run_id"]), permission="code_task:read")
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
                      "description": "app/run title instead of an id, e.g. 'UnitFlip'"}}), permission="workflow:read")
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
        _obj({"run_id": {"type": "string"}}, ["run_id"]), permission="code_task:read")
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
              "limit": _LIMIT, "project_id": {"type": "string"}}), permission="budget:read")
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
        _obj({"project_id": {"type": "string"}, "limit": _LIMIT}), permission="repository:read")
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
              "project_id": {"type": "string"}}), permission="github:read")
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

    # THE ORG'S REPOSITORY REGISTRY, AS TOOLS. Asked to set up three projects, the agent said
    # their repositories were "blocked, and not something I can fix from here": its only add
    # tool demanded a `connection_id` and list_github_connections (per project) showed none —
    # while the org's one GitHub App connection reached all three repositories. The console's
    # path is the registry (Settings › Repo and models › Repositories, then the project's
    # Repositories): these tools call the same services as those routes, so there is one way to
    # add a repository and the agent can take it.
    @reg.tool(
        "list_org_repositories",
        "The ORGANIZATION's repository registry: every repository the org's ONE GitHub "
        "connection (its GitHub App installation, else the org token) reaches. A project needs "
        "no GitHub connection of its own. Each row: id (ghr_…), full_name, allowed (whether BOSS "
        "may build in it), state (working | read_only | needs_fix | checking | not_allowed | "
        "archived | unreachable), default_branch and used_by (the projects it is in); plus "
        "`connection`, the org's App or token. To give a project a repository: allow_repository "
        "if it is not allowed yet, then register_repository with its full_name. refresh=true "
        "re-reads what GitHub lists first (a repository just added on GitHub).",
        _obj({"refresh": {"type": "boolean", "default": False,
                          "description": "re-read GitHub's list before answering"}}),
        permission="repository:read",
        # Refreshing writes the registry (the console's Refresh), so it takes the route's key.
        permission_by_arg=("refresh", (("True", "repository:register"),
                                       ("true", "repository:register"))))
    def list_org_repositories(ctx, *, project_id, actor, args):
        from ..multirepo import registry
        from ..multirepo.common import MultiRepoError

        try:
            with ctx.begin() as c:
                refreshed = None
                if args.get("refresh") in (True, "true", "True"):
                    refreshed = registry.refresh(c, ctx.settings, project_id, actor=actor)
                out = registry.list_registry(c, project_id)
        except MultiRepoError as exc:
            return {"ok": False, **exc.body()}
        return {"ok": True, "project_id": project_id, **_json_safe(out),
                **({"refreshed": _json_safe(refreshed)} if refreshed is not None else {}),
                "note": ("One connection serves every project in the organization. "
                         "allow_repository ticks a row; register_repository adds an allowed "
                         "row to a project (pass `project` for another project).")}

    @reg.tool(
        "allow_repository",
        "Allow (or, allowed=false, disallow) one repository of the org's registry for BOSS — "
        "the console's tick in Settings › Repo and models › Repositories. Allowing runs the "
        "read / write / pull-request checks; disallowing takes it out of EVERY project of the "
        "org. Org-wide: the GitHub connection is the org's, not a project's. `repository` is "
        "owner/name or the registry id from list_org_repositories.",
        _obj({"repository": {"type": "string", "description": "owner/name or ghr_… id"},
              "allowed": {"type": "boolean", "default": True}},
             ["repository"]),
        kind="write",
        # It gives BOSS's agents reach on a GitHub repository through the org's App: a person
        # presses it, as for any grant.
        reversibility="grants_authority", permission="repository:register")
    def allow_repository(ctx, *, project_id, actor, args):
        from ..multirepo import project_repos, registry
        from ..multirepo.common import MultiRepoError
        from .registry import CURRENT_TOOL_AUTH

        allowed = args.get("allowed")
        allowed = True if allowed is None else allowed in (True, "true", "True")
        try:
            with ctx.begin() as c:
                rid = project_repos.registry_id_for(c, project_id, str(args.get("repository") or ""))
                row = registry.allow(c, ctx.settings, project_id, rid, allowed, actor=actor,
                                     auth=CURRENT_TOOL_AUTH.get())
        except MultiRepoError as exc:
            return {"ok": False, **exc.body()}
        return {"ok": True, "repository": _json_safe(row)}

    @reg.tool(
        "register_repository",
        "Add a repository to a project so BOSS can build in it — the console's project "
        "Repositories › Add. `repository` is owner/name or the registry id from "
        "list_org_repositories; it must be ALLOWED there (allow_repository). No connection is "
        "needed: the org's GitHub App (else the project's token) is used. Pass `project` to add "
        "it to another project. Already in the project: updates its base branch / role. "
        "connection_id is only for binding under one specific project connection "
        "(list_github_connections), and never takes a token.",
        _obj(
            {
                "repository": {"type": "string", "description": "owner/name or ghr_… id"},
                "base_branch": {"type": "string",
                                "description": "branch BOSS builds on; the repository's default "
                                               "when omitted"},
                "role_hint": {"type": "string",
                              "enum": ["contracts", "backend", "frontend", "deploy_config",
                                       "library", "service", "other"]},
                "connection_id": {"type": "string",
                                  "description": "optional; only to bind under that connection"},
                "role": {"type": "string", "enum": ["primary", "source", "upstream", "mirror"],
                         "description": "only with connection_id; primary = the default target"},
                "project_id": {"type": "string"},
            },
            ["repository"],
        ),
        kind="write",
        reversibility="safe", permission="repository:register",
        # The registry path is the project Repositories route (PUT /v1/projects/{id}/repos/{repo}),
        # which needs repository:manage; binding under a named connection keeps repository:register.
        permission_by_arg=("connection_id", (("", "repository:manage"),)))
    def register_repository(ctx, *, project_id, actor, args):
        pid = args.get("project_id") or project_id
        base_branch = args.get("base_branch") or args.get("default_branch")
        if args.get("connection_id"):
            gc = services_module("github_connections")
            svc = gc.GitHubConnectionService(ctx.settings)
            try:
                with ctx.begin() as c:
                    row = svc.create_repository_binding(
                        c, project_id=pid, connection_id=args["connection_id"],
                        repository=args["repository"], role=args.get("role") or "secondary",
                        default_branch=base_branch, metadata={}, actor=actor,
                    )
            except Exception as exc:
                return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            return {"ok": True, "binding": _json_safe(dict(row))}
        from ..multirepo import project_repos
        from ..multirepo.common import MultiRepoError

        try:
            with ctx.begin() as c:
                out = project_repos.put(
                    c, ctx.settings, pid, str(args.get("repository") or ""),
                    role_hint=args["role_hint"] if args.get("role_hint") else ...,
                    base_branch=base_branch, actor=actor)
        except MultiRepoError as exc:
            return {"ok": False, **exc.body(),
                    **({"do_this": "allow_repository first (a person presses it), then add it."}
                       if exc.code == "not_allowed" else {})}
        return {"ok": True, "project_id": pid, "repositories": _json_safe(out["items"]),
                "repo_mode": out.get("repo_mode")}

    @reg.tool(
        "remove_project_repository",
        "Take a repository out of a project (the console's project Repositories › Remove). It "
        "stays in the org's registry and in other projects. Refused while an app's component "
        "uses it. `repository` is owner/name, the registry id or the binding id.",
        _obj({"repository": {"type": "string"}}, ["repository"]),
        kind="write",
        # Undone by adding it again (register_repository); nothing built is lost.
        reversibility="safe", permission="repository:manage")
    def remove_project_repository(ctx, *, project_id, actor, args):
        from ..multirepo import project_repos
        from ..multirepo.common import MultiRepoError

        try:
            with ctx.begin() as c:
                out = project_repos.delete(c, project_id, str(args.get("repository") or ""),
                                           actor=actor)
        except MultiRepoError as exc:
            return {"ok": False, **exc.body()}
        return {"ok": True, "project_id": project_id, "repositories": _json_safe(out["items"])}

    @reg.tool(
        "list_github_connections",
        "A project's OWN GitHub connections (tokens or App bindings made for it), with status "
        "and any missing permissions — plus `org_connection`, the organization's GitHub App or "
        "token that every project uses. Zero project connections does NOT mean GitHub is "
        "unconnected: repositories come from the org registry (list_org_repositories). Secrets "
        "are never returned — only the reference and its health.",
        _obj({"project_id": {"type": "string"}, "limit": _LIMIT}), permission="github:read")
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
            org_connection = _org_github_connection(c, pid)
        return {"project_id": pid, "count": len(rows),
                "connections": [_json_safe(dict(r)) for r in rows],
                "org_connection": org_connection}

    @reg.tool(
        "get_meeting",
        "Full detail for one PRD meeting run (mrun_…): status, phase, directive, the "
        "panel roles and the linked decision. Read this BEFORE approving its PRD.",
        _obj({"meeting_id": {"type": "string"}}, ["meeting_id"]), permission="meeting:read")
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
        from ..services.meeting_service import meeting_title

        meeting = _json_safe(dict(row))
        meeting["title"] = meeting_title(row.get("title"), row.get("directive"))
        return {"meeting": meeting,
                "decision": _json_safe(dict(dec)) if dec else None}


def _declared_inputs(raw: Any) -> list[str]:
    """The input names a workflow version declares, whichever shape it wrote them in.

    TWO SHAPES EXIST IN ONE DATABASE and this read only understood the rarer one:

      * ``{"media_artifact_id": "string", "title": "string"}`` — a name-to-type map, which is
        what `draft_workflow` emits.
      * ``{"required": ["project_id", "task_ref"], "properties": {...}}`` — JSON Schema, which
        is what 21 of 45 real versions in one database carry.

    Reading `.keys()` on the second yields ``["required"]``, so every failed run of those
    workflows reported `missing_inputs: ["required"]` and told the operator to supply an input
    called "required". Confidently wrong — and because the list was truthy it displaced the
    step-level diagnosis that would have named the actual cause.
    """
    if not isinstance(raw, dict) or not raw:
        return []
    if "required" in raw or "properties" in raw:
        required = raw.get("required")
        if isinstance(required, list):
            return [str(x) for x in required]
        properties = raw.get("properties")
        return [str(k) for k in properties] if isinstance(properties, dict) else []
    return [str(k) for k in raw]


def _draft_problems(connection, project_id: str, spec: dict, caps_by_key: dict,
                    caps_module: Any) -> list[str]:
    """Everything wrong with a drafted spec, in sentences, before anything is written.

    `publish_version` validates too — this is not a second opinion but an EARLIER one, so the
    agent can say "that step needs an input nothing produces" instead of creating a definition
    and then failing to publish into it.
    """
    problems: list[str] = []
    catalogue = services_module("skill_catalogue")
    custom_steps = services_module("workflow_custom_steps")
    for entry in spec.get("steps", []):
        binding = entry.get("binding") or {}
        if entry.get("kind") != "capability" or binding.get("agent_work") \
                or binding.get("typed_decision"):
            # A gate, a wait or an agent step binds no capability. What can be wrong with one —
            # who answers it, what an agent is scoped to — is the agent check below.
            continue
        if binding.get("custom_step"):
            step_key = str(binding.get("custom_step") or "")
            if step_key.startswith("mcp:"):
                _, server, tool = (step_key.split(":", 2) + ["", ""])[:3]
                problems.append(
                    f"step '{entry.get('id')}' calls {tool} on MCP server '{server}', which this "
                    "project has not registered. Register the server under Skills and MCP "
                    "first, then draft the workflow again.")
                continue
            defined = custom_steps.get(connection, project_id, step_key)
            if defined and defined.get("binding_kind") == "mcp_tool":
                # AN MCP STEP HAS NO SKILL. Resolving its key as one refused every MCP-bound
                # step as "attaches skill <key>, which this project does not have". The
                # custom-step check is the one that knows what an MCP step needs.
                problems.extend(custom_steps.validate_step(connection, project_id, entry))
                continue
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
            if row is not None and row["binding_kind"] == "mcp_tool":
                # A step that calls an MCP tool has no skill behind it; publish_version checks
                # its server and tool through resolve_risk.
                continue
            skill_key = str((row or {}).get("skill_id") or step_key)
            # THE ONE CATALOGUE (skill_catalogue), for authored and shipped skills alike. This
            # read `get_skill` (authored only) and, for a shipped skill, made up a stand-in at
            # tier 1 — so a tier-3 publish validated as tier 1 here and was refused only later,
            # at publish, and an authored skill's review was never seen (it was read from a
            # `provenance` key the authored row does not have).
            found = catalogue.entry(connection, project_id, skill_key)
            skill = None if found is None else {
                "skill_key": skill_key, "risk_tier": found["risk_tier"],
                "enabled": found["enabled"] and found["usable"],
                "provenance": {"trusted": found["trusted"]},
                "requires_person": found.get("requires_person"),
            }
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
    # WHO RUNS EACH STEP — the same check publish makes, made before anything is written, so
    # the agent can say "nobody holds that skill" instead of creating a workflow that cannot
    # publish. Names checked against the PROJECT's agents.
    problems.extend(services_module("step_agents").assignment_problems(
        connection, project_id, spec))
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


def _workflow_step_of(c, project_id: str, run_id: str) -> "_Optional[dict]":
    """The workflow run step this build is, if it is one — so a retry goes to THE STEP.

    Asked to "rerun the build step on the Order API run", the agent found the build
    (crun_…) and offered retry_code_task on it: a new attempt outside the run, which the run
    never follows, while the step stayed failed (battle test, retry-existing-step)."""
    try:
        with c.begin_nested():
            row = c.execute(_text(
                "SELECT workflow_run_id, step_id, status FROM workflow_step_runs "
                "WHERE project_id = :p AND (output->>'build_run_id' = :r "
                "OR output->>'run_id' = :r) ORDER BY updated_at DESC LIMIT 1"),
                {"p": project_id, "r": run_id}).mappings().first()
    except Exception:  # noqa: BLE001 — the link is extra; the run is the answer
        return None
    if not row:
        return None
    return {"workflow_run_id": row["workflow_run_id"], "step_id": row["step_id"],
            "step_status": row["status"],
            "retry_with": (f"retry_workflow_step(run_id={row['workflow_run_id']}, "
                           f"step_id={row['step_id']})"),
            "note": ("This build is a step of a workflow run. Retry or resume THE STEP "
                     "(retry_workflow_step) so the run follows it; retry_code_task would start "
                     "an attempt the run never picks up.")}


def _slim_run(r: dict) -> dict:
    # "pr_url" is not a key on a run — the column is pull_request_url. So the PR link this
    # tool's own description promises ("with status, phase, title and PR link") was ALWAYS
    # dropped, and an agent asked to act on a PR could not find one. Include both: the real
    # key, plus the short alias callers may already expect.
    keys = ("id", "status", "phase", "title", "pull_request_url", "pull_request_number",
            "created_at", "task_id")
    out = {k: r.get(k) for k in keys if k in r}
    return _json_safe(out)


def _offer_button_label(label: Any, tool: str, args: Any) -> str:
    from ..services.offer_label import button_label

    return button_label(label, tool, args if isinstance(args, dict) else {})


def _org_github_connection(c: Any, project_id: str) -> dict:
    """The organization's GitHub connection as the registry describes it, and how many
    repositories it reaches / are allowed — the fact a per-project connection list leaves out."""
    try:
        from ..multirepo import registry

        with c.begin_nested():
            reg = registry.list_registry(c, project_id)
        return {**(reg.get("connection") or {}), "repositories": (reg.get("counts") or {}),
                "note": "One connection for the whole organization; no project needs its own."}
    except Exception as exc:  # noqa: BLE001 — no registry here: say so, do not fail the read
        return {"error": f"{type(exc).__name__}: {exc}"[:200]}


def _may_read_project(auth: Any, project_id: str) -> bool:
    """Whether this caller holds project:read in that project (the console's project list uses
    the same permission). No verified identity means the gate already let the call through
    unenforced, so the list is not narrowed further."""
    if auth is None:
        return True
    from ..identity import authz
    from ..identity.scope import ResolvedScope

    try:
        return bool(authz.check(auth, "project:read",
                                ResolvedScope.in_project(auth.principal.org_id, project_id)).allowed)
    except Exception:  # noqa: BLE001 — a decision that cannot be made is not an allow
        return False


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
    r"merge|call|do|vacuum|analyze|reindex|comment|lock|into)\b",
    re.IGNORECASE,
)
_SECRET_TABLES = re.compile(
    r"\b(secret_refs|secret_material|pg_shadow|pg_authid|pg_auth_members)\b",
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


#: The most rows query_project_data returns. A model reads them, so a page, not a dump.
PROJECT_DATA_ROW_CAP = 50
#: Tables query_project_data never reads, whatever the project: credential stores, identity
#: secrets, sessions and keys. Matched as a word anywhere in the table name.
_PROJECT_DATA_DENY = re.compile(
    r"(secret|credential|token|session|password|passkey|webauthn|totp|recovery|signing|"
    r"api_key|apikey|_keys?$|^identity_|oauth|vault|cookie|otp)", re.IGNORECASE)
_IDENT = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


def _query_project_data(ctx: Any, project_id: str, args: dict) -> dict:
    """query_project_data: one table, this project's rows only, SELECT-only by construction.

    WHY NOT query_db. Raw SQL has no tenant fence (a prompt-supplied WHERE is not one), so it is
    refused to every project-scoped agent and in Community. An agent diagnosing a failure still
    needs to read the data, so this builds the statement itself: the table and every column are
    checked against information_schema and quoted, the project filter is not optional, the
    values are bound parameters, the transaction is READ ONLY, rows are capped and the result
    goes through the one redaction layer."""
    from ..services.agent_baseline import redact_for_agent

    table = str(args.get("table") or "").strip().lower()
    if not _IDENT.match(table):
        return {"ok": False, "error": "table must be a plain table name (describe_schema lists them)"}
    if _PROJECT_DATA_DENY.search(table):
        return {"ok": False, "error": f"{table} holds credentials or identity secrets and is "
                "never readable here"}
    try:
        limit = max(1, min(int(args.get("limit") or 20), PROJECT_DATA_ROW_CAP))
    except (TypeError, ValueError):
        limit = 20                       # a model's "twenty" is a page, not a crash
    with ctx.begin() as c:
        if c.dialect.name == "postgresql":
            # FIRST statement of the transaction, or Postgres refuses it.
            c.execute(_text("SET TRANSACTION READ ONLY"))
        cols = [str(r[0]) for r in c.execute(_text(
            # current_schema(), as describe_schema reads it: the columns checked are the columns
            # of the very table the statement below resolves to, whatever the search_path.
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = current_schema() "
            "AND table_name = :t ORDER BY ordinal_position"), {"t": table}).all()]
        if not cols:
            return {"ok": False, "error": f"no table {table!r}; describe_schema lists them"}
        if "project_id" not in cols:
            return {"ok": False, "error": f"{table} has no project_id column, so it cannot be "
                    "fenced to this project; use the dedicated read tools"}
        wanted = [str(x).strip().lower() for x in (args.get("columns") or [])] or cols
        unknown = [x for x in wanted if x not in cols]
        where = args.get("where") if isinstance(args.get("where"), dict) else {}
        unknown += [str(k) for k in where if str(k).lower() not in cols]
        order = str(args.get("order_by") or ("created_at" if "created_at" in cols else "")).lower()
        if order and order not in cols:
            unknown.append(order)
        if unknown:
            return {"ok": False, "error": f"unknown column(s) {sorted(set(unknown))}",
                    "columns": cols}
        sql = (f'SELECT {", ".join(chr(34) + x + chr(34) for x in wanted)} FROM "{table}" '
               "WHERE project_id = :__p")
        params: dict = {"__p": project_id, "__l": limit}
        for i, (key, value) in enumerate(where.items()):
            if str(key).lower() == "project_id":
                continue                          # the fence is not the caller's to move
            if value is None:
                sql += f' AND "{str(key).lower()}" IS NULL'
                continue
            sql += f' AND CAST("{str(key).lower()}" AS TEXT) = :__w{i}'
            params[f"__w{i}"] = str(value)
        if order:
            sql += f' ORDER BY "{order}" DESC NULLS LAST'
        sql += " LIMIT :__l"
        _bound_statement(c)
        rows = c.execute(_text(sql), params).mappings().all()
    return {"table": table, "row_count": len(rows), "capped_at": limit,
            "rows": redact_for_agent([_json_safe(dict(r)) for r in rows])}


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
    if _SECRET_TABLES.search(sql):
        return False, "secret catalog tables are not queryable"
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


def _github_token_for(ctx: Any, project_id: str) -> str:
    """The credential this project's repository is connected with.

    ONE IMPLEMENTATION, in the service layer. This used to hold the resolution itself, and
    preview_service went on reading `settings.github_token` directly — so the fix lived here
    and the bug lived there, and every preview clone of a private repository failed with git
    asking for a username on a terminal nobody is at. Both call
    `github_connections.github_token_for` now, which is where the behaviour is documented.
    """
    from ..services.github_connections import github_token_for

    try:
        with ctx.begin() as connection:
            return github_token_for(connection, ctx.settings, project_id)
    except Exception:  # noqa: BLE001 — no token is an answer; the clone reports it
        return ""


def _project_repository_bound(ctx: Any, project_id: str, repo: str) -> bool:
    """Only a current project binding can authorize an explicit repository read."""
    if repo.count("/") != 1:
        return False
    try:
        with ctx.begin() as connection:
            row = connection.execute(
                _text("SELECT id FROM github_repository_bindings "
                      "WHERE project_id = :p AND lower(full_name) = lower(:repo) "
                      "AND status = 'active' LIMIT 1"),
                {"p": project_id, "repo": repo},
            ).first()
        return row is not None
    except Exception:  # noqa: BLE001 — no binding proof means no repository read
        return False


def _repo_root(settings: Any, repo: str,
               ref: _Optional[str] = None,
               token: str = "") -> tuple[_Optional[str], _Optional[str]]:
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
        from ..services.repo_host import git_access

        # The checkout is what the read tools hand an agent: the token rides in git's
        # environment only, never in the URL or `.git/config` (IDENTITY.md §7.10).
        access = git_access(getattr(settings, "target_repo_host", "github") or "github",
                            owner, short,
                            token or (getattr(settings, "github_token", "") or ""))
        url, genv = access.url, access.environ()
        if _os.path.isdir(_os.path.join(dest, ".git")):
            # A cache made before this kept the token in its remote URL: rewrite it.
            _subprocess.run(["git", "-C", dest, "remote", "set-url", "origin", url],
                            capture_output=True, timeout=30)
            # Refresh, but never fail the read because the network is down — a slightly
            # stale checkout answers most questions; no checkout answers none.
            _subprocess.run(["git", "-C", dest, "fetch", "--depth", "1", "origin"]
                            + ([ref] if ref else []),
                            capture_output=True, timeout=120, env=genv)
            _subprocess.run(["git", "-C", dest, "reset", "--hard",
                             "FETCH_HEAD" if ref else "origin/HEAD"],
                            capture_output=True, timeout=60)
        else:
            out = _subprocess.run(
                ["git", "clone", "--depth", "1"] + (["--branch", ref] if ref else [])
                + [url, dest], capture_output=True, text=True, timeout=300, env=genv)
            if out.returncode != 0:
                # A ref that is a SHA, or a tag, cannot be cloned with --branch. Fall back to a
                # plain clone and fetch it, so "ref" means any ref rather than "branch only".
                if ref:
                    plain = _subprocess.run(["git", "clone", "--depth", "1", url, dest],
                                            capture_output=True, text=True, timeout=300, env=genv)
                    if plain.returncode != 0:
                        return None, f"clone failed: {(plain.stderr or '')[-200:]}"
                    got = _subprocess.run(["git", "-C", dest, "fetch", "--depth", "1",
                                           "origin", ref], capture_output=True, text=True,
                                          timeout=120, env=genv)
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


def _source_for_run(ctx: Any, run_id: str, *, project_id: str) -> tuple[
    _Optional[str], _Optional[str], _Optional[str],
]:
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
                _text("SELECT spec FROM code_task_runs WHERE id = :id AND project_id = :p"),
                {"id": run_id, "p": project_id},
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
        from ..services.repo_host import git_access

        access = git_access(getattr(settings, "target_repo_host", "github") or "github",
                            owner or (getattr(settings, "target_github_owner", "") or ""),
                            short, getattr(settings, "github_token", "") or "")
        for candidate in [c for c in candidates if c]:
            probe = _subprocess.run(["git", "ls-remote", "--heads", access.url, candidate],
                                    capture_output=True, text=True, timeout=60, env=access.environ())
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


def _project_probe_host(ctx: Any, project_id: _Optional[str], host: str
                        ) -> tuple[_Optional[dict], _Optional[str]]:
    """THE TARGET RULE FOR PORT PROBES (M10): a probe goes only to a deployment host registered
    in the acting project (by address or name, not retired). An arbitrary address is a network
    scanner pointed from inside BOSS's network; another project's host is not this caller's.
    Returns (host row, error)."""
    host = (host or "").strip()
    if not host or not project_id:
        return None, "host and project are required"
    try:
        with ctx.begin() as c:
            row = c.execute(
                _text("SELECT id, name, address, port_min, port_max FROM deployment_hosts "
                      "WHERE project_id = :p AND (address = :h OR name = :h) "
                      "AND status <> 'retired' LIMIT 1"),
                {"p": project_id, "h": host}).mappings().first()
    except Exception:  # noqa: BLE001 — no proof of registration is no probe
        row = None
    if row is None:
        return None, ("host is not a deployment host registered in this project; register it "
                      "(list_deployment_hosts) before probing it")
    return dict(row), None


def _resolve_source(ctx: Any, args: dict, *, project_id: str) -> tuple[
    str, _Optional[str], _Optional[str],
]:
    """(repo, ref, error) for a source tool's arguments.

    One place, so search_code and read_code cannot disagree about which tree they are on —
    finding a line in one repo and then reading a different file from another is a worse
    failure than finding nothing.
    """
    run_id = str(args.get("run_id") or "").strip()
    if run_id:
        repo, ref, err = _source_for_run(ctx, run_id, project_id=project_id)
        if err:
            return "", None, err
        return repo or "boss", (args.get("ref") or ref or None), None
    repo = str(args.get("repo") or "boss").strip()
    if repo not in {"boss", "self", "platform", ""}:
        # EVERY EDITION (not only Community): an explicit repository is read only when the
        # acting project has it bound. A paid install's broad token is not authority for one
        # project to read another project's private code. A short name means the configured
        # owner, exactly as _repo_root resolves it.
        full = repo if "/" in repo else (
            f"{getattr(ctx.settings, 'target_github_owner', '') or ''}/{repo}")
        if not _project_repository_bound(ctx, project_id, full):
            return "", None, "repository unavailable in this project"
    return repo, (args.get("ref") or None), None


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
        reversibility="safe", permission="host:exec")
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
                allow_fix=wants_fix, settings=getattr(ctx, "settings", None))

    @reg.tool(
        "factory_briefing",
        "Everything in the whole factory that wants a person right now — unanswered decisions, "
        "failed workflow steps with what each failure MEANS, apps that are deployed and not "
        "answering, and builds that have gone quiet. Call this FIRST when asked an open "
        "question about the system — 'what changed', 'what is blocked', 'what should I do "
        "next', 'how are we doing' — instead of assembling the same picture from six separate "
        "lookups. Read-only.",
        _obj({}),
        kind="read", permission="assistant:chat")
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
        kind="read", permission="workflow:read")
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
        "precheck_workflow",
        "WILL THIS RUN WORK, before it spends anything: checks each step's REAL infrastructure "
        "(the run's consent vs every call the step makes, GitHub access and CI, the sandbox, "
        "budget headroom, the app's variables per environment, the deploy's target, port and "
        "manifest, smoke's contract), plus BOSS's recent BEHAVIOUR for this app (recurring "
        "failures, the app's real start logs, review/restart/retry history, split app keys, "
        "leaked leases), and answers per check ready / fixable / will_fail / unknown "
        "with the evidence and the fix (and which fixes BOSS can make itself). checkpoint 'pre_run' before starting; 'pre_deploy' "
        "after the first build, against its saved workspace (give build_run_id). Read-only. Use "
        "it before offering to start a workflow and when asked 'why will this fail'.",
        _obj({"workflow": {"type": "string",
                           "description": "workflow key or name (or omit and give run_id)"},
              "run_id": {"type": "string", "description": "an existing workflow run (wfr_)"},
              "checkpoint": {"type": "string", "enum": ["pre_run", "pre_deploy"]},
              "build_run_id": {"type": "string",
                               "description": "pre_deploy: the pass-1 build (crun_)"},
              "input": {"type": "object", "description": "the run input it would start with"},
              "project_id": {"type": "string"}}),
        kind="read", permission="workflow:read")
    def precheck_workflow(ctx, *, project_id, actor, args):
        from ..services import workflow_precheck

        workflow = str(args.get("workflow") or "").strip() or None
        run_id = str(args.get("run_id") or "").strip() or None
        if not (workflow or run_id):
            return {"ok": False, "error": "name a workflow, or give run_id"}
        try:
            report = workflow_precheck.precheck(
                ctx.engine, ctx.settings, project_id=args.get("project_id") or project_id,
                actor=actor, workflow=workflow, run_id=run_id,
                run_input=args.get("input") if isinstance(args.get("input"), dict) else None,
                checkpoint=str(args.get("checkpoint") or "pre_run"),
                build_run_id=str(args.get("build_run_id") or "").strip() or None)
        except workflow_precheck.PrecheckError as error:
            return {"ok": False, "error": str(error)}
        return {"ok": True, **report}

    @reg.tool(
        "watch",
        "Keep THIS conversation checking something on an interval until a condition is met — "
        "a pull request, a CI run, a deployment. Use it when the operator says 'keep an eye "
        "on', 'let me know when', 'monitor', or 'tell me when it is done'. Each wake runs in "
        "this same conversation, so you keep the context. It costs a model call per cycle, so "
        "choose an interval matched to how fast the thing actually changes: a CI run that "
        "takes eight minutes does not need a check every minute.",
        _obj({"check": {"type": "string",
                        "description": "what to do on each wake, in your own words"},
              "until": {"type": "string",
                        "description": "REQUIRED. The condition that means it can stop — "
                                       "'the PR is approved', 'the deploy reports healthy'"},
              "interval_seconds": {"type": "integer",
                                   "description": "default 300; minimum 60"},
              "max_cycles": {"type": "integer",
                             "description": "default 24; 0 is unlimited and should be rare"},
              "project_id": {"type": "string"}},
             ["check", "until"]),
        kind="write",
        reversibility="safe", permission="autopilot:operate")
    def watch(ctx, *, project_id, actor, args):
        """The answer to "keep an eye on this", which BOSS simply did not have.

        Before this, the only honest reply was "ask me again later": BOSS could act when
        someone typed and at no other time.
        """
        from ..services.agent_wakeups import WakeupError, arm_monitor

        thread_id = str(args.get("_thread_id") or "").strip()
        if not thread_id:
            return {"ok": False,
                    "error": "a monitor runs inside a conversation, and this turn has none"}
        try:
            with ctx.begin() as c:
                row = arm_monitor(
                    c, project_id=args.get("project_id") or project_id, actor=actor,
                    thread_id=thread_id, prompt=str(args.get("check") or ""),
                    exit_condition=str(args.get("until") or ""),
                    interval_seconds=int(args.get("interval_seconds") or 300),
                    max_cycles=int(args.get("max_cycles", 24) or 0))
        except WakeupError as error:
            return {"ok": False, "error": str(error)}
        return {"ok": True, **_json_safe(row),
                "note": ("Armed. This conversation will wake itself and check; the operator "
                         "does not need to stay. Tell them what you will watch for and how "
                         "often, then stop.")}

    @reg.tool(
        "schedule_work",
        "Run something on a clock, reporting into a NEW conversation each time — a morning "
        "briefing, a nightly audit, a weekly sweep. Use it for 'every weekday at 9', 'each "
        "morning', 'every Monday'. Times are in the operator's timezone, so ask which one if "
        "you do not know it rather than assuming UTC.",
        _obj({"prompt": {"type": "string", "description": "what to do on each run"},
              "at_time": {"type": "string", "description": "24h clock, e.g. '09:00'"},
              "days": {"type": "array", "items": {"type": "string"},
                       "description": "mon..sun; omit for every day"},
              "timezone": {"type": "string",
                           "description": "IANA name, e.g. 'Europe/London'. Ask if unsure."},
              "project_id": {"type": "string"}},
             ["prompt", "at_time"]),
        kind="write",
        reversibility="safe", permission="autopilot:operate")
    def schedule_work(ctx, *, project_id, actor, args):
        from ..services.agent_wakeups import WakeupError, schedule as _schedule

        try:
            with ctx.begin() as c:
                row = _schedule(
                    c, project_id=args.get("project_id") or project_id, actor=actor,
                    prompt=str(args.get("prompt") or ""),
                    at_time=str(args.get("at_time") or "").strip(),
                    days=[str(d) for d in (args.get("days") or [])],
                    tz_name=str(args.get("timezone") or "UTC").strip() or "UTC")
        except WakeupError as error:
            return {"ok": False, "error": str(error)}
        return {"ok": True, **_json_safe(row),
                "note": "Scheduled. Say when the first run lands, in their timezone."}

    @reg.tool(
        "stop_watching",
        "End a monitor or a schedule. Call it with finished=true from inside a monitor's own "
        "wake when the exit condition has been met — that is how a loop finishes rather than "
        "running to its cap. Without an id it stops whatever is watching THIS conversation.",
        _obj({"wakeup_id": {"type": "string",
                            "description": "omit to stop this conversation's monitor"},
              "finished": {"type": "boolean",
                           "description": "true when the thing you were waiting for happened"},
              "reason": {"type": "string"},
              "project_id": {"type": "string"}}),
        kind="write",
        reversibility="safe", permission="autopilot:operate")
    def stop_watching(ctx, *, project_id, actor, args):
        from ..services.agent_wakeups import WakeupError, stop

        pid = args.get("project_id") or project_id
        wakeup_id = str(args.get("wakeup_id") or "").strip()
        thread_id = str(args.get("_thread_id") or "").strip()
        with ctx.begin() as c:
            if not wakeup_id:
                wakeup_id = c.execute(
                    _text("SELECT id FROM agent_wakeups WHERE project_id=:p AND thread_id=:t "
                          "  AND status='active' LIMIT 1"),
                    {"p": pid, "t": thread_id}).scalar() or ""
            if not wakeup_id:
                return {"ok": False, "error": "nothing is watching this conversation"}
            finished = bool(args.get("finished"))
            try:
                row = stop(
                    c, project_id=pid, wakeup_id=wakeup_id, actor=actor,
                    # COMPLETED AND STOPPED ARE DIFFERENT OUTCOMES. One means the thing being
                    # waited for happened; the other means somebody called it off, and an
                    # operator reading back later needs to know which.
                    status="completed" if finished else "stopped",
                    reason=str(args.get("reason") or
                               ("the exit condition was met" if finished
                                else "stopped by the operator")))
            except WakeupError as error:
                return {"ok": False, "error": str(error)}
        return {"ok": True, **row}

    @reg.tool(
        "list_watches",
        "What is currently watching or scheduled on this project: what each one checks, when "
        "it next wakes, which cycle it is on, and what the last one found.",
        _obj({"all": {"type": "boolean", "description": "include stopped and finished ones"},
              "project_id": {"type": "string"}}),
        kind="read", permission="autopilot:read")
    def list_watches(ctx, *, project_id, actor, args):
        from ..services.agent_wakeups import list_wakeups

        with ctx.begin() as c:
            rows = list_wakeups(c, project_id=args.get("project_id") or project_id,
                                active_only=not bool(args.get("all")))
        return {"ok": True, "count": len(rows), "watches": [_json_safe(r) for r in rows]}

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
        reversibility="safe", permission="memory:manage")
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
                    # THE AGENT'S AMBIENT VALUES ARRIVE IN `args`, NOT ON `ctx`.
                    # `engine_agent` merges them into the argument dict before invoking
                    # (`args={**self.ambient, **args, ...}`); reading `ctx.ambient` raised
                    # AttributeError on every call. Found in production by the agent itself,
                    # which traced it to this line and declined to guess a workaround.
                    source_thread_id=args.get("_thread_id"),
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
        kind="read", permission="memory:read")
    def recall(ctx, *, project_id, actor, args):
        from ..services.agent_memory import recall as _recall

        pid = args.get("project_id") or project_id
        kinds = tuple(str(k) for k in (args.get("kinds") or []) if str(k).strip())
        with ctx.begin() as c:
            rows = _recall(c, project_id=pid, actor=actor,
                           role=str(args.get("role") or "").strip() or None,
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
        reversibility="safe", permission="memory:manage")
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
        reversibility="safe", permission="assistant:chat")
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
        thread_id = str(args.get("_thread_id") or "").strip()
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
              "note": {"type": "string",
                       "description": "optional: what should change this time (max 4000 "
                                      "chars). A retried build hands it to the builder."},
              "project_id": {"type": "string"}},
             ["run_id", "step_id"]),
        kind="write",
        reversibility="safe", permission="workflow_run:control")
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
                available = _in_declared_order(c, run_id, c.execute(
                    _text("SELECT step_id, status FROM workflow_step_runs "
                          " WHERE workflow_run_id=:r AND project_id=:p ORDER BY created_at"),
                    {"r": run_id, "p": pid}).mappings().all())
                return {"ok": False,
                        "error": f"no step {step_id!r} on {run_id}",
                        "steps_on_this_run": [
                            {"step_id": s["step_id"], "status": s["status"]}
                            for s in available]}
            # ONE IMPLEMENTATION with the console's "Try … again" — see
            # `workflows.retry_failed_step`. This copy and the API's had drifted: only this one
            # recorded an event.
            from ..services import workflows as _workflows
            try:
                done = _workflows.retry_failed_step(
                    c, project_id=pid, run_id=run_id, step_id=step_id, actor=actor,
                    note=str(args.get("note") or ""))
            except (_workflows.WorkflowConflictError,
                    _workflows.WorkflowValidationError) as error:
                return {"ok": False, "error": str(error)}
        return {"ok": True, "run_id": run_id, "step_id": step_id, "status": "ready",
                "was": done["was"],
                "note": ("Re-queued. The worker picks it up on its next sweep; everything the "
                         "run already produced is kept. Check back with "
                         "describe_workflow_run.")}

    @reg.tool(
        "skip_workflow_step",
        "Go on WITHOUT one failed or dead-lettered workflow step: it is marked skipped (never "
        "succeeded) with the reason and who decided, and the run proceeds to the next steps. "
        "This is the console's \"Skip and proceed\" button — the other answer to a step that "
        "stopped its run, beside retry_workflow_step. Steps that use what it would have "
        "produced are told it was skipped, never handed a value. Refused for an approval "
        "gate (a refused gate is a person's no) and for a step that did not fail. A reason is "
        "required, in the operator's words.",
        _obj({"run_id": {"type": "string", "description": "the wfr_... the step belongs to"},
              "step_id": {"type": "string",
                          "description": "the step's id, e.g. 'research' — NOT its label"},
              "reason": {"type": "string",
                         "description": "why the run should go on without it — kept on the "
                                        "run's record"},
              "project_id": {"type": "string"}},
             ["run_id", "step_id", "reason"]),
        kind="write",
        reversibility="safe", permission="workflow_run:control")
    def skip_workflow_step(ctx, *, project_id, actor, args):
        """The agent's half of "Skip and proceed".

        A failed step now stops its run (boss-1, 2026-10-02: research died, was recorded green,
        and the run walked on into a panel and a build). The console offers Try again and Skip;
        the agent could only offer Try again, so asked "can we just go on without research?" it
        could diagnose the stop and not perform the answer — the same dead end
        `retry_workflow_step` was added to close. Same implementation as the console's button
        (`workflows.skip_failed_step`), so the two cannot drift.
        """
        pid = args.get("project_id") or project_id
        run_id = str(args.get("run_id") or "").strip()
        step_id = str(args.get("step_id") or "").strip()
        reason = str(args.get("reason") or "").strip()
        if not run_id or not step_id:
            return {"ok": False, "error": "run_id and step_id are required"}
        from ..services import workflows as _workflows
        with ctx.begin() as c:
            try:
                done = _workflows.skip_failed_step(
                    c, project_id=pid, run_id=run_id, step_id=step_id, actor=actor,
                    reason=reason)
            except _workflows.WorkflowError as error:
                if isinstance(error, _workflows.WorkflowNotFoundError):
                    available = _in_declared_order(c, run_id, c.execute(
                        _text("SELECT step_id, status FROM workflow_step_runs "
                              " WHERE workflow_run_id=:r AND project_id=:p ORDER BY created_at"),
                        {"r": run_id, "p": pid}).mappings().all())
                    return {"ok": False, "error": str(error),
                            "steps_on_this_run": [
                                {"step_id": s["step_id"], "status": s["status"]}
                                for s in available]}
                return {"ok": False, "error": str(error)}
        return {"ok": True, **done,
                "note": (f"Skipped; the run is now {done.get('run_status')}. Steps that read "
                         "this one's output are told it was skipped. Check back with "
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
        kind="read", permission="workflow_run:read")
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
        declared = _declared_inputs(spec.get("input_schema"))
        supplied = list((run["input"] or {}).keys())
        missing = [k for k in declared if k not in supplied]
        stopped = [dict(s) for s in steps
                   if s["status"] in ("failed", "deadlettered", "cancelled")]

        # WHAT TO DO, COMPUTED — not left for the reader to infer from an error string.
        remedy: dict[str, Any] = {}
        if run["status"] in ("failed", "deadlettered", "cancelled"):
            # NEITHER DIAGNOSIS MAY SUPPRESS THE OTHER.
            #
            # This was `if missing: … elif stopped: …`, so any non-empty `missing` list hid
            # the step's own error. That was catastrophic while `missing` was mis-parsed —
            # 21 of 45 real versions produced a phantom input named "required", which then
            # displaced the true cause. The parsing is fixed (`_declared_inputs`), but the
            # precedence was wrong on its own terms: a step can fail for a reason that has
            # nothing to do with inputs.
            #
            # So missing inputs still lead when there are any — that is usually why the step
            # died — and the step's recorded error is attached either way.
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
                    # Attached, never instead-of: if the step died of something unrelated to
                    # inputs, this is the sentence that says so.
                    **({"step_error": (f"{stopped[0]['step_id']}: "
                                       f"{stopped[0].get('last_error') or 'no error recorded'}")}
                       if stopped else {}),
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
        reversibility="safe", permission="workflow_run:control")
    def recheck_workflow_run(ctx, *, project_id, actor, args):
        from ..services import workflows as _workflows
        from ..services.workflow_recheck import recheck_run

        with ctx.begin() as c:
            try:
                return recheck_run(c, project_id=project_id, run_id=str(args["run_id"]),
                                   requeue=bool(args.get("requeue", True)), actor=actor)
            except _workflows.WorkflowError as error:
                # The run's "recheck" action refused it (a stopped or finished run is not put
                # back in the queue) — the same sentence the console shows, as an answer.
                return {"ok": False, "error": str(error)}

    @reg.tool(
        "get_investigation",
        "The result of an investigation: what it found, what it ran to prove it, the fix, and "
        "whether it applied one. Call this when an investigation was started earlier in the "
        "conversation — while it is still running, say so and answer with what you have.",
        _obj({"investigation_id": {"type": "string",
                                   "description": "optional — defaults to the latest"}}),
        kind="read", permission="reliability:read")
    def get_investigation(ctx, *, project_id, actor, args):
        from ..services.debug_session import get_investigation as _get

        with ctx.begin() as c:
            return _get(c, project_id=project_id,
                        investigation_id=str(args.get("investigation_id") or ""))


def _register_system_health_tools(reg: ToolRegistry) -> None:
    """The machine BOSS runs on, as the system doctor sees it (stability spec §4e): the same
    gauges, findings and options the console's system-health card shows, so a client agent is
    told the same thing an operator is — and can make the same choice on their instruction."""

    @reg.tool(
        "system_health",
        "How the machine BOSS runs on is doing: memory, memory pressure, swap, disk, load — "
        "each with its scope (this server / the Docker Desktop VM, not the Mac under it) — the "
        "degradation level, \"Builds at once\" and what is queued for room, the system "
        "doctor's open findings with the options each offers, and what BOSS fixed on its own. "
        "Read this before blaming a slow or failed build on BOSS.",
        _obj({"project_id": {"type": "string"}}),
        kind="read", permission="system:read")
    def system_health(ctx, *, project_id, actor, args):
        from ..services import admission, system_doctor

        cap = admission.sample()
        with ctx.begin() as c:
            state = admission.snapshot(c, cap=cap)
            findings = system_doctor.open_findings(c)
            actions = system_doctor.recent_actions(c, limit=20)
        return _json_safe({"scope": cap.scope, "level": state.get("level"),
                           "gauges": {k: v for k, v in cap.__dict__.items() if k != "notes"},
                           "admission": state, "findings": findings,
                           "recent_actions": actions})

    @reg.tool(
        "system_doctor_act",
        "Choose one of a system-health finding's options (by its key, as system_health lists "
        "them) — e.g. queue new builds, hold updates while builds run, free BOSS's disk. "
        "Options that stop a build only on the operator's explicit instruction.",
        _obj({"finding_id": {"type": "string"}, "option": {"type": "string"}},
             ["finding_id", "option"]),
        kind="write",
        # Some options pause or cancel a build: never pressed on a guess.
        reversibility="destroys_work", permission="system:operate",
        # The doctor acts on the whole install (builds, disk, updates), like its API route
        # (Scope.org): a project-level system:operate binding is not enough (M6).
        org_scope=True)
    def system_doctor_act(ctx, *, project_id, actor, args):
        from ..services import system_doctor

        try:
            return _json_safe(system_doctor.act(ctx.engine, ctx.settings,
                                                str(args["finding_id"]), str(args["option"]),
                                                actor=actor or "agent"))
        except (LookupError, system_doctor.ActRefused) as exc:
            return {"ok": False, "error": str(exc)}


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
        kind="read", permission="code_task:read")
    def diagnose_run(ctx, *, project_id, actor, args):
        run_id = str(args.get("run_id") or "").strip()
        pid = project_id
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
                      "FROM code_task_runs WHERE id = :r AND project_id = :p"),
                {"r": run_id, "p": pid}).mappings().first()
            if run is None:
                return {"ok": False, "error": f"no code task run {run_id}"}
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
            # THE ENGINE COULD NOT RUN (harness_failures): recorded when a session found its
            # sandbox, a runtime binary, its login or account broken and the build stopped.
            harness = c.execute(
                _text("SELECT data FROM events WHERE project_id = :p AND entity_id = :r "
                      "AND event_type = 'code_task.harness_failed' "
                      "ORDER BY created_at DESC LIMIT 1"),
                {"p": pid, "r": run_id}).scalar()
            # THE APP'S OWN START (app_start): command, the variables BOSS set by name and
            # source, the exit code and the app's own last lines — for "why did it not start".
            from ..services import app_start as _app_start

            app_start_rec = _app_start.latest(c, project_id=pid, run_id=run_id)
            if app_start_rec is not None:
                checked.append("the app's last start record")

        minutes = float(run["minutes"] or 0)
        if isinstance(harness, dict) and harness.get("sentence"):
            # First: nothing else about the run matters until the engine can run.
            findings.append(
                f"THE ENGINE COULD NOT RUN — not the app's code: {harness['sentence']} "
                "Make that fix, then retry; the run stopped as soon as it was seen.")
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
        start_finding = _app_start.finding(app_start_rec)
        if start_finding:
            findings.append(start_finding)
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
        with ctx.begin() as c:
            owning_step = _workflow_step_of(c, project_id, run_id)
        if status in ("cancelled", "failed", "blocked", "error") and owning_step:
            resolution = {
                "tool": "retry_workflow_step",
                "args": {"run_id": owning_step["workflow_run_id"],
                         "step_id": owning_step["step_id"]},
                "label": f"Retry the {owning_step['step_id']} step",
                "why": owning_step["note"],
            }
        elif status in ("cancelled", "failed", "blocked", "error"):
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
            "app_start": ({k: app_start_rec.get(k) for k in (
                "path", "label", "ok", "command", "cwd", "env", "exit_code", "error_lines")}
                | {"output_tail": list(app_start_rec.get("output_tail") or [])[-40:]}
                if app_start_rec else None),
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
        "promised to an app between deploys is not free. Unavailable in Community until "
        "host probes have a project-bound network policy.",
        _obj({"host": {"type": "string", "description": "address, e.g. 192.0.2.202"},
              "start": {"type": "integer", "description": "first port to consider (default 3000)"},
              "count": {"type": "integer", "description": "how many to report (default 3)"},
              "project_id": {"type": "string"}},
             ["host"]),
        # A port scan from inside BOSS's network: a person's host operation, never an agent's
        # on its own (host:exec is non-delegable), and only at the project's own hosts (M10).
        kind="read", permission="host:exec")
    def find_free_port(ctx, *, project_id, actor, args):
        import socket

        if _community_restricted(ctx.settings):
            return {"ok": False, "error": "Community host-port probes are paused until "
                    "the destination and port range are bound to the current project."}
        host = str(args.get("host") or "").strip()
        if not host:
            return {"ok": False, "error": "host is required"}
        pid = args.get("project_id") or project_id
        target, refused = _project_probe_host(ctx, pid, host)
        if refused:
            return {"ok": False, "error": refused}
        host = str(target["address"])
        start = max(1024, int(target.get("port_min") or 1024), int(args.get("start") or 3000))
        want = max(1, min(int(args.get("count") or 3), 10))

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
        ceiling = min(65535, int(target.get("port_max") or 65535) + 1)
        while len(free) < want and checked < 200 and port < ceiling:
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
        kind="read", permission="code_task:read")
    def read_run_turns(ctx, *, project_id, actor, args):
        run_id = str(args.get("run_id") or "").strip()
        if not run_id:
            return {"ok": False, "error": "run_id is required"}
        limit = max(1, min(int(args.get("limit") or 40), 200))
        stage = str(args.get("stage") or "").strip()
        with ctx.begin() as c:
            run = c.execute(
                _text("SELECT status, phase FROM code_task_runs "
                      "WHERE id = :r AND project_id = :p"),
                {"r": run_id, "p": project_id}).mappings().first()
            if run is None:
                return {"ok": False, "error": f"no code task run {run_id}"}
            summary = c.execute(
                _text("SELECT count(*) AS turns, min(created_at) AS first_turn, "
                      "max(created_at) AS last_turn, "
                      "count(*) FILTER (WHERE direction = 'tool_call') AS tool_calls, "
                      "count(*) FILTER (WHERE direction = 'tool_result') AS tool_results, "
                      "count(*) FILTER (WHERE ok = false) AS failures, "
                      "EXTRACT(EPOCH FROM (now() - max(created_at))) AS silent_seconds "
                      "FROM agent_turns WHERE code_task_run_id = :r"),
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
        kind="read", permission="policy:read")
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
        reversibility="safe", permission="policy:manage",
        # all_projects=true writes a rule binding EVERY project: decided at org scope (M7).
        org_scope="all_projects")
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
        _obj({"host": {"type": "string", "description": "public address, e.g. 192.0.2.202"},
              "ports": {"type": "array", "items": {"type": "integer"},
                        "description": "ports to probe"},
              "project_id": {"type": "string"}},
             ["host", "ports"]),
        # Connects out from inside BOSS's network: non-delegable host:exec, and only to the
        # project's own registered hosts (M10).
        kind="read", permission="host:exec")
    def check_port_exposure(ctx, *, project_id, actor, args):
        import socket

        host = str(args.get("host") or "").strip()
        ports = [int(p) for p in (args.get("ports") or [])
                 if str(p).strip().isdigit() and 0 < int(p) < 65536][:20]
        if not host or not ports:
            return {"ok": False, "error": "host and at least one port are required"}
        target, refused = _project_probe_host(ctx, args.get("project_id") or project_id, host)
        if refused:
            return {"ok": False, "error": refused}
        host = str(target["address"])

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

    Agent Studio listed a workforce and "Ask" opened the GENERAL agent with "@QA Lead: "
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
        "positions it took, on which brief, and when. Use it when the question turns on "
        "JUDGEMENT this role has exercised before — a trade-off, a standard, a decision it "
        "has taken a side on: an SME's value is the specifics it has seen, and a generic "
        "answer from a named expert is worse than no expert. Also useful for 'what has our "
        "QA lead been worried about'. "
        "DO NOT call it for a lookup. 'Where is the Google Cloud Console', 'what does this "
        "flag do', 'what is the API endpoint' need the answer, not a position — calling this "
        "first produced a reply that opened with governance advice and reported having no "
        "recorded position, for a question that wanted a URL. And if it returns nothing, say "
        "nothing about it: the absence of a recorded position is not news to the reader.",
        _obj({"role": {"type": "string",
                       "description": "role title or key, e.g. 'QA / Test' or 'qa'"},
              "limit": _LIMIT,
              "project_id": {"type": "string"}},
             ["role"]),
        kind="read", permission="meeting:read")
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
                _text("SELECT id, title, directive, state, created_at FROM meeting_runs "
                      "WHERE project_id = :p AND state != '{}'::jsonb "
                      "ORDER BY created_at DESC LIMIT 200"),
                {"p": pid}).mappings().all()

        from ..services.meeting_service import meeting_title
        from ..services.agent_acts import resolve_role

        # THE SEAT IT SAT IN, as signed work: each contribution carries its act id so the
        # agent can cite it as [act:…] and the dock can link it.
        with ctx.begin() as c:
            seat_acts = {r["meeting_run_id"]: r["act_id"] for r in c.execute(
                _text("SELECT DISTINCT ON (meeting_run_id) meeting_run_id, act_id "
                      "FROM agent_acts WHERE project_id = :p AND kind = 'seat' "
                      "AND role_key = :r ORDER BY meeting_run_id, started_at DESC"),
                {"p": pid, "r": resolve_role(wanted)}).mappings()}

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
                        "brief": meeting_title(row["title"], row["directive"])[:160],
                        "round": round_name,
                        "when": row["created_at"].isoformat() if row["created_at"] else None,
                        # Enough to be specific, not so much that a dozen of them flood the
                        # context this is meant to inform.
                        "said": said.strip()[:1200],
                        "act_id": seat_acts.get(row["id"]),
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
        "my_recent_work",
        "This agent's OWN recent signed work — workflow steps, build stages, panel seats, "
        "chats — newest first, each with its act id, what it was and how it reached the "
        "agent. Use it when asked what you have done or worked on, and cite an item as "
        "[act:<act_id>] so the reader can open it. Never claim work this does not list.",
        _obj({"role": {"type": "string",
                       "description": "the agent's role key; defaults to the caller"},
              "kind": {"type": "string",
                       "enum": ["step", "stage", "seat", "chat", "approval", "handoff"]},
              "limit": _LIMIT,
              "project_id": {"type": "string"}},
             []),
        kind="read", permission="activity:read")
    def my_recent_work(ctx, *, project_id, actor, args):
        from ..services.agent_acts import my_recent_work as recent, resolve_role

        pid = args.get("project_id") or project_id
        role = resolve_role(str(args.get("role") or actor or ""))
        if not role:
            return {"ok": False, "error": "role is required"}
        with ctx.begin() as c:
            items = recent(c, pid, role, limit=int(args.get("limit") or 10),
                           kind=args.get("kind") or None)
        return {"project_id": pid, "role": role, "items": items, "count": len(items),
                "note": ("cite an item as [act:<act_id>]" if items else
                         "no signed work recorded for this agent yet; say so only if asked")}

    @reg.tool(
        "describe_role",
        "Who a role is on this project: the lens it argues from, and the rules and skills its "
        "agent definition carries. Read this before answering as a role, so the answer comes "
        "from that role's actual remit rather than a guess at what the title implies.",
        _obj({"role": {"type": "string"}, "project_id": {"type": "string"}}, ["role"]),
        kind="read", permission="agent:read")
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
            # Agent World: who it is (callsign, tagline, voice) — how it speaks, never what it
            # may do (services/agent_persona.py).
            "persona": spec.get("persona") or {},
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
    fields apart: `domains` said 192.0.2.202:8000 while `app_port` said 3020. The health check
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
        kind="read", permission="deployment:read")
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
        reversibility="safe", permission="deployment_target:manage")
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
        reversibility="costs_money", permission="artifact:rebuild")
    def generate_image(ctx, *, project_id, actor, args):
        import base64
        import urllib.error
        import urllib.request

        settings = ctx.settings
        if _community_raw_sql_disabled(settings):
            return {"ok": False, "error": "Community image generation is paused until "
                    "provider spend has a budget reservation and receipt."}
        # THE DECLARED REQUIREMENT, READ THE ONE WAY (services/model_routing.py). This read
        # OPENAI_API_KEY from the environment, so a key stored through the console was invisible
        # to it and the failure said "OPENAI_BASE_URL and OPENAI_API_KEY must both be set" — a
        # sentence about env vars, on an install configured through the console.
        from types import SimpleNamespace

        from ..services import model_routing

        try:
            with ctx.begin() as c:
                key = model_routing.provider_credential(
                    SimpleNamespace(connection=c, project_id=project_id, settings=settings),
                    model_routing.tool_requirement_id("generate_image"), "openai_images")
        except model_routing.ProviderRequirementMissing as exc:
            return {"ok": False, "error": str(exc), "needs": exc.requirement.need_key}
        base = (str(getattr(settings, "openai_base_url", "") or "").strip()
                or "https://api.openai.com/v1").rstrip("/")

        prompt = str(args.get("prompt") or "").strip()
        if not prompt:
            return {"ok": False, "error": "prompt is required"}
        model = str(args.get("model")
                    or getattr(settings, "image_model", "") or "gpt-image-1")
        size = str(args.get("size") or "1024x1024")

        rel = str(args.get("path") or "").strip().lstrip("/")
        if not rel:
            return {"ok": False, "error": "path is required"}
        # ONE DIRECTORY PER PROJECT. A shared directory let one project overwrite (or read
        # back) another's artwork by naming the same relative path.
        if not project_id or not re.fullmatch(r"[A-Za-z0-9_.-]{1,120}", str(project_id)) \
                or str(project_id) in {".", ".."}:
            return {"ok": False, "error": "generate_image needs the acting project"}
        root = _os.path.join(_os.environ.get("BOSS_IMAGE_DIR") or _os.path.join(_REPO_CACHE, "images"),
                             str(project_id))
        _os.makedirs(root, exist_ok=True)
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
        _obj({"project_id": {"type": "string"}}), permission="code_task:read")
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
        reversibility="safe", permission="code_task:steer")
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
             ["pattern"]), permission="repository:read")
    def search_code(ctx, *, project_id, actor, args):
        import fnmatch as _fn

        try:
            rx = re.compile(args["pattern"])
        except re.error as exc:
            return {"ok": False, "error": f"bad regex: {exc}"}
        repo, ref, err = _resolve_source(ctx, args, project_id=project_id)
        if err:
            return {"ok": False, "error": err}
        root, err = _repo_root(ctx.settings, repo, ref,
                               token=_github_token_for(ctx, project_id))
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
              "limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 200}}), permission="repository:read")
    def list_source(ctx, *, project_id, actor, args):
        """WHAT IS IN THIS APP — the question every investigation starts from.

        BOSS could grep a repo and read a named file, and had no way to ask what the repo
        CONTAINED. So an investigation opened with a guess at a filename, and a wrong guess is
        indistinguishable from a file that does not exist.
        """
        import fnmatch as _fn

        repo, ref, err = _resolve_source(ctx, args, project_id=project_id)
        if err:
            return {"ok": False, "error": err}
        root, err = _repo_root(ctx.settings, repo, ref,
                               token=_github_token_for(ctx, project_id))
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
             ["file"]), permission="repository:read")
    def read_code(ctx, *, project_id, actor, args):
        repo, ref, err = _resolve_source(ctx, args, project_id=project_id)
        if err:
            return {"ok": False, "error": err}
        root, err = _repo_root(ctx.settings, repo, ref,
                               token=_github_token_for(ctx, project_id))
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
        "optionally filtered by a regex. Community reads only this project's captured "
        "database logs, never shared service log files.",
        _obj({"service": {"type": "string", "enum": ["api", "worker", "web"]},
              "pattern": {"type": "string", "description": "optional regex filter"},
              "limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 120}}), permission="observability:read")
    def read_logs(ctx, *, project_id, actor, args):
        service = (args.get("service") or "api").lower()
        limit = int(args.get("limit") or 120)
        restricted = _community_restricted(ctx.settings)
        rx = None
        if args.get("pattern"):
            try:
                rx = re.compile(args["pattern"])
            except re.error as exc:
                return {"ok": False, "error": f"bad regex: {exc}"}

        # Docker keeps container stdout on disk; a local run writes .local-logs/.
        # Try both rather than assuming one deployment shape.
        candidates = [] if restricted else [
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
            if restricted:
                sql += "AND project_id = :pid "
                params["pid"] = project_id
            elif args.get("project_id") or project_id:
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
        "The database's own shape: tables, columns and types. Use it to inspect the "
        "schema when troubleshooting; use project-scoped read tools for data. Raw SQL "
        "is unavailable from project-pinned chat.",
        _obj({"table": {"type": "string", "description": "optional: one table's columns"},
              "like": {"type": "string", "description": "optional name filter, e.g. deployment"}}), permission="system:read")
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
                "hint": "call describe_schema with table=<name> for columns; use project-scoped read tools for data"}

    @reg.tool(
        "query_project_data",
        "Read rows of ONE table for THIS project: read-only, fenced to this project's rows, "
        "capped, and redacted (credential columns and secret-shaped values are removed). Name "
        "the table (describe_schema lists them), optional equality filters on its columns, "
        "the columns you want and a column to sort newest-first by. Use it when no dedicated "
        "tool answers: 'which deployment target has no host', 'the last 5 events of type X'. "
        "Not raw SQL; a table without a project_id column cannot be read here.",
        _obj({"table": {"type": "string", "description": "a public table with a project_id column"},
              "where": {"type": "object",
                        "description": "optional {column: value} equality filters"},
              "columns": {"type": "array", "items": {"type": "string"},
                          "description": "optional; default all (credential columns removed)"},
              "order_by": {"type": "string",
                           "description": "optional column, newest first (default created_at "
                                          "when the table has it)"},
              "limit": {"type": "integer", "minimum": 1, "maximum": PROJECT_DATA_ROW_CAP,
                        "default": 20}},
             ["table"]), permission="system:read")
    def query_project_data(ctx, *, project_id, actor, args):
        return _query_project_data(ctx, project_id, args)

    @reg.tool(
        "inspect_host",
        "Run a READ-ONLY command on a registered deployment host over SSH — what is "
        "running there, disk, a deployed app's own container logs. The database records "
        "what BOSS INTENDED; this shows what the machine is actually doing. Refuses "
        "anything that could change the host.",
        _obj({"host": {"type": "string", "description": "host name or id from the fleet"},
              "command": {"type": "string",
                          "description": "e.g. 'docker ps', 'df -h', 'docker logs --tail 50 app'"}},
             ["host", "command"]), permission="host:exec")
    def inspect_host(ctx, *, project_id, actor, args):
        command, error = _inspect_host_command(args.get("command"))
        if error:
            return {"ok": False, "error": error,
                    "note": "this tool inspects; it never changes a host."}
        return _run_read_on_host(ctx, project_id, (args.get("host") or "").strip(), command)

    @reg.tool(
        "read_app_logs",
        "A deployed app's own container logs, read-only: name the app (or its deployment "
        "target) and BOSS finds its host and its containers, every service of a compose "
        "stack included, and returns the last lines of each. Use it for 'show me the app's "
        "logs', 'did the seed run', 'why does it crash'.",
        _obj({"app": {"type": "string",
                      "description": "the app's name, or its deployment target id"},
              "environment": {"type": "string", "description": "e.g. staging; optional"},
              "tail": {"type": "integer", "minimum": 1, "maximum": 400, "default": 80}},
             ["app"]), permission="host:exec")
    def read_app_logs(ctx, *, project_id, actor, args):
        """Harbor was asked for HelpDesk's container logs and answered "I have no tool to run
        docker logs": inspect_host can, but only given a container name, and a compose stack's
        names (helpdesk-ai-app-1, helpdesk-ai-db-1) live on the host, not in any record the
        agent reads. Finding them is BOSS's job, so this does it: the target, its host, and its
        containers by the compose project label or the single-container name."""
        command, where = _app_logs_command(ctx, project_id, args)
        if command is None:
            return {"ok": False, "error": where}
        target, host_id, source = where
        out = _run_read_on_host(ctx, project_id, host_id, command)
        if out.get("ok") is False:
            return out
        if not (out.get("stdout") or "").strip():
            out["note"] = f"no containers found for {target['app_name']} ({source}) on the host"
        return {**out, "app": target["app_name"], "environment": target.get("environment"),
                "containers_from": source}

    def _run_read_on_host(ctx, project_id, want, command):
        """inspect_host's transport: the registered host, its own key, one read command."""
        from ..services.deploy_service import (_discard_key_file, _materialise_key, _ssh,
                                               _ssh_key_file)
        from ..services.secret_backends import resolve_target_secret

        with ctx.begin() as c:
            row = c.execute(_text(
                "SELECT * FROM deployment_hosts "
                "WHERE project_id = :p AND (id = :h OR name = :h OR address = :h)"),
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
                       int(host.get("ssh_port") or 22), command,
                       timeout=_turn_seconds(45))
        finally:
            if ephemeral:
                _discard_key_file(ephemeral)
        return {"host": host["name"], "command": command, "exit_code": res.returncode,
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
             ["pull_request"]), permission="github:read")
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

        pid = project_id
        raw = str(args.get("pull_request") or "").strip()
        if not raw:
            return {"ok": False, "error": "pull_request is required (number, url, or crun_ id)"}

        repo_full = str(args.get("repo") or "").strip()
        run_repo = ""
        number = ""

        if raw.startswith("crun_"):
            # Resolve the task's own PR, so "why did crun_x fail to preview" is one call.
            with ctx.begin() as c:
                row = c.execute(
                    # repo and owner live in the spec JSON, not as columns: selecting them bare
                    # raised UndefinedColumn 'repo' on every crun_ id (boss-1, 2026-10-02).
                    _text("SELECT pull_request_url, pull_request_number, "
                          "spec->>'repo' AS repo, spec->>'owner' AS owner "
                          "FROM code_task_runs WHERE id = :r AND project_id = :p"),
                    {"r": raw, "p": pid}).mappings().first()
            if not row:
                return {"ok": False, "error": f"no code task {raw!r} in this project"}
            if not (row.get("pull_request_url") or row.get("pull_request_number")):
                return {"ok": False, "error": f"{raw} has not opened a pull request",
                        "note": "a task with no PR has nothing to read"}
            number = str(row.get("pull_request_number") or "")
            if row.get("owner") and row.get("repo"):
                run_repo = f"{row['owner']}/{row['repo']}"
                repo_full = run_repo
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

        # A run ID proves the project's own recorded repository. Raw PR numbers/URLs require a
        # current binding — in EVERY edition: neither caller text nor a broad install token is
        # authority to inspect an arbitrary private repository.
        if not run_repo and not _project_repository_bound(ctx, pid, repo_full):
            return {"ok": False, "error": "repository unavailable in this project"}
        if _community_restricted(ctx.settings):
            token = _github_token_for(ctx, pid)
        else:
            token = getattr(ctx.settings, "github_token", "") or ""

        def get(path: str):
            req = _ureq.Request(
                f"https://api.github.com{path}",
                headers={"Accept": "application/vnd.github+json",
                         **({"Authorization": f"Bearer {token}"} if token else {})})
            with _ureq.urlopen(req, timeout=_turn_seconds(25)) as resp:
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
        "Use inspect_host when you only need to look. Not available in Community, and off "
        "elsewhere unless `BOSS_AGENT_HOST_SHELL` is set: where it is unavailable, do not "
        "offer it — host work such as creating the deploy directory is done by the deploy's "
        "own preflight, so offer a retry or `redeploy` instead.",
        _obj({"host": {"type": "string", "description": "host name, id or address"},
              "command": {"type": "string", "description": "the shell command to run"},
              "timeout_s": {"type": "integer", "minimum": 5, "maximum": 600, "default": 120}},
             ["host", "command"]),
        kind="write",
        reversibility="irreversible", permission="host:exec")
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

        unavailable = action_unavailable("run_on_host", ctx.settings)
        if unavailable:
            return {"ok": False, "error": unavailable,
                    "note": "inspect_host remains available for supported read-only commands."}

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
        "which is not the same question. Unavailable in Community until URLs can be "
        "bound to the current project and a network policy.",
        _obj({"url": {"type": "string"},
              "timeout_s": {"type": "integer", "minimum": 1, "maximum": 30, "default": 10}},
             ["url"]), permission="observability:manage")
    def http_probe(ctx, *, project_id, actor, args):
        import time as _time
        import urllib.error as _uerr
        import urllib.request as _ureq

        if _community_restricted(ctx.settings):
            return {"ok": False, "error": "Community URL probes require a project-bound "
                    "destination and a network policy; use recorded deployment health."}
        url = (args.get("url") or "").strip()
        if not url.startswith(("http://", "https://")):
            return {"ok": False, "error": "url must start with http:// or https://"}
        started = _time.time()
        try:
            with _ureq.urlopen(url, timeout=_turn_seconds(int(args.get("timeout_s") or 10))) \
                    as resp:
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
                         "description": "optional: commits touching this string"}}), permission="repository:read")
    def git_history(ctx, *, project_id, actor, args):
        repo, ref, err = _resolve_source(ctx, args, project_id=project_id)
        if err:
            return {"ok": False, "error": err}
        root, err = _repo_root(ctx.settings, repo, ref,
                               token=_github_token_for(ctx, project_id))
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
        _obj({"like": {"type": "string", "description": "optional name filter, e.g. sandbox"}}), permission="settings:read")
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


# A project-pinned conversation may not run arbitrary SQL against the control-plane DB.
# The SQL handlers have no project predicate or tenant row policy, even in paid editions.
# Keep direct/admin tools registered separately, but hold raw SQL out of this surface until
# a database-enforced project boundary exists.
_DISCUSS_EXCLUDED_READ_TOOLS: frozenset[str] = frozenset({"query_db"})


def _turn_seconds(default: float) -> float:
    """A tool's own timeout, cut to the chat turn's tool time (services/call_deadline.py).

    Outside a turn it is the tool's own setting. Inside one, a slow host, URL or API cannot
    carry the turn past the console's 240 s stall line: the tool gives up when the turn would,
    and the agent answers with what it has.
    """
    from ..services.call_deadline import tool_timeout

    return tool_timeout(default)


def _bound_statement(connection) -> None:
    """Cut a read-only SQL statement at the turn's tool time, on Postgres. See _turn_seconds."""
    from ..services.call_deadline import remaining, tool_timeout

    if remaining() is None or connection.dialect.name != "postgresql":
        return
    connection.execute(_text(f"SET LOCAL statement_timeout = {int(tool_timeout(3600) * 1000)}"))


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
    _register_system_health_tools(reg)
    _register_debug_session_tools(reg)
    _register_write_tools(reg)
    _register_operations_tools(reg)
    _register_governance_tools(reg)
    # Giving an agent a skill or an MCP server: the act Agent Studio exists for, by asking.
    from .agent_tools import register_agent_tools

    register_agent_tools(reg)
    # App templates and the security loop's reads (APP_TEMPLATES_AND_SECURITY_LOOP.md §2.6, §4.3).
    from .app_platform_tools import register_app_platform_tools

    register_app_platform_tools(reg)
    from .knowledge_tools import register_knowledge_tools

    register_knowledge_tools(reg)
    # The captain's powers are the BOSS agent's actions now, not a second engine (BOSSAPP-759).
    from .captain_tools import register_captain_tools

    register_captain_tools(reg)
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
    # "Remember that ..." from chat: one fact, stored `inferred` (quarantined, forgettable, never
    # above a governed or approved fact) until a person promotes it (BOSSAPP-622).
    "kg_propose_fact",
    # "Scan my app" from chat: the pinned scanners on the app's own code, BOSS's own targets only.
    "run_security_scan",
    # SETUP REPAIR, because a diagnosis with no matching capability is a loop — and setup is
    # where that loop hurts most, since it is the first thing a customer touches. Asked twice
    # about the same failed GitHub connection, BOSS explained it correctly and then told the
    # operator to go and press Test access itself: "just explaining the problem would not help
    # anyone." Both of these are reversible and neither takes a credential.
    "test_github_access",
    "set_project_setting",
    # "Create a project called northpeak-bank-a": a new project in the org (project:create).
    "create_project",
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
    # ...and its other half. A failed step stops its run; the operator's two answers are try
    # it again or go on without it, and the agent has to be able to offer both.
    "skip_workflow_step",
    # MEMORY IS WORTHLESS IF THE AGENT CANNOT REACH IT FROM A CONVERSATION. The moment an
    # operator corrects you is the only moment that correction exists — it has to be writable
    # from the turn in which it is said, not from a separate admin path.
        # "Keep an eye on this" is said in a conversation, so it has to be armable from one.
    "watch",
    "schedule_work",
    "stop_watching",
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
    # A hollow review (PR #223) is fixed by reviewing the PR's real diff again, on request.
    "review_pull_request_again",
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
    # THE OTHER END OF A SKILL: who holds it. Authoring and placing a skill were reachable from
    # chat and giving one to an agent was not, so "give the editor the trim skill" ended in a
    # direction to go and press a button. Same kind of act as the three above — it changes what
    # an agent CAN do, starts nothing and spends nothing — and taking it back is the same tool.
    "assign_skill_to_agent",
    # Reachable from chat, but OFFERED: it is declared `grants_authority` (a server's
    # credentials go with it), so the model proposes the grant and an operator's click runs it.
    "assign_mcp_to_agent",
    # CONNECTING A SOURCE OF TOOLS, so "add my MCP server and use its search tool in this
    # workflow" can be finished in the conversation that asked for it. Registered unreviewed
    # (see the tool), so nothing at tier 2 or above becomes placeable without a person.
    "register_mcp_server",
    # STARTING WORK MEANS STARTING A WORKFLOW.
    #
    # `create_discovery` and `request_meeting` are deliberately NOT here. Research and the PRD
    # panel are steps of a workflow, and one started on its own belongs to no run: the operator
    # asked BOSS to research a stock-recommendation site, BOSS ran a bare discovery, and the
    # finished dossier was reachable from nothing — no run linked to it, and the console has no
    # standing page for one because nobody should be starting them individually. The workflow is
    # the unit of work and its document is the outcome.
    "start_workflow",
    # ...and its narrower twin: "add Stripe checkout to the shop" is a build onto the app's
    # branch through the same governed start, gated by the template's own tests.
    "apply_template",
    # Delivery and environment
    # A shell on the operator's own host. Gated a second time by BOSS_AGENT_HOST_SHELL, so
    # admitting it here does not by itself hand anybody a shell.
    "run_on_host",
    "redeploy",
    # Removing an app: a button the person presses, governed like a deploy (app_removal).
    "remove_app",
    "start_preview",
    "stop_preview",
    # Repositories from the org's registry: allow one for BOSS (pressed by a person — it is a
    # grant), add it to / take it out of a project. The same services as the console's routes.
    "register_repository",
    "allow_repository",
    "remove_project_repository",
    # The operator's own authority, exercised on their instruction
    "decide",
    "waive_gates",
    "send_feedback",
    # The machine: a system-health card's option, chosen on the operator's instruction
    # (stability spec §4e). The same options the console offers; stopping a build is one only
    # when they say so.
    "system_doctor_act",
    # The captain's powers, retired as an engine and kept as the agent's actions (BOSSAPP-759).
    # drive / overwatch / theater are operator_only: offered, pressed by a person.
    "captain_watch",
    "captain_set_goal",
    "captain_drive",
    "captain_overwatch",
    "captain_theater",
})

#: REGISTERED, AND DELIBERATELY NOT REACHABLE FROM CHAT.
#:
#: The workflow's `research` and `meeting` steps need these tools, but a chat must start a
#: workflow so the run and its outcome stay linked. The separate raw SQL hold below protects
#: project-pinned chat until the database enforces project scope.
_WORKFLOW_ONLY_TOOLS = frozenset({"create_discovery", "request_meeting"})
_DISCUSS_UNSCOPED_WRITE_TOOLS = frozenset({"write_db"})

#: Tables raw SQL may never write: every identity table (bindings, roles, seats, credentials, the
#: audit log) and the legacy grant tables the identity migration reads.
_ACCESS_TABLE = __import__("re").compile(
    r"(?<![a-z0-9_])(identity_[a-z0-9_]+|project_members|org_members|break_glass_grants|"
    r"api_credentials)(?![a-z0-9_])")


def _writes_access_tables(statement: str) -> str | None:
    """The first access table a raw SQL statement names, or None."""
    found = _ACCESS_TABLE.search((statement or "").lower().replace('"', ""))
    return found.group(1) if found else None


#: What a person reads when an action cannot run here — plain words, the same ones the agent
#: repeats. The governance detail behind each ("approved ChangeSet, budget reservation and
#: ResourceReceipt") is for the log, not for the operator: Harbor read it aloud, and an operator
#: was told to "turn on governed host-shell execution (BOSSAGENTHOSTSHELL / the Resource Plane)".
_UNAVAILABLE_SAID: dict[str, str] = {
    "community_host_shell": (
        "This edition doesn't let agents run commands on your hosts. BOSS's own deploy steps "
        "do the host work — preflight creates the deploy directory, and a retry or `redeploy` "
        "ships the build again."),
    "host_shell_off": (
        "Agents can't run commands on your hosts on this install (the `BOSS_AGENT_HOST_SHELL` "
        "setting is off). BOSS's own deploy steps still do the host work."),
}
_UNAVAILABLE_DETAIL: dict[str, str] = {
    "community_host_shell": ("Community: run_on_host stays off until host commands go through "
                             "an approved ChangeSet, budget reservation and ResourceReceipt."),
    "host_shell_off": "run_on_host needs BOSS_AGENT_HOST_SHELL=true.",
}


def action_unavailable(name: str, settings: Any) -> str | None:
    """Why the action `name` cannot run on this install, in the words a person reads, or None.
    THE ONE RULE.

    The tool and `propose_action` both read it, so an agent never offers a button the tool
    will refuse. Harbor offered "run on host"
    to fix a deploy's directory, and the click answered "Community host shell is disabled".
    """
    if name == "run_on_host":
        if settings is None:
            from ..config import get_settings

            settings = get_settings()
        if _community_raw_sql_disabled(settings):
            key = "community_host_shell"
        elif not getattr(settings, "agent_host_shell", False):
            key = "host_shell_off"
        else:
            return None
        import logging

        logging.getLogger(__name__).info("action %s unavailable: %s", name,
                                         _UNAVAILABLE_DETAIL[key])
        return _UNAVAILABLE_SAID[key]
    return None


def build_discuss_read_registry(*, edition: str | None = None) -> ToolRegistry:
    """Project-pinned tools for IntentRouter's tool-grounded discuss path.

    Project-safe read tools, plus the operational writes in
    ``_DISCUSS_OPERATIONS_TOOLS``.
    ``project_id`` is stripped from schemas/args so the model cannot override the authorised
    project silently. Every tool that acts in one project instead takes the one generic
    ``project`` argument (registry.PROJECT_ARG): another project's id or slug, which
    ``ToolRegistry.invoke`` resolves in the caller's organization and authorizes against the
    caller's own permission IN THAT project. Raw SQL is excluded in every edition because its
    handlers cannot enforce that project boundary. Community also excludes arbitrary URL probes;
    direct Community SQL invocations are separately rejected by the handlers.
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
        if (edition is not None
                and edition.strip().lower() not in _PAID_EFFECT_EDITIONS
                and spec.name in {"query_db", "write_db", "generate_image",
                                  "http_probe", "find_free_port"}):
            continue
        properties = dict((spec.parameters or {}).get("properties") or {})
        properties.pop("project_id", None)
        if spec.org_scope is not True:
            # A tool that acts in one project can act in another the caller may: said in its
            # own schema, so the model knows it from the tool and not from a prompt.
            properties[PROJECT_ARG] = dict(PROJECT_ARG_SCHEMA)
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
                # IDENTITY: the rebuilt spec keeps its permission, or the gate would see none.
                permission=spec.permission,
                inherits_route_permission=spec.inherits_route_permission,
                # PINNED: the identity gate decides the session project, the one this handler
                # acts in — never an args.project_id the handler drops (H1).
                pinned_project=True,
                org_scope=spec.org_scope,
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
                # ...AND WHO MAY PRESS IT. Dropping this would let the chat model run the
                # captain's overwatch itself — the very thing BOSSAPP-759 took away from a
                # sentence.
                operator_only=spec.operator_only,
                # ...AND THE PERMISSION EACH OPERATION NEEDS. Both were dropped here, so the chat
                # registry decided every call on the base key alone: an operation-dependent key
                # (decide's execute, list_org_repositories' refresh, register_repository's
                # registry path) and a second key a route checks were never asked for.
                permission_by_arg=spec.permission_by_arg,
                also_requires=spec.also_requires,
            )
        )
    return reg
