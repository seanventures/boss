"""The daily run on an install: what the schedule's wake and Govern's "Run now" start.

THE SCHEDULE IS BOSS'S OWN, AND SPENDS NO MODEL TURN. `arm_daily` writes ONE install-wide
`schedule_work` row (agent_wakeups, kind `schedule`) marked WAKE_MARKER; when it is due,
agent_wakeups.wake_one sees the marker and starts the run here directly instead of opening an
agent turn. No chat turn can start a run: there is no agent tool for it.

ONE FIXTURE PROJECT, OPERATOR-SET VALUES. Journeys always run in BOSS_JOURNEY_PROJECT (refused
when unset) with the values in BOSS_JOURNEY_VARS (sandbox repos, ticket connection, staging
target), both set by the operator in the install's environment. Nobody calling the API or an
agent can supply a project, a repository or a target: the reserved keys are set here, last.

A NARROW ACCOUNT, NEVER ANONYMOUS. The run calls the install's own API (BOSS_JOURNEY_API_URL,
else BOSS_CONTROL_PLANE_URL, else http://127.0.0.1:8000) as a service account whose API key the
vault holds (BOSS_JOURNEY_KEY_REF; refused when unset). Before anything runs, `preflight` checks
that account through the API itself and refuses a broad one:
  * it is a service account, with no role bound at org scope;
  * it reaches exactly one project, the fixture project, and not through an org-wide binding;
  * there it holds none of FORBIDDEN (production deploys, granting roles, identity providers,
    secrets management, deleting projects, budgets).
Give it a project-scoped role in the fixture project only (an operator-like custom role without
those permissions). A production promotion therefore always waits for a person (J6 asserts the
approval card and stops there).

THE RUN IS IN THE BACKGROUND, ONE AT A TIME PER INSTALL (a Postgres advisory lock), each
journey's row written the moment it is judged. A run killed by a restart leaves its remaining
journeys without a row, and the day is red — the honest reading.
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

#: The marker of the install's one daily journeys wake.
WAKE_MARKER = "[golden-journeys]"
WAKE_PROMPT = (WAKE_MARKER + " The daily golden journeys. BOSS starts them itself when this is "
               "due; results land on Govern › Journeys as each journey is judged.")
#: Keys only this module sets: a journey's values can never move the run somewhere else.
RESERVED = frozenset({"project_id", "actor", "stamp"})
#: What the journey account must not hold in the fixture project.
FORBIDDEN = frozenset({"deployment:production", "role_binding:assign", "identity_provider:manage",
                       "secret:manage", "project:delete", "budget:manage"})
#: pg_try_advisory_lock key: one run per install.
LOCK_KEY = 1284_1285

_lock = threading.Lock()
_state: dict[str, Any] = {"running": False, "started_at": None, "journeys": []}


class Refused(RuntimeError):
    """The run cannot start; the message says what to set."""


def api_url() -> str:
    return (os.environ.get("BOSS_JOURNEY_API_URL") or os.environ.get("BOSS_CONTROL_PLANE_URL")
            or "http://127.0.0.1:8000").rstrip("/")


def fixture_project() -> str:
    return (os.environ.get("BOSS_JOURNEY_PROJECT") or "").strip()


def configured_variables() -> dict[str, Any]:
    """The operator's journey values (BOSS_JOURNEY_VARS), without the reserved keys."""
    raw = os.environ.get("BOSS_JOURNEY_VARS") or ""
    try:
        value = json.loads(raw) if raw.strip() else {}
    except ValueError:
        log.warning("BOSS_JOURNEY_VARS is not JSON; ignored")
        return {}
    return {k: v for k, v in value.items() if k not in RESERVED} if isinstance(value, dict) else {}


def _key(engine: Any, settings: Any, project_id: str) -> str:
    ref = (os.environ.get("BOSS_JOURNEY_KEY_REF") or "").strip()
    if not ref:
        raise Refused("set BOSS_JOURNEY_KEY_REF to the vault reference of the journey service "
                      "account's API key; journeys never call the API without one")
    from ..secret_backends import resolve_secret_ref

    with engine.begin() as c:
        key = resolve_secret_ref(c, ref, settings, project_id)
    if not key:
        raise Refused("the journey key's vault reference resolved to nothing")
    return key


def preflight(client: Any, project_id: str) -> str:
    """The journey account, checked through the API: narrow, or refused. Returns its id."""
    status, me = client.call("GET", "/v1/identity/me")
    if status != 200 or not isinstance(me, dict):
        raise Refused(f"the journey key was not accepted (GET /v1/identity/me answered {status})")
    if me.get("kind") != "service_account":
        raise Refused(f"the journey key belongs to a {me.get('kind')}, not a service account")
    if me.get("access_roles"):
        raise Refused("the journey account holds roles at org scope "
                      f"({', '.join(me['access_roles'])}); bind it in the fixture project only")
    status, mine = client.call("GET", "/v1/identity/me/projects")
    projects = (mine or {}).get("projects") if isinstance(mine, dict) else None
    if status != 200 or projects is None:
        raise Refused(f"could not read the journey account's projects (HTTP {status})")
    ids = sorted(str(p.get("project_id")) for p in projects)
    if ids != [project_id]:
        raise Refused(f"the journey account must reach only the fixture project {project_id}; "
                      f"it reaches {', '.join(ids) or 'none'}")
    here = projects[0]
    if "org" in (here.get("via") or []):
        raise Refused("the journey account reaches the fixture project through an org-wide binding")
    broad = sorted(FORBIDDEN & set(here.get("permissions") or []))
    if broad:
        raise Refused(f"the journey account holds {', '.join(broad)}; give it a narrower role")
    return str(me.get("principal_id") or "")


def status() -> dict[str, Any]:
    with _lock:
        return dict(_state)


def _advisory(engine: Any):
    """A connection holding the install's journeys lock, or None when another run holds it."""
    from sqlalchemy import text

    conn = engine.connect()
    try:
        got = conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": LOCK_KEY}).scalar()
    except Exception:  # noqa: BLE001
        conn.close()
        raise
    if not got:
        conn.close()
        return None
    return conn


def start(engine: Any, settings: Any, *, actor: str, only: Optional[list[str]] = None,
          client: Any = None, wait: bool = False) -> dict[str, Any]:
    """Start today's run (all journeys, or `only`) in the fixture project. Returns at once
    unless `wait`. Refuses without a fixture project, a key, or a narrow journey account."""
    journeys = d.load_all()
    if only:
        unknown = sorted(set(only) - {j.id for j in journeys})
        if unknown:
            return {"ok": False, "error": f"no journey {', '.join(unknown)}"}
    project_id = fixture_project()
    if not project_id:
        return {"ok": False, "error": "set BOSS_JOURNEY_PROJECT to the journeys' fixture project"}
    try:
        if client is None:
            client = _runner.HttpClient(api_url(), _key(engine, settings, project_id))
        principal = preflight(client, project_id)
    except Refused as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001 — said, never a value
        return {"ok": False, "error": f"the journey key could not be read ({type(exc).__name__})"}
    held = _advisory(engine)
    if held is None:
        return {"ok": False, "error": "a golden journey run is already going"}
    picked = [j for j in journeys if not only or j.id in only]
    from datetime import datetime, timezone

    with _lock:
        _state.update(running=True, started_at=datetime.now(timezone.utc).isoformat(),
                      journeys=[j.id for j in picked])
    values = {**configured_variables(), "project_id": project_id, "actor": principal}

    def work() -> None:
        try:
            for j in picked:
                row = _runner.run_all([j], client, variables=values, fake_factory=_fakes.start)[0]
                try:
                    with engine.begin() as c:
                        _results.record(c, row, actor=actor, source="run")
                except Exception:  # noqa: BLE001 — one unwritable row must not stop the rest
                    log.exception("golden journey %s: the result could not be stored", j.id)
        finally:
            with _lock:
                _state.update(running=False)
            try:
                held.close()           # ends the session, releasing the advisory lock
            except Exception:  # noqa: BLE001
                pass

    if wait:
        work()
    else:
        threading.Thread(target=work, name="golden-journeys", daemon=True).start()
    return {"ok": True, "started": [j.id for j in picked], "project_id": project_id,
            "api": api_url()}


def arm_daily(connection: Any, *, project_id: str, actor: str, at_time: str,
              tz_name: str = "UTC", days: Optional[list[str]] = None) -> dict[str, Any]:
    """The install's ONE daily wake (schedule_work's own store). Arming again — from any
    project — stops the one before, under a transaction-scoped advisory lock."""
    from sqlalchemy import text

    from .. import agent_wakeups

    connection.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": LOCK_KEY})
    for row in connection.execute(text(
            "SELECT id, project_id FROM agent_wakeups WHERE kind = 'schedule' "
            "AND status = 'active' AND prompt LIKE :m"), {"m": WAKE_MARKER + "%"}).mappings().all():
        agent_wakeups.stop(connection, project_id=row["project_id"], wakeup_id=row["id"],
                           actor=actor, status="stopped",
                           reason="replaced by a new golden-journey schedule")
    return agent_wakeups.schedule(connection, project_id=project_id, actor=actor,
                                  prompt=WAKE_PROMPT, at_time=at_time, days=days or [],
                                  tz_name=tz_name)


def daily_wake(connection: Any) -> Optional[dict[str, Any]]:
    from sqlalchemy import text

    row = connection.execute(text(
        "SELECT id, at_time, timezone, next_wake_at FROM agent_wakeups WHERE kind = 'schedule' "
        "AND status = 'active' AND prompt LIKE :m ORDER BY created_at DESC LIMIT 1"),
        {"m": WAKE_MARKER + "%"}).mappings().first()
    if row is None:
        return None
    nxt = row["next_wake_at"]
    return {"id": row["id"], "at_time": row["at_time"], "timezone": row["timezone"],
            "next_wake_at": nxt.isoformat() if hasattr(nxt, "isoformat") else nxt}


def is_journeys_wake(row: Any) -> bool:
    return str((row or {}).get("prompt") or "").startswith(WAKE_MARKER)


def wake(engine: Any, settings: Any, row: Any) -> str:
    """What the due wake does: start the run (no model turn). The result is the wake's report."""
    out = start(engine, settings, actor=str(row.get("actor") or "schedule"))
    if not out.get("ok"):
        raise Refused(str(out.get("error")))
    return f"golden journeys started: {', '.join(out['started'])}"
