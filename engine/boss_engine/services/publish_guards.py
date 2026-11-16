"""Org publish guards (BOSSAPP-1256).

An owner or admin can require private publishes, block a severity, and ban
source export and chat uploads. With no row stored, each of those stays
allowed, which is today's behavior.
"""
from __future__ import annotations

from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

# critical is the top. "At or above" a floor means this rank or a higher one.
SEVERITY_RANK = {"critical": 4, "high": 3, "medium": 2, "low": 1, "info": 0}
_UNSET = object()

_REFUSED = {
    "public_publish_refused": "Public publish is refused. Private is required.",
    "severity_blocked": "A finding at this severity is refused.",
    "source_export_banned": "Source export is refused.",
    "chat_upload_banned": "Chat upload is refused.",
}
_ERRORS = {
    "not_admin": "Only an admin can change publish guards.",
    "bad_severity": "Severity must be critical, high, medium, low, or info.",
}


class PublishGuardRefused(Exception):
    """The policy refuses this publish, finding, export, or upload."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(_REFUSED[code])


class PublishGuardError(ValueError):
    """The change itself is not allowed (not an admin, or not a severity)."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(_ERRORS[code])


def _defaults(org_id: str) -> dict[str, Any]:
    return {
        "org_id": org_id,
        "require_private": False,
        "blocked_severity": None,
        "ban_source_export": False,
        "ban_chat_upload": False,
    }


def _is_admin(connection: Connection, org_id: str, actor: str) -> bool:
    """Org-scoped owner or admin, still active, binding not expired."""
    row = connection.execute(text(
        "SELECT 1 FROM identity_role_bindings b "
        "JOIN identity_principals p ON p.id = b.subject_id AND p.org_id = b.org_id "
        "WHERE b.subject_type = 'principal' AND b.subject_id = :actor AND b.org_id = :org "
        "AND b.role_id IN ('arl_owner', 'arl_admin') AND b.scope_type = 'org' "
        "AND b.scope_id = :org AND p.status = 'active' "
        "AND (b.expires_at IS NULL OR b.expires_at > now()) LIMIT 1"),
        {"actor": actor, "org": org_id}).first()
    return row is not None


def policy(connection: Connection, org_id: str) -> dict[str, Any]:
    """The stored guards, or the allow defaults when the org has not set any."""
    row = connection.execute(text(
        "SELECT require_private, blocked_severity, ban_source_export, ban_chat_upload "
        "FROM publish_guards WHERE org_id = :o"), {"o": org_id}).mappings().first()
    if row is None:
        return _defaults(org_id)
    return {
        "org_id": org_id,
        "require_private": bool(row["require_private"]),
        "blocked_severity": row["blocked_severity"],
        "ban_source_export": bool(row["ban_source_export"]),
        "ban_chat_upload": bool(row["ban_chat_upload"]),
    }


def set_policy(connection: Connection, org_id: str, *, actor: str,
               require_private: Any = _UNSET, blocked_severity: Any = _UNSET,
               ban_source_export: Any = _UNSET, ban_chat_upload: Any = _UNSET) -> dict[str, Any]:
    """Change the knobs an admin named. Omitted knobs stay as they are."""
    if not _is_admin(connection, org_id, actor):
        raise PublishGuardError("not_admin")
    nxt = policy(connection, org_id)
    if require_private is not _UNSET:
        nxt["require_private"] = bool(require_private)
    if ban_source_export is not _UNSET:
        nxt["ban_source_export"] = bool(ban_source_export)
    if ban_chat_upload is not _UNSET:
        nxt["ban_chat_upload"] = bool(ban_chat_upload)
    if blocked_severity is not _UNSET:
        nxt["blocked_severity"] = _severity(blocked_severity)
    connection.execute(text(
        "INSERT INTO publish_guards (org_id, require_private, blocked_severity, "
        "ban_source_export, ban_chat_upload, updated_by, updated_at) "
        "VALUES (:o, :p, :s, :e, :u, :by, now()) "
        "ON CONFLICT (org_id) DO UPDATE SET require_private = EXCLUDED.require_private, "
        "blocked_severity = EXCLUDED.blocked_severity, "
        "ban_source_export = EXCLUDED.ban_source_export, "
        "ban_chat_upload = EXCLUDED.ban_chat_upload, "
        "updated_by = EXCLUDED.updated_by, updated_at = now()"),
        {"o": org_id, "p": nxt["require_private"], "s": nxt["blocked_severity"],
         "e": nxt["ban_source_export"], "u": nxt["ban_chat_upload"], "by": actor})
    return policy(connection, org_id)


def _severity(value: Any) -> Optional[str]:
    if value is None:
        return None
    word = str(value).strip().lower()
    if not word:
        return None
    if word not in SEVERITY_RANK:
        raise PublishGuardError("bad_severity")
    return word


def check_publish(connection: Connection, org_id: str, visibility: str) -> None:
    """Refuse a publish that is not private when the org requires private."""
    if not policy(connection, org_id)["require_private"]:
        return
    if str(visibility or "").strip().lower() != "private":
        raise PublishGuardRefused("public_publish_refused")


def check_finding(connection: Connection, org_id: str, severity: str) -> None:
    """Refuse a finding at or above the blocked severity. No floor allows every finding."""
    floor = policy(connection, org_id)["blocked_severity"]
    if not floor:
        return
    rank = SEVERITY_RANK.get(str(severity or "").strip().lower())
    if rank is None or rank >= SEVERITY_RANK[floor]:
        raise PublishGuardRefused("severity_blocked")


def check_source_export(connection: Connection, org_id: str) -> None:
    if policy(connection, org_id)["ban_source_export"]:
        raise PublishGuardRefused("source_export_banned")


def check_chat_upload(connection: Connection, org_id: str) -> None:
    if policy(connection, org_id)["ban_chat_upload"]:
        raise PublishGuardRefused("chat_upload_banned")
