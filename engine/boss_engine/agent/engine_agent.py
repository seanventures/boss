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



def _propose_label(tool: str, args: dict) -> str:
    """Button text a person can act on without reading the arguments."""
    title = str((args or {}).get("title") or "").strip()
    run_id = str((args or {}).get("run_id") or "").strip()
    if tool == "create_code_task":
        return f"Build: {title}"[:70] if title else "Start a new build"
    if tool == "retry_code_task":
        return f"Retry {run_id[:20]}" if run_id else "Retry the run"
    if tool == "extend_code_task":
        return f"Keep working on {run_id[:20]}" if run_id else "Continue the run"
    if tool == "create_discovery":
        return f"Research: {title}"[:70] if title else "Start research"
    if tool == "request_meeting":
        return "Convene a PRD panel"
    return tool.replace("_", " ")

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

        # A TURN MUST NEVER END WITHOUT AN ANSWER.
        #
        # The loop below spends a step per model round-trip, and a step that makes tool calls
        # produces no text. So when the budget ran out mid-investigation the operator got
        # "(reached the step limit while working; ask me to continue.)" — carrying NONE of what
        # had just been read. With max_steps=4 from the router that was the ORDINARY outcome of
        # any real question: the prompt lists ~47 tools and says "chain tools, several calls is
        # normal", and four round-trips are consumed before anything can be written down.
        #
        # The last step is therefore reserved: it runs with NO tools, so the model has nothing
        # to do but answer from what it gathered. Running out of budget now costs completeness,
        # not the whole reply.
        answer_step = max(1, max_steps - 1)

        for i in range(max_steps):
            steps = i + 1
            last = i >= answer_step
            if last:
                messages.append({
                    "role": "user",
                    "content": (
                        "You are out of tool budget for this turn. Do not apologise and do not "
                        "ask to continue. Answer from what you have already established:\n"
                        "1. What you FOUND, with concrete evidence (ids, counts, timings).\n"
                        "2. What you could NOT determine, and the one check that would settle it.\n"
                        "3. The single next action you would take.\n"
                        "Partial findings are useful; a report of running out of steps is not."
                    ),
                })
            msg, _usage = self._adapter_obj().create_chat_completion(
                messages, tools=(None if last else schemas))
            messages.append(msg["raw"])
            tool_calls = None if last else msg.get("tool_calls")
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
            # Only reachable if the reserved answer step ALSO produced nothing, which means the
            # model returned an empty completion — a provider fault, not a budget one. Say which.
            final_text = final_text or (
                "I gathered evidence but the model returned an empty answer on the final step. "
                "That is a provider fault, not a limit on what BOSS knows — the same question "
                "asked again will usually work."
            )

        return AgentTurn(reply=final_text, trace=trace, pending_writes=pending, steps=steps)

    #: Tools that START EXPENSIVE, LONG-RUNNING WORK. From chat these are OFFERED, never taken.
    #:
    #: Asked to "deploy Living Colors to live", the agent tried redeploy, found the app had
    #: never deployed, and silently called retry_code_task instead — a fresh build worth
    #: minutes and real money, reported as "I've retried the failed task, which will redeploy".
    #: Told not to substitute, in the system prompt AND in redeploy's own failure message, it
    #: did it again on the very next attempt.
    #:
    #: Four instructions ignored is a design answer, not a prompting problem: an action whose
    #: cost the operator would want to authorise must not be reachable by the model deciding it
    #: is a good idea. These come back as buttons; one click runs them unchanged. Everything
    #: else — redeploy, previews, priorities, cancels, decisions, write_db — still executes
    #: directly, because those are cheap, reversible, or exactly what "fix it" means.
    PROPOSE_ONLY = frozenset({
        "create_code_task", "create_discovery", "request_meeting",
        "retry_code_task", "extend_code_task",
    })

    def _run_tool(
        self, name: str, args: dict, *, project_id: str, actor: str,
        allow_writes: bool, pending: list[dict],
    ) -> dict:
        try:
            spec = self.registry.get(name)
            # Models emit `functions/read_logs` for `read_logs`. registry.get resolves that,
            # but everything downstream (write-gate, invoke) must use the resolved name or the
            # tool is looked up twice under two spellings.
            name = spec.name
        except ToolError as exc:
            return {"ok": False, "error": str(exc)}
        if name in self.PROPOSE_ONLY:
            pending.append({"tool": name, "args": args})
            return {
                "ok": True, "proposed": True, "tool": name, "args": args,
                "label": _propose_label(name, args),
                "why": "starts new work and spends model budget",
                "note": (f"NOT run. {name} starts expensive work, so it is offered to the "
                         "operator as a button and executes only if they click it. Tell them "
                         "plainly that this is what you are offering and why — do not describe "
                         "it as something you have already done."),
            }
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
