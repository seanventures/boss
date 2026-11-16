"""Publish a visitor snapshot. It is not the in-progress preview.

A snapshot freezes one page for one audience. Publishing without approval leaves
the row pending and stores no visitor URL. Approving mints ``/v1/visitors/{id}``,
which is never the live preview URL. A repeat of the same dedupe key returns the
original row and does not flip a pending plate to approved.
"""
from __future__ import annotations

import hashlib
import json
import secrets
from typing import Any, Mapping, Optional

from sqlalchemy.exc import IntegrityError

from ..ports import sql

AUDIENCES = ("public", "customers", "partners", "staff")
_VISITOR_PREFIX = "/v1/visitors/"


class PublishAudienceError(Exception):
    """The publish was refused. ``status`` is the HTTP status the router should show."""

    def __init__(self, message: str, status: int = 422):
        super().__init__(message)
        self.status = status


def _audience(audience: str) -> str:
    name = str(audience or "").strip().lower()
    if name not in AUDIENCES:
        allowed = ", ".join(AUDIENCES)
        raise PublishAudienceError(f"audience must be one of: {allowed}", status=422)
    return name


def _actor(actor: str) -> str:
    who = str(actor or "").strip()
    if not who:
        raise PublishAudienceError("actor is required", status=422)
    return who


def _optional(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _freeze(snapshot: Mapping[str, Any]) -> tuple[dict[str, Any], str, str]:
    if not isinstance(snapshot, Mapping) or isinstance(snapshot, (str, bytes)) or not snapshot:
        raise PublishAudienceError("a visitor snapshot needs the frozen page", status=422)
    try:
        encoded = json.dumps(dict(snapshot), sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as error:
        raise PublishAudienceError(
            f"snapshot is not a frozen page: {error}", status=422) from error
    frozen = json.loads(encoded)
    digest = hashlib.sha256(encoded.encode()).hexdigest()
    return frozen, encoded, digest


def _door(snapshot_id: str, preview_url: Optional[str]) -> str:
    url = f"{_VISITOR_PREFIX}{snapshot_id}"
    if preview_url and url == preview_url:
        raise PublishAudienceError(
            "refusing to use the live preview as the visitor URL", status=409)
    return url


def _plate(row: Mapping[str, Any]) -> dict[str, Any]:
    snapshot = row["snapshot"]
    if isinstance(snapshot, str):
        snapshot = json.loads(snapshot)
    return {
        "id": row["id"],
        "project_id": row["project_id"],
        "audience": row["audience"],
        "approval_state": row["approval_state"],
        "visitor_url": row["visitor_url"],
        "preview_url": row["preview_url"],
        "source_preview_id": row["source_preview_id"],
        "snapshot": snapshot,
        "snapshot_hash": row["snapshot_hash"],
        "is_live_preview": False,
        "kind": "visitor_snapshot",
        "published_by": row["published_by"],
        "dedupe_key": row["dedupe_key"],
    }


def _require_project(connection, project_id: str) -> None:
    found = connection.execute(
        sql("SELECT 1 FROM projects WHERE id = :id"), {"id": project_id}).first()
    if not found:
        raise PublishAudienceError(f"no such project: {project_id}", status=404)


def _by_id(connection, project_id: str, snapshot_id: str) -> Optional[dict[str, Any]]:
    row = connection.execute(
        sql("SELECT id, project_id, audience, approval_state, source_preview_id, preview_url, "
            "visitor_url, snapshot, snapshot_hash, dedupe_key, published_by "
            "FROM visitor_snapshots WHERE project_id = :project AND id = :id"),
        {"project": project_id, "id": snapshot_id}).mappings().first()
    return _plate(row) if row else None


def publish_visitor_snapshot(connection, *, project_id: str, audience: str, actor: str,
                             snapshot: Mapping[str, Any], approved: bool = False,
                             source_preview_id: Optional[str] = None,
                             preview_url: Optional[str] = None,
                             dedupe_key: Optional[str] = None) -> dict[str, Any]:
    """Freeze ``snapshot`` for ``audience``. Unapproved publishes stay pending."""
    if not isinstance(approved, bool):
        raise PublishAudienceError("approved must be true or false", status=422)
    name = _audience(audience)
    who = _actor(actor)
    frozen, encoded, digest = _freeze(snapshot)
    bench = _optional(preview_url)
    source = _optional(source_preview_id)
    explicit = _optional(dedupe_key)
    if explicit and len(explicit) > 200:
        raise PublishAudienceError("dedupe_key is too long", status=422)
    key = explicit or (
        f"{project_id}:{name}:{digest}:{'approved' if approved else 'pending'}:{source or ''}")
    _require_project(connection, project_id)
    snapshot_id = "vsnap_" + secrets.token_hex(12)
    state = "approved" if approved else "pending"
    visitor_url = _door(snapshot_id, bench) if approved else None
    try:
        inserted = connection.execute(
            sql("INSERT INTO visitor_snapshots ("
                "id, project_id, audience, approval_state, source_preview_id, preview_url, "
                "visitor_url, snapshot, snapshot_hash, dedupe_key, published_by) "
                "VALUES ("
                ":id, :project_id, :audience, :approval_state, :source_preview_id, :preview_url, "
                ":visitor_url, CAST(:snapshot AS JSONB), :snapshot_hash, :dedupe_key, "
                ":published_by) "
                "ON CONFLICT (project_id, dedupe_key) DO NOTHING "
                "RETURNING id, project_id, audience, approval_state, source_preview_id, "
                "preview_url, visitor_url, snapshot, snapshot_hash, dedupe_key, published_by"),
            {"id": snapshot_id, "project_id": project_id, "audience": name,
             "approval_state": state, "source_preview_id": source, "preview_url": bench,
             "visitor_url": visitor_url, "snapshot": encoded, "snapshot_hash": digest,
             "dedupe_key": key, "published_by": who}).mappings().first()
    except IntegrityError as error:
        raise PublishAudienceError(
            f"visitor snapshot was not stored: {error}", status=409) from error
    if inserted:
        return _plate(inserted)
    # Same dedupe key: return the original plate. Do not upgrade pending to approved.
    existing = connection.execute(
        sql("SELECT id, project_id, audience, approval_state, source_preview_id, preview_url, "
            "visitor_url, snapshot, snapshot_hash, dedupe_key, published_by "
            "FROM visitor_snapshots WHERE project_id = :project AND dedupe_key = :key"),
        {"project": project_id, "key": key}).mappings().first()
    if not existing:
        raise PublishAudienceError("visitor snapshot was not stored", status=409)
    return _plate(existing)


def approve_visitor_snapshot(connection, *, project_id: str, snapshot_id: str,
                             actor: str) -> dict[str, Any]:
    """Open the visitor door for a pending plate. A second approve is the same URL."""
    _actor(actor)
    _require_project(connection, project_id)
    current = _by_id(connection, project_id, snapshot_id)
    if current is None:
        raise PublishAudienceError(f"no such visitor snapshot: {snapshot_id}", status=404)
    if current["approval_state"] == "approved":
        return current
    visitor_url = _door(snapshot_id, current["preview_url"])
    connection.execute(
        sql("UPDATE visitor_snapshots SET approval_state = 'approved', visitor_url = :url "
            "WHERE project_id = :project AND id = :id AND approval_state = 'pending'"),
        {"url": visitor_url, "project": project_id, "id": snapshot_id})
    opened = _by_id(connection, project_id, snapshot_id)
    if opened is None or opened["approval_state"] != "approved":
        raise PublishAudienceError(
            f"visitor snapshot {snapshot_id} was not approved", status=409)
    return opened


def current_visitor(connection, *, project_id: str, audience: str) -> Optional[dict[str, Any]]:
    """The latest approved plate for this audience. Pending is not a visitor URL."""
    name = _audience(audience)
    row = connection.execute(
        sql("SELECT id, project_id, audience, approval_state, source_preview_id, preview_url, "
            "visitor_url, snapshot, snapshot_hash, dedupe_key, published_by "
            "FROM visitor_snapshots "
            "WHERE project_id = :project AND audience = :audience "
            "AND approval_state = 'approved' "
            "ORDER BY created_at DESC, id DESC LIMIT 1"),
        {"project": project_id, "audience": name}).mappings().first()
    return _plate(row) if row else None


def get_visitor_snapshot(connection, *, project_id: str,
                         snapshot_id: str) -> Optional[dict[str, Any]]:
    return _by_id(connection, project_id, snapshot_id)
