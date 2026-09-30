"""Reusable captain playbooks — dictated workflow + autonomous loop.

Users author a playbook once (natural language or structured), save it, and
repeat it. The playbook *guides* who must/must-not be used and what "done"
means. The captain still assesses the live run and loops until that goal is
honestly met.

Hard rules:
  * Missing evidence is never done.
  * Decisions / DLQ never auto-apply.
  * Loop is bounded (max_ticks). No silent BOSS self-edit.
  * Simulated / auto_build=false never spends.
"""
from __future__ import annotations

import json
import re
from typing import Any, Optional


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS captain_playbooks (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  playbook_key TEXT NOT NULL,
  name TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  instructions TEXT NOT NULL DEFAULT '',
  process_key TEXT NOT NULL DEFAULT 'prd_to_accepted_work',
  goal JSONB NOT NULL DEFAULT '{}'::jsonb,
  constraints JSONB NOT NULL DEFAULT '{}'::jsonb,
  loop JSONB NOT NULL DEFAULT '{}'::jsonb,
  status TEXT NOT NULL DEFAULT 'active',
  created_by TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(project_id, playbook_key)
);
CREATE INDEX IF NOT EXISTS idx_captain_playbooks_project
  ON captain_playbooks(project_id, status, updated_at DESC);
CREATE TABLE IF NOT EXISTS captain_playbook_runs (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  playbook_id TEXT NOT NULL,
  playbook_key TEXT NOT NULL,
  process_run_id TEXT,
  factory_run_id TEXT,
  status TEXT NOT NULL DEFAULT 'running',
  ticks INTEGER NOT NULL DEFAULT 0,
  last_verdict TEXT,
  last_blockers JSONB NOT NULL DEFAULT '[]'::jsonb,
  created_by TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""

DEFAULT_OFFICIAL_KEY = "official_verify_green"
MAX_TICKS_CAP = 40
DEFAULT_MAX_TICKS = 12

_ROLE_ALIASES = {
    "architect": "chief_architect",
    "chief architect": "chief_architect",
    "cto": "cto",
    "vp": "vp_product",
    "vp product": "vp_product",
    "pm": "product_manager",
    "product manager": "product_manager",
    "qa": "qa",
    "ux": "ux_engineer",
    "ui": "ux_engineer",
    "ui/ux": "ux_engineer",
    "captain": "engineering_captain",
    "factory": "factory_driver",
    "reviewer": "reviewer",
}


def default_official_playbook() -> dict[str, Any]:
    return {
        "playbook_key": DEFAULT_OFFICIAL_KEY,
        "name": "Official verify-green",
        "description": "Reusable official canary playbook. Captain loops until honest verify.",
        "instructions": (
            "Use the PRD-to-accepted-work workflow. Staff QA and UX. "
            "Convene a 5-seat panel after verify. Loop until npm run verify "
            "exits 0 and the factory run is completed. Never invent scores."
        ),
        "process_key": "prd_to_accepted_work",
        "goal": {
            "kind": "verify_green",
            "require_completed": True,
            "require_tests_passed": True,
        },
        "constraints": {
            "required_roles": ["engineering_captain", "qa", "ux_engineer"],
            "forbidden_roles": [],
            "must_panel": True,
            "panel_size": 5,
            "honesty": True,
        },
        "loop": {"autonomous": True, "until": "goal", "max_ticks": DEFAULT_MAX_TICKS},
    }


def compile_playbook(text: str, *, name: str | None = None) -> dict[str, Any]:
    """Deterministic NL → playbook. Unknown spendy verbs do not invent a factory pass."""
    raw = (text or "").strip()
    if not raw:
        raise ValueError("playbook text is empty")
    lowered = raw.lower()
    playbook = default_official_playbook()
    playbook["instructions"] = raw
    if name and name.strip():
        playbook["name"] = name.strip()[:160]
    elif len(raw) < 80:
        playbook["name"] = raw[:160]
    key_source = re.sub(r"[^a-z0-9]+", "_", (playbook["name"] or "playbook").lower()).strip("_")
    playbook["playbook_key"] = (key_source or "custom_playbook")[:80]
    if "prd" not in lowered and "accepted work" not in lowered:
        playbook["process_key"] = "prd_to_accepted_work"

    required: list[str] = list(playbook["constraints"]["required_roles"])
    forbidden: list[str] = []
    for phrase, role in _ROLE_ALIASES.items():
        if re.search(rf"\bwithout {re.escape(phrase)}\b|\bno {re.escape(phrase)}\b", lowered):
            if role not in forbidden:
                forbidden.append(role)
            if role in required:
                required.remove(role)
        elif re.search(rf"\bmust (?:include |use )?{re.escape(phrase)}\b|\balways {re.escape(phrase)}\b", lowered):
            if role not in required:
                required.append(role)

    panel_match = re.search(r"panel of (\d+)", lowered)
    panel_size = int(panel_match.group(1)) if panel_match else 5
    must_panel = "no panel" not in lowered
    if "panel" in lowered:
        must_panel = "no panel" not in lowered

    goal_kind = "verify_green"
    if "until verify" in lowered or "tests pass" in lowered or "verify green" in lowered:
        goal_kind = "verify_green"
    elif "working software" in lowered:
        goal_kind = "working_software"

    max_ticks = DEFAULT_MAX_TICKS
    ticks_match = re.search(r"max(?:imum)? (\d+) ticks", lowered)
    if ticks_match:
        max_ticks = min(MAX_TICKS_CAP, max(1, int(ticks_match.group(1))))

    playbook["goal"] = {
        "kind": goal_kind,
        "require_completed": True,
        "require_tests_passed": True,
    }
    playbook["constraints"] = {
        "required_roles": required,
        "forbidden_roles": forbidden,
        "must_panel": must_panel,
        "panel_size": max(1, min(panel_size, 9)),
        "honesty": "invent" not in lowered or "never invent" in lowered,
    }
    playbook["loop"] = {
        "autonomous": "manual only" not in lowered,
        "until": "goal",
        "max_ticks": max_ticks,
    }
    return playbook


def apply_playbook_to_plan(plan: dict[str, Any], playbook: dict[str, Any] | None) -> dict[str, Any]:
    """Dictate staffing/finish from a saved playbook. Captain still assesses fit."""
    if not playbook:
        return plan
    constraints = playbook.get("constraints") if isinstance(playbook.get("constraints"), dict) else {}
    required = [str(item) for item in (constraints.get("required_roles") or []) if str(item).strip()]
    forbidden = {str(item) for item in (constraints.get("forbidden_roles") or []) if str(item).strip()}
    assignments = []
    for item in plan.get("assignments") or []:
        agent_id = str(item.get("agent_id") or "")
        if agent_id in forbidden:
            continue
        row = dict(item)
        if agent_id in required and row.get("timing") != "now":
            row["timing"] = "now"
            row["reason"] = (row.get("reason") or "") + " (playbook required)"
        assignments.append(row)
    have = {str(item.get("agent_id")) for item in assignments}
    from .captain_coordination import ROSTER

    for role in required:
        if role in have:
            continue
        agent = next((item for item in ROSTER if item["id"] == role), None)
        if not agent:
            continue
        assignments.append({
            "agent_id": role,
            "name": agent.get("name"),
            "kind": agent.get("kind"),
            "task": "playbook_required",
            "reason": "playbook required this role",
            "timing": "now",
            "skills": list(agent.get("skills") or []),
        })
    plan = dict(plan)
    plan["assignments"] = assignments
    plan["playbook_key"] = playbook.get("playbook_key")
    plan["instructions"] = playbook.get("instructions")
    if constraints.get("must_panel"):
        panel = dict(plan.get("panel") or {})
        roles = list(panel.get("roles") or ["chief_architect", "cto", "qa", "ux_engineer"])
        roles = [role for role in roles if role not in forbidden]
        for role in required:
            if role not in roles and role not in {"factory_driver", "queue_helper", "engineering_captain"}:
                roles.append(role)
        panel["roles"] = roles
        panel["size"] = int(constraints.get("panel_size") or panel.get("size") or len(roles))
        panel["reason"] = panel.get("reason") or "Playbook requires a review panel."
        panel["when"] = panel.get("when") or "after_verify"
        plan["panel"] = panel
    goal = playbook.get("goal") if isinstance(playbook.get("goal"), dict) else {}
    finish = dict(plan.get("finish") or {})
    blockers = list(finish.get("blockers") or [])
    if goal.get("require_tests_passed") and plan.get("finish", {}).get("verdict") == "finished":
        pass
    if goal.get("require_completed") or goal.get("require_tests_passed"):
        # Playbook cannot waive honest finish_check; it can only add blockers.
        finish["playbook_goal"] = goal.get("kind")
    finish["blockers"] = blockers
    plan["finish"] = finish
    plan["loop"] = playbook.get("loop") or {}
    return plan


class CaptainPlaybookService:
    def __init__(self, settings: Any):
        self.settings = settings
        self._schema_ready = False

    def ensure_schema(self, connection: Any) -> None:
        if self._schema_ready:
            return
        for statement in (chunk.strip() for chunk in SCHEMA_SQL.split(";")):
            if statement:
                connection.exec_driver_sql(statement)
        self._schema_ready = True

    def ensure_default(self, connection: Any, project_id: str, *, actor: str) -> dict[str, Any]:
        self.ensure_schema(connection)
        existing = self.get(connection, project_id, DEFAULT_OFFICIAL_KEY)
        if existing:
            return existing
        return self.save(connection, project_id=project_id, actor=actor, **default_official_playbook())

    def save(
        self,
        connection: Any,
        *,
        project_id: str,
        actor: str,
        playbook_key: str,
        name: str,
        description: str = "",
        instructions: str = "",
        process_key: str = "prd_to_accepted_work",
        goal: dict[str, Any] | None = None,
        constraints: dict[str, Any] | None = None,
        loop: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        from sqlalchemy import text

        from .core import emit_event, make_id

        self.ensure_schema(connection)
        key = re.sub(r"[^a-z0-9_]+", "_", (playbook_key or name).lower()).strip("_")[:80]
        if not key:
            raise ValueError("playbook_key is required")
        existing = self.get(connection, project_id, key)
        payload = {
            "p": project_id,
            "k": key,
            "name": (name or key)[:160],
            "d": (description or "")[:1000],
            "ins": (instructions or "")[:4000],
            "pk": process_key or "prd_to_accepted_work",
            "goal": json.dumps(goal or default_official_playbook()["goal"]),
            "c": json.dumps(constraints or default_official_playbook()["constraints"]),
            "loop": json.dumps(loop or default_official_playbook()["loop"]),
            "actor": actor,
        }
        if existing:
            connection.execute(
                text(
                    """
                    UPDATE captain_playbooks
                    SET name=:name, description=:d, instructions=:ins, process_key=:pk,
                        goal=CAST(:goal AS JSONB), constraints=CAST(:c AS JSONB),
                        loop=CAST(:loop AS JSONB), updated_at=now()
                    WHERE project_id=:p AND playbook_key=:k
                    """
                ),
                payload,
            )
            row_id = existing["id"]
        else:
            row_id = make_id("pbook")
            connection.execute(
                text(
                    """
                    INSERT INTO captain_playbooks
                      (id, project_id, playbook_key, name, description, instructions,
                       process_key, goal, constraints, loop, created_by)
                    VALUES
                      (:id, :p, :k, :name, :d, :ins, :pk, CAST(:goal AS JSONB),
                       CAST(:c AS JSONB), CAST(:loop AS JSONB), :actor)
                    """
                ),
                {**payload, "id": row_id},
            )
        emit_event(
            connection, project_id, "captain.playbook.saved", actor,
            "captain_playbook", row_id, {"playbook_key": key},
        )
        saved = self.get(connection, project_id, key)
        assert saved is not None
        return saved

    def get(self, connection: Any, project_id: str, playbook_key: str) -> Optional[dict[str, Any]]:
        from sqlalchemy import text

        self.ensure_schema(connection)
        row = connection.execute(
            text(
                "SELECT * FROM captain_playbooks WHERE project_id=:p AND playbook_key=:k"
            ),
            {"p": project_id, "k": playbook_key},
        ).mappings().first()
        return dict(row) if row else None

    def list(self, connection: Any, project_id: str) -> list[dict[str, Any]]:
        from sqlalchemy import text

        self.ensure_schema(connection)
        self.ensure_default(connection, project_id, actor="system")
        rows = connection.execute(
            text(
                "SELECT * FROM captain_playbooks WHERE project_id=:p ORDER BY updated_at DESC"
            ),
            {"p": project_id},
        ).mappings().all()
        return [dict(row) for row in rows]

    def start(
        self,
        connection: Any,
        *,
        project_id: str,
        actor: str,
        playbook_key: str,
        prd: str,
        attach_run_id: str | None = None,
        auto_build: bool = False,
    ) -> dict[str, Any]:
        from sqlalchemy import text

        from .agent_process import AgentProcessService
        from .core import emit_event, make_id

        playbook = self.get(connection, project_id, playbook_key)
        if playbook is None:
            if playbook_key == DEFAULT_OFFICIAL_KEY:
                playbook = self.ensure_default(connection, project_id, actor=actor)
            else:
                raise ValueError(f"unknown playbook {playbook_key}")
        process = AgentProcessService(self.settings).start_run(
            connection,
            project_id=project_id,
            actor=actor,
            prd=prd,
            title=f"Playbook {playbook.get('name')}",
            process_key=str(playbook.get("process_key") or "prd_to_accepted_work"),
            auto_build=bool(auto_build),
            attach_run_id=attach_run_id,
            official=True,
        )
        run_id = make_id("pbrun")
        connection.execute(
            text(
                """
                INSERT INTO captain_playbook_runs
                  (id, project_id, playbook_id, playbook_key, process_run_id,
                   factory_run_id, status, created_by)
                VALUES
                  (:id, :p, :pid, :k, :pr, :fr, 'running', :actor)
                """
            ),
            {
                "id": run_id,
                "p": project_id,
                "pid": playbook.get("id"),
                "k": playbook.get("playbook_key"),
                "pr": process.get("id"),
                "fr": attach_run_id,
                "actor": actor,
            },
        )
        emit_event(
            connection, project_id, "captain.playbook.started", actor,
            "captain_playbook_run", run_id,
            {"playbook_key": playbook.get("playbook_key"), "process_run_id": process.get("id")},
        )
        return {
            "playbook_run_id": run_id,
            "playbook_key": playbook.get("playbook_key"),
            "process": process,
            "loop": playbook.get("loop"),
            "goal": playbook.get("goal"),
        }

    def loop_once(
        self,
        connection: Any,
        *,
        project_id: str,
        actor: str,
        playbook_key: str | None = None,
    ) -> dict[str, Any]:
        """One autonomous captain tick under playbook constraints. Bounded by max_ticks."""
        from sqlalchemy import text

        from .captain_coordination import CaptainCoordinationService, plan_staffing
        from .factory_flow import FactoryFlowService

        self.ensure_schema(connection)
        playbook = None
        if playbook_key:
            playbook = self.get(connection, project_id, playbook_key)
        if playbook is None:
            playbook = self.ensure_default(connection, project_id, actor=actor)
        max_ticks = int((playbook.get("loop") or {}).get("max_ticks") or DEFAULT_MAX_TICKS)
        max_ticks = min(MAX_TICKS_CAP, max(1, max_ticks))
        flow = FactoryFlowService(self.settings)
        board = flow.board(connection, project_id)
        captain = CaptainCoordinationService(self.settings)
        captain.ensure_schema(connection)
        plans = []
        finished = 0
        for item in board.get("items") or []:
            plan = apply_playbook_to_plan(plan_staffing(item), playbook)
            plans.append(plan)
            captain._persist_assignments(connection, project_id, item, plan, actor=actor)
            if (plan.get("finish") or {}).get("ready"):
                finished += 1
        # Advance playbook run tick counters
        connection.execute(
            text(
                """
                UPDATE captain_playbook_runs
                SET ticks = ticks + 1, last_verdict=:v, updated_at=now(),
                    status = CASE
                      WHEN ticks + 1 >= :max THEN 'exhausted'
                      ELSE status
                    END
                WHERE project_id=:p AND playbook_key=:k AND status='running'
                """
            ),
            {
                "p": project_id,
                "k": playbook.get("playbook_key"),
                "max": max_ticks,
                "v": "finished" if finished else "not_finished",
            },
        )
        return {
            "playbook_key": playbook.get("playbook_key"),
            "plans": plans,
            "finished": finished,
            "in_flight": board.get("in_flight"),
            "autonomous": bool((playbook.get("loop") or {}).get("autonomous")),
            "note": (
                "Captain looped once under playbook instructions. "
                "Done only if factory completed and tests_passed is true."
            ),
        }
