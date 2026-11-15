"""THE ROUTER SEAM (P9a-1 → P9a-2). `route(connection, conn, event, event_row) -> list[Fire]`.

The processor (services/intake.py) hands every parsed, screened, loop-checked event here and
fires whatever comes back through `agent_triggers.fire`. P9a-2 replaces `route`'s body with the
real router — the filter DSL over the envelope (LLD §5.2), addressing, thread keys (§5.4),
identity (§6), per-entity rate guards and storm/digest mode (§7.3) — and keeps this signature.

P9a-2 also owns the HTTP path that creates a route: `TriggerBody` in routers/agent_platform.py
does not yet pass `connection_id` / `priority` to `agent_triggers.create`, so today an intake
route is created from code (tests) only. Keep this module's names: another build sits on them.

What P9a-1 ships is the trivial default: every live `agent_triggers` row of kind `intake` on the
event's connection fires once, on the trigger's own thread, under the trigger's standing grant,
with the envelope as the payload and `intake:<event id>` as the dedupe key (a replay that is not
forced can therefore never admit a second run for a route that already admitted one).

    @dataclass Fire
        trigger_id    the agent_triggers row to fire
        dedupe_key    `intake:<event id>` (reprocess) | `intake:<event id>:r<n>` (force)
        payload       what the agent receives as "What arrived" (the envelope by default)
        thread_id     None = the trigger's own thread; P9a-2 sets the thread the thread key names
        acting        None = the trigger's standing grant; P9a-2 sets the resolved person's Acting
        reason        for the outcome row ("default route", "matched rule #1", ...)
        extra         merged into the run's finalize_ctx (reply_to, intake_event_id, trace_id)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..connectors.intake.contract import IntakeConnection, IntakeEvent

#: The outcome a fire records on the event (`intake_events.outcome[]`).
FIRE_OUTCOMES = ("admitted", "skipped", "refused", "duplicate", "filtered", "deferred", "failed")


@dataclass
class Fire:
    trigger_id: str
    dedupe_key: str
    payload: Mapping[str, Any]
    thread_id: Optional[str] = None
    acting: Any = None
    reason: str = "default route"
    extra: dict[str, Any] = field(default_factory=dict)


def routes_for(connection: Connection, intake_connection_id: str, *, project_id: Optional[str]
               ) -> list[dict]:
    """The `intake` triggers of one connection, in (priority, created_at) order, live ones only
    (a paused route is still listed: `fire` records the refusal, which the console shows)."""
    rows = connection.execute(text("""
        SELECT * FROM agent_triggers
         WHERE kind = 'intake' AND connection_id = :c
           AND (CAST(:p AS TEXT) IS NULL OR project_id = CAST(:p AS TEXT))
         ORDER BY priority, created_at, id"""),
        {"c": intake_connection_id, "p": project_id}).mappings().all()
    return [dict(r) for r in rows]


def dedupe_key_for(connection: Connection, trigger_id: str, event_row_id: str, *,
                   replay_n: int = 0) -> str:
    """The fires-table dedupe key for (route, event): `intake:<id>` the first time; a FORCED
    replay gets `intake:<id>:r<n>` (runs again); a reprocess keeps `intake:<id>` when that route
    already ADMITTED a run (so the fire is a no-op duplicate) and gets `intake:<id>:a<n>` when
    every earlier firing was refused, skipped or failed (so the fixed route runs now)."""
    base = f"intake:{event_row_id}"
    if replay_n:
        return f"{base}:r{replay_n}"
    prior = [str(r[0]) for r in connection.execute(text(
        "SELECT outcome FROM agent_trigger_fires WHERE trigger_id = :t "
        "AND (dedupe_key = :k OR dedupe_key LIKE :ka) ORDER BY fired_at"),
        {"t": trigger_id, "k": base, "ka": base + ":a%"}).all()]
    if not prior or "admitted" in prior:
        return base
    return f"{base}:a{len(prior)}"


def route(connection: Connection, conn: IntakeConnection, event: IntakeEvent,
          event_row: Mapping[str, Any], *, replay_n: int = 0) -> list[Fire]:
    """Which routes fire for this event, and how. P9a-2 implements the real thing here."""
    fires: list[Fire] = []
    for trigger in routes_for(connection, conn.id, project_id=conn.project_id):
        fires.append(Fire(
            trigger_id=str(trigger["id"]),
            dedupe_key=dedupe_key_for(connection, str(trigger["id"]), str(event_row["id"]),
                                      replay_n=replay_n),
            payload=event.to_dict(), reason="default route (every intake route of the connection)",
            extra={"intake_event_id": str(event_row["id"]), "intake_connection_id": conn.id,
                   "trace_id": event_row.get("trace_id")}))
    return fires


def fire_all(connection: Connection, fires: list[Fire], *, actor: str = "intake") -> list[dict]:
    """Fire each route through the one path every trigger kind uses (agent_triggers.fire); one
    savepoint per fire so a refused admission never takes the others down."""
    from . import agent_triggers

    outcomes: list[dict] = []
    for f in fires:
        try:
            with connection.begin_nested():
                got = agent_triggers.fire(connection, f.trigger_id, actor=actor, payload=f.payload,
                                          dedupe_key=f.dedupe_key, finalize_extra=f.extra)
        except Exception as error:  # noqa: BLE001 — recorded, the event retries
            got = {"outcome": "failed", "reason": f"{type(error).__name__}: {error}"[:300]}
        outcomes.append({"trigger_id": f.trigger_id, "fire_outcome": got.get("outcome"),
                         "run_id": got.get("run_id"), "reason": got.get("reason") or f.reason,
                         "dedupe_key": f.dedupe_key})
    return outcomes


__all__ = ["FIRE_OUTCOMES", "Fire", "dedupe_key_for", "fire_all", "route", "routes_for"]
