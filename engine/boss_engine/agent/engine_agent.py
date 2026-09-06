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

import datetime
import decimal
import uuid
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



def _jsonable(value):
    """Make a tool result serialisable instead of killing the turn.

    "What is my spend so far this month?" answered with
    "TypeError: Object of type Decimal is not JSON serializable" -- the tool worked, read the
    real numbers, and the answer was destroyed encoding them. Postgres NUMERIC arrives as
    Decimal, timestamps as datetime, ids as UUID; any tool that returns a DB value unconverted
    hits this, so it is fixed once here rather than in each tool.

    Decimal -> float, because this is the model's copy of the result and it needs to compare and
    add; the authoritative value stays Decimal in the database.
    """
    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, (set, frozenset)):
        return sorted(value, key=str)
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


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

def _report_from_trace(trace: list[dict]) -> str:
    """What BOSS established, when the model would not write it up.

    Reached only when every attempt to get prose failed. The alternative that shipped was an
    empty string, which the router rendered as "I could not answer that: the model returned an
    empty answer" — telling an operator nothing, while the trace held the reads that answered
    their question. A list of what was looked up and how it came back is worse than a written
    answer and far better than silence, and it is verifiable in a way prose is not.
    """
    if not trace:
        return (
            "The model returned nothing for this turn and I had gathered no evidence yet, so "
            "I have nothing to report. That is a provider fault on my side, not a limit on "
            "what BOSS knows — asking again usually works."
        )
    lines = [
        "The model would not write up an answer for this turn, so here is what I actually "
        "checked and what came back. This is a fault on my side; ask again and you should get "
        "a written answer.",
        "",
    ]
    for step in trace[:12]:
        result = step.get("result") or {}
        ok = result.get("ok", True)
        detail = ""
        # The fields worth a line: what failed, or how much was found.
        if not ok:
            detail = str(result.get("error") or result.get("detail") or "failed")[:160]
        else:
            for key in ("count", "status", "state", "summary", "note", "title"):
                if result.get(key) not in (None, ""):
                    detail = f"{key}={str(result[key])[:120]}"
                    break
            if not detail:
                for key, value in result.items():
                    if isinstance(value, list):
                        detail = f"{key}: {len(value)} item(s)"
                        break
        lines.append(f"- {'✓' if ok else '✗'} {step.get('tool')}"
                     + (f" — {detail}" if detail else ""))
    if len(trace) > 12:
        lines.append(f"- …and {len(trace) - 12} further tool call(s).")
    return "\n".join(lines)


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
        #: Completions that came back with neither text nor a tool call. Two are tolerated.
        empty_completions = 0

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
                if final_text.strip():
                    break
                # AN EMPTY COMPLETION IS NOT AN ANSWER, AND `break` MADE IT ONE.
                #
                # No tool calls and no text ended the loop with final_text = "" — and because
                # `break` skips a for/else, the reserved answer step and the fallback below
                # were both bypassed. Every tool result gathered on the way was discarded and
                # the operator was told "I could not answer that: the model returned an empty
                # answer" about a question BOSS had already done the work to answer.
                #
                # An empty completion is a provider hiccup, the same class of fault as a 529,
                # and it is retried like one: press the model to answer, up to twice, before
                # giving up. `continue` rather than `break`, so the reserved final step still
                # gets its chance.
                empty_completions += 1
                if empty_completions <= 2 and not last:
                    messages.append({
                        "role": "user",
                        "content": (
                            "Your last message was empty. Answer the question now in plain "
                            "text, using what you have already established. Do not call any "
                            "more tools and do not apologise."
                        ),
                    })
                    continue
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
                        "content": json.dumps(result, default=_jsonable)[:16000],
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

        # LAST LINE OF DEFENCE: A TURN NEVER RETURNS AN EMPTY REPLY.
        #
        # Every path above tries to produce text, and the one that did not is the one that
        # reached an operator. If the model would not write a sentence, BOSS reports what it
        # DID — which is real, checkable evidence and is very often the answer — instead of
        # handing the router an empty string to turn into "I could not answer that".
        if not final_text.strip():
            final_text = _report_from_trace(trace)

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
        # EVERY CALL COSTS MONEY. An image is billed per generation, and an agent that decides
        # on its own that a page "needs a hero image" can spend real money on a hunch. The
        # operator sees the prompt and the destination and clicks, or does not.
        "generate_image",
    })

    #: THROWS AWAY WORK, OR REMOVES A SAFEGUARD. Always offered, never executed by the model.
    #:
    #: PROPOSE_ONLY above was built on one idea — "starts expensive work" — and the taxonomy had
    #: no category for the opposite. So `cancel_job` was an ordinary write, and asked why a build
    #: was slow, BOSS cancelled it: twenty-seven minutes of work destroyed to answer a question,
    #: with `force: true`, and the operator found out by reading the reply.
    #:
    #: Phrasing cannot be the control here. A gate that reads the operator's words is a
    #: heuristic and every heuristic has holes; the cost of a hole in THIS direction is
    #: destroyed work that nobody can get back. So these are unconditional: no sentence, however
    #: explicit, makes the model itself press them. The operator presses them.
    #:
    #: That is not a refusal to act — the button is right there in the reply, it carries the
    #: exact arguments BOSS chose, and one click runs it verbatim. It costs a click and it
    #: cannot cost an afternoon.
    DESTRUCTIVE = frozenset({
        "cancel_job",
        "cancel_code_task",
        # Removes a check somebody put there on purpose.
        "waive_gates",
        # Arbitrary mutation. `query_db` reads every table; this one can empty them.
        "write_db",
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
        if name in self.DESTRUCTIVE:
            pending.append({"tool": name, "args": args})
            return {
                "ok": True, "proposed": True, "tool": name, "args": args,
                "label": _propose_label(name, args),
                "why": "destroys work or removes a safeguard — the operator decides",
                "note": (f"NOT run. {name} throws away work or removes a check, so it is ALWAYS "
                         "offered as a button and never performed by you, whatever was asked. "
                         "Say plainly what you are offering and why you think it is right — and "
                         "do not describe it as something you have done. If the operator only "
                         "asked a question, ANSWER the question first; the offer is a footnote "
                         "to the answer, not the answer."),
            }
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
