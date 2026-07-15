from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..config import Settings
from .core import emit_event, make_id


_TRACEABILITY_ROW = re.compile(
    r"^([A-Z][A-Z0-9]*(?:-[A-Z0-9]+)*-\d{3})\s+(.+)$"
)
_REQUIREMENT_LABEL = re.compile(
    r"\b[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)*-\d{3}\b"
)


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug[:120] or "feature"


def _cell_refs(value: str) -> list[str]:
    return [
        item.strip().strip("`")
        for item in value.split(",")
        if item.strip().strip("`")
    ]


def parse_traceability(markdown: str) -> list[dict[str, Any]]:
    """Parse requirement rows from both traceability tables in the product ledger."""
    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    for line in markdown.splitlines():
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) < 5:
            continue
        match = _TRACEABILITY_ROW.fullmatch(cells[0])
        if match is None:
            continue
        label, title = match.groups()
        if label in seen:
            continue
        seen.add(label)
        slug = _slugify(f"{label}-{title}")
        entries.append(
            {
                "label": label,
                "title": title.strip(),
                "slug": slug,
                "doc_path": f"docs/features/{slug}.md",
                "source": cells[1],
                "implementation_files": _cell_refs(cells[2]),
                "test_coverage": _cell_refs(cells[3]),
                "status": cells[4],
                "source_refs": [
                    {
                        "kind": "traceability",
                        "path": "docs/product/TRACEABILITY.md",
                        "requirement": label,
                    }
                ],
                "needs_label": False,
            }
        )
    return entries


def _serialize(row, *, idempotent: bool = False) -> dict[str, Any]:
    return {
        "id": row["id"],
        "project_id": row["project_id"],
        "label": row["label"],
        "slug": row["slug"],
        "title": row["title"],
        "doc_path": row["doc_path"],
        "status": row["status"],
        "source": row["source"],
        "implementation_files": row["implementation_files"] or [],
        "test_coverage": row["test_coverage"] or [],
        "source_refs": row["source_refs"] or [],
        "needs_label": bool(row["needs_label"]),
        "last_documented_event_id": row["last_documented_event_id"],
        "last_documented_at": row["last_documented_at"].isoformat()
        if row["last_documented_at"] is not None
        else None,
        "created_at": row["created_at"].isoformat()
        if hasattr(row["created_at"], "isoformat")
        else row["created_at"],
        "updated_at": row["updated_at"].isoformat()
        if hasattr(row["updated_at"], "isoformat")
        else row["updated_at"],
        "idempotent": idempotent,
    }


_RETURNING = """
    id, project_id, label, slug, title, doc_path, status, source,
    implementation_files, test_coverage, source_refs, needs_label,
    last_documented_event_id, last_documented_at, created_at, updated_at
"""


class FeatureCatalog:
    def __init__(self, settings: Settings):
        self.settings = settings

    @property
    def traceability_path(self) -> Path:
        return self.settings.repo_root / "docs" / "product" / "TRACEABILITY.md"

    def list_entries(
        self, connection: Connection, project_id: str
    ) -> list[dict[str, Any]]:
        rows = connection.execute(
            text(
                f"""
                SELECT {_RETURNING}
                FROM feature_catalog
                WHERE project_id = :project_id
                ORDER BY label NULLS LAST, title
                """
            ),
            {"project_id": project_id},
        ).mappings().all()
        return [_serialize(row) for row in rows]

    def seed_from_traceability(
        self,
        connection: Connection,
        project_id: str,
        path: Optional[Path] = None,
    ) -> list[dict[str, Any]]:
        source_path = path or self.traceability_path
        entries = parse_traceability(source_path.read_text(encoding="utf-8"))
        seeded: list[dict[str, Any]] = []
        for entry in entries:
            seeded.append(
                self._upsert_entry(connection, project_id, entry, emit=False)
            )
        try:
            source_ref = str(source_path.relative_to(self.settings.repo_root))
        except ValueError:
            source_ref = str(source_path)
        emit_event(
            connection,
            project_id,
            "feature_catalog.seeded",
            "bootstrap",
            "project",
            project_id,
            {
                "source": source_ref,
                "entry_count": len(seeded),
            },
        )
        return seeded

    def upsert_entry(
        self,
        connection: Connection,
        project_id: str,
        entry: dict[str, Any],
        *,
        actor: str = "system",
    ) -> dict[str, Any]:
        return self._upsert_entry(
            connection, project_id, entry, emit=True, actor=actor
        )

    def _upsert_entry(
        self,
        connection: Connection,
        project_id: str,
        entry: dict[str, Any],
        *,
        emit: bool,
        actor: str = "system",
    ) -> dict[str, Any]:
        label = entry.get("label") or None
        title = str(entry["title"]).strip()
        slug = str(entry.get("slug") or _slugify(f"{label or ''}-{title}"))
        doc_path = str(entry.get("doc_path") or f"docs/features/{slug}.md")
        conflict_target = "(project_id, label)" if label else "(project_id, slug)"
        lookup = "label = :lookup" if label else "slug = :lookup"
        existing = connection.execute(
            text(
                f"""
                SELECT source_refs
                FROM feature_catalog
                WHERE project_id = :project_id AND {lookup}
                """
            ),
            {"project_id": project_id, "lookup": label or slug},
        ).mappings().first()
        source_refs = list(existing["source_refs"] or []) if existing else []
        for source_ref in entry.get("source_refs") or []:
            if source_ref not in source_refs:
                source_refs.append(source_ref)
        row = connection.execute(
            text(
                f"""
                INSERT INTO feature_catalog (
                    id, project_id, label, slug, title, doc_path, status, source,
                    implementation_files, test_coverage, source_refs, needs_label
                )
                VALUES (
                    :id, :project_id, :label, :slug, :title, :doc_path,
                    :status, :source, CAST(:implementation_files AS JSONB),
                    CAST(:test_coverage AS JSONB), CAST(:source_refs AS JSONB),
                    :needs_label
                )
                ON CONFLICT {conflict_target}
                DO UPDATE SET
                    label = COALESCE(EXCLUDED.label, feature_catalog.label),
                    slug = EXCLUDED.slug,
                    title = EXCLUDED.title,
                    doc_path = EXCLUDED.doc_path,
                    status = EXCLUDED.status,
                    source = EXCLUDED.source,
                    implementation_files = EXCLUDED.implementation_files,
                    test_coverage = EXCLUDED.test_coverage,
                    source_refs = EXCLUDED.source_refs,
                    needs_label = EXCLUDED.needs_label,
                    updated_at = now()
                RETURNING {_RETURNING}
                """
            ),
            {
                "id": entry.get("id") or make_id("feature"),
                "project_id": project_id,
                "label": label,
                "slug": slug,
                "title": title,
                "doc_path": doc_path,
                "status": entry.get("status") or "Planned",
                "source": entry.get("source"),
                "implementation_files": json.dumps(
                    entry.get("implementation_files") or []
                ),
                "test_coverage": json.dumps(entry.get("test_coverage") or []),
                "source_refs": json.dumps(source_refs),
                "needs_label": bool(entry.get("needs_label", label is None)),
            },
        ).mappings().one()
        if emit:
            emit_event(
                connection,
                project_id,
                "feature_catalog.entry_upserted",
                actor,
                "feature_catalog_entry",
                row["id"],
                {"label": row["label"], "slug": row["slug"]},
            )
        return _serialize(row)

    def resolve(
        self,
        connection: Connection,
        project_id: str,
        entity_type: str,
        entity_id: str,
        *,
        event_id: Optional[str] = None,
        actor: str = "system",
    ) -> dict[str, Any]:
        entity = self._load_entity(
            connection, project_id, entity_type, entity_id
        )
        label_match = _REQUIREMENT_LABEL.search(entity["searchable"])
        label = label_match.group(0) if label_match else None
        row = None
        if label:
            row = connection.execute(
                text(
                    f"""
                    SELECT {_RETURNING}
                    FROM feature_catalog
                    WHERE project_id = :project_id AND label = :label
                    """
                ),
                {"project_id": project_id, "label": label},
            ).mappings().first()
        if row is not None and event_id and row["last_documented_event_id"] == event_id:
            return _serialize(row, idempotent=True)

        source_ref = {
            "kind": "entity",
            "entity_type": entity_type,
            "entity_id": entity_id,
        }
        if row is None:
            title = entity["title"] or f"{entity_type} {entity_id}"
            entry = {
                "label": label,
                "title": title,
                "slug": _slugify(
                    f"{label}-{title}" if label else f"{entity_type}-{entity_id}-{title}"
                ),
                "status": "Partial",
                "source": f"Emergent from {entity_type} {entity_id}",
                "source_refs": [source_ref],
                "needs_label": True,
            }
            resolved = self._upsert_entry(
                connection, project_id, entry, emit=False
            )
            created = True
        else:
            refs = list(row["source_refs"] or [])
            if source_ref not in refs:
                refs.append(source_ref)
                row = connection.execute(
                    text(
                        f"""
                        UPDATE feature_catalog
                        SET source_refs = CAST(:source_refs AS JSONB), updated_at = now()
                        WHERE id = :id AND project_id = :project_id
                        RETURNING {_RETURNING}
                        """
                    ),
                    {
                        "id": row["id"],
                        "project_id": project_id,
                        "source_refs": json.dumps(refs),
                    },
                ).mappings().one()
            resolved = _serialize(row)
            created = False

        emit_event(
            connection,
            project_id,
            "feature_catalog.resolved",
            actor,
            "feature_catalog_entry",
            resolved["id"],
            {
                "entity_type": entity_type,
                "entity_id": entity_id,
                "label": resolved["label"],
                "created": created,
            },
        )
        return resolved

    def mark_documented(
        self,
        connection: Connection,
        project_id: str,
        entry_id: str,
        event_id: str,
        *,
        actor: str = "system",
    ) -> dict[str, Any]:
        row = connection.execute(
            text(
                f"""
                UPDATE feature_catalog
                SET last_documented_event_id = :event_id,
                    last_documented_at = now(),
                    updated_at = now()
                WHERE id = :entry_id AND project_id = :project_id
                RETURNING {_RETURNING}
                """
            ),
            {
                "entry_id": entry_id,
                "project_id": project_id,
                "event_id": event_id,
            },
        ).mappings().first()
        if row is None:
            raise ValueError("Feature catalog entry not found in project.")
        emit_event(
            connection,
            project_id,
            "feature_catalog.documented",
            actor,
            "feature_catalog_entry",
            entry_id,
            {"event_id": event_id},
        )
        return _serialize(row)

    def _load_entity(
        self,
        connection: Connection,
        project_id: str,
        entity_type: str,
        entity_id: str,
    ) -> dict[str, str]:
        queries = {
            "code_task_run": """
                SELECT r.title,
                       concat_ws(' ', r.title, r.spec::text, r.state::text,
                                 t.title, t.description) AS searchable
                FROM code_task_runs r
                LEFT JOIN tasks t ON t.id = r.task_id AND t.project_id = r.project_id
                WHERE r.id = :entity_id AND r.project_id = :project_id
            """,
            "discovery_run": """
                SELECT title,
                       concat_ws(' ', title, seed, context, state::text) AS searchable
                FROM discovery_runs
                WHERE id = :entity_id AND project_id = :project_id
            """,
            "plan": """
                SELECT title,
                       concat_ws(' ', title, objective, phases::text,
                                 success_criteria::text, risks::text) AS searchable
                FROM plans
                WHERE id = :entity_id AND project_id = :project_id
            """,
        }
        query = queries.get(entity_type)
        if query is None:
            raise ValueError(
                "entity_type must be one of: code_task_run, discovery_run, plan."
            )
        row = connection.execute(
            text(query),
            {"entity_id": entity_id, "project_id": project_id},
        ).mappings().first()
        if row is None:
            raise ValueError("Feature source entity not found in project.")
        return {
            "title": row["title"] or "",
            "searchable": row["searchable"] or "",
        }
