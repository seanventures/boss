"""One project holds a website, a mobile app, slides, and a data view (BOSSAPP-1253).

Publishing one surface updates that row only. Publishing together marks every surface
the project holds, in one record. A repeat of the same dedupe key does not write again.
"""
from __future__ import annotations

import json
from typing import Any, Optional

from sqlalchemy import text

from .core import make_id

SURFACES = ("website", "mobile", "slides", "data_view")

LABELS = {
    "website": "Website",
    "mobile": "Mobile app",
    "slides": "Slides",
    "data_view": "Data view",
}


class SuiteError(Exception):
    """A refusal with a status, a machine code, and a sentence a person can act on."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message

    def body(self) -> dict[str, str]:
        return {"error": self.code, "message": self.message}


def read_suite(connection: Any, project_id: str) -> dict[str, Any]:
    """The surfaces this project holds, in sheet order. Missing kinds are absent, not invented."""
    project = _project(connection, project_id)
    stored = _stored(connection, project_id)
    return {
        "project_id": project_id,
        "project_name": project["name"],
        "surfaces": [_public(stored[kind]) for kind in SURFACES if kind in stored],
    }


def hold_surface(connection: Any, project_id: str, *, kind: str, title: str,
                 body: str = "") -> dict[str, Any]:
    """Keep one surface on the project. The other surfaces are not written."""
    _project(connection, project_id)
    known = _known(kind)
    kept_title = str(title or "").strip()
    kept_body = str(body or "").strip()
    if not kept_title:
        raise SuiteError(422, "title_required", "A surface needs a title.")
    if len(kept_title) > 200:
        raise SuiteError(422, "title_required", "That title is too long.")
    if len(kept_body) > 4000:
        raise SuiteError(422, "body_required", "That body is too long.")
    updated = connection.execute(text(
        """
        UPDATE project_suite_surfaces
        SET title = :title, body = :body, revision = revision + 1, updated_at = now()
        WHERE project_id = :project_id AND kind = :kind
        """), {"project_id": project_id, "kind": known, "title": kept_title, "body": kept_body})
    if updated.rowcount == 0:
        connection.execute(text(
            """
            INSERT INTO project_suite_surfaces (project_id, kind, title, body)
            VALUES (:project_id, :kind, :title, :body)
            """), {"project_id": project_id, "kind": known, "title": kept_title, "body": kept_body})
    return read_suite(connection, project_id)


def publish(connection: Any, project_id: str, *, mode: str, dedupe_key: str,
            kind: Optional[str] = None) -> dict[str, Any]:
    """Publish one surface, or every surface together. One dedupe key writes one record."""
    _project(connection, project_id)
    key = str(dedupe_key or "").strip()
    if not key:
        raise SuiteError(422, "dedupe_required", "A publish needs a dedupe key.")
    if len(key) > 200:
        raise SuiteError(422, "dedupe_required", "That dedupe key is too long.")
    if mode == "one":
        requested = [_known(kind)]
    elif mode == "together":
        # An extra kind must not narrow the sheet. Together always means all four.
        requested = list(SURFACES)
    else:
        raise SuiteError(422, "bad_mode", "Publish one surface, or publish the sheet together.")
    existing = _publish_row(connection, project_id, key)
    if existing is not None:
        stored_kinds = _kinds(existing["kinds"])
        if existing["mode"] != mode or stored_kinds != requested:
            raise SuiteError(409, "dedupe_conflict",
                             "That dedupe key already published a different set.")
        view = read_suite(connection, project_id)
        view["publish"] = {
            "id": existing["id"],
            "mode": existing["mode"],
            "kinds": stored_kinds,
            "dedupe_key": key,
            "repeated": True,
        }
        return view
    stored = _stored(connection, project_id)
    if mode == "one":
        target = requested[0]
        if target not in stored:
            raise SuiteError(409, "surface_missing",
                             f"Hold the {LABELS[target]} before publishing it.")
        targets = [target]
    else:
        missing = [item for item in SURFACES if item not in stored]
        if missing:
            raise SuiteError(409, "sheet_incomplete",
                             f"The sheet is missing {_names(missing)}.")
        targets = list(SURFACES)
    for target in targets:
        connection.execute(text(
            """
            UPDATE project_suite_surfaces
            SET status = 'published', published_revision = revision, updated_at = now()
            WHERE project_id = :project_id AND kind = :kind
            """), {"project_id": project_id, "kind": target})
    publish_id = make_id("pspb")
    connection.execute(text(
        """
        INSERT INTO project_suite_publishes (id, project_id, mode, kinds, dedupe_key)
        VALUES (:id, :project_id, :mode, CAST(:kinds AS jsonb), :dedupe_key)
        """), {
            "id": publish_id,
            "project_id": project_id,
            "mode": mode,
            "kinds": json.dumps(targets),
            "dedupe_key": key,
        })
    view = read_suite(connection, project_id)
    view["publish"] = {
        "id": publish_id,
        "mode": mode,
        "kinds": targets,
        "dedupe_key": key,
        "repeated": False,
    }
    return view


def _project(connection: Any, project_id: str) -> dict[str, Any]:
    row = connection.execute(text(
        "SELECT id, name FROM projects WHERE id = :id"), {"id": project_id}).mappings().first()
    if row is None:
        raise SuiteError(404, "project_missing", "That project does not exist.")
    return dict(row)


def _known(kind: Optional[str]) -> str:
    if kind not in SURFACES:
        raise SuiteError(422, "unknown_surface",
                         f"{kind or 'That'} is not a surface on this sheet.")
    return str(kind)


def _stored(connection: Any, project_id: str) -> dict[str, dict[str, Any]]:
    rows = connection.execute(text(
        """
        SELECT kind, title, body, status, revision, published_revision
        FROM project_suite_surfaces
        WHERE project_id = :id
        """), {"id": project_id}).mappings().all()
    return {str(row["kind"]): dict(row) for row in rows}


def _publish_row(connection: Any, project_id: str, dedupe_key: str) -> Optional[dict[str, Any]]:
    row = connection.execute(text(
        """
        SELECT id, mode, kinds
        FROM project_suite_publishes
        WHERE project_id = :project_id AND dedupe_key = :dedupe_key
        """), {"project_id": project_id, "dedupe_key": dedupe_key}).mappings().first()
    return dict(row) if row is not None else None


def _kinds(value: Any) -> list[str]:
    if isinstance(value, str):
        value = json.loads(value)
    return [str(item) for item in value]


def _names(kinds: list[str]) -> str:
    labels = [LABELS[kind] for kind in kinds]
    if len(labels) == 1:
        return labels[0]
    if len(labels) == 2:
        return f"{labels[0]} and {labels[1]}"
    return ", ".join(labels[:-1]) + f", and {labels[-1]}"


def _public(row: dict[str, Any]) -> dict[str, Any]:
    published = row["published_revision"]
    return {
        "kind": row["kind"],
        "title": row["title"],
        "body": row["body"],
        "status": row["status"],
        "revision": int(row["revision"]),
        "published_revision": None if published is None else int(published),
    }
