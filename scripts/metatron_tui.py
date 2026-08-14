#!/usr/bin/env python3
"""Metatron captain TUI — stdlib REPL against /v1/metatron/*.

Reads BOSS_API_URL, BOSS_API_KEY, and BOSS_PROJECT_ID from the environment.
Never prints the API key.

Usage:
  BOSS_API_KEY=… make metatron
  python3 scripts/metatron_tui.py
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from typing import Any, Mapping, Optional, TextIO
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request
from urllib.request import urlopen as default_urlopen

# Patch point for tests. Live runs use urllib.request.urlopen.
urlopen = default_urlopen

DEFAULT_API_URL = "http://127.0.0.1:8000"
DEFAULT_PROJECT_ID = "boss-dogfood"
DEFAULT_ACTOR = "executive_console"
DEFAULT_TIMEOUT_SECONDS = 30.0

HELP_TEXT = (
    "Slash commands: /watch  /overwatch  /drive [auto|go]  /goals  /quit\n"
    "Type naturally to POST /v1/metatron/session.\n"
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


def redact(text: str, secret: str = "") -> str:
    """Strip a known secret from any operator-visible string."""
    if not text:
        return text
    redacted = text
    if secret and secret in redacted:
        redacted = redacted.replace(secret, "***")
    return redacted


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


class MetatronClient:
    """Thin HTTP client for the captain surface. Inject urlopen in tests."""

    def __init__(self, config: Config, urlopen=None):
        self.config = config
        self.thread_id: Optional[str] = None
        self._injected_urlopen = urlopen

    def _opener(self):
        return self._injected_urlopen if self._injected_urlopen is not None else urlopen

    def request(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        url = join_url(self.config.api_url, path)
        encoded = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {
            "Accept": "application/json",
            "X-Boss-Api-Key": self.config.api_key,
        }
        if encoded is not None:
            headers["Content-Type"] = "application/json"
        request = Request(url, data=encoded, headers=headers, method=method)
        try:
            with self._opener()(request, timeout=DEFAULT_TIMEOUT_SECONDS) as response:
                raw = response.read().decode("utf-8", errors="replace")
                status = int(getattr(response, "status", 200) or 200)
        except HTTPError as error:
            raw = error.read().decode("utf-8", errors="replace")
            status = int(error.code)
            return {"__http_error__": True, "status": status, "body": raw}
        except URLError as error:
            reason = redact(str(getattr(error, "reason", error)), self.config.api_key)
            return {"__http_error__": True, "status": 0, "body": f"unreachable: {reason}"}
        raw = redact(raw, self.config.api_key)
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
            "project_id": self.config.project_id,
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
            {"project_id": self.config.project_id, "actor": self.config.actor},
        )

    def drive(self, *, auto: bool) -> Any:
        return self.request(
            "POST",
            "/v1/metatron/drive",
            {
                "project_id": self.config.project_id,
                "actor": self.config.actor,
                "auto": bool(auto),
            },
        )

    def overwatch(self) -> Any:
        return self.request(
            "POST",
            "/v1/metatron/overwatch",
            {"project_id": self.config.project_id, "actor": self.config.actor},
        )

    def goals(self) -> Any:
        query = urlencode({"project_id": self.config.project_id})
        return self.request("GET", f"/v1/metatron/goals?{query}")

    def handle(self, command: Command) -> str:
        if command.kind == "empty":
            return ""
        if command.kind == "help":
            return HELP_TEXT
        if command.kind == "quit":
            return ""
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
        else:
            return f"unknown command: {command.kind}"
        if isinstance(payload, dict) and payload.get("__http_error__"):
            status = payload.get("status")
            body = redact(str(payload.get("body") or ""), self.config.api_key)
            return f"HTTP {status}: {body}"
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
    del argv  # reserved for a future non-interactive flag
    try:
        config = load_config()
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return run_repl(MetatronClient(config))


if __name__ == "__main__":
    raise SystemExit(main())
