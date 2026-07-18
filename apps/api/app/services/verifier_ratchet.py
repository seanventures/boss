"""VAG #107 — cross-run eval ratchet.

Every time a human OVERRULES the gate, that disagreement is ground truth the verifier should be
measured against forever after (#105 meta-eval). Two directions:

  - gate FAILED but a human OVERRODE it  → the gate was wrong to block → a **clean** item
    (a false-fail the verifier produced). The judged files are right there in
    ``finalize_ctx.pending_diff``.
  - gate PASSED but a human REJECTED the PR → the verifier was fooled → a **bugged** item
    (a false-pass — the highest-value signal there is).

Captured items are tagged onto the run (``finalize_ctx.ratchet`` — the bake-off "tag the run"
idiom, no migration) with a self-contained copy of the files, and merged into the meta-eval
corpus. The corpus therefore **ratchets**: it only grows, and it grows from the verifier's own
mistakes, so a regression the verifier let through once is scored against it from then on.

The stored item carries a ``stub_verdict`` of the ground-truth label (bugged→refuted,
clean→survived) so an offline dry-run stays deterministic; a ``--real`` run replays the item
through the actual reviewer to see whether it has since learned to get it right.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

logger = logging.getLogger(__name__)

RATCHET_KEY = "ratchet"           # finalize_ctx sub-key
_CLEAN, _BUGGED = "clean", "bugged"
_DEFAULT_BUG_CLASS = {"gate_override": "human-overrode-false-fail",
                      "decision_reject": "human-rejected-false-pass"}


def _files_from(run: dict[str, Any], explicit: Optional[list[dict]]) -> Optional[list[dict]]:
    """Resolve the implementation files: an explicit list wins; otherwise the diff the pipeline
    preserved at gate-failure time (``finalize_ctx.pending_diff``, a list of {path, content}).
    Returns None when the code can't be recovered — a corpus item without the code is useless."""
    if explicit:
        cand = explicit
    else:
        cand = (run.get("finalize_ctx") or {}).get("pending_diff")
    if not isinstance(cand, list) or not cand:
        return None
    out = [{"path": str(f["path"]), "content": str(f["content"])}
           for f in cand if isinstance(f, dict) and "path" in f and "content" in f]
    return out or None


def tag_run(
    connection: Connection, run_id: str, *, label: str, source: str,
    bug_class: Optional[str] = None, files: Optional[list[dict]] = None,
    reason: Optional[str] = None,
) -> bool:
    """Tag a run as a ratchet corpus item. Idempotent on ``source`` (a re-fire is a no-op).
    Returns True if a new tag was written, False if skipped (already tagged / no files / no run)."""
    if label not in (_CLEAN, _BUGGED):
        raise ValueError(f"label must be clean|bugged, got {label!r}")
    row = connection.execute(
        text("SELECT finalize_ctx FROM code_task_runs WHERE id = :id"), {"id": run_id}
    ).mappings().first()
    if row is None:
        logger.warning("ratchet: run %s not found", run_id)
        return False
    fctx = row["finalize_ctx"] or {}
    if isinstance(fctx, str):
        fctx = json.loads(fctx or "{}")
    existing = fctx.get(RATCHET_KEY)
    if isinstance(existing, dict) and existing.get("source") == source:
        return False  # already captured from this signal
    resolved = _files_from({"finalize_ctx": fctx}, files)
    if not resolved:
        logger.info("ratchet: no recoverable files for run %s (source=%s) — not captured", run_id, source)
        return False
    tag = {"label": label, "source": source,
           "bug_class": bug_class or _DEFAULT_BUG_CLASS.get(source),
           "reason": reason, "files": resolved}
    connection.execute(
        text("UPDATE code_task_runs SET finalize_ctx = jsonb_set("
             "COALESCE(finalize_ctx, '{}'::jsonb), :path, CAST(:val AS jsonb), true), "
             "updated_at = now() WHERE id = :id"),
        {"path": "{" + RATCHET_KEY + "}", "val": json.dumps(tag), "id": run_id},
    )
    logger.info("ratchet: captured run %s as %s (%s, %d files)", run_id, label, source, len(resolved))
    return True


def item_from_run(run: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Turn a ratchet-tagged run row into a #105 corpus item, or None if untagged/fileless."""
    fctx = run.get("finalize_ctx") or {}
    tag = fctx.get(RATCHET_KEY)
    if not isinstance(tag, dict) or not tag.get("files"):
        return None
    spec = run.get("spec") or {}
    label = tag.get("label")
    return {
        "key": f"ratchet_{str(run.get('id', ''))[:16]}",
        "title": run.get("title") or (spec.get("instructions", "")[:80]),
        "acceptance_criteria": spec.get("acceptance_criteria", []),
        "implementation": {f["path"]: f["content"] for f in tag["files"]},
        "label": label,
        "bug_class": tag.get("bug_class"),
        # ground-truth stub so an offline dry-run stays deterministic (perfect reviewer).
        "stub_verdict": "refuted" if label == _BUGGED else "survived",
        "source": tag.get("source"),
        "provenance": {"run_id": run.get("id"), "reason": tag.get("reason")},
    }


def load_ratchet_items(connection: Connection, project_id: Optional[str] = None) -> list[dict[str, Any]]:
    """Load every ratchet-tagged run as a corpus item (optionally scoped to one project)."""
    sql = ("SELECT id, project_id, title, spec, finalize_ctx FROM code_task_runs "
           "WHERE finalize_ctx -> 'ratchet' IS NOT NULL")
    params: dict[str, Any] = {}
    if project_id:
        sql += " AND project_id = :p"
        params["p"] = project_id
    sql += " ORDER BY created_at"
    items = []
    for row in connection.execute(text(sql), params).mappings():
        run = dict(row)
        for k in ("spec", "finalize_ctx"):
            if isinstance(run.get(k), str):
                run[k] = json.loads(run[k] or "{}")
        it = item_from_run(run)
        if it:
            items.append(it)
    return items


def merged_corpus(
    connection: Connection, base_corpus: dict[str, Any], project_id: Optional[str] = None
) -> dict[str, Any]:
    """Base (file) corpus + ratchet items (DB), de-duplicated by key — the growing corpus the
    meta-eval scores the verifier against."""
    seen = {i.get("key") for i in base_corpus.get("items", [])}
    ratchet = [i for i in load_ratchet_items(connection, project_id) if i["key"] not in seen]
    return {"_readme": base_corpus.get("_readme", ""),
            "items": list(base_corpus.get("items", [])) + ratchet,
            "ratchet_count": len(ratchet)}


def capture_run(
    connection: Connection, run_id: str, *, label: str, source: str = "manual",
    bug_class: Optional[str] = None, files: Optional[list[dict]] = None, reason: Optional[str] = None,
) -> dict[str, Any]:
    """High-level capture used by the endpoint / hooks: tag the run and report the outcome."""
    tagged = tag_run(connection, run_id, label=label, source=source,
                     bug_class=bug_class, files=files, reason=reason)
    return {"captured": tagged, "run_id": run_id, "label": label, "source": source}
