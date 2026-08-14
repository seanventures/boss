"""Metatron Captain — always-on oversight, memory, goals, and governed drive.

Metatron's historical surface is a one-shot status query. The captain keeps a
durable session, remembers operator facts, registers measurable goals, watches
the control plane on a no-spend tick, and proposes (never silently applies)
the next governed move.

Hard rules:
  * Every drive action is validated before apply (budget, target, no-duplicate,
    human-only kinds). Failed checks stay on the move.
  * Decisions and deadletters never auto-apply.
  * BOSS self-change launches a governed PR, never a silent write.
  * Watch is no-LLM. Spendy apply needs a Receipt with a unique dedupe_key.
  * Every failure is visible: validation, events, findings, or a Decision.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..config import Settings
from .agent_service import AgentService
from .core import emit_event, make_id
from .health import HealthSnapshotService
from .metatron import MetatronService
from .receipts import ReceiptService


CAPTAIN_EXTERNAL_REF = "metatron:captain"
CAPTAIN_SURFACE = "chat"

GOAL_KINDS = frozenset({"working_software", "budget", "queue", "quality", "custom"})

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS metatron_goals (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  title TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  kind TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active',
  target JSONB NOT NULL DEFAULT '{}'::jsonb,
  last_score NUMERIC,
  last_measured_at TIMESTAMPTZ,
  last_evidence JSONB NOT NULL DEFAULT '{}'::jsonb,
  auto_drive BOOLEAN NOT NULL DEFAULT false,
  created_by TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_metatron_goals_project
  ON metatron_goals(project_id, status, updated_at DESC);
CREATE TABLE IF NOT EXISTS metatron_memories (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  thread_id TEXT,
  kind TEXT NOT NULL DEFAULT 'fact',
  content TEXT NOT NULL,
  salience NUMERIC NOT NULL DEFAULT 0.5,
  source TEXT NOT NULL DEFAULT 'chat',
  refs JSONB NOT NULL DEFAULT '[]'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_metatron_memories_project
  ON metatron_memories(project_id, created_at DESC);
CREATE TABLE IF NOT EXISTS metatron_watch_ticks (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  observed JSONB NOT NULL DEFAULT '{}'::jsonb,
  findings JSONB NOT NULL DEFAULT '[]'::jsonb,
  proposed_moves JSONB NOT NULL DEFAULT '[]'::jsonb,
  receipt_id TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_metatron_watch_project
  ON metatron_watch_ticks(project_id, created_at DESC);
"""


def interpret_turn(message: str) -> dict[str, Any]:
    """Deterministic captain intent. Never invents a tool from reviewer prose."""
    raw = (message or "").strip()
    lowered = raw.lower()
    if not raw:
        return {"intent": "help", "text": ""}
    if lowered in {"help", "?", "what can you do"}:
        return {"intent": "help", "text": raw}
    if lowered in {"watch", "overwatch", "scan", "tick"}:
        return {"intent": "watch", "text": raw}
    if lowered in {"measure", "score", "how are we doing"}:
        return {"intent": "measure", "text": raw}
    if lowered in {"goals", "list goals"}:
        return {"intent": "goals", "text": raw}
    if lowered in {"memory", "memories", "what do you remember"}:
        return {"intent": "memory", "text": raw}
    if lowered.startswith("remember "):
        return {"intent": "remember", "text": raw[9:].strip()}
    if lowered.startswith("set goal "):
        rest = raw[9:].strip()
        kind = "custom"
        m = re.search(r"\bkind=([a-z_]+)\b", rest, re.I)
        if m and m.group(1).lower() in GOAL_KINDS:
            kind = m.group(1).lower()
            rest = (rest[: m.start()] + rest[m.end() :]).strip()
        return {"intent": "set_goal", "text": rest, "kind": kind}
    if lowered in {"drive auto", "go", "launch", "execute", "do it", "run it"} or lowered.startswith("drive auto"):
        return {"intent": "drive", "text": raw, "auto": True}
    if lowered.startswith("drive") or lowered in {"propose next", "next move"}:
        return {"intent": "drive", "text": raw, "auto": False}
    if re.search(r"\b(build|implement|fix|change|refactor|add|update|ship)\b", lowered) and not lowered.endswith("?"):
        return {"intent": "act", "text": raw}
    if re.search(r"\b(prioriti[sz]e|p0|p1|p2)\b", lowered):
        return {"intent": "prioritize", "text": raw}
    return {"intent": "status", "text": raw}


def measure_from_snapshot(kind: str, snapshot: dict[str, Any], target: dict[str, Any] | None = None) -> dict[str, Any]:
    """Score one goal against an already-gathered watch snapshot. Pure."""
    tgt = target or {}
    if kind == "working_software":
        completed = int(snapshot.get("completed_verify_green") or 0)
        previews = int(snapshot.get("running_previews") or 0)
        score = 0
        if completed:
            score += 50
        if previews:
            score += 50
        return {
            "score": score,
            "status": "achieved" if score >= 100 else "active",
            "evidence": {"completed_verify_green": completed, "running_previews": previews},
        }
    if kind == "budget":
        state = str(snapshot.get("budget_state") or "unknown")
        score = {"green": 100, "yellow": 70, "orange": 40, "red": 10}.get(state, 0)
        return {"score": score, "status": "achieved" if score >= 100 else "active",
                "evidence": {"budget_state": state}}
    if kind == "queue":
        dead = int(snapshot.get("deadletters") or 0)
        pending = int(snapshot.get("pending_decisions") or 0)
        max_dead = int(tgt.get("max_deadletters") or 0)
        max_pending = int(tgt.get("max_pending_decisions") or 3)
        score = 100
        if dead > max_dead:
            score -= min(60, 20 * (dead - max_dead))
        if pending > max_pending:
            score -= min(40, 10 * (pending - max_pending))
        return {"score": max(0, score), "status": "achieved" if score >= 80 else "active",
                "evidence": {"deadletters": dead, "pending_decisions": pending}}
    if kind == "quality":
        min_score = int(tgt.get("min_score") or 75)
        observed = snapshot.get("quality_score")
        if observed is None:
            return {"score": None, "status": "active", "evidence": {"reason": "quality_score not observed"}}
        score = int(observed)
        return {"score": score, "status": "achieved" if score >= min_score else "active",
                "evidence": {"quality_score": score, "min_score": min_score}}
    return {"score": None, "status": "active", "evidence": {"reason": "custom goal needs operator evidence"}}


def findings_from_snapshot(snapshot: dict[str, Any], goals: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Turn a snapshot + goals into visible findings. Pure. Never spends."""
    findings: list[dict[str, Any]] = []
    dead = int(snapshot.get("deadletters") or 0)
    if dead:
        findings.append({
            "id": "deadletters",
            "severity": "high" if dead > 5 else "medium",
            "title": f"{dead} deadletter(s) need operator review",
            "action": "review_deadletters",
            "auto_safe": False,
        })
    pending = int(snapshot.get("pending_decisions") or 0)
    if pending:
        findings.append({
            "id": "decisions",
            "severity": "high",
            "title": f"{pending} pending decision(s) block autonomous progress",
            "action": "open_decision_inbox",
            "auto_safe": False,
        })
    if int(snapshot.get("running_previews") or 0) == 0 and int(snapshot.get("completed_verify_green") or 0) > 0:
        findings.append({
            "id": "preview_expired",
            "severity": "medium",
            "title": "Verify-green apps exist but no preview is running",
            "action": "restart_preview",
            "auto_safe": True,
        })
    if snapshot.get("budget_state") in {"orange", "red"}:
        findings.append({
            "id": "budget",
            "severity": "high",
            "title": f"Budget state is {snapshot.get('budget_state')}",
            "action": "reduce_spend_or_raise_cap",
            "auto_safe": False,
        })
    for goal in goals:
        if goal.get("status") != "active":
            continue
        measured = measure_from_snapshot(str(goal.get("kind") or "custom"), snapshot, goal.get("target") or {})
        if measured.get("score") is not None and int(measured["score"]) < 80:
            findings.append({
                "id": f"goal:{goal.get('id')}",
                "severity": "medium",
                "title": f"Goal '{goal.get('title')}' is at {measured['score']}",
                "action": "pursue_goal",
                "auto_safe": False,
                "goal_id": goal.get("id"),
            })
    if not findings:
        findings.append({
            "id": "steady",
            "severity": "info",
            "title": "No urgent captain finding. Continue measuring.",
            "action": "hold",
            "auto_safe": True,
        })
    return findings


def proposed_moves_from_findings(findings: list[dict[str, Any]], *, auto: bool) -> list[dict[str, Any]]:
    """Map findings to proposal-only moves. auto=True still refuses high-risk."""
    moves: list[dict[str, Any]] = []
    for finding in findings:
        action = finding.get("action")
        if action == "hold":
            continue
        if action == "open_decision_inbox":
            moves.append({
                "kind": "escalate_decision",
                "status": "needs_human",
                "detail": finding["title"],
                "finding_id": finding["id"],
            })
            continue
        if action == "review_deadletters":
            moves.append({
                "kind": "propose_deadletter_review",
                "status": "proposed",
                "detail": finding["title"],
                "finding_id": finding["id"],
            })
            continue
        if action == "restart_preview" and finding.get("auto_safe"):
            moves.append({
                "kind": "restart_preview",
                "status": "queued" if auto else "proposed",
                "detail": "Restart verify-green previews (no new code.task).",
                "finding_id": finding["id"],
                "apply": bool(auto),
            })
            continue
        if action == "pursue_goal":
            moves.append({
                "kind": "launch_factory_job",
                "status": "queued" if auto else "proposed",
                "detail": finding["title"],
                "finding_id": finding["id"],
                "goal_id": finding.get("goal_id"),
                "apply": bool(auto),
            })
            continue
        moves.append({
            "kind": "propose",
            "status": "proposed",
            "detail": finding["title"],
            "finding_id": finding["id"],
            "requires_human": not finding.get("auto_safe"),
        })
    return moves


# Every captain action — even auto-drive — must pass these gates before apply.
BLOCKED_APPLY_KINDS = frozenset({
    "escalate_decision",
    "propose_deadletter_review",
    "propose",
})


def validate_move(
    move: dict[str, Any],
    snapshot: dict[str, Any],
    *,
    auto: bool,
    in_flight: int = 0,
    budget_state: str = "unknown",
    verify_green_run_id: Optional[str] = None,
    self_mod_repo: str = "",
) -> dict[str, Any]:
    """Fail-closed validation for one drive move. Pure: never spends.

    A move is valid to APPLY only when every check passes. Failed checks stay
    visible on the move so the operator can see why it did not launch.
    """
    checks: list[dict[str, Any]] = []
    kind = str(move.get("kind") or "")

    def check(name: str, ok: bool, detail: str) -> None:
        checks.append({"name": name, "ok": ok, "detail": detail})

    check("has_kind", bool(kind), "move declares a kind" if kind else "move has no kind")
    check(
        "not_human_only",
        kind not in BLOCKED_APPLY_KINDS and not move.get("requires_human")
        and move.get("status") != "needs_human",
        "high-risk actions (decisions, DLQ, budget) stay human",
    )
    check(
        "budget_not_red",
        budget_state not in {"red", "orange"},
        f"budget_state={budget_state}",
    )
    if kind == "restart_preview":
        check(
            "verify_green_target",
            bool(verify_green_run_id),
            f"run={verify_green_run_id}" if verify_green_run_id else "no completed verify-green run",
        )
    if kind == "launch_factory_job":
        check("no_duplicate_inflight", int(in_flight) == 0, f"in_flight={in_flight}")
        check("auto_requested", bool(auto), "factory launch requires auto=true")
        check(
            "self_mod_is_pr_only",
            True,
            "BOSS self-change launches a governed PR, never a silent write",
        )
        if self_mod_repo:
            check("repo_bound", bool(self_mod_repo), f"repo={self_mod_repo}")
    ok = all(c["ok"] for c in checks)
    return {
        "ok": ok,
        "checks": checks,
        "failed": [c["name"] for c in checks if not c["ok"]],
    }


class MetatronCaptain:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.metatron = MetatronService(settings)
        self.health = HealthSnapshotService(settings)
        self.agents = AgentService(settings)
        self.receipts = ReceiptService()
        self._schema_ready = False

    def ensure_schema(self, connection: Connection) -> None:
        if self._schema_ready:
            return
        for statement in (chunk.strip() for chunk in SCHEMA_SQL.split(";")):
            if statement:
                connection.exec_driver_sql(statement)
        self._schema_ready = True

    def session_turn(
        self,
        connection: Connection,
        *,
        project_id: str,
        actor: str,
        message: str,
        thread_id: Optional[str] = None,
    ) -> dict[str, Any]:
        self.ensure_schema(connection)
        thread = self.agents.start_or_load_thread(
            connection,
            project_id=project_id,
            actor=actor,
            thread_id=thread_id,
            surface=CAPTAIN_SURFACE,
            external_ref=CAPTAIN_EXTERNAL_REF,
        )
        parsed = interpret_turn(message)
        self.agents.append_message(connection, thread_id=thread["id"], role="user", content=message.strip() or parsed["intent"])
        actions: list[dict[str, Any]] = []
        reply = ""
        watch = None
        if parsed["intent"] == "help":
            reply = (
                "I am Metatron, captain of this project. I remember facts, track goals, "
                "watch the control plane, and can launch validated factory work. "
                "Decisions stay human. BOSS self-change is a PR. Every action is validated "
                "before it runs.\n"
                "Try: remember … · set goal … · watch · measure · drive · drive auto · "
                "build … · goals · memory"
            )
        elif parsed["intent"] == "remember":
            mem = self.remember(connection, project_id=project_id, thread_id=thread["id"],
                                content=parsed["text"], source="chat", kind="fact")
            actions.append({"kind": "remembered", "memory_id": mem["id"]})
            reply = f"Remembered: {parsed['text']}"
        elif parsed["intent"] == "set_goal":
            goal = self.set_goal(connection, project_id=project_id, actor=actor,
                                 title=parsed["text"], kind=parsed.get("kind") or "custom")
            actions.append({"kind": "goal_set", "goal_id": goal["id"]})
            reply = f"Tracking goal '{goal['title']}' ({goal['kind']}). I will measure it on every watch."
        elif parsed["intent"] == "watch":
            watch = self.watch_once(connection, project_id=project_id, actor=actor)
            actions.append({"kind": "watched", "tick_id": watch["id"], "finding_count": len(watch["findings"])})
            reply = self._render_watch(watch)
        elif parsed["intent"] == "measure":
            goals = self.measure_all(connection, project_id=project_id)
            actions.append({"kind": "measured", "count": len(goals)})
            reply = self._render_goals(goals) or "No goals registered yet. `set goal …`"
        elif parsed["intent"] == "goals":
            reply = self._render_goals(self.list_goals(connection, project_id)) or "No goals yet."
        elif parsed["intent"] == "memory":
            reply = self._render_memories(self.list_memories(connection, project_id)) or "No memories yet."
        elif parsed["intent"] == "drive":
            drive = self.drive(
                connection, project_id=project_id, actor=actor,
                auto=bool(parsed.get("auto")),
            )
            actions.append({
                "kind": "drive",
                "auto": bool(parsed.get("auto")),
                "moves": len(drive.get("moves") or []),
                "applied": len(drive.get("applied") or []),
                "rejected": len(drive.get("rejected") or []),
            })
            reply = self._render_drive(drive)
        elif parsed["intent"] in {"act", "prioritize"}:
            drive = self.drive(
                connection, project_id=project_id, actor=actor, auto=True,
                directive=parsed["text"],
            )
            actions.append({
                "kind": "act",
                "applied": len(drive.get("applied") or []),
                "rejected": len(drive.get("rejected") or []),
            })
            reply = self._render_drive(drive)
        else:
            query = self.metatron.answer_query(connection, project_id, parsed["text"] or message)
            reply = query["answer"]
            actions.append({"kind": "status_query", "category": query.get("category")})
            memories = self.list_memories(connection, project_id, limit=3)
            if memories:
                reply += "\n\nFrom memory: " + "; ".join(m["content"][:120] for m in memories)
        assistant = self.agents.append_message(
            connection, thread_id=thread["id"], role="assistant", content=reply, actions=actions
        )
        emit_event(connection, project_id, "metatron.captain.turn", actor, "agent_thread",
                   thread["id"], {"intent": parsed["intent"], "action_count": len(actions)})
        return {
            "thread_id": thread["id"],
            "reply": reply,
            "intent": parsed["intent"],
            "actions": actions,
            "memories": self.list_memories(connection, project_id, limit=8),
            "goals": self.list_goals(connection, project_id),
            "watch": watch or self.latest_watch(connection, project_id),
            "messages": self.agents.list_messages(connection, thread["id"])[-20:],
            "assistant_message_id": assistant.get("id"),
        }

    def load_session(self, connection: Connection, *, project_id: str, actor: str) -> dict[str, Any]:
        self.ensure_schema(connection)
        thread = self.agents.start_or_load_thread(
            connection, project_id=project_id, actor=actor,
            surface=CAPTAIN_SURFACE, external_ref=CAPTAIN_EXTERNAL_REF,
        )
        return {
            "thread_id": thread["id"],
            "messages": self.agents.list_messages(connection, thread["id"])[-40:],
            "memories": self.list_memories(connection, project_id, limit=20),
            "goals": self.list_goals(connection, project_id),
            "watch": self.latest_watch(connection, project_id),
        }

    def remember(
        self, connection: Connection, *, project_id: str, content: str,
        thread_id: Optional[str] = None, kind: str = "fact", source: str = "chat",
        salience: float = 0.7, refs: Optional[list] = None,
    ) -> dict[str, Any]:
        self.ensure_schema(connection)
        row = connection.execute(
            text(
                """
                INSERT INTO metatron_memories (id, project_id, thread_id, kind, content, salience, source, refs)
                VALUES (:id, :p, :t, :k, :c, :s, :src, CAST(:refs AS JSONB))
                RETURNING id, project_id, kind, content, salience, source, created_at
                """
            ),
            {
                "id": make_id("mmem"), "p": project_id, "t": thread_id, "k": kind,
                "c": content.strip()[:2000], "s": max(0, min(1, salience)),
                "src": source, "refs": json.dumps(refs or []),
            },
        ).mappings().one()
        return self._row(row)

    def set_goal(
        self, connection: Connection, *, project_id: str, actor: str,
        title: str, kind: str = "custom", description: str = "",
        target: Optional[dict] = None, auto_drive: bool = False,
    ) -> dict[str, Any]:
        self.ensure_schema(connection)
        resolved = kind if kind in GOAL_KINDS else "custom"
        row = connection.execute(
            text(
                """
                INSERT INTO metatron_goals
                  (id, project_id, title, description, kind, target, auto_drive, created_by)
                VALUES (:id, :p, :title, :d, :k, CAST(:tgt AS JSONB), :auto, :actor)
                RETURNING *
                """
            ),
            {
                "id": make_id("mgoal"), "p": project_id, "title": title.strip()[:200],
                "d": (description or "")[:1000], "k": resolved,
                "tgt": json.dumps(target or {}), "auto": bool(auto_drive), "actor": actor,
            },
        ).mappings().one()
        emit_event(connection, project_id, "metatron.goal.set", actor, "metatron_goal",
                   row["id"], {"kind": resolved, "title": title[:80]})
        return self._row(row)

    def list_goals(self, connection: Connection, project_id: str) -> list[dict[str, Any]]:
        self.ensure_schema(connection)
        rows = connection.execute(
            text("SELECT * FROM metatron_goals WHERE project_id=:p ORDER BY updated_at DESC"),
            {"p": project_id},
        ).mappings().all()
        return [self._row(r) for r in rows]

    def list_memories(self, connection: Connection, project_id: str, limit: int = 12) -> list[dict[str, Any]]:
        self.ensure_schema(connection)
        rows = connection.execute(
            text(
                """
                SELECT id, kind, content, salience, source, created_at
                FROM metatron_memories WHERE project_id=:p
                ORDER BY created_at DESC LIMIT :n
                """
            ),
            {"p": project_id, "n": limit},
        ).mappings().all()
        return [self._row(r) for r in rows]

    def gather_snapshot(self, connection: Connection, project_id: str) -> dict[str, Any]:
        health = self.health.current_state(connection, project_id)
        summary = health.get("summary") or {}

        def _count(sql: str) -> int:
            try:
                return int(connection.execute(text(sql), {"p": project_id}).scalar_one() or 0)
            except Exception:
                return 0

        return {
            "budget_state": health.get("budget_state"),
            "open_tasks": summary.get("open_tasks", 0),
            "pending_decisions": summary.get("pending_decisions", 0),
            "active_jobs": summary.get("active_jobs", 0),
            "deadletters": summary.get("deadletters", 0),
            "completed_verify_green": _count(
                "SELECT count(*) FROM code_task_runs WHERE project_id=:p AND status='completed' "
                "AND COALESCE((state->>'tests_passed')::boolean, false)"
            ),
            "running_previews": _count(
                "SELECT count(*) FROM app_previews WHERE project_id=:p AND status='running' "
                "AND COALESCE(mode,'') <> 'fixture'"
            ),
            "in_flight": _count(
                "SELECT count(*) FROM code_task_runs WHERE project_id=:p "
                "AND status IN ('running','queued','repairing')"
            ),
            "top_risks": health.get("top_risks") or [],
        }

    def measure_all(self, connection: Connection, project_id: str) -> list[dict[str, Any]]:
        self.ensure_schema(connection)
        snapshot = self.gather_snapshot(connection, project_id)
        out: list[dict[str, Any]] = []
        for goal in self.list_goals(connection, project_id):
            measured = measure_from_snapshot(goal["kind"], snapshot, goal.get("target") or {})
            connection.execute(
                text(
                    """
                    UPDATE metatron_goals
                    SET last_score=:s, last_measured_at=now(),
                        last_evidence=CAST(:e AS JSONB),
                        status=CASE WHEN :st IN ('achieved','failed') THEN :st ELSE status END,
                        updated_at=now()
                    WHERE id=:id
                    """
                ),
                {
                    "s": measured.get("score"), "e": json.dumps(measured.get("evidence") or {}),
                    "st": measured.get("status") or "active", "id": goal["id"],
                },
            )
            goal.update({"last_score": measured.get("score"), "last_evidence": measured.get("evidence"),
                         "measured_status": measured.get("status")})
            out.append(goal)
        return out

    def watch_once(self, connection: Connection, *, project_id: str, actor: str) -> dict[str, Any]:
        self.ensure_schema(connection)
        snapshot = self.gather_snapshot(connection, project_id)
        goals = self.measure_all(connection, project_id)
        findings = findings_from_snapshot(snapshot, goals)
        moves = proposed_moves_from_findings(findings, auto=False)
        scope = self._scope(connection, project_id)
        minute = datetime.now(timezone.utc).strftime("%Y%m%d%H%M")
        receipt, _ = self.receipts.create_or_reuse(
            connection, scope["org_id"], scope["workspace_id"], project_id,
            f"metatron:watch:{project_id}:{minute}",
            "metatron.watch",
            {"project_id": project_id},
            {"findings": findings, "moves": moves, "snapshot": snapshot},
        )
        row = connection.execute(
            text(
                """
                INSERT INTO metatron_watch_ticks
                  (id, project_id, observed, findings, proposed_moves, receipt_id)
                VALUES (:id, :p, CAST(:o AS JSONB), CAST(:f AS JSONB), CAST(:m AS JSONB), :r)
                RETURNING *
                """
            ),
            {
                "id": make_id("mwt"), "p": project_id,
                "o": json.dumps(snapshot), "f": json.dumps(findings),
                "m": json.dumps(moves), "r": receipt.get("id"),
            },
        ).mappings().one()
        self.remember(
            connection, project_id=project_id, content=f"Watch: {findings[0]['title']}",
            kind="watch", source="watch", salience=0.6,
            refs=[{"object_type": "metatron_watch_tick", "object_id": row["id"]}],
        )
        emit_event(connection, project_id, "metatron.watch.ticked", actor, "metatron_watch_tick",
                   row["id"], {"finding_count": len(findings), "receipt_id": receipt.get("id")})
        return self._row(row)

    def latest_watch(self, connection: Connection, project_id: str) -> Optional[dict[str, Any]]:
        self.ensure_schema(connection)
        row = connection.execute(
            text(
                """
                SELECT * FROM metatron_watch_ticks
                WHERE project_id=:p ORDER BY created_at DESC LIMIT 1
                """
            ),
            {"p": project_id},
        ).mappings().first()
        return self._row(row) if row else None

    def drive(
        self, connection: Connection, *, project_id: str, actor: str,
        auto: bool = False, directive: Optional[str] = None,
    ) -> dict[str, Any]:
        """Validate every move, then apply only those that pass.

        Decisions, deadletters, and budget-red never apply. Preview restart and
        factory launch apply only when validation (target, budget, no duplicate)
        passes. BOSS self-change is a governed PR, never a silent write.
        """
        self.ensure_schema(connection)
        watch = self.latest_watch(connection, project_id) or self.watch_once(
            connection, project_id=project_id, actor=actor
        )
        findings = list(watch.get("findings") or [])
        snapshot = watch.get("observed") or self.gather_snapshot(connection, project_id)
        if directive:
            findings.append({
                "id": "directive",
                "severity": "medium",
                "title": directive,
                "action": "pursue_goal",
                "auto_safe": False,
            })
        moves = proposed_moves_from_findings(findings, auto=bool(auto))
        verify_run = self._latest_verify_green_without_preview(connection, project_id)
        in_flight = int(snapshot.get("in_flight") or 0)
        budget_state = str(snapshot.get("budget_state") or "unknown")
        repo = f"{self.settings.target_github_owner}/{self.settings.target_github_repo}"
        applied: list[dict[str, Any]] = []
        rejected: list[dict[str, Any]] = []
        for move in moves:
            verdict = validate_move(
                move, snapshot, auto=bool(auto), in_flight=in_flight,
                budget_state=budget_state, verify_green_run_id=verify_run,
                self_mod_repo=repo,
            )
            move["validation"] = verdict
            if not (auto and move.get("apply") and verdict["ok"]):
                if not verdict["ok"] and move.get("apply"):
                    move["status"] = "rejected"
                    rejected.append(move)
                continue
            result = self._apply_validated_move(
                connection, project_id=project_id, actor=actor,
                move=move, verify_run=verify_run, directive=directive,
            )
            move.update(result)
            if result.get("status") == "applied":
                applied.append(move)
                if move.get("kind") == "launch_factory_job":
                    in_flight += 1
            else:
                rejected.append(move)
        human = [m for m in moves if m.get("status") == "needs_human" or m.get("requires_human")]
        emit_event(
            connection, project_id, "metatron.drive.completed", actor, "metatron_watch_tick",
            watch.get("id") or "none",
            {
                "auto": bool(auto), "applied": len(applied), "rejected": len(rejected),
                "human_required": len(human),
            },
        )
        status = "applied" if applied else "proposed"
        return {
            "status": status,
            "auto": bool(auto),
            "applied": applied,
            "rejected": rejected,
            "moves": moves,
            "watch_id": watch.get("id"),
            "note": (
                f"Validated {len(moves)} move(s): {len(applied)} applied, "
                f"{len(rejected)} rejected, {len(human)} need a human. "
                "Decisions and deadletters are never auto-applied. "
                "BOSS self-change opens a PR."
            ),
        }

    def _latest_verify_green_without_preview(self, connection: Connection, project_id: str) -> Optional[str]:
        try:
            row = connection.execute(
                text(
                    """
                    SELECT r.id FROM code_task_runs r
                    WHERE r.project_id=:p AND r.status='completed'
                      AND COALESCE((r.state->>'tests_passed')::boolean, false)
                      AND NOT EXISTS (
                        SELECT 1 FROM app_previews p
                        WHERE p.run_id=r.id AND p.status='running'
                          AND COALESCE(p.mode,'') <> 'fixture'
                      )
                    ORDER BY r.updated_at DESC LIMIT 1
                    """
                ),
                {"p": project_id},
            ).mappings().first()
        except Exception:
            return None
        return str(row["id"]) if row else None

    def _apply_validated_move(
        self, connection: Connection, *, project_id: str, actor: str,
        move: dict[str, Any], verify_run: Optional[str], directive: Optional[str],
    ) -> dict[str, Any]:
        kind = move.get("kind")
        scope = self._scope(connection, project_id)
        if kind == "restart_preview":
            if not verify_run:
                return {"status": "rejected", "error": "no verify-green run to preview"}
            try:
                from .preview_service import start_preview_for_run
                engine = connection.engine
                preview = start_preview_for_run(engine, self.settings, verify_run, ttl_minutes=60)
            except Exception as exc:  # noqa: BLE001
                return {"status": "rejected", "error": f"preview failed: {type(exc).__name__}"}
            ok = preview.get("status") == "running"
            receipt, _ = self.receipts.create_or_reuse(
                connection, scope["org_id"], scope["workspace_id"], project_id,
                f"metatron:preview:{verify_run}",
                "metatron.preview_restart",
                {"run_id": verify_run},
                {"preview": preview.get("id"), "status": preview.get("status")},
            )
            return {
                "status": "applied" if ok else "rejected",
                "preview_id": preview.get("id"),
                "run_id": verify_run,
                "receipt_id": receipt.get("id"),
                "error": None if ok else preview.get("error"),
            }
        if kind == "launch_factory_job":
            from .code_task_service import start_code_task
            from .jobs import JobLeaseService
            jobs = JobLeaseService(self.settings)
            title = (directive or move.get("detail") or "Captain factory launch")[:200]
            self_mod = "boss" in title.lower() or "self-improv" in title.lower()
            owner = self.settings.target_github_owner
            repo = self.settings.target_github_repo
            if self_mod:
                owner = getattr(self.settings, "github_owner", None) or owner
                repo = "boss"
            try:
                run_id = start_code_task(
                    connection,
                    project_id=project_id,
                    task_id=None,
                    title=f"Captain: {title}",
                    instructions=(
                        (directive or move.get("detail") or title)
                        + ("\n\nThis is a BOSS self-improvement change. Open a PR only. "
                           "Do not apply to production in place." if self_mod else "")
                    ),
                    acceptance_criteria=["npm run verify or equivalent exits 0"],
                    test_commands=["npm run verify"] if not self_mod else [],
                    owner=owner,
                    repo=repo,
                    base_branch="main",
                    finalize_ctx={
                        "source": "metatron_captain",
                        "actor": actor,
                        "self_mod": bool(self_mod),
                        "open_pr": True,
                    },
                    jobs=jobs,
                    open_pr=True,
                    risk_tier=1 if self_mod else 0,
                )
            except Exception as exc:  # noqa: BLE001
                return {"status": "rejected", "error": f"factory launch failed: {exc}"}
            receipt, _ = self.receipts.create_or_reuse(
                connection, scope["org_id"], scope["workspace_id"], project_id,
                f"metatron:factory:{run_id}",
                "metatron.factory_launch",
                {"run_id": run_id, "self_mod": self_mod},
                {"title": title},
            )
            return {
                "status": "applied",
                "run_id": run_id,
                "receipt_id": receipt.get("id"),
                "self_mod": bool(self_mod),
            }
        return {"status": "rejected", "error": f"no apply path for {kind}"}

    def _scope(self, connection: Connection, project_id: str) -> dict[str, str]:
        row = connection.execute(
            text(
                """
                SELECT p.id AS project_id, w.id AS workspace_id, o.id AS org_id
                FROM projects p
                JOIN workspaces w ON w.id = p.workspace_id
                JOIN orgs o ON o.id = w.org_id
                WHERE p.id = :project_id
                """
            ),
            {"project_id": project_id},
        ).mappings().first()
        if row is None:
            return {"project_id": project_id, "workspace_id": "workspace_unknown", "org_id": "org_unknown"}
        return dict(row)

    @staticmethod
    def _row(row) -> dict[str, Any]:
        out = dict(row)
        for key, value in list(out.items()):
            if hasattr(value, "isoformat"):
                out[key] = value.isoformat()
            elif hasattr(value, "__float__") and key in {"last_score", "salience"}:
                try:
                    out[key] = float(value)
                except Exception:
                    pass
        return out

    @staticmethod
    def _render_watch(watch: dict[str, Any]) -> str:
        findings = watch.get("findings") or []
        lines = ["Watch complete."]
        for item in findings[:6]:
            lines.append(f"- [{item.get('severity')}] {item.get('title')}")
        return "\n".join(lines)

    @staticmethod
    def _render_goals(goals: list[dict[str, Any]]) -> str:
        if not goals:
            return ""
        lines = ["Goals:"]
        for goal in goals:
            score = goal.get("last_score")
            score_s = "—" if score is None else str(score)
            lines.append(f"- {goal.get('title')} ({goal.get('kind')}) score={score_s} {goal.get('status')}")
        return "\n".join(lines)

    @staticmethod
    def _render_memories(memories: list[dict[str, Any]]) -> str:
        if not memories:
            return ""
        return "Memory:\n" + "\n".join(f"- {m.get('content')}" for m in memories[:8])

    @staticmethod
    def _render_drive(drive: dict[str, Any]) -> str:
        moves = drive.get("moves") or []
        if not moves:
            return "No drive proposed. The watch is steady."
        lines = [drive.get("note") or "Proposed moves (not applied):"]
        for move in moves:
            lines.append(f"- {move.get('kind')}: {move.get('detail')}")
        return "\n".join(lines)
