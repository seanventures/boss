"""THE INFRA AGENT (BOSSAPP-1157; boss-infra-agent-lld.md §3, §8) — Layer 2.

A P3 agent deployment (`runtime: local`, `replicas: 2`) that owns storage incidents from first
signal to final report: it reads the measurements and the forecast, explains the cause in plain
words, orders the plan, asks (through the executor's card) only for irreversible work, proposes a
rule when a cause repeats, and posts the report. It NEVER blocks the deterministic executor: the
watch opens incidents and the executor runs tiers 1–2 whether or not a model answers. Its model
spend is capped per incident (`limits.run_usd`, $1) and per day ($5, checked here before a run
is admitted); at the cap the rules still run and the console says "explanations shortened".

Until `triggers` land in the spec (P3), the watch calls `admit_for_incident` directly — the same
bridge the Fleet doctor will use (LLD §3).
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Optional

from sqlalchemy import text

_log = logging.getLogger("boss.infra_agent")

KEY = "infra_agent"
NAME = "Infra Agent"
ENVIRONMENT = "prod"
MAX_USD_PER_INCIDENT = 1.0
MAX_USD_PER_DAY = 5.0
REPLICAS = 2

AGENT_DOCUMENT: dict[str, Any] = {
    "apiVersion": "boss.agent/v1",
    "kind": "Agent",
    "metadata": {"key": KEY, "name": NAME, "team": "platform",
                 "description": "Keeps every BOSS host's storage healthy: detect, pause, clean "
                                "safest-first, ask only for irreversible work, resume, report, learn.",
                 "labels": {"system": "true", "epic": "BOSSAPP-1145"}},
    "spec": {
        "mission": "Keep every BOSS host's storage healthy. Detect pressure, pause work safely, "
                   "clean safest first, ask a person only for what cannot be undone, resume, "
                   "report in plain words, and turn a repeated cause into a permanent rule.",
        "outcomes": ["A storage incident explained in one plain sentence",
                     "A cleanup plan in tier order, every step receipted",
                     "One approval card at most, carrying the rule proposal",
                     "A short report: cause, removed, paused, learned"],
        "boundaries": {
            "must": ["Read storage_inspect before explaining anything",
                     "Let the deterministic executor run tiers 1–2; never wait for it",
                     "Keep every label under five words; prose goes in the report",
                     "Stop once free space is above the target"],
            "must_not": ["Run a shell command or ask for one",
                         "Delete or propose deleting anything without a BOSS label",
                         "Release the hold while the host is below target",
                         "Claim an action happened without its receipt"]},
        "decision_rights": {"can_decide": ["The order of tier 1 and 2 steps", "The cause sentence",
                                           "Whether to propose a rule"],
                            "must_escalate": ["Any irreversible deletion", "Anything unlabelled",
                                              "A helper unreachable for ten minutes"]},
        "prompts": {
            "system": ("You are BOSS's Infra Agent. A host's storage is under pressure. First call "
                       "storage_inspect. Then: (1) write the cause in one plain sentence with "
                       "storage_report; (2) call storage_run_step to move the cleanup along while "
                       "you work — tiers 1 and 2 are automatic, tier 3 opens a card you never "
                       "bypass; (3) if the same cause_key was seen before, call storage_propose_rule "
                       "with the concrete rule; (4) finish with storage_report notes: what was "
                       "removed and why, what was paused, what should change. Short sentences. "
                       "No shell. Never touch anything unlabelled."),
            "job": "Own storage incident {incident_id} on {host_label} until it is resolved."},
        "model": {"tier": "balanced", "need": ["reasoning"]},
        "tools": {"skills": ["storage.steward"], "exposure": "scoped",
                  "forbidden": ["run_on_host", "github.*", "slack.post_real", "infra.*"]},
        "autonomy": {"level": 3,
                     "escalate": ["unlabelled_target", "target_not_reached_after_tier2",
                                  "helper_unreachable_10m"]},
        "coordination": {"enabled": False},
        "limits": {"max_steps": 12, "max_calls": 16, "timeout_seconds": 900, "max_retries": 1,
                   "run_usd": MAX_USD_PER_INCIDENT},
        "comms": {"tone": "direct, operational, plain words"},
        "persona": {"version": 1, "callsign": "Steward",
                    "tagline": "Safest first, and stop when enough.",
                    "about_me": "I look after the disks BOSS runs on. When one fills up I pause "
                                "work at a checkpoint, clean what can be rebuilt, ask once for what "
                                "cannot be undone, and turn the lesson into a rule.",
                    "voice": {"blunt": 2, "thorough": 2, "bold": 1, "casual": 0},
                    "avatar": {"kit": "boss-v1", "seed": "infra"}},
    },
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def spend_today_usd(connection: Any) -> float:
    """The Infra Agent's model spend since midnight UTC (llm_calls by its runs)."""
    try:
        micros = connection.execute(text("""
            SELECT COALESCE(sum(l.cost_usd_micros), 0) FROM llm_calls l
              JOIN agent_runs r ON r.id = l.run_id
             WHERE r.agent_key = :k AND r.created_at >= date_trunc('day', now() AT TIME ZONE 'UTC')
               AT TIME ZONE 'UTC'"""), {"k": KEY}).scalar() or 0
        return float(micros) / 1e6
    except Exception:  # noqa: BLE001 — no telemetry tables: nothing spent that we can see
        return 0.0


def ensure_deployed(connection: Any, project_id: str, *, actor: str = "boss") -> Optional[dict[str, Any]]:
    """The agent exists, is published and is deployed to prod on the local runtime with two
    replicas. Idempotent; returns the deployment or None when the agent platform is absent."""
    from ..agents import versions
    from . import agent_deployments

    try:
        if versions.active(connection, project_id, KEY) is None:
            if versions._role_row(connection, project_id, KEY) is None:  # noqa: SLF001 — the one existence check
                versions.create_from_spec(connection, project_id, AGENT_DOCUMENT, actor=actor, source="shipped")
            else:
                versions.save_and_publish(connection, project_id, AGENT_DOCUMENT, actor=actor, key=KEY,
                                          source="shipped")
        current = agent_deployments.get(connection, project_id, KEY, ENVIRONMENT)
        if current is None or current.get("status") != "live":
            active = versions.active(connection, project_id, KEY)
            current = agent_deployments.deploy(connection, project_id, KEY, ENVIRONMENT, actor=actor,
                                               version=int(active["version"]), source="shipped",
                                               display_name=NAME,
                                               metadata={"system": True, "epic": "BOSSAPP-1145"})
        if (current.get("runtime") or "local") != "local" or int(current.get("replicas") or 0) < REPLICAS:
            try:
                agent_deployments.set_runtime(connection, project_id, KEY, ENVIRONMENT, actor=actor,
                                              runtime="local", replicas=REPLICAS)
            except Exception:  # noqa: BLE001 — an older deployments table has no runtime columns
                _log.debug("infra agent runtime not set", exc_info=True)
        return agent_deployments.get(connection, project_id, KEY, ENVIRONMENT)
    except Exception:  # noqa: BLE001 — the executor never depends on the agent existing
        _log.info("infra agent not deployed", exc_info=True)
        return None


def admit_for_incident(connection: Any, incident: Mapping[str, Any], *, actor: str = "boss") -> Optional[str]:
    """One run per incident, on the incident's own thread, under the daily cap. Returns the run
    id, or None (cap reached, platform absent, already running)."""
    from . import agent_deployments, thread_runs

    project_id = incident.get("project_id")
    if not project_id:
        return None
    if incident.get("agent_run_id"):
        status = connection.execute(text("SELECT status FROM agent_runs WHERE id = :r"),
                                    {"r": incident["agent_run_id"]}).scalar()
        if status in ("queued", "running", "pending", "awaiting_approval"):
            return str(incident["agent_run_id"])
    if spend_today_usd(connection) >= MAX_USD_PER_DAY:
        _note(connection, incident, "daily cap reached: explanations shortened; the rules still run")
        return None
    deployment = ensure_deployed(connection, str(project_id), actor=actor)
    if deployment is None:
        return None
    thread_id = thread_runs.ensure_thread(connection, project_id=str(project_id), actor=actor,
                                          external_ref=f"storage_incident:{incident['id']}", agent_id=KEY)
    brief = (f"Storage incident {incident['id']} on {incident.get('host_label') or incident['host_id']} "
             f"is {incident.get('severity') or 'warn'}: {incident.get('cause_text') or 'cause not yet known'}. "
             f"Free {float(incident.get('free_pct_now') or 0):.0f} %, target {float(incident.get('target_pct') or 25):.0f} %. "
             "Inspect, explain, move the cleanup along, propose a rule if this cause repeated, report.")
    admitted = agent_deployments.admit_run(
        connection, deployment, thread_id=thread_id, messages=[{"role": "user", "content": brief}],
        trigger="event", actor=actor,
        extra={"incident_id": incident["id"], "host_id": incident["host_id"], "event": "storage.pressure",
               "max_usd": MAX_USD_PER_INCIDENT})
    return str(admitted.get("run_id") or "") or None


def on_resolved(connection: Any, incident: Mapping[str, Any], report: Mapping[str, Any]) -> None:
    """Book the agent's spend on the incident so the card and the report can show it."""
    run_id = incident.get("agent_run_id")
    if not run_id:
        return
    try:
        micros = connection.execute(text(
            "SELECT COALESCE(sum(cost_usd_micros), 0) FROM llm_calls WHERE run_id = :r"),
            {"r": run_id}).scalar() or 0
        connection.execute(text("UPDATE storage_incidents SET spend_usd = :u WHERE id = :id"),
                           {"u": float(micros) / 1e6, "id": incident["id"]})
    except Exception:  # noqa: BLE001
        _log.debug("infra agent spend not booked", exc_info=True)


def _note(connection: Any, incident: Mapping[str, Any], note: str) -> None:
    try:
        connection.execute(text(
            "UPDATE storage_incidents SET report = COALESCE(report, '{}'::jsonb) || "
            "jsonb_build_object('agent_note', CAST(:n AS text)) WHERE id = :id"),
            {"n": note[:300], "id": incident["id"]})
    except Exception:  # noqa: BLE001
        pass


__all__ = ["AGENT_DOCUMENT", "ENVIRONMENT", "KEY", "MAX_USD_PER_DAY", "MAX_USD_PER_INCIDENT", "NAME",
           "REPLICAS", "admit_for_incident", "ensure_deployed", "on_resolved", "spend_today_usd"]
