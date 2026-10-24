#!/usr/bin/env python3
"""Metatron — BOSS's terminal. A client of the one BOSS agent and the /v1 API.

Plain text is an Ask BOSS turn: POST /v1/metatron/session, a thin adapter onto the same
`agent_turn` the console calls (BOSSAPP-759). The agent's offers become numbered choices; picking
one goes through the console's own button path (POST /v1/engine/agent/action).

Reads BOSS_API_URL, BOSS_API_KEY, and BOSS_PROJECT_ID from the environment.
Never prints the API key.

THIS FILE IS THE WHOLE CLIENT, AND IT IS STDLIB. The rich terminal (BOSSAPP-761) is two optional
siblings: metatron_render.py (Rich: banner, Markdown replies, cards, tables, diffs, error panels)
and metatron_shell.py (prompt_toolkit: the bottom bar, completion, pickers). They only draw what
this client fetched and send what the person chose through this client's methods; neither has a
decision of its own. Without them, or without a terminal, or under NO_COLOR, the output is the
plain text it always was, and `--exec … --json` is byte-for-byte what it always was.

Usage:
  boss metatron                       (on the install machine)
  BOSS_API_KEY=… make metatron
  python3 scripts/metatron_tui.py [--plain] [--print COMMAND [--json]]
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import sys
import textwrap
from pathlib import Path
from dataclasses import dataclass
from typing import Any, Mapping, Optional, TextIO
import http.client
import socket
import time
from urllib.error import HTTPError, URLError
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


def _seconds(name: str, default: float) -> float:
    try:
        value = float(os.environ.get(name) or default)
    except ValueError:
        return default
    return value if value > 0 else default


#: A TURN IS NEVER FAILED FOR BEING SLOW. An Ask BOSS turn runs a tool loop and a model; on
#: 2026-10-23 one took 35 s to create three projects, and the 30 s read timeout every other
#: request uses reported "unreachable" for a turn that succeeded. Turns (a sentence, /confirm, a
#: pressed offer) wait this long instead, and if the connection still drops after the turn was
#: sent the client reads the thread for the reply rather than calling the server unreachable.
TURN_TIMEOUT_SECONDS = _seconds("BOSS_METATRON_TURN_TIMEOUT", 900.0)
#: How long a dropped turn's thread is read for its reply, and how often.
RECOVER_SECONDS = _seconds("BOSS_METATRON_RECOVER_SECONDS", 600.0)
RECOVER_POLL_SECONDS = _seconds("BOSS_METATRON_RECOVER_POLL", 3.0)
OPS_PAGE_SIZE = 25

HELP_TEXT = (
    "Type naturally to talk to the BOSS agent — the same agent, thread and actions as the "
    "console's Ask BOSS.\n"
    "Its offers are numbered: /pick N (or just N) runs one, /confirm dispatches a pending plan.\n"
    "Captain commands: /watch  /overwatch  /drive [auto|go]  /goals  — run only when typed.\n"
    "Slash commands: /projects  /project [ID]  /quit\n"
    "Work: /tasks [PAGE]  /task ID  /task create DEDUPE_KEY TITLE\n"
    "Decisions: /decisions [open|all] [PAGE]  /decision ID  /decision approve ID NOTE  /decision reject ID NOTE\n"
    "Browse the API: /api [domain or search|*] [--page N]  /api NUMBER  /api run NUMBER [JSON]\n"
    "By operation ID: /ops [domain or search|*] [--page N]  /describe OPERATION_ID  /op OPERATION_ID [JSON]  /coverage\n"
    "Raw path: /api METHOD /v1/path [JSON]\n"
    "/api with no method lists API domains; /api DOMAIN or /api * lists numbered operations.\n"
    "/api NUMBER shows one operation's path template and a ready example to copy.\n"
    "/api run NUMBER and /op accept JSON with path, query, and body keys.\n"
    "For typed JSON writes, /op inserts the selected project_id when the body omits it.\n"
    "The API enforces project, permission, edition, approval, budget, and receipt rules.\n"
    "/overwatch runs observe→measure→validate→apply-safe→verify.\n"
    "'/go' or '/drive auto' applies only moves that pass validation."
)

#: EVERY SLASH COMMAND, ONCE: (command, arguments, one line). The rich terminal's completion menu
#: and its /help table read this list, so a command cannot exist without its description.
SLASH_COMMANDS: tuple[tuple[str, str, str], ...] = (
    ("/help", "", "Commands and keys"),
    ("/agent", "[KEY|auto]", "Pick the agent you are talking to (Agent Studio)"),
    ("/project", "[ID]", "Pick or switch the project"),
    ("/projects", "", "List the projects this key can see"),
    ("/model", "[ENGINE[:MODEL]|auto]", "Pick the chat model (Setup › jobs › chat)"),
    ("/status", "", "Connection checklist: API, identity, project, models, GitHub"),
    ("/pick", "[N]", "Run one of the last reply's numbered choices"),
    ("/confirm", "", "Go ahead with the plan waiting on you"),
    ("/expand", "", "The last reply's tool calls, in full"),
    ("/new", "", "Start a new conversation (actor/role alone no longer forks one)"),
    ("/thread", "[ID]", "The conversation so far, from the server (and a slow turn's reply)"),
    ("/tasks", "[PAGE]", "Task list, 25 a page"),
    ("/task", "ID | create KEY TITLE", "Show one task, or create one"),
    ("/decisions", "[open|all] [PAGE]", "Decision inbox"),
    ("/decision", "ID | approve|reject ID NOTE", "Show a decision card, or act on it"),
    ("/runs", "[STATUS]", "Workflow runs"),
    ("/run", "ID", "One run's steps"),
    ("/follow", "ID", "Follow a run's steps live"),
    ("/alerts", "[firing|resolved|all]", "Alerts across apps"),
    ("/goals", "", "Captain goals"),
    ("/watch", "", "Captain: one watch tick"),
    ("/overwatch", "", "Captain: observe, measure, validate, apply-safe, verify"),
    ("/drive", "[auto|go]", "Captain: propose moves (auto applies validated ones)"),
    ("/go", "", "Captain: drive auto"),
    ("/ops", "[TERM] [--page N]", "Search the live API operations"),
    ("/describe", "OPERATION_ID", "An operation's parameters and body"),
    ("/op", "OPERATION_ID [JSON]", "Invoke an operation"),
    ("/api", "[TERM] | N | run N [JSON] | METHOD /v1/path [JSON]", "Browse the API and call an operation"),
    ("/coverage", "", "Live OpenAPI against the mounted routes"),
    ("/clear", "", "Clear the screen"),
    ("/quit", "", "Leave Metatron"),
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
ALERT_STATES = ("firing", "resolved", "all")
#: How long a startup check may take. The checklist is a welcome, not a gate.
CHECK_TIMEOUT_SECONDS = 5.0
PATH_PARAMETER = re.compile(r"\{([A-Za-z_][A-Za-z_0-9]*)(?::path)?\}")
SIMPLE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
#: A chooser pick: a row number from the last /api or /ops listing, or an operation ID.
MENU_SELECTOR = re.compile(r"(?:[0-9]{1,6}|[A-Za-z][A-Za-z0-9._:-]{0,127})\Z")


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


def redact_payload(value: Any, secret: str) -> Any:
    """`redact`, through a whole payload: what the rich terminal draws is scrubbed too."""
    if not secret:
        return value
    if isinstance(value, str):
        return value.replace(secret, "***")
    if isinstance(value, list):
        return [redact_payload(item, secret) for item in value]
    if isinstance(value, dict):
        return {redact_payload(k, secret): redact_payload(v, secret) for k, v in value.items()}
    return value


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


def _parse_op_arguments(text: str) -> tuple[Any, str]:
    """Shared JSON argument parsing for /op and /api run."""
    try:
        arguments = json.loads(text)
    except json.JSONDecodeError:
        return None, "arguments must be valid JSON"
    if not isinstance(arguments, dict) or set(arguments) - {"path", "query", "body"}:
        return None, "JSON must contain only path, query, and body keys"
    return arguments, ""


def parse_api_line(raw: str) -> Command:
    """Route /api to the chooser, one operation's template, a run, or a written-out path.

    NOBODY KNOWS THE ROUTE TABLE. /api used to demand a method and a path, which only
    worked if you already knew the route; the plain forms browse it and print the
    template. METHOD-first keeps the written-out call exactly as it was.
    """
    rest = raw.partition(" ")[2].strip()
    if not rest:
        return Command(kind="api_menu", message="")
    parts = rest.split(None, 2)
    head = parts[0]
    if head.lower() == "list":
        return Command(kind="api_menu", message=rest.partition(" ")[2].strip())
    if head.lower() == "run":
        if len(parts) < 2 or not MENU_SELECTOR.fullmatch(parts[1]):
            return Command(kind="error", message="usage: /api run NUMBER [JSON], with NUMBER from /api")
        arguments: Any = {}
        if len(parts) == 3:
            arguments, error = _parse_op_arguments(parts[2])
            if error:
                return Command(kind="error", message=f"/api run {error}")
        return Command(kind="api_run", message=parts[1], body=arguments)
    if re.fullmatch(r"[0-9]{1,6}", head):
        if len(parts) > 1:
            return Command(
                kind="error",
                message=f"usage: /api {head} to see the template, or /api run {head} [JSON] to invoke it",
            )
        if head == "0":
            return Command(kind="error", message="/api numbers start at 1; run /api to list operations")
        return Command(kind="api_template", message=head)
    if head.upper() in HTTP_METHODS:
        if len(parts) < 2:
            return Command(
                kind="error",
                message="usage: /api METHOD /v1/path [JSON], or /api SEARCH to browse operations",
            )
        method, path = head.upper(), parts[1]
        if not valid_v1_path(path):
            return Command(kind="error", message="/api path must be a local /v1/ route")
        body = None
        if len(parts) == 3:
            if method == "GET":
                return Command(kind="error", message="GET requests cannot include a JSON body")
            try:
                body = json.loads(parts[2])
            except json.JSONDecodeError:
                return Command(kind="error", message="/api body must be valid JSON")
        return Command(kind="api", method=method, path=path, body=body)
    if parts[0].startswith("/") or (len(parts) > 1 and parts[1].startswith("/")):
        return Command(kind="error", message="/api method must be GET, POST, PUT, PATCH, or DELETE")
    return Command(kind="api_menu", message=rest)


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
    if lowered in {"/agent", "/agents"} or lowered.startswith("/agent "):
        return Command(kind="agent", message=raw.partition(" ")[2].strip())
    if lowered in {"/model", "/models"} or lowered.startswith("/model "):
        return Command(kind="model", message=raw.partition(" ")[2].strip())
    if lowered == "/status":
        return Command(kind="status")
    if lowered == "/expand":
        return Command(kind="expand")
    if lowered == "/new":
        return Command(kind="new")
    if lowered == "/thread" or lowered.startswith("/thread "):
        parts = raw.split()
        if len(parts) > 2 or (len(parts) == 2 and not SIMPLE_ID.fullmatch(parts[1])):
            return Command(kind="error", message="usage: /thread [THREAD_ID]")
        return Command(kind="thread", message=parts[1] if len(parts) == 2 else "")
    if lowered == "/clear":
        return Command(kind="clear")
    if lowered == "/runs" or lowered.startswith("/runs "):
        parts = raw.split()
        if len(parts) > 2 or (len(parts) == 2 and not SIMPLE_ID.fullmatch(parts[1])):
            return Command(kind="error", message="usage: /runs [STATUS]")
        return Command(kind="runs", message=parts[1].lower() if len(parts) == 2 else "")
    if lowered in {"/run", "/follow"} or lowered.startswith(("/run ", "/follow ")):
        parts = raw.split()
        if len(parts) != 2 or not SIMPLE_ID.fullmatch(parts[1]):
            return Command(kind="error", message=f"usage: {parts[0].lower()} RUN_ID")
        return Command(kind="run", message=parts[1], auto=lowered.startswith("/follow"))
    if lowered == "/alerts" or lowered.startswith("/alerts "):
        parts = raw.split()
        if len(parts) > 2 or (len(parts) == 2 and parts[1].lower() not in ALERT_STATES):
            return Command(kind="error", message="usage: /alerts [firing|resolved|all]")
        return Command(kind="alerts", message=parts[1].lower() if len(parts) == 2 else "firing")
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
            arguments, error = _parse_op_arguments(parts[2])
            if error:
                return Command(kind="error", message=f"/op {error}")
        return Command(kind="op", message=parts[1], body=arguments)
    if lowered == "/api" or lowered.startswith("/api "):
        return parse_api_line(raw)
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


def _ok(payload: Any) -> bool:
    return isinstance(payload, dict) and not payload.get("__http_error__") \
        and not payload.get("__command_error__")


def build_status(api_url: str, project_id: str, *, health: Any, me: Any, projects: Any,
                 setup: Any, role_name: str = "") -> dict[str, Any]:
    """Six checks from four API reads. A read this key may not make is "unknown", not ✗."""
    checks: list[dict[str, Any]] = []

    def check(check_id: str, label: str, ok: Optional[bool], detail: str, fix: str = "") -> None:
        checks.append({"id": check_id, "label": label, "ok": ok, "detail": detail,
                       "fix": "" if ok else fix})

    reachable = _ok(health)
    if not reachable:
        check("api", "API reachable", False, f"no answer from {api_url}",
              "Start BOSS: `boss status` shows what is down (or `make dev` in the install).")
    elif health.get("database_connected") is False:
        check("api", "API reachable", False, "the API answers but its database does not",
              "`boss status`, then `boss logs db`.")
    else:
        check("api", "API reachable", True, api_url)

    if _ok(me):
        who = str(me.get("display_name") or me.get("principal_id") or "signed in")
        kind = str(me.get("kind") or "")
        check("identity", "Signed in", True, f"{who}" + (f" ({kind.replace('_', ' ')})" if kind else ""))
    elif isinstance(me, dict) and me.get("status") in {401, 403}:
        check("identity", "Signed in", False, "this key was refused",
              "On the install machine `boss identity host-key` issues a new one; elsewhere "
              "make a key in My account › API keys.")
    else:
        check("identity", "Signed in", None if not reachable else False,
              "could not read who this key is", "`boss doctor` checks sign-in.")

    project_name = project_id
    if _ok(projects) and isinstance(projects.get("items"), list):
        match = next((p for p in projects["items"]
                      if isinstance(p, dict) and p.get("id") == project_id), None)
        if match:
            project_name = str(match.get("name") or project_id)
            check("project", "Project", True, f"{project_name} ({project_id})")
        else:
            check("project", "Project", False, f"{project_id} is not visible to this key",
                  "`/project` picks one this key can see.")
    else:
        check("project", "Project", None, "could not list projects", "`/projects` retries.")

    model_label, budget = "", None
    if _ok(setup):
        budget = setup.get("budget") if isinstance(setup.get("budget"), dict) else None
        models = [m for m in setup.get("models") or [] if isinstance(m, dict)]
        chat = next((j for j in setup.get("jobs") or []
                     if isinstance(j, dict) and j.get("job") == "chat"), {})
        resolved = chat.get("resolved") if isinstance(chat.get("resolved"), dict) else {}
        model_label = str(resolved.get("label") or resolved.get("model") or "")
        green = [m for m in models if (m.get("pill") or {}).get("tone") == "green"]
        if green:
            check("model", "Model connected", True,
                  model_label or str(green[0].get("label") or green[0].get("provider") or "connected"))
        elif models:
            pill = models[0].get("pill") or {}
            check("model", "Model connected", False,
                  f"{models[0].get('label') or models[0].get('provider')}: {pill.get('label') or 'not working'}",
                  "`boss connect` finds model keys, subscriptions and local models here.")
        else:
            check("model", "Model connected", False, "no model connected",
                  "`boss connect` finds model keys, subscriptions and local models here.")

        embedding = setup.get("embedding_model")
        if isinstance(embedding, dict) and embedding.get("configured") and embedding.get("search"):
            check("embedding", "Embedding model", True,
                  " ".join(str(v) for v in (embedding.get("label"), embedding.get("model")) if v))
        elif isinstance(embedding, dict) and embedding.get("configured"):
            check("embedding", "Embedding model", False,
                  str(embedding.get("reason") or "set, but knowledge search is off"),
                  "Console: Setup › Repo and models › Embedding model.")
        else:
            check("embedding", "Embedding model", False, "not set; knowledge search is off",
                  "Console: Setup › Repo and models › Embedding model.")

        repository = setup.get("repository") if isinstance(setup.get("repository"), dict) else {}
        repos = [r for r in repository.get("repos") or [] if isinstance(r, dict)]
        default = next((r for r in repos if r.get("default")), repos[0] if repos else None)
        connection = repository.get("connection") if isinstance(repository.get("connection"), dict) else None
        github_fix = "Console: Setup › Repo and models › Repository (or `boss connect`)."
        if default and (default.get("pill") or {}).get("tone") == "green":
            check("github", "GitHub access", True, str(default.get("full_name")))
        elif default:
            check("github", "GitHub access", False,
                  f"{default.get('full_name')}: {(default.get('pill') or {}).get('label') or 'not checked'}",
                  github_fix)
        elif connection:
            login = connection.get("account_login") or connection.get("auth") or "connected"
            check("github", "GitHub access", False, f"connected as {login}; no repository chosen",
                  github_fix)
        else:
            check("github", "GitHub access", False, "not connected", github_fix)
    else:
        why = ("this key cannot read Setup" if isinstance(setup, dict) and setup.get("status") in {401, 403}
               else "could not read Setup")
        for check_id, label in (("model", "Model connected"), ("embedding", "Embedding model"),
                                ("github", "GitHub access")):
            check(check_id, label, None, why, "Open the console's Setup to check.")
    return {
        "api_url": api_url, "project_id": project_id, "checks": checks,
        "context": {"project": project_name, "agent": role_name or "BOSS",
                    "model": model_label, "budget": budget},
    }


def _version_key(path: Path) -> tuple[int, ...]:
    return tuple(int(n) for n in re.findall(r"\d+", path.stem))


def whats_new(roots: Optional[list[Path]] = None, limit: int = 4) -> Optional[dict[str, Any]]:
    """The newest release notes' headline and section names, for the welcome panel. Read
    from the install's own docs; absent docs mean no panel, never an error."""
    here = Path(__file__).resolve().parent
    candidates = roots or [here.parent / "docs" / "community", Path.cwd() / "docs" / "community"]
    for root in candidates:
        notes = sorted(root.glob("RELEASE_NOTES_v*.md"), key=_version_key) if root.is_dir() else []
        if not notes:
            continue
        newest = notes[-1]
        try:
            lines = newest.read_text(encoding="utf-8").splitlines()
        except OSError:
            return None
        title = next((ln[2:].strip() for ln in lines if ln.startswith("# ")), newest.stem)
        items = [ln[4:].strip() for ln in lines if ln.startswith("### ")][:limit]
        if not items:
            items = [re.sub(r"\*\*", "", ln[2:]).strip() for ln in lines
                     if ln.startswith("- **")][:limit]
        return {"release": title, "items": items, "source": f"docs/community/{newest.name}"}
    return None


def format_status(payload: Mapping[str, Any]) -> str:
    lines = [f"Connection ({payload.get('project_id')}):"]
    for item in payload.get("checks") or []:
        mark = {True: "✓", False: "✗", None: "–"}[item.get("ok")]
        lines.append(f"  {mark} {item['label']}: {item.get('detail') or ''}".rstrip())
        if item.get("fix"):
            lines.append(f"      fix: {item['fix']}")
    return "\n".join(lines)


def format_agents(payload: Mapping[str, Any]) -> str:
    if "items" not in payload:
        return str(payload.get("note") or json.dumps(payload, default=str))
    active = payload.get("active_role")
    lines = [f"Agents in {payload.get('project_id')} (talking to: {active or 'BOSS'}):"]
    for agent in payload["items"]:
        mark = "*" if agent.get("role_key") == active else " "
        mission = str(agent.get("mission") or "").strip()
        paused = " (paused)" if agent.get("lifecycle") == "paused" else ""
        lines.append(f"{mark} {agent['role_key']}: {agent.get('name') or agent['role_key']}{paused}"
                     + (f" — {mission[:80]}" if mission else ""))
    lines.append("Use /agent KEY to talk to one, /agent auto for BOSS.")
    return "\n".join(lines)


def format_models(payload: Mapping[str, Any]) -> str:
    resolved = payload.get("resolved") or {}
    lines = [f"Chat model in {payload.get('project_id')}: "
             f"{resolved.get('label') or resolved.get('model') or 'not resolved'}"
             + (" (Auto)" if not payload.get("choice") else "")]
    if resolved.get("why"):
        lines.append(f"  {resolved['why']}")
    options = payload.get("options") or []
    if options:
        lines.append("Choices:")
        for option in options:
            spec = option.get("engine") + (f":{option['model']}" if option.get("model") else "")
            metered = " (metered)" if option.get("metered") else ""
            lines.append(f"  {spec}  {option.get('label') or ''}{metered}".rstrip())
    lines.append("Use /model ENGINE[:MODEL] to choose, /model auto to let the router choose.")
    return "\n".join(lines)


def step_marks(steps: list[Mapping[str, Any]]) -> list[tuple[str, str]]:
    """(mark, name) for each step: ✓ done, ● running, ✗ failed, ○ waiting."""
    out = []
    for step in steps:
        status = str(step.get("status") or "").lower()
        mark = ("✓" if status in DONE_STATES else "✗" if status in FAILED_STATES
                else "●" if status in RUNNING_STATES else "○")
        out.append((mark, str(step.get("step_id") or step.get("name") or step.get("title") or "step")))
    return out


DONE_STATES = {"done", "succeeded", "success", "completed", "complete", "ok", "passed",
               "approved", "skipped", "merged"}
FAILED_STATES = {"failed", "error", "errored", "rejected", "cancelled", "canceled", "dead",
                 "blocked", "timed_out"}
RUNNING_STATES = {"running", "in_progress", "active", "leased", "started", "working",
                  "waiting_for_decision", "awaiting_approval", "paused"}


def format_run(payload: Mapping[str, Any]) -> str:
    steps = [s for s in payload.get("steps") or [] if isinstance(s, dict)]
    marks = step_marks(steps)
    done = sum(1 for mark, _ in marks if mark == "✓")
    lines = [f"Run {payload.get('id')}: {payload.get('workflow_name') or payload.get('workflow_key')} "
             f"[{payload.get('status')}]"]
    if marks:
        lines.append("  " + " › ".join(f"{m} {n}" for m, n in marks) + f"   {done}/{len(marks)}")
    for field in ("blocked_reason", "error"):
        if payload.get(field):
            lines.append(f"  {field.replace('_', ' ')}: {payload[field]}")
    return "\n".join(lines)


def format_runs(payload: Mapping[str, Any]) -> str:
    items = payload.get("items")
    if not isinstance(items, list):
        return json.dumps(payload, indent=2, default=str)
    lines = [f"Workflow runs in {payload.get('project_id')}:"]
    for run in items:
        if isinstance(run, dict):
            lines.append(f"  {run.get('id')}  [{run.get('status')}]  "
                         f"{run.get('workflow_name') or run.get('workflow_key')}  {run.get('created_at') or ''}".rstrip())
    if not items:
        lines.append("  No runs.")
    lines.append("Use /run ID for its steps.")
    return "\n".join(lines)


def format_alerts(payload: Mapping[str, Any]) -> str:
    alerts = payload.get("alerts")
    if not isinstance(alerts, list):
        return json.dumps(payload, indent=2, default=str)
    counts = payload.get("counts") or {}
    lines = [f"Alerts in {payload.get('project_id')} ({payload.get('state')}): "
             f"{counts.get('firing', 0)} firing, {counts.get('pending', 0)} pending, "
             f"{counts.get('needs_attention', 0)} need attention"]
    for alert in alerts:
        if isinstance(alert, dict):
            lines.append(f"  {alert.get('sev') or alert.get('severity') or '?'}  "
                         f"[{alert.get('state_word') or alert.get('state')}]  "
                         f"{alert.get('app') or ''}  {alert.get('summary') or alert.get('alertname') or alert.get('label') or ''}".rstrip())
    if not alerts:
        lines.append("  No alerts.")
    return "\n".join(lines)


def format_thread(payload: Mapping[str, Any]) -> str:
    lines = [f"Thread {payload.get('thread_id')}" + (" (BOSS is working)" if payload.get("working") else "") + ":"]
    for message in payload.get("messages") or []:
        if isinstance(message, dict) and message.get("role") in {"user", "assistant"}:
            who = "you" if message["role"] == "user" else "BOSS"
            lines.append(f"  {who}: {str(message.get('content') or '').strip()}")
    return "\n".join(lines)


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


def format_operation_rows(operations: Any) -> list[str]:
    """Numbered chooser rows, grouped by domain. The number is what /api NUMBER picks."""
    lines: list[str] = []
    domain = None
    for operation in operations:
        if operation.get("domain") and operation["domain"] != domain:
            domain = operation["domain"]
            lines.append(f"{domain}:")
        index = operation.get("index")
        marker = f"{index:>4}  " if index else "      "
        lines.append(f"{marker}{operation['method']:<6} {operation['path']}")
        summary = str(operation.get("summary") or "").strip()
        if summary:
            lines.append(f"        {summary}")
        if not operation["terminal_invokable"]:
            lines.append(f"        Limit: {operation.get('limitation') or 'not addressable by /op'}")
    return lines


def format_api_menu(payload: Mapping[str, Any]) -> str:
    """The chooser: the API's domains, or one listing of numbered operations."""
    if payload.get("view") == "domains":
        lines = [f"BOSS API domains on this install (project {payload['active_project_id']}):"]
        for domain in payload["domains"]:
            lines.append(
                f"  {domain['id']:<18} {domain['route_count']:>4}  {domain.get('title') or ''}".rstrip()
            )
        lines.append(textwrap.fill(str(payload["hint"]), width=78))
        return "\n".join(lines)
    noun = "operation" if payload["count"] == 1 else "operations"
    lines = [
        f"{payload['count']} {noun} for {payload['search']!r} "
        f"(page {payload['page']}/{payload['pages']}), project {payload['active_project_id']}:"
    ]
    if payload["domains"]:
        domains = "  ".join(f"{item['id']}({item['count']})" for item in payload["domains"])
        lines.append(textwrap.fill(f"Domains: {domains}", width=78, subsequent_indent="         "))
    lines.extend(format_operation_rows(payload["operations"]))
    if payload.get("next_page_command"):
        lines.append(f"Next: {payload['next_page_command']}")
    lines.append(str(payload["hint"]))
    lines.append("Narrow the list with /api SEARCH, or send a raw call with /api METHOD /v1/path [JSON].")
    return "\n".join(lines)


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
        index = operation.get("index")
        marker = f"{index:>4}  " if index else "  "
        lines.append(f"{marker}{alias}  {operation['method']} {operation['path']}")
        summary = str(operation.get("summary") or "").strip()
        if summary:
            lines.append(f"        {summary}")
        if not operation["terminal_invokable"]:
            lines.append(f"        Limit: {operation.get('limitation') or 'not addressable by /op'}")
    if payload["page"] < payload["pages"]:
        lines.append(f"Next: /ops {payload['search']} --page {payload['page'] + 1}")
    lines.append("Use /api NUMBER or /describe OPERATION_ID to see fields and a template before invoking.")
    return "\n".join(lines)


def format_description(payload: Mapping[str, Any]) -> str:
    heading = f"{payload['method']} {payload['path']}"
    if payload.get("index"):
        heading = f"[{payload['index']}] {heading}"
    lines = [heading]
    if payload.get("operation_id"):
        lines.append(f"Operation: {payload['operation_id']}")
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
    if payload.get("path_template"):
        lines.append("Path template:")
        lines.append(f"  {payload['method']} {payload['path_template']}")
    examples = [
        line for line in (
            payload.get("run_command"), payload.get("example"), payload.get("api_example"),
        ) if line
    ]
    if examples:
        lines.append("Copy one of these and replace every <value>:")
        lines.extend(f"  {line}" for line in examples)
    lines.append(str(payload["access_note"]))
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


def _path_template(path: str) -> str:
    """Turn {task_id} into <task_id> so the operator sees what to replace."""
    return PATH_PARAMETER.sub(lambda match: f"<{match.group(1)}>", path)


def _api_example(method: str, path: str, body: Any) -> str:
    """A written-out /api line the operator can copy, edit, and send."""
    line = f"/api {method} {path}"
    if method != "GET" and body is not None:
        line += f" {json.dumps(body, separators=(',', ':'))}"
    return line


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


def _dropped_after_sending(error: BaseException) -> bool:
    """Did the request reach BOSS and only the reply fail to come back?

    A refused connection or an unknown host never reached it: that is "unreachable". A read
    timeout, a reset, or a server that closed without answering happened after the turn was
    sent; the turn is the server's now, and calling the server unreachable is false."""
    reason = getattr(error, "reason", error) if isinstance(error, URLError) else error
    if isinstance(reason, (ConnectionRefusedError, socket.gaierror)):
        return False
    return isinstance(reason, (TimeoutError, socket.timeout, http.client.RemoteDisconnected,
                               http.client.IncompleteRead, ConnectionResetError,
                               ConnectionAbortedError, BrokenPipeError))


def _iso_seconds(value: Any) -> Optional[float]:
    from datetime import datetime

    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


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
        #: The listing a row number refers to, so /api NUMBER is never ambiguous.
        self._menu: list[dict[str, Any]] | None = None
        self._menu_search: str = "*"
        #: WHICH AGENT IS BEING ASKED (AgentChatRequest.role): unset is BOSS-in-general.
        self.role: Optional[str] = None
        self.role_name: str = ""
        #: Patch points for tests: the dropped-turn recovery's clock and wait.
        self._sleep = time.sleep
        self._clock = time.time

    def _opener(self):
        return self._injected_urlopen if self._injected_urlopen is not None else urlopen

    def request(self, method: str, path: str, body: Any = None, *,
                timeout: Optional[float] = None, turn: bool = False) -> Any:
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
            wait = timeout or (TURN_TIMEOUT_SECONDS if turn else DEFAULT_TIMEOUT_SECONDS)
            with self._opener()(request, timeout=wait) as response:
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
            if turn and _dropped_after_sending(error):
                # The server has the turn; only the wait for its answer ended.
                return {"__http_error__": True, "status": 0, "__turn_dropped__": True,
                        "body": f"the connection ended before BOSS answered ({reason_type}); "
                                "the turn is still running on the server"}
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
        if self.role:
            body["role"] = self.role
        sent_at = self._clock()
        payload = self.request("POST", "/v1/metatron/session", body, turn=True)
        if isinstance(payload, dict) and payload.get("__turn_dropped__"):
            payload = self.recover_turn(message, sent_at=sent_at)
        self._remember_thread(payload)
        if isinstance(payload, dict) and not payload.get("__http_error__"):
            self.last_turn = payload
        return payload

    def ensure_thread(self) -> Optional[str]:
        """The thread a turn will land in, before the turn: the console's own first call
        (/v1/agent/threads/ensure resolves the same key the turn does, role included), so
        progress can be read from the first second and a dropped turn can be found."""
        if self.thread_id:
            return self.thread_id
        payload = self.request("POST", "/v1/agent/threads/ensure", {
            "project_id": self.project_id, "actor": self.config.actor, "surface": "chat",
            **({"role": self.role} if self.role else {}),
        })
        self._remember_thread(payload)
        return self.thread_id

    def new_conversation(self) -> Any:
        """/new and --new: start over, explicitly.

        BOSS_ACTOR used to fork a conversation by naming a different one; under identity
        enforcement the actor is the signed-in principal, not whatever is claimed here, so
        three different BOSS_ACTOR values all landed back in this key's one thread. There is
        no implicit way to start fresh any more, so this asks for one outright
        (/v1/agent/threads/ensure with new_thread, the same key the next turn will resolve) and
        shows the new thread id immediately, the way the very first message does."""
        self.thread_id, self.last_turn = None, None
        payload = self.request("POST", "/v1/agent/threads/ensure", {
            "project_id": self.project_id, "actor": self.config.actor, "surface": "chat",
            "new_thread": True,
            **({"role": self.role} if self.role else {}),
        })
        self._remember_thread(payload)
        if isinstance(payload, dict) and not payload.get("__http_error__"):
            return {**payload, "note": f"Started a new conversation: thread {self.thread_id}."
                    + (f" (talking to {self.role_name})" if self.role else "")}
        return payload

    def thread_messages(self, thread_id: str) -> Any:
        query = urlencode({"project_id": self.project_id, "actor": self.config.actor})
        return self.request(
            "GET", f"/v1/agent/threads/{quote(thread_id, safe='')}/messages?{query}")

    def progress(self, thread_id: str, since: int = 0) -> Any:
        """What BOSS is doing in this thread right now (the console's progress poll)."""
        query = urlencode({"since": int(since or 0)})
        return self.request(
            "GET", f"/v1/agent/threads/{quote(thread_id, safe='')}/progress?{query}",
            timeout=CHECK_TIMEOUT_SECONDS)

    def recover_turn(self, message: str, *, sent_at: float) -> dict[str, Any]:
        """The connection dropped after a turn was sent: read its thread for the reply.

        A turn commits its messages when it finishes, so the reply is the first assistant
        message after a user message carrying this text that is new since the drop (or dated
        after the send). Bounded: past RECOVER_SECONDS the answer is "still running, here is
        the thread", never "unreachable"."""
        thread_id = self.thread_id or self.ensure_thread()
        if not thread_id:
            return {"__http_error__": True, "status": 0, "still_running": True,
                    "thread_id": None,
                    "body": "the connection ended after the turn was sent and BOSS has not "
                            "answered yet; it is still running on the server. /thread shows "
                            "its reply when it lands"}
        wanted = (message or "").strip()
        baseline: Optional[set[str]] = None
        deadline = self._clock() + RECOVER_SECONDS
        idle_polls = 0
        while True:
            payload = self.thread_messages(thread_id)
            if isinstance(payload, dict) and not payload.get("__http_error__"):
                history = [m for m in payload.get("messages") or [] if isinstance(m, dict)]
                if baseline is None:
                    baseline = {str(m.get("id")) for m in history}
                found = self._reply_after(history, wanted, baseline=baseline, sent_at=sent_at)
                if found is not None:
                    return {
                        "thread_id": thread_id,
                        "reply": str(found.get("content") or ""),
                        "actions": list(found.get("actions") or []),
                        "pending_confirmation": payload.get("pending_confirmation"),
                        "proposed_actions": list(payload.get("proposed_actions") or []),
                        "messages": [], "tool_calls": list(found.get("tool_calls") or []),
                        "next_steps": [], "cited_acts": [],
                        "recovered_from_thread": True,
                    }
                idle_polls = idle_polls + 1 if payload.get("working") is False else 0
                if idle_polls >= 3:
                    return {"__http_error__": True, "status": 0, "still_running": False,
                            "thread_id": thread_id,
                            "body": "the connection ended after the turn was sent, and the "
                                    "server reports nothing working in this thread now; "
                                    f"/thread {thread_id} shows what it recorded"}
            if self._clock() >= deadline:
                break
            self._sleep(RECOVER_POLL_SECONDS)
        return {"__http_error__": True, "status": 0, "still_running": True,
                "thread_id": thread_id,
                "body": f"BOSS is still working on this turn on the server (thread {thread_id}); "
                        f"its reply lands there: /thread {thread_id}"}

    @staticmethod
    def _reply_after(history: list[dict[str, Any]], wanted: str, *, baseline: set[str],
                     sent_at: float) -> Optional[dict[str, Any]]:
        for index in range(len(history) - 1, -1, -1):
            item = history[index]
            if item.get("role") != "user" or str(item.get("content") or "").strip() != wanted:
                continue
            created = _iso_seconds(item.get("created_at"))
            if str(item.get("id")) in baseline and (created is None or created < sent_at - 5):
                return None  # An earlier turn that said the same words.
            for later in history[index + 1:]:
                if later.get("role") == "assistant":
                    return later
            return None
        return None

    def thread(self, thread_id: str = "") -> Any:
        """The conversation so far (its last dozen messages), from the server's record."""
        target = thread_id or self.thread_id or self.ensure_thread()
        if not target:
            return _command_error("there is no conversation yet; say something first")
        payload = self.thread_messages(target)
        if isinstance(payload, dict) and not payload.get("__http_error__"):
            if not self.thread_id:
                self.thread_id = target
            payload = {**payload, "messages": list(payload.get("messages") or [])[-12:]}
        return payload

    def choices(self) -> list[dict[str, Any]]:
        """The last turn's numbered choices: its offers, then its next steps — in that order,
        the order they are printed in."""
        turn = self.last_turn if isinstance(self.last_turn, dict) else self._restored_turn()
        out: list[dict[str, Any]] = []
        for offer in turn.get("proposed_actions") or []:
            if isinstance(offer, dict) and offer.get("tool"):
                out.append({"kind": "action", **offer})
        for step in turn.get("next_steps") or []:
            if isinstance(step, dict) and step.get("message"):
                out.append({"kind": "next_step", **step})
        return out

    def _restored_turn(self) -> dict[str, Any]:
        """A fresh process (`--print '/pick 2'`, a script, a restart) has no last turn in memory,
        but the server keeps every reply's offers, its one resume step, and the thread's
        pending plan: read them, so a choice offered in one command can be picked in the next,
        as the console does on reload.

        Both kinds, not just the buttons: a turn that stopped short offers ONE next step
        ("Keep waiting", "Continue") instead of an action button, and it is restored the same
        way `proposed_actions` is -- from the newest reply, via GET .../messages. Dropping
        `next_steps` here left `choices()` with nothing to find for that offer, even though the
        server returned it, so `/pick 1` for "Keep waiting" said "nothing to pick" right after
        the console had shown exactly that choice."""
        thread_id = self.ensure_thread()
        if not thread_id:
            return {}
        payload = self.thread_messages(thread_id)
        if not isinstance(payload, dict) or payload.get("__http_error__"):
            return {}
        # The server's own "offers still standing": the newest reply's, as the console restores them.
        turn = {"proposed_actions": [o for o in payload.get("proposed_actions") or [] if isinstance(o, dict)],
                "next_steps": [s for s in payload.get("next_steps") or [] if isinstance(s, dict)],
                "pending_confirmation": payload.get("pending_confirmation")}
        self.last_turn = turn
        return turn

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
        payload = self.request("POST", "/v1/engine/agent/action", body, turn=True)
        if isinstance(payload, dict) and payload.get("__turn_dropped__"):
            where = f": /thread {self.thread_id}" if self.thread_id else ""
            payload = {**payload, "still_running": True, "thread_id": self.thread_id,
                       "body": f"{payload['body']}; the press is recorded in the "
                               f"conversation{where}"}
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
        turn = self.last_turn if isinstance(self.last_turn, dict) else self._restored_turn()
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

    # ── Who is asked, which model answers ────────────────────────────────────────────────────

    def agents(self) -> Any:
        """Agent Studio's agents in this project (a retired one cannot be asked)."""
        payload = self.request("GET", f"/v1/agents?{urlencode({'project_id': self.project_id})}")
        if isinstance(payload, dict) and payload.get("__http_error__"):
            return payload
        if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
            return _command_error("the API did not return the agent list")
        items = [a for a in payload["items"]
                 if isinstance(a, dict) and a.get("role_key") and a.get("lifecycle") != "retired"]
        return {"project_id": self.project_id, "active_role": self.role, "items": items}

    def agent(self, key: str) -> Any:
        """Address the next turns to one agent (AgentChatRequest.role), or back to BOSS."""
        if not key:
            return self.agents()
        if key.lower() in {"auto", "boss", "none", "off"}:
            changed = self.role is not None
            self.role, self.role_name = None, ""
            if changed:
                self.thread_id, self.last_turn = None, None
            return {"active_role": None, "name": "BOSS",
                    "note": "Talking to BOSS. Its agents still do the work it hands them."}
        listed = self.agents()
        if isinstance(listed, dict) and (listed.get("__http_error__") or listed.get("__command_error__")):
            return listed
        wanted = key.casefold()
        match = next((a for a in listed["items"] if str(a["role_key"]).casefold() == wanted
                      or str(a.get("name") or "").casefold() == wanted), None)
        if match is None:
            return _command_error(f"no agent {key!r} in {self.project_id}; /agent lists them")
        if match["role_key"] != self.role:
            # One agent's conversation is not another's (test_one_agents_history_is_not_anothers).
            self.thread_id, self.last_turn = None, None
        self.role, self.role_name = str(match["role_key"]), str(match.get("name") or match["role_key"])
        return {"active_role": self.role, "name": self.role_name,
                "mission": match.get("mission") or "",
                "note": f"Talking to {self.role_name}. /agent auto goes back to BOSS."}

    def _setup(self) -> Any:
        return self.request("GET", f"/v1/setup?{urlencode({'project_id': self.project_id})}",
                            timeout=CHECK_TIMEOUT_SECONDS)

    def _models_view(self, setup: Mapping[str, Any]) -> dict[str, Any]:
        jobs = [j for j in setup.get("jobs") or [] if isinstance(j, dict)]
        chat = next((j for j in jobs if j.get("job") == "chat"), {})
        return {
            "project_id": self.project_id,
            "job": "chat",
            "resolved": chat.get("resolved"),
            "choice": chat.get("choice"),
            "options": [o for o in chat.get("options") or [] if isinstance(o, dict)],
            "models": [m for m in setup.get("models") or [] if isinstance(m, dict)],
            "budget": setup.get("budget"),
        }

    def models(self) -> Any:
        """The chat job's model and the choices Setup offers for it (Repo and models)."""
        setup = self._setup()
        if isinstance(setup, dict) and setup.get("__http_error__"):
            return setup
        if not isinstance(setup, dict):
            return _command_error("the API did not return the setup view")
        return self._models_view(setup)

    def set_model(self, spec: str) -> Any:
        """Write the chat job's engine and model through Setup (PUT /v1/setup/jobs/chat), the
        same governed call the console's picker makes; "auto" hands it back to the router."""
        if not spec:
            return self.models()
        if spec.lower() == "auto":
            choice = None
        else:
            engine, _, model = spec.partition(":")
            if not SIMPLE_ID.fullmatch(engine) or (model and any(ord(c) < 32 for c in model)):
                return _command_error("usage: /model ENGINE[:MODEL], or /model auto")
            choice = {"engine": engine, "model": model or None}
        setup = self.request("PUT", "/v1/setup/jobs/chat", {
            "project_id": self.project_id, "actor": self.config.actor, "choice": choice})
        if isinstance(setup, dict) and setup.get("__http_error__"):
            return setup
        if not isinstance(setup, dict):
            return _command_error("the API did not return the setup view")
        return self._models_view(setup)

    # ── Runs, alerts, the checklist ──────────────────────────────────────────────────────────

    def runs(self, status: str = "") -> Any:
        query = {"project_id": self.project_id, "limit": OPS_PAGE_SIZE}
        if status:
            query["status"] = status
        return self.request("GET", f"/v1/workflow-runs?{urlencode(query)}")

    def run(self, run_id: str) -> Any:
        payload = self.request("GET", f"/v1/workflow-runs/{quote(run_id, safe='')}")
        if isinstance(payload, dict) and isinstance(payload.get("project_id"), str) \
                and payload["project_id"] != self.project_id:
            return _command_error("run is outside the selected project; use /project ID first")
        return payload

    def alerts(self, state: str = "firing") -> Any:
        query = urlencode({"project_id": self.project_id, "state": state or "firing"})
        return self.request("GET", f"/v1/alerts?{query}")

    def expand(self) -> Any:
        turn = self.last_turn if isinstance(self.last_turn, dict) else {}
        calls = [c for c in turn.get("tool_calls") or [] if isinstance(c, dict)]
        done = [a for a in turn.get("actions") or [] if isinstance(a, dict)]
        if not calls and not done:
            return _command_error("the last reply made no tool or action calls")
        return {"thread_id": self.thread_id, "tool_calls": calls, "actions": done}

    def status(self) -> dict[str, Any]:
        """The welcome checklist, from the API's own reads (never a guess): reachable, who this
        key is, the project, the chat model, the embedding model, GitHub. Each ✗ carries the
        one-line fix the install already documents."""
        from concurrent.futures import ThreadPoolExecutor

        project_query = urlencode({"project_id": self.project_id})
        with ThreadPoolExecutor(max_workers=4) as pool:
            health_f = pool.submit(self.request, "GET", "/health", timeout=CHECK_TIMEOUT_SECONDS)
            me_f = pool.submit(self.request, "GET", "/v1/identity/me", timeout=CHECK_TIMEOUT_SECONDS)
            projects_f = pool.submit(self.request, "GET", "/v1/projects", timeout=CHECK_TIMEOUT_SECONDS)
            setup_f = pool.submit(self.request, "GET", f"/v1/setup?{project_query}",
                                  timeout=CHECK_TIMEOUT_SECONDS)
            health, me, projects, setup = (health_f.result(), me_f.result(),
                                           projects_f.result(), setup_f.result())
        return build_status(self.config.api_url, self.project_id, health=health, me=me,
                            projects=projects, setup=setup, role_name=self.role_name)

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

    def _split_page(self, term: str, usage: str) -> tuple[str, int] | dict[str, Any]:
        page = 1
        if "--page" in term:
            match = re.search(r"(?:^|\s)--page(?:=|\s+)([1-9][0-9]{0,5})\s*$", term)
            if not match:
                return _command_error(usage)
            page = int(match.group(1))
            term = term[:match.start()].strip()
        return term, page

    def _remember_menu(self, term: str, matches: list[dict[str, Any]]) -> None:
        """A row number addresses the listing the person just saw, nothing else."""
        self._menu = matches
        self._menu_search = term or "*"

    def _menu_entry(self, selector: str) -> dict[str, Any]:
        """Resolve a row number from the last listing, or an operation ID."""
        if self._menu is None:
            matches = self._match_operations("")
            if isinstance(matches, dict):
                return matches
            self._remember_menu("", matches)
        menu = self._menu or []
        if not menu:
            return _command_error("no operations are listed; run /api to list the API")
        if re.fullmatch(r"[0-9]{1,6}", selector):
            index = int(selector)
            if not 1 <= index <= len(menu):
                return _command_error(
                    f"pick 1-{len(menu)} from the {self._menu_search!r} listing, "
                    "or run /api [SEARCH] to list again"
                )
            return menu[index - 1]
        entry = next((item for item in menu if item["operation_id"] == selector), None)
        if entry is None:
            return _command_error(
                f"{selector} is not in the {self._menu_search!r} listing; "
                "use /api [SEARCH] to list, then /api NUMBER"
            )
        return entry

    def api_rows(self, term: str = "") -> list[dict[str, Any]] | dict[str, Any]:
        """Every match for one search, numbered and remembered. The rich picker's source."""
        matches = self._match_operations(term)
        if isinstance(matches, dict):
            return matches
        self._remember_menu(term, matches)
        return matches

    def api_menu(self, term: str = "") -> dict[str, Any]:
        """The chooser. No method means browse: domains first, then numbered operations."""
        catalog = self._catalog()
        if catalog.get("__http_error__") or catalog.get("__command_error__"):
            return catalog
        split = self._split_page(term, "usage: /api [domain or search|*] [--page N], with N >= 1")
        if isinstance(split, dict):
            return split
        term, page = split
        if not term:
            # THE LIVE API MOUNTS HUNDREDS OF ROUTES. Page 1 of 40 is not a menu; the
            # domain the person is thinking about is.
            if page != 1:
                return _command_error("use /api * --page N to browse every operation")
            return {
                "view": "domains",
                "active_project_id": self.project_id,
                "domains": [
                    {"id": d.get("id"), "title": d.get("title"), "route_count": d.get("route_count")}
                    for d in catalog["domains"] if isinstance(d, dict) and d.get("route_count")
                ],
                "hint": (
                    "Pick a domain with /api DOMAIN, search with /api SEARCH, or list "
                    "everything with /api *; then /api NUMBER for one operation."
                ),
            }
        matches = self._match_operations(term)
        if isinstance(matches, dict):
            return matches
        if not matches:
            return _command_error(
                f"no operation matches {term!r}; run /api to list the domains "
                "or /api * for every operation"
            )
        pages = max(1, (len(matches) + OPS_PAGE_SIZE - 1) // OPS_PAGE_SIZE)
        if page > pages:
            return _command_error(f"page {page} is past the last page ({pages})")
        self._remember_menu(term, matches)
        counts: dict[str, int] = {}
        for match in matches:
            counts[match["domain"]] = counts.get(match["domain"], 0) + 1
        return {
            "view": "operations",
            "search": term or "*",
            "active_project_id": self.project_id,
            "count": len(matches),
            "page": page,
            "pages": pages,
            "page_size": OPS_PAGE_SIZE,
            "domains": [{"id": name, "count": counts[name]} for name in sorted(counts)],
            "operations": matches[(page - 1) * OPS_PAGE_SIZE:page * OPS_PAGE_SIZE],
            "next_page_command": (
                " ".join(part for part in ("/api", term, f"--page {page + 1}") if part)
                if page < pages else None
            ),
            "hint": "Pick one with /api NUMBER to see its path template and example.",
            "access_note": "The API applies edition, permission, and policy checks when invoked.",
        }

    def api_template(self, selector: str) -> dict[str, Any]:
        """The picked operation's card: path template, fields, and lines to copy."""
        entry = self._menu_entry(selector)
        if entry.get("__command_error__") or entry.get("__http_error__"):
            return entry
        if entry["operation_id"]:
            payload = self.describe(entry["operation_id"])
            if payload.get("__command_error__") or payload.get("__http_error__"):
                return payload
        else:
            template = _path_template(entry["path"])
            payload = {
                "operation_id": None,
                "method": entry["method"],
                "path": entry["path"],
                "summary": entry["summary"],
                "description": "",
                "parameters": [],
                "parameter_fields": [],
                "body_fields": [],
                "auto_filled_body_fields": [],
                "terminal_invokable": False,
                "limitation": entry["limitation"],
                "path_template": template,
                "example": None,
                "api_example": _api_example(entry["method"], template, None),
                "access_note": "The API applies edition, permission, approval, budget, and receipt rules.",
            }
        payload["index"] = entry["index"]
        payload["menu_search"] = self._menu_search
        run_command = None
        if entry["operation_id"] and entry["terminal_invokable"]:
            arguments = payload.get("example_arguments") or {}
            run_command = f"/api run {entry['index']}"
            if arguments:
                run_command += f" {json.dumps(arguments, separators=(',', ':'))}"
        payload["run_command"] = run_command
        return payload

    def api_run(self, selector: str, arguments: Mapping[str, Any]) -> Any:
        """Invoke the picked row. The same invoke() /op uses — no second path to the API."""
        entry = self._menu_entry(selector)
        if isinstance(entry, dict) and (entry.get("__command_error__") or entry.get("__http_error__")):
            return entry
        if not entry["operation_id"]:
            return _command_error(
                f"{entry['method']} {entry['path']} has no typed operation; "
                f"use /api {entry['method']} {_path_template(entry['path'])} [JSON]"
            )
        return self.invoke(entry["operation_id"], arguments)

    def _match_operations(self, term: str) -> list[dict[str, Any]] | dict[str, Any]:
        """One numbered match list behind /ops, /api browsing, and every pick."""
        catalog = self._catalog()
        if catalog.get("__http_error__") or catalog.get("__command_error__"):
            return catalog
        records = self._records()
        if isinstance(records, dict):
            return records
        domain_map = self._domain_map(catalog)
        documented_keys = {_route_key(item["method"], item["path"]) for item in records}
        needle = "" if term in {"", "*"} else term.casefold()
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
        for index, match in enumerate(matches, start=1):
            match["index"] = index
        return matches

    def ops(self, term: str = "") -> dict[str, Any]:
        catalog = self._catalog()
        if catalog.get("__http_error__") or catalog.get("__command_error__"):
            return catalog
        split = self._split_page(term, "usage: /ops [domain or search|*] [--page N], with N >= 1")
        if isinstance(split, dict):
            return split
        term, page = split
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
        matches = self._match_operations(term)
        if isinstance(matches, dict):
            return matches
        pages = max(1, (len(matches) + OPS_PAGE_SIZE - 1) // OPS_PAGE_SIZE)
        if page > pages:
            return _command_error(f"page {page} is past the last page ({pages})")
        self._remember_menu(term, matches)
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
        # THE WRITTEN-OUT FORM INFERS NOTHING, so the template spells out every
        # required query value and body field, project included.
        raw_path = _path_template(operation["path"])
        if required_query:
            raw_path += "?" + "&".join(f"{name}={value}" for name, value in required_query.items())
        raw_body = (
            required_body
            if operation["request_body"].get("required") or required_body
            else None
        )
        return {
            "operation_id": operation_id,
            "method": operation["method"],
            "path": operation["path"],
            "path_template": raw_path,
            "parameter_fields": [
                {"in": p.get("in"), "name": p.get("name"), "required": bool(p.get("required")),
                 "type": _field_type(p.get("schema") or {})}
                for p in operation["parameters"] if isinstance(p, dict)
            ],
            "summary": operation["summary"],
            "description": operation["description"],
            "parameters": operation["parameters"],
            "request_body": operation["request_body"],
            "body_fields": body_fields,
            "auto_filled_body_fields": ["project_id"] if any(field["name"] == "project_id" for field in body_fields) else [],
            "terminal_invokable": operation["addressable"],
            "limitation": operation["limitation"],
            "example": f"/op {operation_id} {json.dumps(example_args, separators=(',', ':'))}" if example_args else f"/op {operation_id}",
            "example_arguments": example_args,
            "api_example": _api_example(operation["method"], raw_path, raw_body),
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
        elif command.kind == "api_menu":
            payload = self.api_menu(command.message)
        elif command.kind == "api_template":
            payload = self.api_template(command.message)
        elif command.kind == "api_run":
            payload = self.api_run(command.message, command.body)
        elif command.kind == "api":
            payload = self.request(command.method, command.path, command.body)
        elif command.kind == "agent":
            payload = self.agent(command.message)
        elif command.kind == "model":
            payload = self.set_model(command.message)
        elif command.kind == "status":
            payload = self.status()
        elif command.kind == "expand":
            payload = self.expand()
        elif command.kind == "new":
            payload = self.new_conversation()
        elif command.kind == "thread":
            payload = self.thread(command.message)
        elif command.kind == "runs":
            payload = self.runs(command.message)
        elif command.kind == "run":
            payload = self.run(command.message)
        elif command.kind == "alerts":
            payload = self.alerts(command.message)
        elif command.kind == "clear":
            return None
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
            if "still_running" in payload or payload.get("__turn_dropped__"):
                # Sent and not answered in time is not a failure of the server: no "HTTP 0".
                return f"Still working: {body}"
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
        if command.kind == "api_menu" and isinstance(payload, dict):
            return redact(format_api_menu(payload), self.config.api_key)
        if command.kind in {"describe", "api_template"} and isinstance(payload, dict):
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
        plain = {"status": format_status, "agent": format_agents, "model": format_models,
                 "runs": format_runs, "run": format_run, "alerts": format_alerts,
                 "thread": format_thread}
        if command.kind in plain and isinstance(payload, dict):
            return redact(plain[command.kind](payload), self.config.api_key)
        if command.kind in {"api", "api_run", "op", "project", "expand"}:
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


def sibling(name: str):
    """metatron_render / metatron_shell, loaded from beside this file (it is run as a script,
    or loaded by path, so the scripts directory is not necessarily importable)."""
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, Path(__file__).resolve().with_name(f"{name}.py"))
    if spec is None or spec.loader is None:
        raise ImportError(name)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    return module


def rich_terminal(stream: TextIO, *, plain: bool = False, need_input: bool = True,
                  env: Mapping[str, str] | None = None) -> bool:
    """The rich terminal only where it can work: a real terminal (both ends for the
    interactive shell), not TERM=dumb, not --plain, and Rich + prompt_toolkit installed.
    Everything else is the plain text Metatron always printed. NO_COLOR keeps the layout and
    drops every escape sequence (metatron_render.make_console)."""
    source = os.environ if env is None else env
    if plain or source.get("BOSS_METATRON_PLAIN") or source.get("TERM") == "dumb":
        return False
    try:
        if not stream.isatty() or (need_input and not sys.stdin.isatty()):
            return False
    except (AttributeError, ValueError):
        return False
    return all(importlib.util.find_spec(m) is not None for m in ("rich", "prompt_toolkit"))


TURN_KINDS = {"session", "confirm", "pick"}


class _Ticker:
    """'BOSS is working · 12s' on stderr while a one-shot turn runs, only when stderr is a
    terminal, so a script's stdout (and --json) is exactly the result and nothing else."""

    def __init__(self, active: bool):
        self.active = active and _isatty(sys.stderr)
        self._stop = None

    def __enter__(self):
        if self.active:
            import threading

            self._stop = threading.Event()
            started = time.monotonic()

            def tick() -> None:
                while not self._stop.wait(1.0):
                    sys.stderr.write(f"\rBOSS is working · {int(time.monotonic() - started)}s ")
                    sys.stderr.flush()

            threading.Thread(target=tick, daemon=True).start()
        return self

    def __exit__(self, *exc: object) -> None:
        if self._stop is not None:
            self._stop.set()
            sys.stderr.write("\r\033[K")
            sys.stderr.flush()


def _isatty(stream: Any) -> bool:
    try:
        return bool(stream.isatty())
    except (AttributeError, ValueError):
        return False


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="Metatron: BOSS's terminal. Interactive by default; --print runs one command.")
    parser.add_argument("--exec", "--print", "-p", dest="command", metavar="COMMAND",
                        help="run one terminal command (a sentence or a /command) and exit")
    parser.add_argument("--new", action="store_true",
                        help="start a new conversation first (see /new); with --exec, "
                             "that command then runs in it")
    parser.add_argument("--json", action="store_true", help="emit the one-shot result as JSON")
    parser.add_argument("--plain", action="store_true",
                        help="plain text: no colours, panels or bottom bar")
    args = parser.parse_args(argv)
    if args.json and args.command is None:
        parser.error("--json requires --exec")
    try:
        config = load_config()
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    client = MetatronClient(config)
    if args.new:
        started = client.new_conversation()
        if isinstance(started, dict) and started.get("__http_error__"):
            print(f"error: could not start a new conversation: {started.get('body')}",
                  file=sys.stderr)
            return 1
    if args.command is None:
        if rich_terminal(sys.stdout, plain=args.plain):
            return int(sibling("metatron_shell").run(sys.modules[__name__], client))
        return run_repl(client)
    command = parse_line(args.command)
    if command.kind in {"empty", "quit"}:
        print("error: --exec requires an actionable command", file=sys.stderr)
        return 2
    if args.json:
        with _Ticker(command.kind in TURN_KINDS):
            payload = client.dispatch(command)
        print(redact(json.dumps(payload, default=str), config.api_key))
    elif rich_terminal(sys.stdout, plain=args.plain, need_input=False):
        render = sibling("metatron_render")
        console = render.make_console(sys.stdout)
        with render.working(render.make_console(sys.stderr), active=command.kind in TURN_KINDS):
            payload = client.dispatch(command)
        render.print_result(console, client, command, payload)
    else:
        with _Ticker(command.kind in TURN_KINDS):
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
