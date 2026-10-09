"""Orders and processed webhook events for the `stripe-checkout` template, on SQLite.

ADAPT to the app's database with the tables in schema.sql. What must survive the adaptation:
  * `processed_events.event_id` is a PRIMARY KEY, inserted in the SAME transaction as the order
    change the event causes (a crash cannot apply an event twice or lose it);
  * order status only moves along TRANSITIONS (paid never goes back to pending or failed);
  * amounts are integer minor units (cents), never floats.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

TRANSITIONS = {
    "pending": {"paid", "failed", "cancelled", "needs_review"},
    "failed": {"paid", "cancelled", "needs_review"},  # a buyer may retry a declined card in the same session
    "paid": {"refunded"},
    "cancelled": {"needs_review"},  # paid after we cancelled it: a person decides
    "refunded": set(),
    "needs_review": set(),
}


class TransitionError(Exception):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class SqliteOrderStore:
    def __init__(self, path: str = ":memory:") -> None:
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._depth = 0
        self._db.executescript((Path(__file__).with_name("schema.sql")).read_text())

    @contextmanager
    def transaction(self):
        """One transaction; a nested call joins the outer one (so an event's record and the order
        change it causes commit or roll back together)."""
        with self._lock:
            if self._depth:
                self._depth += 1
                try:
                    yield self._db
                finally:
                    self._depth -= 1
                return
            self._db.execute("BEGIN IMMEDIATE")
            self._depth = 1
            try:
                yield self._db
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
            else:
                self._db.execute("COMMIT")
            finally:
                self._depth = 0

    @staticmethod
    def _row(r: Optional[sqlite3.Row]) -> Optional[dict]:
        if r is None:
            return None
        d = dict(r)
        d["items"] = json.loads(d["items"])
        return d

    def create_order(self, *, provider: str, items: list[dict], amount_cents: int, currency: str,
                     cart_id: Optional[str] = None) -> dict:
        oid = str(uuid.uuid4())
        with self.transaction() as db:
            db.execute("INSERT INTO orders (id, provider, cart_id, status, items, amount_cents, currency, created_at) "
                       "VALUES (?, ?, ?, 'pending', ?, ?, ?, ?)",
                       (oid, provider, cart_id, json.dumps(items), amount_cents, currency, _now()))
        return self.get_order(oid)  # type: ignore[return-value]

    def get_order(self, oid: str) -> Optional[dict]:
        with self._lock:
            return self._row(self._db.execute("SELECT * FROM orders WHERE id = ?", (str(oid),)).fetchone())

    def all_orders(self) -> list[dict]:
        with self._lock:
            return [self._row(r) for r in self._db.execute("SELECT * FROM orders").fetchall()]  # type: ignore[misc]

    def set_provider_ref(self, oid: str, ref: str) -> None:
        with self.transaction() as db:
            db.execute("UPDATE orders SET provider_ref = ? WHERE id = ?", (ref, oid))

    def set_payment_ref(self, oid: str, ref: str) -> None:
        with self.transaction() as db:
            db.execute("UPDATE orders SET payment_ref = ? WHERE id = ?", (ref, oid))

    def find_by_payment_ref(self, provider: str, ref: str) -> Optional[dict]:
        with self._lock:
            return self._row(self._db.execute("SELECT * FROM orders WHERE provider = ? AND payment_ref = ?",
                                              (provider, ref)).fetchone())

    def transition(self, oid: str, to: str) -> bool:
        """Move an order to `to`: True when this call changed it, False when it was already there
        (the idempotent case). Raises TransitionError on a move the state machine forbids."""
        with self.transaction() as db:
            row = db.execute("SELECT status FROM orders WHERE id = ?", (oid,)).fetchone()
            if row is None:
                raise TransitionError("no such order")
            if row["status"] == to:
                return False
            if to not in TRANSITIONS.get(row["status"], set()):
                raise TransitionError(f"order {row['status']} cannot become {to}")
            stamp = {"paid": ", paid_at = ?", "refunded": ", refunded_at = ?"}.get(to, "")
            args: tuple[Any, ...] = (to, _now(), oid) if stamp else (to, oid)
            db.execute(f"UPDATE orders SET status = ?{stamp} WHERE id = ?", args)
            return True

    def process_event_once(self, event_id: str, provider: str, type_: str, apply: Callable[[], Any]) -> dict:
        """Run `apply()` only if `event_id` was never processed, recording it in the same
        transaction; if `apply` raises, neither the record nor its changes are kept."""
        with self.transaction() as db:
            try:
                db.execute("INSERT INTO processed_events (event_id, provider, type, processed_at) VALUES (?, ?, ?, ?)",
                           (event_id, provider, type_, _now()))
            except sqlite3.IntegrityError:
                return {"duplicate": True}
            return {"duplicate": False, "result": apply()}
