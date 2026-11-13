"""wiki_read and wiki_propose: agents read the project wiki and propose changes to it (BOSSAPP-891).

wiki_propose is a QUEUED write (like record_learning and kg_propose_fact): it records a
diff-bound change_consent and the decision a person answers, and changes no page until that
person approves exactly that diff (services/wiki.propose_patch). An agent never writes a page.
"""
from __future__ import annotations

from .registry import Exposure, ToolSpec

_READ_PARAMS = {
    "type": "object",
    "properties": {
        "slug": {"type": "string",
                 "description": "the page to read; leave out to list every page"},
        "project_id": {"type": "string"},
    },
    "additionalProperties": False,
}

_PROPOSE_PARAMS = {
    "type": "object",
    "properties": {
        "slug": {"type": "string",
                 "description": "lower-case-with-dashes; a new slug proposes a new page"},
        "title": {"type": "string", "description": "the page title (kept when left out)"},
        "body": {"type": "string",
                 "description": "the WHOLE new page in markdown; link pages as [[slug]]. "
                                "Leave out and give edits to change part of the page."},
        "edits": {"type": "array", "maxItems": 20,
                  "description": "find/replace edits on the current page; each `find` must "
                                 "appear on it exactly once",
                  "items": {"type": "object",
                            "properties": {"find": {"type": "string"},
                                           "replace": {"type": "string"}},
                            "required": ["find", "replace"], "additionalProperties": False}},
        "note": {"type": "string", "description": "why, in a sentence, for the person"},
        "project_id": {"type": "string"},
    },
    "required": ["slug"],
    "additionalProperties": False,
}


def _read(ctx, *, project_id, actor, args):
    from ..services import wiki

    args = args or {}
    pid = args.get("project_id") or project_id
    slug = str(args.get("slug") or "").strip()
    with ctx.begin() as connection:
        if not slug:
            pages = wiki.list_pages(connection, pid)
            return {"ok": True, "count": len(pages),
                    "pages": [{"slug": p["slug"], "title": p["title"],
                               "revision_no": p["revision_no"], "pending": p["pending"]}
                              for p in pages]}
        try:
            page = wiki.get_page(connection, pid, slug)
        except wiki.WikiError as error:
            return {"ok": False, "error": str(error)}
    return {"ok": True, "slug": page["slug"], "title": page["title"], "body": page["body"],
            "revision_no": page["revision"]["revision_no"], "links": page["links"],
            "backlinks": page["backlinks"], "pending_changes": len(page["pending"])}


def _propose(ctx, *, project_id, actor, args):
    from ..services import wiki

    args = args or {}
    pid = args.get("project_id") or project_id
    try:
        with ctx.begin() as connection:
            with connection.begin_nested():
                result = wiki.propose_patch(
                    connection, project_id=pid, actor=actor,
                    slug=str(args.get("slug") or "").strip(),
                    title=args.get("title"), body=args.get("body"),
                    edits=args.get("edits") if isinstance(args.get("edits"), list) else None,
                    note=str(args.get("note") or ""))
    except wiki.WikiError as error:
        return {"ok": False, "error": str(error)}
    result.pop("diff", None)
    return {"ok": True, **result,
            "note": ("Proposed, NOT written. Tell the person a wiki change is waiting for their "
                     "approval; do not say the page is updated.")
            if result["status"] == "pending" else result.get("next", "")}


def register_wiki_tools(reg) -> None:
    reg.register(ToolSpec(
        name="wiki_read",
        description="Read the project wiki: with no slug, the list of pages; with a slug, that "
                    "page's markdown, its links and the pages linking to it. The wiki is what "
                    "people read about this project; pages named kg-* are rendered from the "
                    "knowledge graph.",
        parameters=_READ_PARAMS, handler=_read, kind="read", permission="knowledge:read",
        exposure=Exposure(tags=("knowledge", "wiki"))))
    reg.register(ToolSpec(
        name="wiki_propose",
        description="Propose a change to a wiki page (or a new page) so the project's docs stay "
                    "current: a whole new body, or find/replace edits. Nothing is written until a "
                    "person approves exactly this change; say so rather than saying it is done.",
        parameters=_PROPOSE_PARAMS, handler=_propose, kind="write", reversibility="safe",
        # A QUEUED WRITE: it changes no page until a person approves the diff (change_consent).
        queues_for_review=True, permission="knowledge:read",
        exposure=Exposure(tags=("knowledge", "wiki"))))
