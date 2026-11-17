"""Publishing a page to Confluence is a PROPOSAL a person approves (BOSSAPP-1384).

``confluence_publish_page`` never writes. :func:`gather` reads the page it would change (HTTP, no
database transaction open) and :func:`file_proposal` parks the exact change as a diff-bound
``change_consent`` and the decision a person answers ("Approve a Confluence page").

APPROVING records the intent and COMMITS; the write happens AFTER that commit (:func:`reconcile`),
never inside the decision's transaction, so the decision and Confluence cannot disagree about a
half-done step. The write is a VERSIONED update: the consent records the version the author read,
and a page that moved since is refused (``VersionConflict``) and nothing is overwritten; the
failure is recorded on the consent (rejected) and as ``docs.confluence_publish_failed``. A write
claimed but never finished (a crash) is re-claimed after CLAIM_TTL, and :func:`reconcile_pending`
is the catch-up run by the next tool call or sync.
"""

from __future__ import annotations

import difflib
import json
import logging
import threading
from dataclasses import dataclass
from typing import Any, Optional

from sqlalchemy import event, text
from sqlalchemy.engine import Connection

from ..connectors.docs import VersionConflict
from ..connectors.tickets.contract import NotFound, TicketSystemError
from . import change_consent, docs_connections

logger = logging.getLogger(__name__)

ACTION = "apply_confluence_publish"
SUBJECT = "confluence"
MAX_MARKDOWN = 200_000
CLAIM_TTL = "10 minutes"
#: "thread": reconcile in a background thread right after the approval commits. "manual": tests
#: call reconcile_pending themselves.
DISPATCH = "thread"


class ConfluencePublishError(Exception):
    """A refusal in words: the proposal is malformed, or the connection is gone."""


@dataclass
class Gathered:
    """What was read from Confluence (no database involved) for a proposal."""
    proposed: dict[str, Any]
    before: str = ""
    url: str = ""
    page_id: str = ""
    unchanged: bool = False


def _diff(before: str, after: str, title: str) -> str:
    lines = list(difflib.unified_diff(before.splitlines(), after.splitlines(), "current", title,
                                      lineterm="", n=2))
    return "\n".join(lines) if lines else ""


def gather(client: Any, *, space: str, title: str = "", markdown: str, page_id: str = "",
           parent_id: str = "") -> Gathered:
    """Read the target (HTTP only) and build the change. Raises ConfluencePublishError when it is
    malformed; Confluence's own refusals (not found, auth) propagate typed."""
    space, title, page_id = (space or "").strip(), (title or "").strip(), (page_id or "").strip()
    if not markdown or not markdown.strip():
        raise ConfluencePublishError("The page body is empty.")
    if len(markdown) > MAX_MARKDOWN:
        raise ConfluencePublishError(f"The page is over {MAX_MARKDOWN:,} characters; split it.")
    if not (page_id or (space and title)):
        raise ConfluencePublishError("Name the page to update (page_id), or the space and the "
                                     "title of the page to create.")
    existing = None
    if page_id:
        existing = client.get_page(page_id)
        space = space or existing.space_key
    else:
        try:
            existing = client.find_page(title, space)
        except NotFound:
            existing = None
        if existing is None:
            client.space_id(space)          # a space that does not exist refuses here, not later
            if parent_id:
                client.get_page(parent_id, body=False)
    if existing and existing.truncated:
        raise ConfluencePublishError(
            "That page is too long to update safely from BOSS (it was cut when read); edit it in "
            "Confluence, or publish a new page.")
    final_title = title or (existing.title if existing else "")
    proposed = {"op": "update" if existing else "create", "space": space, "title": final_title,
                "markdown": markdown, "page_id": existing.id if existing else "",
                "parent_id": parent_id or "", "base_version": existing.version if existing else 0,
                "old_title": existing.title if existing else ""}
    before = existing.body_markdown if existing else ""
    unchanged = bool(existing and before.strip() == markdown.strip() and final_title == existing.title)
    return Gathered(proposed, before, existing.url if existing else "",
                    existing.id if existing else "", unchanged)


def file_proposal(db: Connection, *, project_id: str, actor: str, gathered: Gathered,
                  note: str = "") -> dict[str, Any]:
    """Park the change as a consent + decision (database only). Writes nothing to Confluence."""
    g, proposed = gathered, gathered.proposed
    if g.unchanged:
        return {"status": "unchanged", "page_id": g.page_id, "url": g.url,
                "next": "The page already says exactly this; nothing to approve."}
    final_title, space = proposed["title"], proposed["space"]
    existing = proposed["op"] == "update"
    key = f"{space}:{proposed['page_id'] or final_title}"
    base = f"v{proposed['base_version']}" if existing else ""
    digest = change_consent.diff_hash(SUBJECT, key, base, proposed)
    for waiting in change_consent.pending(db, project_id, SUBJECT, key):
        if waiting["diff_hash"] == digest:
            return {"status": "pending", "decision_id": waiting["decision_id"],
                    "consent_id": waiting["id"], "duplicate": True,
                    "next": "This exact change is already waiting for a person."}
    diff = _diff(g.before, proposed["markdown"], final_title) or f"+ # {final_title}\n" + "\n".join(
        f"+ {line}" for line in proposed["markdown"].splitlines()[:80])
    if existing and proposed["old_title"] and proposed["old_title"] != final_title:
        diff = f"title: {proposed['old_title']!r} -> {final_title!r}\n" + diff
    shown = diff if len(diff) <= 4000 else diff[:4000] + "\n…"
    verb = "Update" if existing else "Create"
    question = (f"{verb} Confluence page {final_title!r} in space {space}?"
                + (f"\n\n{note.strip()}" if note.strip() else "") + f"\n\n{shown}")
    opened = change_consent.request(
        db, project_id=project_id, subject_kind=SUBJECT, subject_key=key, base_hash=base,
        proposed=proposed, diff=diff, requested_by=actor, question=question, action=ACTION,
        recommendation={"space": space, "title": final_title, "op": proposed["op"],
                        "page_id": proposed["page_id"], "unified_diff": diff,
                        **({"note": note.strip()} if note.strip() else {})},
        reason="confluence_publish", risk_tier=1, meta={"note": note.strip()})
    return {"status": "pending", "operation": proposed["op"], "space": space, "title": final_title,
            "decision_id": opened["decision_id"], "consent_id": opened["consent_id"],
            "next": "Nothing is written to Confluence until a person approves this exact change."}


def propose(db: Connection, app_settings: Any, *, project_id: str, actor: str, space: str,
            title: str = "", markdown: str, page_id: str = "", parent_id: str = "",
            note: str = "") -> dict[str, Any]:
    """gather + file_proposal on one connection (callers that hold no transaction across HTTP use
    the two halves, as the tool does)."""
    _row, client = docs_connections.client_for(db, app_settings, project_id)
    g = gather(client, space=space, title=title, markdown=markdown, page_id=page_id,
               parent_id=parent_id)
    return file_proposal(db, project_id=project_id, actor=actor, gathered=g, note=note)


# ---- approval: record the intent and commit; the write follows --------------------------------


def apply_for_decision(db: Connection, app_settings: Any, decision: dict[str, Any],
                       actor: str) -> dict[str, Any]:
    """The person approved: mark the consent approved (the intent) and arrange for the write to
    run AFTER this transaction commits. No HTTP here."""
    consent = change_consent.approve_for_decision(db, str(decision["id"]), actor)
    if consent is None:
        raise ConfluencePublishError("This decision has no Confluence change waiting to be "
                                     "applied.")
    project_id = str(decision["project_id"])
    from .core import emit_event

    emit_event(db, project_id, "docs.confluence_publish_approved", actor, "change_consent",
               consent["id"], {"space": consent["proposed"].get("space"),
                               "title": consent["proposed"].get("title"),
                               "op": consent["proposed"].get("op")})
    if DISPATCH == "thread":
        engine, settings = db.engine, app_settings

        def after_commit(_conn: Any) -> None:
            threading.Thread(target=_safe_reconcile, args=(engine, settings, project_id),
                             name="confluence-publish", daemon=True).start()

        event.listen(db, "commit", after_commit, once=True)
    return {"queued": True, "consent_id": consent["id"]}


def _safe_reconcile(engine: Any, app_settings: Any, project_id: str) -> None:
    try:
        reconcile_pending(engine, app_settings, project_id=project_id)
    except Exception:  # noqa: BLE001 — the catch-up run retries
        logger.warning("confluence publish reconcile failed", exc_info=True)


def reject_for_decision(db: Connection, decision: dict[str, Any], actor: str) -> None:
    change_consent.reject_for_decision(db, str(decision["id"]), actor)


def _claim(engine: Any, consent_id: str) -> bool:
    with engine.begin() as db:
        got = db.execute(text(
            "UPDATE change_consents SET proposed = jsonb_set(proposed, '{_meta,claimed_at}', "
            "to_jsonb(now()::text), true) WHERE id = :i AND status = 'approved' AND "
            "((proposed->'_meta'->>'claimed_at') IS NULL OR "
            f"(proposed->'_meta'->>'claimed_at')::timestamptz < now() - interval '{CLAIM_TTL}') "
            "RETURNING id"), {"i": consent_id}).first()
    return got is not None


def reconcile_pending(engine: Any, app_settings: Any, *, project_id: Optional[str] = None,
                      limit: int = 20) -> list[dict[str, Any]]:
    """Perform every approved, unfinished Confluence write (HTTP outside any transaction)."""
    with engine.begin() as db:
        rows = db.execute(text(
            "SELECT * FROM change_consents WHERE subject_kind = 'confluence' AND status = 'approved' "
            "AND (CAST(:p AS TEXT) IS NULL OR project_id = :p) ORDER BY decided_at LIMIT :n"),
            {"p": project_id, "n": limit}).mappings().all()
    done = []
    for raw in rows:
        consent = change_consent._row(raw)  # noqa: SLF001
        if _claim(engine, consent["id"]):
            done.append(_publish_one(engine, app_settings, consent))
    return done


def _publish_one(engine: Any, app_settings: Any, consent: dict[str, Any]) -> dict[str, Any]:
    project_id = consent["project_id"]
    proposed = {k: v for k, v in consent["proposed"].items() if k != "_meta"}
    failure: Optional[str] = None
    page = None
    try:
        with engine.begin() as db:
            _row, client = docs_connections.client_for(db, app_settings, project_id)
        who = consent.get("decided_by") or "a person"
        if proposed["op"] == "update":
            page = client.update_page(
                proposed["page_id"], markdown=proposed["markdown"], title=proposed["title"],
                expected_version=int(proposed["base_version"]),
                message=f"Approved by {who}, proposed by {consent['requested_by']}")
        else:
            page = client.create_page(space=proposed["space"], title=proposed["title"],
                                      markdown=proposed["markdown"],
                                      parent_id=proposed.get("parent_id") or None)
    except VersionConflict as stale:
        failure = f"{stale} Ask for a new proposal."
    except TicketSystemError as refused:
        failure = str(refused)
    from .core import emit_event

    with engine.begin() as db:
        if failure is None and page is not None:
            change_consent.consume(db, project_id=project_id, subject_kind=SUBJECT,
                                   subject_key=consent["subject_key"], digest=consent["diff_hash"])
            emit_event(db, project_id, "docs.confluence_published", consent.get("decided_by") or "system",
                       "confluence_page", page.id,
                       {"op": proposed["op"], "space": proposed["space"], "title": page.title,
                        "version": page.version, "url": page.url,
                        "proposed_by": consent["requested_by"]})
            return {"ok": True, "page_id": page.id, "version": page.version, "url": page.url}
        db.execute(text("UPDATE change_consents SET status = 'rejected', decided_by = 'system', "
                        "decided_at = now() WHERE id = :i AND status = 'approved'"),
                   {"i": consent["id"]})
        emit_event(db, project_id, "docs.confluence_publish_failed", "system", "change_consent",
                   consent["id"], {"error": failure, "space": proposed.get("space"),
                                   "title": proposed.get("title")})
    return {"ok": False, "error": failure}
