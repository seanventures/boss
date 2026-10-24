"""Focused always-on agents — easy builder + validated duty loop.

Agent Studio already versions full roles. This service is the thin path:
pick a focus template, start the agent, keep it working.

Hard rules (same as Metatron captain):
  * The playbook proposes; the AGENT decides. Which validated move (if any) a tick applies is
    the agent's turn through the one agent pipeline (AgentService.decide, BOSSAPP-759). No
    model, or unsure: nothing is applied.
  * Every action is validated before apply.
  * Decisions and deadletters never auto-apply.
  * BOSS self-change is a PR, never a silent write.
  * Spendy apply needs a Receipt with a unique dedupe_key.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..config import Settings
from .core import emit_event, make_id
from .metatron_captain import (
    BLOCKED_APPLY_KINDS,
    MetatronCaptain,
    findings_from_snapshot,
    proposed_moves_from_findings,
    quality_improve_directive,
    scorecard_from_snapshot,
    scorecard_quality_gaps,
    validate_move,
    verify_from_snapshots,
)
from .receipts import ReceiptService


FOCUS_KINDS = frozenset({
    "preview_keeper",
    "queue_helper",
    "factory_driver",
    "reviewer",
    "custom",
})

DUTY_MODES = frozenset({"always_on", "on_demand", "hybrid"})
DUTY_STATUSES = frozenset({"draft", "active", "paused", "retired"})

APPLY_ACTIONS = frozenset({"restart_preview", "launch_factory_job"})
ALLOWED_ACTIONS = APPLY_ACTIONS | frozenset({"propose_review"})

MIN_INTERVAL_SECONDS = 60
MAX_INTERVAL_SECONDS = 3600
DEFAULT_INTERVAL_SECONDS = 120

TEMPLATES: dict[str, dict[str, Any]] = {
    "preview_keeper": {
        "name": "Preview keeper",
        "focus": "Keep verify-green apps previewed and reachable.",
        "allowed_actions": ["restart_preview"],
        "wake_on": ["app_preview.expired", "metatron.watch.ticked", "metatron.overwatch.completed"],
        "interval_seconds": 90,
        "mode": "always_on",
    },
    "queue_helper": {
        "name": "Queue helper",
        "focus": "Keep the queue visible and unstick safe factory gaps.",
        "allowed_actions": ["launch_factory_job"],
        "wake_on": ["job.deadlettered", "decision.created", "code_task.failed"],
        "interval_seconds": 180,
        "mode": "hybrid",
    },
    "factory_driver": {
        "name": "Factory driver",
        "focus": "Pursue the stated objective through governed factory jobs.",
        "allowed_actions": ["launch_factory_job"],
        "wake_on": ["code_task.completed", "code_task.failed", "metatron.overwatch.completed"],
        "interval_seconds": 180,
        "mode": "always_on",
    },
    "reviewer": {
        "name": "Reviewer",
        "focus": "Review completed factory work and surface findings. Never merge.",
        "allowed_actions": ["propose_review"],
        "wake_on": ["code_task.completed", "github.pr.opened"],
        "interval_seconds": 180,
        "mode": "hybrid",
    },
    "custom": {
        "name": "Custom agent",
        "focus": "",
        "allowed_actions": [],
        "wake_on": [],
        "interval_seconds": DEFAULT_INTERVAL_SECONDS,
        "mode": "on_demand",
    },
}

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS focused_agents (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  agent_key TEXT NOT NULL,
  name TEXT NOT NULL,
  focus TEXT NOT NULL,
  kind TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'draft',
  mode TEXT NOT NULL DEFAULT 'hybrid',
  interval_seconds INTEGER NOT NULL DEFAULT 120,
  wake_on JSONB NOT NULL DEFAULT '[]'::jsonb,
  allowed_actions JSONB NOT NULL DEFAULT '[]'::jsonb,
  last_tick_at TIMESTAMPTZ,
  last_wake_at TIMESTAMPTZ,
  last_status TEXT,
  last_score NUMERIC,
  last_evidence JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_by TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(project_id, agent_key)
);
CREATE INDEX IF NOT EXISTS idx_focused_agents_project
  ON focused_agents(project_id, status, updated_at DESC);
CREATE TABLE IF NOT EXISTS focused_agent_ticks (
  id TEXT PRIMARY KEY,
  agent_id TEXT NOT NULL REFERENCES focused_agents(id) ON DELETE CASCADE,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  trigger TEXT NOT NULL,
  observed JSONB NOT NULL DEFAULT '{}'::jsonb,
  planned JSONB NOT NULL DEFAULT '[]'::jsonb,
  validation JSONB NOT NULL DEFAULT '{}'::jsonb,
  applied JSONB NOT NULL DEFAULT '[]'::jsonb,
  rejected JSONB NOT NULL DEFAULT '[]'::jsonb,
  verify JSONB NOT NULL DEFAULT '{}'::jsonb,
  receipt_id TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_focused_agent_ticks_agent
  ON focused_agent_ticks(agent_id, created_at DESC);
"""


def slugify_agent_key(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (name or "").strip().lower()).strip("-")
    return (slug or "focused-agent")[:64]


def template_for(kind: str) -> dict[str, Any]:
    return dict(TEMPLATES.get(kind) or TEMPLATES["custom"])


def normalize_actions(raw: Any) -> list[str]:
    items = raw if isinstance(raw, list) else []
    out: list[str] = []
    for item in items:
        action = str(item or "").strip()
        if action in ALLOWED_ACTIONS and action not in out:
            out.append(action)
    return out


def normalize_wake_on(raw: Any) -> list[str]:
    items = raw if isinstance(raw, list) else []
    out: list[str] = []
    for item in items:
        event = str(item or "").strip()
        if event and event not in out and len(event) <= 80:
            out.append(event)
    return out[:16]


def validate_spec(
    *,
    name: str,
    focus: str,
    kind: str,
    mode: str,
    interval_seconds: int,
    allowed_actions: list[str],
) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []

    def check(label: str, ok: bool, detail: str) -> None:
        checks.append({"name": label, "ok": ok, "detail": detail})

    check("nonempty_name", bool(name.strip()), "name is required" if not name.strip() else "ok")
    check("nonempty_focus", bool(focus.strip()), "focus is required" if not focus.strip() else "ok")
    check("known_kind", kind in FOCUS_KINDS, f"kind={kind}")
    check("known_mode", mode in DUTY_MODES, f"mode={mode}")
    check(
        "interval_bounded",
        MIN_INTERVAL_SECONDS <= int(interval_seconds) <= MAX_INTERVAL_SECONDS,
        f"interval={interval_seconds}",
    )
    unknown = [action for action in allowed_actions if action not in ALLOWED_ACTIONS]
    check("known_actions", not unknown, f"unknown={unknown}" if unknown else "ok")
    ok = all(item["ok"] for item in checks)
    return {"ok": ok, "checks": checks, "failed": [item["name"] for item in checks if not item["ok"]]}


def plan_moves(
    *,
    kind: str,
    focus: str,
    allowed_actions: list[str],
    snapshot: dict[str, Any],
    auto: bool,
) -> list[dict[str, Any]]:
    """Map a snapshot + focus into proposed moves, then keep only this agent's actions."""
    synthetic_goals: list[dict[str, Any]] = []
    if kind in {"factory_driver", "queue_helper", "custom"} and "launch_factory_job" in allowed_actions:
        synthetic_goals.append({
            "id": "focus",
            "title": focus,
            "kind": "working_software",
            "status": "active",
            "auto_drive": True,
        })
    findings = findings_from_snapshot(snapshot, synthetic_goals)
    if kind == "reviewer":
        completed = int(snapshot.get("completed_verify_green") or 0)
        if completed:
            findings.append({
                "id": "review_due",
                "severity": "info",
                "title": f"Review {completed} verify-green run(s) for '{focus}'",
                "action": "hold",
                "auto_safe": True,
            })
    moves = proposed_moves_from_findings(findings, auto=bool(auto))
    planned: list[dict[str, Any]] = []
    for move in moves:
        action = str(move.get("kind") or "")
        if action in BLOCKED_APPLY_KINDS:
            planned.append(move)
            continue
        if action in allowed_actions:
            planned.append(move)
            continue
        if kind == "reviewer" and action == "propose":
            planned.append({
                **move,
                "kind": "propose_review",
                "status": "proposed",
                "requires_human": True,
                "apply": False,
            })
    if kind == "reviewer" and not any(item.get("kind") == "propose_review" for item in planned):
        planned.append({
            "kind": "propose_review",
            "status": "proposed",
            "detail": f"Review focus: {focus}",
            "requires_human": True,
            "apply": False,
        })
    gaps = scorecard_quality_gaps(scorecard_from_snapshot(snapshot))
    if gaps and "launch_factory_job" in allowed_actions:
        directive = quality_improve_directive(gaps, focus=focus)
        stamped = False
        for move in planned:
            if move.get("kind") == "launch_factory_job":
                move["detail"] = directive
                move["directive"] = directive
                stamped = True
        if not stamped:
            planned.append({
                "kind": "launch_factory_job",
                "status": "queued" if auto else "proposed",
                "detail": directive,
                "directive": directive,
                "finding_id": "scorecard_gaps",
                "apply": bool(auto),
            })
    return planned


class FocusedAgentService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.captain = MetatronCaptain(settings)
        self.receipts = ReceiptService()
        self._schema_ready = False

    def ensure_schema(self, connection: Connection) -> None:
        if self._schema_ready:
            return
        for statement in (chunk.strip() for chunk in SCHEMA_SQL.split(";")):
            if statement:
                connection.exec_driver_sql(statement)
        self._schema_ready = True

    def create(
        self,
        connection: Connection,
        *,
        project_id: str,
        actor: str,
        kind: str = "custom",
        name: str = "",
        focus: str = "",
        mode: Optional[str] = None,
        interval_seconds: Optional[int] = None,
        allowed_actions: Optional[list[str]] = None,
        wake_on: Optional[list[str]] = None,
        start: bool = True,
    ) -> dict[str, Any]:
        self.ensure_schema(connection)
        resolved_kind = kind if kind in FOCUS_KINDS else "custom"
        template = template_for(resolved_kind)
        resolved_name = (name or template["name"]).strip()[:120]
        resolved_focus = (focus or template["focus"]).strip()[:500]
        resolved_mode = mode if mode in DUTY_MODES else str(template["mode"])
        resolved_interval = int(interval_seconds or template["interval_seconds"])
        resolved_actions = normalize_actions(
            allowed_actions if allowed_actions is not None else template["allowed_actions"]
        )
        resolved_wake = normalize_wake_on(
            wake_on if wake_on is not None else template["wake_on"]
        )
        verdict = validate_spec(
            name=resolved_name,
            focus=resolved_focus,
            kind=resolved_kind,
            mode=resolved_mode,
            interval_seconds=resolved_interval,
            allowed_actions=resolved_actions,
        )
        if not verdict["ok"]:
            raise ValueError("focused agent rejected: " + ", ".join(verdict["failed"]))
        agent_key = slugify_agent_key(resolved_name)
        existing = self._find_by_key(connection, project_id, agent_key)
        suffix = 2
        while existing is not None:
            agent_key = f"{slugify_agent_key(resolved_name)}-{suffix}"[:64]
            existing = self._find_by_key(connection, project_id, agent_key)
            suffix += 1
        status = "active" if start else "paused"
        row = connection.execute(
            text(
                """
                INSERT INTO focused_agents (
                  id, project_id, agent_key, name, focus, kind, status, mode,
                  interval_seconds, wake_on, allowed_actions, created_by
                )
                VALUES (
                  :id, :p, :key, :name, :focus, :kind, :status, :mode,
                  :interval, CAST(:wake AS JSONB), CAST(:actions AS JSONB), :actor
                )
                RETURNING *
                """
            ),
            {
                "id": make_id("fagent"),
                "p": project_id,
                "key": agent_key,
                "name": resolved_name,
                "focus": resolved_focus,
                "kind": resolved_kind,
                "status": status,
                "mode": resolved_mode,
                "interval": resolved_interval,
                "wake": json.dumps(resolved_wake),
                "actions": json.dumps(resolved_actions),
                "actor": actor,
            },
        ).mappings().one()
        emit_event(
            connection, project_id, "focused_agent.created", actor,
            "focused_agent", row["id"],
            {"kind": resolved_kind, "mode": resolved_mode, "start": bool(start)},
        )
        agent = self._row(row)
        agent["validation"] = verdict
        return agent

    def list_agents(self, connection: Connection, project_id: str) -> list[dict[str, Any]]:
        self.ensure_schema(connection)
        rows = connection.execute(
            text(
                """
                SELECT * FROM focused_agents
                WHERE project_id=:p
                ORDER BY updated_at DESC
                """
            ),
            {"p": project_id},
        ).mappings().all()
        return [self._row(row) for row in rows]

    def get(self, connection: Connection, project_id: str, agent_id: str) -> dict[str, Any]:
        self.ensure_schema(connection)
        row = connection.execute(
            text("SELECT * FROM focused_agents WHERE id=:id AND project_id=:p"),
            {"id": agent_id, "p": project_id},
        ).mappings().first()
        if row is None:
            raise ValueError("focused agent not found")
        agent = self._row(row)
        agent["ticks"] = self.list_ticks(connection, project_id, agent_id, limit=8)
        return agent

    def set_status(
        self, connection: Connection, *, project_id: str, agent_id: str, actor: str, status: str,
    ) -> dict[str, Any]:
        if status not in DUTY_STATUSES:
            raise ValueError(f"invalid status {status}")
        self.ensure_schema(connection)
        connection.execute(
            text(
                """
                UPDATE focused_agents
                SET status=:s, updated_at=now()
                WHERE id=:id AND project_id=:p
                """
            ),
            {"s": status, "id": agent_id, "p": project_id},
        )
        emit_event(
            connection, project_id, "focused_agent.status", actor,
            "focused_agent", agent_id, {"status": status},
        )
        return self.get(connection, project_id, agent_id)

    def list_ticks(
        self, connection: Connection, project_id: str, agent_id: str, limit: int = 8,
    ) -> list[dict[str, Any]]:
        self.ensure_schema(connection)
        rows = connection.execute(
            text(
                """
                SELECT * FROM focused_agent_ticks
                WHERE project_id=:p AND agent_id=:id
                ORDER BY created_at DESC LIMIT :n
                """
            ),
            {"p": project_id, "id": agent_id, "n": limit},
        ).mappings().all()
        return [self._row(row) for row in rows]

    def tick_due(
        self, connection: Connection, *, project_id: str, actor: str,
    ) -> dict[str, Any]:
        """Always-on / hybrid agents whose interval elapsed or a wake event arrived."""
        from . import projects_archive

        if projects_archive.is_archived(connection, project_id):
            return {"ran": [], "skipped": [], "active": 0, **projects_archive.skipped(project_id)}
        self.ensure_schema(connection)
        agents = [
            agent for agent in self.list_agents(connection, project_id)
            if agent.get("status") == "active"
        ]
        ran: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        for agent in agents:
            reason = self._due_reason(connection, agent)
            if not reason:
                skipped.append({"id": agent["id"], "reason": "not_due"})
                continue
            ran.append(self.tick_one(
                connection, project_id=project_id, actor=actor,
                agent_id=agent["id"], trigger=reason,
            ))
        return {
            "project_id": project_id,
            "ran": ran,
            "skipped": skipped,
            "active": len(agents),
        }

    def wake(
        self, connection: Connection, *, project_id: str, actor: str, agent_id: str,
        reason: str = "manual",
    ) -> dict[str, Any]:
        return self.tick_one(
            connection, project_id=project_id, actor=actor,
            agent_id=agent_id, trigger=f"wake:{reason[:40]}",
        )

    def tick_one(
        self, connection: Connection, *, project_id: str, actor: str,
        agent_id: str, trigger: str,
    ) -> dict[str, Any]:
        """Observe → plan → validate → apply allowed → verify."""
        self.ensure_schema(connection)
        agent = self.get(connection, project_id, agent_id)
        if agent.get("status") not in {"active", "draft"}:
            raise ValueError("focused agent is not runnable")
        before = self.captain.gather_snapshot(connection, project_id)
        allowed = normalize_actions(agent.get("allowed_actions"))
        auto = agent.get("mode") in {"always_on", "hybrid"} or trigger.startswith("wake:")
        planned = plan_moves(
            kind=str(agent.get("kind") or "custom"),
            focus=str(agent.get("focus") or ""),
            allowed_actions=allowed,
            snapshot=before,
            auto=auto,
        )
        verify_run = self.captain._latest_verify_green_without_preview(connection, project_id)
        in_flight = int(before.get("in_flight") or 0)
        budget_state = str(before.get("budget_state") or "unknown")
        repo = f"{self.settings.target_github_owner}/{self.settings.target_github_repo}"
        applied: list[dict[str, Any]] = []
        rejected: list[dict[str, Any]] = []
        applicable: list[dict[str, Any]] = []
        for move in planned:
            verdict = validate_move(
                move, before, auto=bool(auto), in_flight=in_flight,
                budget_state=budget_state, verify_green_run_id=verify_run,
                self_mod_repo=repo,
            )
            move["validation"] = verdict
            if not (auto and move.get("apply") and verdict["ok"] and move.get("kind") in APPLY_ACTIONS):
                if move.get("apply") and not verdict["ok"]:
                    move["status"] = "rejected"
                    rejected.append(move)
                continue
            applicable.append(move)
        # THE AGENT DECIDES WHICH MOVE, THROUGH THE ONE PIPELINE (BOSSAPP-759). The playbook
        # above proposes and validates; whether to apply one of them now is the agent's turn
        # (AgentService.decide), with its duty, the snapshot and the validated moves as input.
        # No model, or unsure: nothing is applied this tick, and the tick says why.
        decision = self._decide_move(connection, project_id=project_id, actor=actor,
                                     agent=agent, trigger=trigger, snapshot=before,
                                     applicable=applicable) if applicable else None
        chosen = (decision or {}).get("move")
        for move in applicable:
            if move is not chosen:
                move["status"] = "skipped"
                move["skipped_because"] = ((decision or {}).get("word")
                                           or "the agent chose another move")
                continue
            focus = str(agent.get("focus") or "")
            move_directive = str(move.get("directive") or "")
            if move.get("kind") == "launch_factory_job" and move_directive:
                apply_directive = move_directive
            else:
                apply_directive = focus
            result = self.captain._apply_validated_move(
                connection, project_id=project_id, actor=actor,
                move=move, verify_run=verify_run, directive=apply_directive,
            )
            move.update(result)
            if result.get("status") == "applied":
                applied.append(move)
                if move.get("kind") == "launch_factory_job":
                    in_flight += 1
            else:
                rejected.append(move)
        after = self.captain.gather_snapshot(connection, project_id)
        verify = verify_from_snapshots(before, after, applied)
        scope = self.captain._scope(connection, project_id)
        minute = datetime.now(timezone.utc).strftime("%Y%m%d%H%M")
        receipt, _ = self.receipts.create_or_reuse(
            connection, scope["org_id"], scope["workspace_id"], project_id,
            f"focused:{agent_id}:{minute}:{trigger.split(':')[0]}",
            "focused_agent.tick",
            {"agent_id": agent_id, "trigger": trigger},
            {"applied": len(applied), "rejected": len(rejected), "verify_ok": bool(verify.get("ok"))},
        )
        tick = connection.execute(
            text(
                """
                INSERT INTO focused_agent_ticks (
                  id, agent_id, project_id, trigger, observed, planned,
                  validation, applied, rejected, verify, receipt_id
                )
                VALUES (
                  :id, :agent, :p, :trigger, CAST(:obs AS JSONB), CAST(:plan AS JSONB),
                  CAST(:val AS JSONB), CAST(:app AS JSONB), CAST(:rej AS JSONB),
                  CAST(:ver AS JSONB), :receipt
                )
                RETURNING *
                """
            ),
            {
                "id": make_id("fatick"),
                "agent": agent_id,
                "p": project_id,
                "trigger": trigger[:80],
                "obs": json.dumps(before),
                "plan": json.dumps(planned),
                "val": json.dumps({"moves": [move.get("validation") for move in planned],
                                   "decision": (decision or {}).get("decided_by")}),
                "app": json.dumps(applied),
                "rej": json.dumps(rejected),
                "ver": json.dumps(verify),
                "receipt": receipt.get("id"),
            },
        ).mappings().one()
        status = "verified" if verify.get("ok") else "needs_attention"
        if applied:
            status = "applied" if verify.get("ok") else "applied_unverified"
        connection.execute(
            text(
                """
                UPDATE focused_agents
                SET last_tick_at=now(),
                    last_wake_at=CASE WHEN :wake THEN now() ELSE last_wake_at END,
                    last_status=:st,
                    last_evidence=CAST(:ev AS JSONB),
                    updated_at=now()
                WHERE id=:id
                """
            ),
            {
                "wake": trigger.startswith("wake:"),
                "st": status,
                "ev": json.dumps({
                    "applied": len(applied),
                    "rejected": len(rejected),
                    "verify_ok": bool(verify.get("ok")),
                    "trigger": trigger,
                }),
                "id": agent_id,
            },
        )
        emit_event(
            connection, project_id, "focused_agent.ticked", actor,
            "focused_agent", agent_id,
            {
                "trigger": trigger,
                "applied": len(applied),
                "rejected": len(rejected),
                "verify_ok": bool(verify.get("ok")),
                "receipt_id": receipt.get("id"),
            },
        )
        return {
            "status": status,
            "agent": self.get(connection, project_id, agent_id),
            "tick": self._row(tick),
            "applied": applied,
            "rejected": rejected,
            "planned": planned,
            "verify": verify,
            # Which move the agent chose and why (None when no move was applicable).
            "decision": (decision or {}).get("decided_by"),
            "receipt_id": receipt.get("id"),
            "note": (
                f"{agent.get('name')} {trigger}: {len(applied)} applied, "
                f"{len(rejected)} rejected, verify "
                f"{'ok' if verify.get('ok') else 'failed'}. "
                "Decisions and deadletters stay human."
            ),
        }

    #: What a move does, as the agent is offered it.
    _MOVE_MEANS = {
        "restart_preview": "restart the preview of the latest verify-green run, so it is "
                           "reachable again",
        "launch_factory_job": "launch a governed factory job with this directive",
    }

    def _decide_move(self, connection: Connection, *, project_id: str, actor: str,
                     agent: dict[str, Any], trigger: str, snapshot: dict[str, Any],
                     applicable: list[dict[str, Any]]) -> dict[str, Any]:
        """The agent's turn through the one pipeline: which ONE validated move to apply now, or
        none. Returns {move, decided_by, word}; `move` is None unless one was decided."""
        from .agent_service import CLARIFY, NO_MODEL, AgentService, Choice

        choices = [Choice(f"m{i}", f"{self._MOVE_MEANS.get(str(m.get('kind')), m.get('kind'))}"
                                   f": {str(m.get('detail') or m.get('directive') or '')[:400]}",
                          label=str(m.get("kind") or "apply"))
                   for i, m in enumerate(applicable)]
        choices.append(Choice("none", "apply nothing this tick: none of these serves the duty "
                                      "now, or it is better to wait for the next tick",
                              acts=False, label="Wait"))
        name = str(agent.get("name") or "a focused agent")
        decision = AgentService(self.settings).decide(
            connection, project_id=project_id, actor=actor, agent=name,
            external_ref=f"focused_agent:{agent.get('id')}",
            situation=(f"You are {name}, a focused BOSS agent on duty ({trigger}). Your duty: "
                       f"{agent.get('focus') or 'none stated'}. BOSS observed the project and "
                       "its playbook proposed these moves; each one passed BOSS's validation "
                       "(budget, in-flight work, duplicates). Decide which ONE to apply now."),
            message="Which of these validated moves should you apply now, if any?",
            choices=choices,
            context={"what BOSS observed": {k: v for k, v in snapshot.items()
                                            if not isinstance(v, (list, dict))
                                            or len(json.dumps(v, default=str)) < 2000},
                     "the validated moves": [{k: m.get(k) for k in
                                              ("kind", "detail", "directive", "finding_id")}
                                             for m in applicable]},
            hints=[f"BOSS's duty playbook proposed {len(applicable)} move(s) for this focus"])
        said = decision.to_dict()
        if not decision.decided or decision.action == "none":
            word = ("no model answered, so no move was applied" if decision.status == NO_MODEL
                    else decision.question if decision.status == CLARIFY
                    else decision.reason or "the agent chose to wait")
            return {"move": None, "decided_by": said, "word": word}
        try:
            move = applicable[int(str(decision.action)[1:])]
        except (ValueError, IndexError):
            return {"move": None, "decided_by": said, "word": "the answer named no move"}
        move["chosen_because"] = decision.reason
        return {"move": move, "decided_by": said, "word": ""}

    def _due_reason(self, connection: Connection, agent: dict[str, Any]) -> Optional[str]:
        mode = str(agent.get("mode") or "on_demand")
        if mode == "on_demand":
            return None
        last = agent.get("last_tick_at")
        interval = int(agent.get("interval_seconds") or DEFAULT_INTERVAL_SECONDS)
        if not last:
            return "schedule"
        try:
            if isinstance(last, str):
                last_dt = datetime.fromisoformat(last.replace("Z", "+00:00"))
            else:
                last_dt = last
            if last_dt.tzinfo is None:
                last_dt = last_dt.replace(tzinfo=timezone.utc)
            age = (datetime.now(timezone.utc) - last_dt).total_seconds()
            if age >= interval:
                return "schedule"
        except Exception:
            return "schedule"
        if mode == "hybrid" and self._wake_event_since(connection, agent, last):
            return "wake:event"
        return None

    def _wake_event_since(
        self, connection: Connection, agent: dict[str, Any], since: Any,
    ) -> bool:
        events = normalize_wake_on(agent.get("wake_on"))
        if not events:
            return False
        placeholders = ", ".join(f":e{index}" for index, _ in enumerate(events))
        params: dict[str, Any] = {
            "p": agent["project_id"],
            "since": str(since),
        }
        for index, event in enumerate(events):
            params[f"e{index}"] = event
        try:
            row = connection.execute(
                text(
                    f"""
                    SELECT event_type FROM events
                    WHERE project_id=:p AND created_at > CAST(:since AS TIMESTAMPTZ)
                      AND event_type IN ({placeholders})
                    ORDER BY created_at DESC LIMIT 1
                    """
                ),
                params,
            ).first()
        except Exception:
            return False
        return row is not None

    def _find_by_key(self, connection: Connection, project_id: str, agent_key: str):
        return connection.execute(
            text(
                "SELECT id FROM focused_agents WHERE project_id=:p AND agent_key=:k"
            ),
            {"p": project_id, "k": agent_key},
        ).first()

    @staticmethod
    def _row(row) -> dict[str, Any]:
        out = dict(row)
        for key, value in list(out.items()):
            if hasattr(value, "isoformat"):
                out[key] = value.isoformat()
            elif hasattr(value, "__float__") and key in {"last_score"}:
                try:
                    out[key] = float(value)
                except Exception:
                    pass
        return out
