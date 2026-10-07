"""Native Work Plane — initiatives, modules, cycles, roadmap items (BOSS-331/358).

Extends the delivery board story from ``release_planning`` with first-class
planning entities that can link to work-tracker issue URLs and express
dependency edges between any work-plane entity.

Durable store (lazy IF NOT EXISTS on first use):

* ``wp_initiatives`` — multi-quarter themes / programs
* ``wp_modules`` — product modules / workstreams under an initiative
* ``wp_cycles`` — timeboxed iteration windows (sprints / months)
* ``wp_roadmap_items`` — planned deliverables on a roadmap, optionally bound
  to a module / cycle / initiative and external tracker issues
* ``wp_dependencies`` — directed depends_on links between any of the above

Board / status views fold these into the same column vocabulary used by
release planning (built / building / planned / blocked).
"""
from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..config import Settings
from .core import emit_event, make_id, now_utc

ENTITY_TYPES = ("initiative", "module", "cycle", "roadmap_item")
STATUSES = ("backlog", "planned", "active", "building", "done", "blocked", "cancelled")
BOARD_COLUMNS = ("built", "building", "planned", "blocked")

_STATUS_COLUMN = {
    "done": "built",
    "shipped": "built",
    "completed": "built",
    "closed": "built",
    "active": "building",
    "building": "building",
    "in_progress": "building",
    "in-progress": "building",
    "planned": "planned",
    "backlog": "planned",
    "todo": "planned",
    "blocked": "blocked",
    "cancelled": "blocked",
    "hold": "blocked",
}

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS wp_initiatives (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  key TEXT NOT NULL,
  title TEXT NOT NULL,
  description TEXT,
  status TEXT NOT NULL DEFAULT 'planned',
  owner TEXT,
  target_date DATE,
  issue_url TEXT,
  tracker TEXT,
  issue_key TEXT,
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_by TEXT NOT NULL DEFAULT 'system',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(project_id, key)
);
CREATE INDEX IF NOT EXISTS idx_wp_initiatives_project
  ON wp_initiatives(project_id, status, updated_at DESC);

CREATE TABLE IF NOT EXISTS wp_modules (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  initiative_id TEXT REFERENCES wp_initiatives(id) ON DELETE SET NULL,
  key TEXT NOT NULL,
  title TEXT NOT NULL,
  description TEXT,
  status TEXT NOT NULL DEFAULT 'planned',
  owner TEXT,
  issue_url TEXT,
  tracker TEXT,
  issue_key TEXT,
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_by TEXT NOT NULL DEFAULT 'system',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(project_id, key)
);
CREATE INDEX IF NOT EXISTS idx_wp_modules_project
  ON wp_modules(project_id, status, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_wp_modules_initiative
  ON wp_modules(initiative_id);

CREATE TABLE IF NOT EXISTS wp_cycles (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  key TEXT NOT NULL,
  title TEXT NOT NULL,
  description TEXT,
  status TEXT NOT NULL DEFAULT 'planned',
  starts_on DATE,
  ends_on DATE,
  issue_url TEXT,
  tracker TEXT,
  issue_key TEXT,
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_by TEXT NOT NULL DEFAULT 'system',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(project_id, key)
);
CREATE INDEX IF NOT EXISTS idx_wp_cycles_project
  ON wp_cycles(project_id, status, starts_on NULLS LAST);

CREATE TABLE IF NOT EXISTS wp_roadmap_items (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  key TEXT NOT NULL,
  title TEXT NOT NULL,
  description TEXT,
  status TEXT NOT NULL DEFAULT 'planned',
  initiative_id TEXT REFERENCES wp_initiatives(id) ON DELETE SET NULL,
  module_id TEXT REFERENCES wp_modules(id) ON DELETE SET NULL,
  cycle_id TEXT REFERENCES wp_cycles(id) ON DELETE SET NULL,
  target_date DATE,
  sort_order INTEGER NOT NULL DEFAULT 0,
  issue_url TEXT,
  tracker TEXT,
  issue_key TEXT,
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_by TEXT NOT NULL DEFAULT 'system',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(project_id, key)
);
CREATE INDEX IF NOT EXISTS idx_wp_roadmap_items_project
  ON wp_roadmap_items(project_id, status, sort_order, target_date NULLS LAST);
CREATE INDEX IF NOT EXISTS idx_wp_roadmap_items_initiative
  ON wp_roadmap_items(initiative_id);
CREATE INDEX IF NOT EXISTS idx_wp_roadmap_items_cycle
  ON wp_roadmap_items(cycle_id);

CREATE TABLE IF NOT EXISTS wp_dependencies (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  src_type TEXT NOT NULL,
  src_id TEXT NOT NULL,
  dst_type TEXT NOT NULL,
  dst_id TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT 'depends_on',
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_by TEXT NOT NULL DEFAULT 'system',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(project_id, src_type, src_id, dst_type, dst_id, kind),
  CHECK (src_type IN ('initiative', 'module', 'cycle', 'roadmap_item')),
  CHECK (dst_type IN ('initiative', 'module', 'cycle', 'roadmap_item'))
);
CREATE INDEX IF NOT EXISTS idx_wp_dependencies_src
  ON wp_dependencies(project_id, src_type, src_id);
CREATE INDEX IF NOT EXISTS idx_wp_dependencies_dst
  ON wp_dependencies(project_id, dst_type, dst_id);
"""


def _iso(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _as_dict(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def _board_column(status: Optional[str]) -> str:
    if not status:
        return "planned"
    return _STATUS_COLUMN.get(status.strip().lower().replace(" ", "_"), "planned")


def _default_issue_url(tracker: str, issue_key: str) -> Optional[str]:
    t = (tracker or "").lower()
    if not issue_key:
        return None
    if t == "github":
        return f"https://github.com/issues/{issue_key}"
    if t == "jira":
        return f"https://jira.example/browse/{issue_key}"
    if t == "linear":
        return f"https://linear.app/issue/{issue_key}"
    if t == "plane":
        # Placeholder host, like jira.example above: a real deployment sets plane_base_url.
        return f"https://plane.example/browse/{issue_key}"
    return None


def _serialize_row(row: Any, entity_type: str) -> dict[str, Any]:
    data = dict(row)
    out: dict[str, Any] = {
        "id": data["id"],
        "project_id": data["project_id"],
        "entity_type": entity_type,
        "key": data.get("key"),
        "title": data.get("title"),
        "description": data.get("description"),
        "status": data.get("status"),
        "column": _board_column(data.get("status")),
        "issue_url": data.get("issue_url"),
        "tracker": data.get("tracker"),
        "issue_key": data.get("issue_key"),
        "metadata": _as_dict(data.get("metadata")),
        "created_by": data.get("created_by"),
        "created_at": _iso(data.get("created_at")),
        "updated_at": _iso(data.get("updated_at")),
    }
    for optional in (
        "owner",
        "target_date",
        "starts_on",
        "ends_on",
        "initiative_id",
        "module_id",
        "cycle_id",
        "sort_order",
    ):
        if optional in data:
            val = data[optional]
            if optional in ("target_date", "starts_on", "ends_on"):
                out[optional] = _iso(val)
            else:
                out[optional] = val
    return out


class WorkPlaneService:
    """Native Work Plane: initiatives / modules / cycles / roadmap + deps."""

    def __init__(self, settings: Optional[Settings] = None):
        self.settings = settings
        self._schema_ready = False

    def ensure_tables(self, connection: Connection) -> None:
        if self._schema_ready:
            return
        from ..db import ensure_service_schema

        # applied at boot (db.SERVICE_SCHEMAS); DDL here only when a table is really missing
        ensure_service_schema(connection, _SCHEMA_SQL)
        self._schema_ready = True

    def _require_project(self, connection: Connection, project_id: str) -> None:
        if not project_id or not str(project_id).strip():
            raise ValueError("project_id is required")
        exists = connection.execute(
            text("SELECT 1 FROM projects WHERE id = :p"),
            {"p": project_id},
        ).scalar()
        if not exists:
            raise LookupError(f"project not found: {project_id}")

    def _normalize_status(self, status: Optional[str]) -> str:
        s = (status or "planned").strip().lower().replace(" ", "_")
        if s not in STATUSES:
            raise ValueError(f"status must be one of: {', '.join(STATUSES)}")
        return s

    def _normalize_key(self, key: str, title: str) -> str:
        raw = (key or "").strip() or (title or "").strip().lower().replace(" ", "-")
        raw = raw[:120]
        if not raw:
            raise ValueError("key or title is required")
        return raw

    # ------------------------------------------------------------------
    # Initiatives
    # ------------------------------------------------------------------
    def create_initiative(
        self,
        connection: Connection,
        *,
        project_id: str,
        title: str,
        key: Optional[str] = None,
        description: Optional[str] = None,
        status: str = "planned",
        owner: Optional[str] = None,
        target_date: Optional[str] = None,
        issue_url: Optional[str] = None,
        tracker: Optional[str] = None,
        issue_key: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
        actor: str = "system",
    ) -> dict[str, Any]:
        self.ensure_tables(connection)
        self._require_project(connection, project_id)
        title = (title or "").strip()
        if not title:
            raise ValueError("title is required")
        entity_key = self._normalize_key(key or "", title)
        status = self._normalize_status(status)
        if tracker and issue_key and not issue_url:
            issue_url = _default_issue_url(tracker, issue_key)
        entity_id = make_id("wpi")
        row = connection.execute(
            text(
                """
                INSERT INTO wp_initiatives (
                    id, project_id, key, title, description, status, owner,
                    target_date, issue_url, tracker, issue_key, metadata, created_by
                ) VALUES (
                    :id, :project_id, :key, :title, :description, :status, :owner,
                    CAST(:target_date AS DATE), :issue_url, :tracker, :issue_key,
                    CAST(:metadata AS JSONB), :created_by
                )
                ON CONFLICT (project_id, key) DO UPDATE SET
                    title = EXCLUDED.title,
                    description = COALESCE(EXCLUDED.description, wp_initiatives.description),
                    status = EXCLUDED.status,
                    owner = COALESCE(EXCLUDED.owner, wp_initiatives.owner),
                    target_date = COALESCE(EXCLUDED.target_date, wp_initiatives.target_date),
                    issue_url = COALESCE(EXCLUDED.issue_url, wp_initiatives.issue_url),
                    tracker = COALESCE(EXCLUDED.tracker, wp_initiatives.tracker),
                    issue_key = COALESCE(EXCLUDED.issue_key, wp_initiatives.issue_key),
                    metadata = wp_initiatives.metadata || EXCLUDED.metadata,
                    updated_at = now()
                RETURNING *
                """
            ),
            {
                "id": entity_id,
                "project_id": project_id,
                "key": entity_key,
                "title": title,
                "description": description,
                "status": status,
                "owner": owner,
                "target_date": target_date or None,
                "issue_url": issue_url,
                "tracker": (tracker or "").strip().lower() or None,
                "issue_key": issue_key,
                "metadata": json.dumps(metadata or {}),
                "created_by": actor,
            },
        ).mappings().one()
        plan = _serialize_row(row, "initiative")
        emit_event(
            connection,
            project_id,
            "work_plane.initiative_upserted",
            actor,
            "wp_initiative",
            plan["id"],
            {"key": plan["key"], "status": plan["status"]},
        )
        return plan

    def list_initiatives(
        self, connection: Connection, project_id: str, *, status: Optional[str] = None
    ) -> list[dict[str, Any]]:
        self.ensure_tables(connection)
        clauses = ["project_id = :p"]
        params: dict[str, Any] = {"p": project_id}
        if status:
            clauses.append("status = :status")
            params["status"] = status
        rows = connection.execute(
            text(
                f"SELECT * FROM wp_initiatives WHERE {' AND '.join(clauses)} "
                "ORDER BY target_date NULLS LAST, updated_at DESC"
            ),
            params,
        ).mappings().all()
        return [_serialize_row(r, "initiative") for r in rows]

    # ------------------------------------------------------------------
    # Modules
    # ------------------------------------------------------------------
    def create_module(
        self,
        connection: Connection,
        *,
        project_id: str,
        title: str,
        key: Optional[str] = None,
        initiative_id: Optional[str] = None,
        description: Optional[str] = None,
        status: str = "planned",
        owner: Optional[str] = None,
        issue_url: Optional[str] = None,
        tracker: Optional[str] = None,
        issue_key: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
        actor: str = "system",
    ) -> dict[str, Any]:
        self.ensure_tables(connection)
        self._require_project(connection, project_id)
        title = (title or "").strip()
        if not title:
            raise ValueError("title is required")
        if initiative_id:
            self._assert_entity(connection, project_id, "initiative", initiative_id)
        entity_key = self._normalize_key(key or "", title)
        status = self._normalize_status(status)
        if tracker and issue_key and not issue_url:
            issue_url = _default_issue_url(tracker, issue_key)
        entity_id = make_id("wpm")
        row = connection.execute(
            text(
                """
                INSERT INTO wp_modules (
                    id, project_id, initiative_id, key, title, description, status,
                    owner, issue_url, tracker, issue_key, metadata, created_by
                ) VALUES (
                    :id, :project_id, :initiative_id, :key, :title, :description, :status,
                    :owner, :issue_url, :tracker, :issue_key, CAST(:metadata AS JSONB),
                    :created_by
                )
                ON CONFLICT (project_id, key) DO UPDATE SET
                    title = EXCLUDED.title,
                    initiative_id = COALESCE(EXCLUDED.initiative_id, wp_modules.initiative_id),
                    description = COALESCE(EXCLUDED.description, wp_modules.description),
                    status = EXCLUDED.status,
                    owner = COALESCE(EXCLUDED.owner, wp_modules.owner),
                    issue_url = COALESCE(EXCLUDED.issue_url, wp_modules.issue_url),
                    tracker = COALESCE(EXCLUDED.tracker, wp_modules.tracker),
                    issue_key = COALESCE(EXCLUDED.issue_key, wp_modules.issue_key),
                    metadata = wp_modules.metadata || EXCLUDED.metadata,
                    updated_at = now()
                RETURNING *
                """
            ),
            {
                "id": entity_id,
                "project_id": project_id,
                "initiative_id": initiative_id,
                "key": entity_key,
                "title": title,
                "description": description,
                "status": status,
                "owner": owner,
                "issue_url": issue_url,
                "tracker": (tracker or "").strip().lower() or None,
                "issue_key": issue_key,
                "metadata": json.dumps(metadata or {}),
                "created_by": actor,
            },
        ).mappings().one()
        plan = _serialize_row(row, "module")
        emit_event(
            connection,
            project_id,
            "work_plane.module_upserted",
            actor,
            "wp_module",
            plan["id"],
            {"key": plan["key"], "status": plan["status"]},
        )
        return plan

    def list_modules(
        self,
        connection: Connection,
        project_id: str,
        *,
        initiative_id: Optional[str] = None,
        status: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        self.ensure_tables(connection)
        clauses = ["project_id = :p"]
        params: dict[str, Any] = {"p": project_id}
        if initiative_id:
            clauses.append("initiative_id = :initiative_id")
            params["initiative_id"] = initiative_id
        if status:
            clauses.append("status = :status")
            params["status"] = status
        rows = connection.execute(
            text(
                f"SELECT * FROM wp_modules WHERE {' AND '.join(clauses)} "
                "ORDER BY updated_at DESC"
            ),
            params,
        ).mappings().all()
        return [_serialize_row(r, "module") for r in rows]

    # ------------------------------------------------------------------
    # Cycles
    # ------------------------------------------------------------------
    def create_cycle(
        self,
        connection: Connection,
        *,
        project_id: str,
        title: str,
        key: Optional[str] = None,
        description: Optional[str] = None,
        status: str = "planned",
        starts_on: Optional[str] = None,
        ends_on: Optional[str] = None,
        issue_url: Optional[str] = None,
        tracker: Optional[str] = None,
        issue_key: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
        actor: str = "system",
    ) -> dict[str, Any]:
        self.ensure_tables(connection)
        self._require_project(connection, project_id)
        title = (title or "").strip()
        if not title:
            raise ValueError("title is required")
        entity_key = self._normalize_key(key or "", title)
        status = self._normalize_status(status)
        if tracker and issue_key and not issue_url:
            issue_url = _default_issue_url(tracker, issue_key)
        entity_id = make_id("wpc")
        row = connection.execute(
            text(
                """
                INSERT INTO wp_cycles (
                    id, project_id, key, title, description, status,
                    starts_on, ends_on, issue_url, tracker, issue_key,
                    metadata, created_by
                ) VALUES (
                    :id, :project_id, :key, :title, :description, :status,
                    CAST(:starts_on AS DATE), CAST(:ends_on AS DATE),
                    :issue_url, :tracker, :issue_key,
                    CAST(:metadata AS JSONB), :created_by
                )
                ON CONFLICT (project_id, key) DO UPDATE SET
                    title = EXCLUDED.title,
                    description = COALESCE(EXCLUDED.description, wp_cycles.description),
                    status = EXCLUDED.status,
                    starts_on = COALESCE(EXCLUDED.starts_on, wp_cycles.starts_on),
                    ends_on = COALESCE(EXCLUDED.ends_on, wp_cycles.ends_on),
                    issue_url = COALESCE(EXCLUDED.issue_url, wp_cycles.issue_url),
                    tracker = COALESCE(EXCLUDED.tracker, wp_cycles.tracker),
                    issue_key = COALESCE(EXCLUDED.issue_key, wp_cycles.issue_key),
                    metadata = wp_cycles.metadata || EXCLUDED.metadata,
                    updated_at = now()
                RETURNING *
                """
            ),
            {
                "id": entity_id,
                "project_id": project_id,
                "key": entity_key,
                "title": title,
                "description": description,
                "status": status,
                "starts_on": starts_on or None,
                "ends_on": ends_on or None,
                "issue_url": issue_url,
                "tracker": (tracker or "").strip().lower() or None,
                "issue_key": issue_key,
                "metadata": json.dumps(metadata or {}),
                "created_by": actor,
            },
        ).mappings().one()
        plan = _serialize_row(row, "cycle")
        emit_event(
            connection,
            project_id,
            "work_plane.cycle_upserted",
            actor,
            "wp_cycle",
            plan["id"],
            {"key": plan["key"], "status": plan["status"]},
        )
        return plan

    def list_cycles(
        self, connection: Connection, project_id: str, *, status: Optional[str] = None
    ) -> list[dict[str, Any]]:
        self.ensure_tables(connection)
        clauses = ["project_id = :p"]
        params: dict[str, Any] = {"p": project_id}
        if status:
            clauses.append("status = :status")
            params["status"] = status
        rows = connection.execute(
            text(
                f"SELECT * FROM wp_cycles WHERE {' AND '.join(clauses)} "
                "ORDER BY starts_on NULLS LAST, updated_at DESC"
            ),
            params,
        ).mappings().all()
        return [_serialize_row(r, "cycle") for r in rows]

    # ------------------------------------------------------------------
    # Roadmap items
    # ------------------------------------------------------------------
    def create_roadmap_item(
        self,
        connection: Connection,
        *,
        project_id: str,
        title: str,
        key: Optional[str] = None,
        description: Optional[str] = None,
        status: str = "planned",
        initiative_id: Optional[str] = None,
        module_id: Optional[str] = None,
        cycle_id: Optional[str] = None,
        target_date: Optional[str] = None,
        sort_order: int = 0,
        issue_url: Optional[str] = None,
        tracker: Optional[str] = None,
        issue_key: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
        actor: str = "system",
    ) -> dict[str, Any]:
        self.ensure_tables(connection)
        self._require_project(connection, project_id)
        title = (title or "").strip()
        if not title:
            raise ValueError("title is required")
        if initiative_id:
            self._assert_entity(connection, project_id, "initiative", initiative_id)
        if module_id:
            self._assert_entity(connection, project_id, "module", module_id)
        if cycle_id:
            self._assert_entity(connection, project_id, "cycle", cycle_id)
        entity_key = self._normalize_key(key or "", title)
        status = self._normalize_status(status)
        if tracker and issue_key and not issue_url:
            issue_url = _default_issue_url(tracker, issue_key)
        entity_id = make_id("wpr")
        row = connection.execute(
            text(
                """
                INSERT INTO wp_roadmap_items (
                    id, project_id, key, title, description, status,
                    initiative_id, module_id, cycle_id, target_date, sort_order,
                    issue_url, tracker, issue_key, metadata, created_by
                ) VALUES (
                    :id, :project_id, :key, :title, :description, :status,
                    :initiative_id, :module_id, :cycle_id,
                    CAST(:target_date AS DATE), :sort_order,
                    :issue_url, :tracker, :issue_key,
                    CAST(:metadata AS JSONB), :created_by
                )
                ON CONFLICT (project_id, key) DO UPDATE SET
                    title = EXCLUDED.title,
                    description = COALESCE(EXCLUDED.description, wp_roadmap_items.description),
                    status = EXCLUDED.status,
                    initiative_id = COALESCE(EXCLUDED.initiative_id, wp_roadmap_items.initiative_id),
                    module_id = COALESCE(EXCLUDED.module_id, wp_roadmap_items.module_id),
                    cycle_id = COALESCE(EXCLUDED.cycle_id, wp_roadmap_items.cycle_id),
                    target_date = COALESCE(EXCLUDED.target_date, wp_roadmap_items.target_date),
                    sort_order = EXCLUDED.sort_order,
                    issue_url = COALESCE(EXCLUDED.issue_url, wp_roadmap_items.issue_url),
                    tracker = COALESCE(EXCLUDED.tracker, wp_roadmap_items.tracker),
                    issue_key = COALESCE(EXCLUDED.issue_key, wp_roadmap_items.issue_key),
                    metadata = wp_roadmap_items.metadata || EXCLUDED.metadata,
                    updated_at = now()
                RETURNING *
                """
            ),
            {
                "id": entity_id,
                "project_id": project_id,
                "key": entity_key,
                "title": title,
                "description": description,
                "status": status,
                "initiative_id": initiative_id,
                "module_id": module_id,
                "cycle_id": cycle_id,
                "target_date": target_date or None,
                "sort_order": int(sort_order or 0),
                "issue_url": issue_url,
                "tracker": (tracker or "").strip().lower() or None,
                "issue_key": issue_key,
                "metadata": json.dumps(metadata or {}),
                "created_by": actor,
            },
        ).mappings().one()
        plan = _serialize_row(row, "roadmap_item")
        emit_event(
            connection,
            project_id,
            "work_plane.roadmap_item_upserted",
            actor,
            "wp_roadmap_item",
            plan["id"],
            {"key": plan["key"], "status": plan["status"]},
        )
        return plan

    def list_roadmap_items(
        self,
        connection: Connection,
        project_id: str,
        *,
        initiative_id: Optional[str] = None,
        module_id: Optional[str] = None,
        cycle_id: Optional[str] = None,
        status: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        self.ensure_tables(connection)
        clauses = ["project_id = :p"]
        params: dict[str, Any] = {"p": project_id}
        if initiative_id:
            clauses.append("initiative_id = :initiative_id")
            params["initiative_id"] = initiative_id
        if module_id:
            clauses.append("module_id = :module_id")
            params["module_id"] = module_id
        if cycle_id:
            clauses.append("cycle_id = :cycle_id")
            params["cycle_id"] = cycle_id
        if status:
            clauses.append("status = :status")
            params["status"] = status
        rows = connection.execute(
            text(
                f"SELECT * FROM wp_roadmap_items WHERE {' AND '.join(clauses)} "
                "ORDER BY sort_order, target_date NULLS LAST, updated_at DESC"
            ),
            params,
        ).mappings().all()
        return [_serialize_row(r, "roadmap_item") for r in rows]

    # ------------------------------------------------------------------
    # Dependencies + issue links
    # ------------------------------------------------------------------
    def _table_for(self, entity_type: str) -> str:
        return {
            "initiative": "wp_initiatives",
            "module": "wp_modules",
            "cycle": "wp_cycles",
            "roadmap_item": "wp_roadmap_items",
        }[entity_type]

    def _assert_entity(
        self,
        connection: Connection,
        project_id: str,
        entity_type: str,
        entity_id: str,
    ) -> None:
        if entity_type not in ENTITY_TYPES:
            raise ValueError(f"unknown entity_type {entity_type!r}")
        table = self._table_for(entity_type)
        exists = connection.execute(
            text(f"SELECT 1 FROM {table} WHERE id = :id AND project_id = :p"),
            {"id": entity_id, "p": project_id},
        ).scalar()
        if not exists:
            raise LookupError(f"{entity_type} not found: {entity_id}")

    def add_dependency(
        self,
        connection: Connection,
        *,
        project_id: str,
        src_type: str,
        src_id: str,
        dst_type: str,
        dst_id: str,
        kind: str = "depends_on",
        metadata: Optional[dict[str, Any]] = None,
        actor: str = "system",
    ) -> dict[str, Any]:
        """Record that src depends_on (or blocks/relates_to) dst."""
        self.ensure_tables(connection)
        self._require_project(connection, project_id)
        if src_type not in ENTITY_TYPES or dst_type not in ENTITY_TYPES:
            raise ValueError(f"entity types must be one of {ENTITY_TYPES}")
        kind = (kind or "depends_on").strip().lower()
        if kind not in ("depends_on", "blocks", "relates_to"):
            raise ValueError("kind must be depends_on|blocks|relates_to")
        if src_type == dst_type and src_id == dst_id:
            raise ValueError("cannot depend on self")
        self._assert_entity(connection, project_id, src_type, src_id)
        self._assert_entity(connection, project_id, dst_type, dst_id)
        dep_id = make_id("wpd")
        row = connection.execute(
            text(
                """
                INSERT INTO wp_dependencies (
                    id, project_id, src_type, src_id, dst_type, dst_id, kind,
                    metadata, created_by
                ) VALUES (
                    :id, :project_id, :src_type, :src_id, :dst_type, :dst_id, :kind,
                    CAST(:metadata AS JSONB), :created_by
                )
                ON CONFLICT (project_id, src_type, src_id, dst_type, dst_id, kind)
                DO UPDATE SET metadata = wp_dependencies.metadata || EXCLUDED.metadata
                RETURNING *
                """
            ),
            {
                "id": dep_id,
                "project_id": project_id,
                "src_type": src_type,
                "src_id": src_id,
                "dst_type": dst_type,
                "dst_id": dst_id,
                "kind": kind,
                "metadata": json.dumps(metadata or {}),
                "created_by": actor,
            },
        ).mappings().one()
        out = {
            "id": row["id"],
            "project_id": row["project_id"],
            "src_type": row["src_type"],
            "src_id": row["src_id"],
            "dst_type": row["dst_type"],
            "dst_id": row["dst_id"],
            "kind": row["kind"],
            "metadata": _as_dict(row["metadata"]),
            "created_by": row["created_by"],
            "created_at": _iso(row["created_at"]),
        }
        emit_event(
            connection,
            project_id,
            "work_plane.dependency_added",
            actor,
            "wp_dependency",
            out["id"],
            {
                "src_type": src_type,
                "src_id": src_id,
                "dst_type": dst_type,
                "dst_id": dst_id,
                "kind": kind,
            },
        )
        return out

    def list_dependencies(
        self,
        connection: Connection,
        project_id: str,
        *,
        entity_type: Optional[str] = None,
        entity_id: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        self.ensure_tables(connection)
        clauses = ["project_id = :p"]
        params: dict[str, Any] = {"p": project_id}
        if entity_type and entity_id:
            clauses.append(
                "((src_type = :etype AND src_id = :eid) OR "
                "(dst_type = :etype AND dst_id = :eid))"
            )
            params["etype"] = entity_type
            params["eid"] = entity_id
        rows = connection.execute(
            text(
                f"SELECT * FROM wp_dependencies WHERE {' AND '.join(clauses)} "
                "ORDER BY created_at DESC"
            ),
            params,
        ).mappings().all()
        return [
            {
                "id": r["id"],
                "project_id": r["project_id"],
                "src_type": r["src_type"],
                "src_id": r["src_id"],
                "dst_type": r["dst_type"],
                "dst_id": r["dst_id"],
                "kind": r["kind"],
                "metadata": _as_dict(r["metadata"]),
                "created_by": r["created_by"],
                "created_at": _iso(r["created_at"]),
            }
            for r in rows
        ]

    def link_issue(
        self,
        connection: Connection,
        *,
        project_id: str,
        entity_type: str,
        entity_id: str,
        tracker: str,
        issue_key: str,
        url: Optional[str] = None,
        actor: str = "system",
    ) -> dict[str, Any]:
        """Attach a work-tracker issue URL to any work-plane entity."""
        self.ensure_tables(connection)
        self._require_project(connection, project_id)
        if entity_type not in ENTITY_TYPES:
            raise ValueError(f"entity_type must be one of {ENTITY_TYPES}")
        tracker = (tracker or "").strip().lower()
        issue_key = (issue_key or "").strip()
        if not tracker:
            raise ValueError("tracker is required")
        if not issue_key:
            raise ValueError("issue_key is required")
        self._assert_entity(connection, project_id, entity_type, entity_id)
        resolved_url = (url or "").strip() or _default_issue_url(tracker, issue_key)
        table = self._table_for(entity_type)
        row = connection.execute(
            text(
                f"""
                UPDATE {table}
                SET tracker = :tracker,
                    issue_key = :issue_key,
                    issue_url = :issue_url,
                    updated_at = now()
                WHERE id = :id AND project_id = :project_id
                RETURNING *
                """
            ),
            {
                "id": entity_id,
                "project_id": project_id,
                "tracker": tracker,
                "issue_key": issue_key,
                "issue_url": resolved_url,
            },
        ).mappings().one()
        out = _serialize_row(row, entity_type)
        emit_event(
            connection,
            project_id,
            "work_plane.issue_linked",
            actor,
            f"wp_{entity_type}",
            entity_id,
            {"tracker": tracker, "issue_key": issue_key, "url": resolved_url},
        )
        return out

    # ------------------------------------------------------------------
    # Board / status (expand release_planning shape)
    # ------------------------------------------------------------------
    def board(self, connection: Connection, project_id: str) -> dict[str, Any]:
        """Kanban columns over roadmap items + modules + initiatives."""
        self.ensure_tables(connection)
        columns: dict[str, list[dict[str, Any]]] = {c: [] for c in BOARD_COLUMNS}
        for entity_type, lister in (
            ("roadmap_item", self.list_roadmap_items),
            ("module", self.list_modules),
            ("initiative", self.list_initiatives),
            ("cycle", self.list_cycles),
        ):
            for item in lister(connection, project_id):
                col = item.get("column") or "planned"
                if col not in columns:
                    col = "planned"
                columns[col].append(item)
        return {
            "project_id": project_id,
            "columns": columns,
            "counts": {c: len(columns[c]) for c in BOARD_COLUMNS},
        }

    def status_snapshot(
        self, connection: Connection, project_id: str
    ) -> dict[str, Any]:
        self.ensure_tables(connection)
        board = self.board(connection, project_id)
        columns = board["columns"]
        initiatives = self.list_initiatives(connection, project_id)
        cycles = self.list_cycles(connection, project_id)
        roadmap = self.list_roadmap_items(connection, project_id)
        deps = self.list_dependencies(connection, project_id)
        return {
            "project_id": project_id,
            "built_count": len(columns["built"]),
            "todo_count": len(columns["planned"]),
            "in_progress": len(columns["building"]),
            "blocked": len(columns["blocked"]),
            "totals": {
                "initiatives": len(initiatives),
                "modules": len(self.list_modules(connection, project_id)),
                "cycles": len(cycles),
                "roadmap_items": len(roadmap),
                "dependencies": len(deps),
                "board_items": sum(len(columns[c]) for c in BOARD_COLUMNS),
            },
            "active_cycles": [
                c for c in cycles if c.get("status") in ("active", "building")
            ],
            "open_initiatives": [
                i for i in initiatives if i.get("status") not in ("done", "cancelled")
            ],
        }

    def overview(self, connection: Connection, project_id: str) -> dict[str, Any]:
        """Full work-plane snapshot for Mission Control."""
        self.ensure_tables(connection)
        self._require_project(connection, project_id)
        return {
            "project_id": project_id,
            "initiatives": self.list_initiatives(connection, project_id),
            "modules": self.list_modules(connection, project_id),
            "cycles": self.list_cycles(connection, project_id),
            "roadmap_items": self.list_roadmap_items(connection, project_id),
            "dependencies": self.list_dependencies(connection, project_id),
            "board": self.board(connection, project_id),
            "status": self.status_snapshot(connection, project_id),
            "generated_at": _iso(now_utc()),
        }
