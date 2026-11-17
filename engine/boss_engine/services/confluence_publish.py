"""Publishing a page to Confluence is a PROPOSAL a person approves (BOSSAPP-1384).

``confluence_publish_page`` never writes. :func:`propose` reads the page it would change (or
checks the space and parent for a new one), then parks the exact change as a diff-bound
``change_consent`` and the decision a person answers ("Approve a Confluence page"). Approving it
(:func:`apply_for_decision`, from DecisionService) writes exactly what the card showed, once, as
a VERSIONED update: the consent records the version the author read, and a page that moved since
is refused (``VersionConflict``) and the decision stays open — nothing is overwritten.
"""

from __future__ import annotations

import difflib
from typing import Any, Optional

from sqlalchemy.engine import Connection

from ..connectors.docs import VersionConflict
from ..connectors.docs.confluence import MAX_BODY_CHARS
from ..connectors.tickets.contract import NotFound, TicketSystemError
from . import change_consent, docs_connections

ACTION = "apply_confluence_publish"
SUBJECT = "confluence"
MAX_MARKDOWN = 200_000


class ConfluencePublishError(Exception):
    """A refusal in words: the proposal is malformed, or the page moved since it was proposed."""


def _diff(before: str, after: str, title: str) -> str:
    lines = list(difflib.unified_diff(before.splitlines(), after.splitlines(), "current", title,
                                      lineterm="", n=2))
    return "\n".join(lines) if lines else ""


def propose(db: Connection, app_settings: Any, *, project_id: str, actor: str, space: str,
            title: str = "", markdown: str, page_id: str = "", parent_id: str = "",
            note: str = "") -> dict[str, Any]:
    """Park a create or update as a consent + decision. Writes nothing to Confluence."""
    space, title, page_id = (space or "").strip(), (title or "").strip(), (page_id or "").strip()
    if not markdown or not markdown.strip():
        raise ConfluencePublishError("The page body is empty.")
    if len(markdown) > MAX_MARKDOWN:
        raise ConfluencePublishError(f"The page is over {MAX_MARKDOWN:,} characters; split it.")
    if not (page_id or (space and title)):
        raise ConfluencePublishError("Name the page to update (page_id), or the space and the "
                                     "title of the page to create.")
    _row, client = docs_connections.client_for(db, app_settings, project_id)
    existing = None
    if page_id:
        existing = client.get_page(page_id)
        space = space or existing.space_key
    elif title:
        try:
            existing = client.find_page(title, space)
        except NotFound:
            existing = None
        if existing is None:
            client.space_id(space)          # a space that does not exist refuses here, not later
            if parent_id:
                client.get_page(parent_id, body=False)
    op = "update" if existing else "create"
    final_title = title or (existing.title if existing else "")
    proposed = {"op": op, "space": space, "title": final_title, "markdown": markdown,
                "page_id": existing.id if existing else "", "parent_id": parent_id or "",
                "base_version": existing.version if existing else 0}
    if existing and existing.truncated:
        raise ConfluencePublishError(
            "That page is too long to update safely from BOSS (it was cut when read); edit it in "
            "Confluence, or publish a new page.")
    before = existing.body_markdown if existing else ""
    if existing and before.strip() == markdown.strip() and (not title or title == existing.title):
        return {"status": "unchanged", "page_id": existing.id, "url": existing.url,
                "next": "The page already says exactly this; nothing to approve."}
    key = f"{space}:{proposed['page_id'] or final_title}"
    base = f"v{proposed['base_version']}" if existing else ""
    digest = change_consent.diff_hash(SUBJECT, key, base, proposed)
    for waiting in change_consent.pending(db, project_id, SUBJECT, key):
        if waiting["diff_hash"] == digest:
            return {"status": "pending", "decision_id": waiting["decision_id"],
                    "consent_id": waiting["id"], "duplicate": True,
                    "next": "This exact change is already waiting for a person."}
    diff = _diff(before, markdown, final_title) or f"+ # {final_title}\n" + "\n".join(
        f"+ {line}" for line in markdown.splitlines()[:80])
    shown = diff if len(diff) <= 4000 else diff[:4000] + "\n…"
    verb = "Update" if existing else "Create"
    question = (f"{verb} Confluence page {final_title!r} in space {space}?"
                + (f"\n\n{note.strip()}" if note.strip() else "") + f"\n\n{shown}")
    opened = change_consent.request(
        db, project_id=project_id, subject_kind=SUBJECT, subject_key=key, base_hash=base,
        proposed=proposed, diff=diff, requested_by=actor, question=question, action=ACTION,
        recommendation={"space": space, "title": final_title, "op": op,
                        "page_id": proposed["page_id"], "unified_diff": diff,
                        **({"note": note.strip()} if note.strip() else {})},
        reason="confluence_publish", risk_tier=1, meta={"note": note.strip()})
    return {"status": "pending", "operation": op, "space": space, "title": final_title,
            "decision_id": opened["decision_id"], "consent_id": opened["consent_id"],
            "next": "Nothing is written to Confluence until a person approves this exact change."}


def apply_for_decision(db: Connection, app_settings: Any, decision: dict[str, Any],
                       actor: str) -> dict[str, Any]:
    """The person approved: write exactly what the card showed, as a versioned update."""
    consent = change_consent.approve_for_decision(db, str(decision["id"]), actor)
    if consent is None:
        raise ConfluencePublishError("This decision has no Confluence change waiting to be "
                                     "applied.")
    project_id = str(decision["project_id"])
    proposed = {k: v for k, v in consent["proposed"].items() if k != "_meta"}
    try:
        _row, client = docs_connections.client_for(db, app_settings, project_id)
        if proposed["op"] == "update":
            page = client.update_page(
                proposed["page_id"], markdown=proposed["markdown"], title=proposed["title"],
                expected_version=int(proposed["base_version"]),
                message=f"Approved by {actor}, proposed by {consent['requested_by']}")
        else:
            page = client.create_page(space=proposed["space"], title=proposed["title"],
                                      markdown=proposed["markdown"],
                                      parent_id=proposed.get("parent_id") or None)
    except VersionConflict as stale:
        raise ConfluencePublishError(
            f"{stale} Reject this one and ask for a new proposal.") from stale
    except TicketSystemError as refused:
        raise ConfluencePublishError(str(refused)) from refused
    change_consent.consume(db, project_id=project_id, subject_kind=SUBJECT,
                           subject_key=consent["subject_key"], digest=consent["diff_hash"])
    from .core import emit_event

    emit_event(db, project_id, "docs.confluence_published", actor, "confluence_page", page.id,
               {"op": proposed["op"], "space": proposed["space"], "title": page.title,
                "version": page.version, "url": page.url, "proposed_by": consent["requested_by"]})
    return {"page_id": page.id, "version": page.version, "url": page.url}


def reject_for_decision(db: Connection, decision: dict[str, Any], actor: str) -> None:
    change_consent.reject_for_decision(db, str(decision["id"]), actor)
