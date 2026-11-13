"""The project wiki: readable pages people and agents keep current (BOSSAPP-891).

WHY. The knowledge graph holds what BOSS knows as facts (nodes and edges with trust tiers); a
person cannot read a project from it. The wiki is the page surface: per project, pages with a
title and a markdown body, every save a revision, links between pages (``[[slug]]``) read back as
backlinks.

THE ONE WRITER. :func:`save_page` is the only code that writes a page. A save that changes nothing
(same content hash as the current revision) adds no revision; any other save appends one. History
is append-only, and a restore is a NEW revision copying an old one (as skill_versions, 842).

AGENTS PROPOSE, PEOPLE APPROVE. An agent's write is never applied by itself: :func:`propose_patch`
parks it as a diff-bound change_consent (BOSSAPP-843, subject_kind ``wiki``) with the decision a
person answers, and approving that decision (:func:`apply_for_decision`) writes EXACTLY the
proposed page onto EXACTLY the revision it was proposed against — a page edited in between makes
the hash differ and the write refuses (:class:`ConsentStale`). A person's edit is its own consent.

THE MAINTAINER. :func:`maintain` renders the project's current knowledge-graph facts (nodes still
valid, grouped by kind) into pages it owns (``maintained_by = 'kg'``) and an index page. It writes
a page directly only while the page's current revision is its own; once a person or an approved
agent edit has changed it, a refreshed rendering is PROPOSED instead, so the maintainer never
silently overwrites a human. The knowledge graph stays canonical: the wiki renders it, it never
writes back.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import re
from typing import Any, Iterable, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .core import emit_event, make_id

AUTHOR_KINDS = ("person", "agent", "system")
MAINTAINER = "system:wiki_maintainer"
MAINTAINED_BY_KG = "kg"
INDEX_SLUG = "knowledge"
MAX_BODY = 200_000
MAX_TITLE = 200
#: Facts per rendered page. A kind with more is cut, and the page says how many were left out.
MAX_FACTS_PER_PAGE = 200
#: The code layer (files, symbols, modules...) is an index, not something a person reads as prose.
_CODE_KINDS = frozenset({"config_key", "endpoint", "file", "module", "queue", "symbol", "table"})
_TIER_ORDER = {"approved": 0, "governed": 1, "observed": 2, "inferred": 3}
_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,79}$")
_LINK = re.compile(r"\[\[([a-z0-9][a-z0-9-]{0,79})(?:\|[^\]]*)?\]\]")


class WikiError(ValueError):
    """Something the caller can fix, phrased for them."""

    status = 422


class PageNotFound(WikiError):
    status = 404


class ConsentStale(WikiError):
    """An approved patch whose page moved (or whose proposal changed) since it was shown."""

    status = 409


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", str(value or "").strip().lower()).strip("-")
    return slug[:80].rstrip("-")


def _check_slug(slug: str) -> str:
    slug = str(slug or "").strip().lower()
    if not _SLUG.match(slug):
        raise WikiError("a page slug is lower-case letters, digits and dashes (1-80), "
                        "starting with a letter or digit")
    return slug


def _content(title: str, body: str) -> dict[str, str]:
    title = " ".join(str(title or "").split())
    if not title:
        raise WikiError("a page needs a title")
    if len(title) > MAX_TITLE:
        raise WikiError(f"a title is at most {MAX_TITLE} characters")
    body = str(body or "").replace("\r\n", "\n")
    if len(body) > MAX_BODY:
        raise WikiError(f"that page is {len(body)} characters; a page is at most {MAX_BODY}")
    return {"title": title, "body": body}


def content_hash(content: Optional[dict[str, Any]]) -> str:
    if not content:
        return ""
    body = json.dumps({"title": content.get("title") or "", "body": content.get("body") or ""},
                      sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def links_in(body: str) -> list[str]:
    seen: list[str] = []
    for slug in _LINK.findall(body or ""):
        if slug not in seen:
            seen.append(slug)
    return seen


def _iso(value: Any) -> Optional[str]:
    return value.isoformat() if value is not None and hasattr(value, "isoformat") else value


def _revision(row: Any) -> dict[str, Any]:
    sources = row["sources"]
    if isinstance(sources, str):
        sources = json.loads(sources)
    return {
        "id": row["id"], "revision_no": int(row["revision_no"]), "title": row["title"],
        "body": row["body"], "content_hash": row["content_hash"],
        "author_kind": row["author_kind"], "author_id": row["author_id"],
        "source_note": row["source_note"], "consent_id": row["consent_id"],
        "sources": sources or [], "restored_from": row["restored_from"],
        "created_at": _iso(row["created_at"]),
    }


def _page_row(connection: Connection, project_id: str, slug: str,
              for_update: bool = False) -> Optional[dict[str, Any]]:
    row = connection.execute(
        text("SELECT * FROM wiki_pages WHERE project_id = :p AND slug = :s"
             + (" FOR UPDATE" if for_update else "")),
        {"p": project_id, "s": slug},
    ).mappings().first()
    return dict(row) if row else None


def current_revision(connection: Connection, project_id: str,
                     slug: str) -> Optional[dict[str, Any]]:
    row = connection.execute(
        text("SELECT r.* FROM wiki_revisions r JOIN wiki_pages p ON p.current_revision_id = r.id "
             "WHERE p.project_id = :p AND p.slug = :s"),
        {"p": project_id, "s": slug},
    ).mappings().first()
    return _revision(row) if row else None


def _origin(connection: Connection, project_id: str, actor: str) -> str:
    from .skill_definitions import _origin_of

    origin = _origin_of(connection, project_id, actor)
    return origin if origin in AUTHOR_KINDS else "person"


# ── reads ───────────────────────────────────────────────────────────────────────────────────


def list_pages(connection: Connection, project_id: str) -> list[dict[str, Any]]:
    rows = connection.execute(
        text("""
            SELECT p.slug, p.maintained_by, p.created_at, r.title, r.revision_no, r.author_kind,
                   r.author_id, r.created_at AS updated_at, length(r.body) AS chars,
                   (SELECT count(*) FROM change_consents c WHERE c.project_id = p.project_id
                      AND c.subject_kind = 'wiki' AND c.subject_key = p.slug
                      AND c.status = 'pending') AS pending
              FROM wiki_pages p JOIN wiki_revisions r ON r.id = p.current_revision_id
             WHERE p.project_id = :p
             ORDER BY (p.slug = :index) DESC, lower(r.title)
        """),
        {"p": project_id, "index": INDEX_SLUG},
    ).mappings().all()
    return [{**dict(r), "created_at": _iso(r["created_at"]), "updated_at": _iso(r["updated_at"]),
             "pending": int(r["pending"] or 0)} for r in rows]


def backlinks(connection: Connection, project_id: str, slug: str) -> list[dict[str, str]]:
    """Pages whose current revision links to `slug`. Read from the bodies; nothing to keep in
    step."""
    rows = connection.execute(
        text("SELECT p.slug, r.title, r.body FROM wiki_pages p "
             "JOIN wiki_revisions r ON r.id = p.current_revision_id "
             "WHERE p.project_id = :p AND p.slug <> :s AND strpos(r.body, :needle) > 0 "
             "ORDER BY lower(r.title)"),
        {"p": project_id, "s": slug, "needle": f"[[{slug}"},
    ).mappings().all()
    return [{"slug": r["slug"], "title": r["title"]} for r in rows if slug in links_in(r["body"])]


def get_page(connection: Connection, project_id: str, slug: str) -> dict[str, Any]:
    slug = _check_slug(slug)
    page = _page_row(connection, project_id, slug)
    current = current_revision(connection, project_id, slug)
    if page is None or current is None:
        raise PageNotFound(f"no wiki page {slug!r} in this project")
    from . import change_consent

    return {
        "slug": slug, "maintained_by": page["maintained_by"], "title": current["title"],
        "body": current["body"], "revision": current,
        "links": links_in(current["body"]),
        "backlinks": backlinks(connection, project_id, slug),
        "pending": change_consent.pending(connection, project_id, "wiki", slug),
    }


def list_revisions(connection: Connection, project_id: str, slug: str) -> list[dict[str, Any]]:
    slug = _check_slug(slug)
    page = _page_row(connection, project_id, slug)
    if page is None:
        raise PageNotFound(f"no wiki page {slug!r} in this project")
    rows = connection.execute(
        text("SELECT * FROM wiki_revisions WHERE page_id = :i ORDER BY revision_no DESC"),
        {"i": page["id"]},
    ).mappings().all()
    return [{**_revision(r), "current": r["id"] == page["current_revision_id"]} for r in rows]


def get_revision(connection: Connection, project_id: str, slug: str,
                 revision: Any) -> dict[str, Any]:
    ref = str(revision or "").strip()
    for item in list_revisions(connection, project_id, slug):
        if (ref.isdigit() and item["revision_no"] == int(ref)) or item["id"] == ref:
            return item
    raise PageNotFound(f"{slug} has no revision {ref!r}")


def diff_text(before: Optional[dict[str, Any]], after: dict[str, Any], *,
              before_name: str = "current", after_name: str = "proposed") -> str:
    def lines(c: Optional[dict[str, Any]]) -> list[str]:
        if not c:
            return []
        return [f"# {c.get('title') or ''}", *str(c.get("body") or "").splitlines()]

    return "\n".join(difflib.unified_diff(lines(before), lines(after), fromfile=before_name,
                                          tofile=after_name, lineterm=""))


# ── the one writer ──────────────────────────────────────────────────────────────────────────


def save_page(
    connection: Connection,
    *,
    project_id: str,
    actor: str,
    slug: str,
    title: str,
    body: str,
    origin: Optional[str] = None,
    source_note: str = "",
    sources: Iterable[str] = (),
    require_consent: bool = False,
    restored_from: Optional[str] = None,
    maintained_by: Optional[str] = None,
) -> dict[str, Any]:
    """Write a page; returns {slug, revision, changed}. An agent's write needs an approved consent.

    `origin` defaults to what the actor is (skill_definitions._origin_of): an agent principal is
    an agent and must consume a change_consent whose hash is this exact write; a person and the
    platform write directly."""
    from . import change_consent

    slug = _check_slug(slug)
    content = _content(title, body)
    who = origin if origin in AUTHOR_KINDS else _origin(connection, project_id, actor)
    before = current_revision(connection, project_id, slug)
    consent_id = None
    if who == "agent" or require_consent:
        digest = change_consent.diff_hash("wiki", slug, content_hash(before), content)
        consent_id = change_consent.consume(connection, project_id=project_id,
                                            subject_kind="wiki", subject_key=slug, digest=digest)
        if consent_id is None:
            raise change_consent.ConsentRequired(
                f"{actor} cannot change wiki page {slug!r} by itself: an agent's change to the "
                "wiki is written only after a person approves exactly that change. Propose it "
                "with wiki_propose — the person sees the diff and approves it.")
    page = _page_row(connection, project_id, slug, for_update=True)
    if page is None:
        page_id = make_id("wkp")
        connection.execute(
            text("INSERT INTO wiki_pages (id, project_id, slug, maintained_by, created_by) "
                 "VALUES (:i, :p, :s, :m, :by) ON CONFLICT (project_id, slug) DO NOTHING"),
            {"i": page_id, "p": project_id, "s": slug, "m": maintained_by, "by": actor})
        page = _page_row(connection, project_id, slug, for_update=True)
    assert page is not None
    digest = content_hash(content)
    if before is not None and before["content_hash"] == digest and restored_from is None:
        return {"slug": slug, "revision": before, "changed": False}
    number = int(connection.execute(
        text("SELECT COALESCE(MAX(revision_no), 0) FROM wiki_revisions WHERE page_id = :i"),
        {"i": page["id"]}).scalar() or 0) + 1
    note = source_note or ("approved agent change" if consent_id else "")
    row = connection.execute(
        text("""
            INSERT INTO wiki_revisions (id, project_id, page_id, revision_no, title, body,
                                        content_hash, author_kind, author_id, source_note,
                                        consent_id, sources, restored_from)
            VALUES (:id, :p, :pg, :n, :t, :b, :h, :ak, :ai, :note, :cid, CAST(:src AS JSONB), :rf)
            RETURNING *
        """),
        {"id": make_id("wkr"), "p": project_id, "pg": page["id"], "n": number,
         "t": content["title"], "b": content["body"], "h": digest, "ak": who,
         "ai": str(actor or "")[:200], "note": str(note)[:500], "cid": consent_id,
         "src": json.dumps(sorted({str(s) for s in sources})[:500]), "rf": restored_from},
    ).mappings().one()
    connection.execute(
        text("UPDATE wiki_pages SET current_revision_id = :r, updated_at = now(), "
             "maintained_by = COALESCE(:m, maintained_by) WHERE id = :i"),
        {"r": row["id"], "i": page["id"], "m": maintained_by})
    emit_event(connection, project_id, "wiki.page_saved", actor, "wiki_page", page["id"],
               {"slug": slug, "revision_no": number, "origin": who,
                **({"consent_id": consent_id} if consent_id else {})})
    return {"slug": slug, "revision": _revision(row), "changed": True}


def restore(connection: Connection, *, project_id: str, actor: str, slug: str,
            revision: Any) -> dict[str, Any]:
    """Make an old revision current again — as a NEW revision copying it. A person's act."""
    old = get_revision(connection, project_id, slug, revision)
    return save_page(connection, project_id=project_id, actor=actor, slug=slug,
                     title=old["title"], body=old["body"], origin="person",
                     source_note=f"restored revision {old['revision_no']}",
                     sources=old["sources"], restored_from=old["id"])


# ── agents propose ──────────────────────────────────────────────────────────────────────────


def _patched(before: Optional[dict[str, Any]], *, title: Optional[str], body: Optional[str],
             edits: Optional[list[dict[str, Any]]]) -> dict[str, str]:
    """The page a proposal would make: a whole new body, or find/replace edits on the current."""
    if body is None and not edits:
        raise WikiError("a patch is a new body or a list of edits ({find, replace})")
    new_title = title if (title or "").strip() else (before or {}).get("title") or ""
    if body is not None:
        return _content(new_title, body)
    if before is None:
        raise WikiError("edits change an existing page; a new page needs a body")
    text_ = before["body"]
    for i, edit in enumerate(edits or []):
        find = str((edit or {}).get("find") or "")
        if not find:
            raise WikiError(f"edit {i + 1} has nothing to find")
        count = text_.count(find)
        if count != 1:
            raise WikiError(f"edit {i + 1}: the text to find appears {count} times on the page; "
                            "it must appear exactly once")
        text_ = text_.replace(find, str((edit or {}).get("replace") or ""))
    return _content(new_title, text_)


def propose_patch(
    connection: Connection,
    *,
    project_id: str,
    actor: str,
    slug: str,
    title: Optional[str] = None,
    body: Optional[str] = None,
    edits: Optional[list[dict[str, Any]]] = None,
    note: str = "",
    sources: Iterable[str] = (),
    maintained_by: Optional[str] = None,
) -> dict[str, Any]:
    """Park a change to a page as a diff-bound consent + the decision a person answers.

    Writes NO revision. The consent carries the hash of (current revision, proposed page) that
    save_page checks at the write."""
    from . import change_consent

    slug = _check_slug(slug)
    before = current_revision(connection, project_id, slug)
    proposed = _patched(before, title=title, body=body, edits=edits)
    if before is not None and content_hash(before) == content_hash(proposed):
        return {"status": "unchanged", "slug": slug,
                "next": "The page already says exactly this; nothing to approve."}
    base = content_hash(before)
    digest = change_consent.diff_hash("wiki", slug, base, proposed)
    for waiting in change_consent.pending(connection, project_id, "wiki", slug):
        if waiting["diff_hash"] == digest:
            return {"status": "pending", "slug": slug, "decision_id": waiting["decision_id"],
                    "consent_id": waiting["id"], "diff_hash": digest, "duplicate": True,
                    "next": "This exact change is already waiting for a person."}
    diff = diff_text(before, proposed) or f"+ # {proposed['title']}"
    shown = diff if len(diff) <= 4000 else diff[:4000] + "\n…"
    question = (f"{'Change' if before else 'Create'} wiki page {proposed['title']!r}?"
                + (f"\n\n{note.strip()}" if note.strip() else "") + f"\n\n{shown}")
    opened = change_consent.request(
        connection, project_id=project_id, subject_kind="wiki", subject_key=slug,
        base_hash=base, proposed=proposed, diff=diff, requested_by=actor, question=question,
        action="apply_wiki_patch",
        recommendation={"slug": slug, "title": proposed["title"], "unified_diff": diff,
                        **({"note": note.strip()} if note.strip() else {})},
        reason="wiki_patch", risk_tier=0,
        meta={"sources": sorted({str(s) for s in sources})[:500], "note": note.strip(),
              "maintained_by": maintained_by})
    return {"status": "pending", "slug": slug, "decision_id": opened["decision_id"],
            "consent_id": opened["consent_id"], "diff_hash": opened["diff_hash"],
            "diff": diff, "next": "Nothing is written until a person approves this exact change."}


def apply_for_decision(connection: Connection, decision: dict[str, Any],
                       actor: str) -> dict[str, Any]:
    """The person approved the wiki decision: write exactly what it showed (once)."""
    from . import change_consent

    consent = change_consent.approve_for_decision(connection, str(decision["id"]), actor)
    if consent is None:
        raise WikiError("this decision has no wiki change waiting to be applied")
    meta = (consent["proposed"] or {}).get("_meta") or {}
    content = {k: v for k, v in consent["proposed"].items() if k != "_meta"}
    try:
        return save_page(
            connection, project_id=str(decision["project_id"]),
            actor=consent["requested_by"] or actor, slug=consent["subject_key"],
            title=str(content.get("title") or ""), body=str(content.get("body") or ""),
            origin="agent" if consent["requested_by"] != MAINTAINER else "system",
            source_note=(f"{meta.get('note') or 'proposed by ' + consent['requested_by']}"
                         f", approved by {actor}")[:500],
            sources=meta.get("sources") or (), require_consent=True,
            maintained_by=meta.get("maintained_by"))
    except change_consent.ConsentRequired as error:
        raise ConsentStale(
            f"wiki page {consent['subject_key']!r} changed after this was proposed, so this is "
            "no longer the change you are looking at. Reject it and ask for a new proposal."
        ) from error


def reject_for_decision(connection: Connection, decision: dict[str, Any], actor: str) -> None:
    from . import change_consent

    change_consent.reject_for_decision(connection, str(decision["id"]), actor)


# ── the maintainer ──────────────────────────────────────────────────────────────────────────


def _kind_words(kind: str) -> str:
    return kind.replace("_", " ").title()


def _facts(connection: Connection, project_id: str) -> dict[str, list[dict[str, Any]]]:
    rows = connection.execute(
        text("SELECT id, kind, node_key, name, body, trust_tier, confidence, source "
             "FROM kg_nodes WHERE project_id = :p AND valid_to IS NULL "
             "ORDER BY kind, name, node_key"),
        {"p": project_id},
    ).mappings().all()
    grouped: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        if r["kind"] in _CODE_KINDS:
            continue
        grouped.setdefault(r["kind"], []).append(dict(r))
    return grouped


def _render_kind(kind: str, facts: list[dict[str, Any]]) -> tuple[str, str, list[str]]:
    facts = sorted(facts, key=lambda f: (_TIER_ORDER.get(f["trust_tier"], 9),
                                         str(f["name"]).lower()))
    shown = facts[:MAX_FACTS_PER_PAGE]
    lines = [f"Rendered from the knowledge graph: {len(facts)} current "
             f"{_kind_words(kind).lower()} fact(s). Approved and governed facts come first.",
             "", "Back to [[knowledge]].", ""]
    for f in shown:
        detail = " ".join(str(f["body"] or "").split())
        if len(detail) > 400:
            detail = detail[:400] + "…"
        tier = f["trust_tier"]
        line = f"- **{f['name']}** ({tier}, from {f['source']})"
        lines.append(line + (f": {detail}" if detail else ""))
    if len(facts) > len(shown):
        lines += ["", f"{len(facts) - len(shown)} more not shown; read them in Knowledge."]
    return (f"Knowledge: {_kind_words(kind)}", "\n".join(lines) + "\n",
            [str(f["id"]) for f in shown])


def _render_index(kinds: dict[str, int]) -> str:
    lines = ["What BOSS knows about this project, rendered from the knowledge graph. The graph "
             "is the source; these pages are kept current from it. Edit a page to add what the "
             "graph does not say — a later refresh is then proposed, never written over you.", ""]
    for kind, count in sorted(kinds.items()):
        lines.append(f"- [[kg-{slugify(kind)}|{_kind_words(kind)}]] — {count} fact(s)")
    if not kinds:
        lines.append("The knowledge graph has no current facts for this project yet.")
    return "\n".join(lines) + "\n"


def _maintain_one(connection: Connection, project_id: str, slug: str, title: str, body: str,
                  sources: list[str]) -> str:
    current = current_revision(connection, project_id, slug)
    page = _page_row(connection, project_id, slug)
    if current is not None and content_hash(current) == content_hash({"title": title,
                                                                        "body": body}):
        return "unchanged"
    if current is None or (current["author_kind"] == "system" and
                           current["author_id"] == MAINTAINER and
                           (page or {}).get("maintained_by") == MAINTAINED_BY_KG):
        save_page(connection, project_id=project_id, actor=MAINTAINER, slug=slug, title=title,
                  body=body, origin="system", source_note="rendered from the knowledge graph",
                  sources=sources, maintained_by=MAINTAINED_BY_KG)
        return "written"
    # Somebody else changed it since: propose the refresh rather than writing over them.
    result = propose_patch(connection, project_id=project_id, actor=MAINTAINER, slug=slug,
                           title=title, body=body, sources=sources,
                           note="The knowledge graph changed since this page was last edited.",
                           maintained_by=MAINTAINED_BY_KG)
    return "proposed" if result["status"] == "pending" else "unchanged"


def maintain(connection: Connection, project_id: str) -> dict[str, Any]:
    """Render the knowledge graph's current facts into the wiki. Returns what happened per page."""
    grouped = _facts(connection, project_id)
    report: dict[str, list[str]] = {"written": [], "proposed": [], "unchanged": []}
    for kind, facts in sorted(grouped.items()):
        title, body, sources = _render_kind(kind, facts)
        slug = f"kg-{slugify(kind)}"
        report[_maintain_one(connection, project_id, slug, title, body, sources)].append(slug)
    index_outcome = _maintain_one(connection, project_id, INDEX_SLUG, "Project knowledge",
                                  _render_index({k: len(v) for k, v in grouped.items()}), [])
    report[index_outcome].append(INDEX_SLUG)
    emit_event(connection, project_id, "wiki.maintained", MAINTAINER, "project", project_id,
               {k: len(v) for k, v in report.items()})
    return {"project_id": project_id, **report}
