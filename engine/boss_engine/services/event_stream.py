"""Realtime event push with per-event authorization (BOSSAPP-835).

The console polled for everything (apps/web/lib/console-refresh.ts); events were stored and never
pushed. This is the push.

    events INSERT ──trigger──► pg_notify('boss_events', {id, project_id, event_type, ...})
                                  │  (delivered at COMMIT: a rolled-back event is never announced)
                                  ▼
    PgListener (one thread per API process, LISTEN boss_events, reconnects; a reconnect tells
                every subscriber to resync, since notifications in the gap are lost)
                                  │
                                  ▼
    Hub.publish ──interest pre-filter──► Subscription (bounded queue; overflow -> one RESYNC)
                                  │
                                  ▼
    stream(): read the ROW, check THIS VIEWER may see it (Viewer.allowed: the PDP's effective
              permissions at the event's project, for the permission the event's kind needs),
              only then write `id: / data:` to that connection.

THE AUTHORIZATION IS PER EVENT, NOT PER CONNECTION. The route's own declaration checks the viewer
may read activity where they asked (a project, or the org); every event is then checked again
against the viewer's permissions at ITS project, for ITS kind (`permission_for`: decisions need
decision:read, identity/audit events audit:read, secret events secret:read_metadata, chat
integration:read, everything else activity:read). An event the viewer may not see never reaches
their stream — not its id, not its kind. Permissions are cached per connection for a few seconds
(and the PDP's own cache is keyed by the identity epoch, so a revoked binding takes effect within
that window).

LAST-EVENT-ID REPLAY. A client reconnecting with `Last-Event-ID` gets the durable events after
that one, in (created_at, id) order, through the same filters and authorization, before anything
live. The subscription is opened BEFORE the replay query, so nothing committed during the replay is
lost (duplicates are dropped by id). An unknown anchor, or more than REPLAY_MAX behind, is a RESYNC:
the client refetches what it shows. Ordering is by insert time; an event inserted earlier but
committed later than the anchor can be missed by a replay — the console treats events as
invalidations (idempotent), and polling stays on as the fallback.
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from typing import Any, AsyncIterator, Callable, Optional

from ..ports import sql

log = logging.getLogger(__name__)

CHANNEL = "boss_events"
QUEUE_MAX = 500
REPLAY_MAX = 1000
HEARTBEAT_SECONDS = 15.0
AUTHZ_CACHE_SECONDS = 10.0
#: A sentinel queued instead of events the client must refetch (overflow, listener gap).
RESYNC = {"__resync__": True}


def permission_for(event_type: str, entity_type: str = "") -> str:
    """The catalogue permission a viewer needs to see one event of this kind."""
    et = event_type or ""
    if et.startswith(("identity.", "audit.", "auth.", "security.")):
        return "audit:read"
    if et.startswith("secret.") or entity_type == "secret_ref":
        return "secret:read_metadata"
    if et.startswith("decision.") or entity_type == "decision":
        return "decision:read"
    if et.startswith("chat."):
        return "integration:read"
    return "activity:read"


@dataclass(frozen=True)
class Interest:
    """What a connection asked for: event kinds (exact, or a prefix ending in '.' or '*') and
    projects. Empty means everything the viewer may see."""
    kinds: tuple[str, ...] = ()
    project_ids: frozenset[str] = frozenset()

    @classmethod
    def parse(cls, kinds: Optional[str], project_id: Optional[str]) -> "Interest":
        ks = tuple(sorted({k.strip() for k in (kinds or "").split(",") if k.strip()}))[:50]
        return cls(ks, frozenset({project_id}) if project_id else frozenset())

    def _kind_ok(self, event_type: str) -> bool:
        if not self.kinds:
            return True
        for k in self.kinds:
            if k.endswith("*"):
                if event_type.startswith(k[:-1]):
                    return True
            elif k.endswith("."):
                if event_type.startswith(k):
                    return True
            elif event_type == k or event_type.startswith(k + "."):
                return True
        return False

    def matches(self, note: dict) -> bool:
        if self.project_ids and str(note.get("project_id") or "") not in self.project_ids:
            return False
        return self._kind_ok(str(note.get("event_type") or ""))

    def sql_filter(self) -> tuple[str, dict]:
        clauses, params = [], {}
        if self.project_ids:
            clauses.append("project_id = ANY(:pids)")
            params["pids"] = sorted(self.project_ids)
        if self.kinds:
            exact = [k for k in self.kinds if not k.endswith(("*", "."))]
            prefixes = [(k[:-1] if k.endswith("*") else k) + "%" for k in self.kinds
                        if k.endswith(("*", "."))]
            prefixes += [k + ".%" for k in exact]
            parts = []
            if exact:
                parts.append("event_type = ANY(:exact)")
                params["exact"] = exact
            if prefixes:
                parts.append("event_type LIKE ANY(:prefixes)")
                params["prefixes"] = prefixes
            clauses.append("(" + " OR ".join(parts) + ")")
        return (" AND " + " AND ".join(clauses)) if clauses else "", params


# --------------------------------------------------------------------------------------------- #
# Viewers: who is reading, and may they see this event
# --------------------------------------------------------------------------------------------- #
class _Cached:
    def __init__(self, ttl: float, clock: Callable[[], float]):
        self.ttl, self.clock, self.data = ttl, clock, {}

    def get(self, key: Any, compute: Callable[[], Any]) -> Any:
        hit = self.data.get(key)
        now = self.clock()
        if hit is not None and now - hit[0] < self.ttl:
            return hit[1]
        value = compute()
        self.data[key] = (now, value)
        return value


class IdentityViewer:
    """A verified principal: the PDP's effective permissions at each event's project."""

    def __init__(self, ctx: Any, engine: Any, *, ttl: float = AUTHZ_CACHE_SECONDS,
                 clock: Callable[[], float] = time.monotonic):
        self.ctx, self.engine = ctx, engine
        self._cache = _Cached(ttl, clock)

    def _held(self, project_id: str) -> frozenset[str]:
        from ..identity import authz
        from ..identity.scope import ResolvedScope

        org = self.ctx.principal.org_id
        try:
            with self.engine.connect() as conn:
                return authz.effective_permissions(
                    self.ctx, ResolvedScope.in_project(org, project_id), db=conn).permissions
        except Exception:  # noqa: BLE001 — cannot tell: nothing is shown
            log.warning("event stream authz failed for %s", project_id, exc_info=True)
            return frozenset()

    def allowed(self, project_id: str, permission: str) -> bool:
        if not project_id:
            return False
        return permission in self._cache.get(project_id, lambda: self._held(project_id))


class LegacyViewer:
    """No identity context (shadow/off with legacy keys): the members rule require_project_reader
    applies per event's project. Transport auth off (local development) sees everything, exactly
    as every other read does there."""

    def __init__(self, principal: Optional[str], transport_authenticated: bool, engine: Any, *,
                 ttl: float = AUTHZ_CACHE_SECONDS, clock: Callable[[], float] = time.monotonic,
                 settings: Any = None):
        self.principal, self.transport, self.engine = principal, transport_authenticated, engine
        self.settings = settings
        self._cache = _Cached(ttl, clock)

    def _member(self, project_id: str) -> bool:
        from ..config import get_settings
        from .members import MemberDirectoryService

        try:
            with self.engine.connect() as conn:
                MemberDirectoryService(self.settings or get_settings()).require_member(
                    conn, project_id, self.principal)
            return True
        except Exception:  # noqa: BLE001 — not a member, or cannot tell: not shown
            return False

    def allowed(self, project_id: str, permission: str) -> bool:
        if not project_id:
            return False
        if not self.transport:
            return True
        if not self.principal:
            return False
        return self._cache.get(project_id, lambda: self._member(project_id))


# --------------------------------------------------------------------------------------------- #
# The hub and its subscriptions
# --------------------------------------------------------------------------------------------- #
class Subscription:
    """One connection's bounded queue. Fed from the listener thread through its loop; on overflow
    the queue is replaced by ONE resync marker (the client refetches), never grown."""

    def __init__(self, interest: Interest, loop: Optional[asyncio.AbstractEventLoop],
                 maxlen: int = QUEUE_MAX):
        self.interest, self.loop, self.maxlen = interest, loop, maxlen
        self.items: deque = deque()
        self.overflowed = 0
        self._wake = asyncio.Event() if loop is not None else None
        self._lock = threading.Lock()

    def offer(self, note: dict) -> None:
        if self.loop is not None:
            try:
                self.loop.call_soon_threadsafe(self._put, note)
            except RuntimeError:   # the loop closed: the connection is gone
                pass
        else:
            self._put(note)

    def _put(self, note: dict) -> None:
        with self._lock:
            if note is RESYNC:
                self.items.clear()
                self.items.append(RESYNC)
            elif len(self.items) >= self.maxlen:
                self.overflowed += 1
                self.items.clear()
                self.items.append(RESYNC)
            else:
                self.items.append(note)
        if self._wake is not None:
            self._wake.set()

    def pop(self) -> Optional[dict]:
        with self._lock:
            return self.items.popleft() if self.items else None

    async def next(self, timeout: float) -> Optional[dict]:
        item = self.pop()
        if item is not None:
            return item
        assert self._wake is not None
        self._wake.clear()
        item = self.pop()
        if item is not None:
            return item
        try:
            await asyncio.wait_for(self._wake.wait(), timeout)
        except asyncio.TimeoutError:
            return None
        return self.pop()


class Hub:
    def __init__(self) -> None:
        self._subs: set[Subscription] = set()
        self._lock = threading.Lock()

    def subscribe(self, interest: Interest, loop: Optional[asyncio.AbstractEventLoop] = None,
                  maxlen: int = QUEUE_MAX) -> Subscription:
        sub = Subscription(interest, loop, maxlen)
        with self._lock:
            self._subs.add(sub)
        return sub

    def unsubscribe(self, sub: Subscription) -> None:
        with self._lock:
            self._subs.discard(sub)

    def count(self) -> int:
        with self._lock:
            return len(self._subs)

    def publish(self, note: dict) -> None:
        with self._lock:
            subs = list(self._subs)
        for sub in subs:
            if sub.interest.matches(note):
                sub.offer(note)

    def resync_all(self) -> None:
        with self._lock:
            subs = list(self._subs)
        for sub in subs:
            sub.offer(RESYNC)


class PgListener(threading.Thread):
    """LISTEN boss_events on its own autocommit connection; publish each notification. A dropped
    connection is reopened with backoff, and every subscriber is told to resync (notifications
    sent while it was down are gone)."""

    def __init__(self, dsn: str, hub: Hub, *, channel: str = CHANNEL, poll: float = 1.0):
        super().__init__(name="boss-event-listener", daemon=True)
        self.dsn, self.hub, self.channel, self.poll = dsn, hub, channel, poll
        self.ready = threading.Event()
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        import psycopg

        backoff = 1.0
        first = True
        while not self._stop.is_set():
            try:
                with psycopg.connect(self.dsn, autocommit=True) as conn:
                    conn.execute(f"LISTEN {self.channel}")
                    if not first:
                        self.hub.resync_all()
                    first = False
                    backoff = 1.0
                    self.ready.set()
                    while not self._stop.is_set():
                        for n in conn.notifies(timeout=self.poll, stop_after=None):
                            try:
                                note = json.loads(n.payload)
                            except ValueError:
                                continue
                            if isinstance(note, dict):
                                self.hub.publish(note)
                            if self._stop.is_set():
                                break
            except Exception:  # noqa: BLE001 — reconnect; never take the API down
                log.warning("event listener lost its connection; reconnecting", exc_info=True)
                self.ready.clear()
                self._stop.wait(backoff)
                backoff = min(backoff * 2, 30.0)


_HUB: Optional[Hub] = None
_LISTENER: Optional[PgListener] = None
_HUB_LOCK = threading.Lock()


def libpq_dsn(engine: Any) -> str:
    url = engine.url.set(drivername="postgresql")
    return url.render_as_string(hide_password=False)


def get_hub(engine: Any) -> Hub:
    """The process's hub, its listener started on first use."""
    global _HUB, _LISTENER
    with _HUB_LOCK:
        if _HUB is None:
            _HUB = Hub()
        if _LISTENER is None or not _LISTENER.is_alive():
            _LISTENER = PgListener(libpq_dsn(engine), _HUB)
            _LISTENER.start()
        return _HUB


# --------------------------------------------------------------------------------------------- #
# Reads
# --------------------------------------------------------------------------------------------- #
_COLUMNS = "id, project_id, event_type, actor, entity_type, entity_id, data, created_at"


def _row_out(row: Any) -> dict:
    d = dict(row)
    data = d.get("data")
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except ValueError:
            data = {}
    created = d.get("created_at")
    return {"id": d["id"], "project_id": d["project_id"], "event_type": d["event_type"],
            "actor": d["actor"], "entity_type": d["entity_type"], "entity_id": d["entity_id"],
            "data": data if isinstance(data, dict) else {},
            "created_at": created.isoformat() if isinstance(created, datetime) else created}


def fetch_event(engine: Any, event_id: str) -> Optional[dict]:
    with engine.connect() as conn:
        row = conn.execute(sql(f"SELECT {_COLUMNS} FROM events WHERE id = :i"),
                           {"i": event_id}).mappings().first()
    return _row_out(row) if row else None


def replay(engine: Any, last_event_id: str, interest: Interest,
           limit: int = REPLAY_MAX) -> Optional[list[dict]]:
    """The events after `last_event_id` that match `interest`, oldest first, at most limit + 1
    (one more than the cap says "too far behind"). None when the anchor is unknown."""
    where, params = interest.sql_filter()
    with engine.connect() as conn:
        anchor = conn.execute(sql("SELECT created_at, id FROM events WHERE id = :i"),
                              {"i": last_event_id}).mappings().first()
        if anchor is None:
            return None
        rows = conn.execute(sql(
            f"SELECT {_COLUMNS} FROM events WHERE (created_at, id) > (:c, :i)" + where
            + " ORDER BY created_at, id LIMIT :n"),
            {"c": anchor["created_at"], "i": anchor["id"], "n": limit + 1, **params}
        ).mappings().all()
    return [_row_out(r) for r in rows]


def sse(event: dict) -> str:
    body = json.dumps(event, default=str, separators=(",", ":"))
    return f"id: {event['id']}\ndata: {body}\n\n"


def sse_resync(reason: str) -> str:
    return f"event: resync\ndata: {json.dumps({'reason': reason})}\n\n"


async def stream(engine: Any, hub: Hub, viewer: Any, interest: Interest, *,
                 last_event_id: Optional[str] = None, max_seconds: float = 600.0,
                 heartbeat: float = HEARTBEAT_SECONDS, queue_max: int = QUEUE_MAX,
                 to_thread: Optional[Callable[..., Any]] = None) -> AsyncIterator[str]:
    """The SSE body for one connection. Every event written has passed `interest` and
    `viewer.allowed` for its own project and kind."""
    run = to_thread or asyncio.to_thread
    loop = asyncio.get_running_loop()
    sub = hub.subscribe(interest, loop, queue_max)
    seen: set[str] = set()

    async def visible(ev: dict) -> bool:
        if not interest.matches(ev):
            return False
        return bool(await run(viewer.allowed, ev["project_id"],
                              permission_for(ev["event_type"], ev.get("entity_type") or "")))

    try:
        yield "retry: 3000\n: connected\n\n"
        if last_event_id:
            rows = await run(replay, engine, last_event_id, interest)
            if rows is None:
                yield sse_resync("unknown_last_event_id")
            else:
                for ev in rows[:REPLAY_MAX]:
                    seen.add(ev["id"])
                    if await visible(ev):
                        yield sse(ev)
                if len(rows) > REPLAY_MAX:
                    yield sse_resync("too_far_behind")
        deadline = loop.time() + max_seconds
        while loop.time() < deadline:
            item = await sub.next(min(heartbeat, max(0.05, deadline - loop.time())))
            if item is None:
                yield ": ping\n\n"
                continue
            if item is RESYNC:
                seen.clear()
                yield sse_resync("overflow" if sub.overflowed else "gap")
                continue
            event_id = str(item.get("id") or "")
            if not event_id or event_id in seen:
                continue
            seen.add(event_id)
            if len(seen) > 4 * queue_max:
                seen = set(list(seen)[-queue_max:])
            ev = await run(fetch_event, engine, event_id)
            if ev is not None and await visible(ev):
                yield sse(ev)
    finally:
        hub.unsubscribe(sub)


__all__ = ["CHANNEL", "Hub", "IdentityViewer", "Interest", "LegacyViewer", "PgListener",
           "QUEUE_MAX", "REPLAY_MAX", "RESYNC", "Subscription", "fetch_event", "get_hub",
           "libpq_dsn", "permission_for", "replay", "sse", "stream"]
