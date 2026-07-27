"""The engine-native BOSS Agent.

A self-contained tool-calling loop that reasons over the engine's tool layer.
It ships *inside* the OSS engine — no HTTP API, no console — and is the default
"BOSS Agent" for OSS users. The same tools are also exposed over MCP
(`boss_engine.mcp`) so external agents (GitHub Copilot, Claude) can drive the
engine instead; this class is the batteries-included option.

Design:
  * Read tools are always callable — the agent answers "what's running / preview
    this task / how many builds are going" by calling them itself.
  * Write tools (create a code task, start a preview, convene a meeting) are
    gated: unless `allow_writes=True`, a write the model wants to make is returned
    as a `pending_write` for confirmation rather than executed. This mirrors the
    console's confirm-before-dispatch behavior at the engine level.

The loop speaks the same message contract as the rest of BOSS
(`make_chat_adapter().create_chat_completion(messages, tools=...)`), so it works
with whatever model the engine is configured for.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Optional

from .._compat import services_module
from ..tools import ToolError, ToolRegistry, build_default_registry


_SYSTEM_PROMPT = """You are the BOSS Agent — the conversational interface to the \
BOSS autonomous software factory's workflow engine.

You have direct, low-level access to the engine through tools. Use them to ANSWER \
questions and to ACT:
- To report status (what's running, how many background tasks, a task's PR, a \
preview link, the microVM fleet, arbitrary data), CALL the relevant read tool — \
never guess. Prefer `project_status` for a broad view, then drill in.
- To create work (build/implement/fix a feature, research an idea, spec a PRD, \
preview a task), call the matching write tool.

Rules:
- Ground every factual claim in a tool result you actually received this turn.
- Write tools change real state. If you are not certain the user asked you to \
create/launch something, ask first — don't fire a write speculatively.
- Be concise and concrete. Give run ids and URLs when you have them.
"""


@dataclass
class AgentTurn:
    reply: str
    trace: list[dict] = field(default_factory=list)
    pending_writes: list[dict] = field(default_factory=list)
    steps: int = 0

    def to_dict(self) -> dict:
        return {
            "reply": self.reply,
            "trace": self.trace,
            "pending_writes": self.pending_writes,
            "steps": self.steps,
        }


class EngineAgent:
    def __init__(self, ctx: Any, registry: Optional[ToolRegistry] = None):
        self.ctx = ctx
        self.registry = registry or build_default_registry()
        self._adapter = None

    def _adapter_obj(self):
        if self._adapter is None:
            cli = services_module("claude_cli_adapter")
            self._adapter = cli.make_chat_adapter(self.ctx.settings)
        return self._adapter

    def chat(
        self,
        *,
        project_id: str,
        actor: str = "user",
        message: str,
        allow_writes: bool = False,
        max_steps: int = 6,
        history: Optional[list[dict]] = None,
        system: Optional[str] = None,
    ) -> AgentTurn:
        """Run one agent turn. Returns the reply plus the tool trace and any
        write actions that were held for confirmation. `system` overrides the
        default system prompt (the router's answer path passes its own
        discuss-don't-claim-you-started framing)."""
        schemas = self.registry.openai_schema()
        messages: list[dict] = [{"role": "system", "content": system or _SYSTEM_PROMPT}]
        if history:
            messages.extend(history)
        messages.append({"role": "user", "content": message})

        trace: list[dict] = []
        pending: list[dict] = []
        final_text = ""
        steps = 0

        for i in range(max_steps):
            steps = i + 1
            msg, _usage = self._adapter_obj().create_chat_completion(messages, tools=schemas)
            messages.append(msg["raw"])
            tool_calls = msg.get("tool_calls")
            if not tool_calls:
                final_text = msg.get("content") or ""
                break

            for tc in tool_calls:
                name = tc.get("name", "")
                try:
                    args = json.loads(tc.get("arguments") or "{}")
                except Exception:
                    args = {}
                result = self._run_tool(
                    name, args, project_id=project_id, actor=actor,
                    allow_writes=allow_writes, pending=pending,
                )
                trace.append({"tool": name, "args": args, "result": result})
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.get("id"),
                        "content": json.dumps(result)[:16000],
                    }
                )
        else:
            # loop exhausted without a final text answer
            final_text = final_text or "(reached the step limit while working; ask me to continue.)"

        return AgentTurn(reply=final_text, trace=trace, pending_writes=pending, steps=steps)

    def _run_tool(
        self, name: str, args: dict, *, project_id: str, actor: str,
        allow_writes: bool, pending: list[dict],
    ) -> dict:
        try:
            spec = self.registry.get(name)
        except ToolError as exc:
            return {"ok": False, "error": str(exc)}
        if spec.kind == "write" and not allow_writes:
            proposal = {"tool": name, "args": args}
            pending.append(proposal)
            return {
                "ok": False,
                "status": "confirmation_required",
                "note": (
                    f"'{name}' would create/mutate workflow state. It was NOT run. "
                    "Confirm to proceed (or re-run the turn with writes allowed)."
                ),
                "proposed": proposal,
            }
        return self.registry.invoke(
            name, self.ctx, project_id=project_id, actor=actor, args=args
        )
