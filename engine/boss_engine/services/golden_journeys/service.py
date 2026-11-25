"""The daily run on an install: what the schedule's wake and Govern's "Run now" start.

THE SCHEDULE IS BOSS'S OWN, AND SPENDS NO MODEL TURN. `arm_daily` writes ONE install-wide
`schedule_work` row (agent_wakeups, kind `schedule`) and stamps it `purpose = 'golden_journeys'`
— a column only this module writes; agent_wakeups.schedule never sets it and refuses a prompt
that starts with WAKE_MARKER. When it is due, agent_wakeups.wake_one sees the PURPOSE (never the
prompt) and starts the run here directly instead of opening an agent turn. No chat turn can
start a run: there is no agent tool for it, and schedule_work cannot make this row.

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
  * there it holds nothing outside ALLOWED (the permissions the journeys' own calls need).
Give it a custom role holding exactly ALLOWED, bound in the fixture project only. A production promotion therefore always waits for a person (J6 asserts the
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

#: Words no schedule_work prompt may start with (agent_wakeups.schedule refuses them).
WAKE_MARKER = "[golden-journeys]"
#: What identifies the journeys wake: agent_wakeups.purpose, written only by arm_daily.
PURPOSE = "golden_journeys"
WAKE_PROMPT = ("The daily golden journeys. BOSS starts them itself when this is due; results "
               "land on Govern › Journeys as each journey is judged.")
#: Keys only this module sets: a journey's values can never move the run somewhere else.
RESERVED = frozenset({"project_id", "actor", "stamp"})
#: EVERYTHING the journey account may hold in the fixture project: the permissions the journeys'
#: own calls need (each route's catalogue permission; engine/tests/test_golden_journeys.py
#: derives this set from tests/identity/matrix.yaml and fails when they drift) and nothing else.
#: An account holding ANY other permission — an approval, a production deploy, a secret, a
#: role grant, a target or an account to manage — is refused.
ALLOWED = frozenset({
    "project:read", "integration:read",
    "code_task:create", "code_task:read", "code_task:steer", "github:merge",
    "deployment:create", "deployment:read",
    "workflow:create", "workflow:delete", "workflow_run:start", "workflow_run:read",
    "mcp_server:register", "mcp_server:review", "agent_ability:assign",
    "observability:read", "observability:manage", "reliability:read",
})

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
    extra = sorted(set(here.get("permissions") or []) - ALLOWED)
    if extra:
        raise Refused(f"the journey account holds {', '.join(extra)}, which no journey needs; "
                      "give it a role with only the journeys' permissions (service.ALLOWED)")
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
    """The install's ONE daily wake (schedule_work's own store), stamped with its PURPOSE.
    Arming again — from any project — stops the one before, under an advisory lock."""
    from sqlalchemy import text
    from sqlalchemy.exc import IntegrityError

    from .. import agent_wakeups

    connection.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": LOCK_KEY})
    for row in connection.execute(text(
            "SELECT id, project_id FROM agent_wakeups WHERE purpose = :u AND status = 'active'"),
            {"u": PURPOSE}).mappings().all():
        agent_wakeups.stop(connection, project_id=row["project_id"], wakeup_id=row["id"],
                           actor=actor, status="stopped",
                           reason="replaced by a new golden-journey schedule")
    made = agent_wakeups.schedule(connection, project_id=project_id, actor=actor,
                                  prompt=WAKE_PROMPT, at_time=at_time, days=days or [],
                                  tz_name=tz_name)
    try:
        with connection.begin_nested():
            connection.execute(text("UPDATE agent_wakeups SET purpose = :u WHERE id = :i"),
                               {"u": PURPOSE, "i": made["id"]})
    except IntegrityError as exc:
        raise agent_wakeups.WakeupError("another golden-journeys schedule was armed at the same "
                                        "moment; try again") from exc
    return {**made, "purpose": PURPOSE}


def daily_wake(connection: Any) -> Optional[dict[str, Any]]:
    from sqlalchemy import text

    row = connection.execute(text(
        "SELECT id, at_time, timezone, next_wake_at FROM agent_wakeups WHERE purpose = :u "
        "AND status = 'active' ORDER BY created_at DESC LIMIT 1"),
        {"u": PURPOSE}).mappings().first()
    if row is None:
        return None
    nxt = row["next_wake_at"]
    return {"id": row["id"], "at_time": row["at_time"], "timezone": row["timezone"],
            "next_wake_at": nxt.isoformat() if hasattr(nxt, "isoformat") else nxt}


def is_journeys_wake(row: Any) -> bool:
    """By the purpose column only — never by what the prompt says."""
    return (row or {}).get("purpose") == PURPOSE and (row or {}).get("kind") == "schedule"


def wake(engine: Any, settings: Any, row: Any) -> str:
    """What the due wake does: start the run (no model turn). The result is the wake's report."""
    out = start(engine, settings, actor=str(row.get("actor") or "schedule"))
    if not out.get("ok"):
        raise Refused(str(out.get("error")))
    return f"golden journeys started: {', '.join(out['started'])}"
