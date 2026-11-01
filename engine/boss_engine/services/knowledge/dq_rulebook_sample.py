"""DQ sample: the NorthPeak Bank Loan Data Quality Rulebook v3.2, through the upload path (G7).

Knowledge Graph › Documents › "DQ sample" stages the shipped rulebook PDF
(``tools/dq_sample/NorthPeak_Loan_DQ_Rulebook_v3.2.pdf``, 45 pages, 96 rule cards) through the
SAME ``document_upload.stage_upload`` a person's own file takes: the person sees the proposed
sections, confirms them, and only then is the document written. "Extract rules" then starts the
``rulebook_extraction`` workflow on it. The rules come from the PDF's text — never from the
sample data the PDF was rendered from.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from . import document_upload

TITLE = "NorthPeak Bank: Loan Data Quality Rulebook v3.2"
FILENAME = "NorthPeak_Loan_DQ_Rulebook_v3.2.pdf"


def pdf_path() -> Path:
    return Path(__file__).resolve().parents[4] / "tools" / "dq_sample" / FILENAME


def available() -> bool:
    return pdf_path().is_file()


def stage(connection: Connection, *, project_id: str, actor_kind: str = "person",
          actor_ref: Optional[str] = None) -> dict[str, Any]:
    """Stage the shipped rulebook as a pending upload; return its preview (nothing written)."""
    path = pdf_path()
    if not path.is_file():
        raise document_upload.UploadRefused(
            "The DQ sample rulebook is not in this install (tools/dq_sample is missing).")
    return document_upload.stage_upload(
        connection, project_id=project_id, payload=path.read_bytes(), filename=FILENAME,
        title=TITLE, actor_kind=actor_kind, actor_ref=actor_ref)


def status(connection: Connection, *, project_id: str) -> dict[str, Any]:
    """Whether the sample can be loaded, its pending preview, the confirmed document, and how
    many rules have been proposed from it and approved."""
    from .rules import DQ_RULE_KIND, dq_kinds_missing

    document_upload.ensure_schema(connection)
    pending = connection.execute(text(
        "SELECT id FROM kg_document_uploads WHERE project_id = :p AND filename = :f AND status = 'pending' "
        "ORDER BY created_at DESC LIMIT 1"), {"p": project_id, "f": FILENAME}).scalar()
    document = connection.execute(text(
        "SELECT id, name FROM kg_nodes WHERE project_id = :p AND kind = 'document' AND valid_to IS NULL "
        "AND attrs->>'filename' = :f ORDER BY last_seen DESC LIMIT 1"),
        {"p": project_id, "f": FILENAME}).mappings().first()
    counts = {"proposed": 0, "approved": 0}
    sections = 0
    if document is not None:
        sections = int(connection.execute(text(
            "SELECT count(*) FROM kg_nodes WHERE project_id = :p AND kind = 'clause' AND valid_to IS NULL "
            "AND attrs->>'document_id' = :d"), {"p": project_id, "d": document["id"]}).scalar() or 0)
        from .rules import rule_counts

        found = rule_counts(connection, project_id, kinds=[DQ_RULE_KIND],
                            where=" AND attrs->'dq'->>'document_id' = :d", params={"d": document["id"]})
        counts = {k: found[k] for k in counts}
    missing = dq_kinds_missing(connection, project_id)
    return {
        "available": available(), "title": TITLE, "filename": FILENAME,
        "pending_upload_id": pending,
        "document_id": document["id"] if document else None,
        "sections": sections, "rules": counts,
        "workflow_key": "rulebook_extraction",
        "can_extract": not missing,
        "extract_blocked_reason": None if not missing else (
            "This project's ontology has no data-quality kinds (" + ", ".join(missing) + "). Assign the "
            "banking-dq pack, or a pack that includes it, to extract the rules."),
    }
