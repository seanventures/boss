"""Shared helpers for the Project experience reads: hrefs, labels, times, enums."""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Optional
from urllib.parse import urlencode

from sqlalchemy import text
from sqlalchemy.engine import Connection

#: The shared enums (contract "Shared enums").
STAGES = ("onboarding", "ready", "backlog", "working", "staging", "production")
STAGE_LABELS = {"onboarding": "Onboarded", "ready": "Ready", "backlog": "Backlog",
                "working": "Working", "staging": "Staging", "production": "Production"}
STATUS_LABELS = {"onboarding": "Onboarding", "ready": "Ready for work", "backlog": "Backlog ready",
                 "working": "Working", "staging": "On staging", "production": "In production"}
WORK_STATES = ("planned", "running", "stalled", "needs_you", "done", "failed")
HEALTH = ("healthy", "degraded", "down", "unknown")

LIVE_RUN = ("queued", "validating", "running", "waiting", "paused", "blocked")
FAILED_RUN = ("failed", "deadlettered")


def href(project_id: Optional[str] = None, **params: Any) -> str:
    """A console URL in the shared `consoleHref` query shape (`?project_id=…&area=…`)."""
    out: dict[str, str] = {}
    if project_id:
        out["project_id"] = str(project_id)
    for key, value in params.items():
        if value is not None and value != "":
            out[key] = str(value)
    return "?" + urlencode(out)


def project_href(project_id: str, tab: Optional[str] = None, drawer: Optional[str] = None) -> str:
    return href(project_id, area="project", tab=tab, drawer=drawer)


def web_url(value: Any) -> Optional[str]:
    """THE ONE PLACE an app's address becomes a link: a bare `203.0.113.20:8004` (what a deploy
    records as its open URL) gets `http://`, so a link opens the app, never
    `<console>/203.0.113.20:8004`. None stays None."""
    raw = str(value or "").strip()
    if not raw:
        return None
    if re.match(r"^[a-z][a-z0-9+.-]*://", raw, re.I):
        return raw
    return f"http://{raw.lstrip('/')}"


def words(value: Any, limit: int = 4) -> str:
    """A node label: at most `limit` words (contract: every label ≤ 4 words)."""
    parts = str(value or "").split()
    return " ".join(parts[:limit])


def initials(name: str) -> str:
    parts = [p for p in re.split(r"[\s\-_]+", str(name or "").strip()) if p]
    if not parts:
        return "?"
    capitals = [c for c in parts[0] if c.isupper()]
    if len(capitals) >= 2:          # "HelpDesk AI Demo" -> HD
        return (capitals[0] + capitals[1]).upper()
    if len(parts) == 1:
        return parts[0][:2].upper()
    return (parts[0][0] + parts[1][0]).upper()


def iso(value: Any) -> Optional[str]:
    if value is None:
        return None
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def aware(value: Any) -> Optional[datetime]:
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def age(value: Any, now: Optional[datetime] = None) -> Optional[str]:
    """`12m`, `3h`, `2d`: how long ago, in one short token."""
    at = aware(value)
    if at is None:
        return None
    seconds = max(0, int(((now or datetime.now(timezone.utc)) - at).total_seconds()))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def short_day(value: Any) -> Optional[str]:
    at = aware(value)
    return f"{at.strftime('%b')} {at.day}" if at else None


def as_json(value: Any, default: Any = None) -> Any:
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def pr_number(url: Any) -> Optional[int]:
    found = re.search(r"/pull/(\d+)", str(url or ""))
    return int(found.group(1)) if found else None


def project_row(connection: Connection, project_id: str) -> Optional[dict[str, Any]]:
    row = connection.execute(text(
        "SELECT id, name, description, project_type, created_at, archived_at FROM projects "
        "WHERE id = :p"), {"p": project_id}).mappings().first()
    return dict(row) if row else None


def require_project(connection: Connection, project_id: str) -> dict[str, Any]:
    row = project_row(connection, project_id)
    if row is None:
        raise LookupError(f"There is no project {project_id}.")
    return row


def repos(connection: Connection, project_id: str) -> list[dict[str, Any]]:
    """The project's repositories (§2 repo shape): primary first, with the registry's access
    checks (read / write / pull requests) when the registry has probed them."""
    rows = connection.execute(text(
        "SELECT b.id, b.full_name, b.default_branch, b.role, b.created_at, "
        "COALESCE(NULLIF(b.checks, '{}'::jsonb), r.checks) AS checks "
        "FROM github_repository_bindings b "
        "LEFT JOIN LATERAL (SELECT g.checks FROM github_repos g WHERE lower(g.full_name) = "
        "lower(b.full_name) ORDER BY g.checked_at DESC NULLS LAST LIMIT 1) r ON true "
        "WHERE b.project_id = :p AND b.status = 'active' "
        "ORDER BY (b.role = 'primary') DESC, b.created_at"), {"p": project_id}).mappings().all()
    out = []
    seen: set[str] = set()
    for r in rows:
        full = str(r["full_name"])
        if full.lower() in seen:
            continue
        seen.add(full.lower())
        checks = as_json(r["checks"], {}) or {}
        out.append({"binding_id": r["id"], "full_name": full,
                    "branch": r["default_branch"] or "main", "primary": r["role"] == "primary",
                    "access": access_of(checks),
                    "external_url": f"https://github.com/{full}"})
    return out


def _ok(check: Any) -> Optional[bool]:
    if not isinstance(check, Mapping):
        return None
    if "ok" in check:
        return bool(check.get("ok"))
    return None


def access_of(checks: Mapping[str, Any]) -> dict[str, Optional[bool]]:
    """{read, write, pr}: True/False when the registry checked, None when it never asked."""
    return {"read": _ok(checks.get("read")), "write": _ok(checks.get("write")),
            "pr": _ok(checks.get("pull_requests") or checks.get("pr"))}


def first(items: Iterable[Any], default: Any = None) -> Any:
    return next(iter(items), default)




def avatar_of(spec: Any, role_key: str) -> dict[str, Any]:
    persona = (spec or {}).get("persona") if isinstance(spec, dict) else None
    avatar = (persona or {}).get("avatar") if isinstance(persona, dict) else None
    if isinstance(avatar, dict) and avatar.get("seed"):
        return dict(avatar)
    return {"kit": "boss-v1", "seed": role_key}


def agents(connection: Connection, project_id: str) -> dict[str, dict[str, Any]]:
    """role_key -> {agent_id, role_key, name (the callsign when it has one), role, avatar,
    lifecycle}: the faces every row and card draws."""
    rows = connection.execute(text(
        "SELECT role_key, display_name, spec, agent_id, lifecycle FROM roles WHERE project_id = :p"),
        {"p": project_id}).mappings().all()
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        spec = r["spec"] if isinstance(r["spec"], dict) else {}
        persona = spec.get("persona") if isinstance(spec.get("persona"), dict) else {}
        key = str(r["role_key"])
        out[key] = {"agent_id": r["agent_id"] or key, "role_key": key,
                    "name": persona.get("callsign") or r["display_name"],
                    "role": r["display_name"], "avatar": avatar_of(spec, key),
                    "lifecycle": r["lifecycle"] or "active"}
    return out


def face(roster: Mapping[str, Mapping[str, Any]], role_key: Optional[str]) -> Optional[dict[str, Any]]:
    if not role_key:
        return None
    found = roster.get(str(role_key).strip().lower())
    if found is None:
        return {"name": str(role_key).replace("_", " ").title(), "role_key": role_key,
                "avatar": {"kit": "boss-v1", "seed": role_key}}
    return {"name": found["name"], "role_key": found["role_key"], "avatar": found["avatar"],
            "agent_id": found["agent_id"]}
