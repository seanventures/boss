"""TRIGGERS: what starts a deployed agent's runs (AP-P3, BOSSAPP-1054; AGENT_PLATFORM §4.4
"Triggers"). One table, four kinds, each on machinery BOSS already runs:

  cron     a cron expression read by workflow_schedules.next_cron (or a plain `every_seconds`);
           due rows are fired by `tick`, the bots' one clock
  event    an event_subscriptions row with sink_kind `agent_run` (event_dispatch fans a BOSS
           event out to it, deduplicated per event)
  webhook  a signed inbound URL (workflow_webhooks' HMAC scheme: `v1=` over `<ts>.<body>`);
           the secret lives in the secret backend and is shown ONCE, at creation and rotation
  monitor  a polled check with a cycle cap (agent_wakeups' semantics): fires every
           `every_seconds` until `max_cycles`, then pauses itself with the reason

EVERY KIND COMPILES TO "ADMIT A RUN ON A THREAD" (thread_runs.admit, through
agent_deployments.admit_run): the run and its job in one transaction, pinned to the version the
deployment runs now, executed by a worker. Each trigger owns one thread, so its concurrency is
the thread's:

  skip_if_active   (default) a firing while the last run is still going is SKIPPED and recorded
  coalesce         at most one waiting run; further firings while it waits are skipped
  always_enqueue   every firing is admitted and waits its turn

PAUSE stops a trigger at the next tick: the due query never selects a paused row, and `fire`
re-reads the row under its lock, so a firing already selected when the pause lands is refused.
"""
from __future__ import annotations

import hashlib
import json
import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .core import emit_event, make_id

KINDS = ("cron", "event", "webhook", "monitor")
#: workflow_schedules.CONCURRENCY_POLICIES — the same three words.
CONCURRENCY = ("always_enqueue", "skip_if_active", "coalesce")
#: The finest interval an operator may ask for (the bots' clock ticks once a minute).
MIN_EVERY_SECONDS = 60
MAX_PREVIEW = 20
#: The public path a webhook sender POSTs to (under the signed-callback prefix).
INBOUND_PATH_PREFIX = "/v1/webhooks/inbound/agent/"
#: The job kind sink an event subscription names for an agent trigger.
SINK_KIND = "agent_run"
#: A trigger that failed this many times in a row is reported unhealthy (and, with auto_pause,
#: the system doctor pauses it).
FAILING_STREAK = 3

_EVENT_WORDS = {
    "github.issue_opened": "a new GitHub issue opens",
    "github.issue_comment": "someone comments on a GitHub issue",
    "github.pr_opened": "a pull request opens",
    "github.pr_review": "a pull request is reviewed",
    "incident.opened": "an alert opens an incident",
    "incident.resolved": "an incident resolves",
    "code_task.pr_opened": "a build opens its pull request",
    "code_task.completed": "a build finishes",
    "meeting.completed": "a panel finishes",
    "webhook.inbound.received": "an inbound webhook arrives",
}
_DAY = ("Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday")


class TriggerError(ValueError):
    def __init__(self, message: str, *, status: int = 422):
        super().__init__(message)
        self.status = status


def _iso(value: Any) -> Optional[str]:
    return value.isoformat() if hasattr(value, "isoformat") else value


def _json(value: Any, default: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return default
    return default if value is None else value


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ── Validation, timing and words ───────────────────────────────────────────────────────────────

def normalise_config(kind: str, config: Optional[Mapping[str, Any]]) -> dict:
    """The stored config for `kind`, or TriggerError naming what is wrong."""
    from . import event_dispatch
    from .workflow_schedules import ScheduleError, parse_cron

    cfg = dict(config or {})
    if kind not in KINDS:
        raise TriggerError(f"kind must be one of {', '.join(KINDS)}.")
    if kind in ("cron", "monitor"):
        tz = str(cfg.get("timezone") or "UTC")
        try:
            ZoneInfo(tz)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise TriggerError(f"unknown timezone {tz!r}.") from exc
        cfg["timezone"] = tz
        if cfg.get("cron") and kind == "cron":
            try:
                parse_cron(str(cfg["cron"]))
            except ScheduleError as exc:
                raise TriggerError(str(exc)) from exc
            cfg.pop("every_seconds", None)
        else:
            every = cfg.get("every_seconds")
            if every is None or int(every) < MIN_EVERY_SECONDS:
                raise TriggerError(f"say how often: a cron expression, or every_seconds of at "
                                   f"least {MIN_EVERY_SECONDS}.")
            cfg["every_seconds"] = int(every)
        if kind == "monitor":
            cfg["max_cycles"] = int(cfg.get("max_cycles") or 0)
    elif kind == "event":
        event_type = str(cfg.get("event_type") or "").strip()
        if not cfg.get("internal") and not event_dispatch.is_dispatchable_event(event_type):
            raise TriggerError(f"{event_type or 'that event'} cannot start a bot; choose one of "
                               f"{', '.join(sorted(event_dispatch.DISPATCHABLE_EVENT_TYPES))}.")
        cfg["event_type"] = event_type
        cfg["filter"] = {str(k): v for k, v in dict(cfg.get("filter") or {}).items()}
    return cfg


def next_fire(kind: str, config: Mapping[str, Any], *, after: Optional[datetime] = None
              ) -> Optional[datetime]:
    """The next instant a clocked trigger fires, strictly after `after` (UTC); None for event and
    webhook triggers, which fire when something arrives."""
    from .workflow_schedules import next_cron

    after = after or _now()
    if kind not in ("cron", "monitor"):
        return None
    if config.get("cron"):
        return next_cron(str(config["cron"]), str(config.get("timezone") or "UTC"), after)
    return after + timedelta(seconds=int(config.get("every_seconds") or MIN_EVERY_SECONDS))


def preview(kind: str, config: Mapping[str, Any], *, count: int = 3,
            after: Optional[datetime] = None) -> list[dict]:
    """The next `count` firings (wireframe A4's "Next 3 runs"), each as an instant and in words
    in the trigger's own zone."""
    cfg = normalise_config(kind, config)
    out: list[dict] = []
    at = after or _now()
    zone = ZoneInfo(str(cfg.get("timezone") or "UTC"))
    for _ in range(max(0, min(int(count), MAX_PREVIEW))):
        nxt = next_fire(kind, cfg, after=at)
        if nxt is None:
            break
        local = nxt.astimezone(zone)
        out.append({"at": nxt.isoformat(), "label": local.strftime("%a %H:%M")})
        at = nxt
    return out


def _cron_words(expr: str) -> str:
    parts = str(expr or "").split()
    if len(parts) != 5:
        return f"cron {expr}"
    minute, hour, dom, month, dow = parts
    if minute.isdigit() and hour.isdigit() and dom == "*" and month == "*":
        at = f"{int(hour):02d}:{int(minute):02d}"
        if dow == "*":
            return f"Every day {at}"
        if dow in ("1-5", "mon-fri"):
            return f"Weekdays {at}"
        if dow.isdigit():
            return f"{_DAY[int(dow) % 7]}s {at}"
    if minute.startswith("*/") and hour == "*" and dom == "*" and month == "*" and dow == "*":
        return f"Every {minute[2:]} minutes"
    return f"cron {expr}"


def _every_words(seconds: int) -> str:
    if seconds % 3600 == 0:
        hours = seconds // 3600
        return "Every hour" if hours == 1 else f"Every {hours} hours"
    if seconds % 60 == 0:
        minutes = seconds // 60
        return "Every minute" if minutes == 1 else f"Every {minutes} minutes"
    return f"Every {seconds} s"


def words(row: Mapping[str, Any]) -> dict:
    """{when, what, detail} — the A4 row's three columns."""
    kind = str(row["kind"])
    cfg = _json(row.get("config"), {})
    if kind == "cron":
        what = (_cron_words(cfg.get("cron")) if cfg.get("cron")
                else _every_words(int(cfg.get("every_seconds") or 60)))
        return {"when": "Every", "what": f"{what} ({cfg.get('timezone') or 'UTC'})",
                "detail": row.get("name") or ""}
    if kind == "event":
        event_type = str(cfg.get("event_type") or "")
        what = _EVENT_WORDS.get(event_type, event_type.replace(".", " ").replace("_", " "))
        return {"when": "When", "what": what[:1].upper() + what[1:],
                "detail": ", ".join(f"{k}: {v}" for k, v in (cfg.get("filter") or {}).items())
                or (row.get("name") or "")}
    if kind == "webhook":
        token = str(row.get("path_token") or "")
        return {"when": "When", "what": "A webhook is called",
                "detail": f"…{INBOUND_PATH_PREFIX}{token[:4]}…" if token else ""}
    return {"when": "When", "what": cfg.get("check") or row.get("name") or "A monitor fires",
            "detail": _every_words(int(cfg.get("every_seconds") or 60))
            + (f", at most {cfg['max_cycles']} checks" if cfg.get("max_cycles") else "")}


def short_words(row: Mapping[str, Any]) -> str:
    """A1's subtitle after "Bot ·": "every new GitHub issue", "Mondays 08:00"."""
    w = words(row)
    if row["kind"] == "event":
        return w["what"][:1].lower() + w["what"][1:]
    if row["kind"] == "cron":
        return w["what"].split(" (")[0]
    return w["what"]


_CONCURRENCY_WORDS = {"skip_if_active": "If still running: skip",
                      "coalesce": "If still running: run once after",
                      "always_enqueue": "If still running: queue"}
_KIND_WORDS = {"cron": "Schedule", "event": "Event", "webhook": "Webhook", "monitor": "Monitor"}


def describe(connection: Connection, row: Mapping[str, Any], *, previews: int = 3) -> dict:
    cfg = _json(row.get("config"), {})
    dep = connection.execute(text(
        "SELECT agent_key, environment, version FROM agent_deployments WHERE id = :d"),
        {"d": row["deployment_id"]}).mappings().first() or {}
    out = {
        "id": row["id"], "kind": row["kind"], "kind_label": _KIND_WORDS.get(row["kind"]),
        "name": row.get("name") or "", **words(row),
        "config": cfg, "input": _json(row.get("input"), {}),
        "status": row["status"], "on": row["status"] == "on",
        "paused_reason": row.get("paused_reason"), "paused_by": row.get("paused_by"),
        "concurrency": row["concurrency"],
        "concurrency_label": _CONCURRENCY_WORDS.get(row["concurrency"]),
        "agent_key": dep.get("agent_key"), "environment": dep.get("environment"),
        "deployment_id": row["deployment_id"], "thread_id": row.get("thread_id"),
        "next_fire_at": _iso(row.get("next_fire_at")),
        "last_fired_at": _iso(row.get("last_fired_at")), "last_run_id": row.get("last_run_id"),
        "last_outcome": row.get("last_outcome"), "fires": int(row.get("fires") or 0),
        "skips": int(row.get("skips") or 0), "failure_streak": int(row.get("failure_streak") or 0),
        "auto_pause": bool(row.get("auto_pause")), "legacy_ref": row.get("legacy_ref"),
        "runs_as": ({"on_behalf_of": row.get("on_behalf_of"), "grant_id": row.get("grant_id")}
                    if row.get("on_behalf_of") else None),
    }
    if row["kind"] == "webhook" and row.get("path_token"):
        out["url_path"] = INBOUND_PATH_PREFIX + str(row["path_token"])
    if row["kind"] in ("cron", "monitor") and row["status"] == "on":
        out["next_fires"] = preview(row["kind"], cfg, count=previews)
    return out


# ── Reading and writing ────────────────────────────────────────────────────────────────────────

def get(connection: Connection, trigger_id: str, *, project_id: Optional[str] = None,
        lock: bool = False) -> Optional[dict]:
    row = connection.execute(text(
        "SELECT * FROM agent_triggers WHERE id = :t" + (" AND project_id = :p" if project_id else "")
        + (" FOR UPDATE" if lock else "")), {"t": trigger_id, "p": project_id}).mappings().first()
    return dict(row) if row else None


def for_deployments(connection: Connection, deployment_ids: list) -> list[dict]:
    if not deployment_ids:
        return []
    return [dict(r) for r in connection.execute(text(
        "SELECT * FROM agent_triggers WHERE deployment_id = ANY(:d) ORDER BY created_at, id"),
        {"d": list(deployment_ids)}).mappings().all()]


def _secret_name(trigger_id: str) -> str:
    return f"agent_trigger_webhook_{trigger_id}"


def _store_secret(connection: Connection, project_id: str, trigger_id: str, value: str,
                  actor: str) -> str:
    from ..config import get_settings
    from .secret_backends import default_secret_backend
    from .secrets_service import put_secret

    settings = get_settings()
    stored = put_secret(connection, settings, project_id=project_id,
                        name=_secret_name(trigger_id), value=value, secret_kind="api_token",
                        backend=default_secret_backend(settings), actor=actor)
    return str(stored["secret_ref_id"])


def create(connection: Connection, *, project_id: str, deployment_id: str, kind: str,
           actor: str, config: Optional[Mapping[str, Any]] = None,
           input: Optional[Mapping[str, Any]] = None, concurrency: str = "skip_if_active",
           name: str = "", legacy_ref: Optional[str] = None, status: str = "on",
           next_fire_at: Optional[datetime] = None, auto_pause: bool = True) -> dict:
    """A new trigger on a deployment. A webhook's signing secret is in the answer ONCE."""
    if concurrency not in CONCURRENCY:
        raise TriggerError(f"concurrency must be one of {', '.join(CONCURRENCY)}.")
    dep = connection.execute(text(
        "SELECT * FROM agent_deployments WHERE id = :d AND project_id = :p"),
        {"d": deployment_id, "p": project_id}).mappings().first()
    if dep is None:
        raise TriggerError("no such deployment in this project.", status=404)
    cfg = normalise_config(kind, config)
    trigger_id = make_id("atrg")
    from . import thread_runs

    thread_id = thread_runs.ensure_thread(connection, project_id=project_id, actor=actor,
                                          external_ref=f"trigger:{trigger_id}",
                                          agent_id=dep["agent_key"])
    connection.execute(text(
        "UPDATE agent_threads SET deployment_id = :d WHERE id = :t"),
        {"d": deployment_id, "t": thread_id})
    path_token = secrets.token_urlsafe(24) if kind == "webhook" else None
    secret = "whsec_" + secrets.token_urlsafe(32) if kind == "webhook" else None
    secret_ref = _store_secret(connection, project_id, trigger_id, secret, actor) if secret else None
    first = next_fire_at or next_fire(kind, cfg)
    link = _standing_link(connection, project_id, dep["agent_key"])
    connection.execute(text("""
        INSERT INTO agent_triggers (id, project_id, deployment_id, kind, name, config, input,
               concurrency, status, auto_pause, thread_id, next_fire_at, path_token,
               secret_ref_id, legacy_ref, created_by, on_behalf_of, grant_id)
        VALUES (:id, :p, :d, :k, :n, CAST(:c AS JSONB), CAST(:i AS JSONB), :cc, :s, :ap, :t,
                :nf, :tok, :ref, :legacy, :by, :obo, :grant)"""),
        {"id": trigger_id, "p": project_id, "d": deployment_id, "k": kind, "n": name[:120],
         "c": json.dumps(cfg), "i": json.dumps(dict(input or {})), "cc": concurrency,
         "s": status, "ap": bool(auto_pause), "t": thread_id, "nf": first, "tok": path_token,
         "ref": secret_ref, "legacy": legacy_ref, "by": actor,
         "obo": (link or {}).get("on_behalf_of"), "grant": (link or {}).get("grant_id")})
    if kind == "event" and not cfg.get("internal"):
        sub_id = make_id("esub")
        connection.execute(text("""
            INSERT INTO event_subscriptions (id, project_id, event_type, sink_kind, sink_ref,
                   enabled, config)
            VALUES (:id, :p, :e, :k, :ref, :on, '{}'::jsonb)
            ON CONFLICT (project_id, event_type, sink_kind, sink_ref) DO UPDATE
               SET enabled = EXCLUDED.enabled, updated_at = now()"""),
            {"id": sub_id, "p": project_id, "e": cfg["event_type"], "k": SINK_KIND,
             "ref": trigger_id, "on": status == "on"})
        connection.execute(text("UPDATE agent_triggers SET backing_ref = :s WHERE id = :t"),
                           {"s": sub_id, "t": trigger_id})
        _invalidate(project_id)
    emit_event(connection, project_id, "agent.trigger.created", actor, "agent_trigger",
               trigger_id, {"kind": kind, "deployment_id": deployment_id,
                            "agent_key": dep["agent_key"], "environment": dep["environment"]})
    out = describe(connection, get(connection, trigger_id) or {})
    if secret:
        out["secret"] = secret
        out["secret_shown_once"] = True
    return out


def _standing_link(connection: Connection, project_id: str, agent_key: str) -> Optional[dict]:
    """The standing grant a trigger's firings run on: the creating person's (a human session),
    live, not tied to one run, covering this agent in this project — the longest-lived. None when
    nobody is signed in here or no such grant exists (the trigger then runs unlinked)."""
    try:
        from ..identity import consent, grants, lineage

        ctx = lineage.current_context()
        if ctx is None or not ctx.is_human_session:
            return None
        agents = consent._agents(connection, project_id, [agent_key])  # noqa: SLF001
        if not agents:
            return None
        ids = [a.agent_id for a in agents]
        standing = [g for g in grants.for_person(ctx.principal.id, db=connection)
                    if not g.task_run_id and grants.covers(g, agent_ids=ids,
                                                           permissions=frozenset(),
                                                           project_id=project_id, repos=(),
                                                           run_id=None)]
        if not standing:
            return None
        g = sorted(standing, key=lambda x: (x.expires_at is None, x.expires_at))[-1]
        return {"on_behalf_of": ctx.principal.id, "grant_id": g.id}
    except Exception:  # noqa: BLE001 — no link is the unlinked rule, never a failed create
        return None


def _authority(connection: Connection, trigger: Mapping[str, Any]) -> Any:
    """The Acting a firing admits its run under: the linked standing grant, re-checked now
    (consent.schedule_acting raises AuthorityLost when it ended); None when unlinked."""
    if not trigger.get("on_behalf_of"):
        return None
    from ..identity import consent

    return consent.schedule_acting(connection, {"on_behalf_of": trigger["on_behalf_of"],
                                                "grant_id": trigger.get("grant_id")})


def _invalidate(project_id: str) -> None:
    from .event_dispatch import invalidate_subscription_cache

    invalidate_subscription_cache(project_id)


def update(connection: Connection, *, project_id: str, trigger_id: str, actor: str,
           config: Optional[Mapping[str, Any]] = None, input: Optional[Mapping[str, Any]] = None,
           concurrency: Optional[str] = None, name: Optional[str] = None,
           auto_pause: Optional[bool] = None) -> dict:
    row = get(connection, trigger_id, project_id=project_id, lock=True)
    if row is None:
        raise TriggerError("no such trigger in this project.", status=404)
    cfg = normalise_config(row["kind"], config) if config is not None else _json(row["config"], {})
    if concurrency is not None and concurrency not in CONCURRENCY:
        raise TriggerError(f"concurrency must be one of {', '.join(CONCURRENCY)}.")
    connection.execute(text("""
        UPDATE agent_triggers SET config = CAST(:c AS JSONB),
               input = COALESCE(CAST(:i AS JSONB), input),
               concurrency = COALESCE(:cc, concurrency), name = COALESCE(:n, name),
               auto_pause = COALESCE(:ap, auto_pause),
               next_fire_at = CASE WHEN :clocked THEN :nf ELSE next_fire_at END,
               updated_at = now() WHERE id = :t"""),
        {"c": json.dumps(cfg), "i": json.dumps(dict(input)) if input is not None else None,
         "cc": concurrency, "n": name, "ap": auto_pause, "t": trigger_id,
         "clocked": config is not None and row["kind"] in ("cron", "monitor"),
         "nf": next_fire(row["kind"], cfg)})
    emit_event(connection, project_id, "agent.trigger.updated", actor, "agent_trigger",
               trigger_id, {"kind": row["kind"]})
    return describe(connection, get(connection, trigger_id) or {})


def set_paused(connection: Connection, *, project_id: str, trigger_id: str, actor: str,
               paused: bool, reason: str = "") -> dict:
    """Pause or resume. A paused trigger is never selected by the next tick and `fire` refuses
    it under the row lock; resuming re-arms a clocked trigger from now (missed firings are not
    replayed)."""
    row = get(connection, trigger_id, project_id=project_id, lock=True)
    if row is None:
        raise TriggerError("no such trigger in this project.", status=404)
    cfg = _json(row["config"], {})
    if paused:
        connection.execute(text("""
            UPDATE agent_triggers SET status = 'paused', paused_reason = :r, paused_by = :by,
                   paused_at = now(), updated_at = now() WHERE id = :t"""),
            {"r": reason or "Paused by a person.", "by": actor, "t": trigger_id})
    else:
        connection.execute(text("""
            UPDATE agent_triggers SET status = 'on', paused_reason = NULL, paused_by = NULL,
                   paused_at = NULL, failure_streak = 0, cycle = CASE WHEN kind = 'monitor'
                   THEN 0 ELSE cycle END, next_fire_at = :nf, updated_at = now()
             WHERE id = :t"""), {"nf": next_fire(row["kind"], cfg), "t": trigger_id})
    if row["kind"] == "event" and row.get("backing_ref"):
        connection.execute(text(
            "UPDATE event_subscriptions SET enabled = :on, updated_at = now() WHERE id = :s"),
            {"on": not paused, "s": row["backing_ref"]})
        _invalidate(project_id)
    emit_event(connection, project_id, "agent.trigger.paused" if paused else
               "agent.trigger.resumed", actor, "agent_trigger", trigger_id,
               {"reason": reason or None})
    return describe(connection, get(connection, trigger_id) or {})


def delete(connection: Connection, *, project_id: str, trigger_id: str, actor: str) -> bool:
    row = get(connection, trigger_id, project_id=project_id, lock=True)
    if row is None:
        return False
    if row.get("backing_ref"):
        connection.execute(text("DELETE FROM event_subscriptions WHERE id = :s"),
                           {"s": row["backing_ref"]})
        _invalidate(project_id)
    connection.execute(text("DELETE FROM agent_triggers WHERE id = :t"), {"t": trigger_id})
    emit_event(connection, project_id, "agent.trigger.deleted", actor, "agent_trigger",
               trigger_id, {"kind": row["kind"]})
    return True


def rotate_secret(connection: Connection, *, project_id: str, trigger_id: str,
                  actor: str) -> dict:
    """A new signing secret for a webhook trigger, shown once; the old one stops verifying."""
    row = get(connection, trigger_id, project_id=project_id, lock=True)
    if row is None or row["kind"] != "webhook":
        raise TriggerError("no such webhook trigger in this project.", status=404)
    secret = "whsec_" + secrets.token_urlsafe(32)
    ref = _store_secret(connection, project_id, trigger_id, secret, actor)
    connection.execute(text(
        "UPDATE agent_triggers SET secret_ref_id = :r, updated_at = now() WHERE id = :t"),
        {"r": ref, "t": trigger_id})
    emit_event(connection, project_id, "agent.trigger.secret_rotated", actor, "agent_trigger",
               trigger_id, {})
    return {**describe(connection, get(connection, trigger_id) or {}), "secret": secret,
            "secret_shown_once": True}


# ── Firing ─────────────────────────────────────────────────────────────────────────────────────

def _message(trigger: Mapping[str, Any], extra: Optional[Mapping[str, Any]]) -> str:
    base = _json(trigger.get("input"), {}) or {}
    goal = base.get("goal") or base.get("message") or ""
    words_ = words(trigger)
    lines = [f"This run was started by a trigger ({trigger['kind']}: {words_['what']}), not by "
             "a person typing."]
    if goal:
        lines.append(str(goal))
    if extra:
        lines.append("What arrived:\n" + json.dumps(dict(extra), default=str)[:4000])
    return "\n\n".join(lines)


def _record(connection: Connection, trigger: Mapping[str, Any], *, outcome: str,
            reason: str = "", run_id: Optional[str] = None,
            scheduled_for: Optional[datetime] = None, dedupe_key: Optional[str] = None) -> bool:
    """One firing row. False when `dedupe_key` was already fired (nothing written)."""
    got = connection.execute(text("""
        INSERT INTO agent_trigger_fires (id, project_id, trigger_id, scheduled_for, outcome,
               reason, run_id, dedupe_key)
        VALUES (:id, :p, :t, :at, :o, :r, :run, :dk)
        ON CONFLICT (trigger_id, dedupe_key) DO NOTHING RETURNING id"""),
        {"id": make_id("atf"), "p": trigger["project_id"], "t": trigger["id"],
         "at": scheduled_for, "o": outcome, "r": reason[:300] or None, "run": run_id,
         "dk": dedupe_key}).scalar()
    return got is not None


def fire(connection: Connection, trigger_id: str, *, actor: str = "boss",
         payload: Optional[Mapping[str, Any]] = None, scheduled_for: Optional[datetime] = None,
         dedupe_key: Optional[str] = None, finalize_extra: Optional[Mapping[str, Any]] = None
         ) -> dict:
    """Fire one trigger: under its row lock, refuse a paused one, apply its concurrency, and
    admit a run on its thread (pinned to the deployment's version now). Returns {outcome,
    run_id?, job_id?, reason?}."""
    from . import agent_deployments, thread_runs

    trigger = get(connection, trigger_id, lock=True)
    if trigger is None:
        return {"outcome": "refused", "reason": "no such trigger"}
    if dedupe_key and connection.execute(text(
            "SELECT 1 FROM agent_trigger_fires WHERE trigger_id = :t AND dedupe_key = :k"),
            {"t": trigger_id, "k": dedupe_key}).scalar():
        return {"outcome": "duplicate", "reason": "already fired for this delivery"}
    from . import projects_archive

    if projects_archive.is_archived(connection, trigger["project_id"]):
        _record(connection, trigger, outcome="refused", reason="project archived",
                scheduled_for=scheduled_for, dedupe_key=dedupe_key)
        return {"outcome": "refused", "reason": "the project is archived"}
    if trigger["status"] != "on":
        _record(connection, trigger, outcome="refused", reason="paused",
                scheduled_for=scheduled_for, dedupe_key=dedupe_key)
        return {"outcome": "refused", "reason": "paused"}
    dep = agent_deployments.by_id(connection, trigger["deployment_id"])
    if dep is None or dep["status"] != "live" or (
            dep["runner"] == "deployed_agent" and not dep.get("version")):
        _record(connection, trigger, outcome="refused", reason="not deployed",
                scheduled_for=scheduled_for, dedupe_key=dedupe_key)
        return {"outcome": "refused", "reason": "the agent is not deployed"}
    thread = connection.execute(text(
        "SELECT active_run_id FROM agent_threads WHERE id = :t FOR UPDATE"),
        {"t": trigger["thread_id"]}).mappings().first()
    active = thread["active_run_id"] if thread else None
    busy = bool(active) and (connection.execute(text(
        "SELECT status FROM agent_runs WHERE id = :r"), {"r": active}).scalar()
        in thread_runs.ACTIVE)
    waiting = int(connection.execute(text(
        "SELECT count(*) FROM agent_runs WHERE thread_id = :t AND status = 'pending'"),
        {"t": trigger["thread_id"]}).scalar() or 0)
    skip_reason = ""
    if busy and trigger["concurrency"] == "skip_if_active":
        skip_reason = "the last run is still going (skip if active)"
    elif waiting and trigger["concurrency"] == "coalesce":
        skip_reason = "a run is already waiting (coalesce)"
    if skip_reason:
        _record(connection, trigger, outcome="skipped", reason=skip_reason, run_id=active,
                scheduled_for=scheduled_for, dedupe_key=dedupe_key)
        connection.execute(text(
            "UPDATE agent_triggers SET skips = skips + 1, last_outcome = 'skipped', "
            "updated_at = now() WHERE id = :t"), {"t": trigger_id})
        return {"outcome": "skipped", "reason": skip_reason, "active_run_id": active}
    messages = [{"role": "user", "content": _message(trigger, payload)}]
    extra = {"trigger_id": trigger_id, "trigger_kind": trigger["kind"],
             "trigger_input": _json(trigger.get("input"), {}),
             **({"payload": dict(payload)} if payload else {}), **dict(finalize_extra or {})}
    from ..identity import consent, lineage

    try:
        acting = _authority(connection, trigger)
    except consent.AuthorityLost as lost:
        # THE GRANT IT RAN ON ENDED: refused, and the trigger pauses until someone renews it.
        reason = f"Renew authority: the standing grant this trigger runs on ended ({lost})."
        _record(connection, trigger, outcome="refused", reason=reason,
                scheduled_for=scheduled_for, dedupe_key=dedupe_key)
        connection.execute(text(
            "UPDATE agent_triggers SET status = 'paused', paused_reason = :r, paused_by = 'boss', "
            "paused_at = now(), updated_at = now() WHERE id = :t"), {"r": reason, "t": trigger_id})
        return {"outcome": "refused", "reason": reason}
    def admit() -> dict:
        return agent_deployments.admit_run(
            connection, dep, thread_id=trigger["thread_id"], messages=messages,
            trigger=f"{trigger['kind']}:{trigger_id}", policy="enqueue", extra=extra,
            actor=actor)

    try:
        # A savepoint: a refused admission leaves no run row behind.
        with connection.begin_nested():
            if acting is not None:
                with lineage.acting(acting):
                    admitted = admit()
            else:
                admitted = admit()
    except lineage.PrincipalRequired:
        # Unlinked and unattended under enforce: nobody's authority to run it on.
        reason = "no standing grant: re-save the trigger signed in to link yours"
        _record(connection, trigger, outcome="refused", reason=reason,
                scheduled_for=scheduled_for, dedupe_key=dedupe_key)
        return {"outcome": "refused", "reason": reason}
    _record(connection, trigger, outcome="admitted", run_id=admitted["run_id"],
            scheduled_for=scheduled_for, dedupe_key=dedupe_key)
    connection.execute(text("""
        UPDATE agent_triggers SET fires = fires + 1, last_fired_at = now(), last_run_id = :r,
               last_outcome = 'admitted', cycle = cycle + 1, updated_at = now() WHERE id = :t"""),
        {"r": admitted["run_id"], "t": trigger_id})
    return {"outcome": "admitted", "run_id": admitted["run_id"], "job_id": admitted.get("job_id"),
            "status": admitted.get("status")}


#: Legacy-source triggers fire only while their migration flag is on (bots.LEGACY_SOURCES).
def _source_of(legacy_ref: Optional[str]) -> Optional[str]:
    return str(legacy_ref).split(":", 1)[0] if legacy_ref else None


def due(connection: Connection, *, now: Optional[datetime] = None, limit: int = 50,
        project_id: Optional[str] = None) -> list[dict]:
    """The clocked triggers that are due, in live (not archived) projects, locked for this tick
    (SKIP LOCKED: two API processes never fire one twice)."""
    from . import projects_archive

    rows = connection.execute(text(f"""
        SELECT * FROM agent_triggers
         WHERE status = 'on' AND kind IN ('cron', 'monitor') AND next_fire_at <= :now
           AND (CAST(:p AS TEXT) IS NULL OR project_id = CAST(:p AS TEXT))
           AND {projects_archive.live_sql('agent_triggers.project_id')}
         ORDER BY next_fire_at, id LIMIT :n FOR UPDATE SKIP LOCKED"""),
        {"now": now or _now(), "p": project_id, "n": int(limit)}).mappings().all()
    return [dict(r) for r in rows]


def tick(connection: Connection, *, now: Optional[datetime] = None, limit: int = 50,
         project_id: Optional[str] = None, sources: Optional[set] = None,
         checks: Optional[Mapping[str, Any]] = None) -> dict:
    """THE BOTS' ONE CLOCK: fire every clocked trigger that is due, then re-arm it from its
    due time (a long outage fires once, not a burst). A legacy-source trigger fires only when
    its source is in `sources` (its migration flag is on). A monitor stops itself at its cycle
    cap. `checks` maps a config `check_hook` to (connection, trigger) -> reason|None: the
    trigger fires only when the hook says it is due (a focused agent's own due rule)."""
    current = now or _now()
    fired, skipped = [], []
    for trigger in due(connection, now=current, limit=limit, project_id=project_id):
        source = _source_of(trigger.get("legacy_ref"))
        if source and source not in (sources or set()):
            continue
        cfg = _json(trigger["config"], {})
        scheduled = trigger["next_fire_at"]
        following = next_fire(trigger["kind"], cfg, after=max(current, scheduled))
        hook = (checks or {}).get(str(cfg.get("check_hook") or "")) if cfg.get("check_hook") \
            else None
        reason = hook(connection, trigger) if hook is not None else "due"
        if not reason:
            connection.execute(text(
                "UPDATE agent_triggers SET next_fire_at = :n, updated_at = now() WHERE id = :t"),
                {"n": following, "t": trigger["id"]})
            skipped.append({"trigger_id": trigger["id"], "reason": "not_due"})
            continue
        got = fire(connection, trigger["id"], scheduled_for=scheduled,
                   dedupe_key=f"clock:{scheduled.isoformat()}",
                   finalize_extra={"due_reason": reason} if hook is not None else None)
        stop = (trigger["kind"] == "monitor" and int(cfg.get("max_cycles") or 0)
                and int(trigger["cycle"] or 0) + (1 if got["outcome"] == "admitted" else 0)
                >= int(cfg["max_cycles"]))
        connection.execute(text("""
            UPDATE agent_triggers SET next_fire_at = :n,
                   status = CASE WHEN :stop THEN 'paused' ELSE status END,
                   paused_reason = CASE WHEN :stop THEN :why ELSE paused_reason END,
                   paused_at = CASE WHEN :stop THEN now() ELSE paused_at END,
                   updated_at = now() WHERE id = :t"""),
            {"n": following, "stop": bool(stop), "t": trigger["id"],
             "why": f"Finished its {cfg.get('max_cycles')} checks."})
        entry = {"trigger_id": trigger["id"], "project_id": trigger["project_id"],
                 "legacy_ref": trigger.get("legacy_ref"), **got}
        (fired if got["outcome"] == "admitted" else skipped).append(entry)
    return {"fired": fired, "skipped": skipped}


def on_run_settled(connection: Connection, run_id: str) -> None:
    """A trigger's run ended: its failure streak grows on a failure and clears on a success."""
    row = connection.execute(text(
        "SELECT trigger_id, status FROM agent_runs WHERE id = :r"), {"r": run_id}
    ).mappings().first()
    if row is None or not row["trigger_id"]:
        return
    status = str(row["status"] or "")
    if status == "failed":
        connection.execute(text(
            "UPDATE agent_triggers SET failure_streak = failure_streak + 1, last_outcome = "
            "'failed', updated_at = now() WHERE id = :t"), {"t": row["trigger_id"]})
    elif status == "completed":
        connection.execute(text(
            "UPDATE agent_triggers SET failure_streak = 0, last_outcome = 'completed', "
            "updated_at = now() WHERE id = :t"), {"t": row["trigger_id"]})


# ── The event sink and the inbound webhook ─────────────────────────────────────────────────────

def on_event(connection: Connection, subscription: Mapping[str, Any], *, event_id: str,
             event_type: str, entity_type: str, entity_id: str, data: Mapping[str, Any]
             ) -> Optional[dict]:
    """event_dispatch's `agent_run` sink: fire the subscribed trigger once per event (when its
    filter matches the event's data)."""
    trigger = get(connection, str(subscription["sink_ref"]))
    if trigger is None:
        return None
    flt = (_json(trigger["config"], {}) or {}).get("filter") or {}
    if any(str((data or {}).get(k)) != str(v) for k, v in flt.items()):
        return {"kind": SINK_KIND, "trigger_id": trigger["id"], "outcome": "filtered"}
    got = fire(connection, trigger["id"], payload={"event_type": event_type,
                                                    "entity_type": entity_type,
                                                    "entity_id": entity_id, **dict(data or {})},
               dedupe_key=f"event:{event_id}")
    return {"kind": SINK_KIND, "trigger_id": trigger["id"], **got}


def receive(connection: Connection, path_token: str, raw_body: bytes,
            headers: Mapping[str, str], *, now: Optional[float] = None) -> dict:
    """One inbound webhook delivery: verified with workflow_webhooks' scheme (HMAC-SHA256 over
    `<timestamp>.<body>`, a 5-minute window, the delivery id or the body hash as the dedupe
    key), then fired. Never raises for anything a sender controls."""
    from .workflow_webhooks import (DELIVERY_HEADER, MAX_BODY_BYTES, SIGNATURE_HEADER,
                                    TIMESTAMP_HEADER, TIMESTAMP_TOLERANCE_SECONDS,
                                    _signature_matches)

    token = str(path_token or "").strip()
    trigger = None
    if token and len(token) <= 128:
        trigger = connection.execute(text(
            "SELECT * FROM agent_triggers WHERE path_token = :t AND kind = 'webhook'"),
            {"t": token}).mappings().first()
    if trigger is None:
        return {"status": "not_found"}
    trigger = dict(trigger)

    def refuse(reason: str) -> dict:
        _record(connection, trigger, outcome="refused", reason=reason)
        return {"status": "refused", "reason": reason}

    if len(raw_body) > MAX_BODY_BYTES:
        return refuse("too_large")
    signature = str(headers.get(SIGNATURE_HEADER) or "").strip()
    stamp = str(headers.get(TIMESTAMP_HEADER) or "").strip()
    if not signature or not stamp:
        return refuse("missing_signature")
    try:
        sent_at = int(stamp)
    except ValueError:
        return refuse("stale_timestamp")
    if abs((time.time() if now is None else now) - sent_at) > TIMESTAMP_TOLERANCE_SECONDS:
        return refuse("stale_timestamp")
    try:
        from ..config import get_settings
        from .secret_backends import resolve_secret_ref

        secret = resolve_secret_ref(connection, trigger["secret_ref_id"], get_settings(),
                                    project_id=trigger["project_id"])
    except Exception:  # noqa: BLE001 — an unreadable secret verifies nothing
        return refuse("bad_signature")
    if not _signature_matches(secret, stamp, raw_body, signature):
        return refuse("bad_signature")
    try:
        payload = json.loads(raw_body.decode("utf-8") or "{}")
    except (UnicodeDecodeError, ValueError):
        return refuse("invalid_json")
    delivery = str(headers.get(DELIVERY_HEADER) or "").strip()[:200]
    dedupe = "webhook:" + (delivery or hashlib.sha256(
        stamp.encode() + b"." + raw_body).hexdigest())
    got = fire(connection, trigger["id"], actor="webhook",
               payload=payload if isinstance(payload, dict) else {"body": payload},
               dedupe_key=dedupe)
    status = {"admitted": "started", "duplicate": "duplicate"}.get(got["outcome"], got["outcome"])
    return {"status": status, "run_id": got.get("run_id"), "reason": got.get("reason")}


__all__ = ["CONCURRENCY", "FAILING_STREAK", "INBOUND_PATH_PREFIX", "KINDS", "MIN_EVERY_SECONDS",
           "SINK_KIND", "TriggerError", "create", "delete", "describe", "due", "fire",
           "for_deployments", "get", "next_fire", "normalise_config", "on_event",
           "on_run_settled", "preview", "receive", "rotate_secret", "set_paused", "short_words",
           "tick", "update", "words"]
