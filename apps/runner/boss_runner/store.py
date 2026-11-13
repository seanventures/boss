"""The runner's durable state: sessions, the sequenced outbox, and the command journal.

One sqlite file in the runner's volume (`BOSS_RUNNER_STATE_DIR/runner.db`), WAL mode, one
connection behind a lock (the runner is a handful of threads, never a hot path).

OUTBOX. Every turn event (started, stdout, stderr, steered, exited, ...) is appended with the
session's next sequence number, in the same transaction that advances it, so seqs are gapless per
session. The API answers each push with a CUMULATIVE ack ("I hold everything up to n"); rows up to
the ack are deleted, everything after it is re-sent on the next push — after an API restart the
stream resumes from exactly the acked seq. An ack never moves backwards and never past what exists.

JOURNAL. Every command the API sends carries a `command_id`. The first delivery is recorded
`pending`, applied, then recorded `applied` with its result; a second delivery of the same id
returns that recorded result and applies nothing. A command still `pending` at boot (the runner
died between receiving and applying it) is REPLAYED by `Runner.replay_journal`.

No secret is written here: the run tokens live in the runner's memory only.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sessions (
    session_id   TEXT PRIMARY KEY,
    run_id       TEXT NOT NULL,          -- the identity run (the token's boss_task.run)
    task_run_id  TEXT,                   -- the code-task run (crun_...) the API reclaims by
    session_key  TEXT NOT NULL,          -- what makes a re-sent start the SAME turn
    state        TEXT NOT NULL,          -- starting | running | exited | lost | failed
    boot_id      TEXT NOT NULL,          -- the runner boot that owns the process
    pid          INTEGER,
    pgid         INTEGER,
    cwd          TEXT,
    argv         TEXT,
    collect      TEXT,                   -- JSON list of files whose content the result carries
    api_base     TEXT,                   -- the origin the API verifies DPoP `htu` against
    started_at   REAL,
    deadline     REAL,
    exit_code    INTEGER,
    timed_out    INTEGER NOT NULL DEFAULT 0,
    cancelled    INTEGER NOT NULL DEFAULT 0,
    ended_at     REAL,
    next_seq     INTEGER NOT NULL DEFAULT 1,
    acked_seq    INTEGER NOT NULL DEFAULT 0,
    consumed     INTEGER NOT NULL DEFAULT 0,
    created_at   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS sessions_by_key ON sessions (run_id, session_key, consumed);
CREATE TABLE IF NOT EXISTS outbox (
    session_id TEXT NOT NULL,
    seq        INTEGER NOT NULL,
    type       TEXT NOT NULL,
    data       TEXT NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY (session_id, seq)
);
CREATE TABLE IF NOT EXISTS journal (
    command_id  TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,
    session_id  TEXT,
    body        TEXT NOT NULL,
    status      TEXT NOT NULL,           -- pending | applied | failed
    result      TEXT,
    received_at REAL NOT NULL,
    applied_at  REAL
);
"""

LIVE_STATES = ("starting", "running")
#: Outbox payloads are capped so one chatty process cannot fill the volume; the full output is in
#: the session's log files.
MAX_EVENT_BYTES = 16 * 1024


class Store:
    def __init__(self, path: Path | str):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=FULL")
            self._db.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # -- plumbing -------------------------------------------------------------------------------

    def _tx(self) -> "_Tx":
        return _Tx(self)

    def meta(self, key: str, default: Optional[str] = None) -> Optional[str]:
        with self._lock:
            row = self._db.execute("SELECT v FROM meta WHERE k = ?", (key,)).fetchone()
        return row["v"] if row else default

    def set_meta(self, key: str, value: str) -> None:
        with self._lock:
            self._db.execute("INSERT INTO meta (k, v) VALUES (?, ?) ON CONFLICT (k) DO UPDATE SET v = excluded.v",
                             (key, value))

    # -- sessions -------------------------------------------------------------------------------

    def create_session(self, *, session_id: str, run_id: str, task_run_id: Optional[str], session_key: str,
                       boot_id: str, cwd: str, argv: list[str], collect: list[str], api_base: str,
                       deadline: Optional[float]) -> None:
        now = time.time()
        with self._tx() as db:
            db.execute(
                "INSERT INTO sessions (session_id, run_id, task_run_id, session_key, state, boot_id, cwd, argv, "
                "collect, api_base, deadline, created_at) VALUES (?, ?, ?, ?, 'starting', ?, ?, ?, ?, ?, ?, ?)",
                (session_id, run_id, task_run_id, session_key, boot_id, cwd, json.dumps(argv), json.dumps(collect),
                 api_base, deadline, now))

    def session(self, session_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            row = self._db.execute("SELECT * FROM sessions WHERE session_id = ?", (session_id,)).fetchone()
        return dict(row) if row else None

    def unconsumed_for(self, run_id: str, session_key: str) -> Optional[dict[str, Any]]:
        """The turn a re-sent start re-attaches to: same run, same key, result not yet taken.
        A `lost`/`failed` session (its process is gone without a result) is never adopted."""
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM sessions WHERE run_id = ? AND session_key = ? AND consumed = 0 "
                "AND state IN ('starting', 'running', 'exited') ORDER BY created_at DESC LIMIT 1",
                (run_id, session_key)).fetchone()
        return dict(row) if row else None

    def unconsumed_of_run(self, run_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM sessions WHERE run_id = ? AND consumed = 0 AND state IN ('starting', 'running')",
                (run_id,)).fetchall()
        return [dict(r) for r in rows]

    def sessions(self, *, states: Optional[tuple[str, ...]] = None) -> list[dict[str, Any]]:
        with self._lock:
            if states:
                marks = ",".join("?" * len(states))
                rows = self._db.execute(f"SELECT * FROM sessions WHERE state IN ({marks})", states).fetchall()
            else:
                rows = self._db.execute("SELECT * FROM sessions").fetchall()
        return [dict(r) for r in rows]

    def update_session(self, session_id: str, **fields: Any) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k} = ?" for k in fields)
        with self._tx() as db:
            db.execute(f"UPDATE sessions SET {cols} WHERE session_id = ?", (*fields.values(), session_id))

    # -- outbox ---------------------------------------------------------------------------------

    def append(self, session_id: str, type_: str, data: Any) -> int:
        """Append one event; returns its seq. Gapless per session (seq and next_seq move together)."""
        payload = json.dumps(data, default=str)
        if len(payload.encode("utf-8")) > MAX_EVENT_BYTES:
            payload = json.dumps({"truncated": True, "preview": str(data)[: MAX_EVENT_BYTES // 2]})
        with self._tx() as db:
            row = db.execute("SELECT next_seq FROM sessions WHERE session_id = ?", (session_id,)).fetchone()
            if row is None:
                raise KeyError(session_id)
            seq = int(row["next_seq"])
            db.execute("INSERT INTO outbox (session_id, seq, type, data, created_at) VALUES (?, ?, ?, ?, ?)",
                       (session_id, seq, type_, payload, time.time()))
            db.execute("UPDATE sessions SET next_seq = ? WHERE session_id = ?", (seq + 1, session_id))
        return seq

    def pending(self, session_id: str, *, limit: int = 200) -> list[dict[str, Any]]:
        """Events after the ack, in order: exactly what the next push re-sends."""
        with self._lock:
            rows = self._db.execute(
                "SELECT o.seq, o.type, o.data FROM outbox o JOIN sessions s ON s.session_id = o.session_id "
                "WHERE o.session_id = ? AND o.seq > s.acked_seq ORDER BY o.seq LIMIT ?",
                (session_id, limit)).fetchall()
        return [{"seq": r["seq"], "type": r["type"], "data": json.loads(r["data"])} for r in rows]

    def ack(self, session_id: str, upto: int) -> int:
        """Cumulative ACK: everything <= `upto` is delivered. Never backwards, never past the last
        appended seq. Returns the session's acked seq after the call."""
        with self._tx() as db:
            row = db.execute("SELECT next_seq, acked_seq FROM sessions WHERE session_id = ?",
                             (session_id,)).fetchone()
            if row is None:
                raise KeyError(session_id)
            last = int(row["next_seq"]) - 1
            acked = max(int(row["acked_seq"]), min(int(upto), last))
            db.execute("UPDATE sessions SET acked_seq = ? WHERE session_id = ?", (acked, session_id))
            db.execute("DELETE FROM outbox WHERE session_id = ? AND seq <= ?", (session_id, acked))
        return acked

    # -- journal --------------------------------------------------------------------------------

    def journal_get(self, command_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            row = self._db.execute("SELECT * FROM journal WHERE command_id = ?", (command_id,)).fetchone()
        if row is None:
            return None
        out = dict(row)
        out["body"] = json.loads(out["body"])
        out["result"] = json.loads(out["result"]) if out["result"] else None
        return out

    def journal_begin(self, command_id: str, kind: str, body: dict[str, Any],
                      session_id: Optional[str] = None) -> bool:
        """Record a command as received. False when this id was seen before (do not apply)."""
        with self._tx() as db:
            cur = db.execute(
                "INSERT INTO journal (command_id, kind, session_id, body, status, received_at) "
                "VALUES (?, ?, ?, ?, 'pending', ?) ON CONFLICT (command_id) DO NOTHING",
                (command_id, kind, session_id, json.dumps(body, default=str), time.time()))
            return cur.rowcount == 1

    def journal_finish(self, command_id: str, result: dict[str, Any], *, status: str = "applied",
                       session_id: Optional[str] = None) -> None:
        with self._tx() as db:
            db.execute("UPDATE journal SET status = ?, result = ?, applied_at = ?, "
                       "session_id = COALESCE(?, session_id) WHERE command_id = ?",
                       (status, json.dumps(result, default=str), time.time(), session_id, command_id))

    def journal_pending(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute("SELECT command_id FROM journal WHERE status = 'pending' "
                                    "ORDER BY received_at").fetchall()
        return [j for j in (self.journal_get(r["command_id"]) for r in rows) if j is not None]


class _Tx:
    def __init__(self, store: Store):
        self.store = store

    def __enter__(self) -> sqlite3.Connection:
        self.store._lock.acquire()
        self.store._db.execute("BEGIN IMMEDIATE")
        return self.store._db

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            self.store._db.execute("ROLLBACK" if exc_type else "COMMIT")
        finally:
            self.store._lock.release()
