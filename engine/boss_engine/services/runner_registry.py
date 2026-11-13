"""The runner registry: which coding-agent turns a boss-runner owns, and whether they are alive.

One table, `runner_sessions` (db/schema.sql, BOSSAPP-828). Written by:

* the dispatch seam (`runner_dispatch`), when it hands a turn to the runner (`open_session`);
* the runner's pushes (`POST /v1/runner/sessions/{id}/events` -> `record_push`): every push is a
  heartbeat, and the events in it advance `seq`, the API's CUMULATIVE ACK of the runner's outbox.
  Only a contiguous run is accepted (seq == acked + 1, + 2, ...): a gap stops the ack where it is
  and the runner re-sends from there, so nothing is skipped across an API restart.

Read by the RECLAIM PATH, before it declares a run dead (`live_session` / `alive`):
run_liveness.progressing and jobs.reap_lost_leases. A run whose runner pushed within
`STALE_AFTER_SECONDS` and reports its process alive is NOT dead: its job is handed back to the
queue without spending an attempt, and the stage that picks it up re-attaches to the same turn
(the runner's journal returns the live session for the same run and turn) instead of paying for
it again.
"""
from __future__ import annotations

from typing import Any, Iterable, Optional

from ..ports import sql

#: A runner pushes every 10 s (BOSS_RUNNER_PUSH_SECONDS); three missed pushes and a margin.
STALE_AFTER_SECONDS = 90
LIVE_STATES = ("starting", "running")


def open_session(connection, *, session_id: str, run_id: str, identity_run_id: str, runner_id: str,
                 session_key: str, state: str = "running") -> None:
    """Record (or re-record, on a re-attach) a turn the runner owns. A re-attach keeps the acked seq."""
    connection.execute(sql("""
        INSERT INTO runner_sessions (session_id, run_id, identity_run_id, runner_id, session_key, state, alive,
                                     last_heartbeat, created_at, updated_at)
        VALUES (:s, :r, :i, :rn, :k, :st, true, now(), now(), now())
        ON CONFLICT (session_id) DO UPDATE SET runner_id = EXCLUDED.runner_id, state = EXCLUDED.state,
            identity_run_id = EXCLUDED.identity_run_id, last_heartbeat = now(), updated_at = now()"""),
        {"s": session_id, "r": run_id, "i": identity_run_id, "rn": runner_id, "k": session_key, "st": state})


def session(connection, session_id: str) -> Optional[dict[str, Any]]:
    row = connection.execute(sql("SELECT * FROM runner_sessions WHERE session_id = :s"),
                             {"s": session_id}).mappings().first()
    return dict(row) if row else None


def contiguous_ack(acked: int, seqs: Iterable[int]) -> int:
    """Pure: the cumulative ack after receiving `seqs` on top of `acked`."""
    have = set(int(s) for s in seqs)
    while acked + 1 in have:
        acked += 1
    return acked


def record_push(connection, *, session_id: str, identity_run_id: str, runner_id: str, state: str,
                alive: bool, events: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """Apply one runner push. Returns {"ack", "state", "run_id"}, or None when the session is not one
    the API handed to this run (the caller answers 404 — a token for another run never writes)."""
    row = connection.execute(sql(
        "SELECT session_id, run_id, identity_run_id, seq, state FROM runner_sessions "
        "WHERE session_id = :s FOR UPDATE"), {"s": session_id}).mappings().first()
    if row is None or row["identity_run_id"] != identity_run_id:
        return None
    ack = contiguous_ack(int(row["seq"] or 0), (e.get("seq") for e in events if isinstance(e, dict)
                                                 and isinstance(e.get("seq"), int)))
    exited = next((e for e in events if isinstance(e, dict) and e.get("type") == "exited"
                   and int(e.get("seq", 0)) <= ack), None)
    new_state = "exited" if exited is not None else (state if state in (*LIVE_STATES, "exited", "lost") else row["state"])
    if exited is not None and (exited.get("data") or {}).get("lost"):
        new_state = "lost"
    connection.execute(sql(
        "UPDATE runner_sessions SET seq = :q, state = :st, alive = :a, runner_id = :rn, last_heartbeat = now(), "
        "updated_at = now() WHERE session_id = :s"),
        {"q": ack, "st": new_state, "a": bool(alive) and new_state in LIVE_STATES, "rn": runner_id,
         "s": session_id})
    return {"ack": ack, "state": new_state, "run_id": row["run_id"]}


def close_session(connection, session_id: str, *, state: str = "consumed") -> None:
    connection.execute(sql("UPDATE runner_sessions SET state = :st, alive = false, updated_at = now() "
                           "WHERE session_id = :s"), {"st": state, "s": session_id})


def live_session(connection, run_id: str, *, stale_after: int = STALE_AFTER_SECONDS) -> Optional[dict[str, Any]]:
    """The turn of code-task run `run_id` a runner still owns and reports alive, or None.

    Alive = a live state, the runner's last push said the process runs, and that push is recent."""
    query = sql("""
        SELECT * FROM runner_sessions
        WHERE run_id = :r AND state IN ('starting', 'running') AND alive
          AND last_heartbeat > now() - make_interval(secs => :stale)
        ORDER BY last_heartbeat DESC LIMIT 1""")
    try:
        # A savepoint: a database without the table (an install not yet migrated) must not poison
        # the caller's transaction — it simply has no runner, so nothing is alive.
        with connection.begin_nested():
            row = connection.execute(query, {"r": str(run_id), "stale": int(stale_after)}).mappings().first()
    except Exception:  # noqa: BLE001
        return None
    return dict(row) if row else None


def alive(connection, run_id: str) -> bool:
    return live_session(connection, run_id) is not None
