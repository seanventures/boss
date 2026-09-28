#!/usr/bin/env python3
"""Metatron captain TUI — stdlib REPL against /v1/metatron/*.

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

HELP_TEXT = (
    "Slash commands: /watch  /overwatch  /drive [auto|go]  /goals  /project [ID]  /quit\n"
    "API commands: /ops [domain or search]  /describe OPERATION_ID  /op OPERATION_ID [JSON]  /coverage\n"
    "Fallback: /api METHOD /v1/path [JSON]\n"
    "Type naturally to POST /v1/metatron/session.\n"
    "Use /ops to discover live API operations; /op accepts JSON with path, query, and body keys.\n"
    "The API enforces project, permission, edition, approval, budget, and receipt rules.\n"
    "/overwatch runs observe→measure→validate→apply-safe→verify.\n"
    "'go' or '/drive auto' applies only moves that pass validation."
)


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


HTTP_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE"}
PATH_PARAMETER = re.compile(r"\{([A-Za-z_][A-Za-z_0-9]*)(?::path)?\}")


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
    if lowered == "/project" or lowered.startswith("/project "):
        return Command(kind="project", message=raw.partition(" ")[2].strip())
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
    if lowered == "go" or lowered == "/go":
        return Command(kind="drive", auto=True)
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


def _body_fields(document: Mapping[str, Any], request_body: Mapping[str, Any]) -> list[dict[str, Any]]:
    content = request_body.get("content") or {}
    media = content.get("application/json") or {}
    schema = media.get("schema") or {}
    ref = schema.get("$ref")
    if isinstance(ref, str) and ref.startswith("#/components/schemas/"):
        name = ref.rsplit("/", 1)[-1]
        schema = (document.get("components") or {}).get("schemas", {}).get(name, schema)
    if not isinstance(schema, dict):
        return []
    required = set(schema.get("required") or [])
    return [
        {
            "name": name,
            "required": name in required,
            "type": field.get("type") or ("reference" if field.get("$ref") else "composite"),
            "description": field.get("description") or "",
        }
        for name, field in (schema.get("properties") or {}).items()
        if isinstance(field, dict)
    ]


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

    def session(self, message: str) -> Any:
        body: dict[str, Any] = {
            "project_id": self.project_id,
            "actor": self.config.actor,
            "message": message,
        }
        if self.thread_id:
            body["thread_id"] = self.thread_id
        payload = self.request("POST", "/v1/metatron/session", body)
        self._remember_thread(payload)
        return payload

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

    def project(self, project_id: str) -> dict[str, Any]:
        if project_id:
            if (
                "/" in project_id or "\\" in project_id or "?" in project_id
                or "#" in project_id or any(ord(c) < 32 or ord(c) == 127 for c in project_id)
            ):
                return _command_error("project ID contains an unsafe character")
            self.project_id = project_id
            self.thread_id = None  # Conversation context must not cross projects.
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
        if not term:
            return {
                "active_project_id": self.project_id,
                "domains": [
                    {"id": d.get("id"), "title": d.get("title"), "route_count": d.get("route_count")}
                    for d in catalog["domains"] if isinstance(d, dict) and d.get("route_count")
                ],
                "hint": "Use /ops DOMAIN or /ops SEARCH, then /describe OPERATION_ID and /op OPERATION_ID [JSON].",
            }
        records = self._records()
        if isinstance(records, dict):
            return records
        domain_map = self._domain_map(catalog)
        documented_keys = {_route_key(item["method"], item["path"]) for item in records}
        needle = term.casefold()
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
        return {"search": term, "count": len(matches), "operations": matches,
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
        elif command.kind == "watch":
            payload = self.watch()
        elif command.kind == "overwatch":
            payload = self.overwatch()
        elif command.kind == "drive":
            payload = self.drive(auto=command.auto)
        elif command.kind == "goals":
            payload = self.goals()
        elif command.kind == "project":
            payload = self.project(command.message)
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
        if payload is None:
            return ""
        if isinstance(payload, dict) and payload.get("__command_error__"):
            return f"error: {payload['message']}"
        if isinstance(payload, dict) and payload.get("__http_error__"):
            status = payload.get("status")
            body = redact(str(payload.get("body") or ""), self.config.api_key)
            return f"HTTP {status}: {body}"
        if command.kind == "coverage" and isinstance(payload, dict):
            return (
                f"discovered: {payload['discovered_operations']}  operation IDs: {payload['operation_id_invokable']}"
                f"  raw API: {payload['raw_api_invokable']}\n"
                "Access is checked by the API on invocation; use --exec '/coverage' --json for every route."
            )
        if command.kind in {"api", "ops", "describe", "op", "project"}:
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
            print(f"error: {payload['message']}", file=sys.stderr)
        elif isinstance(payload, dict) and payload.get("__http_error__"):
            print(f"HTTP {payload.get('status')}: {payload.get('body') or ''}", file=sys.stderr)
        else:
            if command.kind == "coverage" and isinstance(payload, dict):
                print(
                    f"discovered: {payload['discovered_operations']}  "
                    f"operation IDs: {payload['operation_id_invokable']}  "
                    f"raw API: {payload['raw_api_invokable']}\n"
                    "Access is checked by the API on invocation; use --json for every route."
                )
            elif command.kind in {"api", "ops", "describe", "op", "project"}:
                print(redact(json.dumps(payload, indent=2, default=str), config.api_key))
            else:
                print(redact(format_payload(payload), config.api_key))
    if isinstance(payload, dict) and payload.get("__command_error__"):
        return 2
    if isinstance(payload, dict) and payload.get("__http_error__"):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
