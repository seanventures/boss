"""Project delivery board + release planning.

Unifies FeatureCatalog, work_orchestration work items, open tasks, and
deployment rollouts into:

* status snapshot (built / todo / in_progress / blocked / last_release)
* kanban-style board columns (built, building, planned, blocked)
* release plan milestones with tracker issue links

Durable store: ``release_plans`` + ``release_board_items`` (schema.sql and
lazy IF NOT EXISTS ensure on first use).
"""

from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..config import Settings
from .core import emit_event, make_id
from .feature_catalog import FeatureCatalog
from .work_orchestration import WorkOrchestrationService


# Feature catalog statuses → board column
_FEATURE_COLUMN = {
    "Implemented": "built",
    "Partial": "building",
    "Planned": "planned",
    "Blocked": "blocked",
}

# Work item / task status tokens → board column
_WORK_STATUS_COLUMN = {
    "done": "built",
    "completed": "built",
    "shipped": "built",
    "closed": "built",
    "implemented": "built",
    "active": "building",
    "in_progress": "building",
    "in-progress": "building",
    "running": "building",
    "allocated": "building",
    "building": "building",
    "open": "planned",
    "todo": "planned",
    "backlog": "planned",
    "planned": "planned",
    "unallocated": "planned",
    "queued": "planned",
    "blocked": "blocked",
    "hold": "blocked",
    "waiting": "blocked",
    "cancelled": "blocked",
}

BOARD_COLUMNS = ("built", "building", "planned", "blocked")

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS release_plans (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  target_date DATE,
  status TEXT NOT NULL DEFAULT 'planned',
  item_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
  issue_links JSONB NOT NULL DEFAULT '[]'::jsonb,
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_by TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_release_plans_project
  ON release_plans(project_id, target_date NULLS LAST, created_at DESC);

CREATE TABLE IF NOT EXISTS release_board_items (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  column_key TEXT NOT NULL,
  source_type TEXT NOT NULL,
  source_id TEXT NOT NULL,
  title TEXT NOT NULL,
  status TEXT,
  external_url TEXT,
  tracker TEXT,
  issue_key TEXT,
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  sort_order INTEGER NOT NULL DEFAULT 0,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(project_id, source_type, source_id),
  CHECK (column_key IN ('built', 'building', 'planned', 'blocked'))
);
CREATE INDEX IF NOT EXISTS idx_release_board_items_project_col
  ON release_board_items(project_id, column_key, sort_order, updated_at DESC);
"""

_PLAN_RETURNING = """
    id, project_id, name, target_date, status, item_ids, issue_links,
    metadata, created_by, created_at, updated_at
"""

_ITEM_RETURNING = """
    id, project_id, column_key, source_type, source_id, title, status,
    external_url, tracker, issue_key, metadata, sort_order,
    created_at, updated_at
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


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except json.JSONDecodeError:
            return []
    return list(value) if value else []


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


def _serialize_plan(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "project_id": row["project_id"],
        "name": row["name"],
        "target_date": _iso(row["target_date"]),
        "status": row["status"],
        "item_ids": _as_list(row["item_ids"]),
        "issue_links": _as_list(row["issue_links"]),
        "metadata": _as_dict(row["metadata"]),
        "created_by": row["created_by"],
        "created_at": _iso(row["created_at"]),
        "updated_at": _iso(row["updated_at"]),
    }


def _serialize_item(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "project_id": row["project_id"],
        "column": row["column_key"],
        "source_type": row["source_type"],
        "source_id": row["source_id"],
        "title": row["title"],
        "status": row["status"],
        "external_url": row["external_url"],
        "tracker": row["tracker"],
        "issue_key": row["issue_key"],
        "metadata": _as_dict(row["metadata"]),
        "sort_order": int(row["sort_order"] or 0),
        "created_at": _iso(row["created_at"]),
        "updated_at": _iso(row["updated_at"]),
    }


def _feature_column(status: Optional[str]) -> str:
    if not status:
        return "planned"
    return _FEATURE_COLUMN.get(status.strip(), "planned")


def _work_column(status: Optional[str]) -> str:
    if not status:
        return "planned"
    key = status.strip().lower().replace(" ", "_")
    return _WORK_STATUS_COLUMN.get(key, "planned")


class ReleasePlanningService:
    """Project delivery board and release plan milestones."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.features = FeatureCatalog(settings)
        self.work = WorkOrchestrationService(settings)
        self._schema_ready = False

    def ensure_tables(self, connection: Connection) -> None:
        """Idempotent CREATE TABLE IF NOT EXISTS for release planning store."""
        if self._schema_ready:
            return
        # Split on bare statements — execute via raw so multiple DDL is fine.
        for stmt in _SCHEMA_SQL.split(";"):
            sql = stmt.strip()
            if sql:
                connection.execute(text(sql))
        self._schema_ready = True

    # ------------------------------------------------------------------
    # Status + board
    # ------------------------------------------------------------------
    def status_snapshot(
        self, connection: Connection, project_id: str
    ) -> dict[str, Any]:
        self.ensure_tables(connection)
        board = self.board(connection, project_id)
        columns = board["columns"]
        last_release = self._last_release(connection, project_id)
        last_deployment = self._last_deployment(connection, project_id)
        return {
            "project_id": project_id,
            "built_count": len(columns["built"]),
            "todo_count": len(columns["planned"]),
            "in_progress": len(columns["building"]),
            "blocked": len(columns["blocked"]),
            "last_release": last_release,
            "last_deployment": last_deployment,
            "totals": {
                "board_items": sum(len(columns[c]) for c in BOARD_COLUMNS),
                "release_plans": self._count_releases(connection, project_id),
            },
        }

    def board(
        self, connection: Connection, project_id: str
    ) -> dict[str, Any]:
        """Return board columns. Prefer durable board items; otherwise live view."""
        self.ensure_tables(connection)
        items = self._list_board_items(connection, project_id)
        if not items:
            items = self._live_board_items(connection, project_id)
        columns: dict[str, list[dict[str, Any]]] = {
            col: [] for col in BOARD_COLUMNS
        }
        for item in items:
            col = item.get("column") or "planned"
            if col not in columns:
                col = "planned"
            columns[col].append(item)
        return {
            "project_id": project_id,
            "columns": columns,
            "counts": {col: len(columns[col]) for col in BOARD_COLUMNS},
        }

    # ------------------------------------------------------------------
    # Releases
    # ------------------------------------------------------------------
    def create_release(
        self,
        connection: Connection,
        *,
        project_id: str,
        name: str,
        target_date: Optional[str] = None,
        item_ids: Optional[list[str]] = None,
        actor: str = "system",
        status: str = "planned",
        metadata: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        self.ensure_tables(connection)
        name = (name or "").strip()
        if not name:
            raise ValueError("Release plan name is required.")
        if status not in {"planned", "active", "shipped", "cancelled"}:
            raise ValueError(
                "status must be one of: planned, active, shipped, cancelled"
            )
        ids = [str(i) for i in (item_ids or []) if str(i).strip()]
        release_id = make_id("rel")
        row = connection.execute(
            text(
                f"""
                INSERT INTO release_plans (
                    id, project_id, name, target_date, status,
                    item_ids, issue_links, metadata, created_by
                )
                VALUES (
                    :id, :project_id, :name, CAST(:target_date AS DATE), :status,
                    CAST(:item_ids AS JSONB), CAST(:issue_links AS JSONB),
                    CAST(:metadata AS JSONB), :created_by
                )
                RETURNING {_PLAN_RETURNING}
                """
            ),
            {
                "id": release_id,
                "project_id": project_id,
                "name": name,
                "target_date": target_date or None,
                "status": status,
                "item_ids": json.dumps(ids),
                "issue_links": json.dumps([]),
                "metadata": json.dumps(metadata or {}),
                "created_by": actor,
            },
        ).mappings().one()
        plan = _serialize_plan(row)
        emit_event(
            connection,
            project_id,
            "release_planning.release_created",
            actor,
            "release_plan",
            plan["id"],
            {
                "name": plan["name"],
                "target_date": plan["target_date"],
                "item_count": len(ids),
            },
        )
        return plan

    def list_releases(
        self, connection: Connection, project_id: str
    ) -> list[dict[str, Any]]:
        self.ensure_tables(connection)
        rows = connection.execute(
            text(
                f"""
                SELECT {_PLAN_RETURNING}
                FROM release_plans
                WHERE project_id = :project_id
                ORDER BY target_date NULLS LAST, created_at DESC
                """
            ),
            {"project_id": project_id},
        ).mappings().all()
        return [_serialize_plan(row) for row in rows]

    def get_release(
        self, connection: Connection, project_id: str, release_id: str
    ) -> dict[str, Any]:
        self.ensure_tables(connection)
        row = connection.execute(
            text(
                f"""
                SELECT {_PLAN_RETURNING}
                FROM release_plans
                WHERE id = :id AND project_id = :project_id
                """
            ),
            {"id": release_id, "project_id": project_id},
        ).mappings().first()
        if row is None:
            raise LookupError(f"Release plan not found: {release_id}")
        plan = _serialize_plan(row)
        # Hydrate linked board items when present.
        item_ids = plan["item_ids"]
        items: list[dict[str, Any]] = []
        if item_ids:
            items = self._board_items_by_ids(connection, project_id, item_ids)
        plan["items"] = items
        return plan

    def link_issue(
        self,
        connection: Connection,
        *,
        project_id: str,
        release_id: str,
        tracker: str,
        issue_key: str,
        url: Optional[str] = None,
        actor: str = "system",
    ) -> dict[str, Any]:
        self.ensure_tables(connection)
        tracker = (tracker or "").strip().lower()
        issue_key = (issue_key or "").strip()
        if not tracker:
            raise ValueError("tracker is required (plane, jira, linear, github, ado).")
        if not issue_key:
            raise ValueError("issue_key is required.")
        resolved_url = (url or "").strip() or self._default_issue_url(
            tracker, issue_key
        )
        row = connection.execute(
            text(
                f"""
                SELECT {_PLAN_RETURNING}
                FROM release_plans
                WHERE id = :id AND project_id = :project_id
                """
            ),
            {"id": release_id, "project_id": project_id},
        ).mappings().first()
        if row is None:
            raise LookupError(f"Release plan not found: {release_id}")
        links = _as_list(row["issue_links"])
        # Upsert by tracker+issue_key
        replaced = False
        new_link = {
            "tracker": tracker,
            "issue_key": issue_key,
            "url": resolved_url,
            "linked_by": actor,
            "linked_at": datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
        }
        for idx, existing in enumerate(links):
            if not isinstance(existing, dict):
                continue
            if (
                str(existing.get("tracker", "")).lower() == tracker
                and str(existing.get("issue_key", "")) == issue_key
            ):
                links[idx] = {**existing, **new_link}
                replaced = True
                break
        if not replaced:
            links.append(new_link)
        updated = connection.execute(
            text(
                f"""
                UPDATE release_plans
                SET issue_links = CAST(:issue_links AS JSONB),
                    updated_at = now()
                WHERE id = :id AND project_id = :project_id
                RETURNING {_PLAN_RETURNING}
                """
            ),
            {
                "id": release_id,
                "project_id": project_id,
                "issue_links": json.dumps(links),
            },
        ).mappings().one()
        plan = _serialize_plan(updated)
        emit_event(
            connection,
            project_id,
            "release_planning.issue_linked",
            actor,
            "release_plan",
            release_id,
            {
                "tracker": tracker,
                "issue_key": issue_key,
                "url": resolved_url,
                "replaced": replaced,
            },
        )
        return plan

    # ------------------------------------------------------------------
    # Sync
    # ------------------------------------------------------------------
    def sync_from_features(
        self,
        connection: Connection,
        project_id: str,
        *,
        actor: str = "system",
    ) -> dict[str, Any]:
        """Pull feature catalog entries into the durable board."""
        self.ensure_tables(connection)
        entries = self.features.list_entries(connection, project_id)
        upserted = 0
        for entry in entries:
            column = _feature_column(entry.get("status"))
            self._upsert_board_item(
                connection,
                project_id=project_id,
                column_key=column,
                source_type="feature",
                source_id=entry["id"],
                title=entry.get("title") or entry.get("label") or entry["id"],
                status=entry.get("status"),
                metadata={
                    "label": entry.get("label"),
                    "slug": entry.get("slug"),
                    "doc_path": entry.get("doc_path"),
                    "needs_label": entry.get("needs_label"),
                },
            )
            upserted += 1
        emit_event(
            connection,
            project_id,
            "release_planning.synced_features",
            actor,
            "project",
            project_id,
            {"upserted": upserted},
        )
        return {
            "project_id": project_id,
            "source": "features",
            "upserted": upserted,
            "board": self.board(connection, project_id),
        }

    def sync_from_trackers(
        self,
        connection: Connection,
        project_id: str,
        *,
        actor: str = "system",
    ) -> dict[str, Any]:
        """Best-effort pull from work tracker bindings. Failures are soft."""
        self.ensure_tables(connection)
        upserted = 0
        errors: list[dict[str, str]] = []
        try:
            bindings = self.work.list_tracker_bindings(connection, project_id)
        except Exception as exc:  # noqa: BLE001 — fail soft
            errors.append({"stage": "list_bindings", "error": str(exc)})
            bindings = []

        for binding in bindings:
            try:
                external_state = (
                    binding.get("external_state")
                    or binding.get("sync_status")
                    or "open"
                )
                column = _work_column(str(external_state))
                # Prefer work item status when binding is still syncing.
                if column == "planned" and binding.get("priority_band") in {
                    "p0",
                    "p1",
                }:
                    column = "building"
                title = (
                    binding.get("work_title")
                    or binding.get("external_key")
                    or binding.get("id")
                )
                self._upsert_board_item(
                    connection,
                    project_id=project_id,
                    column_key=column,
                    source_type="tracker",
                    source_id=str(binding["id"]),
                    title=str(title),
                    status=str(external_state),
                    external_url=binding.get("external_url"),
                    tracker=binding.get("provider_key"),
                    issue_key=binding.get("external_key"),
                    metadata={
                        "work_item_id": binding.get("work_item_id"),
                        "priority_band": binding.get("priority_band"),
                        "effective_score": binding.get("effective_score"),
                        "sync_status": binding.get("sync_status"),
                    },
                )
                upserted += 1
            except Exception as exc:  # noqa: BLE001 — fail soft per binding
                errors.append(
                    {
                        "stage": "binding",
                        "binding_id": str(binding.get("id")),
                        "error": str(exc),
                    }
                )

        # Also fold open work_items that may lack bindings.
        try:
            work_items = self.work.list_work_items(connection, project_id, limit=200)
            for item in work_items:
                try:
                    column = _work_column(item.get("status"))
                    if item.get("allocation_status") == "allocated" and column == "planned":
                        column = "building"
                    self._upsert_board_item(
                        connection,
                        project_id=project_id,
                        column_key=column,
                        source_type="work_item",
                        source_id=item["id"],
                        title=item.get("title") or item["id"],
                        status=item.get("status"),
                        metadata={
                            "subject_type": item.get("subject_type"),
                            "subject_id": item.get("subject_id"),
                            "priority_band": item.get("priority_band"),
                            "effective_score": item.get("effective_score"),
                            "allocation_status": item.get("allocation_status"),
                        },
                    )
                    upserted += 1
                except Exception as exc:  # noqa: BLE001
                    errors.append(
                        {
                            "stage": "work_item",
                            "work_item_id": str(item.get("id")),
                            "error": str(exc),
                        }
                    )
        except Exception as exc:  # noqa: BLE001
            errors.append({"stage": "list_work_items", "error": str(exc)})

        emit_event(
            connection,
            project_id,
            "release_planning.synced_trackers",
            actor,
            "project",
            project_id,
            {"upserted": upserted, "error_count": len(errors)},
        )
        return {
            "project_id": project_id,
            "source": "trackers",
            "upserted": upserted,
            "errors": errors,
            "ok": True,  # always soft-success
            "board": self.board(connection, project_id),
        }

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _list_board_items(
        self, connection: Connection, project_id: str
    ) -> list[dict[str, Any]]:
        rows = connection.execute(
            text(
                f"""
                SELECT {_ITEM_RETURNING}
                FROM release_board_items
                WHERE project_id = :project_id
                ORDER BY sort_order, updated_at DESC
                """
            ),
            {"project_id": project_id},
        ).mappings().all()
        return [_serialize_item(row) for row in rows]

    def _board_items_by_ids(
        self,
        connection: Connection,
        project_id: str,
        item_ids: list[str],
    ) -> list[dict[str, Any]]:
        if not item_ids:
            return []
        # Expand IDs in Python to avoid driver-specific array binds.
        id_set = {str(i) for i in item_ids}
        rows = connection.execute(
            text(
                f"""
                SELECT {_ITEM_RETURNING}
                FROM release_board_items
                WHERE project_id = :project_id
                ORDER BY sort_order, updated_at DESC
                """
            ),
            {"project_id": project_id},
        ).mappings().all()
        return [
            _serialize_item(row)
            for row in rows
            if row["id"] in id_set or row["source_id"] in id_set
        ]

    def _live_board_items(
        self, connection: Connection, project_id: str
    ) -> list[dict[str, Any]]:
        """Compose a board from live sources without writing durable rows."""
        items: list[dict[str, Any]] = []
        try:
            for entry in self.features.list_entries(connection, project_id):
                items.append(
                    {
                        "id": f"live_feature_{entry['id']}",
                        "project_id": project_id,
                        "column": _feature_column(entry.get("status")),
                        "source_type": "feature",
                        "source_id": entry["id"],
                        "title": entry.get("title")
                        or entry.get("label")
                        or entry["id"],
                        "status": entry.get("status"),
                        "external_url": None,
                        "tracker": None,
                        "issue_key": entry.get("label"),
                        "metadata": {
                            "label": entry.get("label"),
                            "slug": entry.get("slug"),
                            "live": True,
                        },
                        "sort_order": 0,
                        "created_at": entry.get("created_at"),
                        "updated_at": entry.get("updated_at"),
                    }
                )
        except Exception:  # noqa: BLE001
            pass

        try:
            for item in self.work.list_work_items(connection, project_id, limit=200):
                column = _work_column(item.get("status"))
                if item.get("allocation_status") == "allocated" and column == "planned":
                    column = "building"
                items.append(
                    {
                        "id": f"live_work_{item['id']}",
                        "project_id": project_id,
                        "column": column,
                        "source_type": "work_item",
                        "source_id": item["id"],
                        "title": item.get("title") or item["id"],
                        "status": item.get("status"),
                        "external_url": None,
                        "tracker": None,
                        "issue_key": None,
                        "metadata": {
                            "priority_band": item.get("priority_band"),
                            "live": True,
                        },
                        "sort_order": 0,
                        "created_at": item.get("created_at"),
                        "updated_at": item.get("updated_at"),
                    }
                )
        except Exception:  # noqa: BLE001
            pass

        try:
            task_rows = connection.execute(
                text(
                    """
                    SELECT id, title, status, priority, updated_at, created_at
                    FROM tasks
                    WHERE project_id = :project_id
                      AND status NOT IN ('done', 'completed', 'cancelled')
                    ORDER BY updated_at DESC
                    LIMIT 100
                    """
                ),
                {"project_id": project_id},
            ).mappings().all()
            for row in task_rows:
                items.append(
                    {
                        "id": f"live_task_{row['id']}",
                        "project_id": project_id,
                        "column": _work_column(row["status"]),
                        "source_type": "task",
                        "source_id": row["id"],
                        "title": row["title"],
                        "status": row["status"],
                        "external_url": None,
                        "tracker": None,
                        "issue_key": None,
                        "metadata": {
                            "priority": row["priority"],
                            "live": True,
                        },
                        "sort_order": 0,
                        "created_at": _iso(row["created_at"]),
                        "updated_at": _iso(row["updated_at"]),
                    }
                )
        except Exception:  # noqa: BLE001
            pass

        try:
            deploy_rows = connection.execute(
                text(
                    """
                    SELECT id, title, status, environment, pull_request_url,
                           created_at, updated_at
                    FROM deployment_rollouts
                    WHERE project_id = :project_id
                    ORDER BY created_at DESC
                    LIMIT 20
                    """
                ),
                {"project_id": project_id},
            ).mappings().all()
            for row in deploy_rows:
                status = (row["status"] or "").lower()
                if status in {"succeeded", "completed", "shipped", "healthy"}:
                    column = "built"
                elif status in {"failed", "rolled_back", "blocked"}:
                    column = "blocked"
                else:
                    column = "building"
                items.append(
                    {
                        "id": f"live_deploy_{row['id']}",
                        "project_id": project_id,
                        "column": column,
                        "source_type": "deployment",
                        "source_id": row["id"],
                        "title": row["title"] or f"Deploy {row['id']}",
                        "status": row["status"],
                        "external_url": row["pull_request_url"],
                        "tracker": None,
                        "issue_key": None,
                        "metadata": {
                            "environment": row["environment"],
                            "live": True,
                        },
                        "sort_order": 0,
                        "created_at": _iso(row["created_at"]),
                        "updated_at": _iso(row["updated_at"]),
                    }
                )
        except Exception:  # noqa: BLE001
            pass

        return items

    def _upsert_board_item(
        self,
        connection: Connection,
        *,
        project_id: str,
        column_key: str,
        source_type: str,
        source_id: str,
        title: str,
        status: Optional[str] = None,
        external_url: Optional[str] = None,
        tracker: Optional[str] = None,
        issue_key: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
        sort_order: int = 0,
    ) -> dict[str, Any]:
        if column_key not in BOARD_COLUMNS:
            column_key = "planned"
        item_id = make_id("rbi")
        row = connection.execute(
            text(
                f"""
                INSERT INTO release_board_items (
                    id, project_id, column_key, source_type, source_id,
                    title, status, external_url, tracker, issue_key,
                    metadata, sort_order
                )
                VALUES (
                    :id, :project_id, :column_key, :source_type, :source_id,
                    :title, :status, :external_url, :tracker, :issue_key,
                    CAST(:metadata AS JSONB), :sort_order
                )
                ON CONFLICT (project_id, source_type, source_id) DO UPDATE SET
                    column_key = EXCLUDED.column_key,
                    title = EXCLUDED.title,
                    status = EXCLUDED.status,
                    external_url = COALESCE(EXCLUDED.external_url, release_board_items.external_url),
                    tracker = COALESCE(EXCLUDED.tracker, release_board_items.tracker),
                    issue_key = COALESCE(EXCLUDED.issue_key, release_board_items.issue_key),
                    metadata = EXCLUDED.metadata,
                    sort_order = EXCLUDED.sort_order,
                    updated_at = now()
                RETURNING {_ITEM_RETURNING}
                """
            ),
            {
                "id": item_id,
                "project_id": project_id,
                "column_key": column_key,
                "source_type": source_type,
                "source_id": source_id,
                "title": title,
                "status": status,
                "external_url": external_url,
                "tracker": tracker,
                "issue_key": issue_key,
                "metadata": json.dumps(metadata or {}),
                "sort_order": sort_order,
            },
        ).mappings().one()
        return _serialize_item(row)

    def _last_release(
        self, connection: Connection, project_id: str
    ) -> Optional[dict[str, Any]]:
        row = connection.execute(
            text(
                f"""
                SELECT {_PLAN_RETURNING}
                FROM release_plans
                WHERE project_id = :project_id
                ORDER BY
                  CASE status
                    WHEN 'shipped' THEN 0
                    WHEN 'active' THEN 1
                    ELSE 2
                  END,
                  target_date DESC NULLS LAST,
                  created_at DESC
                LIMIT 1
                """
            ),
            {"project_id": project_id},
        ).mappings().first()
        return _serialize_plan(row) if row else None

    def _last_deployment(
        self, connection: Connection, project_id: str
    ) -> Optional[dict[str, Any]]:
        try:
            row = connection.execute(
                text(
                    """
                    SELECT id, title, status, environment, pull_request_url,
                           created_at, updated_at
                    FROM deployment_rollouts
                    WHERE project_id = :project_id
                    ORDER BY created_at DESC
                    LIMIT 1
                    """
                ),
                {"project_id": project_id},
            ).mappings().first()
        except Exception:  # noqa: BLE001
            return None
        if row is None:
            return None
        return {
            "id": row["id"],
            "title": row["title"],
            "status": row["status"],
            "environment": row["environment"],
            "pull_request_url": row["pull_request_url"],
            "created_at": _iso(row["created_at"]),
            "updated_at": _iso(row["updated_at"]),
        }

    def _count_releases(
        self, connection: Connection, project_id: str
    ) -> int:
        return int(
            connection.execute(
                text(
                    "SELECT count(*) FROM release_plans WHERE project_id = :project_id"
                ),
                {"project_id": project_id},
            ).scalar()
            or 0
        )

    def _default_issue_url(self, tracker: str, issue_key: str) -> Optional[str]:
        """Best-effort URL templates from settings (Plane etc.)."""
        tracker = tracker.lower()
        if tracker == "plane":
            base = (
                getattr(self.settings, "plane_project_url", None)
                or getattr(self.settings, "plane_base_url", None)
            )
            if base:
                base = str(base).rstrip("/")
                # Plane issue deep links commonly: {project_url}/issues/{key}
                if "/projects/" in base:
                    return f"{base}/issues/{issue_key}"
                workspace = getattr(self.settings, "plane_workspace", "goldeneye")
                return f"{base}/{workspace}/browse/{issue_key}"
            return (
                f"https://plane.local.sean.network/goldeneye/browse/{issue_key}"
            )
        if tracker == "jira":
            return None  # needs site host
        if tracker == "github":
            # issue_key may be owner/repo#123 or just #123
            if "#" in issue_key:
                repo, num = issue_key.rsplit("#", 1)
                if "/" in repo:
                    return f"https://github.com/{repo}/issues/{num}"
            return None
        if tracker == "linear":
            return f"https://linear.app/issue/{issue_key}"
        return None
