"""The daily run on an install: what the `run_golden_journeys` tool starts and the schedule arms.

THE SCHEDULE IS BOSS'S OWN. `arm_daily` writes one `schedule_work` wake (agent_wakeups.schedule)
whose brief asks the agent to call `run_golden_journeys`; the wake's turn starts the run and
reports. No cron, nothing outside BOSS.

THE RUN IS IN THE BACKGROUND. A day's journeys are real builds (tens of minutes each), far
longer than one agent turn, so the tool starts them on a thread of this process and returns at
once; each journey's row is written the moment it is judged. One run at a time per process. A
run killed by a restart leaves its remaining journeys without a row, and the day is red — the
honest reading.

AGAINST WHAT. The install's own API (BOSS_JOURNEY_API_URL, else BOSS_CONTROL_PLANE_URL, else
http://127.0.0.1:8000), as a service account whose API key is held in BOSS's vault: the secret
reference BOSS_JOURNEY_KEY_REF names it. The key is resolved per run, held in memory, and never
printed, recorded or returned. The journeys' own values (sandbox repos, ticket connection,
staging target) come from BOSS_JOURNEY_VARS (a JSON object) and the tool's `variables`.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from typing import Any, Optional

from . import definitions as d
from . import fakes as _fakes
from . import results as _results
from . import runner as _runner

log = logging.getLogger(__name__)

#: The marker that makes the daily wake findable (one per project; arming again replaces it).
WAKE_MARKER = "[golden-journeys]"
WAKE_PROMPT = (WAKE_MARKER + " Run today's golden journeys: call `run_golden_journeys` "
               "(no arguments unless this brief gives some), then say in one line that the run "
               "started. The results land on Govern › Journeys as each journey is judged.")

_lock = threading.Lock()
_state: dict[str, Any] = {"running": False, "started_at": None, "journeys": []}


def api_url() -> str:
    return (os.environ.get("BOSS_JOURNEY_API_URL") or os.environ.get("BOSS_CONTROL_PLANE_URL")
            or "http://127.0.0.1:8000").rstrip("/")


def configured_variables() -> dict[str, Any]:
    raw = os.environ.get("BOSS_JOURNEY_VARS") or ""
    if not raw.strip():
        return {}
    try:
        value = json.loads(raw)
    except ValueError:
        log.warning("BOSS_JOURNEY_VARS is not JSON; ignored")
        return {}
    return value if isinstance(value, dict) else {}


def _key(engine: Any, settings: Any, project_id: str) -> Optional[str]:
    ref = (os.environ.get("BOSS_JOURNEY_KEY_REF") or "").strip()
    if not ref:
        return None
    from ..secret_backends import resolve_secret_ref

    with engine.begin() as c:
        return resolve_secret_ref(c, ref, settings, project_id)


def status() -> dict[str, Any]:
    with _lock:
        return dict(_state)


def start(engine: Any, settings: Any, *, project_id: str, actor: str,
          variables: Optional[dict[str, Any]] = None, only: Optional[list[str]] = None,
          client: Any = None, wait: bool = False) -> dict[str, Any]:
    """Start today's run (all journeys, or `only`). Returns at once unless `wait`."""
    journeys = d.load_all()
    if only:
        unknown = sorted(set(only) - {j.id for j in journeys})
        if unknown:
            return {"ok": False, "error": f"no journey {', '.join(unknown)}"}
    with _lock:
        if _state["running"]:
            return {"ok": False, "error": "a golden journey run is already going",
                    "started_at": _state["started_at"]}
        from datetime import datetime, timezone

        _state.update(running=True, started_at=datetime.now(timezone.utc).isoformat(),
                      journeys=[j.id for j in journeys if not only or j.id in only])
    note = ""
    if client is None:
        try:
            key = _key(engine, settings, project_id)
        except Exception as exc:  # noqa: BLE001 — said, never the value
            key, note = None, f"the journey key could not be read ({type(exc).__name__})"
        if key is None and not note:
            note = "no service-account key (BOSS_JOURNEY_KEY_REF): calls go without one"
        client = _runner.HttpClient(api_url(), key)
    values = {"project_id": project_id, **configured_variables(), **dict(variables or {})}

    def work() -> None:
        try:
            for j in journeys:
                if only and j.id not in only:
                    continue
                row = _runner.run_all([j], client, variables=values, fake_factory=_fakes.start)[0]
                try:
                    with engine.begin() as c:
                        _results.record(c, row, actor=actor)
                except Exception:  # noqa: BLE001 — one unwritable row must not stop the rest
                    log.exception("golden journey %s: the result could not be stored", j.id)
        finally:
            with _lock:
                _state.update(running=False)

    if wait:
        work()
    else:
        threading.Thread(target=work, name="golden-journeys", daemon=True).start()
    return {"ok": True, "started": _state["journeys"] if not wait else [j.id for j in journeys
                                                                         if not only or j.id in only],
            "api": api_url(), **({"note": note} if note else {})}


def arm_daily(connection: Any, *, project_id: str, actor: str, at_time: str,
              tz_name: str = "UTC", days: Optional[list[str]] = None) -> dict[str, Any]:
    """The daily wake (schedule_work's own store). Arming again stops the one before."""
    from sqlalchemy import text

    from .. import agent_wakeups

    for row in connection.execute(text(
            "SELECT id FROM agent_wakeups WHERE project_id = :p AND kind = 'schedule' "
            "AND status = 'active' AND prompt LIKE :m"),
            {"p": project_id, "m": WAKE_MARKER + "%"}).scalars():
        agent_wakeups.stop(connection, project_id=project_id, wakeup_id=row, actor=actor,
                           status="stopped", reason="replaced by a new golden-journey schedule")
    return agent_wakeups.schedule(connection, project_id=project_id, actor=actor,
                                  prompt=WAKE_PROMPT, at_time=at_time, days=days or [],
                                  tz_name=tz_name)


def daily_wake(connection: Any, project_id: str) -> Optional[dict[str, Any]]:
    from sqlalchemy import text

    row = connection.execute(text(
        "SELECT id, at_time, timezone, next_wake_at FROM agent_wakeups WHERE project_id = :p "
        "AND kind = 'schedule' AND status = 'active' AND prompt LIKE :m "
        "ORDER BY created_at DESC LIMIT 1"),
        {"p": project_id, "m": WAKE_MARKER + "%"}).mappings().first()
    if row is None:
        return None
    nxt = row["next_wake_at"]
    return {"id": row["id"], "at_time": row["at_time"], "timezone": row["timezone"],
            "next_wake_at": nxt.isoformat() if hasattr(nxt, "isoformat") else nxt}
