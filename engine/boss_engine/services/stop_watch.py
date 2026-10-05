"""STOP KILLS (stability spec §4a.3).

Cancel used to be cooperative: `cancel_run` wrote a `code_task_stop_requests` row and the
executor honoured it at its next stage boundary — after the agent in flight had finished its
turn, which could be twenty minutes and several dollars later. Shepherd's "cancel" changed rows
only. Nothing signalled a process.

This watch runs on its own thread in the API — the process the builds run in — every two
seconds. For every build that still has live processes here, it asks the database whether that
build was stopped (a stop request, or a status that ended it), and if so ends the build's whole
tree through its owner (`owned_process.terminate_owner`): agents, review seats, commands, the
serve proof. The executor then sees its stage die, recognises the stop
(`owned_process.was_cancelled`) and finishes it as a cancel, not a failure.

Guarantee: Stop pressed → zero processes of that build within 10 s (2 s poll + 5 s grace + the
verify).
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Optional

from sqlalchemy import text

from . import owned_process

_log = logging.getLogger("boss.stop_watch")

POLL_SECONDS = 2.0
#: Statuses that mean "this build must not be running". `completed` is not among them: a run
#: is marked completed by its own executor, which ends its tree itself on the way out.
_ENDED = ("cancelled", "failed", "superseded", "simulated")


def stopped_runs(connection: Any, run_ids: list[str]) -> list[str]:
    if not run_ids:
        return []
    rows = connection.execute(text(
        "SELECT id FROM code_task_runs WHERE id = ANY(:ids) AND status = ANY(:ended) "
        # An honoured request has already become the run's `cancelled` status; reading only
        # pending ones keeps a later attempt of the same run id from being stopped by an old one.
        "UNION SELECT run_id FROM code_task_stop_requests WHERE run_id = ANY(:ids) "
        "AND honoured_at IS NULL"),
        {"ids": list(run_ids), "ended": list(_ENDED)}).all()
    return sorted({str(r[0]) for r in rows})


def tick(engine: Any) -> list[dict[str, Any]]:
    """One pass. Returns what it ended (for tests and the log)."""
    live = sorted(owned_process.live_owner_ids(kind="code_run"))
    if not live:
        return []
    try:
        with engine.begin() as c:
            stopped = stopped_runs(c, live)
    except Exception:  # noqa: BLE001 — a DB blip must not kill the watch
        _log.debug("stop watch query failed", exc_info=True)
        return []
    ended = []
    for run_id in stopped:
        result = owned_process.terminate_owner(run_id, reason="cancel")
        if result["ended"]:
            _log.warning("stop watch ended %d process tree(s) of stopped build %s",
                         result["ended"], run_id)
        ended.append(result)
    return ended


_THREAD: Optional[threading.Thread] = None


def start(engine: Any, *, interval: float = POLL_SECONDS) -> threading.Thread:
    global _THREAD
    if _THREAD is not None and _THREAD.is_alive():
        return _THREAD

    def _loop() -> None:
        while True:
            time.sleep(interval)
            try:
                tick(engine)
            except Exception:  # noqa: BLE001
                _log.debug("stop watch tick failed", exc_info=True)

    _THREAD = threading.Thread(target=_loop, name="boss-stop-watch", daemon=True)
    _THREAD.start()
    return _THREAD
