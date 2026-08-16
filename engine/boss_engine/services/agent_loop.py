from __future__ import annotations

import json
import re
from hashlib import sha256
from pathlib import Path
from typing import Any, Callable, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..config import Settings
from .core import create_artifact, create_blocking_decision, emit_event, make_id
from .github_adapter import GitHubAdapter
from .mcp_client import MCPClient
from .mcp_registry import list_mcp_servers
from .receipts import ReceiptService
from .role_governance import RoleGovernanceService
from .claude_cli_adapter import make_chat_adapter
from .openai_adapter import OpenAIAdapter, _usage_from_response  # noqa: F401
from .pdf_render import pdf_page_count, render_markdown_pdf
from .skill_executors import SkillExecutorContext
from .skill_registry import resolve_for_task
from .skill_runtime import SkillRuntimeService


def _slug(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_]+", "_", value or "").strip("_").lower()


def _unique_tool_name(
    base: str,
    occupied: set[str],
    identity: str,
) -> str:
    candidate = base[:64]
    if candidate not in occupied:
        occupied.add(candidate)
        return candidate
    suffix = "_" + sha256(identity.encode("utf-8")).hexdigest()[:8]
    candidate = base[: 64 - len(suffix)] + suffix
    counter = 2
    while candidate in occupied:
        numbered = f"_{counter}"
        candidate = base[: 64 - len(suffix) - len(numbered)] + suffix + numbered
        counter += 1
    occupied.add(candidate)
    return candidate


def _skill_tool_parameters(input_schema: dict[str, Any]) -> dict[str, Any]:
    if input_schema.get("type") == "object":
        return input_schema
    properties: dict[str, Any] = {}
    required: list[str] = []
    supported_types = {
        "array",
        "boolean",
        "integer",
        "number",
        "object",
        "string",
    }
    for raw_name, declaration in input_schema.items():
        name = str(raw_name).rstrip("?")
        optional = str(raw_name).endswith("?")
        if isinstance(declaration, dict):
            property_schema = dict(declaration)
            optional = optional or bool(property_schema.pop("optional", False))
        else:
            declared_type = str(declaration or "string")
            optional = optional or declared_type.endswith("?")
            declared_type = declared_type.rstrip("?")
            property_schema = {
                "type": declared_type if declared_type in supported_types else "string"
            }
        properties[name] = property_schema
        if not optional:
            required.append(name)
    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if required:
        schema["required"] = required
    return schema


def _append_skill_tools(
    schemas: list[dict[str, Any]],
    occupied: set[str],
    resolved_skills: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    skill_map: dict[str, dict[str, Any]] = {}
    for skill in sorted(resolved_skills, key=lambda item: str(item["id"])):
        skill_id = str(skill["id"])
        name = _unique_tool_name(
            "skill_" + _slug(skill_id),
            occupied,
            f"skill:{skill_id}",
        )
        schemas.append(
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": (
                        f"[BOSS Skill · {skill.get('kind', 'unknown')}] "
                        f"{skill.get('description', '')}"
                    ),
                    "parameters": _skill_tool_parameters(
                        skill.get("input_schema") or {}
                    ),
                },
            }
        )
        skill_map[name] = skill
    return skill_map


def _mcp_tool_risk(server: dict[str, Any], tool: dict[str, Any]) -> int:
    tool_name = str(tool.get("name") or "")
    overrides = server.get("tool_risk_overrides") or {}
    annotations = tool.get("annotations") or {}
    candidate = overrides.get(
        tool_name,
        tool.get("risk_tier", annotations.get("risk_tier", server.get("risk_tier", 1))),
    )
    try:
        risk = int(candidate)
    except (TypeError, ValueError):
        risk = int(server.get("risk_tier") or 1)
    return max(0, min(risk, 3))


def _mcp_skill_spec(
    server: dict[str, Any], tool: dict[str, Any]
) -> dict[str, Any]:
    tool_name = str(tool.get("name") or "tool")
    return {
        "id": f"mcp.{server['id']}.{_slug(tool_name)}",
        "kind": "mcp",
        "system": False,
        "load": "eager",
        "risk_tier": _mcp_tool_risk(server, tool),
        "description": (
            f"{server.get('name', 'MCP')} / {tool_name}: "
            f"{tool.get('description', '')}"
        ),
        "mock_mode": False,
        "input_schema": tool.get("inputSchema")
        or {"type": "object", "properties": {}},
        "output_schema": {"type": "object", "properties": {}},
        "cost": {"estimator": "fixed", "category": "tool"},
        "retry": {"max_attempts": 2, "backoff": "exponential"},
        "permissions": {
            "allowed_callers": ["agent_loop"],
            "required_permissions": ["mcp_call"],
        },
        "data_boundary": server.get("data_boundary") or "internal",
        "policy": {"requires_receipt": True},
        "publisher": server.get("publisher") or {},
        "provenance": server.get("provenance") or {},
    }


def _invoke_mcp_skill(
    connection: Connection,
    settings: Settings,
    project_id: str,
    server: dict[str, Any],
    tool: dict[str, Any],
    args: dict[str, Any],
    *,
    run_id: str,
    step: int,
    actor: str,
) -> dict[str, Any]:
    tool_name = str(tool.get("name") or "tool")
    skill_spec = _mcp_skill_spec(server, tool)
    context = SkillExecutorContext(
        settings=settings,
        connection=connection,
        project_id=project_id,
        actor=actor,
    )

    def execute(inputs: dict[str, Any]) -> dict[str, Any]:
        org_id, workspace_id = context.tenant_scope()
        receipts = ReceiptService()
        args_digest = sha256(
            json.dumps(inputs, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()[:20]
        dedupe_key = (
            f"mcp:{run_id}:{step}:{server['id']}:{tool_name}:{args_digest}"
        )
        receipt, created = receipts.create_or_reuse(
            connection,
            org_id,
            workspace_id,
            project_id,
            dedupe_key,
            "mcp.tool.call",
            {
                "server_id": server["id"],
                "server": server["name"],
                "tool": tool_name,
                "data_boundary": server.get("data_boundary") or "internal",
            },
            inputs,
            data_boundary=server.get("data_boundary") or "internal",
        )
        if not created:
            if receipt.get("status") == "succeeded":
                return {
                    "output": receipt.get("result") or {},
                    "receipt_id": receipt["id"],
                    "actual_cost_usd_micros": 0,
                    "deduplicated": True,
                }
            raise RuntimeError(
                f"MCP effect receipt already exists with status {receipt.get('status')}"
            )
        try:
            output = MCPClient(settings).call_tool(server["url"], tool_name, inputs)
        except Exception as exc:
            receipts.mark_failed(
                connection,
                receipt["id"],
                {"type": type(exc).__name__, "message": str(exc)},
            )
            raise
        receipts.mark_succeeded(
            connection,
            receipt["id"],
            f"{server['id']}:{tool_name}",
            output if isinstance(output, dict) else {"value": output},
        )
        return {
            "output": output,
            "receipt_id": receipt["id"],
            "actual_cost_usd_micros": 0,
        }

    return SkillRuntimeService(settings).invoke(
        connection,
        project_id,
        str(skill_spec["id"]),
        "agent_loop",
        args,
        mock=False,
        executor=execute,
        executor_context=context,
        skill_spec=skill_spec,
        actor=actor,
    )

# ============================================================
# Read tools — internal/local data only (no external side effects).
# Each returns a JSON-serializable dict that is fed back to the LLM.
# ============================================================

_SENSITIVE = (".env", ".git", "id_rsa", ".pem", ".key", "secret", "credential")


def _repo_root(settings: Settings) -> Path:
    return Path(settings.agent_repo_root or ".").resolve()


def _safe_path(settings: Settings, rel: str) -> Optional[Path]:
    root = _repo_root(settings)
    try:
        target = (root / rel).resolve()
    except Exception:
        return None
    if root not in target.parents and target != root:
        return None
    lowered = str(target).lower()
    if any(tok in lowered for tok in _SENSITIVE):
        return None
    return target


def _tool_budget_and_costs(connection: Connection, project_id: str, settings: Settings, args: dict) -> dict:
    policy = connection.execute(
        text(
            "SELECT daily_cap_usd_micros FROM budget_policies "
            "WHERE scope_type='project' AND scope_id=:p ORDER BY created_at DESC LIMIT 1"
        ),
        {"p": project_id},
    ).mappings().first()
    spent = connection.execute(
        text(
            "SELECT COALESCE(SUM(COALESCE(actual_usd_micros, estimated_usd_micros)),0) AS s "
            "FROM cost_receipts WHERE project_id=:p"
        ),
        {"p": project_id},
    ).scalar() or 0
    by_model = connection.execute(
        text(
            "SELECT skill_or_model_id AS model, count(*) AS calls, "
            "COALESCE(SUM(COALESCE(actual_usd_micros, estimated_usd_micros)),0) AS micros "
            "FROM cost_receipts WHERE project_id=:p GROUP BY skill_or_model_id ORDER BY micros DESC"
        ),
        {"p": project_id},
    ).mappings().all()
    infra = connection.execute(
        text("SELECT provider, status, count(*) AS n FROM infra_resources WHERE project_id=:p GROUP BY provider, status"),
        {"p": project_id},
    ).mappings().all()
    cap = int(policy["daily_cap_usd_micros"]) if policy else 0
    return {
        "daily_budget_cap_usd": round(cap / 1e6, 2),
        "total_spend_usd": round(int(spent) / 1e6, 4),
        "spend_by_model": [
            {"model": r["model"], "calls": int(r["calls"]), "usd": round(int(r["micros"]) / 1e6, 4)}
            for r in by_model
        ],
        "provisioned_infrastructure": [
            {"provider": r["provider"], "status": r["status"], "count": int(r["n"])} for r in infra
        ]
        or "none provisioned",
        "note": "These are BOSS's real recorded costs. No external cloud-provider bill is connected yet.",
    }


# --------------------------------------------------------------------------------------- #
# Ask the database.
#
# The agent had twelve hand-written tools and still could not answer "why did the last build
# fail", because that answer lives in Postgres — in code_task_runs.state, in jobs, in events —
# and every new question needed a new tool. Twelve becomes twenty and still misses the next
# one. Tools do not scale to open-ended questions; access does.
#
# READ ONLY, enforced three ways: one statement, a SELECT/WITH prefix, and a row cap. The point
# is to answer questions, not to let a language model write to the control plane — every action
# BOSS takes still goes through its governed tools and their approval gates.
# --------------------------------------------------------------------------------------- #

_MUTATING_KEYWORDS = (
    "insert", "update", "delete", "drop", "alter", "create", "truncate", "grant", "revoke",
    "copy", "call", "vacuum", "refresh", "comment", "reindex", "lock",
)
_QUERY_ROW_CAP = 200


def _query_rejection(sql: str) -> Optional[str]:
    """Why this statement must not run, or None. Separate so it is testable on its own."""
    stripped = re.sub(r"--[^\n]*", " ", sql)
    stripped = re.sub(r"/\*.*?\*/", " ", stripped, flags=re.S).strip()
    if not stripped:
        return "empty query"
    # One statement. A trailing semicolon is fine; a second statement is not — chaining is how
    # a read tool quietly becomes a write tool.
    body = stripped.rstrip(";")
    if ";" in body:
        return "one statement at a time"
    first = body.lstrip("( ").split(None, 1)[0].lower() if body.strip() else ""
    if first not in {"select", "with"}:
        return f"read-only: must start with SELECT or WITH, got {first.upper() or '?'}"
    lowered = f" {body.lower()} "
    for word in _MUTATING_KEYWORDS:
        if re.search(rf"\b{word}\b", lowered):
            return f"read-only: {word.upper()} is not allowed"
    return None


def _tool_query_db(connection: Connection, project_id: str, settings: Settings, args: dict) -> dict:
    sql = str(args.get("sql") or "").strip()
    rejection = _query_rejection(sql)
    if rejection:
        return {"error": rejection, "hint": "SELECT only, one statement, e.g. SELECT ... LIMIT 50"}
    try:
        rows = connection.execute(text(sql.rstrip(";"))).mappings().all()
    except Exception as exc:  # noqa: BLE001 — for a bad query the message IS the answer
        return {"error": f"{type(exc).__name__}: {exc}"}
    out = [
        {k: (v if isinstance(v, (str, int, float, bool, type(None))) else str(v))
         for k, v in dict(row).items()}
        for row in rows[:_QUERY_ROW_CAP]
    ]
    return {
        "rows": out,
        "row_count": len(out),
        # Said out loud, so the model never reasons from a silently cut result set.
        "truncated": len(rows) > _QUERY_ROW_CAP,
        "project_id_for_filtering": project_id,
    }


def _tool_describe_schema(connection: Connection, project_id: str, settings: Settings, args: dict) -> dict:
    """What can be queried. Without it the model guesses column names and burns turns on
    errors — the same way a person explores an unfamiliar database."""
    table = str(args.get("table") or "").strip()
    if table:
        columns = connection.execute(
            text("SELECT column_name, data_type FROM information_schema.columns "
                 "WHERE table_name = :t ORDER BY ordinal_position"),
            {"t": table},
        ).mappings().all()
        return {"table": table, "columns": [dict(c) for c in columns]}
    tables = connection.execute(
        text("SELECT table_name FROM information_schema.tables "
             "WHERE table_schema='public' ORDER BY table_name")
    ).scalars().all()
    return {"tables": list(tables)}


def _tool_project_status(connection: Connection, project_id: str, settings: Settings, args: dict) -> dict:
    def scalar(sql: str) -> int:
        return int(connection.execute(text(sql), {"p": project_id}).scalar() or 0)

    recent = connection.execute(
        text(
            "SELECT event_type, count(*) AS n FROM events WHERE project_id=:p "
            "AND created_at > now() - interval '7 days' GROUP BY event_type ORDER BY n DESC LIMIT 8"
        ),
        {"p": project_id},
    ).mappings().all()
    return {
        "open_tasks": scalar("SELECT count(*) FROM tasks WHERE project_id=:p AND status='open'"),
        "pending_decisions": scalar("SELECT count(*) FROM decisions WHERE project_id=:p AND status='pending'"),
        "deadletters": scalar("SELECT count(*) FROM deadletters WHERE project_id=:p AND status='open'"),
        "jobs_succeeded": scalar("SELECT count(*) FROM jobs WHERE project_id=:p AND status='succeeded'"),
        "recent_activity": [{"event": r["event_type"], "count": int(r["n"])} for r in recent],
    }


def _tool_list_repo_files(connection: Connection, project_id: str, settings: Settings, args: dict) -> dict:
    subdir = str(args.get("subdir") or "").strip().lstrip("/")
    base = _safe_path(settings, subdir or ".")
    if base is None or not base.exists():
        return {"error": f"path not allowed or not found: {subdir or '.'}"}
    root = _repo_root(settings)
    skip = {"node_modules", ".git", ".next", ".venv", "__pycache__", "dist", ".local-logs"}
    files: list[str] = []
    for p in sorted(base.rglob("*")):
        if any(part in skip for part in p.parts):
            continue
        if p.is_file() and not any(tok in str(p).lower() for tok in _SENSITIVE):
            files.append(str(p.relative_to(root)))
        if len(files) >= 120:
            break
    return {"root": str(root.name), "subdir": subdir or ".", "files": files, "truncated": len(files) >= 120}


def _tool_read_repo_file(connection: Connection, project_id: str, settings: Settings, args: dict) -> dict:
    rel = str(args.get("path") or "").strip().lstrip("/")
    target = _safe_path(settings, rel)
    if target is None or not target.is_file():
        return {"error": f"file not allowed or not found: {rel}"}
    try:
        content = target.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:  # pragma: no cover
        return {"error": str(exc)}
    limit = 8000
    return {
        "path": rel,
        "content": content[:limit],
        "truncated": len(content) > limit,
        "bytes": len(content),
    }


def _tool_list_apps(connection, project_id, settings, args) -> dict:
    """The apps this project has built, so the agent can resolve "the ApproveIt app" to a run.

    Without this the agent could read repository FILES but had no idea what BOSS had built, so
    a request naming an app by name had nothing to attach to.
    """
    from sqlalchemy import text as _sql

    rows = connection.execute(_sql(
        "SELECT id, title, status, phase, spec->>'owner' AS owner, spec->>'repo' AS repo, "
        "       finalize_ctx->>'root_run_id' AS root, pull_request_url, created_at "
        "FROM code_task_runs WHERE project_id = :p "
        "ORDER BY created_at DESC LIMIT :n"),
        {"p": project_id, "n": int(args.get("limit") or 20)}).mappings().all()
    return {"apps": [{
        "run_id": r["id"], "title": r["title"], "status": r["status"], "phase": r["phase"],
        "repo": f"{r['owner']}/{r['repo']}" if r["owner"] else None,
        "branch": f"boss/code/{r['root'] or r['id']}",
        "pull_request": r["pull_request_url"],
        "created_at": str(r["created_at"]),
    } for r in rows]}


def _tool_inspect_app(connection, project_id, settings, args) -> dict:
    """Report what a built app actually IS: does it have a server, does its UI call one.

    An operator asked why an app forgot everything on reload. Answering needed the built
    bundle and the source tree; the agent could reach neither, so it guessed. This returns
    facts -- whether a server exists, whether the frontend makes API calls, what the preview
    would launch -- so the agent explains instead of speculating.
    """
    from pathlib import Path
    from .code_task_service import load_code_task_run, work_branch_for
    from .preview_service import resolve_app_entry

    run = load_code_task_run(connection, str(args.get("run_id") or ""))
    if run is None:
        return {"error": f"no such run: {args.get('run_id')}"}
    spec = run.get("spec") or {}
    owner, repo = spec.get("owner"), spec.get("repo")
    branch = work_branch_for(run)

    import subprocess
    import tempfile

    from .github_app_adapter import repo_token

    workdir = Path(tempfile.mkdtemp(prefix="boss-inspect-"))
    try:
        token = repo_token(settings, owner, repo)
        clone = subprocess.run(
            ["git", "clone", "--depth", "1", "--branch", branch,
             f"https://x-access-token:{token}@github.com/{owner}/{repo}.git", str(workdir / "r")],
            capture_output=True, text=True, timeout=300)
        if clone.returncode != 0:
            return {"error": f"could not read {owner}/{repo}@{branch}",
                    "detail": clone.stderr[-300:]}
        root = workdir / "r"
        files = [str(f.relative_to(root)) for f in root.rglob("*")
                 if f.is_file() and ".git/" not in str(f)]
        servers = [f for f in files if f.endswith(("server.js", "server.ts", "index.js"))
                   and ("server" in f or "api" in f)]
        sources = [f for f in files if f.endswith((".tsx", ".ts", ".js", ".jsx"))
                   and not f.startswith(("node_modules", "dist"))]
        calls = 0
        for rel in sources[:400]:
            try:
                body = (root / rel).read_text(errors="ignore")
            except OSError:
                continue
            calls += body.count("fetch(") + body.count("axios.")
        try:
            kind, value = resolve_app_entry(root, files)
        except Exception as error:  # noqa: BLE001
            kind, value = "ambiguous", str(error)[:200]
        return {
            "run_id": run["id"], "title": run.get("title"), "repo": f"{owner}/{repo}",
            "branch": branch,
            "preview_would_run": {"kind": kind, "entry": value},
            "has_server_file": bool(servers), "server_files": servers[:5],
            "frontend_api_calls": calls,
            "has_database": any(f.endswith(".sql") or "migrations" in f for f in files),
            "verdict": (
                "No backend: the UI holds state in memory, so anything entered is lost on "
                "reload." if not servers and calls == 0 else
                "Has a server but the UI never calls it." if servers and calls == 0 else
                "Frontend calls an API but no server was found in this branch."
                if calls and not servers else
                "Full stack: a server exists and the frontend calls it."),
        }
    finally:
        import shutil

        shutil.rmtree(workdir, ignore_errors=True)


def _wt_change_existing_app(connection, project_id, settings, args) -> dict:
    """Change an app that already exists, starting from its own code.

    The agent's only write tools created TASK ROWS -- a to-do nothing acts on. Asked to "make
    this save to a database", the best it could do was file a note, which is why asking it to
    fix an app produced nothing useful. This starts real, governed work on the app's branch.
    """
    from .code_task_service import start_followup_task
    from .jobs import JobLeaseService

    run_id = str(args.get("run_id") or "").strip()
    instructions = str(args.get("instructions") or "").strip()
    if not run_id or not instructions:
        return {"error": "run_id and instructions are both required"}
    return start_followup_task(
        connection, parent_run_id=run_id, instructions=instructions,
        jobs=JobLeaseService(settings), title=args.get("title"), actor="boss_agent")


def _tool_diagnose_preview(connection, project_id, settings, args) -> dict:
    """Look at a preview and report what is actually wrong with it.

    Asked why a preview showed "Error: HTTP 404", the agent had nothing to look at. It has no
    way to make an HTTP request and no tool that reports what a preview is running, so it
    answered from imagination: that the preview was "still building", that the operator should
    open a 127.0.0.1 address belonging to a container, and that a tunnel might help. None of
    that was true -- the gateway was returning 200 and the app's own /api calls were 404ing
    because the preview was serving built files with no server behind them.

    A confident wrong answer is worse than "I cannot see that", and the fix is to let it see.
    This fetches the page and the API paths the bundle actually calls, so the difference
    between "not ready", "serving statically with no backend" and "the server crashed" is
    read off the responses rather than guessed.
    """
    import re
    import urllib.error
    import urllib.request

    from sqlalchemy import text as _sql

    from .preview_service import operator_preview_open_url

    preview_id = str(args.get("preview_id") or "").strip()
    if not preview_id:
        return {"error": "preview_id is required (prev_...)"}
    row = connection.execute(_sql(
        "SELECT id, run_id, status, mode, url, failure_kind, error, expires_at "
        "FROM app_previews WHERE id = :id"), {"id": preview_id}).mappings().first()
    if row is None:
        return {"error": f"no such preview: {preview_id}"}

    report: dict = {
        "preview_id": row["id"], "run_id": row["run_id"], "status": row["status"],
        "mode": row["mode"],
        # The address a PERSON can open — /v1/previews/{id}/web/ (absolute when
        # BOSS_PREVIEW_PUBLIC_BASE is set). app_previews.url holds the sandbox-internal
        # one (http://127.0.0.1:43053), reachable from this process and from nowhere a
        # browser runs. Reporting that column verbatim is exactly how an operator was
        # told to open http://127.0.0.1:46181 -- true of the machine, useless to them.
        "open_url": operator_preview_open_url(row["id"]),
        "internal_url": row["url"],
        "failure_kind": row["failure_kind"], "error": (row["error"] or "")[:300],
    }

    # Probed internally because that is what this process can reach; what gets REPORTED is
    # open_url above.
    base = (row["url"] or "").rstrip("/")
    if not base:
        report["verdict"] = "the preview has no URL yet; it has not started serving."
        return report

    def fetch(path: str = "") -> dict:
        target = f"{base}/{path}" if path else base + "/"
        try:
            with urllib.request.urlopen(target, timeout=20) as response:
                body = response.read(200_000)
                return {"status": response.status, "bytes": len(body), "body": body}
        except urllib.error.HTTPError as error:
            return {"status": error.code, "bytes": 0, "body": b""}
        except Exception as error:  # noqa: BLE001
            return {"status": None, "error": str(error)[:200], "bytes": 0, "body": b""}

    page = fetch()
    report["page_http"] = page.get("status")
    report["page_bytes"] = page.get("bytes")

    # Which API paths does the SHIPPED bundle call? Guessing /api/health tells you nothing
    # about an app whose routes are /api/bugs.
    api_paths: list[str] = []
    html = (page.get("body") or b"").decode("utf-8", "ignore")
    for asset in re.findall(r'src="\.?/?(assets/[^"]+\.js)"', html)[:2]:
        bundle = fetch(asset)
        text_body = (bundle.get("body") or b"").decode("utf-8", "ignore")
        api_paths.extend(re.findall(r'"(/api/[a-zA-Z0-9/_-]+)"', text_body))
    api_paths = sorted(set(api_paths))[:6]

    checks = {}
    for path in api_paths or ["/api/health"]:
        checks[path] = fetch(path.lstrip("/")).get("status")
    report["api_calls_in_bundle"] = api_paths
    report["api_responses"] = checks

    live = [code for code in checks.values() if code and 200 <= code < 400]
    if page.get("status") != 200:
        report["verdict"] = (
            f"the page itself returns HTTP {page.get('status')}; the preview is not serving.")
    elif not api_paths:
        report["verdict"] = (
            "the page serves and the bundle makes no API calls at all -- this app keeps its "
            "state in the browser, so nothing it shows will survive a reload.")
    elif live:
        report["verdict"] = "the page and its API both respond; this preview looks healthy."
    else:
        report["verdict"] = (
            "the page serves but every API path the app calls returns "
            f"{sorted({c for c in checks.values()})} -- the frontend is being served without "
            "its server running behind it, so the UI loads and every action fails.")
    return report


def _tool_diagnose_deployment(connection, project_id, settings, args) -> dict:
    """Look at what is actually deployed and whether it works.

    The counterpart to diagnose_preview, and missing for the same reason: the agent could read
    a deployment ROW but never reach the deployed thing, so "is it live?" was answered from a
    status column. A status column says a deploy finished; it does not say the app serves, and
    the health gate behind it is a 200 on `/` -- which a static shell with no backend passes.

    Fetches the app's real address, and the /api paths its own bundle calls, so a deployment
    that is green and useless can be told apart from one that works.
    """
    import re
    import urllib.error
    import urllib.request

    from sqlalchemy import text as _sql

    app_name = str(args.get("app_name") or "").strip()
    where = "d.project_id = :p" + (" AND d.app_name = :a" if app_name else "")
    params = {"p": project_id, **({"a": app_name} if app_name else {})}
    rows = connection.execute(_sql(
        f"""SELECT d.id, d.app_name, d.environment, d.status, d.target_kind, d.target_ref,
                   d.commit_ref, d.failure_reason, d.health_check_results, d.finished_at,
                   t.domains
            FROM deployment_runs d
            LEFT JOIN deployment_targets t ON t.id = d.deployment_target_id
            WHERE {where}
            ORDER BY d.created_at DESC LIMIT :n"""),
        {**params, "n": int(args.get("limit") or 5)}).mappings().all()
    if not rows:
        return {"deployments": [], "verdict":
                f"nothing has been deployed{' for ' + app_name if app_name else ''} in this "
                f"project."}

    def probe(url: str) -> dict:
        try:
            with urllib.request.urlopen(url, timeout=15) as response:
                return {"http": response.status, "body": response.read(120_000)}
        except urllib.error.HTTPError as error:
            return {"http": error.code, "body": b""}
        except Exception as error:  # noqa: BLE001
            return {"http": None, "error": str(error)[:160], "body": b""}

    out = []
    for row in rows:
        entry = {
            "deployment_id": row["id"], "app": row["app_name"],
            "environment": row["environment"], "status": row["status"],
            "target": f"{row['target_kind']}:{row['target_ref']}",
            "commit": (row["commit_ref"] or "")[:12],
            "failure_reason": (row["failure_reason"] or "")[:200] or None,
            "finished_at": str(row["finished_at"]) if row["finished_at"] else None,
        }
        domains = row["domains"] if isinstance(row["domains"], list) else []
        url = None
        for candidate in domains:
            if isinstance(candidate, str) and candidate.startswith("http"):
                url = candidate.rstrip("/")
                break
        if url is None and row["target_kind"] == "ssh" and row["target_ref"]:
            url = f"http://{row['target_ref'].split('@')[-1]}"
        if url:
            entry["url"] = url
            page = probe(url + "/")
            entry["page_http"] = page.get("http")
            html = (page.get("body") or b"").decode("utf-8", "ignore")
            api_paths: list[str] = []
            for asset in re.findall(r'src="\.?/?(assets/[^"]+\.js)"', html)[:2]:
                bundle = probe(f"{url}/{asset}")
                api_paths.extend(re.findall(
                    r'"(/api/[a-zA-Z0-9/_-]+)"',
                    (bundle.get("body") or b"").decode("utf-8", "ignore")))
            api_paths = sorted(set(api_paths))[:5]
            entry["api_calls_in_bundle"] = api_paths
            entry["api_responses"] = {q: probe(url + q).get("http") for q in api_paths}
            live = [c for c in entry["api_responses"].values() if c and 200 <= c < 400]
            if entry["page_http"] != 200:
                entry["verdict"] = f"the app does not serve (HTTP {entry['page_http']})."
            elif api_paths and not live:
                entry["verdict"] = (
                    "serves the frontend but every API path it calls fails -- deployed without "
                    "its server running, so it looks live and does nothing.")
            elif not api_paths:
                entry["verdict"] = "serves; the bundle makes no API calls (nothing persists)."
            else:
                entry["verdict"] = "serves, and its API responds."
        else:
            entry["verdict"] = ("no address recorded for this deployment, so it cannot be "
                                "checked from here.")
        out.append(entry)
    return {"deployments": out}


ToolFn = Callable[[Connection, str, Settings, dict], dict]

TOOLS: list[dict[str, Any]] = [
    {
        "fn": _tool_budget_and_costs,
        "schema": {
            "type": "function",
            "function": {
                "name": "get_budget_and_costs",
                "description": "Get this project's REAL recorded budget cap, total spend, spend broken down by model, and provisioned infrastructure. Use this before proposing anything about cost, budget, or cloud spend.",
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
        },
    },
    {
        "fn": _tool_query_db,
        "schema": {
            "type": "function",
            "function": {
                "name": "query_db",
                "description": (
                    "Run a read-only SQL query against BOSS's own database and get rows back. "
                    "Use it for anything the other tools do not already answer: why a run "
                    "failed, what a job's error was, what changed between two runs, what a "
                    "preview recorded, what is in the decision queue. SELECT or WITH only, one "
                    "statement. Call describe_schema first if unsure of table or column names. "
                    "Filter by project_id where the table has that column."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "sql": {"type": "string", "description": "A single SELECT statement."},
                    },
                    "required": ["sql"],
                    "additionalProperties": False,
                },
            },
        },
    },
    {
        "fn": _tool_describe_schema,
        "schema": {
            "type": "function",
            "function": {
                "name": "describe_schema",
                "description": (
                    "List BOSS's tables, or the columns of one table. Call before writing a "
                    "query so the SQL matches the real schema instead of a guess."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "table": {"type": "string", "description": "Optional table name."},
                    },
                    "required": [],
                    "additionalProperties": False,
                },
            },
        },
    },
    {
        "fn": _tool_project_status,
        "schema": {
            "type": "function",
            "function": {
                "name": "get_project_status",
                "description": "Get the project's real current state: open tasks, pending decisions, deadletters, jobs completed, and recent activity. Use this to ground plans in what is actually happening.",
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
        },
    },
    {
        "fn": _tool_diagnose_deployment,
        "schema": {
            "type": "function",
            "function": {
                "name": "diagnose_deployment",
                "description": (
                    "Look at what is actually deployed for an app and whether it works: the "
                    "recorded deployment, its address, and what the app's own API paths "
                    "return when called. USE THIS for any question about a live or published "
                    "app. A deployment row saying `succeeded` only means the deploy finished; "
                    "the health gate behind it is a 200 on `/`, which a frontend with no "
                    "backend passes. Do not answer from the status column."),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "app_name": {"type": "string", "description": "omit for all apps"},
                        "limit": {"type": "integer"},
                    },
                    "required": [],
                },
            },
        },
    },
    {
        "fn": _tool_diagnose_preview,
        "schema": {
            "type": "function",
            "function": {
                "name": "diagnose_preview",
                "description": (
                    "Look at a running preview and report what is actually wrong: whether the "
                    "page serves, which /api paths its bundle calls, and what each of those "
                    "returns. USE THIS whenever someone reports anything about a preview -- a "
                    "404, a blank page, an empty dashboard, 'it resets when I reload'. Those "
                    "symptoms have different causes and only the responses tell them apart. "
                    "Never speculate about a preview you have not looked at."),
                "parameters": {
                    "type": "object",
                    "properties": {"preview_id": {"type": "string",
                                                  "description": "prev_... from the URL"}},
                    "required": ["preview_id"],
                },
            },
        },
    },
    {
        "fn": _tool_list_apps,
        "schema": {
            "type": "function",
            "function": {
                "name": "list_apps",
                "description": (
                    "List the apps this project has BUILT, with each one's run id, repo, branch "
                    "and status. Use this FIRST whenever someone names an app ('the ApproveIt "
                    "app', 'the expense tool') so you can attach their request to a real run. "
                    "Reading repository files tells you what code exists; this tells you what "
                    "BOSS built and where it lives."),
                "parameters": {
                    "type": "object",
                    "properties": {"limit": {"type": "integer"}},
                    "required": [],
                },
            },
        },
    },
    {
        "fn": _tool_inspect_app,
        "schema": {
            "type": "function",
            "function": {
                "name": "inspect_app",
                "description": (
                    "Report what a built app actually IS: whether it has a server, whether its "
                    "frontend calls an API, whether it has a database, and what a preview would "
                    "launch for it. Use this to explain behaviour like 'it resets when I close "
                    "the tab' or 'the preview shows an empty dashboard' instead of guessing -- "
                    "those symptoms have different causes and this distinguishes them."),
                "parameters": {
                    "type": "object",
                    "properties": {"run_id": {"type": "string",
                                              "description": "run id from list_apps"}},
                    "required": ["run_id"],
                },
            },
        },
    },
    {
        "fn": _tool_list_repo_files,
        "schema": {
            "type": "function",
            "function": {
                "name": "list_repo_files",
                "description": "List real files in the codebase under an optional subdirectory. Use to discover what code exists before planning a code change.",
                "parameters": {
                    "type": "object",
                    "properties": {"subdir": {"type": "string", "description": "Optional subdirectory, e.g. 'apps/web'"}},
                    "required": [],
                },
            },
        },
    },
    {
        "fn": _tool_read_repo_file,
        "schema": {
            "type": "function",
            "function": {
                "name": "read_repo_file",
                "description": "Read the real contents of a file in the codebase. Use to ground a code plan in the actual implementation.",
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string", "description": "Repo-relative file path, e.g. 'README.md'"}},
                    "required": ["path"],
                },
            },
        },
    },
]

_TOOL_FNS: dict[str, ToolFn] = {t["schema"]["function"]["name"]: t["fn"] for t in TOOLS}
_TOOL_SCHEMAS: list[dict] = [t["schema"] for t in TOOLS]


def _dispatch(connection: Connection, project_id: str, settings: Settings, name: str, args: dict) -> dict:
    fn = _TOOL_FNS.get(name)
    if fn is None:
        return {"error": f"unknown tool: {name}"}
    # SAVEPOINT so a failing tool query rolls back just itself and cannot poison
    # the outer transaction that owns the whole plan-generation effect.
    nested = connection.begin_nested()
    try:
        result = fn(connection, project_id, settings, args or {})
        nested.commit()
        return result
    except Exception as exc:  # pragma: no cover
        nested.rollback()
        return {"error": f"{type(exc).__name__}: {exc}"}


# ============================================================
# Write tools — real effects, gated by risk tier. Low-risk run inline; high-risk
# are deferred to a Decision that executes them on human approval.
# ============================================================


def _create_task(
    connection: Connection,
    project_id: str,
    title: str,
    description: Optional[str],
    priority: str,
    risk_tier: int,
    parent_task_id: Optional[str] = None,
) -> dict:
    tid = make_id("tsk")
    connection.execute(
        text(
            """
            INSERT INTO tasks (id, project_id, title, description, priority, risk_tier, parent_task_id, dedupe_key)
            VALUES (:id, :p, :title, :desc, :prio, :risk, :parent, :dedupe)
            """
        ),
        {
            "id": tid,
            "p": project_id,
            "title": title[:200],
            "desc": description,
            "prio": priority,
            "risk": int(risk_tier),
            "parent": parent_task_id,
            "dedupe": tid,
        },
    )
    emit_event(
        connection, project_id, "task.created", "agent_loop", "task", tid,
        {"via": "agent_tool", "parent_task_id": parent_task_id},
    )
    return {"task_id": tid, "status": "created", "title": title}


def _wt_create_followup_task(connection, project_id, settings, args) -> dict:
    return _create_task(
        connection, project_id,
        str(args.get("title") or "Follow-up task"),
        args.get("description"),
        str(args.get("priority") or "p2"),
        1,
    )


def _wt_open_incident(connection, project_id, settings, args) -> dict:
    return _create_task(
        connection, project_id,
        "INCIDENT: " + str(args.get("title") or "untitled"),
        args.get("description"),
        "p0",
        2,
    )


def _wt_publish_document(connection, project_id, settings, args) -> dict:
    """Render a written markdown document to PDF and publish it to a GitHub PR
    (commits both the .md and the .pdf), so a human can review/verify the output.
    HIGH RISK — deferred to a human decision and only runs on approval."""
    title = str(args.get("title") or "BOSS Document").strip()
    markdown = str(args.get("markdown") or "").strip()
    subtitle = args.get("subtitle")
    task_id = args.get("task_id")
    if not markdown:
        return {"error": "markdown is required to publish a document"}
    if len(markdown) < 200:
        return {"error": "document markdown is too short to publish (write the full document first)"}

    subdir = str(args.get("repo_subdir") or "docs/prd").strip("/")
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", title).strip("-").lower()[:50] or "document"
    short = make_id("doc").split("_")[-1][:8]
    branch = f"boss/{slug}-{short}"
    md_path = f"{subdir}/{slug}.md"
    pdf_path = f"{subdir}/{slug}.pdf"

    try:
        pdf_bytes = render_markdown_pdf(markdown, title=title, subtitle=subtitle)
    except Exception as exc:
        return {"error": f"PDF render failed: {type(exc).__name__}: {exc}"}

    try:
        gh = GitHubAdapter(settings)
        gh.ensure_branch(branch)
        gh.upsert_file(branch, md_path, markdown, f"BOSS: add {md_path}")
        gh.upsert_binary_file(branch, pdf_path, pdf_bytes, f"BOSS: add {pdf_path}")
        body = (
            f"{subtitle or ''}\n\n"
            f"This PR was produced by a BOSS agent and approved by a human.\n\n"
            f"- 📄 Markdown: `{md_path}` (renders inline on GitHub)\n"
            f"- 📑 PDF: `{pdf_path}` ({pdf_page_count(pdf_bytes)} pages, {len(pdf_bytes)} bytes)\n\n"
            f"---\n\n{markdown[:8000]}"
        )
        pr = gh.create_pull_request(branch, title, body)
    except Exception as exc:
        return {"error": f"GitHub publish failed: {type(exc).__name__}: {exc}"}

    artifact_id = create_artifact(
        connection, settings, project_id, task_id, "published_document", title,
        {
            "pull_request_url": pr.url,
            "pull_request_number": pr.number,
            "branch": branch,
            "md_path": md_path,
            "pdf_path": pdf_path,
            "pages": pdf_page_count(pdf_bytes),
            "bytes": len(pdf_bytes),
            "subtitle": subtitle,
            "output_text": markdown,
        },
    )
    emit_event(
        connection, project_id, "document.published", "agent_loop", "artifact", artifact_id,
        {"pull_request_url": pr.url, "pull_request_number": pr.number, "task_id": task_id},
    )
    return {
        "status": "published",
        "pull_request_url": pr.url,
        "pull_request_number": pr.number,
        "branch": branch,
        "md_path": md_path,
        "pdf_path": pdf_path,
        "pages": pdf_page_count(pdf_bytes),
        "artifact_id": artifact_id,
    }


WRITE_TOOLS: list[dict[str, Any]] = [
    {
        "fn": _wt_change_existing_app,
        "risk_tier": 1,
        "schema": {
            "type": "function",
            "function": {
                "name": "change_existing_app",
                "description": (
                    "Start real, governed work to CHANGE an app that already exists, building "
                    "on the code it already has. Use this -- not create_followup_task -- "
                    "whenever someone asks for a fix or an addition to an app that has been "
                    "built ('make it save to a database', 'the server is broken', 'add a "
                    "filter'). create_followup_task only files a note that nobody acts on. "
                    "This starts from the app's own branch, so nothing is rebuilt; get the "
                    "run_id from list_apps. Say what should change and why, in the "
                    "instructions -- they are handed to a coding agent."),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "run_id": {"type": "string",
                                   "description": "the app to change, from list_apps"},
                        "instructions": {"type": "string",
                                         "description": "the change to make, in detail"},
                        "title": {"type": "string"},
                    },
                    "required": ["run_id", "instructions"],
                },
            },
        },
    },
    {
        "fn": _wt_create_followup_task,
        "risk_tier": 1,
        "schema": {
            "type": "function",
            "function": {
                "name": "create_followup_task",
                "description": ("Record a to-do in BOSS for a concrete next step. This only FILES an item -- no code is written and nothing runs. To actually change an app that exists, use change_existing_app instead; to start a new product, use a meeting. Reach for this only when the next step is genuinely for a person to do."),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "description": {"type": "string"},
                        "priority": {"type": "string", "enum": ["p0", "p1", "p2", "p3"]},
                    },
                    "required": ["title"],
                },
            },
        },
    },
    {
        "fn": _wt_open_incident,
        "risk_tier": 2,
        "schema": {
            "type": "function",
            "function": {
                "name": "open_incident",
                "description": "Open a P0 incident. HIGH RISK — this requires human approval and will be deferred to a decision before it runs.",
                "parameters": {
                    "type": "object",
                    "properties": {"title": {"type": "string"}, "description": {"type": "string"}},
                    "required": ["title"],
                },
            },
        },
    },
    {
        "fn": _wt_publish_document,
        "risk_tier": 2,
        "schema": {
            "type": "function",
            "function": {
                "name": "publish_document",
                "description": (
                    "Publish a finished written document (e.g. a PRD/spec) to a GitHub "
                    "pull request as BOTH markdown and a rendered PDF, so a human can "
                    "review and verify it. Pass the COMPLETE document as `markdown`. "
                    "HIGH RISK — deferred for human approval; the PR is created on approval."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string", "description": "Document title (also the PR title)"},
                        "markdown": {"type": "string", "description": "The COMPLETE document body in markdown"},
                        "subtitle": {"type": "string", "description": "Optional subtitle / source context line"},
                        "repo_subdir": {"type": "string", "description": "Repo folder to commit into (default 'docs/prd')"},
                    },
                    "required": ["title", "markdown"],
                },
            },
        },
    },
]
_WRITE_FNS: dict[str, tuple[Any, int]] = {
    t["schema"]["function"]["name"]: (t["fn"], t["risk_tier"]) for t in WRITE_TOOLS
}
_WRITE_SCHEMAS: list[dict] = [t["schema"] for t in WRITE_TOOLS]


def describe_native_tools() -> list[dict]:
    """Introspect the agent's built-in tool catalog for the Capabilities UI —
    so agents and humans can see exactly what every native tool does and which
    require approval."""
    catalog: list[dict] = []

    def _params(fn_schema: dict) -> list[dict]:
        params = fn_schema.get("parameters") or {}
        props = params.get("properties") or {}
        required = set(params.get("required") or [])
        return [
            {
                "name": k,
                "type": (v or {}).get("type", "any"),
                "required": k in required,
                "description": (v or {}).get("description", ""),
            }
            for k, v in props.items()
        ]

    for t in TOOLS:
        f = t["schema"]["function"]
        catalog.append({
            "name": f["name"], "kind": "read", "risk_tier": 0,
            "description": f.get("description", ""), "parameters": _params(f),
        })
    for t in WRITE_TOOLS:
        f = t["schema"]["function"]
        catalog.append({
            "name": f["name"], "kind": "write", "risk_tier": int(t["risk_tier"]),
            "description": f.get("description", ""), "parameters": _params(f),
        })
    return catalog


def execute_agent_tool(
    connection: Connection,
    project_id: str,
    settings: Settings,
    tool: str,
    args: dict,
    actor: str = "system",
    task_id: Optional[str] = None,
) -> dict:
    """Run a write tool's effect (used inline for low-risk and by decision
    approval for deferred high-risk calls)."""
    entry = _WRITE_FNS.get(tool)
    if entry is None:
        return {"error": f"unknown write tool: {tool}"}
    fn, _ = entry
    # Make the owning task available to write tools (e.g. so a published document
    # artifact links to the right task) without the model having to supply it.
    if task_id and isinstance(args, dict) and not args.get("task_id"):
        args = {**args, "task_id": task_id}
    nested = connection.begin_nested()
    try:
        result = fn(connection, project_id, settings, args or {})
        nested.commit()
    except Exception as exc:  # pragma: no cover
        nested.rollback()
        return {"error": f"{type(exc).__name__}: {exc}"}
    emit_event(connection, project_id, "agent.tool_executed", actor, "task", project_id, {"tool": tool})
    return result


def _zero_usage() -> dict:
    return {"model": None, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}


def _save_run(connection, run_id, project_id, task_id, status, messages, transcript, usage, finalize_ctx, *, step=0, pending=None, output_text=None) -> None:
    connection.execute(
        text(
            """
            INSERT INTO agent_runs (id, project_id, task_id, status, messages, transcript, usage, finalize_ctx, step, pending, output_text)
            VALUES (:id, :p, :t, :status, CAST(:messages AS JSONB), CAST(:transcript AS JSONB),
                    CAST(:usage AS JSONB), CAST(:fctx AS JSONB), :step, CAST(:pending AS JSONB), :output)
            ON CONFLICT (id) DO UPDATE SET
              status = EXCLUDED.status, messages = EXCLUDED.messages, transcript = EXCLUDED.transcript,
              usage = EXCLUDED.usage, finalize_ctx = EXCLUDED.finalize_ctx, step = EXCLUDED.step,
              pending = EXCLUDED.pending, output_text = EXCLUDED.output_text, updated_at = now()
            """
        ),
        {
            "id": run_id, "p": project_id, "t": task_id, "status": status,
            "messages": json.dumps(messages), "transcript": json.dumps(transcript),
            "usage": json.dumps(usage), "fctx": json.dumps(finalize_ctx or {}), "step": int(step),
            "pending": json.dumps(pending) if pending is not None else None, "output": output_text,
        },
    )


def load_agent_run(connection, run_id) -> Optional[dict]:
    row = connection.execute(
        text(
            "SELECT id, project_id, task_id, status, messages, transcript, usage, finalize_ctx, step, pending, output_text "
            "FROM agent_runs WHERE id = :id"
        ),
        {"id": run_id},
    ).mappings().first()
    return dict(row) if row else None


def _skill_resolution_for_run(
    connection: Connection,
    settings: Settings,
    run: dict[str, Any],
) -> dict[str, Any]:
    finalize_ctx = run.get("finalize_ctx") or {}
    module_descriptor = finalize_ctx.get("module_descriptor") or {}
    return resolve_for_task(
        connection,
        settings,
        str(run["project_id"]),
        {
            "job_kind": finalize_ctx.get("job_kind"),
            "agent_module_id": (
                finalize_ctx.get("module_id") or module_descriptor.get("id")
            ),
            "task_id": run.get("task_id"),
            "skill_caller": finalize_ctx.get("skill_caller"),
        },
    )


def _invoke_agent_skill(
    connection: Connection,
    settings: Settings,
    project_id: str,
    skill_id: str,
    caller: str,
    args: dict[str, Any],
    *,
    actor: str,
    skill_spec: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    return SkillRuntimeService(settings).invoke(
        connection,
        project_id,
        skill_id,
        caller,
        args,
        mock=False,
        executor_context=SkillExecutorContext(
            settings=settings,
            connection=connection,
            project_id=project_id,
            actor=actor,
        ),
        skill_spec=skill_spec,
        actor=actor,
    )


def run_agent_loop(
    connection: Connection,
    settings: Settings,
    project_id: str,
    system_prompt: Optional[str],
    user_prompt: str,
    *,
    task_id: Optional[str] = None,
    max_steps: Optional[int] = None,
    finalize_ctx: Optional[dict] = None,
) -> dict:
    """Start a durable agent run and drive it until it completes or suspends for
    a human approval. State is persisted to agent_runs after every step so the
    run survives a crash and can be resumed."""
    run_id = make_id("arun")
    messages: list[dict] = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": user_prompt})
    _save_run(connection, run_id, project_id, task_id, "running", messages, [], _zero_usage(), finalize_ctx or {}, step=0)
    emit_event(connection, project_id, "agent.run_started", "agent_loop", "agent_run", run_id, {"task_id": task_id})
    return _drive_run(connection, settings, run_id, max_steps)


def resume_agent_run(connection: Connection, settings: Settings, run_id: str) -> dict:
    """Resume a suspended run: execute the approved deferred tool, feed its real
    result back into the conversation, and continue the loop."""
    run = load_agent_run(connection, run_id)
    if run is None:
        return {"status": "not_found", "run_id": run_id}
    if run["status"] != "awaiting_approval":
        return {"status": run["status"], "run_id": run_id}
    project_id = run["project_id"]
    pending = run["pending"] or {}
    # Governance invariant: deferred high-risk tools only execute after the linked
    # Decision is approved. The HTTP resume endpoint is worker-facing and historically
    # had no actor check — without this gate, anyone who knew a suspended run_id could
    # POST /v1/agent-runs/{id}/resume and execute the pending tool as if approved.
    decision_id = str(pending.get("decision_id") or "").strip()
    if not decision_id:
        return {
            "status": "denied",
            "run_id": run_id,
            "reason": "missing approval decision for suspended run",
        }
    decision = connection.execute(
        text(
            "SELECT status FROM decisions WHERE id = :id AND project_id = :project_id"
        ),
        {"id": decision_id, "project_id": project_id},
    ).mappings().first()
    if decision is None or decision["status"] != "approved":
        return {
            "status": "denied",
            "run_id": run_id,
            "decision_id": decision_id,
            "reason": (
                "decision not approved"
                if decision is not None
                else "approval decision not found"
            ),
            "decision_status": None if decision is None else decision["status"],
        }
    if pending.get("kind") == "mcp":
        server = next(
            (
                item
                for item in list_mcp_servers(
                    connection, project_id, enabled_only=True
                )
                if item["id"] == pending.get("server_id")
            ),
            None,
        )
        tool = None
        if server is not None:
            try:
                tool = next(
                    (
                        item
                        for item in MCPClient(settings).list_tools(server["url"])
                        if item.get("name") == pending.get("tool_name")
                    ),
                    None,
                )
            except Exception:
                tool = None
        if server is None or tool is None:
            result = {
                "status": "denied",
                "reason": "MCP server or tool is no longer enabled and available",
            }
        else:
            result = _invoke_mcp_skill(
                connection,
                settings,
                project_id,
                server,
                tool,
                pending.get("args") or {},
                run_id=run_id,
                step=int(pending.get("step") or run.get("step") or 0),
                actor="decision_approval",
            )
            emit_event(
                connection,
                project_id,
                "agent.mcp_invoked",
                "decision_approval",
                "agent_run",
                run_id,
                {
                    "server": server["name"],
                    "tool": tool["name"],
                    "approved": True,
                },
            )
    elif pending.get("kind") == "skill":
        skill_id = str(pending.get("skill_id") or "")
        resolution = _skill_resolution_for_run(connection, settings, run)
        still_resolved = next(
            (
                skill
                for skill in resolution["eager"] + resolution["on_demand"]
                if skill["id"] == skill_id
            ),
            None,
        )
        if still_resolved is None:
            scope = None
            if resolution.get("role_key"):
                scope = RoleGovernanceService(settings).check_scope(
                    connection,
                    project_id=project_id,
                    role_key=str(resolution["role_key"]),
                    action="skill.invoke",
                    skill_id=skill_id,
                    actor="decision_approval",
                    evidence_ref=run_id,
                )
            result = {
                "status": "denied",
                "reason": (
                    (scope or {}).get("reason")
                    if scope and not scope.get("allowed")
                    else "skill is no longer available for this project/task"
                ),
                "skill_id": skill_id,
            }
        else:
            result = _invoke_agent_skill(
                connection,
                settings,
                project_id,
                skill_id,
                str(pending.get("caller") or resolution.get("role_key") or "agent_loop"),
                pending.get("args") or {},
                actor="decision_approval",
                skill_spec=still_resolved,
            )
            emit_event(
                connection,
                project_id,
                "agent.skill_invoked",
                "decision_approval",
                "agent_run",
                run_id,
                {"skill_id": skill_id, "approved": True},
            )
    else:
        result = execute_agent_tool(
            connection, project_id, settings, str(pending.get("tool")), pending.get("args") or {},
            "decision_approval", task_id=run.get("task_id"),
        )
    messages = run["messages"]
    messages.append(
        {
            "role": "user",
            "content": (
                f"APPROVED & EXECUTED: a human approved the deferred action "
                f"'{pending.get('tool')}'. Result: {json.dumps(result)[:2000]}. "
                "Continue and finalize your plan, incorporating this outcome."
            ),
        }
    )
    transcript = run["transcript"]
    transcript.append(
        {
            "step": int(run["step"] or 0),
            "tool": pending.get("tool"),
            "args": pending.get("args"),
            "result_preview": json.dumps(result)[:240],
            "deferred": False,
            "resumed": True,
        }
    )
    _save_run(
        connection, run_id, project_id, run["task_id"], "running", messages, transcript,
        run["usage"] or _zero_usage(), run["finalize_ctx"] or {}, step=int(run["step"] or 0), pending=None,
    )
    emit_event(connection, project_id, "agent.run_resumed", "decision_approval", "agent_run", run_id, {"tool": pending.get("tool")})
    return _drive_run(connection, settings, run_id, None)


def _drive_run(connection: Connection, settings: Settings, run_id: str, max_steps: Optional[int]) -> dict:
    run = load_agent_run(connection, run_id)
    project_id = run["project_id"]
    task_id = run["task_id"]
    messages = run["messages"]
    transcript = run["transcript"]
    total = run["usage"] or _zero_usage()
    finalize_ctx = run["finalize_ctx"] or {}
    step0 = int(run["step"] or 0)

    adapter = make_chat_adapter(settings)
    mcp = MCPClient(settings)
    schemas = list(_TOOL_SCHEMAS) + list(_WRITE_SCHEMAS)
    occupied = {
        str(schema["function"]["name"])
        for schema in schemas
    }
    mcp_map: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
    for server in list_mcp_servers(connection, project_id, enabled_only=True):
        try:
            server_tools = mcp.list_tools(server["url"])
        except Exception as exc:
            emit_event(
                connection,
                project_id,
                "agent.mcp_discovery_failed",
                "agent_loop",
                "mcp_server",
                str(server["id"]),
                {"server": server["name"], "error": str(exc)},
            )
            continue
        for t in server_tools:
            tool_name = str(t.get("name") or "tool")
            ns = _unique_tool_name(
                "mcp_" + _slug(server["name"]) + "_" + _slug(tool_name),
                occupied,
                f"mcp:{server['id']}:{tool_name}",
            )
            schemas.append(
                {
                    "type": "function",
                    "function": {
                        "name": ns,
                        "description": f"[MCP · {server['name']}] {t.get('description', '')}",
                        "parameters": t.get("inputSchema") or {"type": "object", "properties": {}},
                    },
                }
            )
            mcp_map[ns] = (server, t)

    skill_resolution = _skill_resolution_for_run(connection, settings, run)
    skill_map = _append_skill_tools(
        schemas,
        occupied,
        skill_resolution["eager"],
    )
    on_demand_loader: Optional[str] = None
    if skill_resolution["on_demand"]:
        on_demand_loader = _unique_tool_name(
            "load_on_demand_skill", occupied, "skill:on_demand:loader"
        )
        discoverable = [
            {
                "id": str(skill["id"]),
                "description": str(skill.get("description") or ""),
            }
            for skill in skill_resolution["on_demand"]
        ]
        schemas.append(
            {
                "type": "function",
                "function": {
                    "name": on_demand_loader,
                    "description": (
                        "Attach one on-demand BOSS skill for the remainder of this run. "
                        f"Available skills: {json.dumps(discoverable)}"
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "skill_id": {
                                "type": "string",
                                "enum": [item["id"] for item in discoverable],
                            }
                        },
                        "required": ["skill_id"],
                        "additionalProperties": False,
                    },
                },
            }
        )

    steps_cap = int(max_steps or settings.agent_max_steps)
    threshold = int(settings.agent_autonomous_risk_max)

    def add_usage(u: dict) -> None:
        total["model"] = u.get("model")
        for k in ("prompt_tokens", "completion_tokens", "total_tokens"):
            total[k] = int(total.get(k) or 0) + int(u.get(k) or 0)
        # carry exact per-call cost (e.g. Claude CLI total_cost_usd) when present
        if u.get("cost_usd_micros") is not None:
            total["cost_usd_micros"] = int(total.get("cost_usd_micros") or 0) + int(u["cost_usd_micros"])

    final_text: Optional[str] = None
    last_step = step0
    for i in range(steps_cap):
        step = step0 + i
        last_step = step
        message, usage = adapter.create_chat_completion(messages, tools=schemas)
        add_usage(usage)
        messages.append(message["raw"])
        tool_calls = message.get("tool_calls")
        if not tool_calls:
            final_text = message.get("content") or ""
            break

        deferred: Optional[dict] = None
        thought = (message.get("content") or "").strip()
        for tc_idx, tc in enumerate(tool_calls):
            try:
                args = json.loads(tc.get("arguments") or "{}")
            except Exception:
                args = {}
            name = tc["name"]
            is_deferred = False
            if deferred is not None:
                result = {"status": "skipped", "note": "a prior action this turn awaits approval"}
            elif on_demand_loader is not None and name == on_demand_loader:
                requested_id = str(args.get("skill_id") or "")
                current_resolution = _skill_resolution_for_run(
                    connection, settings, run
                )
                skill = next(
                    (
                        item
                        for item in current_resolution["on_demand"]
                        if item["id"] == requested_id
                    ),
                    None,
                )
                if skill is None:
                    scope = None
                    if current_resolution.get("role_key") and requested_id:
                        scope = RoleGovernanceService(settings).check_scope(
                            connection,
                            project_id=project_id,
                            role_key=str(current_resolution["role_key"]),
                            action="skill.invoke",
                            skill_id=requested_id,
                            actor="agent_loop",
                            evidence_ref=run_id,
                        )
                    result = {
                        "status": "denied",
                        "skill_id": requested_id,
                        "reason": (
                            (scope or {}).get("reason")
                            if scope and not scope.get("allowed")
                            else None
                        )
                        or "skill is not available on demand for this task",
                    }
                else:
                    loaded = _append_skill_tools(schemas, occupied, [skill])
                    skill_map.update(loaded)
                    result = {
                        "status": "loaded",
                        "skill_id": requested_id,
                        "tool_name": next(iter(loaded)),
                        "available_for_remainder_of_run": True,
                    }
                    emit_event(
                        connection,
                        project_id,
                        "agent.skill_lazy_loaded",
                        "agent_loop",
                        "agent_run",
                        run_id,
                        {"skill_id": requested_id, "step": step},
                    )
            elif name in mcp_map:
                server, tool = mcp_map[name]
                tool_name = str(tool.get("name") or "tool")
                risk = _mcp_tool_risk(server, tool)
                if risk > threshold:
                    decision_id = create_blocking_decision(
                        connection,
                        project_id,
                        task_id,
                        f"Approve MCP tool: {server['name']} / {tool_name}",
                        {
                            "action": "resume_agent_run",
                            "run_id": run_id,
                            "tool": name,
                            "kind": "mcp",
                            "server_id": server["id"],
                            "tool_name": tool_name,
                            "args": args,
                            "task_id": task_id,
                        },
                        {},
                        "agent_mcp_requires_approval",
                        risk_tier=risk,
                    )
                    result = {
                        "status": "deferred_for_approval",
                        "decision_id": decision_id,
                        "message": (
                            f"MCP tool '{tool_name}' (risk tier {risk}) requires "
                            "human approval."
                        ),
                    }
                    is_deferred = True
                    deferred = {
                        "kind": "mcp",
                        "tool": name,
                        "server_id": server["id"],
                        "tool_name": tool_name,
                        "args": args,
                        "decision_id": decision_id,
                        "step": step,
                    }
                    emit_event(
                        connection,
                        project_id,
                        "agent.mcp_deferred",
                        "agent_loop",
                        "decision",
                        decision_id,
                        {
                            "server": server["name"],
                            "tool": tool_name,
                            "risk_tier": risk,
                            "step": step,
                        },
                    )
                else:
                    result = _invoke_mcp_skill(
                        connection,
                        settings,
                        project_id,
                        server,
                        tool,
                        args,
                        run_id=run_id,
                        step=step,
                        actor="agent_loop",
                    )
                    emit_event(
                        connection,
                        project_id,
                        "agent.mcp_invoked",
                        "agent_loop",
                        "agent_run",
                        run_id,
                        {
                            "server": server["name"],
                            "tool": tool_name,
                            "risk_tier": risk,
                            "step": step,
                        },
                    )
            elif name in skill_map:
                skill = skill_map[name]
                skill_id = str(skill["id"])
                current_resolution = _skill_resolution_for_run(
                    connection, settings, run
                )
                current_skill = next(
                    (
                        item
                        for item in current_resolution["eager"]
                        + current_resolution["on_demand"]
                        if item["id"] == skill_id
                    ),
                    None,
                )
                if current_skill is None:
                    scope = None
                    if current_resolution.get("role_key"):
                        scope = RoleGovernanceService(settings).check_scope(
                            connection,
                            project_id=project_id,
                            role_key=str(current_resolution["role_key"]),
                            action="skill.invoke",
                            skill_id=skill_id,
                            actor="agent_loop",
                            evidence_ref=run_id,
                        )
                    result = {
                        "status": "denied",
                        "skill_id": skill_id,
                        "reason": (
                            (scope or {}).get("reason")
                            if scope and not scope.get("allowed")
                            else None
                        )
                        or "skill is no longer enabled for this task",
                    }
                    current_skill = None
                if current_skill is None:
                    step_obj = {
                        "step": step,
                        "tool": name,
                        "args": args,
                        "result_preview": json.dumps(result)[:240],
                        "deferred": False,
                    }
                    if tc_idx == 0 and thought:
                        step_obj["thought"] = thought
                    transcript.append(step_obj)
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc["id"],
                            "content": json.dumps(result)[:16000],
                        }
                    )
                    continue
                skill = current_skill
                risk = int(skill.get("risk_tier") or 0)
                caller = str(
                    skill.get("caller")
                    or skill.get("role_key")
                    or skill_resolution.get("role_key")
                    or "agent_loop"
                )
                if risk > threshold:
                    decision_id = create_blocking_decision(
                        connection,
                        project_id,
                        task_id,
                        f"Approve agent skill: {skill_id}",
                        {
                            "action": "resume_agent_run",
                            "run_id": run_id,
                            "tool": name,
                            "kind": "skill",
                            "skill_id": skill_id,
                            "caller": caller,
                            "args": args,
                            "task_id": task_id,
                        },
                        {},
                        "agent_skill_requires_approval",
                        risk_tier=risk,
                    )
                    result = {
                        "status": "deferred_for_approval",
                        "decision_id": decision_id,
                        "message": (
                            f"'{skill_id}' (risk tier {risk}) requires human "
                            "approval; the run is paused and resumes on approval."
                        ),
                    }
                    is_deferred = True
                    deferred = {
                        "kind": "skill",
                        "tool": name,
                        "skill_id": skill_id,
                        "caller": caller,
                        "args": args,
                        "decision_id": decision_id,
                    }
                    emit_event(
                        connection,
                        project_id,
                        "agent.skill_deferred",
                        "agent_loop",
                        "decision",
                        decision_id,
                        {"skill_id": skill_id, "risk_tier": risk, "step": step},
                    )
                else:
                    result = _invoke_agent_skill(
                        connection,
                        settings,
                        project_id,
                        skill_id,
                        caller,
                        args,
                        actor="agent_loop",
                        skill_spec=skill,
                    )
                    emit_event(
                        connection,
                        project_id,
                        "agent.skill_invoked",
                        "agent_loop",
                        "agent_run",
                        run_id,
                        {"skill_id": skill_id, "step": step},
                    )
            elif name in _WRITE_FNS:
                _, risk = _WRITE_FNS[name]
                if risk > threshold:
                    decision_id = create_blocking_decision(
                        connection, project_id, task_id,
                        f"Approve agent action: {name}",
                        {"action": "resume_agent_run", "run_id": run_id, "tool": name, "args": args, "task_id": task_id},
                        {}, "agent_tool_requires_approval", risk_tier=risk,
                    )
                    result = {"status": "deferred_for_approval", "decision_id": decision_id, "message": f"'{name}' (risk tier {risk}) requires human approval; the run is paused and resumes on approval."}
                    is_deferred = True
                    deferred = {"tool": name, "args": args, "decision_id": decision_id}
                    emit_event(connection, project_id, "agent.tool_deferred", "agent_loop", "decision", decision_id, {"tool": name, "risk_tier": risk, "step": step})
                else:
                    result = execute_agent_tool(connection, project_id, settings, name, args, "agent_loop", task_id=task_id)
            else:
                result = _dispatch(connection, project_id, settings, name, args)
                emit_event(connection, project_id, "agent.tool_invoked", "agent_loop", "task", task_id or project_id, {"tool": name, "step": step})
            step_obj = {"step": step, "tool": name, "args": args, "result_preview": json.dumps(result)[:240], "deferred": is_deferred}
            if tc_idx == 0 and thought:
                step_obj["thought"] = thought
            transcript.append(step_obj)
            messages.append({"role": "tool", "tool_call_id": tc["id"], "content": json.dumps(result)[:16000]})

        if deferred is not None:
            _save_run(connection, run_id, project_id, task_id, "awaiting_approval", messages, transcript, total, finalize_ctx, step=step + 1, pending=deferred)
            emit_event(connection, project_id, "agent.run_suspended", "agent_loop", "agent_run", run_id, {"decision_id": deferred["decision_id"], "tool": deferred["tool"]})
            return {"status": "suspended", "run_id": run_id, "decision_id": deferred["decision_id"], "output_text": "", "usage": total, "transcript": transcript}

        _save_run(connection, run_id, project_id, task_id, "running", messages, transcript, total, finalize_ctx, step=step + 1)

    if final_text is None:
        message, usage = adapter.create_chat_completion(messages, tools=None)
        add_usage(usage)
        final_text = message.get("content") or "(agent reached its step limit before finishing)"

    _save_run(connection, run_id, project_id, task_id, "completed", messages, transcript, total, finalize_ctx, step=last_step + 1, output_text=final_text)
    emit_event(connection, project_id, "agent.run_completed", "agent_loop", "agent_run", run_id, {"steps": len(transcript)})
    return {"status": "completed", "run_id": run_id, "output_text": final_text, "usage": total, "transcript": transcript, "tool_calls": len(transcript)}


def conversation_from_messages(messages: list[dict]) -> list[dict]:
    """Transform the raw LLM `messages` array (OpenAI chat format) into clean,
    display-ready turns for the UI — the faithful agent<->LLM back-and-forth.

    Each turn is one of:
      {"role":"system","text":...}
      {"role":"user","text":...}
      {"role":"assistant","text":...,"calls":[{"name","args"}]}
      {"role":"tool","name":...,"text":...}
    """
    id_to_name: dict[str, str] = {}
    turns: list[dict] = []
    for m in messages or []:
        role = m.get("role")
        if role == "system":
            turns.append({"role": "system", "text": m.get("content") or ""})
        elif role == "user":
            turns.append({"role": "user", "text": m.get("content") or ""})
        elif role == "assistant":
            calls = []
            for tc in (m.get("tool_calls") or []):
                fn = tc.get("function") or {}
                name = fn.get("name")
                if tc.get("id"):
                    id_to_name[tc["id"]] = name
                raw_args = fn.get("arguments")
                try:
                    args = json.loads(raw_args) if isinstance(raw_args, str) else (raw_args or {})
                except Exception:
                    args = {"_raw": raw_args}
                calls.append({"name": name, "args": args})
            turns.append({"role": "assistant", "text": m.get("content") or "", "calls": calls})
        elif role == "tool":
            turns.append(
                {
                    "role": "tool",
                    "name": id_to_name.get(m.get("tool_call_id")) or "tool",
                    "text": m.get("content") or "",
                }
            )
    return turns
