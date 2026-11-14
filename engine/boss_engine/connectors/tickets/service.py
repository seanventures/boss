"""What Phase B calls: a ready connection for a project, and references across every connected
ticket system. Everything else is the adapter's contract (get_ticket, search, comment, ...)."""

from __future__ import annotations

import json
from typing import Any, Iterable, Optional

from .contract import TicketConnection, TicketRef
from .registry import REGISTRY, get_ticket_adapter


def connection_for(db: Any, project_id: str, provider_key: str = "", *,
                   person_id: Optional[str] = None, principal: Any = None,
                   app_settings: Any = None) -> Optional[TicketConnection]:
    """The project's CONNECTED ticket system (the default first, or the named one) as a
    TicketConnection whose credential resolves server-side. None when nothing is connected."""
    from ...services.work_orchestration import connected_tracker

    found = connected_tracker(db, project_id, provider_key)
    if found is None:
        return None
    config = found["configuration"]
    if isinstance(config, str):
        config = json.loads(config or "{}")
    return TicketConnection(str(found["provider_key"]), dict(config or {}), project_id=project_id,
                            db=db, app_settings=app_settings, person_id=person_id,
                            principal=principal)


def connections_for(db: Any, project_id: str, **kw: Any) -> list[TicketConnection]:
    """Every connected ticket system of a project (one per provider)."""
    from sqlalchemy import text

    keys = db.execute(text("SELECT provider_key FROM work_tracker_providers WHERE project_id = :p "
                           "AND status = 'active' ORDER BY is_default DESC, updated_at DESC"),
                      {"p": project_id}).scalars().all()
    out = []
    for key in keys:
        if REGISTRY.get(str(key)) is None:
            continue
        conn = connection_for(db, project_id, str(key), **kw)
        if conn is not None:
            out.append(conn)
    return out


def parse_refs(text: str, connections: Iterable[TicketConnection]) -> list[TicketRef]:
    """Every ticket reference in ``text`` that one of these connections owns (URLs, and keys
    whose project prefix is known to that connection). Order of appearance is not kept across
    systems; each reference appears once."""
    refs: list[TicketRef] = []
    seen: set[tuple[Any, ...]] = set()
    for conn in connections:
        adapter = get_ticket_adapter(conn.provider)
        for ref in adapter.parse_ref(text, conn=conn):
            marker = (ref.provider, ref.key, ref.id)
            if marker not in seen:
                seen.add(marker)
                refs.append(ref)
    return refs
