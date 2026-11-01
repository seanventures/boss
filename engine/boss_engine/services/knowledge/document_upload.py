"""Upload a document (a PDF, or plain text) into Knowledge Graph › Documents (rulebook source G3).

OPERATOR DECISION 2026-11-01, amending SPEC resolution #1 ("BOSS does not run text-segmentation
over a pasted blob"): an UPLOADED file may be split into sections automatically, but only after a
person confirms the split in a preview. Nothing is written to the graph before that confirmation.

The life of an upload:

1. ``stage_upload`` reads the file's text with the one PDF reader (``document_text.pdf_pages``),
   removes the running header/footer lines every page repeats, and proposes sections
   deterministically (``propose_sections``): one per numbered heading ("0.1 Purpose",
   "Appendix C. Change log"), one per rule card (a line that opens with an identifier such as
   ``NPB-DQ-LN-003 v2 — Principal outstanding …``), a title-page section for what comes before
   the first heading, and a contents page kept whole. The proposal is stored in
   ``kg_document_uploads`` as ``pending`` — nothing in the graph yet.
2. ``edit_upload``: the person may drop, merge (with the next) or split (at a line) sections; the
   stored proposal changes, still pending. Section text is never retyped: a section is always a
   run of the extracted lines.
3. ``confirm_upload`` (a PERSON only) writes one ``document`` node and one ``clause`` node per
   section through ``documents.add_document``: the exact extracted text, and a citation of the
   document title, the section and its page(s). The same file uploaded again lands on the same
   keys (idempotent).

An agent can stage an upload (``add_document_from_file``) — a proposal the person then confirms
in Documents; it can never confirm one.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Sequence

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..core import make_id, now_utc

#: Largest file accepted (bytes).
MAX_UPLOAD_BYTES = 20 * 1024 * 1024
#: How many sections one document may hold.
MAX_SECTIONS = 2000
#: Lines shown per section in the preview.
PREVIEW_LINES = 2

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS kg_document_uploads (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  title TEXT NOT NULL,
  filename TEXT NOT NULL,
  sha256 TEXT NOT NULL,
  size_bytes BIGINT NOT NULL,
  page_count INTEGER NOT NULL,
  pattern TEXT,
  sections JSONB NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  proposed_by_kind TEXT NOT NULL,
  proposed_by TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  confirmed_by TEXT,
  confirmed_at TIMESTAMPTZ,
  document_id TEXT
);
CREATE INDEX IF NOT EXISTS kg_document_uploads_project ON kg_document_uploads(project_id, created_at DESC)
"""


class UploadRefused(ValueError):
    """An upload, an edit or a confirmation refused in words a person can act on."""


def ensure_schema(connection: Connection) -> None:
    for stmt in _SCHEMA_SQL.split(";"):
        sql = stmt.strip()
        if sql:
            connection.execute(text(sql))


# ── reading ────────────────────────────────────────────────────────────────────────────────────

def read_pages(payload: bytes, filename: str) -> list[str]:
    """The file's pages of text: a PDF through the shared reader, a text file as one page (a
    form feed starts a new one). Anything else is refused in words."""
    from ..document_text import DocumentTextError, pdf_pages

    if not payload:
        raise UploadRefused(f"'{filename}' is empty.")
    if len(payload) > MAX_UPLOAD_BYTES:
        raise UploadRefused(f"'{filename}' is {len(payload) // (1024 * 1024)} MB; the most an upload "
                            f"may be is {MAX_UPLOAD_BYTES // (1024 * 1024)} MB.")
    if payload[:5] == b"%PDF-":
        try:
            return pdf_pages(payload, filename, advice=(
                "Upload a PDF exported from the original document (not a scan), or a .txt file."))
        except DocumentTextError as error:
            raise UploadRefused(str(error)) from error
    if b"\x00" in payload:
        raise UploadRefused(f"'{filename}' is neither a PDF nor a text file. Upload a PDF or a .txt file.")
    try:
        body = payload.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise UploadRefused(f"'{filename}' is not UTF-8 text or a PDF. Upload a PDF or a UTF-8 .txt file.") from error
    pages = [p.replace("\r\n", "\n").strip("\n") for p in body.split("\f")]
    if not any(p.strip() for p in pages):
        raise UploadRefused(f"'{filename}' has no text in it.")
    return pages


# ── sections ───────────────────────────────────────────────────────────────────────────────────

#: A rule card's first line: an identifier with at least two hyphenated parts, an optional
#: version, a dash and the title ("NPB-DQ-LN-003 v2 — Principal outstanding reconciles …").
RULE_CARD = re.compile(
    r"^(?P<id>[A-Z][A-Z0-9]*(?:-[A-Z0-9]+){2,})(?:\s+v(?P<version>\d+))?\s+[—–]\s+(?P<title>\S.{0,200})$")
#: A numbered heading: "1. Severity and grading", "0.4 Version history", "Appendix C. Change log",
#: "C.2 Retired rules". A heading has no full stop at its end and is short.
HEADING = re.compile(
    r"^(?P<num>(?:\d{1,2}\.(?:\d{1,2}(?:\.\d{1,2})*)?|Appendix\s+[A-Z](?:\.\d{1,2})?\.?|[A-Z]\.\d{1,2}))"
    r"\s+(?P<title>[A-Z][^\n]{0,100})$")
_DIGITS = re.compile(r"\d+")


@dataclass
class Section:
    title: str
    kind: str                      # title_page | contents | heading | rule_card | custom
    page: int                      # 1-based page the section starts on
    page_end: int
    lines: list[str] = field(default_factory=list)
    rule_id: Optional[str] = None
    #: The page each line is on (a section may cross a page break).
    line_pages: list[int] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "\n".join(self.lines).strip()


def _heading(line: str, pattern: Optional[re.Pattern[str]]) -> Optional[tuple[str, str, Optional[str]]]:
    """(kind, title, rule id) when ``line`` opens a section."""
    stripped = line.strip()
    if not stripped:
        return None
    card = RULE_CARD.match(stripped)
    if card:
        return "rule_card", stripped, card.group("id")
    if pattern is not None:
        return ("custom", stripped, None) if pattern.search(stripped) else None
    head = HEADING.match(stripped)
    if head and not stripped.endswith(".") and len(stripped) <= 110:
        return "heading", stripped, None
    return None


def running_lines(pages: Sequence[str]) -> set[str]:
    """Lines (digits ignored) that half or more of the pages repeat: a running header or footer
    ("NorthPeak Bank · … Page 12 of 45"), never a section's text."""
    if len(pages) < 3:
        return set()
    counts: dict[str, int] = {}
    for page in pages:
        body = [ln.strip() for ln in page.split("\n") if ln.strip()]
        # Only a page's first and last two lines can be its running header or footer: a line
        # many rule cards share ("Regulatory reach: none.") is text, wherever it repeats.
        for line in {_DIGITS.sub("#", ln) for ln in body[:2] + body[-2:]}:
            counts[line] = counts.get(line, 0) + 1
    floor = max(3, (len(pages) + 1) // 2)
    return {line for line, n in counts.items() if n >= floor}


def _contents_page(lines: Sequence[str], pattern: Optional[re.Pattern[str]]) -> bool:
    body = [ln for ln in lines if ln.strip()]
    if len(body) < 5:
        return False
    heads = sum(1 for ln in body if _heading(ln, pattern) is not None)
    first = body[0].strip().lower()
    return heads / len(body) >= 0.6 and (first in ("contents", "table of contents") or heads == len(body) - 1)


def propose_sections(pages: Sequence[str], *, pattern: Optional[str] = None,
                     title: str = "") -> list[Section]:
    """The document's sections, deterministically (same pages, same sections).

    ``pattern``: a regular expression a person gave instead of the numbered-heading rule; a rule
    card still opens its own section."""
    compiled = None
    if pattern:
        try:
            compiled = re.compile(pattern)
        except re.error as error:
            raise UploadRefused(f"The split pattern is not a valid regular expression: {error}.") from error
    repeated = running_lines(pages)
    sections: list[Section] = []
    current: Optional[Section] = None
    for number, page in enumerate(pages, start=1):
        lines = [ln.rstrip() for ln in page.split("\n")]
        lines = [ln for ln in lines if ln.strip() and _DIGITS.sub("#", ln.strip()) not in repeated]
        if _contents_page(lines, compiled):
            current = Section(title=lines[0].strip(), kind="contents", page=number, page_end=number,
                              lines=list(lines), line_pages=[number] * len(lines))
            sections.append(current)
            current = None
            continue
        for line in lines:
            opened = _heading(line, compiled)
            if opened is not None:
                kind, heading, rule_id = opened
                current = Section(title=heading, kind=kind, page=number, page_end=number,
                                  lines=[line], rule_id=rule_id, line_pages=[number])
                sections.append(current)
                continue
            if current is None:
                current = Section(title=title or line.strip(), kind="title_page", page=number,
                                  page_end=number, lines=[])
                sections.append(current)
            current.lines.append(line)
            current.line_pages.append(number)
            current.page_end = number
    sections = [s for s in sections if s.text]
    if len(sections) > MAX_SECTIONS:
        raise UploadRefused(f"The document splits into {len(sections)} sections; at most {MAX_SECTIONS}. "
                            "Give a split pattern that matches fewer headings.")
    return sections


def _label(section: Mapping[str, Any]) -> str:
    """The clause's section name: a rule card by its id, a heading by itself."""
    if section.get("rule_id"):
        return str(section["rule_id"])
    return str(section.get("title") or "Section")


def _as_dicts(sections: Sequence[Section]) -> list[dict[str, Any]]:
    out = []
    for i, s in enumerate(sections, start=1):
        out.append({"key": f"s{i}", "title": s.title, "kind": s.kind, "page": s.page,
                    "page_end": s.page_end, "rule_id": s.rule_id, "lines": list(s.lines),
                    "line_pages": list(s.line_pages)})
    return out


def _renumber(sections: list[dict[str, Any]]) -> list[dict[str, Any]]:
    for i, s in enumerate(sections, start=1):
        s["key"] = f"s{i}"
    return sections


def _text_of(section: Mapping[str, Any]) -> str:
    return "\n".join(section.get("lines") or []).strip()


def _first_lines(section: Mapping[str, Any]) -> list[str]:
    lines = [ln for ln in (section.get("lines") or [])[1:] if ln.strip()] if section.get("kind") != "title_page" \
        else [ln for ln in (section.get("lines") or []) if ln.strip()]
    return [ln[:200] for ln in lines[:PREVIEW_LINES]]


def preview_payload(row: Mapping[str, Any]) -> dict[str, Any]:
    """The preview a person confirms: section titles, first lines and counts — never the ids of
    graph nodes (there are none yet)."""
    sections = list(row["sections"] or [])
    counts: dict[str, int] = {}
    for s in sections:
        counts[s["kind"]] = counts.get(s["kind"], 0) + 1
    return {
        "upload_id": row["id"], "title": row["title"], "filename": row["filename"],
        "pages": int(row["page_count"]), "status": row["status"], "pattern": row.get("pattern"),
        "proposed_by_kind": row["proposed_by_kind"],
        "document_id": row.get("document_id"),
        "counts": {"sections": len(sections), "rule_cards": counts.get("rule_card", 0),
                   "headings": counts.get("heading", 0) + counts.get("custom", 0),
                   "other": counts.get("title_page", 0) + counts.get("contents", 0)},
        "sections": [{
            "key": s["key"], "title": s["title"], "label": _label(s), "kind": s["kind"],
            "page": s["page"], "page_end": s["page_end"], "first_lines": _first_lines(s),
            "line_count": len(s.get("lines") or []), "chars": len(_text_of(s)),
        } for s in sections],
    }


def _row(connection: Connection, project_id: str, upload_id: str) -> dict[str, Any]:
    ensure_schema(connection)
    row = connection.execute(text(
        "SELECT * FROM kg_document_uploads WHERE id = :id"), {"id": upload_id}).mappings().first()
    if row is None:
        raise LookupError(f"no upload {upload_id!r}")
    if row["project_id"] != project_id:
        raise PermissionError(f"upload {upload_id} is not in project {project_id}")
    return dict(row)


def _require_pack(connection: Connection, project_id: str) -> None:
    from .ontology_packs import effective_node_kinds

    kinds = effective_node_kinds(connection, project_id=project_id)
    if "document" not in kinds or "clause" not in kinds:
        raise UploadRefused(
            "This project's ontology has no 'document' and 'clause' kinds yet. Assign a pack that "
            "includes them (banking-regulatory, or a pack that includes it) in Documents first.")


def _title_from(pages: Sequence[str], filename: str) -> str:
    for page in pages:
        for line in page.split("\n"):
            if line.strip():
                return line.strip()[:200]
    return re.sub(r"\.[A-Za-z0-9]+$", "", filename) or "Document"


def stage_upload(
    connection: Connection, *, project_id: str, payload: bytes, filename: str,
    title: Optional[str] = None, pattern: Optional[str] = None, actor_kind: str = "person",
    actor_ref: Optional[str] = None,
) -> dict[str, Any]:
    """Read the file and store its proposed sections as a pending upload; return the preview.
    Writes nothing to the graph."""
    if actor_kind not in ("person", "agent"):
        raise UploadRefused(f"unknown actor kind {actor_kind!r}")
    _require_pack(connection, project_id)
    filename = (str(filename or "").strip() or "document")[:200]
    pages = read_pages(payload, filename)
    name = (str(title or "").strip() or _title_from(pages, filename))[:200]
    sections = _as_dicts(propose_sections(pages, pattern=pattern or None, title=name))
    if not sections:
        raise UploadRefused(f"'{filename}' has no section with any text in it.")
    ensure_schema(connection)
    upload_id = make_id("kgup")
    connection.execute(text(
        """
        INSERT INTO kg_document_uploads (id, project_id, title, filename, sha256, size_bytes,
          page_count, pattern, sections, status, proposed_by_kind, proposed_by, created_at)
        VALUES (:id, :p, :title, :filename, :sha, :size, :pages, :pattern, CAST(:sections AS JSONB),
          'pending', :kind, :by, :now)
        """), {"id": upload_id, "p": project_id, "title": name, "filename": filename,
               "sha": hashlib.sha256(payload).hexdigest(), "size": len(payload), "pages": len(pages),
               "pattern": pattern or None, "sections": json.dumps(sections), "kind": actor_kind,
               "by": actor_ref, "now": now_utc()})
    return preview_payload(_row(connection, project_id, upload_id))


def get_upload(connection: Connection, *, project_id: str, upload_id: str) -> dict[str, Any]:
    return preview_payload(_row(connection, project_id, upload_id))


def pending_uploads(connection: Connection, *, project_id: str) -> list[dict[str, Any]]:
    """Uploads waiting for a person's confirmation, newest first (an agent's proposals too)."""
    ensure_schema(connection)
    rows = connection.execute(text(
        "SELECT * FROM kg_document_uploads WHERE project_id = :p AND status = 'pending' "
        "ORDER BY created_at DESC LIMIT 20"), {"p": project_id}).mappings().all()
    return [preview_payload(dict(r)) for r in rows]


def apply_edits(sections: list[dict[str, Any]], edits: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Drop, merge-with-next and split-at-line, in order, on a copy. A section is always a run of
    the extracted lines: nothing is retyped."""
    out = []
    for s in sections:
        lines = list(s.get("lines") or [])
        pages = list(s.get("line_pages") or [])
        if len(pages) != len(lines):
            pages = [s["page"]] * len(lines)
        out.append(dict(s, lines=lines, line_pages=pages))
    for edit in edits or []:
        if not isinstance(edit, Mapping):
            raise UploadRefused("Each edit is {op, key[, line]}.")
        op = str(edit.get("op") or "")
        key = str(edit.get("key") or "")
        index = next((i for i, s in enumerate(out) if s["key"] == key), None)
        if index is None:
            raise UploadRefused(f"There is no section {key!r} in this preview.")
        section = out[index]
        if op == "drop":
            out.pop(index)
        elif op == "merge":
            if index + 1 >= len(out):
                raise UploadRefused(f"“{section['title']}” is the last section: there is nothing after it to merge.")
            nxt = out.pop(index + 1)
            section["lines"] += nxt["lines"]
            section["line_pages"] += nxt["line_pages"]
            section["page_end"] = max(section["page_end"], nxt["page_end"])
        elif op == "split":
            try:
                at = int(edit.get("line"))
            except (TypeError, ValueError) as error:
                raise UploadRefused("A split names the line it starts at.") from error
            if not 0 < at < len(section["lines"]):
                raise UploadRefused(f"Line {at} is not inside “{section['title']}” "
                                    f"(it has {len(section['lines'])} lines).")
            head = section["lines"][at].strip()
            card = RULE_CARD.match(head)
            tail = {"key": "", "title": head[:200], "kind": "rule_card" if card else "custom",
                    "page": section["line_pages"][at], "page_end": section["page_end"],
                    "rule_id": card.group("id") if card else None,
                    "lines": section["lines"][at:], "line_pages": section["line_pages"][at:]}
            section["lines"] = section["lines"][:at]
            section["line_pages"] = section["line_pages"][:at]
            section["page_end"] = section["line_pages"][-1]
            out.insert(index + 1, tail)
        else:
            raise UploadRefused(f"Unknown edit {op!r}: use drop, merge or split.")
        _renumber(out)
    if not out:
        raise UploadRefused("Every section was dropped: nothing would be added.")
    return out


def section_lines(connection: Connection, *, project_id: str, upload_id: str, key: str) -> list[str]:
    """One section's lines, for choosing where to split it."""
    row = _row(connection, project_id, upload_id)
    for s in row["sections"] or []:
        if s["key"] == key:
            return list(s.get("lines") or [])
    raise LookupError(f"no section {key!r} in upload {upload_id}")


def edit_upload(connection: Connection, *, project_id: str, upload_id: str,
                edits: Sequence[Mapping[str, Any]], title: Optional[str] = None) -> dict[str, Any]:
    row = _row(connection, project_id, upload_id)
    if row["status"] != "pending":
        raise UploadRefused(f"This upload is {row['status']}; only a pending upload changes.")
    sections = apply_edits(list(row["sections"] or []), edits)
    name = (str(title).strip()[:200] if title is not None and str(title).strip() else row["title"])
    connection.execute(text(
        "UPDATE kg_document_uploads SET sections = CAST(:s AS JSONB), title = :t WHERE id = :id"),
        {"s": json.dumps(sections), "t": name, "id": upload_id})
    return preview_payload(_row(connection, project_id, upload_id))


def discard_upload(connection: Connection, *, project_id: str, upload_id: str, actor_ref: str) -> dict[str, Any]:
    row = _row(connection, project_id, upload_id)
    if row["status"] != "pending":
        raise UploadRefused(f"This upload is {row['status']}; only a pending upload is discarded.")
    connection.execute(text(
        "UPDATE kg_document_uploads SET status = 'discarded', confirmed_by = :by, confirmed_at = :now "
        "WHERE id = :id"), {"by": actor_ref, "now": now_utc(), "id": upload_id})
    return preview_payload(_row(connection, project_id, upload_id))


def _citation(title: str, section: Mapping[str, Any]) -> str:
    pages = (f"p. {section['page']}" if section["page"] == section["page_end"]
             else f"pp. {section['page']}–{section['page_end']}")
    return f"{title}, {_label(section)}, {pages}"


def confirm_upload(
    connection: Connection, *, project_id: str, upload_id: str, actor_kind: str = "person",
    actor_ref: Optional[str] = None, title: Optional[str] = None,
) -> dict[str, Any]:
    """A PERSON confirms the split: one ``document`` and one ``clause`` per section, written."""
    from .documents import add_document

    if actor_kind != "person":
        raise PermissionError("Only a person confirms how a document is split: an agent proposes "
                              "the upload, and a person confirms it in Knowledge Graph › Documents.")
    if not str(actor_ref or "").strip():
        raise UploadRefused("Who is confirming is required (actor).")
    row = _row(connection, project_id, upload_id)
    if row["status"] != "pending":
        raise UploadRefused(f"This upload is already {row['status']}.")
    _require_pack(connection, project_id)
    name = (str(title or "").strip() or row["title"])[:200]
    sections = list(row["sections"] or [])
    labels: dict[str, int] = {}
    clauses = []
    for order, s in enumerate(sections, start=1):
        label = _label(s)
        labels[label] = labels.get(label, 0) + 1
        if labels[label] > 1:                       # two sections with one name stay two clauses
            label = f"{label} ({labels[label]})"
        clauses.append({
            "section": label, "text": _text_of(s), "citation": _citation(name, {**s, "rule_id": None, "title": label}),
            "page": s["page"], "page_end": s["page_end"], "order": order, "section_kind": s["kind"],
            "heading": s["title"], "rule_id": s.get("rule_id"),
        })
    written = add_document(
        connection, project_id=project_id, title=name, source_url=f"upload:{row['sha256']}",
        citation=f"{name} ({row['filename']}, {row['page_count']} pages)", clauses=clauses,
        actor_kind="person", actor_ref=actor_ref,
        extra_attrs={"filename": row["filename"], "sha256": row["sha256"], "pages": row["page_count"],
                     "upload_id": upload_id, "sections": len(clauses)},
    )
    connection.execute(text(
        "UPDATE kg_document_uploads SET status = 'confirmed', confirmed_by = :by, confirmed_at = :now, "
        "document_id = :d, title = :t WHERE id = :id"),
        {"by": actor_ref, "now": now_utc(), "d": written["document_id"], "t": name, "id": upload_id})
    # A confirmed document is what "Extract rules" reads: install whatever the project's packs
    # now unlock (rulebook_extraction), so the press right after this one finds its workflow.
    # The document stands either way; a failed install is the next press's own refusal.
    from ..workflow_catalogue import seed_now_unlocked

    try:
        with connection.begin_nested():
            seed_now_unlocked(connection, project_id, actor=actor_ref or "boss")
    except Exception:  # noqa: BLE001
        pass
    return {**written, "title": name, "sections": len(clauses),
            "rule_cards": sum(1 for s in sections if s["kind"] == "rule_card")}
