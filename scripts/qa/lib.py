"""Shared primitives for BOSS live QA gates and the unified harness.

Talks only to the BOSS control-plane HTTP API. Never prints API keys.
Never calls models, GitHub, or sandboxes directly.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

DEFAULT_API_URL = "https://boss-api.local.sean.network"
DEFAULT_PROJECT_ID = "boss-dogfood"
DEFAULT_ACTOR = "executive_console"
DEFAULT_TIMEOUT_SECONDS = 30.0


@dataclass
class CheckResult:
    name: str
    ok: bool
    required: bool = True
    detail: str = ""
    status_code: int | None = None
    duration_ms: int = 0
    evidence: dict[str, Any] = field(default_factory=dict)
    phase: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def redact_secrets(text: str, secrets: list[str] | None = None) -> str:
    if not text:
        return text
    redacted = text
    for secret in secrets or []:
        if secret and secret in redacted:
            redacted = redacted.replace(secret, "***")
    if "X-Boss-Api-Key" in redacted:
        redacted = redacted.replace("X-Boss-Api-Key", "X-Boss-Api-Key(redacted)")
    return redacted


def join_url(base: str, path: str) -> str:
    return f"{base.rstrip('/')}/{path.lstrip('/')}"


def is_2xx(status: int | None) -> bool:
    return status is not None and 200 <= status < 300


def item_count(payload: Any) -> int | None:
    if isinstance(payload, list):
        return len(payload)
    if not isinstance(payload, dict):
        return None
    for key in (
        "items",
        "decisions",
        "deadletters",
        "tasks",
        "projects",
        "jobs",
        "events",
        "receipts",
        "previews",
        "code_tasks",
        "benchmark_apps",
        "secrets",
        "connections",
        "bindings",
    ):
        value = payload.get(key)
        if isinstance(value, list):
            return len(value)
    return None


class BossClient:
    """Minimal control-plane client. Credentials stay in headers only."""

    def __init__(
        self,
        api_url: str,
        api_key: str | None = None,
        *,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        project_id: str = DEFAULT_PROJECT_ID,
        actor: str = DEFAULT_ACTOR,
    ) -> None:
        self.api_url = api_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self.project_id = project_id
        self.actor = actor

    def request(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
        timeout: float | None = None,
        authenticate: bool = True,
    ) -> tuple[int, Any, int]:
        from urllib.parse import urlencode

        url = join_url(self.api_url, path)
        if query:
            filtered = {k: v for k, v in query.items() if v is not None}
            if filtered:
                url = f"{url}?{urlencode(filtered)}"
        encoded = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Accept": "application/json"}
        if encoded is not None:
            headers["Content-Type"] = "application/json"
        if authenticate and self.api_key:
            headers["X-Boss-Api-Key"] = self.api_key
        started = time.monotonic()
        req = Request(url, data=encoded, headers=headers, method=method.upper())
        try:
            with urlopen(req, timeout=timeout or self.timeout) as response:  # noqa: S310
                raw = response.read().decode("utf-8")
                status = int(response.status)
        except HTTPError as error:
            raw = error.read().decode("utf-8", errors="replace")
            status = int(error.code)
        except URLError as error:
            elapsed = int((time.monotonic() - started) * 1000)
            raise RuntimeError(
                redact_secrets(
                    f"BOSS API {method} {path} unreachable: {error.reason}",
                    [self.api_key] if self.api_key else None,
                )
            ) from error
        elapsed = int((time.monotonic() - started) * 1000)
        payload: Any
        try:
            payload = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError:
            payload = {"raw": raw[:500]}
        return status, payload, elapsed


def summarize(checks: list[CheckResult]) -> dict[str, Any]:
    required = [c for c in checks if c.required]
    optional = [c for c in checks if not c.required]
    failed = [c.name for c in required if not c.ok]
    return {
        "total": len(checks),
        "required": len(required),
        "required_passed": sum(1 for c in required if c.ok),
        "optional_passed": sum(1 for c in optional if c.ok),
        "failed": failed,
        "ok": not failed,
    }


def env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}
