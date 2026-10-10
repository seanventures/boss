"""Bounded, editable checks copied from an accepted recorded result.

Revisions are ordinary immutable artifacts, not workflow definitions or authority.
No filesystem, adapter, model or job is used. The caller authorizes a human in the
project; a run lock fences review changes and an artifact lock fences edits.
"""
from __future__ import annotations

import hashlib
import json
import re

from sqlalchemy import text

from . import deliverable_review
from .core import emit_event, make_id

TYPE = "acceptance_check_template"


class TemplateConflict(ValueError):
    pass


def _hash(value: dict) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _content(title: str, criteria: list[str]) -> tuple[str, list[str]]:
    title = title.strip()
    if not title or len(title) > 120:
        raise ValueError("Name the checks in 1–120 characters.")
    if any(ord(char) < 32 for char in title):
        raise ValueError("The check name cannot contain control characters.")
    if not isinstance(criteria, list) or not 1 <= len(criteria) <= 20:
        raise ValueError("Keep 1–20 acceptance checks.")
    clean = []
    for item in criteria:
        if not isinstance(item, str) or not item.strip() or len(item.strip()) > 500:
            raise ValueError("Each acceptance check needs 1–500 characters.")
        if any(ord(char) < 32 and char not in "\n\t" for char in item):
            raise ValueError("Acceptance checks cannot contain control characters.")
        clean.append(item.strip())
    if sum(len(item) for item in clean) > 6000:
        raise ValueError("Keep the checks within 6,000 characters.")
    return title, clean


def _submission(value: str) -> None:
    if not re.fullmatch(r"[A-Za-z0-9_-]{16,80}", value):
        raise ValueError("Invalid submission identifier.")


def _accepted(connection, project_id: str, run_id: str, review_id: str,
              snapshot_hash: str) -> dict:
    found = connection.execute(text("SELECT id FROM code_task_runs WHERE id=:r AND project_id=:p FOR UPDATE"),
                               {"r": run_id, "p": project_id}).scalar()
    if found is None:
        raise LookupError("Source result not found in this project.")
    latest = connection.execute(text("""
        SELECT id,outcome,snapshot_hash FROM deliverable_reviews
        WHERE project_id=:p AND run_id=:r ORDER BY created_at DESC,id DESC LIMIT 1
    """), {"p": project_id, "r": run_id}).mappings().first()
    current = deliverable_review.snapshot(connection, project_id, run_id)
    if (not latest or latest["id"] != review_id or latest["outcome"] != "accepted"
            or latest["snapshot_hash"] != snapshot_hash or not current["reviewable"]
            or current["snapshot_hash"] != snapshot_hash):
        raise TemplateConflict("The source needs a current accepted review of this exact evidence. Reload the result.")
    snap = current["snapshot"]
    # Deliberately do not copy the private brief, review note, receipts, code or logs.
    return {"run_id": run_id, "review_id": review_id, "snapshot_hash": snapshot_hash,
            "delivered_head": snap["delivered_head"], "check_state": snap["checks"]["state"],
            "criteria": snap["criteria"]}


def _project(connection, row: dict) -> dict:
    content = row["content"]
    return {"id": row["id"], "template_id": content["template_id"],
            "revision": content["revision"], "revision_hash": row["content_hash"],
            "title": content["title"], "criteria": content["criteria"],
            "source": content["source"], "actor": content["actor"],
            "created_at": row["created_at"]}


def list_latest(connection, project_id: str) -> list[dict]:
    rows = connection.execute(text("""
        SELECT * FROM (
          SELECT DISTINCT ON (content->>'template_id') id,content,content_hash,created_at
          FROM artifacts WHERE project_id=:p AND artifact_type=:kind
          ORDER BY content->>'template_id',(content->>'revision')::integer DESC
        ) latest ORDER BY created_at DESC,id DESC LIMIT 100
    """), {"p": project_id, "kind": TYPE}).mappings()
    return [_project(connection, row) for row in rows]


def _replay(connection, project_id: str, actor: str, submission_id: str, request_hash: str):
    prior = connection.execute(text("""
        SELECT id,content,content_hash,created_at FROM artifacts
        WHERE project_id=:p AND artifact_type=:kind AND content->>'actor'=:a
          AND content->>'submission_id'=:s
    """), {"p": project_id, "kind": TYPE, "a": actor, "s": submission_id}).mappings().first()
    if prior:
        if prior["content"]["request_hash"] != request_hash:
            raise TemplateConflict("This submission already saved different checks.")
        return {**_project(connection, prior), "reused": True}
    return None


def _lock_submission(connection, project_id: str, actor: str, submission_id: str) -> None:
    # No new uniqueness schema: all writes use this transaction lock plus immutable rows.
    connection.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"),
                       {"key": f"acceptance-template:{project_id}:{actor}:{submission_id}"})


def _insert(connection, *, project_id: str, actor: str, submission_id: str,
            request_hash: str, template_id: str | None, revision: int, parent_id: str | None,
            title: str, criteria: list[str], source: dict) -> dict:
    artifact_id = make_id("acheck")
    content = {"template_id": template_id or artifact_id, "revision": revision,
               "parent_id": parent_id, "title": title, "criteria": criteria, "source": source,
               "actor": actor, "submission_id": submission_id, "request_hash": request_hash}
    row = connection.execute(text("""
        INSERT INTO artifacts(id,project_id,artifact_type,title,content,content_hash,data_boundary)
        VALUES(:id,:p,:kind,:title,CAST(:body AS JSONB),:hash,'internal')
        RETURNING id,content,content_hash,created_at
    """), {"id": artifact_id, "p": project_id, "kind": TYPE, "title": title,
            "body": json.dumps(content), "hash": _hash(content)}).mappings().one()
    emit_event(connection, project_id, "acceptance_checks.saved", actor, "artifact", artifact_id,
               {"template_id": content["template_id"], "revision": revision,
                "source_review_id": source["review_id"], "source_snapshot_hash": source["snapshot_hash"]})
    return {**_project(connection, row), "reused": False}


def create(connection, *, project_id: str, actor: str, run_id: str, review_id: str,
           snapshot_hash: str, title: str, submission_id: str) -> dict:
    _submission(submission_id)
    request_hash = _hash({"run_id": run_id, "review_id": review_id,
                          "snapshot_hash": snapshot_hash, "title": title.strip()})
    _lock_submission(connection, project_id, actor, submission_id)
    prior = _replay(connection, project_id, actor, submission_id, request_hash)
    if prior:
        return prior
    source = _accepted(connection, project_id, run_id, review_id, snapshot_hash)
    title, criteria = _content(title, source.pop("criteria"))
    return _insert(connection, project_id=project_id, actor=actor, submission_id=submission_id,
                   request_hash=request_hash, template_id=None, revision=1, parent_id=None,
                   title=title, criteria=criteria, source=source)


def revise(connection, *, project_id: str, actor: str, template_id: str,
           expected_revision_hash: str, title: str, criteria: list[str], submission_id: str) -> dict:
    _submission(submission_id)
    title, criteria = _content(title, criteria)
    request_hash = _hash({"template_id": template_id, "expected_revision_hash": expected_revision_hash,
                          "title": title, "criteria": criteria})
    _lock_submission(connection, project_id, actor, submission_id)
    prior = _replay(connection, project_id, actor, submission_id, request_hash)
    if prior:
        return prior
    # The first immutable artifact is the stable serialization point for all revisions.
    root = connection.execute(text("""
        SELECT id FROM artifacts WHERE id=:id AND project_id=:p AND artifact_type=:kind FOR UPDATE
    """), {"id": template_id, "p": project_id, "kind": TYPE}).scalar()
    if root is None:
        raise LookupError("Saved checks not found in this project.")
    latest = connection.execute(text("""
        SELECT id,content,content_hash FROM artifacts WHERE project_id=:p AND artifact_type=:kind
          AND content->>'template_id'=:id ORDER BY (content->>'revision')::integer DESC LIMIT 1
    """), {"p": project_id, "kind": TYPE, "id": template_id}).mappings().one()
    if latest["content_hash"] != expected_revision_hash:
        raise TemplateConflict("These checks changed in another edit. Reload before saving.")
    source = latest["content"]["source"]
    _accepted(connection, project_id, source["run_id"], source["review_id"], source["snapshot_hash"])
    return _insert(connection, project_id=project_id, actor=actor, submission_id=submission_id,
                   request_hash=request_hash, template_id=template_id,
                   revision=latest["content"]["revision"] + 1, parent_id=latest["id"],
                   title=title, criteria=criteria, source=source)
