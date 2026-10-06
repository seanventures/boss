"""The identity epoch: one number that moves on ANY identity change, and a push channel for it
(IDENTITY.md §6.6 "identity_epoch"; the console refetches /me/permissions when it moves). Owned by
stream S5.

  - `install(db)` puts a statement-level trigger on every identity table whose change can alter
    what someone may do or see about themselves (people, credentials that grant, groups, roles,
    bindings, seats, service accounts and keys, Passports, ceilings, grants, providers, policy).
    Each fires once per statement: it bumps identity_settings.epoch IN THE SAME TRANSACTION and
    sends `NOTIFY identity_epoch`. High-churn bookkeeping columns (last_used_at, sign_count, TOTP
    steps, session last_seen) are excluded, so a request never moves the epoch. A bump that rolls
    back never happened; a missed one costs a refetch, never a stale grant (S2 also re-checks).
    S2's explicit authz.bump_epoch keeps working on top (a double bump is harmless).
  - `current_epoch(org, db)` is what GET /v1/identity/me returns as `epoch` (S1 reads it) and
    GET /v1/identity/epoch answers.
  - `FEED` is the push: one LISTEN connection per API process, fanned out to every open
    GET /v1/identity/epoch/stream (server-sent events) without a thread or connection per tab.

The function and triggers are in db/schema.sql (lead's amendment at integration), generated from
WATCHED; `install` stays as an idempotent repair for a database whose schema predates them.
engine/tests/test_identity_integration_wiring.py pins the two to each other.
"""
from __future__ import annotations

import asyncio
import json
import threading
import time
from typing import Any, AsyncIterator, Callable, Optional

CHANNEL = "identity_epoch"
FUNCTION = "identity_epoch_bump"

#: table -> the UPDATE columns that matter (None = every update). INSERT and DELETE always count.
WATCHED: dict[str, Optional[tuple[str, ...]]] = {
    "identity_principals": ("status", "kind", "display_name", "network_bound",
                            "legacy_member_ids", "org_id"),
    "identity_users": ("email", "mfa_required", "principal_id"),
    "identity_user_identities": ("principal_id", "issuer", "subject"),
    "identity_providers": None,
    "identity_provider_group_mappings": None,
    "identity_totp": ("principal_id", "vault_item_id"),
    "identity_webauthn_credentials": ("principal_id", "name"),
    "identity_groups": None,
    "identity_group_members": None,
    "identity_access_roles": None,
    "identity_access_role_permissions": None,
    "identity_role_bindings": None,
    "identity_approval_seats": None,
    "identity_service_accounts": None,
    "identity_api_credentials": ("principal_id", "scopes", "project_ids", "cidrs", "expires_at",
                                 "grace_until", "suspended_at", "revoked_at"),
    "identity_agent_passports": None,
    "identity_agent_ceilings": None,
    "identity_delegation_grants": ("status", "expires_at", "revoked_at", "agent_ids",
                                   "project_ids", "resources", "allow_subdelegation",
                                   "max_depth"),
    "identity_delegation_grant_permissions": None,
    "identity_settings": ("policy",),
}

_FUNCTION_SQL = f"""
CREATE OR REPLACE FUNCTION {FUNCTION}() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE v BIGINT;
BEGIN
  UPDATE identity_settings SET epoch = epoch + 1;
  SELECT max(epoch) INTO v FROM identity_settings;
  PERFORM pg_notify('{CHANNEL}', coalesce(v, 0)::text);
  RETURN NULL;
END $$"""


def _sql(statement: str):
    from sqlalchemy import text
    return text(statement)


def install(db: Any) -> int:
    """Create the bump function and any missing trigger. Idempotent; returns triggers created."""
    db.execute(_sql(_FUNCTION_SQL))
    created = 0
    for table, columns in WATCHED.items():
        name = f"identity_epoch_{table.removeprefix('identity_')}"
        exists = db.execute(_sql("SELECT 1 FROM pg_trigger WHERE tgname = :n AND tgrelid = "
                                 "CAST(:t AS regclass)"), {"n": name, "t": table}).first()
        if exists:
            continue
        update = "UPDATE" + (f" OF {', '.join(columns)}" if columns else "")
        db.execute(_sql(f"CREATE TRIGGER {name} AFTER INSERT OR DELETE OR {update} ON {table} "
                        f"FOR EACH STATEMENT EXECUTE FUNCTION {FUNCTION}()"))
        created += 1
    return created


def current_epoch(org_id: str, *, db: Any) -> int:
    row = db.execute(_sql("SELECT epoch FROM identity_settings WHERE org_id = :o"),
                     {"o": org_id}).first()
    return int(row[0]) if row else 0


# ---- the push channel ---------------------------------------------------------------------------

class EpochFeed:
    """One LISTEN per process; subscribers are asyncio events, woken from the listener thread.
    It also re-reads the epoch every `poll_seconds`, so a lost notification (a reconnect, a
    restart of Postgres) delays a refresh by seconds at most."""

    def __init__(self, *, connect: Optional[Callable[[], Any]] = None, poll_seconds: float = 5.0):
        self._connect = connect
        self.poll_seconds = poll_seconds
        self.latest: Optional[int] = None
        self._lock = threading.Lock()
        self._subscribers: set[tuple[asyncio.AbstractEventLoop, asyncio.Event]] = set()
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    # -- the listener thread --
    def _open(self) -> Any:
        if self._connect is not None:
            return self._connect()
        # its own connection, outside the pool: a LISTEN holds it for the process's life
        import psycopg

        from ..db import get_engine
        url = get_engine().url.set(drivername="postgresql").render_as_string(hide_password=False)
        return psycopg.connect(url, autocommit=True)

    def _read(self, conn: Any) -> int:
        row = conn.execute("SELECT coalesce(max(epoch), 0) FROM identity_settings").fetchone()
        return int(row[0])

    def publish(self, epoch: int) -> None:
        with self._lock:
            if self.latest is not None and epoch <= self.latest:
                return
            self.latest = epoch
            subscribers = list(self._subscribers)
        for loop, event in subscribers:
            try:
                loop.call_soon_threadsafe(event.set)
            except RuntimeError:   # its loop is gone
                with self._lock:
                    self._subscribers.discard((loop, event))

    def _run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            conn = None
            try:
                conn = self._open()
                conn.execute(f"LISTEN {CHANNEL}")
                self.publish(self._read(conn))
                backoff = 1.0
                while not self._stop.is_set():
                    for note in conn.notifies(timeout=self.poll_seconds, stop_after=1):
                        try:
                            self.publish(int(note.payload))
                        except ValueError:
                            pass
                    self.publish(self._read(conn))
            except Exception:  # noqa: BLE001 — reconnect; the poll covers the gap
                self._stop.wait(backoff)
                backoff = min(backoff * 2, 30.0)
            finally:
                try:
                    if conn is not None:
                        conn.close()
                except Exception:  # noqa: BLE001
                    pass

    def start(self) -> bool:
        if self._thread is not None and self._thread.is_alive():
            return False
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="identity-epoch-feed",
                                        daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()

    # -- subscribers --
    async def changes(self, *, last: Optional[int] = None, heartbeat: float = 25.0,
                      is_disconnected: Optional[Callable[[], Any]] = None
                      ) -> AsyncIterator[Optional[int]]:
        """Yield the epoch whenever it moves past `last` (first value at once), and None as a
        heartbeat when nothing happened for `heartbeat` seconds. Ends when the client goes."""
        loop = asyncio.get_running_loop()
        event = asyncio.Event()
        entry = (loop, event)
        with self._lock:
            self._subscribers.add(entry)
        try:
            while True:
                if is_disconnected is not None and await is_disconnected():
                    return
                current = self.latest
                if current is not None and current != last:
                    last = current
                    yield current
                    continue
                event.clear()
                try:
                    await asyncio.wait_for(event.wait(), timeout=heartbeat)
                except asyncio.TimeoutError:
                    yield None
        finally:
            with self._lock:
                self._subscribers.discard(entry)


FEED = EpochFeed()


def sse(epoch: Optional[int]) -> str:
    """One server-sent event (or a comment line as a heartbeat that keeps proxies open)."""
    if epoch is None:
        return f": keep-alive {int(time.time())}\n\n"
    return f"event: epoch\ndata: {json.dumps({'epoch': epoch})}\n\n"
