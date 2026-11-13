"""DOCUMENTS WITH REVISIONS, CONFLICT DETECTION AND ANCHORED COMMENTS (BOSSAPP-874).

WHY. A PRD, a plan, minutes and an architecture document were immutable artifact rows: each
agent rewrite was a new artifact, a person could not edit one in place, and a comment could only
point at a line of code. Reviewing a plan needs a comment on a PASSAGE that survives the agent
rewriting the document around it, and an agent and a person editing the same document need to
be told when they would overwrite each other.

THE SHAPE.

* ``documents`` — one per document, optionally on a work item (the canonical node, BOSSAPP-830),
  with ``source_ref`` naming what it was opened from (``artifact:<id>`` for a PRD or minutes,
  ``architecture_review:<id>`` for an architecture), so the same PRD opened twice is one document.
* ``document_revisions`` — APPEND-ONLY (a trigger refuses UPDATE; a revision goes only with its
  document), the skill_versions pattern: a save whose body is the current one adds nothing, a
  restore is a NEW revision copying an old body (``restored_from``).
* EVERY SAVE NAMES ITS BASE. :func:`save_revision` takes ``base_revision_id`` — the revision the
  writer started from — and refuses (:class:`DocumentConflict`, HTTP 409) when the document has
  moved on since, handing back the current revision so the writer can merge. A person may hold a
  short edit lock (:func:`lock`); anyone else's save, agent or person, is refused while it holds.
* ``doc_comment_threads`` — a thread anchored by its QUOTE plus a little text either side
  (prefix/suffix) and the offsets in the revision it was last anchored to. Every new revision
  RE-ANCHORS the open threads (:func:`locate`): the quote found where it was (``exact``),
  elsewhere (``moved``), approximately through the edit (``fuzzy``, the quote updated to what is
  there now), or gone (``orphaned`` — kept, never deleted).
* The open threads are handed to the agent that writes the next revision
  (:func:`open_threads_brief`; architecture_docs.revise_sent_back reads it).
"""
from __future__ import annotations

import difflib
import hashlib
from datetime import timedelta
from typing import Any, Mapping, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .core import emit_event, make_id, now_utc

KINDS = ("prd", "plan", "minutes", "architecture", "note")
AUTHOR_KINDS = ("person", "agent", "system")
#: How much text either side of a quote is kept to tell two occurrences apart.
CONTEXT = 32
#: How alike the text found through an edit must be to the old quote to keep the thread there.
FUZZY_MIN = 0.5
LOCK_MINUTES = 10
MAX_BODY = 400_000
#: An artifact type → the document kind it opens as.
ARTIFACT_KINDS = {
    "prd": "prd", "prd_document": "prd", "product_requirements": "prd",
    "meeting_minutes": "minutes", "minutes": "minutes", "meeting_transcript": "minutes",
    "architecture_document": "architecture", "plan": "plan", "implementation_plan": "plan",
}


class DocumentError(ValueError):
    """Something the writer can fix, phrased for them (HTTP 422)."""


class DocumentConflict(DocumentError):
    """The document moved on since the writer's base (HTTP 409). Carries the current revision."""

    def __init__(self, message: str, *, current: Optional[dict[str, Any]] = None):
        super().__init__(message)
        self.current = current or {}


class DocumentLocked(DocumentConflict):
    """Someone else holds the edit lock (HTTP 409)."""


def _hash(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _iso(value: Any) -> Any:
    return value.isoformat() if hasattr(value, "isoformat") else value


def _visible_item(connection: Connection, project_id: str, work_item_id: Optional[str]) -> None:
    """A document on a work item is read through the item: one the viewer cannot see is not
    there (BOSSAPP-876 — the same 404 the item itself answers)."""
    if work_item_id:
        from . import work_items

        work_items._row(connection, project_id, work_item_id)


# ── reading ──────────────────────────────────────────────────────────────────────────────────

def _doc(connection: Connection, project_id: str, document_id: str, *,
         for_update: bool = False) -> dict[str, Any]:
    row = connection.execute(text(
        "SELECT * FROM documents WHERE id = :id AND project_id = :p"
        + (" FOR UPDATE" if for_update else "")),
        {"id": document_id, "p": project_id}).mappings().first()
    if row is None:
        raise LookupError(f"no document {document_id} in this project")
    _visible_item(connection, project_id, row.get("work_item_id"))
    return dict(row)


def _revision(connection: Connection, document_id: str, *, revision_id: Optional[str] = None,
              revision_no: Optional[int] = None) -> dict[str, Any]:
    if revision_id:
        row = connection.execute(text(
            "SELECT * FROM document_revisions WHERE id = :r AND document_id = :d"),
            {"r": revision_id, "d": document_id}).mappings().first()
    else:
        row = connection.execute(text(
            "SELECT * FROM document_revisions WHERE document_id = :d AND revision_no = :n"),
            {"d": document_id, "n": int(revision_no or 0)}).mappings().first()
    if row is None:
        raise LookupError("no such revision of this document")
    return dict(row)


def _public_revision(row: Mapping[str, Any], *, body: bool = True) -> dict[str, Any]:
    out = {"id": row["id"], "revision_no": row["revision_no"],
           "base_revision_id": row.get("base_revision_id"), "author_kind": row["author_kind"],
           "author_id": row["author_id"], "note": row.get("note") or "",
           "restored_from": row.get("restored_from"), "created_at": _iso(row["created_at"])}
    if body:
        out["body"] = row["body"]
    return out


def _public(connection: Connection, row: Mapping[str, Any], *, detail: bool) -> dict[str, Any]:
    out = {"id": row["id"], "project_id": row["project_id"], "work_item_id": row.get("work_item_id"),
           "kind": row["kind"], "title": row["title"], "source_ref": row.get("source_ref"),
           "current_revision_id": row.get("current_revision_id"),
           "current_revision_no": row.get("current_revision_no") or 0,
           "locked_by": None, "locked_until": None,
           "created_by": row.get("created_by"), "created_at": _iso(row.get("created_at")),
           "updated_at": _iso(row.get("updated_at"))}
    until = row.get("locked_until")
    if row.get("locked_by") and until is not None and until > now_utc():
        out["locked_by"], out["locked_until"] = row["locked_by"], _iso(until)
    out["open_threads"] = int(connection.execute(text(
        "SELECT count(*) FROM doc_comment_threads WHERE document_id = :d AND status = 'open'"),
        {"d": row["id"]}).scalar() or 0)
    if detail:
        current = (_revision(connection, row["id"], revision_id=row["current_revision_id"])
                   if row.get("current_revision_id") else None)
        out["current"] = _public_revision(current) if current else None
        out["revisions"] = revisions(connection, row["id"])
        out["threads"] = threads(connection, row["id"])
    return out


def get_document(connection: Connection, project_id: str, document_id: str) -> dict[str, Any]:
    return _public(connection, _doc(connection, project_id, document_id), detail=True)


def list_documents(connection: Connection, project_id: str, *,
                   work_item_id: Optional[str] = None) -> list[dict[str, Any]]:
    _visible_item(connection, project_id, work_item_id)
    from . import work_items

    # A document on a private item is listed only to those who may read the item (BOSSAPP-876).
    rows = connection.execute(text(
        "SELECT * FROM documents d WHERE d.project_id = :p "
        "AND (CAST(:w AS TEXT) IS NULL OR d.work_item_id = :w) "
        "AND " + work_items.visible_sql(connection, "d.work_item_id") + " "
        "ORDER BY d.updated_at DESC LIMIT 200"),
        {"p": project_id, "w": work_item_id, **work_items.visible_params(connection)}).mappings()
    return [_public(connection, r, detail=False) for r in rows]


def revisions(connection: Connection, document_id: str) -> list[dict[str, Any]]:
    return [_public_revision(r, body=False) for r in connection.execute(text(
        "SELECT * FROM document_revisions WHERE document_id = :d ORDER BY revision_no DESC"),
        {"d": document_id}).mappings()]


def get_revision(connection: Connection, project_id: str, document_id: str,
                 revision_no: int) -> dict[str, Any]:
    _doc(connection, project_id, document_id)
    return _public_revision(_revision(connection, document_id, revision_no=revision_no))


def diff(connection: Connection, project_id: str, document_id: str, *, from_no: int,
         to_no: Optional[int] = None) -> dict[str, Any]:
    """What changed between two revisions, as unified-diff lines (`to` defaults to current)."""
    doc = _doc(connection, project_id, document_id)
    to_no = int(to_no or doc["current_revision_no"] or 0)
    old = _revision(connection, document_id, revision_no=from_no)
    new = _revision(connection, document_id, revision_no=to_no)
    lines = list(difflib.unified_diff(
        old["body"].splitlines(), new["body"].splitlines(),
        fromfile=f"revision {from_no}", tofile=f"revision {to_no}", lineterm="", n=2))
    added = sum(1 for ln in lines if ln.startswith("+") and not ln.startswith("+++"))
    removed = sum(1 for ln in lines if ln.startswith("-") and not ln.startswith("---"))
    return {"from": from_no, "to": to_no, "lines": lines, "added": added, "removed": removed}


# ── creating and saving ──────────────────────────────────────────────────────────────────────

def _body(value: Any) -> str:
    body = str(value if value is not None else "")
    if len(body) > MAX_BODY:
        raise DocumentError(f"a document is at most {MAX_BODY} characters")
    return body


def create_document(connection: Connection, *, project_id: str, title: str, body: str = "",
                    kind: str = "note", work_item_id: Optional[str] = None,
                    source_ref: Optional[str] = None, actor: str = "system",
                    author_kind: str = "person") -> dict[str, Any]:
    title = str(title or "").strip()[:300]
    if not title:
        raise DocumentError("a document needs a title")
    if kind not in KINDS:
        raise DocumentError(f"kind must be one of {', '.join(KINDS)}")
    _visible_item(connection, project_id, work_item_id)
    document_id = make_id("doc")
    connection.execute(text(
        "INSERT INTO documents (id, project_id, work_item_id, kind, title, source_ref, created_by) "
        "VALUES (:id, :p, :w, :k, :t, :s, :a)"),
        {"id": document_id, "p": project_id, "w": work_item_id, "k": kind, "t": title,
         "s": source_ref, "a": actor})
    _append(connection, project_id, document_id, body=_body(body), base=None, author=actor,
            author_kind=author_kind, note="created")
    emit_event(connection, project_id, "document.created", actor, "document", document_id,
               {"kind": kind, "work_item_id": work_item_id, "source_ref": source_ref})
    return get_document(connection, project_id, document_id)


def _append(connection: Connection, project_id: str, document_id: str, *, body: str,
            base: Optional[str], author: str, author_kind: str, note: str = "",
            restored_from: Optional[str] = None) -> Optional[dict[str, Any]]:
    """Write the next revision (the caller holds the document row) and re-anchor the threads.
    None when the body is the current one: an unchanged save is not a revision."""
    if author_kind not in AUTHOR_KINDS:
        raise DocumentError(f"author_kind must be one of {', '.join(AUTHOR_KINDS)}")
    doc = connection.execute(text(
        "SELECT current_revision_id, current_revision_no FROM documents WHERE id = :d"),
        {"d": document_id}).mappings().one()
    digest = _hash(body)
    if doc["current_revision_id"]:
        current = _revision(connection, document_id, revision_id=doc["current_revision_id"])
        if current["body_hash"] == digest:
            return None
    revision_no = int(doc["current_revision_no"] or 0) + 1
    revision_id = make_id("drev")
    connection.execute(text(
        "INSERT INTO document_revisions (id, document_id, project_id, revision_no, "
        "base_revision_id, body, body_hash, author_kind, author_id, note, restored_from) "
        "VALUES (:id, :d, :p, :n, :b, :body, :h, :ak, :a, :note, :rf)"),
        {"id": revision_id, "d": document_id, "p": project_id, "n": revision_no, "b": base,
         "body": body, "h": digest, "ak": author_kind, "a": author, "note": note[:500],
         "rf": restored_from})
    connection.execute(text(
        "UPDATE documents SET current_revision_id = :r, current_revision_no = :n, "
        "updated_at = now() WHERE id = :d"), {"r": revision_id, "n": revision_no, "d": document_id})
    moved = reanchor(connection, document_id, revision_id)
    return {"id": revision_id, "revision_no": revision_no, "reanchored": moved}


def _lock_holder(doc: Mapping[str, Any]) -> Optional[str]:
    until = doc.get("locked_until")
    if doc.get("locked_by") and until is not None and until > now_utc():
        return str(doc["locked_by"])
    return None


def save_revision(connection: Connection, *, project_id: str, document_id: str, body: str,
                  base_revision_id: Optional[str], actor: str, author_kind: str = "person",
                  note: str = "") -> dict[str, Any]:
    """THE ONE SAVE. Refused when the document moved past `base_revision_id` (a conflict: the
    other writer's revision is handed back) or someone else holds the lock."""
    doc = _doc(connection, project_id, document_id, for_update=True)
    holder = _lock_holder(doc)
    if holder and holder != actor:
        raise DocumentLocked(f"{holder} is editing this document; try again when they finish",
                             current=_current(connection, doc))
    if (base_revision_id or None) != (doc.get("current_revision_id") or None):
        # Nothing is written for a refused save (its transaction is rolled back by the caller).
        raise DocumentConflict(
            f"this document changed since you opened it (now revision "
            f"{doc['current_revision_no']}); merge with the current text and save again",
            current=_current(connection, doc))
    written = _append(connection, project_id, document_id, body=_body(body),
                      base=base_revision_id, author=actor, author_kind=author_kind, note=note)
    if written:
        emit_event(connection, project_id, "document.revised", actor, "document", document_id,
                   {"revision_no": written["revision_no"], "author_kind": author_kind,
                    "reanchored": written["reanchored"]})
    return {**get_document(connection, project_id, document_id), "saved": bool(written)}


def _current(connection: Connection, doc: Mapping[str, Any]) -> dict[str, Any]:
    if not doc.get("current_revision_id"):
        return {}
    return _public_revision(_revision(connection, doc["id"], revision_id=doc["current_revision_id"]))


def restore(connection: Connection, *, project_id: str, document_id: str, revision_no: int,
            base_revision_id: Optional[str], actor: str) -> dict[str, Any]:
    """Put an old revision back: a NEW revision with that body (history is never rewritten)."""
    old = _revision(connection, document_id, revision_no=revision_no)
    doc = _doc(connection, project_id, document_id, for_update=True)
    if (base_revision_id or None) != (doc.get("current_revision_id") or None):
        raise DocumentConflict("this document changed since you opened it; reload and restore again",
                               current=_current(connection, doc))
    holder = _lock_holder(doc)
    if holder and holder != actor:
        raise DocumentLocked(f"{holder} is editing this document", current=_current(connection, doc))
    _append(connection, project_id, document_id, body=old["body"], base=base_revision_id,
            author=actor, author_kind="person", note=f"restored revision {revision_no}",
            restored_from=old["id"])
    emit_event(connection, project_id, "document.restored", actor, "document", document_id,
               {"revision_no": revision_no})
    return get_document(connection, project_id, document_id)


def lock(connection: Connection, *, project_id: str, document_id: str, actor: str,
         release: bool = False) -> dict[str, Any]:
    """Hold (or let go of) the edit lock for LOCK_MINUTES. Taking a lock someone else holds is
    refused; an expired lock is anyone's."""
    doc = _doc(connection, project_id, document_id, for_update=True)
    holder = _lock_holder(doc)
    if holder and holder != actor:
        raise DocumentLocked(f"{holder} is editing this document", current=_current(connection, doc))
    if release:
        connection.execute(text(
            "UPDATE documents SET locked_by = NULL, locked_until = NULL WHERE id = :d"),
            {"d": document_id})
    else:
        connection.execute(text(
            "UPDATE documents SET locked_by = :a, locked_until = :u WHERE id = :d"),
            {"a": actor, "u": now_utc() + timedelta(minutes=LOCK_MINUTES), "d": document_id})
    return get_document(connection, project_id, document_id)


# ── opening the documents BOSS already writes ────────────────────────────────────────────────

def _markdown_of(content: Any) -> str:
    if isinstance(content, str):
        import json

        try:
            content = json.loads(content)
        except ValueError:
            return content
    if isinstance(content, dict):
        for key in ("markdown", "prd", "body", "minutes", "text", "content"):
            value = content.get(key)
            if isinstance(value, str) and value.strip():
                return value
        import json

        return json.dumps(content, indent=2, default=str)
    return str(content or "")


def ensure_for_artifact(connection: Connection, *, project_id: str, artifact_id: str,
                        actor: str, work_item_id: Optional[str] = None) -> dict[str, Any]:
    """The document behind a PRD, minutes, plan or architecture artifact, made on first open
    (revision 1 = the artifact's text, written by the system). The same artifact is one document."""
    source = f"artifact:{artifact_id}"
    existing = connection.execute(text(
        "SELECT id FROM documents WHERE project_id = :p AND source_ref = :s"),
        {"p": project_id, "s": source}).scalar()
    if existing:
        return get_document(connection, project_id, existing)
    row = connection.execute(text(
        "SELECT id, artifact_type, title, content FROM artifacts WHERE id = :a AND project_id = :p"),
        {"a": artifact_id, "p": project_id}).mappings().first()
    if row is None:
        raise LookupError(f"no artifact {artifact_id} in this project")
    kind = ARTIFACT_KINDS.get(str(row["artifact_type"] or "").lower(), "note")
    return create_document(connection, project_id=project_id, title=row["title"] or kind,
                           body=_markdown_of(row["content"]), kind=kind,
                           work_item_id=work_item_id, source_ref=source, actor=actor,
                           author_kind="system")


def for_source(connection: Connection, project_id: str, source_ref: str) -> Optional[dict[str, Any]]:
    row = connection.execute(text(
        "SELECT * FROM documents WHERE project_id = :p AND source_ref = :s"),
        {"p": project_id, "s": source_ref}).mappings().first()
    if row is None:
        return None
    out = dict(row)
    out["body"] = (_revision(connection, row["id"], revision_id=row["current_revision_id"])["body"]
                   if row["current_revision_id"] else "")
    return out


def record_agent_revision(connection: Connection, *, project_id: str, source_ref: str,
                          kind: str, title: str, body: str, author: str,
                          base_revision_id: Optional[str],
                          work_item_id: Optional[str] = None) -> dict[str, Any]:
    """An agent wrote the next version of a document BOSS keeps (an architecture revision). The
    first write makes the document; later writes are saves from the base the agent READ, so a
    person's edit made while the agent was writing is a conflict, not a silent overwrite."""
    existing = connection.execute(text(
        "SELECT id FROM documents WHERE project_id = :p AND source_ref = :s"),
        {"p": project_id, "s": source_ref}).scalar()
    if not existing:
        return create_document(connection, project_id=project_id, title=title, body=body,
                               kind=kind, work_item_id=work_item_id, source_ref=source_ref,
                               actor=author, author_kind="agent")
    return save_revision(connection, project_id=project_id, document_id=existing, body=body,
                         base_revision_id=base_revision_id, actor=author, author_kind="agent",
                         note="agent revision")


# ── anchored comment threads ─────────────────────────────────────────────────────────────────

def threads(connection: Connection, document_id: str) -> list[dict[str, Any]]:
    rows = connection.execute(text(
        "SELECT * FROM doc_comment_threads WHERE document_id = :d ORDER BY status, created_at"),
        {"d": document_id}).mappings().all()
    comments: dict[str, list[dict[str, Any]]] = {}
    for c in connection.execute(text(
            "SELECT c.* FROM doc_comments c JOIN doc_comment_threads t ON t.id = c.thread_id "
            "WHERE t.document_id = :d ORDER BY c.created_at"), {"d": document_id}).mappings():
        comments.setdefault(c["thread_id"], []).append(
            {"id": c["id"], "author": c["author"], "body": c["body"],
             "created_at": _iso(c["created_at"])})
    return [{"id": r["id"], "revision_id": r["revision_id"], "quote": r["quote"],
             "prefix": r["prefix"], "suffix": r["suffix"], "start": r["start_offset"],
             "end": r["end_offset"], "anchor_state": r["anchor_state"], "status": r["status"],
             "created_by": r["created_by"], "resolved_by": r.get("resolved_by"),
             "created_at": _iso(r["created_at"]), "comments": comments.get(r["id"], [])}
            for r in rows]


def add_thread(connection: Connection, *, project_id: str, document_id: str,
               revision_id: str, start: int, end: int, body: str, actor: str) -> dict[str, Any]:
    """Comment on a passage of a revision: [start, end) of its body. The quote and its context
    are taken from the body here — a client cannot anchor to text that is not there."""
    doc = _doc(connection, project_id, document_id)
    rev = _revision(connection, document_id, revision_id=revision_id)
    text_ = rev["body"]
    start, end = int(start), int(end)
    if not 0 <= start < end <= len(text_):
        raise DocumentError("select some text of the document to comment on")
    body = str(body or "").strip()
    if not body:
        raise DocumentError("a comment needs some text")
    quote = text_[start:end]
    thread_id = make_id("dct")
    connection.execute(text(
        "INSERT INTO doc_comment_threads (id, document_id, project_id, revision_id, quote, "
        "prefix, suffix, start_offset, end_offset, created_by) "
        "VALUES (:id, :d, :p, :r, :q, :pre, :suf, :s, :e, :a)"),
        {"id": thread_id, "d": document_id, "p": project_id, "r": revision_id, "q": quote,
         "pre": text_[max(0, start - CONTEXT):start], "suf": text_[end:end + CONTEXT],
         "s": start, "e": end, "a": actor})
    _comment(connection, project_id, thread_id, body, actor)
    # A thread made on an older revision is carried to the current one straight away.
    if doc.get("current_revision_id") and doc["current_revision_id"] != revision_id:
        reanchor(connection, document_id, doc["current_revision_id"])
    emit_event(connection, project_id, "document.comment_thread_opened", actor, "document",
               document_id, {"thread_id": thread_id})
    return next(t for t in threads(connection, document_id) if t["id"] == thread_id)


def _comment(connection: Connection, project_id: str, thread_id: str, body: str, actor: str) -> None:
    connection.execute(text(
        "INSERT INTO doc_comments (id, thread_id, project_id, author, body) "
        "VALUES (:id, :t, :p, :a, :b)"),
        {"id": make_id("dcm"), "t": thread_id, "p": project_id, "a": actor, "b": body[:8000]})


def _thread(connection: Connection, project_id: str, document_id: str, thread_id: str) -> dict:
    _doc(connection, project_id, document_id)
    row = connection.execute(text(
        "SELECT * FROM doc_comment_threads WHERE id = :t AND document_id = :d"),
        {"t": thread_id, "d": document_id}).mappings().first()
    if row is None:
        raise LookupError("no such comment thread on this document")
    return dict(row)


def reply(connection: Connection, *, project_id: str, document_id: str, thread_id: str,
          body: str, actor: str) -> dict[str, Any]:
    _thread(connection, project_id, document_id, thread_id)
    body = str(body or "").strip()
    if not body:
        raise DocumentError("a reply needs some text")
    _comment(connection, project_id, thread_id, body, actor)
    return next(t for t in threads(connection, document_id) if t["id"] == thread_id)


def set_thread_status(connection: Connection, *, project_id: str, document_id: str,
                      thread_id: str, resolved: bool, actor: str) -> dict[str, Any]:
    _thread(connection, project_id, document_id, thread_id)
    connection.execute(text(
        "UPDATE doc_comment_threads SET status = :s, resolved_by = :rb, "
        "resolved_at = CASE WHEN :res THEN now() ELSE NULL END, updated_at = now() WHERE id = :t"),
        {"s": "resolved" if resolved else "open", "rb": actor if resolved else None,
         "res": resolved, "t": thread_id})
    emit_event(connection, project_id,
               "document.comment_thread_resolved" if resolved else "document.comment_thread_reopened",
               actor, "document", document_id, {"thread_id": thread_id})
    return next(t for t in threads(connection, document_id) if t["id"] == thread_id)


def _common_suffix(a: str, b: str) -> int:
    n = 0
    while n < len(a) and n < len(b) and a[-1 - n] == b[-1 - n]:
        n += 1
    return n


def _common_prefix(a: str, b: str) -> int:
    n = 0
    while n < len(a) and n < len(b) and a[n] == b[n]:
        n += 1
    return n


def _map_offset(opcodes: list[tuple[str, int, int, int, int]], pos: int, *, end: bool) -> int:
    """Where an offset of the old body lands in the new one (through difflib's opcodes)."""
    for tag, i1, i2, j1, j2 in opcodes:
        if i1 <= pos < i2 or (end and pos == i2 and i1 < i2):
            if tag == "equal":
                return j1 + (pos - i1)
            return j2 if end else j1
    return opcodes[-1][4] if opcodes else 0


def locate(old_body: str, new_body: str, *, quote: str, prefix: str, suffix: str,
           start: Optional[int]) -> dict[str, Any]:
    """Re-anchor one quote in a new body. Pure (tested on its own).

    1. The quote appears verbatim: among its occurrences the one whose surrounding text best
       matches the stored prefix/suffix wins (closest to the old position breaks a tie):
       `exact` when it is where it was, `moved` otherwise.
    2. Not verbatim: the old span is carried through the edit (difflib) and kept as `fuzzy` when
       what is there now is still like the quote (the quote becomes the new text).
    3. Otherwise `orphaned` — the thread stays, pointing at nothing."""
    hits = []
    at = new_body.find(quote) if quote else -1
    while at >= 0:
        hits.append(at)
        at = new_body.find(quote, at + 1)
    if hits:
        def score(i: int) -> tuple[int, int]:
            ctx = (_common_suffix(prefix, new_body[max(0, i - len(prefix)):i])
                   + _common_prefix(suffix, new_body[i + len(quote):i + len(quote) + len(suffix)]))
            return (ctx, -abs(i - (start or 0)))

        best = max(hits, key=score)
        return {"state": "exact" if best == start else "moved", "start": best,
                "end": best + len(quote), "quote": quote}
    if start is not None and old_body and quote:
        ops = difflib.SequenceMatcher(None, old_body, new_body, autojunk=False).get_opcodes()
        ns = _map_offset(ops, start, end=False)
        ne = _map_offset(ops, start + len(quote), end=True)
        found = new_body[ns:ne]
        if found.strip() and difflib.SequenceMatcher(None, quote, found).ratio() >= FUZZY_MIN:
            return {"state": "fuzzy", "start": ns, "end": ne, "quote": found}
    return {"state": "orphaned", "start": None, "end": None, "quote": quote}


def reanchor(connection: Connection, document_id: str, revision_id: str) -> dict[str, int]:
    """Carry every open thread to `revision_id`. Returns how many landed in each state."""
    new_body = _revision(connection, document_id, revision_id=revision_id)["body"]
    counts: dict[str, int] = {}
    bodies: dict[str, str] = {}
    for t in connection.execute(text(
            "SELECT * FROM doc_comment_threads WHERE document_id = :d AND status = 'open' "
            "AND revision_id <> :r"), {"d": document_id, "r": revision_id}).mappings().all():
        if t["revision_id"] not in bodies:
            try:
                bodies[t["revision_id"]] = _revision(connection, document_id,
                                                     revision_id=t["revision_id"])["body"]
            except LookupError:
                bodies[t["revision_id"]] = ""
        if t["anchor_state"] == "orphaned":
            spot = locate("", new_body, quote=t["quote"], prefix=t["prefix"], suffix=t["suffix"],
                          start=None)
        else:
            spot = locate(bodies[t["revision_id"]], new_body, quote=t["quote"],
                          prefix=t["prefix"], suffix=t["suffix"], start=t["start_offset"])
        s, e = spot["start"], spot["end"]
        connection.execute(text(
            "UPDATE doc_comment_threads SET revision_id = :r, anchor_state = :st, quote = :q, "
            "start_offset = :s, end_offset = :e, prefix = :pre, suffix = :suf, updated_at = now() "
            "WHERE id = :id"),
            {"r": revision_id if s is not None else t["revision_id"], "st": spot["state"],
             "q": spot["quote"], "s": s if s is not None else t["start_offset"],
             "e": e if e is not None else t["end_offset"],
             "pre": new_body[max(0, s - CONTEXT):s] if s is not None else t["prefix"],
             "suf": new_body[e:e + CONTEXT] if e is not None else t["suffix"], "id": t["id"]})
        counts[spot["state"]] = counts.get(spot["state"], 0) + 1
    return counts


def open_threads_brief(connection: Connection, document_id: str) -> str:
    """The open comment threads, as the agent revising the document reads them."""
    lines = []
    for t in threads(connection, document_id):
        if t["status"] != "open":
            continue
        where = ("(the passage it was on is gone)" if t["anchor_state"] == "orphaned"
                 else f'on "{t["quote"][:200]}"')
        said = " / ".join(f'{c["author"]}: {c["body"][:400]}' for c in t["comments"])
        lines.append(f"- {where} — {said}")
    if not lines:
        return ""
    return ("OPEN REVIEW COMMENTS ON THIS DOCUMENT (answer each in the new version; the passage "
            "each is anchored to is quoted):\n" + "\n".join(lines))


__all__ = [
    "KINDS", "DocumentError", "DocumentConflict", "DocumentLocked", "create_document",
    "get_document", "list_documents", "revisions", "get_revision", "diff", "save_revision",
    "restore", "lock", "ensure_for_artifact", "for_source", "record_agent_revision", "threads",
    "add_thread", "reply", "set_thread_status", "locate", "reanchor", "open_threads_brief",
]
