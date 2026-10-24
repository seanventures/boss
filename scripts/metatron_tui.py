#!/usr/bin/env python3
"""Metatron — BOSS's terminal. A stdlib REPL client of the one BOSS agent and the /v1 API.

Plain text is an Ask BOSS turn: POST /v1/metatron/session, a thin adapter onto the same
`agent_turn` the console calls (BOSSAPP-759). The agent's offers become numbered choices; picking
one goes through the console's own button path (POST /v1/engine/agent/action).

Reads BOSS_API_URL, BOSS_API_KEY, and BOSS_PROJECT_ID from the environment.
Never prints the API key.

Usage:
  BOSS_API_KEY=… make metatron
  python3 scripts/metatron_tui.py
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass
from typing import Any, Mapping, Optional, TextIO
from urllib.error import HTTPError
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


class NoRedirectHandler(HTTPRedirectHandler):
    """Never forward the API key to a server-selected redirect destination."""

    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


# Patch point for tests. Live runs refuse redirects, including same-host redirects.
urlopen = build_opener(NoRedirectHandler()).open

DEFAULT_API_URL = "http://127.0.0.1:8000"
DEFAULT_PROJECT_ID = "boss-dogfood"
DEFAULT_ACTOR = "executive_console"
DEFAULT_TIMEOUT_SECONDS = 30.0
OPS_PAGE_SIZE = 25

HELP_TEXT = (
    "Type naturally to talk to the BOSS agent — the same agent, thread and actions as the "
    "console's Ask BOSS.\n"
    "Its offers are numbered: /pick N (or just N) runs one, /confirm dispatches a pending plan.\n"
    "Captain commands: /watch  /overwatch  /drive [auto|go]  /goals  — run only when typed.\n"
    "Slash commands: /projects  /project [ID]  /quit\n"
    "Work: /tasks [PAGE]  /task ID  /task create DEDUPE_KEY TITLE\n"
    "Decisions: /decisions [open|all] [PAGE]  /decision ID  /decision approve ID NOTE  /decision reject ID NOTE\n"
    "API commands: /ops [domain or search|*] [--page N]  /describe OPERATION_ID  /op OPERATION_ID [JSON]  /coverage\n"
    "Fallback: /api METHOD /v1/path [JSON]\n"
    "Use /ops to discover live API operations; /op accepts JSON with path, query, and body keys.\n"
    "For typed JSON writes, /op inserts the selected project_id when the body omits it.\n"
    "The API enforces project, permission, edition, approval, budget, and receipt rules.\n"
    "/overwatch runs observe→measure→validate→apply-safe→verify.\n"
    "'/go' or '/drive auto' applies only moves that pass validation."
)

#: A bare number picks one of the last turn's numbered choices.
CHOICE_NUMBER = re.compile(r"[1-9][0-9]{0,2}\Z")


class ConfigError(Exception):
    """Missing or invalid TUI configuration. Never includes the API key."""


@dataclass(frozen=True)
class Config:
    api_url: str
    api_key: str
    project_id: str
    actor: str = DEFAULT_ACTOR


@dataclass(frozen=True)
class Command:
    kind: str
    message: str = ""
    auto: bool = False
    method: str = ""
    path: str = ""
    body: Any = None
    page: int = 1
    dedupe_key: str = ""


HTTP_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE"}
PATH_PARAMETER = re.compile(r"\{([A-Za-z_][A-Za-z_0-9]*)(?::path)?\}")
SIMPLE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")


def valid_v1_path(path: str) -> bool:
    """Refuse redirects to external or ambiguous paths before sending credentials."""
    parsed = urlsplit(path)
    decoded_path = unquote(parsed.path)
    return bool(
        path.startswith("/v1/")
        and not parsed.scheme
        and not parsed.netloc
        and not parsed.fragment
        and decoded_path.startswith("/v1/")
        and "//" not in decoded_path
        and ".." not in decoded_path.split("/")
        and "\\" not in decoded_path
        and not any(ord(char) < 32 or ord(char) == 127 for char in path + decoded_path)
    )


def redact(text: str, secret: str = "") -> str:
    """Strip a known secret from any operator-visible string."""
    if not text:
        return text
    redacted = text
    if secret and secret in redacted:
        redacted = redacted.replace(secret, "***")
    return redacted


def _secret_field(name: str) -> bool:
    with_word_breaks = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name)
    normalized = re.sub(r"[^a-z0-9]+", "_", with_word_breaks.lower()).strip("_")
    if normalized in {
        "secret", "secrets", "secret_value", "secret_key", "secret_access_key",
        "aws_secret_access_key", "token", "access_token", "refresh_token",
        "password", "passphrase", "api_key", "apikey", "private_key",
        "client_secret", "credential", "credentials", "credential_value", "authorization",
    }:
        return True
    return normalized.endswith(("_secret", "_token", "_password", "_api_key", "_private_key"))


def _request_secrets(body: Any, path: str) -> set[str]:
    """Collect submitted credential values without retaining or printing them."""
    segments = set(urlsplit(path).path.lower().split("/"))
    credential_route = bool(segments & {
        "credential", "credentials", "secret", "secrets", "key", "keys",
    })
    secrets: set[str] = set()

    def visit(value: Any, *, sensitive: bool = False) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                field_name = str(key)
                visit(item, sensitive=sensitive or _secret_field(field_name)
                      or (credential_route and field_name.lower() == "value"))
        elif isinstance(value, list):
            for item in value:
                visit(item, sensitive=sensitive)
        elif sensitive and isinstance(value, str) and value:
            secrets.add(value)

    visit(body)
    for name, value in parse_qsl(urlsplit(path).query, keep_blank_values=True):
        if _secret_field(name) and value:
            secrets.add(value)
    return secrets


def _redact_response(raw: str, *, api_key: str, body: Any, path: str) -> str:
    secrets = _request_secrets(body, path)
    secrets.add(api_key)
    # Redact decoded string fields first so escaped JSON values and short
    # secrets are handled without corrupting the response structure.
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        payload = None
    if payload is not None:
        def scrub(value: Any) -> Any:
            if isinstance(value, str):
                for secret in sorted(secrets, key=len, reverse=True):
                    if secret:
                        value = value.replace(secret, "***")
                return value
            if isinstance(value, list):
                return [scrub(item) for item in value]
            if isinstance(value, dict):
                return {scrub(key): scrub(item) for key, item in value.items()}
            return value

        return json.dumps(scrub(payload), ensure_ascii=False)
    for secret in sorted(secrets, key=len, reverse=True):
        raw = redact(raw, secret)
    return raw


def join_url(base: str, path: str) -> str:
    return f"{base.rstrip('/')}/{path.lstrip('/')}"


def load_config(env: Mapping[str, str] | None = None) -> Config:
    source = os.environ if env is None else env
    api_key = (source.get("BOSS_API_KEY") or "").strip()
    if not api_key:
        raise ConfigError("BOSS_API_KEY is required (never print the key).")
    api_url = (source.get("BOSS_API_URL") or DEFAULT_API_URL).strip().rstrip("/")
    project_id = (source.get("BOSS_PROJECT_ID") or DEFAULT_PROJECT_ID).strip()
    actor = (source.get("BOSS_ACTOR") or DEFAULT_ACTOR).strip() or DEFAULT_ACTOR
    if not api_url:
        raise ConfigError("BOSS_API_URL is required.")
    if not project_id:
        raise ConfigError("BOSS_PROJECT_ID is required.")
    return Config(api_url=api_url, api_key=api_key, project_id=project_id, actor=actor)


def parse_line(line: str) -> Command:
    raw = (line or "").strip()
    if not raw:
        return Command(kind="empty")
    lowered = raw.lower()
    if lowered in {"/quit", "/exit", "/q"}:
        return Command(kind="quit")
    if lowered in {"/help", "/?", "help"}:
        return Command(kind="help")
    if lowered == "/watch":
        return Command(kind="watch")
    if lowered in {"/overwatch", "/ow"}:
        return Command(kind="overwatch")
    if lowered == "/goals":
        return Command(kind="goals")
    if lowered == "/projects":
        return Command(kind="projects")
    if lowered == "/project" or lowered.startswith("/project "):
        return Command(kind="project", message=raw.partition(" ")[2].strip())
    if lowered == "/tasks" or lowered.startswith("/tasks "):
        parts = raw.split()
        if len(parts) > 2 or (len(parts) == 2 and not re.fullmatch(r"[1-9][0-9]{0,5}", parts[1])):
            return Command(kind="error", message="usage: /tasks [PAGE], with PAGE >= 1")
        return Command(kind="tasks", page=int(parts[1]) if len(parts) == 2 else 1)
    if lowered == "/task" or lowered.startswith("/task "):
        parts = raw.split(None, 3)
        if len(parts) >= 2 and parts[1].lower() == "create":
            if len(parts) != 4 or not SIMPLE_ID.fullmatch(parts[2]) or not parts[3].strip():
                return Command(kind="error", message="usage: /task create DEDUPE_KEY TITLE")
            return Command(kind="task_create", message=parts[3].strip(), dedupe_key=parts[2])
        if len(parts) != 2 or not SIMPLE_ID.fullmatch(parts[1]):
            return Command(kind="error", message="usage: /task ID or /task create DEDUPE_KEY TITLE")
        return Command(kind="task", message=parts[1])
    if lowered == "/decisions" or lowered.startswith("/decisions "):
        parts = raw.split()
        if (len(parts) > 3 or (len(parts) >= 2 and parts[1].lower() not in {"open", "all"})
                or (len(parts) == 3 and not re.fullmatch(r"[1-9][0-9]{0,5}", parts[2]))):
            return Command(kind="error", message="usage: /decisions [open|all] [PAGE], with PAGE >= 1")
        return Command(kind="decisions", message=parts[1].lower() if len(parts) >= 2 else "open",
                       page=int(parts[2]) if len(parts) == 3 else 1)
    if lowered == "/decision" or lowered.startswith("/decision "):
        parts = raw.split(None, 3)
        if len(parts) >= 2 and parts[1].lower() in {"approve", "reject"}:
            if len(parts) != 4 or not SIMPLE_ID.fullmatch(parts[2]) or not parts[3].strip():
                return Command(kind="error", message="usage: /decision approve|reject ID NOTE")
            return Command(kind="decision_action", method=parts[1].lower(),
                           message=parts[2], body={"note": parts[3].strip()})
        if len(parts) != 2 or not SIMPLE_ID.fullmatch(parts[1]):
            return Command(kind="error", message="usage: /decision ID or /decision approve|reject ID NOTE")
        return Command(kind="decision", message=parts[1])
    if lowered == "/ops" or lowered.startswith("/ops "):
        return Command(kind="ops", message=raw.partition(" ")[2].strip())
    if lowered == "/coverage":
        return Command(kind="coverage")
    if lowered == "/describe" or lowered.startswith("/describe "):
        operation_id = raw.partition(" ")[2].strip()
        if not operation_id or " " in operation_id:
            return Command(kind="error", message="usage: /describe OPERATION_ID")
        return Command(kind="describe", message=operation_id)
    if lowered == "/op" or lowered.startswith("/op "):
        parts = raw.split(None, 2)
        if len(parts) < 2:
            return Command(kind="error", message="usage: /op OPERATION_ID [JSON]")
        arguments: Any = {}
        if len(parts) == 3:
            try:
                arguments = json.loads(parts[2])
            except json.JSONDecodeError:
                return Command(kind="error", message="/op arguments must be valid JSON")
        if not isinstance(arguments, dict) or set(arguments) - {"path", "query", "body"}:
            return Command(kind="error", message="/op JSON must contain only path, query, and body keys")
        return Command(kind="op", message=parts[1], body=arguments)
    if lowered == "/api" or lowered.startswith("/api "):
        parts = raw.split(None, 3)
        if len(parts) < 3:
            return Command(kind="error", message="usage: /api METHOD /v1/path [JSON]")
        method, path = parts[1].upper(), parts[2]
        if method not in HTTP_METHODS:
            return Command(kind="error", message="/api method must be GET, POST, PUT, PATCH, or DELETE")
        if not valid_v1_path(path):
            return Command(kind="error", message="/api path must be a local /v1/ route")
        body = None
        if len(parts) == 4:
            if method == "GET":
                return Command(kind="error", message="GET requests cannot include a JSON body")
            try:
                body = json.loads(parts[3])
            except json.JSONDecodeError:
                return Command(kind="error", message="/api body must be valid JSON")
        return Command(kind="api", method=method, path=path, body=body)
    # A CAPTAIN POWER IS TYPED AS A COMMAND, NEVER READ OUT OF A SENTENCE. Bare "go" used to
    # apply drive moves, a factory job among them; it is a word the agent hears now (BOSSAPP-759).
    if lowered == "/go":
        return Command(kind="drive", auto=True)
    if lowered == "/confirm":
        return Command(kind="confirm")
    if CHOICE_NUMBER.match(lowered):
        return Command(kind="pick", page=int(lowered))
    if lowered == "/pick" or lowered.startswith("/pick "):
        parts = raw.split()
        if len(parts) != 2 or not CHOICE_NUMBER.match(parts[1]):
            return Command(kind="error", message="usage: /pick N, with N one of the numbered choices")
        return Command(kind="pick", page=int(parts[1]))
    if lowered == "/drive" or lowered.startswith("/drive "):
        rest = raw.split(None, 1)[1].strip().lower() if " " in raw else ""
        return Command(kind="drive", auto=rest in {"auto", "go"})
    if raw.startswith("/"):
        return Command(kind="help", message=raw)
    return Command(kind="session", message=raw)


def format_banner(config: Config) -> str:
    return (
        f"Metatron captain  project={config.project_id}  api={config.api_url}\n"
        f"{HELP_TEXT}"
    )


def format_payload(payload: Any) -> str:
    if not isinstance(payload, dict):
        return "" if payload is None else str(payload)
    reply = payload.get("reply")
    if isinstance(reply, str) and reply.strip():
        return reply.strip()
    lines: list[str] = []
    status = payload.get("status")
    if status:
        lines.append(f"status: {status}")
    note = payload.get("note")
    if note:
        lines.append(str(note))
    goals = payload.get("goals")
    if isinstance(goals, list):
        if not goals:
            lines.append("No goals.")
        for goal in goals:
            if not isinstance(goal, dict):
                lines.append(f"- {goal}")
                continue
            title = goal.get("title") or goal.get("id") or "goal"
            kind = goal.get("kind")
            extra = f" ({kind})" if kind else ""
            lines.append(f"- {title}{extra}")
    findings = payload.get("findings")
    if isinstance(findings, list):
        for finding in findings:
            if isinstance(finding, dict):
                lines.append(f"- {finding.get('title') or finding.get('id') or finding}")
            else:
                lines.append(f"- {finding}")
    moves = payload.get("moves")
    if isinstance(moves, list) and moves:
        lines.append(f"moves: {len(moves)}")
        for move in moves:
            if isinstance(move, dict):
                lines.append(f"- {move.get('kind') or 'move'}: {move.get('status') or move.get('detail') or ''}".rstrip())
    if lines:
        return "\n".join(lines)
    return json.dumps(payload, indent=2, default=str)


def format_agent_turn(payload: Any, choices: list[dict[str, Any]]) -> str:
    """An Ask BOSS reply, for a terminal: the words, what it did, what it offers (numbered, for
    /pick N), and anything waiting on the person's say-so."""
    if not isinstance(payload, dict):
        return format_payload(payload)
    lines: list[str] = []
    reply = str(payload.get("reply") or "").strip()
    if reply:
        lines.append(reply)
    done = [a for a in payload.get("actions") or [] if isinstance(a, dict)]
    if done:
        lines.append("")
        lines.append("Done:")
        for action in done:
            lines.append(f"  - {action.get('summary') or action.get('kind') or 'action'}")
    pending = payload.get("pending_confirmation")
    if isinstance(pending, dict) and pending.get("plan_id"):
        lines.append("")
        summary = pending.get("summary") or pending.get("title") or pending.get("plan_id")
        lines.append(f"Waiting for your confirmation: {summary}")
        lines.append("  /confirm to go ahead, or say what to change.")
    if choices:
        lines.append("")
        lines.append("Choices (nothing has run yet — /pick N, or type N):")
        for number, choice in enumerate(choices, 1):
            label = choice.get("label") or choice.get("tool") or choice.get("message") or "choice"
            why = str(choice.get("why") or "").strip()
            tool = f" [{choice['tool']}]" if choice.get("kind") == "action" else ""
            lines.append(f"  {number}. {label}{tool}" + (f" — {why}" if why else ""))
    return "\n".join(lines) if lines else format_payload(payload)


def format_action_result(payload: Mapping[str, Any]) -> str:
    """What pressing an offer did (AgentActionRunResponse)."""
    tool = payload.get("tool") or "action"
    if payload.get("ok") is False or payload.get("error"):
        return f"{tool} did not run: {payload.get('error') or 'refused'}"
    result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
    said = result.get("reply") or result.get("summary") or result.get("note") or result.get("message")
    return f"{tool}: done." + (f"\n{said}" if said else "")


def format_ops(payload: Mapping[str, Any]) -> str:
    if "domains" in payload:
        lines = [f"Project: {payload['active_project_id']}", "API domains:"]
        for domain in payload["domains"]:
            lines.append(f"  {domain['id']:<18} {domain['route_count']:>3}  {domain.get('title') or ''}".rstrip())
        lines.append(str(payload["hint"]))
        return "\n".join(lines)
    lines = [
        f"{payload['count']} matches for {payload['search']!r} "
        f"(page {payload['page']}/{payload['pages']}):"
    ]
    for operation in payload["operations"]:
        alias = operation["operation_id"] or f"/api {operation['method']} {operation['path']}"
        lines.append(f"  {alias}  {operation['method']} {operation['path']}")
        summary = str(operation.get("summary") or "").strip()
        if summary:
            lines.append(f"    {summary}")
        if not operation["terminal_invokable"]:
            lines.append(f"    Limit: {operation.get('limitation') or 'not addressable by /op'}")
    if payload["page"] < payload["pages"]:
        lines.append(f"Next: /ops {payload['search']} --page {payload['page'] + 1}")
    lines.append("Use /describe OPERATION_ID to inspect fields before /op.")
    return "\n".join(lines)


def format_description(payload: Mapping[str, Any]) -> str:
    lines = [
        f"{payload['method']} {payload['path']}",
        f"Operation: {payload['operation_id']}",
    ]
    if payload.get("summary"):
        lines.append(str(payload["summary"]))
    if payload.get("description"):
        lines.append(str(payload["description"]).strip())
    parameters = payload.get("parameters") or []
    if parameters:
        lines.append("Parameters:")
        for parameter in parameters:
            required = "required" if parameter.get("required") else "optional"
            kind = _field_type(parameter.get("schema") or {})
            lines.append(f"  {parameter.get('in')} {parameter.get('name')}: {kind}, {required}")
    body_fields = payload.get("body_fields") or []
    if body_fields:
        lines.append("JSON body fields:")
        for field in body_fields:
            required = "required" if field["required"] else "optional"
            detail = f"  {field['name']}: {field['type']}, {required}"
            if field.get("enum"):
                detail += f", choices={field['enum']}"
            if field.get("required_children"):
                detail += f", required keys={field['required_children']}"
            if field.get("description"):
                detail += f" — {field['description']}"
            lines.append(detail)
    if payload.get("auto_filled_body_fields"):
        lines.append("The selected project_id is inserted into a JSON body when omitted.")
    if not payload["terminal_invokable"]:
        lines.append(f"Limit: {payload['limitation']}")
    lines.extend((f"Example: {payload['example']}", str(payload["access_note"])))
    return "\n".join(lines)


def format_projects(payload: Mapping[str, Any]) -> str:
    lines = [f"Projects returned by the API (active: {payload['active_project_id']}):"]
    for project in payload["items"]:
        if not isinstance(project, dict):
            continue
        mark = "*" if project.get("id") == payload["active_project_id"] else " "
        label = project.get("name") or project.get("slug") or project.get("id")
        location = " / ".join(str(value) for value in (
            project.get("org_name"), project.get("workspace_name")
        ) if value)
        lines.append(f"{mark} {project.get('id')}: {label}" + (f" ({location})" if location else ""))
    lines.append("Use /project ID to select a project for subsequent commands.")
    return "\n".join(lines)


def format_tasks(payload: Mapping[str, Any], page: int) -> str:
    items = payload.get("items")
    if not isinstance(items, list):
        return json.dumps(payload, indent=2, default=str)
    lines = [f"Tasks in {payload.get('project_id') or 'selected project'} (page {page}):"]
    for task in items:
        if isinstance(task, dict):
            lines.append(f"  {task.get('id') or '?'}  [{task.get('status') or '?'}]  {task.get('title') or 'Untitled'}")
    if not items:
        lines.append("  No tasks on this page.")
    if len(items) == OPS_PAGE_SIZE:
        lines.append(f"Next: /tasks {page + 1}")
    lines.append("Use /task ID for details or /task create DEDUPE_KEY TITLE to add work.")
    return "\n".join(lines)


def format_task(payload: Mapping[str, Any]) -> str:
    if not payload.get("id"):
        return json.dumps(payload, indent=2, default=str)
    lines = [f"Task {payload['id']}: {payload.get('title') or 'Untitled'}"]
    if payload.get("project_id"):
        lines.append(f"project: {payload['project_id']}")
    for field in ("status", "priority", "owner_role_id", "due_at", "latest_job_status"):
        if payload.get(field) is not None:
            lines.append(f"{field.replace('_', ' ')}: {payload[field]}")
    if payload.get("description"):
        lines.append(str(payload["description"]))
    if payload.get("has_open_decision"):
        lines.append("Open decision: yes; use /decisions to review the inbox.")
    lines.append("For the complete record, use --exec '/task ID' --json.")
    return "\n".join(lines)


def format_decisions(payload: Mapping[str, Any]) -> str:
    view = payload["view"]
    lines = [
        f"{view.title()} decisions in {payload['project_id']} "
        f"({payload['total_count']} total, page {payload['page']}/{payload['pages']}):"
    ]
    for decision in payload["items"]:
        lines.append(
            f"  {decision.get('id') or '?'}  [{decision.get('status') or '?'}; "
            f"risk {decision.get('risk_tier', '?')}]  {decision.get('question') or 'Untitled'}"
        )
    if not payload["items"]:
        lines.append("  No decisions on this page.")
    if payload["page"] < payload["pages"]:
        lines.append(f"Next: /decisions {view} {payload['page'] + 1}")
    lines.append("Use /decision ID to review a decision before acting.")
    return "\n".join(lines)


def format_decision(payload: Mapping[str, Any]) -> str:
    lines = [f"Decision {payload['id']}: {payload.get('question') or 'Untitled'}"]
    for field in ("status", "risk_tier", "task_id", "deadline_at", "reason",
                  "default_action", "approved_by", "delegated_to", "resolution_note"):
        if payload.get(field) is not None:
            lines.append(f"{field.replace('_', ' ')}: {payload[field]}")
    for field in ("recommendation", "options", "cost_summary"):
        if payload.get(field):
            lines.append(f"{field.replace('_', ' ')}: {json.dumps(payload[field], ensure_ascii=False)}")
    lines.append("Use /decision approve ID NOTE or /decision reject ID NOTE; the API checks the authenticated reviewer and policy.")
    lines.append("For the complete record, use --exec '/decision ID' --json.")
    return "\n".join(lines)


def operation_records(document: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Enumerate the running server's OpenAPI operations, including gaps."""
    paths = document.get("paths")
    if not isinstance(paths, dict):
        raise ValueError("the API returned an OpenAPI document without paths")
    records: list[dict[str, Any]] = []
    for path, methods in paths.items():
        if not isinstance(path, str) or not isinstance(methods, dict):
            continue
        for method, spec in methods.items():
            if method.upper() not in HTTP_METHODS or not isinstance(spec, dict):
                continue
            # FastAPI-generated IDs can collide for multi-method routes. Use
            # the same stable method+path ID as the shipped Community contract.
            slug = re.sub(r"[^a-z0-9]+", "_", f"{method}_{path}".lower()).strip("_")
            digest = hashlib.sha256(f"{method.lower()} {path}".encode()).hexdigest()[:8]
            operation_id = f"{slug[:68]}_{digest}"
            runtime_operation_id = spec.get("operationId")
            parameters = [
                p for p in (methods.get("parameters") or []) + (spec.get("parameters") or [])
                if isinstance(p, dict)
            ]
            request_body = spec.get("requestBody") or {}
            media_types = list((request_body.get("content") or {}).keys()) if isinstance(request_body, dict) else []
            unsupported = []
            if not path.startswith("/v1/") or not valid_v1_path(path):
                unsupported.append("outside the authenticated /v1/ operator API")
            if any(p.get("required") and p.get("in") in {"header", "cookie"} for p in parameters):
                unsupported.append("requires an explicit header or cookie")
            if media_types and "application/json" not in media_types:
                unsupported.append("requires a non-JSON request body")
            records.append({
                "operation_id": operation_id,
                "runtime_operation_id": runtime_operation_id,
                "method": method.upper(),
                "path": path,
                "summary": spec.get("summary") or "",
                "description": spec.get("description") or "",
                "parameters": parameters,
                "request_body": request_body,
                "addressable": not unsupported,
                "limitation": "; ".join(unsupported) if unsupported else None,
            })
    return sorted(records, key=lambda item: (item["path"], item["method"]))


def _command_error(message: str) -> dict[str, Any]:
    return {"__command_error__": True, "message": message}


def _route_key(method: str, path: str) -> tuple[str, str]:
    # Starlette exposes catch-all routes as {path:path}; OpenAPI calls them
    # {path}. They are the same mounted operation, not a documentation gap.
    normalized = re.sub(r"\{([A-Za-z_][A-Za-z_0-9]*):path\}", r"{\1}", path)
    return method.upper(), normalized


def _query_value(value: Any) -> str:
    if isinstance(value, bool):
        return str(value).lower()
    if value is None:
        return ""
    return str(value)


def _object_schema(
    document: Mapping[str, Any], schema: Any, seen: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    """Resolve a bounded object shape for help and safe project defaults."""
    if not isinstance(schema, dict):
        return {}
    ref = schema.get("$ref")
    if isinstance(ref, str) and ref.startswith("#/components/schemas/") and ref not in seen:
        name = ref.rsplit("/", 1)[-1]
        target = (document.get("components") or {}).get("schemas", {}).get(name)
        return _object_schema(document, target, seen | {ref})
    if "allOf" in schema:
        parts = [_object_schema(document, part, seen) for part in schema["allOf"]]
        properties = dict(schema.get("properties") or {})
        required = set(schema.get("required") or [])
        for part in parts:
            properties.update(part.get("properties") or {})
            required.update(part.get("required") or [])
        return {"properties": properties, "required": sorted(required)}
    for variant_key in ("anyOf", "oneOf"):
        if variant_key in schema:
            candidates = [
                resolved for part in schema[variant_key]
                if (resolved := _object_schema(document, part, seen)).get("properties")
            ]
            # A union of multiple objects is ambiguous. Never infer fields
            # or add a project to a branch the caller did not select.
            return candidates[0] if len(candidates) == 1 else {}
    return schema


def _field_type(field: Mapping[str, Any]) -> str:
    if isinstance(field.get("type"), str):
        return str(field["type"])
    if isinstance(field.get("$ref"), str):
        return str(field["$ref"]).rsplit("/", 1)[-1]
    for key in ("anyOf", "oneOf"):
        if isinstance(field.get(key), list):
            variants = sorted({
                _field_type(part) for part in field[key]
                if isinstance(part, dict) and part.get("type") != "null"
            })
            if variants:
                return " | ".join(variants)
    return "composite"


def _body_fields(document: Mapping[str, Any], request_body: Mapping[str, Any]) -> list[dict[str, Any]]:
    content = request_body.get("content") or {}
    media = content.get("application/json") or {}
    schema = _object_schema(document, media.get("schema") or {})
    required = set(schema.get("required") or [])
    fields: list[dict[str, Any]] = []
    for name, field in (schema.get("properties") or {}).items():
        if not isinstance(field, dict):
            continue
        nested = _object_schema(document, field)
        fields.append({
            "name": name,
            "required": name in required,
            "type": _field_type(field),
            "description": field.get("description") or "",
            "enum": field.get("enum") if isinstance(field.get("enum"), list) else [],
            "required_children": sorted(nested.get("required") or []),
        })
    return fields


def _request_path(
    operation: Mapping[str, Any], arguments: Mapping[str, Any], project_id: str,
    *, catchall_parameters: set[str] | None = None,
) -> tuple[str, Any]:
    """Build one declared HTTP call. The API remains the policy authority."""
    path_values = arguments.get("path", {})
    query_values = arguments.get("query", {})
    if not isinstance(path_values, dict) or not isinstance(query_values, dict):
        raise ValueError("path and query arguments must be JSON objects")
    template = str(operation["path"])
    placeholders = set(PATH_PARAMETER.findall(template))
    if set(path_values) != placeholders:
        missing = sorted(placeholders - set(path_values))
        unexpected = sorted(set(path_values) - placeholders)
        raise ValueError(f"path parameters: missing={missing}, unexpected={unexpected}")
    path = template
    for name in placeholders:
        value = path_values[name]
        if not isinstance(value, (str, int)) or isinstance(value, bool):
            raise ValueError(f"path parameter {name} must be a string or integer")
        value = str(value)
        catchall = name in (catchall_parameters or set())
        segments = value.split("/") if catchall else [value]
        if (
            not value or any(segment in {"", ".", ".."} for segment in segments)
            or ("/" in value and not catchall) or "\\" in value or "%" in value
            or any(ord(c) < 32 or ord(c) == 127 for c in value)
        ):
            raise ValueError(f"path parameter {name} contains an unsafe value")
        encoded = "/".join(quote(segment, safe="") for segment in segments)
        path = re.sub(r"\{" + re.escape(name) + r"(?::path)?\}", encoded, path)
    if "{" in path or "}" in path or not valid_v1_path(path):
        raise ValueError("operation resolved to an unsafe /v1/ path")
    declared_query = {
        p.get("name"): p for p in operation["parameters"] if p.get("in") == "query"
    }
    unknown_query = sorted(set(query_values) - set(declared_query))
    if unknown_query:
        raise ValueError(f"undeclared query parameters: {unknown_query}")
    query = dict(query_values)
    if "project_id" in declared_query and "project_id" not in query:
        query["project_id"] = project_id
    required_query = sorted(
        name for name, p in declared_query.items() if p.get("required") and name not in query
    )
    if required_query:
        raise ValueError(f"missing required query parameters: {required_query}")
    pairs: list[tuple[str, str]] = []
    for name, value in query.items():
        values = value if isinstance(value, list) else [value]
        if any(isinstance(item, (dict, list)) for item in values):
            raise ValueError(f"query parameter {name} must be a scalar or list of scalars")
        pairs.extend((name, _query_value(item)) for item in values)
    if pairs:
        path = f"{path}?{urlencode(pairs)}"
    body = arguments.get("body")
    if operation["method"] == "GET" and "body" in arguments:
        raise ValueError("GET operations cannot include a JSON body")
    request_body = operation["request_body"]
    if request_body.get("required") and "body" not in arguments:
        raise ValueError("operation requires a JSON body; see /describe")
    return path, body


class MetatronClient:
    """Thin HTTP client for the captain surface. Inject urlopen in tests."""

    def __init__(self, config: Config, urlopen=None):
        self.config = config
        self.project_id = config.project_id
        self.thread_id: Optional[str] = None
        #: The last agent turn, whose offers /pick N and /confirm act on.
        self.last_turn: Optional[dict[str, Any]] = None
        self._injected_urlopen = urlopen
        self._openapi_document: dict[str, Any] | None = None
        self._api_catalog: dict[str, Any] | None = None

    def _opener(self):
        return self._injected_urlopen if self._injected_urlopen is not None else urlopen

    def request(self, method: str, path: str, body: Any = None) -> Any:
        url = join_url(self.config.api_url, path)
        encoded = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {
            "Accept": "application/json",
            "X-Boss-Api-Key": self.config.api_key,
        }
        if encoded is not None:
            headers["Content-Type"] = "application/json"
        try:
            request = Request(url, data=encoded, headers=headers, method=method)
            with self._opener()(request, timeout=DEFAULT_TIMEOUT_SECONDS) as response:
                raw = response.read().decode("utf-8", errors="replace")
                status = int(getattr(response, "status", 200) or 200)
        except HTTPError as error:
            status = int(error.code)
            try:
                error_body = error.read().decode("utf-8", errors="replace")
            except Exception:  # noqa: BLE001 — response read failures may contain request bodies
                return {"__http_error__": True, "status": status,
                        "body": "response body unavailable"}
            raw = _redact_response(
                error_body,
                api_key=self.config.api_key, body=body, path=path,
            )
            return {"__http_error__": True, "status": status, "body": raw}
        except Exception as error:  # noqa: BLE001 — transport errors may contain request bodies
            reason_type = type(getattr(error, "reason", error)).__name__
            return {"__http_error__": True, "status": 0,
                    "body": f"unreachable: request failed before an HTTP response ({reason_type})"}
        raw = _redact_response(raw, api_key=self.config.api_key, body=body, path=path)
        if not raw.strip():
            return {"status": status}
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {"status": status, "body": raw}

    def _remember_thread(self, payload: Any) -> None:
        if isinstance(payload, dict):
            thread_id = payload.get("thread_id")
            if isinstance(thread_id, str) and thread_id.strip():
                self.thread_id = thread_id.strip()

    def session(self, message: str, *, confirm_plan_id: str = "", depth: str = "") -> Any:
        """One Ask BOSS turn — the console's request body, the console's reply (AgentChatResponse)."""
        body: dict[str, Any] = {
            "project_id": self.project_id,
            "actor": self.config.actor,
            "message": message,
            "surface": "chat",
        }
        if self.thread_id:
            body["thread_id"] = self.thread_id
        if confirm_plan_id:
            body["confirm_plan_id"] = confirm_plan_id
        if depth:
            body["depth"] = depth
        payload = self.request("POST", "/v1/metatron/session", body)
        self._remember_thread(payload)
        if isinstance(payload, dict) and not payload.get("__http_error__"):
            self.last_turn = payload
        return payload

    def choices(self) -> list[dict[str, Any]]:
        """The last turn's numbered choices: its offers, then its next steps — in that order,
        the order they are printed in."""
        turn = self.last_turn if isinstance(self.last_turn, dict) else {}
        out: list[dict[str, Any]] = []
        for offer in turn.get("proposed_actions") or []:
            if isinstance(offer, dict) and offer.get("tool"):
                out.append({"kind": "action", **offer})
        for step in turn.get("next_steps") or []:
            if isinstance(step, dict) and step.get("message"):
                out.append({"kind": "next_step", **step})
        return out

    def pick(self, number: int) -> Any:
        """Run choice N exactly as the console's button would: an offer goes to
        /v1/engine/agent/action with the tool and args the person was shown (no model in
        between); a next step is the turn the console's next-step button sends."""
        options = self.choices()
        if not options:
            return _command_error("there is nothing to pick — the last reply offered no choices")
        if number < 1 or number > len(options):
            return _command_error(f"pick a number from 1 to {len(options)}")
        chosen = options[number - 1]
        if chosen["kind"] == "next_step":
            return self.session(str(chosen["message"]), depth=str(chosen.get("depth") or ""))
        body: dict[str, Any] = {
            "project_id": self.project_id,
            "actor": self.config.actor,
            "tool": chosen["tool"],
            "args": dict(chosen.get("args") or {}),
        }
        if self.thread_id:
            body["thread_id"] = self.thread_id
        if chosen.get("id"):
            body["action_id"] = chosen["id"]
        if chosen.get("label"):
            body["label"] = chosen["label"]
        payload = self.request("POST", "/v1/engine/agent/action", body)
        if isinstance(payload, dict) and not payload.get("__http_error__"):
            # SPENT, AS IN THE CONSOLE: a pressed offer is struck from the message that made it.
            if isinstance(self.last_turn, dict):
                self.last_turn = {
                    **self.last_turn,
                    "proposed_actions": [
                        o for o in self.last_turn.get("proposed_actions") or []
                        if not (isinstance(o, dict) and o.get("id") == chosen.get("id"))
                    ],
                }
        return payload

    def confirm(self) -> Any:
        turn = self.last_turn if isinstance(self.last_turn, dict) else {}
        pending = turn.get("pending_confirmation") or {}
        plan_id = str(pending.get("plan_id") or "") if isinstance(pending, dict) else ""
        if not plan_id:
            return _command_error("there is no plan waiting for your confirmation")
        return self.session("", confirm_plan_id=plan_id)

    def watch(self) -> Any:
        return self.request(
            "POST",
            "/v1/metatron/watch",
            {"project_id": self.project_id, "actor": self.config.actor},
        )

    def drive(self, *, auto: bool) -> Any:
        return self.request(
            "POST",
            "/v1/metatron/drive",
            {
                "project_id": self.project_id,
                "actor": self.config.actor,
                "auto": bool(auto),
            },
        )

    def overwatch(self) -> Any:
        return self.request(
            "POST",
            "/v1/metatron/overwatch",
            {"project_id": self.project_id, "actor": self.config.actor},
        )

    def goals(self) -> Any:
        query = urlencode({"project_id": self.project_id})
        return self.request("GET", f"/v1/metatron/goals?{query}")

    def projects(self) -> Any:
        payload = self.request("GET", "/v1/projects")
        if isinstance(payload, dict) and payload.get("__http_error__"):
            return payload
        if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
            return _command_error("the API did not return a project list")
        return {"active_project_id": self.project_id, "items": payload["items"]}

    def tasks(self, page: int) -> Any:
        query = urlencode({
            "project_id": self.project_id,
            "limit": OPS_PAGE_SIZE,
            "offset": (page - 1) * OPS_PAGE_SIZE,
        })
        return self.request("GET", f"/v1/tasks?{query}")

    def task(self, task_id: str) -> Any:
        payload = self.request("GET", f"/v1/tasks/{quote(task_id, safe='')}")
        if isinstance(payload, dict) and isinstance(payload.get("project_id"), str) and payload["project_id"] != self.project_id:
            return _command_error("task is outside the selected project; use /project ID first")
        return payload

    def create_task(self, title: str, dedupe_key: str) -> Any:
        return self.request("POST", "/v1/tasks", {
            "project_id": self.project_id,
            "actor": self.config.actor,
            "title": title,
            "dedupe_key": dedupe_key,
        })

    def decisions(self, view: str, page: int) -> Any:
        query = urlencode({"project_id": self.project_id})
        payload = self.request("GET", f"/v1/decisions/inbox?{query}")
        if isinstance(payload, dict) and payload.get("__http_error__"):
            return payload
        if (not isinstance(payload, dict) or payload.get("project_id") != self.project_id
                or not isinstance(payload.get("items"), list)):
            return _command_error("the API did not return the selected project's decision inbox")
        items = payload["items"]
        if any(not isinstance(item, dict) for item in items):
            return _command_error("the API returned an invalid decision inbox")
        if view == "open":
            items = [item for item in items if item.get("status") in {"pending", "delegated"}]
        pages = max(1, (len(items) + OPS_PAGE_SIZE - 1) // OPS_PAGE_SIZE)
        if page > pages:
            return _command_error(f"page {page} is past the last page ({pages})")
        return {"project_id": self.project_id, "view": view, "page": page, "pages": pages,
                "total_count": len(items), "items": items[(page - 1) * OPS_PAGE_SIZE:page * OPS_PAGE_SIZE]}

    def decision(self, decision_id: str) -> Any:
        query = urlencode({"project_id": self.project_id})
        payload = self.request("GET", f"/v1/decisions/{quote(decision_id, safe='')}?{query}")
        if isinstance(payload, dict) and payload.get("__http_error__"):
            return payload
        if not isinstance(payload, dict) or payload.get("id") != decision_id:
            return _command_error("the API did not return the requested decision")
        return payload

    def decide(self, action: str, decision_id: str, note: str) -> Any:
        # The scoped read keeps the selected project meaningful even though the
        # transition endpoint itself identifies the decision only by ID.
        detail = self.decision(decision_id)
        if isinstance(detail, dict) and (detail.get("__http_error__") or detail.get("__command_error__")):
            return detail
        return self.request(
            "POST", f"/v1/decisions/{quote(decision_id, safe='')}/{action}",
            {"actor": self.config.actor, "note": note},
        )

    def project(self, project_id: str) -> dict[str, Any]:
        if project_id:
            if (
                "/" in project_id or "\\" in project_id or "?" in project_id
                or "#" in project_id or any(ord(c) < 32 or ord(c) == 127 for c in project_id)
            ):
                return _command_error("project ID contains an unsafe character")
            self.project_id = project_id
            self.thread_id = None  # Conversation context must not cross projects.
            self.last_turn = None  # ...and neither may the choices it offered.
        return {"active_project_id": self.project_id, "note": "The API checks project access on every request."}

    def _openapi(self) -> dict[str, Any]:
        if self._openapi_document is None:
            payload = self.request("GET", "/openapi.json")
            if isinstance(payload, dict) and payload.get("__http_error__"):
                return payload
            if not isinstance(payload, dict) or not isinstance(payload.get("paths"), dict):
                return _command_error("the running API did not return a valid OpenAPI document")
            self._openapi_document = payload
        return self._openapi_document

    def _catalog(self) -> dict[str, Any]:
        if self._api_catalog is None:
            payload = self.request("GET", "/v1/meta/api-catalog")
            if isinstance(payload, dict) and (payload.get("__http_error__") or payload.get("__command_error__")):
                return payload
            if not isinstance(payload, dict) or not isinstance(payload.get("domains"), list):
                return _command_error("the running API did not return a valid capability catalog")
            self._api_catalog = payload
        return self._api_catalog

    def _records(self) -> list[dict[str, Any]] | dict[str, Any]:
        document = self._openapi()
        if document.get("__http_error__") or document.get("__command_error__"):
            return document
        try:
            return operation_records(document)
        except ValueError as exc:
            return _command_error(str(exc))

    def _domain_map(self, catalog: Mapping[str, Any]) -> dict[tuple[str, str], str]:
        return {
            _route_key(str(route.get("method", "")), str(route.get("path", ""))): str(domain.get("id", "other"))
            for domain in catalog.get("domains", []) if isinstance(domain, dict)
            for route in domain.get("routes", []) if isinstance(route, dict)
        }

    def ops(self, term: str = "") -> dict[str, Any]:
        catalog = self._catalog()
        if catalog.get("__http_error__") or catalog.get("__command_error__"):
            return catalog
        page = 1
        if "--page" in term:
            match = re.search(r"(?:^|\s)--page(?:=|\s+)([1-9][0-9]{0,5})\s*$", term)
            if not match:
                return _command_error("usage: /ops [domain or search|*] [--page N], with N >= 1")
            page = int(match.group(1))
            term = term[:match.start()].strip()
        if not term:
            if page != 1:
                return _command_error("use /ops * --page N to browse all operations")
            return {
                "active_project_id": self.project_id,
                "domains": [
                    {"id": d.get("id"), "title": d.get("title"), "route_count": d.get("route_count")}
                    for d in catalog["domains"] if isinstance(d, dict) and d.get("route_count")
                ],
                "hint": "Use /ops DOMAIN, /ops SEARCH, or /ops * --page N; then /describe OPERATION_ID and /op OPERATION_ID [JSON].",
            }
        records = self._records()
        if isinstance(records, dict):
            return records
        domain_map = self._domain_map(catalog)
        documented_keys = {_route_key(item["method"], item["path"]) for item in records}
        needle = "" if term == "*" else term.casefold()
        matches = []
        for operation in records:
            domain = domain_map.get(_route_key(operation["method"], operation["path"]), "other")
            if not operation["path"].startswith("/v1/"):
                continue
            haystack = " ".join([
                domain, operation["method"], operation["path"],
                operation["operation_id"] or "", operation["summary"],
            ]).casefold()
            if needle in haystack:
                matches.append({
                    "domain": domain,
                    "operation_id": operation["operation_id"],
                    "method": operation["method"],
                    "path": operation["path"],
                    "summary": operation["summary"],
                    "terminal_invokable": operation["addressable"],
                    "limitation": operation["limitation"],
                })
        for (method, path), domain in domain_map.items():
            if (method, path) in documented_keys or not path.startswith("/v1/"):
                continue
            if needle in f"{domain} {method} {path}".casefold():
                matches.append({
                    "domain": domain, "operation_id": None, "method": method,
                    "path": path, "summary": "Catalog-only route",
                    "terminal_invokable": False,
                    "limitation": "use /api METHOD /v1/path [JSON]",
                })
        matches.sort(key=lambda item: (item["domain"], item["path"], item["method"]))
        pages = max(1, (len(matches) + OPS_PAGE_SIZE - 1) // OPS_PAGE_SIZE)
        if page > pages:
            return _command_error(f"page {page} is past the last page ({pages})")
        shown = matches[(page - 1) * OPS_PAGE_SIZE:page * OPS_PAGE_SIZE]
        return {"search": term, "count": len(matches), "shown_count": len(shown),
                "page": page, "pages": pages, "page_size": OPS_PAGE_SIZE,
                "operations": shown,
                "access_note": "The API applies edition, permission, and policy checks when invoked."}

    def describe(self, operation_id: str) -> dict[str, Any]:
        records = self._records()
        if isinstance(records, dict):
            return records
        operation = next((item for item in records if item["operation_id"] == operation_id), None)
        if operation is None:
            return _command_error(f"unknown operationId: {operation_id}; use /ops SEARCH")
        body_fields = _body_fields(self._openapi_document or {}, operation["request_body"])
        required_query = {
            p["name"]: (self.project_id if p["name"] == "project_id" else f"<{p['name']}>")
            for p in operation["parameters"]
            if p.get("in") == "query" and p.get("required") and isinstance(p.get("name"), str)
        }
        required_body = {
            item["name"]: (self.project_id if item["name"] == "project_id" else f"<{item['name']}>")
            for item in body_fields if item["required"]
        }
        example_args: dict[str, Any] = {}
        path_names = PATH_PARAMETER.findall(operation["path"])
        if path_names:
            example_args["path"] = {name: f"<{name}>" for name in path_names}
        if required_query:
            example_args["query"] = required_query
        if operation["request_body"].get("required"):
            example_args["body"] = required_body
        return {
            "operation_id": operation_id,
            "method": operation["method"],
            "path": operation["path"],
            "summary": operation["summary"],
            "description": operation["description"],
            "parameters": operation["parameters"],
            "request_body": operation["request_body"],
            "body_fields": body_fields,
            "auto_filled_body_fields": ["project_id"] if any(field["name"] == "project_id" for field in body_fields) else [],
            "terminal_invokable": operation["addressable"],
            "limitation": operation["limitation"],
            "example": f"/op {operation_id} {json.dumps(example_args, separators=(',', ':'))}" if example_args else f"/op {operation_id}",
            "access_note": "The API applies edition, permission, approval, budget, and receipt rules.",
        }

    def invoke(self, operation_id: str, arguments: Mapping[str, Any]) -> Any:
        records = self._records()
        if isinstance(records, dict):
            return records
        operation = next((item for item in records if item["operation_id"] == operation_id), None)
        if operation is None:
            return _command_error(f"unknown operationId: {operation_id}; use /ops SEARCH")
        if not operation["addressable"]:
            return _command_error(f"operation is not terminal invokable: {operation['limitation']}")
        catchall_parameters: set[str] = set()
        path_values = arguments.get("path")
        if isinstance(path_values, dict):
            for name, value in path_values.items():
                if isinstance(value, str) and (
                    any(part in {"", ".", ".."} for part in value.split("/"))
                    or "\\" in value or "%" in value
                ):
                    return _command_error(f"path parameter {name} contains an unsafe value")
        if isinstance(path_values, dict) and any("/" in str(value) for value in path_values.values()):
            catalog = self._catalog()
            if catalog.get("__http_error__") or catalog.get("__command_error__"):
                return catalog
            for domain in catalog["domains"]:
                for route in domain.get("routes", []):
                    if _route_key(route.get("method", ""), route.get("path", "")) == _route_key(operation["method"], operation["path"]):
                        catchall_parameters.update(re.findall(r"\{([A-Za-z_][A-Za-z_0-9]*):path\}", route["path"]))
        try:
            path, body = _request_path(
                operation, arguments, self.project_id, catchall_parameters=catchall_parameters
            )
        except ValueError as exc:
            return _command_error(str(exc))
        if isinstance(body, dict) and "project_id" not in body:
            fields = _body_fields(self._openapi_document or {}, operation["request_body"])
            if any(field["name"] == "project_id" for field in fields):
                body = {"project_id": self.project_id, **body}
        return self.request(operation["method"], path, body)

    def coverage(self) -> dict[str, Any]:
        catalog = self._catalog()
        if catalog.get("__http_error__") or catalog.get("__command_error__"):
            return catalog
        records = self._records()
        if isinstance(records, dict):
            return records
        domain_map = self._domain_map(catalog)
        documented_keys = {_route_key(r["method"], r["path"]) for r in records}
        operations = [
            {
                "source": "live_openapi",
                "domain": domain_map.get(_route_key(r["method"], r["path"]), "other"),
                "method": r["method"], "path": r["path"],
                "operation_id": r["operation_id"],
                "terminal_invokable": r["addressable"],
                "raw_api_invokable": r["path"].startswith("/v1/") and valid_v1_path(r["path"]),
                "limitation": r["limitation"],
                "edition_policy_access": "unverified_server_enforced",
            }
            for r in records
        ]
        for (method, path), domain in sorted(domain_map.items()):
            if (method, path) not in documented_keys:
                operations.append({
                    "source": "live_catalog_only", "domain": domain,
                    "method": method, "path": path, "operation_id": None,
                    "terminal_invokable": False,
                    "raw_api_invokable": path.startswith("/v1/") and valid_v1_path(path),
                    "limitation": "missing from live OpenAPI; use /api with explicit path and body",
                    "edition_policy_access": "unverified_server_enforced",
                })
        return {
            "source": ["/openapi.json", "/v1/meta/api-catalog"],
            "active_project_id": self.project_id,
            "discovered_operations": len(operations),
            "operation_id_invokable": sum(bool(r["terminal_invokable"]) for r in operations),
            "raw_api_invokable": sum(bool(r["raw_api_invokable"]) for r in operations),
            "edition_policy_access": "unverified_server_enforced",
            "operations": operations,
        }

    def dispatch(self, command: Command) -> Any:
        if command.kind == "empty":
            return None
        if command.kind == "help":
            return HELP_TEXT
        if command.kind == "quit":
            return None
        if command.kind == "error":
            return {"__command_error__": True, "message": command.message}
        if command.kind == "session":
            payload = self.session(command.message)
        elif command.kind == "pick":
            payload = self.pick(command.page)
        elif command.kind == "confirm":
            payload = self.confirm()
        elif command.kind == "watch":
            payload = self.watch()
        elif command.kind == "overwatch":
            payload = self.overwatch()
        elif command.kind == "drive":
            payload = self.drive(auto=command.auto)
        elif command.kind == "goals":
            payload = self.goals()
        elif command.kind == "projects":
            payload = self.projects()
        elif command.kind == "project":
            payload = self.project(command.message)
        elif command.kind == "tasks":
            payload = self.tasks(command.page)
        elif command.kind == "task":
            payload = self.task(command.message)
        elif command.kind == "task_create":
            payload = self.create_task(command.message, command.dedupe_key)
        elif command.kind == "decisions":
            payload = self.decisions(command.message, command.page)
        elif command.kind == "decision":
            payload = self.decision(command.message)
        elif command.kind == "decision_action":
            payload = self.decide(command.method, command.message, command.body["note"])
        elif command.kind == "ops":
            payload = self.ops(command.message)
        elif command.kind == "describe":
            payload = self.describe(command.message)
        elif command.kind == "op":
            payload = self.invoke(command.message, command.body)
        elif command.kind == "coverage":
            payload = self.coverage()
        elif command.kind == "api":
            payload = self.request(command.method, command.path, command.body)
        else:
            return {"__command_error__": True, "message": f"unknown command: {command.kind}"}
        return payload

    def handle(self, command: Command) -> str:
        payload = self.dispatch(command)
        return self.render(command, payload)

    def render(self, command: Command, payload: Any) -> str:
        if payload is None:
            return ""
        if isinstance(payload, dict) and payload.get("__command_error__"):
            return f"error: {payload['message']}"
        if isinstance(payload, dict) and payload.get("__http_error__"):
            status = payload.get("status")
            body = redact(str(payload.get("body") or ""), self.config.api_key)
            if status == 401 and "step_up_required" in body:
                return ("This needs you to confirm it is you (step-up). Sign in again in the "
                        "console — My account — then run the same command here.\n"
                        f"HTTP {status}: {body}")
            return f"HTTP {status}: {body}"
        if command.kind in {"session", "confirm"} or (
                command.kind == "pick" and isinstance(payload, dict) and "reply" in payload):
            return redact(format_agent_turn(payload, self.choices()), self.config.api_key)
        if command.kind == "pick" and isinstance(payload, dict):
            return redact(format_action_result(payload), self.config.api_key)
        if command.kind == "coverage" and isinstance(payload, dict):
            return (
                f"discovered: {payload['discovered_operations']}  operation IDs: {payload['operation_id_invokable']}"
                f"  raw API: {payload['raw_api_invokable']}\n"
                "Access is checked by the API on invocation; use --exec '/coverage' --json for every route."
            )
        if command.kind == "ops" and isinstance(payload, dict):
            return redact(format_ops(payload), self.config.api_key)
        if command.kind == "describe" and isinstance(payload, dict):
            return redact(format_description(payload), self.config.api_key)
        if command.kind == "projects" and isinstance(payload, dict):
            return redact(format_projects(payload), self.config.api_key)
        if command.kind == "tasks" and isinstance(payload, dict):
            return redact(format_tasks(payload, command.page), self.config.api_key)
        if command.kind == "task" and isinstance(payload, dict):
            return redact(format_task(payload), self.config.api_key)
        if command.kind == "decisions" and isinstance(payload, dict):
            return redact(format_decisions(payload), self.config.api_key)
        if command.kind in {"decision", "decision_action"} and isinstance(payload, dict):
            return redact(format_decision(payload), self.config.api_key)
        if command.kind in {"api", "op", "project"}:
            return redact(json.dumps(payload, indent=2, default=str), self.config.api_key)
        return redact(format_payload(payload), self.config.api_key)


def run_repl(
    client: MetatronClient,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
) -> int:
    incoming = stdin or sys.stdin
    outgoing = stdout or sys.stdout

    def emit(text: str) -> None:
        print(text, file=outgoing)

    emit(format_banner(client.config))
    while True:
        try:
            if incoming.isatty() and outgoing.isatty():
                outgoing.write("metatron> ")
                outgoing.flush()
            line = incoming.readline()
        except (EOFError, KeyboardInterrupt):
            emit("")
            return 0
        if line == "":
            emit("")
            return 0
        command = parse_line(line)
        if command.kind == "quit":
            return 0
        if command.kind == "empty":
            continue
        rendered = client.handle(command)
        if rendered:
            emit(rendered)
    return 0


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Metatron terminal and BOSS API console")
    parser.add_argument("--exec", dest="command", help="run one terminal command and exit")
    parser.add_argument("--json", action="store_true", help="emit the one-shot result as JSON")
    args = parser.parse_args(argv)
    if args.json and args.command is None:
        parser.error("--json requires --exec")
    try:
        config = load_config()
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    client = MetatronClient(config)
    if args.command is None:
        return run_repl(client)
    command = parse_line(args.command)
    if command.kind in {"empty", "quit"}:
        print("error: --exec requires an actionable command", file=sys.stderr)
        return 2
    if args.json:
        payload = client.dispatch(command)
        print(redact(json.dumps(payload, default=str), config.api_key))
    else:
        payload = client.dispatch(command)
        if isinstance(payload, dict) and payload.get("__command_error__"):
            print(client.render(command, payload), file=sys.stderr)
        elif isinstance(payload, dict) and payload.get("__http_error__"):
            print(client.render(command, payload), file=sys.stderr)
        else:
            print(client.render(command, payload))
    if isinstance(payload, dict) and payload.get("__command_error__"):
        return 2
    if isinstance(payload, dict) and payload.get("__http_error__"):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
