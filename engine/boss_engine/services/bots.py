"""BOTS: a bot is a deployment with triggers (AP-P3, BOSSAPP-1054; AGENT_PLATFORM §4.4 "Bots").

    bot = agent_deployments row  +  agent_triggers rows  →  runs admitted on threads
                                                            (thread_runs.admit), executed by a
                                                            worker through RUNNERS, recorded in
                                                            agent_runs, visible in health

THE RUNNERS. A run of a deployment executes on a worker through agent_run_worker.RUNNERS:

  deployed_agent   a boss.agent/v1 agent: the kernel drives the version the run is PINNED to
                   (finalize_ctx.agent_version), with its prompt, limits and grants — never the
                   deployment's current version, so a promotion never changes a run in flight
  focused_agent    a focused agent's duty cycle (FocusedAgentService.tick_one)
  wakeup           one wake of a monitor or schedule (agent_wakeups.wake_one)
  squad_owner      one handed-over item's squad turn (ambient/owners)
  priors_build     the procedural-priors fold (memory_api.build_priors_job), an hourly bot on
                   the home project (§4.6 "priors build as a scheduled bot")

THE MIGRATION (§5 Phase 3 "Bots"; design risk "migrating ~20 tick functions": one at a time,
each behind a flag, legacy deleted only after its tests pass on the new primitive). Three of
today's always-on mechanisms become bots, each behind its own flag, off by default:

  BOSS_BOTS_FOCUSED_AGENTS   the worker's _tick_focused_agents and FocusedAgentService.tick_due
  BOSS_BOTS_WAKEUPS          the worker's _tick_agent_wakeups and agent_wakeups.run_due
  BOSS_BOTS_SQUAD_OWNERS     ambient owners' per-item squad turn (owners._work)

With a flag on, every unit of that mechanism's work is a bot run: the legacy row is mirrored
onto a legacy deployment and a trigger (`legacy_ref`), the firing goes through the trigger
(pause, skip_if_active, the firing record), the run through admission, the work through its
runner, the outcome through agent_runs (health, the failure streak, the doctor). The legacy
entry points — the ones the worker's _tick_focused_agents / _tick_agent_wakeups and the
ambient tick knock on — keep their answers: each unit is fired, leased in the admitting
transaction and executed right there (where it ran before), so they return exactly what they
returned before and their existing tests run on the new primitive
(test_ap_p3_deployments_and_bots (d)). Operator-made bots run on worker slots.

The deterministic daemons (the doctor, the reaper, stop_watch) are NOT bots (D7): they are in
system_services.
"""
from __future__ import annotations

import contextvars
import json
import logging
import os
from typing import Any, Callable, Optional

from sqlalchemy import text

log = logging.getLogger(__name__)

BOTS_FOCUSED_AGENTS = "BOSS_BOTS_FOCUSED_AGENTS"
BOTS_WAKEUPS = "BOSS_BOTS_WAKEUPS"
BOTS_SQUAD_OWNERS = "BOSS_BOTS_SQUAD_OWNERS"
#: Each migration flag and the legacy source its triggers carry (`legacy_ref` prefix).
FLAGS = {BOTS_FOCUSED_AGENTS: "focused_agent", BOTS_WAKEUPS: "wakeup",
         BOTS_SQUAD_OWNERS: "squad_owner"}
#: Off until each migration has run on an install (D3: flipped per surface, after parity).
DEFAULTS = {BOTS_FOCUSED_AGENTS: False, BOTS_WAKEUPS: False, BOTS_SQUAD_OWNERS: False}
_OFF = ("0", "false", "no", "off")

#: The legacy deployments' keys and names (one per project per source).
LEGACY_AGENTS = {"priors_build": ("priors_build", "Procedural priors build"),
                 "focused_agent": ("focused_agents", "Focused agents"),
                 "wakeup": ("boss_wakeups", "Watches and schedules"),
                 "squad_owner": ("squad_owners", "Squad owners")}

#: An inline unit of work for the run being executed in this thread (the squad owners' turn,
#: with its scripted model in tests): the runner calls it instead of rebuilding it.
_INLINE_UNIT: contextvars.ContextVar[Optional[Callable[[], Any]]] = contextvars.ContextVar(
    "boss_bot_inline_unit", default=None)


def flag_on(name: str) -> bool:
    said = (os.environ.get(name) or "").strip().lower()
    if not said:
        return bool(DEFAULTS.get(name, False))
    return said not in _OFF


def sources_on() -> set:
    return {source for flag, source in FLAGS.items() if flag_on(flag)}


# ── Legacy deployments and their triggers ──────────────────────────────────────────────────────

def legacy_deployment(connection: Any, project_id: str, source: str) -> dict:
    """The project's deployment for a migrated mechanism (prod, its own runner, no spec)."""
    from . import agent_deployments

    key, name = LEGACY_AGENTS[source]
    found = agent_deployments.get(connection, project_id, key, "prod")
    if found is not None:
        return found
    return agent_deployments.deploy(connection, project_id, key, "prod", actor="boss",
                                    runner=source, source=f"legacy:{source}", display_name=name,
                                    expose={"rest": False, "mcp": False, "a2a": False})


def legacy_trigger(connection: Any, project_id: str, source: str, ref: str, *, kind: str,
                   config: dict, name: str, actor: str = "boss",
                   next_fire_at: Any = None) -> dict:
    """The trigger mirroring one legacy row (`legacy_ref` = "<source>:<ref>"), made once."""
    from . import agent_triggers

    legacy_ref = f"{source}:{ref}"
    row = connection.execute(text(
        "SELECT * FROM agent_triggers WHERE project_id = :p AND legacy_ref = :r"),
        {"p": project_id, "r": legacy_ref}).mappings().first()
    if row is not None:
        return dict(row)
    dep = legacy_deployment(connection, project_id, source)
    created = agent_triggers.create(connection, project_id=project_id, deployment_id=dep["id"],
                                    kind=kind, actor=actor, config=config, name=name[:120],
                                    legacy_ref=legacy_ref, next_fire_at=next_fire_at)
    return dict(agent_triggers.get(connection, created["id"]) or {})


# ── Inline execution (the legacy entry points keep their synchronous answers) ──────────────────

INLINE_WORKER = "api-inline"


def fire_and_lease(connection: Any, trigger_id: str, *, settings: Any, **fire_kwargs: Any) -> dict:
    """Fire a trigger and, when a run was admitted, lease its job IN THE SAME TRANSACTION — no
    worker can claim a job it never saw queued, so the caller executes it here."""
    from . import agent_triggers
    from .jobs import JobLeaseService

    got = agent_triggers.fire(connection, trigger_id, **fire_kwargs)
    if got.get("outcome") != "admitted" or not got.get("job_id"):
        return got
    project_id, target = connection.execute(text(
        "SELECT project_id, target FROM jobs WHERE id = :j"), {"j": got["job_id"]}).one()
    if target:
        # A run TARGETED at a microvm deployment's replicas (P8a, jobs.target) is never leased
        # here: its hosts claim it through the run plane. The caller gets it queued.
        return {**got, "remote": True}
    leased = JobLeaseService(settings).lease_owned_job(
        connection, project_id=str(project_id), job_id=str(got["job_id"]),
        worker_id=INLINE_WORKER, lease_ttl_seconds=3600)
    return {**got, "lease_token": leased["lease_token"]}


def execute_leased(engine: Any, settings: Any, got: dict) -> dict:
    """Execute an admitted, leased run in this process (agent_run_worker.execute: the same path
    a worker takes) and settle its job. Returns the run as it ended: {status, output, error}."""
    from . import agent_run_worker
    from .jobs import JobLeaseService

    jobs = JobLeaseService(settings)
    try:
        result = agent_run_worker.execute(
            engine, settings, kind="agent.run", job_id=got["job_id"], run_id=got["run_id"],
            worker_id=INLINE_WORKER, lease_token=got["lease_token"], jobs=jobs)
    except Exception as exc:  # noqa: BLE001 — recorded on the run; the caller reports it
        result = {"status": "failed", "error": f"{type(exc).__name__}: {exc}"[:500]}
    if not result.get("job_deferred"):
        try:
            with engine.begin() as c:
                jobs.complete(c, INLINE_WORKER, got["job_id"], got["lease_token"],
                              {"status": result.get("status"), "run_id": got["run_id"]})
        except Exception:  # noqa: BLE001 — the run is recorded; the job's lease expires
            log.debug("inline job %s not completed", got["job_id"], exc_info=True)
    with engine.connect() as c:
        row = c.execute(text("SELECT status, output_text, error FROM agent_runs WHERE id = :r"),
                        {"r": got["run_id"]}).mappings().first() or {}
    output = row.get("output_text") or ""
    try:
        parsed = json.loads(output) if output else None
    except ValueError:
        parsed = None
    return {"run_id": got["run_id"], "status": row.get("status") or result.get("status"),
            "output": parsed, "output_text": output, "error": row.get("error")
            or result.get("error")}


def run_inline(engine: Any, settings: Any, trigger_id: str, *,
               unit: Optional[Callable[[], Any]] = None, **fire_kwargs: Any) -> dict:
    """Fire → admit → lease → execute, here. `unit` is the work the runner does (when the
    runner takes it from the caller)."""
    with engine.begin() as c:
        got = fire_and_lease(c, trigger_id, settings=settings, **fire_kwargs)
    if got.get("outcome") != "admitted":
        return {"fired": False, **got}
    if got.get("remote"):
        return {"fired": True, "run_id": got.get("run_id"), "status": "queued", **got}
    token = _INLINE_UNIT.set(unit)
    try:
        return {"fired": True, **execute_leased(engine, settings, got)}
    finally:
        _INLINE_UNIT.reset(token)


# ── Focused agents (BOSS_BOTS_FOCUSED_AGENTS) ──────────────────────────────────────────────────

def _focused_trigger(connection: Any, project_id: str, agent: dict) -> dict:
    """One monitor trigger per focused agent, checked every minute by the agent's OWN due rule
    (interval elapsed, or a wake event for a hybrid agent): check_hook focused_agent_due."""
    from . import agent_triggers

    trig = legacy_trigger(connection, project_id, "focused_agent", str(agent["id"]),
                          kind="monitor", name=str(agent.get("name") or "Focused agent"),
                          config={"every_seconds": agent_triggers.MIN_EVERY_SECONDS,
                                  "check": str(agent.get("focus") or "")[:200],
                                  "check_hook": "focused_agent_due"})
    want = "on" if agent.get("status") == "active" else "paused"
    if trig.get("status") != want:
        agent_triggers.set_paused(connection, project_id=project_id, trigger_id=trig["id"],
                                  actor="boss", paused=want == "paused",
                                  reason=f"The focused agent is {agent.get('status')}.")
        trig = dict(agent_triggers.get(connection, trig["id"]) or trig)
    return trig


def tick_focused_agents(service: Any, engine: Any, *, project_id: str, actor: str) -> dict:
    """FocusedAgentService.tick_due on the bot primitive: each active agent that is due by its
    own rule fires its trigger, and its duty cycle runs as a bot run (inline; same answer)."""
    with engine.begin() as c:
        service.ensure_schema(c)
        agents = [a for a in service.list_agents(c, project_id) if a.get("status") == "active"]
        due = []
        for agent in agents:
            reason = service._due_reason(c, agent)  # noqa: SLF001 — the agent's own rule
            due.append((agent, reason, _focused_trigger(c, project_id, agent)["id"]
                        if reason else None))
    ran: list[dict] = []
    skipped: list[dict] = []
    for agent, reason, trigger_id in due:
        if not reason:
            skipped.append({"id": agent["id"], "reason": "not_due"})
            continue
        got = run_inline(engine, service.settings, trigger_id, actor=actor,
                         finalize_extra={"legacy": {"focused_agent_id": agent["id"],
                                                    "reason": reason, "actor": actor}})
        if not got.get("fired"):
            skipped.append({"id": agent["id"], "reason": got.get("reason") or got.get("outcome")})
            continue
        if got.get("status") == "completed" and isinstance(got.get("output"), dict):
            ran.append(got["output"])
        else:
            ran.append({"id": agent["id"], "agent_id": agent["id"], "error":
                        got.get("error") or got.get("status"), "run_id": got["run_id"]})
    return {"project_id": project_id, "ran": ran, "skipped": skipped, "active": len(agents),
            "primitive": "bots"}


def sync_focused_agents(connection: Any, project_id: Optional[str] = None) -> int:
    """Mirror every focused agent onto its trigger (the worker's bot clock, flag on)."""
    from .focused_agents import FocusedAgentService

    rows = connection.execute(text(
        "SELECT * FROM focused_agents WHERE status IN ('active', 'paused') "
        "AND (CAST(:p AS TEXT) IS NULL OR project_id = CAST(:p AS TEXT))"),
        {"p": project_id}).mappings().all() if _table(connection, "focused_agents") else []
    for row in rows:
        _focused_trigger(connection, row["project_id"], FocusedAgentService._row(row))
    return len(rows)


def _focused_due_check(connection: Any, trigger: dict) -> Optional[str]:
    from ..config import get_settings
    from .focused_agents import FocusedAgentService

    ref = str(trigger.get("legacy_ref") or "").split(":", 1)[-1]
    row = connection.execute(text("SELECT * FROM focused_agents WHERE id = :i"),
                             {"i": ref}).mappings().first()
    if row is None or row["status"] != "active":
        return None
    service = FocusedAgentService(get_settings())
    return service._due_reason(connection, FocusedAgentService._row(row))  # noqa: SLF001


def _focused_runner(connection: Any, settings: Any, run: dict, *, kind: str) -> dict:
    from .focused_agents import FocusedAgentService

    fctx = dict(run.get("finalize_ctx") or {})
    legacy = dict(fctx.get("legacy") or {})
    agent_id = legacy.get("focused_agent_id") or str(
        (fctx.get("trigger_input") or {}).get("focused_agent_id") or "")
    if not agent_id:
        ref = connection.execute(text(
            "SELECT legacy_ref FROM agent_triggers WHERE id = :t"),
            {"t": fctx.get("trigger_id")}).scalar() or ""
        agent_id = str(ref).split(":", 1)[-1]
    reason = str(legacy.get("reason") or fctx.get("due_reason") or "schedule")
    try:
        out = FocusedAgentService(settings).tick_one(
            connection, project_id=str(run["project_id"]),
            actor=str(legacy.get("actor") or "system"), agent_id=agent_id, trigger=reason)
    except Exception as exc:  # noqa: BLE001 — the run records it; health shows it
        connection.rollback()
        return {"status": "failed", "error": f"{type(exc).__name__}: {exc}"[:500],
                "clean_failure": True}
    return {"status": "completed", "output_text": json.dumps(out, default=str)}


# ── Wakeups (BOSS_BOTS_WAKEUPS) ────────────────────────────────────────────────────────────────

_DAYS = {"sun": 0, "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6}


def _wakeup_trigger(connection: Any, row: dict) -> dict:
    """A monitor wakeup mirrors as a monitor trigger; a schedule as a cron (`at_time` on
    `days`, in its zone). The wakeup keeps its own cycle cap, deadline and failure stop."""
    if row["kind"] == "monitor":
        kind = "monitor"
        config = {"every_seconds": max(60, int(row.get("interval_seconds") or 60)),
                  "check": str(row.get("prompt") or "")[:200], "max_cycles": 0}
    else:
        kind = "cron"
        hour, _, minute = str(row.get("at_time") or "09:00").partition(":")
        days = _json(row.get("days"), [])
        dow = ",".join(str(_DAYS[d]) for d in days if d in _DAYS) or "*"
        config = {"cron": f"{int(minute or 0)} {int(hour or 0)} * * {dow}",
                  "timezone": str(row.get("timezone") or "UTC")}
    trig = legacy_trigger(connection, row["project_id"], "wakeup", str(row["id"]), kind=kind,
                          config=config, name=str(row.get("prompt") or "Wakeup")[:120],
                          actor=str(row.get("actor") or "boss"),
                          next_fire_at=row.get("next_wake_at"))
    connection.execute(text(
        "UPDATE agent_triggers SET next_fire_at = :n, updated_at = now() WHERE id = :t"),
        {"n": row.get("next_wake_at"), "t": trig["id"]})
    return trig


def run_wakeups(settings: Any, engine: Any, *, limit: int = 5,
                project_id: Optional[str] = None) -> dict:
    """agent_wakeups.run_due on the bot primitive: the same due rows, each fired through its
    trigger and run as a bot run (inline; the same `{due, ran}` answer)."""
    from . import agent_wakeups as W

    with engine.begin() as connection:
        W.close_spent(connection, project_id=project_id)
        pending = [dict(r) for r in W.due(connection, limit=limit, project_id=project_id)]
        triggers = {row["id"]: _wakeup_trigger(connection, row)["id"] for row in pending}
    ran: list[dict] = []
    for row in pending:
        got = run_inline(engine, settings, triggers[row["id"]], actor=str(row["actor"]),
                         scheduled_for=row.get("next_wake_at"),
                         dedupe_key=f"wake:{row['id']}:{row.get('cycle')}",
                         finalize_extra={"legacy": {"wakeup_id": row["id"]}})
        if isinstance(got.get("output"), dict):
            ran.append(got["output"])
        elif got.get("fired"):
            ran.append({"project_id": row["project_id"], "kind": row["kind"],
                        "error": "RunFailed", "detail": got.get("error")})
        else:
            ran.append({"project_id": row["project_id"], "kind": row["kind"],
                        "skipped": got.get("reason") or got.get("outcome")})
    return {"due": len(pending), "ran": ran, "primitive": "bots"}


def sync_wakeups(connection: Any, project_id: Optional[str] = None) -> int:
    """Mirror active wakeups onto their triggers (next_fire_at = next_wake_at) and pause the
    triggers of ended ones (the worker's bot clock, flag on)."""
    from . import agent_wakeups as W

    W.close_spent(connection, project_id=project_id)
    rows = [dict(r) for r in connection.execute(text(
        "SELECT * FROM agent_wakeups WHERE status = 'active' "
        "AND (CAST(:p AS TEXT) IS NULL OR project_id = CAST(:p AS TEXT))"),
        {"p": project_id}).mappings().all()]
    for row in rows:
        _wakeup_trigger(connection, row)
    connection.execute(text("""
        UPDATE agent_triggers t SET status = 'paused', paused_reason = 'The wakeup ended.',
               paused_at = now(), updated_at = now()
          FROM agent_wakeups w
         WHERE t.legacy_ref = 'wakeup:' || w.id AND w.status <> 'active' AND t.status = 'on'"""))
    return len(rows)


def _wakeup_runner(connection: Any, settings: Any, run: dict, *, kind: str) -> dict:
    from . import agent_wakeups as W

    fctx = dict(run.get("finalize_ctx") or {})
    wakeup_id = (fctx.get("legacy") or {}).get("wakeup_id")
    if not wakeup_id:
        ref = connection.execute(text("SELECT legacy_ref FROM agent_triggers WHERE id = :t"),
                                 {"t": fctx.get("trigger_id")}).scalar() or ""
        wakeup_id = str(ref).split(":", 1)[-1]
    row = connection.execute(text("SELECT * FROM agent_wakeups WHERE id = :i"),
                             {"i": wakeup_id}).mappings().first()
    connection.commit()  # the turn runs in its own transactions (wake_one)
    if row is None or row["status"] != "active":
        return {"status": "completed", "output_text": json.dumps(
            {"project_id": run["project_id"], "outcome": (row or {}).get("status") or "gone"})}
    entry = W.wake_one(settings, connection.engine, dict(row))
    if entry.get("error"):
        return {"status": "failed", "error": f"{entry['error']}: "
                f"{str(entry.get('last_result') or '')[:300]}",
                "output_text": json.dumps(entry, default=str), "clean_failure": True}
    return {"status": "completed", "output_text": json.dumps(entry, default=str)}


# ── Squad owners (BOSS_BOTS_SQUAD_OWNERS) ──────────────────────────────────────────────────────

def squad_owner_turn(engine: Any, settings: Any, entry: dict, unit: Callable[[], Any]) -> Any:
    """One handed-over item's squad turn as a bot run: fired through the project's squad-owner
    trigger (event kind, internal), admitted, executed with `unit` (the owners' own code)."""
    with engine.begin() as c:
        trig = legacy_trigger(c, entry["project_id"], "squad_owner", "items", kind="event",
                              config={"event_type": "ambient.handoff", "internal": True},
                              name="A handed-over item is owed a turn")
    got = run_inline(engine, settings, trig["id"], unit=unit, actor="agent:chief_of_staff",
                     payload={"work_item_id": entry["id"], "kind": entry.get("kind")},
                     finalize_extra={"legacy": {"work_item_id": entry["id"]}})
    if not got.get("fired"):
        raise RuntimeError(f"the squad owners' trigger did not fire: {got.get('reason')}")
    return got


def _squad_owner_runner(connection: Any, settings: Any, run: dict, *, kind: str) -> dict:
    unit = _INLINE_UNIT.get()
    connection.commit()
    if unit is None:
        # Only ever leased inline (fire_and_lease): a worker never sees this job queued.
        return {"status": "failed", "error": "a squad owner turn runs inline only",
                "clean_failure": True}
    try:
        out = unit()
    except Exception as exc:  # noqa: BLE001
        return {"status": "failed", "error": f"{type(exc).__name__}: {exc}"[:500],
                "clean_failure": True}
    if isinstance(out, dict) and out.get("kind") == "error":
        return {"status": "failed", "error": str(out.get("reason") or "the turn failed"),
                "output_text": json.dumps(out, default=str), "clean_failure": True}
    return {"status": "completed", "output_text": json.dumps(out, default=str)}


# ── The procedural-priors build (AGENT_PLATFORM §4.6: "priors build as a scheduled bot") ──────

#: How often the priors fold runs (it is idempotent: a high-water mark per org).
PRIORS_EVERY_SECONDS = 3600


def ensure_priors_bot(connection: Any, project_id: str) -> Optional[dict]:
    """The install's priors build as a bot: one hourly trigger on the home project's
    `priors_build` deployment, folding executed runs into kg_action_priors for its org
    (memory_api.build_priors_job). Made once; its pause, health and runs are any bot's."""
    org = connection.execute(text(
        "SELECT w.org_id FROM projects p JOIN workspaces w ON w.id = p.workspace_id "
        "WHERE p.id = :p"), {"p": project_id}).scalar()
    if not org:
        return None
    return legacy_trigger(connection, project_id, "priors_build", str(org), kind="cron",
                          config={"every_seconds": PRIORS_EVERY_SECONDS},
                          name="Fold executed runs into procedural priors")


def _priors_runner(connection: Any, settings: Any, run: dict, *, kind: str) -> dict:
    from .memory_api import build_priors_job

    fctx = dict(run.get("finalize_ctx") or {})
    ref = connection.execute(text("SELECT legacy_ref FROM agent_triggers WHERE id = :t"),
                             {"t": fctx.get("trigger_id")}).scalar() or ""
    org = str(ref).split(":", 1)[-1]
    connection.commit()
    try:
        out = build_priors_job(connection.engine, org_id=org, settings=settings)
    except Exception as exc:  # noqa: BLE001 — recorded on the run; health shows it
        return {"status": "failed", "error": f"{type(exc).__name__}: {exc}"[:500],
                "clean_failure": True}
    return {"status": "completed", "output_text": json.dumps(out, default=str)}


# ── Deployed agents: the kernel on the pinned version ──────────────────────────────────────────

#: Tests (and a future engine lane) replace how a deployed run gets its engine and tools:
#: (connection, settings, run, version_row) -> {"engine": ..., "tools": ..., "gate": ...}.
PARTS_HOOK: Optional[Callable[..., dict]] = None


def _default_parts(connection: Any, settings: Any, run: dict, version_row: dict) -> dict:
    """The project's routed model through the budget gate, and exactly the agent's grants as
    tools (agent_loop's bounded AgentStepToolSet: its skills and MCP servers, no native tools,
    every write held on a Decision)."""
    from ..kernel.governance import AdapterGate
    from .agent_loop import AgentStepToolSet, _kernel_engine_for
    from .model_routing import governed, project_chat_adapter

    project_id = str(run["project_id"])
    raw = project_chat_adapter(connection, settings, project_id)
    held = governed(raw, connection, settings, project_id, skill_id="model.agent_turn",
                    ref=str(run["id"]))
    engine = _kernel_engine_for(raw)
    gate = AdapterGate(held) if callable(getattr(held, "reserve_turn", None)) else None
    if gate is None and held is not raw:
        engine = _kernel_engine_for(held)
    bounded = {**run, "finalize_ctx": {**dict(run.get("finalize_ctx") or {}),
                                       "agent_step": {"role_key": version_row["agent_key"]}}}
    return {"engine": engine, "tools": AgentStepToolSet(connection, settings, bounded,
                                                        str(run["id"])), "gate": gate}


def _build_deployed(connection: Any, settings: Any, run: dict) -> Any:
    from .. import kernel
    from ..agents import spec as S
    from ..agents import versions as V
    from ..kernel import presets
    from ..kernel.agent_spec import policies_for, run_fields

    fctx = dict(run.get("finalize_ctx") or {})
    key, version = str(fctx["agent_key"]), int(fctx["agent_version"])
    # THE PINNED VERSION: the one this run was admitted on, whatever is deployed now.
    row = V.get_version(connection, str(run["project_id"]), key, version)
    compiled = S.compile_spec(S.load_spec(_json(row["spec"], {})))
    parts = (PARTS_HOOK or _default_parts)(connection, settings, run, row)
    # P9a-6: a run a chat conversation started also gets chat_reply / chat_status / chat_react.
    from .chat_reply import with_chat_tools

    parts = {**parts, "tools": with_chat_tools(parts["tools"], connection, settings, run)}
    fields = run_fields(compiled)
    steps = int(compiled.limits.max_steps or getattr(settings, "agent_max_steps", 20) or 20)
    sink = None
    try:
        from ..kernel import ledger as kernel_ledger

        if kernel_ledger.enabled():
            sink = kernel_ledger.DbSink(connection.engine)
    except Exception:  # noqa: BLE001
        sink = None
    return kernel.RunSpec(
        engine=parts["engine"], tools=parts["tools"],
        policies=policies_for(presets.agent_loop(steps), compiled),
        project_id=str(run["project_id"]), system=row["compiled_prompt"] or compiled.prompt,
        messages=[m for m in (run.get("messages") or []) if m.get("role") != "system"],
        actor=fields["actor"], gate=parts.get("gate"), sink=sink, db=connection,
        # kernel_runner replaces it with the run's PgCheckpointStore (committed, fenced).
        store=None, persist=True,
        extra={**fields["extra"], "agent_version": version,
               "deployment_id": fctx.get("deployment_id"),
               "environment": fctx.get("environment"),
               # AP-P5: the protocol caller's trace context (MCP `_meta`, A2A / REST header):
               # the run's root span joins the caller's trace (kernel/trace.py).
               **({"traceparent": fctx["traceparent"]} if fctx.get("traceparent") else {})})


def _deployed_runner(connection: Any, settings: Any, run: dict, *, kind: str) -> dict:
    """A deployed agent's run, under the agent's delegated identity (as a workflow agent step
    runs), driven by the kernel on its pinned version."""
    from ..identity import catalogue as _catalogue
    from ..identity import runs as _identity_runs
    from . import agent_run_worker
    from .connection_credentials import acting_agent

    fctx = dict(run.get("finalize_ctx") or {})
    key = str(fctx.get("agent_key") or "")
    with _identity_runs.delegated(connection.engine, kind="ag", entity_id=str(run["id"]),
                                  project_id=str(run["project_id"]), role_key=key,
                                  scope=_catalogue.delegable(),
                                  job_id=fctx.get("created_by_job_id")), acting_agent(key):
        return agent_run_worker.kernel_runner(_build_deployed)(connection, settings, run,
                                                                kind=kind)


def _json(value: Any, default: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return default
    return default if value is None else value


def _table(connection: Any, name: str) -> bool:
    return bool(connection.execute(text("SELECT to_regclass(:t)"), {"t": name}).scalar())


def _settings() -> Any:
    from ..config import get_settings

    return get_settings()


def register_runners() -> None:
    from . import agent_run_worker

    agent_run_worker.RUNNERS.setdefault("deployed_agent", _deployed_runner)
    agent_run_worker.RUNNERS.setdefault("focused_agent", _focused_runner)
    agent_run_worker.RUNNERS.setdefault("wakeup", _wakeup_runner)
    agent_run_worker.RUNNERS.setdefault("squad_owner", _squad_owner_runner)
    agent_run_worker.RUNNERS.setdefault("priors_build", _priors_runner)


#: Hooks a monitor trigger names in `check_hook`.
CHECKS = {"focused_agent_due": _focused_due_check}


# ── The bot clock ──────────────────────────────────────────────────────────────────────────────

def tick(engine: Any, settings: Any = None, *, project_id: Optional[str] = None,
         limit: int = 50) -> dict:
    """THE BOTS' ONE CLOCK (the worker's _tick_bots → POST /v1/bots/tick): fire every due
    clocked trigger of a live project; the runs it admits execute on worker slots like every
    agent run. A migrated mechanism's triggers are fired by its own entry point (the worker's
    legacy tick still knocks on it, and with the flag on it runs on this primitive), so the
    clock only mirrors their state (paused when the wakeup ended or the agent was paused)."""
    from . import agent_triggers, projects_archive, system_services

    sources = sources_on()
    synced: dict = {}
    with engine.begin() as c:
        if project_id and projects_archive.is_archived(c, project_id):
            return {"fired": [], "skipped": [], **projects_archive.skipped(project_id)}
        home = project_id or str(getattr(settings or _settings(), "default_project_id", "")
                                 or "")
        if home and not projects_archive.is_archived(c, home):
            ensure_priors_bot(c, home)
        if "focused_agent" in sources:
            synced["focused_agent"] = sync_focused_agents(c, project_id)
        if "wakeup" in sources:
            synced["wakeup"] = sync_wakeups(c, project_id)
        out = agent_triggers.tick(c, limit=limit, project_id=project_id,
                                  sources={"priors_build"},
                                  checks=CHECKS)
    system_services.beat(engine, "bot_clock", details={"fired": len(out["fired"])})
    return {**out, "sources": sorted(sources), "synced": synced}


register_runners()

__all__ = ["BOTS_FOCUSED_AGENTS", "BOTS_SQUAD_OWNERS", "BOTS_WAKEUPS", "CHECKS", "FLAGS",
           "PARTS_HOOK", "execute_leased", "fire_and_lease", "flag_on", "legacy_deployment",
           "legacy_trigger", "register_runners", "run_inline", "run_wakeups", "sources_on",
           "squad_owner_turn", "sync_focused_agents", "sync_wakeups", "tick",
           "tick_focused_agents"]
