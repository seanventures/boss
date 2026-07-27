from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..config import Settings
from .budget import BudgetGovernor
from .canonical import CanonicalObjectService
from .capability_manifest import CapabilityManifest
from .claude_cli_adapter import make_chat_adapter
from .code_task_service import start_code_task
from .core import make_id
from .discovery_service import start_discovery
from .intake import CommandIntakeService
from .jobs import JobLeaseService
from .meeting_service import start_meeting_run
from .members import (
    MemberDirectoryService,
    ProjectMemberNotFoundError,
    ProjectMemberPermissionError,
)
from .openai_adapter import estimate_cost_usd_micros
from .receipts import ReceiptService


@dataclass
class RouterTurn:
    reply: str
    actions: list[dict[str, Any]] = field(default_factory=list)
    pending_confirmation: Optional[dict[str, Any]] = None
    tool_calls: list[dict[str, Any]] = field(default_factory=list)


_TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "create_code_task",
            "description": "Create one narrow, unambiguous governed coding task.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "instructions": {"type": "string"},
                },
                "required": ["title", "instructions"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_meeting",
            "description": "Create a meeting for an open design or decision question.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "purpose": {"type": "string"},
                },
                "required": ["title", "purpose"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_prd",
            "description": "Convene a governed PRD-authoring panel for a fuzzy feature.",
            "parameters": {
                "type": "object",
                "properties": {"directive": {"type": "string"}},
                "required": ["directive"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_discovery",
            "description": "Research or compare an opportunity before delivery work.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "seed": {"type": "string"},
                },
                "required": ["title", "seed"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "propose_command",
            "description": "Propose, but do not dispatch, a multi-phase plan.",
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
            },
        },
    },
]


class IntentRouter:
    """Transport-free intent router and thin tool adapters.

    Explicit intent patterns avoid an unnecessary model call. Ambiguous turns can
    opt into the same tool-calling adapter used by BOSS's agent loop, but only
    behind a budget reservation and Receipt.
    """

    def __init__(self, settings: Settings):
        self.settings = settings
        self.intake = CommandIntakeService(settings)
        self.jobs = JobLeaseService(settings)
        self.members = MemberDirectoryService(settings)
        self.canonical = CanonicalObjectService()
        self.budget = BudgetGovernor(settings)
        self.receipts = ReceiptService()
        self._answer_agent_obj = None  # lazy: read-only tool-grounded answerer

    def _answer_agent(self):
        """A read-only, tool-grounded agent used by the DISCUSS/answer path so the
        one chat interface can answer 'what's running / preview this / query the DB'
        with REAL engine data — not a blind LLM reply. Built once, lazily; imported
        inside the method to avoid any services<->agent import cycle."""
        if self._answer_agent_obj is None:
            from ..agent.engine_agent import EngineAgent
            from ..context import EngineContext
            from ..db import get_engine
            from ..tools import ToolRegistry, build_default_registry

            ctx = EngineContext(settings=self.settings, engine=get_engine(self.settings))
            # read-only view of the tool layer (no write tools on the discuss path)
            read_only = ToolRegistry()
            for spec in build_default_registry().specs(kind="read"):
                read_only.register(spec)
            self._answer_agent_obj = EngineAgent(ctx, registry=read_only)
        return self._answer_agent_obj

    def route_turn(
        self,
        connection: Connection,
        *,
        project_id: str,
        actor: str,
        messages: list[dict[str, Any]],
        manifest: CapabilityManifest,
        confirm_plan_id: Optional[str] = None,
        pending_plan_id: Optional[str] = None,
        external_ref: Optional[str] = None,
    ) -> RouterTurn:
        latest = str(messages[-1].get("content") or "").strip() if messages else ""
        if confirm_plan_id:
            return self._confirm_plan(
                connection,
                project_id=project_id,
                actor=actor,
                confirm_plan_id=confirm_plan_id,
                pending_plan_id=pending_plan_id,
            )

        if pending_plan_id:
            low = latest.lower()
            # DISMISS: clear a stale/unwanted plan and move on (agent_service clears it on this action).
            if re.search(r"\b(cancel|discard|dismiss|scrap|forget (it|this|that)|never ?mind|"
                         r"drop (it|this|the plan)|(remove|delete|clear|scrap|ignore) the plan|"
                         r"start over)\b", low):
                return RouterTurn(reply="Okay — cleared that pending plan. What would you like to do next?",
                                  actions=[{"kind": "plan_dismissed"}])
            # A leading QUESTION is discussion — answer it without touching the plan (the pending plan
            # must not swallow every message: a user has to be able to ask things).
            if bool(re.match(r"^\s*(how|what|why|when|where|who|which|explain|tell me|show me)\b", low)) or (
                    latest.strip().endswith("?") and not re.match(r"^\s*(can|could|would|will|please)\b", low)):
                return self._answer(connection, project_id, messages)
            # Any other new instruction REVISES the plan to reflect it (re-propose). This also unjams a
            # thread whose plan was already handled another way — the user just steers with a message.
            row = connection.execute(
                text("SELECT objective FROM plans WHERE id = :id AND project_id = :project_id"),
                {"id": pending_plan_id, "project_id": project_id},
            ).mappings().first()
            if row is None:
                return RouterTurn(reply="That pending plan no longer exists — cleared it.",
                                  actions=[{"kind": "plan_dismissed"}])
            revising = bool(re.search(r"\b(no|don't|do not|change|revise|edit|instead|not yet)\b", low))
            command = (f"{row['objective']}\n\nRequested revision: {latest}" if revising else latest)
            try:
                return self._dispatch(
                    connection, "propose_command", {"command": command},
                    project_id=project_id, actor=actor, external_ref=external_ref)
            except (ProjectMemberNotFoundError, ProjectMemberPermissionError) as error:
                return RouterTurn(reply=f"I can’t revise that plan: {error}")

        if self._asks_for_capabilities(latest):
            enabled = [item for item in manifest.capabilities if item.enabled]
            disabled = [item for item in manifest.capabilities if not item.enabled]
            reply = "I can currently:\n" + "\n".join(
                f"- {item.title}: {item.summary}" for item in enabled
            )
            if disabled:
                reply += "\n\nUnavailable in this project:\n" + "\n".join(
                    f"- {item.title}: {item.reason or 'not enabled'}" for item in disabled
                )
            return RouterTurn(reply=reply)

        name, arguments = self._deterministic_intent(latest)
        if name is None and self.settings.agent_router_llm_enabled:
            selected = self._model_intent(
                connection, project_id, messages, manifest
            )
            if selected:
                name, arguments = selected
        if name is None:
            # DISCUSS BY DEFAULT: an unclassified message is a question / discussion, NOT a build
            # order. Answer it conversationally and DON'T spawn a task (that spawned junk tasks for
            # every greeting, question, or test string). Work starts only on an explicit intent.
            return self._answer(connection, project_id, messages)

        capability_id = {
            "create_code_task": "code_task",
            "create_meeting": "meeting",
            "create_prd": "prd",
            "run_discovery": "discovery",
            "propose_command": "multi_phase_command",
        }[name]
        capability = manifest.get(capability_id)
        if capability is None or not capability.enabled:
            reason = capability.reason if capability else "Capability is not registered."
            return RouterTurn(reply=f"I can’t start that here: {reason}")

        try:
            return self._dispatch(
                connection,
                name,
                arguments,
                project_id=project_id,
                actor=actor,
                external_ref=external_ref,
            )
        except (ProjectMemberNotFoundError, ProjectMemberPermissionError) as error:
            return RouterTurn(reply=f"I can’t start that work: {error}")
        except ValueError as error:
            return RouterTurn(reply=f"I couldn’t start that work: {error}")

    def _answer(self, connection, project_id: str, messages: list) -> RouterTurn:
        """Conversational answer — DISCUSS/answer without creating any task. Grounded in the recent
        thread (a task-scoped thread carries that task's history), gated on the model budget. Only an
        explicit build intent (handled in route_turn) actuates work."""
        system = (
            "You are BOSS, the assistant for an autonomous software factory. Answer the user's "
            "question or discuss their request clearly and concisely, grounded in the conversation. "
            "You have READ-ONLY tools into the live engine — USE them to ground factual answers "
            "(what's running, a task's status/PR, live preview links, the microVM fleet, arbitrary "
            "data) instead of guessing. If they clearly want you to BUILD or CHANGE something, "
            "briefly say what you'd do and that you'll start once they confirm (e.g. 'build it' / "
            "'go') — do NOT claim you already started anything, and do NOT invent facts."
        )
        # last 12 user/assistant turns as history; newest user message drives the turn
        convo = [
            {"role": m["role"], "content": m["content"]}
            for m in messages[-12:]
            if isinstance(m, dict) and m.get("role") in ("user", "assistant") and m.get("content")
        ]
        latest = convo[-1]["content"] if convo and convo[-1]["role"] == "user" else (
            convo[-1]["content"] if convo else "")
        history = convo[:-1] if convo else []
        reply = ""
        try:
            reservation = self.budget.reserve(
                connection, project_id, "model",
                self.settings.agent_router_budget_usd_micros,
                "Orchestration conversational answer.", 10)
            if reservation.get("status") == "reserved":
                try:
                    # Tool-grounded answer: the read-only agent may call engine read
                    # tools to fetch real data, then reply. Falls back to a plain
                    # reply if the tool loop yields nothing.
                    turn = self._answer_agent().chat(
                        project_id=project_id, actor="orchestrator",
                        message=latest, history=history, system=system,
                        allow_writes=False, max_steps=4,
                    )
                    reply = str(turn.reply or "").strip()
                finally:
                    try:
                        self.budget.release(connection, project_id, reservation["reservation_id"], "answer")
                    except Exception:  # noqa: BLE001
                        pass
        except Exception:  # noqa: BLE001 — a model/budget hiccup must never break the chat
            reply = ""
        if not reply:
            reply = ("I can help discuss this. When you want me to act, tell me explicitly — e.g. "
                     "“build it”, “add a currency picker”, or “deploy it” — and I’ll start the governed flow.")
        return RouterTurn(reply=reply)

    def _existing_app_for_task(self, connection: Connection, project_id: str,
                               task_id: Optional[str]) -> tuple[Optional[str], Optional[str]]:
        """If this task already has a built app (a completed code task with a PR/branch), return that
        branch + its lineage root — so a chat enhancement MODIFIES the existing code instead of
        building fresh on `main`. Simple: existing task -> its PR/branch -> keep building on top.

        First builds often store only `{source, actor}` in finalize_ctx (no root_run_id). The worker
        still opens `boss/code/{run_id}` in that case — so fall back to the completed run's id,
        matching governed_code_task_service's `root_run_id or run_id` branch naming.
        """
        if not task_id:
            return None, None
        row = connection.execute(
            text("SELECT id, finalize_ctx FROM code_task_runs "
                 "WHERE project_id = :p AND task_id = :t AND status = 'completed' "
                 "AND pull_request_url IS NOT NULL ORDER BY created_at DESC LIMIT 1"),
            {"p": project_id, "t": task_id},
        ).mappings().first()
        if not row:
            return None, None
        fc = row["finalize_ctx"]
        if isinstance(fc, str):
            try:
                fc = json.loads(fc)
            except (ValueError, TypeError):
                fc = {}
        root = (fc or {}).get("root_run_id") or row["id"]
        return (f"boss/code/{root}", root)

    def _dispatch(
        self,
        connection: Connection,
        name: str,
        arguments: dict[str, Any],
        *,
        project_id: str,
        actor: str,
        external_ref: Optional[str] = None,
    ) -> RouterTurn:
        if name == "create_code_task":
            self._require(connection, project_id, actor, "task_create")
            title = str(arguments.get("title") or "Code task")[:300]
            instructions = str(arguments.get("instructions") or title)
            # BUILD-ON-EXISTING: if the chat is scoped to a task that already shipped an app, extend
            # THAT app's branch (one lean web build on top) instead of a new task built on main.
            base_branch, root = self._existing_app_for_task(connection, project_id, external_ref)
            extend = base_branch is not None
            if extend:
                task = {"task_id": external_ref}
                instructions = (
                    "The repo ALREADY CONTAINS a working app on this branch — EXTEND it, do NOT "
                    "re-scaffold or rebuild from scratch. Change only what the request needs and keep "
                    "`npm run build` green.\n\nREQUEST:\n" + instructions)
            else:
                task = self.intake.create_task(
                    connection,
                    project_id=project_id,
                    title=title,
                    description=instructions,
                    dedupe_key=make_id("agent-code"),
                    priority="p1",
                    risk_tier=1,
                    inputs={"source": "orchestration_agent", "instructions": instructions},
                    required_artifacts=["pull_request"],
                )
            run_id = start_code_task(
                connection,
                project_id=project_id,
                task_id=task["task_id"],
                title=title,
                instructions=instructions,
                acceptance_criteria=[],
                test_commands=(["npm run build"] if extend else []),
                owner=self.settings.target_github_owner,
                repo=self.settings.target_github_repo,
                base_branch=base_branch or "main",
                finalize_ctx=({"source": "orchestration_agent", "actor": actor,
                               "web_app_build": True, "root_run_id": root} if extend
                              else {"source": "orchestration_agent", "actor": actor}),
                risk_tier=(0 if extend else None),
                jobs=self.jobs,
            )
            action = {
                "kind": "code_task_created",
                "task_id": task["task_id"],
                "run_id": run_id,
                "href": f"/?area=code_tasks&run={run_id}",
            }
            return RouterTurn(
                reply=("On it — extending the existing app for this task (building on top of its "
                       "current branch). I'll open a PR with just the change."
                       if extend else f"Started the governed code task “{title}”."),
                actions=[action],
                tool_calls=[{"name": name, "arguments": arguments}],
            )

        if name == "create_meeting":
            self._require(connection, project_id, actor, "task_create")
            title = str(arguments.get("title") or "Decision meeting")[:300]
            purpose = str(arguments.get("purpose") or title)
            meeting = self.canonical.create_meeting(
                connection,
                project_id=project_id,
                title=title,
                purpose=purpose,
                participants=[],
                agenda=[purpose],
                actor=actor,
            )
            action = {
                "kind": "meeting_created",
                "meeting_id": meeting["id"],
                "href": f"/?area=meetings&meeting={meeting['id']}",
            }
            return RouterTurn(
                reply=f"Created the decision meeting “{title}”.",
                actions=[action],
                tool_calls=[{"name": name, "arguments": arguments}],
            )

        if name == "create_prd":
            self._require(connection, project_id, actor, "task_create")
            directive = str(arguments.get("directive") or "").strip()
            task = self.intake.create_task(
                connection,
                project_id=project_id,
                title=f"PRD meeting: {directive[:90]}",
                description=directive,
                dedupe_key=make_id("agent-prd"),
                priority="p1",
                risk_tier=2,
                inputs={"directive": directive, "source": "orchestration_agent"},
                required_artifacts=["meeting_minutes"],
            )
            reservation = self.budget.reserve(
                connection,
                project_id,
                "model",
                self.settings.plan_budget_usd_micros * 4,
                "Orchestration agent PRD meeting.",
                120,
            )
            if reservation.get("status") != "reserved":
                raise ValueError(reservation.get("message") or "PRD meeting budget denied.")
            scope = connection.execute(
                text(
                    """
                    SELECT w.org_id, p.workspace_id
                    FROM projects p JOIN workspaces w ON w.id = p.workspace_id
                    WHERE p.id = :project_id
                    """
                ),
                {"project_id": project_id},
            ).mappings().one()
            receipt, _created = self.receipts.create_or_reuse(
                connection,
                scope["org_id"],
                scope["workspace_id"],
                project_id,
                make_id("agent-prd"),
                "model.prd_meeting",
                {"project_id": project_id, "data_boundary": "internal"},
                {"directive": directive},
            )
            run_id = start_meeting_run(
                connection,
                project_id=project_id,
                task_id=task["task_id"],
                directive=directive,
                url=None,
                constraints={},
                role_keys=None,
                finalize_ctx={
                    "reservation_id": reservation["reservation_id"],
                    "receipt_id": receipt["id"],
                    "estimated_usd_micros": self.settings.plan_budget_usd_micros * 4,
                    "actor": actor,
                },
            )
            self.jobs.create_job(
                connection,
                project_id=project_id,
                task_id=task["task_id"],
                kind="meeting.step",
                priority=48,
                payload={"run_id": run_id},
            )
            action = {
                "kind": "prd_started",
                "task_id": task["task_id"],
                "run_id": run_id,
                "href": f"/?area=meetings&run={run_id}",
            }
            return RouterTurn(
                reply="Convened a governed PRD panel. Publishing remains human-gated.",
                actions=[action],
                tool_calls=[{"name": name, "arguments": arguments}],
            )

        if name == "run_discovery":
            self._require(connection, project_id, actor, "task_create")
            seed = str(arguments.get("seed") or "").strip()
            title = str(arguments.get("title") or seed[:120] or "Discovery")
            reservation_amount = self.settings.plan_budget_usd_micros * 4
            reservation = self.budget.reserve(
                connection,
                project_id,
                "model",
                reservation_amount,
                "Orchestration agent discovery run.",
                120,
            )
            if reservation.get("status") != "reserved":
                raise ValueError(reservation.get("message") or "Discovery budget denied.")
            scope = connection.execute(
                text(
                    """
                    SELECT w.org_id, p.workspace_id
                    FROM projects p JOIN workspaces w ON w.id = p.workspace_id
                    WHERE p.id = :project_id
                    """
                ),
                {"project_id": project_id},
            ).mappings().one()
            receipt, _created = self.receipts.create_or_reuse(
                connection,
                scope["org_id"],
                scope["workspace_id"],
                project_id,
                make_id("agent-discovery"),
                "model.discovery",
                {"project_id": project_id, "data_boundary": "internal"},
                {"title": title, "seed": seed},
            )
            run_id = start_discovery(
                project_id=project_id,
                title=title,
                seed=seed,
                context="Started by the orchestration agent.",
                settings=self.settings,
                governance={
                    "reservation_id": reservation["reservation_id"],
                    "receipt_id": receipt["id"],
                    "estimated_usd_micros": reservation_amount,
                },
            )
            action = {
                "kind": "discovery_started",
                "run_id": run_id,
                "href": f"/?area=discovery&run={run_id}",
            }
            return RouterTurn(
                reply=f"Started evidence discovery for “{title}”.",
                actions=[action],
                tool_calls=[{"name": name, "arguments": arguments}],
            )

        if name == "propose_command":
            self._require(connection, project_id, actor, "command_submit")
            command = str(arguments.get("command") or "").strip()
            proposed = self.intake.propose_command(connection, project_id, command, {})
            plan_id = str(proposed["plan_id"])
            phases = list(proposed.get("phases") or [])
            pending = {
                "plan_id": plan_id,
                "summary": self._phase_summary(phases),
                "phase_count": len(phases),
                "phases": phases,
            }
            action = {
                "kind": "plan_proposed",
                **pending,
                "requires_confirmation": True,
                "task_id": proposed["task_id"],
                "href": f"/?area=work&task={proposed['task_id']}",
            }
            return RouterTurn(
                reply=(
                    f"I drafted {len(phases)} sequenced phase"
                    f"{'s' if len(phases) != 1 else ''}. Nothing has been fanned out yet. "
                    "Review the plan and confirm when you want me to dispatch it.\n\n"
                    + self._phase_summary(phases)
                ),
                actions=[action],
                pending_confirmation=pending,
                tool_calls=[{"name": name, "arguments": arguments}],
            )
        raise ValueError(f"Unsupported orchestration tool: {name}")

    def _confirm_plan(
        self,
        connection: Connection,
        *,
        project_id: str,
        actor: str,
        confirm_plan_id: str,
        pending_plan_id: Optional[str],
    ) -> RouterTurn:
        if not pending_plan_id or confirm_plan_id != pending_plan_id:
            return RouterTurn(
                reply="I didn’t dispatch anything because that plan is not the thread’s pending proposal."
            )
        try:
            self._require(connection, project_id, actor, "task_create")
            dispatched = self.intake.dispatch_plan(
                connection,
                confirm_plan_id,
                project_id=project_id,
                actor=actor,
            )
        except (ProjectMemberNotFoundError, ProjectMemberPermissionError) as error:
            return RouterTurn(reply=f"I can’t dispatch that plan: {error}")
        except ValueError as error:
            return RouterTurn(reply=f"I couldn’t dispatch that plan: {error}")
        ids = list(dispatched.get("phase_task_ids") or [])
        return RouterTurn(
            reply=f"Confirmed and dispatched {len(ids)} sequenced tasks.",
            actions=[
                {
                    "kind": "plan_dispatched",
                    "plan_id": confirm_plan_id,
                    "phase_count": len(ids),
                    "href": "/?area=work",
                }
            ],
            tool_calls=[{"name": "dispatch_plan", "arguments": {"plan_id": confirm_plan_id}}],
        )

    def _pending_plan(
        self, connection: Connection, project_id: str, plan_id: str
    ) -> Optional[dict[str, Any]]:
        row = connection.execute(
            text("SELECT phases FROM plans WHERE id = :id AND project_id = :project_id"),
            {"id": plan_id, "project_id": project_id},
        ).mappings().first()
        if row is None:
            return None
        phases = row["phases"]
        if isinstance(phases, str):
            phases = json.loads(phases or "[]")
        phases = list(phases or [])
        return {
            "plan_id": plan_id,
            "summary": self._phase_summary(phases),
            "phase_count": len(phases),
            "phases": phases,
        }

    def _require(
        self, connection: Connection, project_id: str, actor: str, permission: str
    ) -> None:
        self.members.require_member(
            connection, project_id, actor, permission=permission
        )

    @staticmethod
    def _asks_for_capabilities(message: str) -> bool:
        lowered = message.lower()
        return any(
            phrase in lowered
            for phrase in ("what can you do", "capabilities", "how can you help")
        )

    def _deterministic_intent(
        self, message: str
    ) -> tuple[Optional[str], dict[str, Any]]:
        lowered = message.lower()
        if not lowered:
            return None, {}
        # A leading DISCUSSION question ("how/what/why … ?") is NOT a build order — even if it
        # mentions a build verb ("what can you BUILD?"). A polite imperative ("can you add…",
        # "could you build…") IS an action, so it's deliberately excluded from lead_question.
        starts_discussion = bool(re.match(
            r"^\s*(how|what|why|when|where|who|which|whose|explain|tell me|show me)\b", lowered))
        polite_imperative = bool(re.match(r"^\s*(can|could|would|will|please)\b", lowered))
        lead_question = starts_discussion or (message.strip().endswith("?") and not polite_imperative)
        if self._looks_multi_phase(lowered):
            return "propose_command", {"command": message}
        if re.search(r"\b(prd|product requirements?|requirements document|write a spec)\b", lowered):
            return "create_prd", {"directive": message}
        if re.search(r"\b(should we|decide between|design review|architecture decision)\b", lowered):
            return "create_meeting", {
                "title": self._short_title(message, "Decision meeting"),
                "purpose": message,
            }
        if re.search(r"\b(research|compare|investigate|market scan|discover)\b", lowered):
            return "run_discovery", {
                "title": self._short_title(message, "Discovery"),
                "seed": message,
            }
        if re.search(r"\b(fix|implement|build|change|refactor|add|remove|update)\b", lowered) and (
                not lead_question):
            return "create_code_task", {
                "title": self._short_title(message, "Code task"),
                "instructions": message,
            }
        return None, {}

    def classify_message(self, message: str) -> tuple[Optional[str], dict[str, Any]]:
        """Classify text without actuating the selected capability.

        Community-facing agents use this shared, deterministic fast path so an
        untrusted issue cannot create work merely by being classified.
        """
        return self._deterministic_intent(message.strip())

    @staticmethod
    def _looks_multi_phase(message: str) -> bool:
        action_verbs = re.findall(
            r"\b(research|compare|design|plan|build|implement|ship|fix|deploy|document|test)\b",
            message,
        )
        return bool(
            re.search(r"\b(first|then|after that|followed by|multiple|end[- ]to[- ]end)\b", message)
            or len(re.findall(r"\band\b", message)) >= 2
            or (" and " in message and len(set(action_verbs)) >= 2)
        )

    @staticmethod
    def _short_title(message: str, fallback: str) -> str:
        cleaned = " ".join(message.strip().split())
        return (cleaned[:117] + "…") if len(cleaned) > 118 else (cleaned or fallback)

    @staticmethod
    def _phase_summary(phases: list[dict[str, Any]]) -> str:
        return "\n".join(
            f"{index + 1}. {phase.get('title') or f'Phase {index + 1}'}"
            + (f" (after {', '.join(str(i + 1) for i in phase.get('depends_on') or [])})" if phase.get("depends_on") else "")
            for index, phase in enumerate(phases)
        )

    def _model_intent(
        self,
        connection: Connection,
        project_id: str,
        messages: list[dict[str, Any]],
        manifest: CapabilityManifest,
    ) -> Optional[tuple[str, dict[str, Any]]]:
        scope = connection.execute(
            text(
                """
                SELECT w.org_id, p.workspace_id
                FROM projects p JOIN workspaces w ON w.id = p.workspace_id
                WHERE p.id = :project_id
                """
            ),
            {"project_id": project_id},
        ).mappings().first()
        if scope is None:
            return None
        fingerprint = hashlib.sha256(
            json.dumps(messages, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()[:24]
        dedupe_key = f"agent:intent:{project_id}:{fingerprint}"
        existing = self.receipts.get_by_dedupe_key(
            connection, scope["org_id"], dedupe_key
        )
        if existing and existing.get("status") == "succeeded":
            result = existing.get("result") or {}
            return (result["name"], result.get("arguments") or {}) if result.get("name") else None
        if existing:
            return None
        reservation = self.budget.reserve(
            connection,
            project_id,
            "model",
            self.settings.agent_router_budget_usd_micros,
            "Orchestration intent classification.",
            10,
        )
        if reservation.get("status") != "reserved":
            return None
        receipt, _created = self.receipts.create_or_reuse(
            connection,
            scope["org_id"],
            scope["workspace_id"],
            project_id,
            dedupe_key,
            "model.intent_classification",
            {"project_id": project_id, "data_boundary": "internal"},
            {"messages": messages},
        )
        prompt = {
            "role": "system",
            "content": (
                "Route exactly one user ask to one enabled BOSS initiator. Never select a disabled "
                "capability. Prefer code for a narrow change, meeting for an open decision, PRD for "
                "a fuzzy feature, discovery for research, and propose_command for two or more "
                "dependent phases.\n\nCAPABILITY MANIFEST\n" + manifest.render_table()
            ),
        }
        try:
            response, usage = make_chat_adapter(self.settings).create_chat_completion(
                [prompt, *messages], tools=_TOOL_SCHEMAS
            )
            calls = response.get("tool_calls") or []
            if not calls:
                self.receipts.mark_succeeded(
                    connection, receipt["id"], usage.get("model"), {"name": None}
                )
                self.budget.settle(
                    connection,
                    project_id,
                    reservation["reservation_id"],
                    receipt["id"],
                    "model",
                    usage.get("model") or "intent-router",
                    self.settings.agent_router_budget_usd_micros,
                    estimate_cost_usd_micros(usage),
                )
                return None
            call = calls[0]
            arguments = call.get("arguments") or {}
            if isinstance(arguments, str):
                arguments = json.loads(arguments)
            result = {"name": call.get("name"), "arguments": arguments}
            self.receipts.mark_succeeded(
                connection, receipt["id"], usage.get("model"), result
            )
            self.budget.settle(
                connection,
                project_id,
                reservation["reservation_id"],
                receipt["id"],
                "model",
                usage.get("model") or "intent-router",
                self.settings.agent_router_budget_usd_micros,
                estimate_cost_usd_micros(usage),
            )
            if result["name"] not in {item["function"]["name"] for item in _TOOL_SCHEMAS}:
                return None
            return result["name"], arguments
        except Exception as error:  # noqa: BLE001 - persist provider failure and fall back safely
            self.receipts.mark_failed(connection, receipt["id"], {"message": str(error)[:1000]})
            self.budget.release(
                connection,
                project_id,
                reservation["reservation_id"],
                "Intent classification failed before actuation.",
            )
            return None


def route_turn(
    connection: Connection,
    settings: Settings,
    *,
    project_id: str,
    actor: str,
    messages: list[dict[str, Any]],
    manifest: CapabilityManifest,
    confirm_plan_id: Optional[str] = None,
    pending_plan_id: Optional[str] = None,
) -> RouterTurn:
    return IntentRouter(settings).route_turn(
        connection,
        project_id=project_id,
        actor=actor,
        messages=messages,
        manifest=manifest,
        confirm_plan_id=confirm_plan_id,
        pending_plan_id=pending_plan_id,
    )
