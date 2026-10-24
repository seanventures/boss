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

import contextlib
import datetime
import decimal
import re
import uuid
import json
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from .._compat import services_module
from ..tools import ToolError, ToolRegistry, build_discuss_read_registry


_UNSCOPED_SQL_TOOLS = frozenset({"query_db", "write_db"})


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
- Claims about THIS SYSTEM — what is running, what it cost, what a run did, \
what is connected — must come from a tool result you received this turn. Never \
guess at state; that is what the tools are for.
- GENERAL KNOWLEDGE NEEDS NO TOOL. Where the Google Cloud Console is, what a \
YouTube API scope means, how OAuth works, what a SQL error means, which flag a \
CLI takes — answer it directly, the way a knowledgeable colleague would. No tool \
returns a public URL, and refusing to say one because no tool returned it sends \
the user to a search engine for something you already know. If you are unsure \
whether something has changed recently, say so in a clause — do not withhold \
the answer.
- ANSWER THE QUESTION THAT WAS ASKED, at the length it deserves. "What is the \
console link?" is a lookup: give the link first, on the first line. A question \
that turns on judgement gets your judgement. Do not wrap a one-line answer in \
framing, caveats, or a list of things to get right first — a correct answer \
buried four paragraphs down reads as evasion, and the user has to ask twice.
- NEVER NARRATE AN EMPTY LOOKUP. "I have no recorded position on this" or "I \
checked and there is nothing" is noise on a question that never needed one. \
Say nothing about what you did not find unless its absence IS the answer.
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
    if tool == "start_workflow":
        # NAME THE WORKFLOW, not the tool. "Research-Led Discovery: …" tells the operator which
        # of twelve processes is about to run and therefore what they get at the end; "start
        # workflow" tells them nothing they did not already know from asking.
        key = str((args or {}).get("workflow_key") or "").strip()
        name = key.replace("_", " ").title() if key else "Workflow"
        return f"{name}: {title}"[:70] if title else f"Start {name}"
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


@contextlib.contextmanager
def _no_deadline():
    yield


def _findings_from_trace(trace: list[dict]) -> tuple[list[str], Optional[dict]]:
    """The findings the tools themselves wrote down, and the first fix one of them named.

    diagnose_run (and the tools built like it) return `findings` and a `resolution` with real
    arguments — the diagnosis and the fix are already in the trace, whether or not the model
    got round to writing them up.
    """
    found: list[str] = []
    fix: Optional[dict] = None
    for step in trace:
        result = step.get("result") or {}
        if not isinstance(result, dict):
            continue
        for line in (result.get("findings") or [])[:6]:
            if str(line) not in found:
                found.append(str(line))
        resolution = result.get("resolution")
        if fix is None and isinstance(resolution, dict) and resolution.get("tool"):
            fix = resolution
    return found, fix


def _out_of_time_report(trace: list[dict], seconds: int, fix: Optional[dict],
                        found: list[str]) -> str:
    """The answer when the TURN's time ran out before the model wrote one (call_deadline.py).

    THE OWNER'S COMPLAINT, 2026-10-02: a turn ran to the console's 240 s line and ended in
    "Still working". What BOSS had found by then was in the trace and was thrown away with the
    turn. Now the turn ends inside its time and says three things: what was found, what was
    checked, and the one thing to do next.
    """
    lines = [f"I ran out of this turn's time ({seconds}s) before the model finished writing "
             "an answer, so here is what the checks themselves established."]
    if found:
        lines += ["", "What I found:"] + [f"- {line}" for line in found[:8]]
    checked = [f"- {step.get('tool')}" + (
        "" if (step.get("result") or {}).get("ok", True) is not False else " (failed)")
        for step in trace[:12] if step.get("tool") != "propose_action"]
    if checked:
        lines += ["", "What I checked:"] + checked
    lines.append("")
    if fix:
        lines.append(f"Next: {fix.get('label') or fix.get('tool')} — {fix.get('why') or ''}"
                     .rstrip(" —") + ". It is the button below; nothing runs until you press "
                     "it.")
    elif trace:
        lines.append("Next: ask me to continue from these findings — the checks above are "
                     "done and will not be repeated.")
    else:
        lines.append("Next: ask again in a sentence that names the run or app (for example "
                     "its crun_ id), so the first check can go straight to it.")
    return "\n".join(lines)


#: Arguments that turn an action into its strongest form: skip the checks, take everything,
#: do not stop at the first refusal.
_ESCALATION_ARGS = frozenset({"force", "hard", "all", "recursive", "skip_checks", "no_verify"})

#: The operator asking for it in their own words. Narrow on purpose — this is what LICENSES the
#: escalation, so a loose match here hands back exactly what was taken away.
_FORCE_WORDS = re.compile(r"\b(force|forcibly|hard|regardless|anyway|no matter what|"
                          r"skip the checks?|override)\b", re.IGNORECASE)


def _escalation_flags(args: dict) -> set[str]:
    """Escalating arguments the model set to a truthy value."""
    return {k for k in _ESCALATION_ARGS if args.get(k) not in (None, False, "", 0)}


def _asked_for_force(text: str) -> bool:
    return bool(_FORCE_WORDS.search(text or ""))


def _step_label(name: str, args: dict) -> str:
    """What BOSS is doing, in the operator's language rather than the schema's.

    "read_run_turns crun_33a0…" is not what somebody watching wants; "reading what the build
    actually did" is. The subject matters more than the verb — a label with no object reads as
    a spinner with extra words.
    """
    subject = ""
    for key in ("preview_id", "run_id", "job_id", "target_id", "app_name", "host", "service",
                "path", "query", "repo", "role", "sql"):
        value = str(args.get(key) or "").strip()
        if value:
            subject = value[:40]
            break
    friendly = {
        "diagnose_run": "diagnosing the run",
        "read_run_turns": "reading what the build actually did",
        "read_logs": "reading the service logs",
        "explain_run": "breaking down where the time went",
        "query_db": "querying the database",
        "search_code": "searching the code",
        "read_code": "reading the code",
        "list_code_tasks": "listing the work",
        "get_code_task": "reading the run",
        "inspect_host": "inspecting the host",
        "read_app_logs": "reading the app's logs",
        "recall_role_experience": "recalling what this role has argued here",
        "describe_role": "reading the role's remit",
        # The ones an "ask about this step" turn reaches for. "chaining tools" was all the dock
        # could say while these ran.
        "read_preview_log": "reading the preview's start-up log",
        "get_task_preview": "reading the build's previews and why they failed",
        "list_previews": "listing the previews",
        "describe_workflow_run": "reading the workflow run and its steps",
        "read_spec": "reading the build's PRD and acceptance criteria",
        "read_pull_request": "reading the pull request",
        "list_source": "listing the app's files",
        "describe_deployment_target": "reading the deployment target",
        "list_deployments": "listing the deployments",
        "preflight_deployment_target": "checking the target can be deployed to",
        "list_deployment_hosts": "listing the deployment hosts",
        "http_probe": "checking whether it answers",
        "list_decisions": "reading the open decisions",
        "list_discovery": "reading the discovery dossier",
        "propose_action": "preparing a button for the fix",
    }.get(name, name.replace("_", " "))
    return f"{friendly} — {subject}" if subject else friendly


#: Why a tool came back as an offer, in the words the operator should read.
_WHY_NOT_SELF_EXECUTED = {
    "destroys_work": "throws away work in flight, or removes a control — the operator decides",
    "irreversible": "cannot be undone once done — the operator decides",
    "grants_authority": "gives an agent reach on somebody's account — the operator decides",
}


class StreamedAnswer:
    """THE ANSWER'S STREAMING FORM (BOSSAPP-759): one model call, no tools, its words yielded as
    the model writes them, then its usage (a dict) last.

    This is the answer a turn with nothing to do gives — a seat speaking in a talk, a plan or a
    room (`discuss` writes nothing, which is the talk's rule) — and the form a turn's decision
    takes when it runs under a caller's spend envelope (`agent_decision.SpendEnvelope`). It
    streams through the adapter's own `stream_text_response` (the Claude CLI's stream-json, the
    OpenAI adapter's `stream=True`; the governed and fallback wrappers pass it through); an
    adapter that cannot stream answers whole, as one piece.

    LAZY, AND CLOSED AS THE PROVIDER'S. Nothing reaches the provider until the first `next()`,
    so an envelope that pins one attempt makes that contact inside its own single-attempt and
    deadline scope. `close()` closes the provider's own iterator (a stopped stream ends the
    CLI process) and lets its failure surface, so the envelope can hold what it cannot prove."""

    def __init__(self, adapter: Any, *, prompt: str, system_prompt: Optional[str]):
        self.adapter = adapter
        self.prompt = prompt
        self.system_prompt = system_prompt
        self._inner: Any = None

    def __iter__(self) -> "StreamedAnswer":
        return self

    def __next__(self) -> Any:
        if self._inner is None:
            stream = getattr(self.adapter, "stream_text_response", None)
            self._inner = (iter(stream(self.prompt, system_prompt=self.system_prompt))
                           if callable(stream) else self._whole())
        return next(self._inner)

    def _whole(self):
        reply = self.adapter.create_text_response(self.prompt, system_prompt=self.system_prompt)
        if isinstance(reply, tuple):
            yield str(reply[1] if len(reply) > 1 else reply[0] or "")
            yield reply[2] if len(reply) > 2 and isinstance(reply[2], dict) else {}
        else:
            yield str(reply or "")

    def close(self) -> None:
        close = getattr(self._inner, "close", None)
        if close is not None:
            close()


def stream_answer(adapter: Any, *, prompt: str, system_prompt: Optional[str]) -> StreamedAnswer:
    """The answer, streamed, with no tools: see `StreamedAnswer`."""
    return StreamedAnswer(adapter, prompt=prompt, system_prompt=system_prompt)


class EngineAgent:
    def __init__(self, ctx: Any, registry: Optional[ToolRegistry] = None,
                 adapter: Any = None):
        self.ctx = ctx
        self.registry = registry or build_discuss_read_registry(
            edition=getattr(getattr(ctx, "settings", None), "boss_edition", None)
        )
        #: An explicit backend, when the caller has chosen one. The chat agent runs on Claude
        #: Opus while everything else keeps the engine-wide default — a distinction that has to
        #: be made per consumer, because the worker image has no Claude CLI to run.
        self._adapter = adapter
        #: The project of the turn in flight; the routed model is this project's.
        self._project_id: Optional[str] = None
        #: The operator's own words for the turn in flight. Read by the escalation guard: only
        #: the operator can license `force`, so the guard has to see what they actually wrote.
        self._operator_text = ""
        #: Things the loop did to the model's own calls, to be reported rather than hidden.
        self._notes: list[str] = []
        #: Values every tool call gets whether the model asked for them or not — see invoke.
        self.ambient: dict[str, Any] = {}

    def _adapter_obj(self, project_id: Optional[str] = None):
        if self._adapter is None:
            # THE PROJECT'S MODEL, from the one router (services/model_routing.py). This was
            # `make_chat_adapter(settings)` — the deployment's `llm_engine`, "auto", the OpenAI
            # adapter — which on a subscription-only install answered nothing.
            routing = services_module("model_routing")
            project_id = project_id or self._project_id
            if project_id and getattr(self.ctx, "engine", None) is not None:
                with self.ctx.begin() as connection:
                    self._adapter = routing.project_chat_adapter(
                        connection, self.ctx.settings, project_id)
            else:
                self._adapter = routing.project_chat_adapter(None, self.ctx.settings, None)
        return self._adapter

    def chat(
        self,
        *,
        project_id: str,
        actor: str = "user",
        #: The operator's text, or OpenAI content blocks when they attached an image. A list
        #: is passed straight to the model and never to a string operation — see
        #: `_operator_text` below, which every heuristic in this class reads instead.
        message: Any,
        allow_writes: bool = False,
        max_steps: int = 6,
        history: Optional[list[dict]] = None,
        system: Optional[str] = None,
        #: Called with a short human label as each tool starts. The turn is one HTTP request,
        #: so without this the operator watches a dot for as long as the investigation takes —
        #: and the reliability work deliberately makes investigations longer.
        on_step: Optional[Callable[[str], None]] = None,
        #: The TURN's budget, shared by every pass in it (see services/turn_budget.py). When it
        #: is spent the next round-trip is the answer step, whatever `max_steps` says.
        budget: Any = None,
        #: What this pass is about, in the operator's words — "workflow step wfr_…/preview".
        #: Used in the progress line so the dock says what is being looked at, not a phase.
        focus: str = "",
    ) -> AgentTurn:
        """Run one agent turn. Returns the reply plus the tool trace and any
        write actions that were held for confirmation. `system` overrides the
        default system prompt (the router's answer path passes its own
        discuss-don't-claim-you-started framing)."""
        self._project_id = project_id or self._project_id
        # THE WORDS, WHATEVER SHAPE THE MESSAGE ARRIVED IN. `_operator_text` is matched against
        # regexes all over this class; a list of content blocks reaching one of them turns a
        # screenshot into a TypeError.
        self._operator_text = (
            message if isinstance(message, str)
            else " ".join(str(b.get("text") or "") for b in (message or [])
                          if isinstance(b, dict) and b.get("type") == "text")
        ) or ""
        self._notes = []
        schemas = [schema for schema in self.registry.openai_schema()
                   if schema["function"]["name"] not in _UNSCOPED_SQL_TOOLS]
        messages: list[dict] = [{"role": "system", "content": system or _SYSTEM_PROMPT}]
        if history:
            messages.extend(history)
        messages.append({"role": "user", "content": message})

        trace: list[dict] = []
        #: How many times each tool has FAILED this turn. See the nudge below `_run_tool`.
        tool_failures: dict[str, int] = {}
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
        #: THE BUTTON SURVIVES THE CLOCK. When the TURN's budget (not this pass's step limit)
        #: forces the answer, the model may still attach the fix it found as a button. A
        #: read → read → propose chain that crossed the budget used to end as prose describing a
        #: fix with nothing to press, and the action-claim re-ask that would have caught it does
        #: not start on a spent budget. propose_action changes nothing, so this cannot buy a
        #: write the turn was not given.
        #:
        #: IN THE SAME CALL AS THE ANSWER, INSIDE THE BUDGET. The first version gave the offer a
        #: round-trip of its own and then the answer another, so a turn forced to answer made
        #: model_calls + 1 calls and could run two full round-trips past its clock — ~474 s on
        #: boss-1's measured 157 s calls against the console's 240 s stall line (review of
        #: 374ba07). The forced answer step now offers propose_action alongside the answer: one
        #: call, its proposals run here without another model call, its text is the answer.
        offer_schemas = [sc for sc in schemas if sc["function"]["name"] == "propose_action"]
        offer_spent = False
        #: The turn's deadline cut a model call off: the next round-trip is the answer.
        out_of_time = False

        for i in range(max_steps):
            steps = i + 1
            # THE TURN'S BUDGET OUTRANKS THIS PASS'S STEP LIMIT. In the incident each pass was
            # within its own limit and the turn ran 276 seconds; the turn is what the operator
            # waits on, so the turn is what is counted.
            out_of_turn = out_of_time or (budget is not None and budget.must_answer_now())
            last = i >= answer_step or out_of_turn
            # The forced answer, with the button allowed in the same call — never a call of its
            # own (see `offer_schemas`).
            closing_offer = bool(last and out_of_turn and offer_schemas and not offer_spent)
            if last:
                messages.append({
                    "role": "user",
                    "content": (
                        ("This turn's budget is spent ("
                         + (budget.describe() if budget is not None else "its time is up")
                         + "). "
                         if out_of_turn else "You are out of tool budget for this turn. ")
                        + "Do not apologise and do not "
                        "ask to continue. Answer from what you have already established:\n"
                        "1. The answer to the operator's question, with concrete evidence.\n"
                        "2. What you could NOT check, in one sentence each, and why.\n"
                        "3. The single next action you would take.\n"
                        "Partial findings are useful; a report of running out of steps is not."
                        + ("\nIf you have found a fix and have not yet offered it, you may call "
                           "propose_action ONCE in this same message to attach it as a button "
                           "— and write the answer in this message too. There is no further "
                           "turn after this one."
                           if closing_offer else "")
                    ),
                })
            # THE SILENCE BEFORE THE FIRST TOOL WAS THE WHOLE PROBLEM.
            #
            # on_step fires per tool call, so the console showed real activity the moment tools
            # started — and nothing at all before that. On a long question the first model
            # round-trip takes a minute or more while it decides what to look at, and the
            # operator watched a generic "still working, this turn is running long" for the
            # entire time. Reported as wanting "some level abstract info what it is doing in
            # one line and keep refreshing".
            #
            # The phase IS the information: deciding what to look at, then weighing what came
            # back. Emitted before the round-trip rather than after, because a line that
            # appears once the work is done is not progress.
            # NAME THE THING, NOT THE PHASE. "Weighing what came back" for four minutes said
            # nothing the elapsed counter did not; "reading the record for workflow step
            # wfr_…/preview" says what is being looked at.
            if on_step is not None:
                try:
                    on_step(
                        "writing the answer" + (" — the turn's time is up" if out_of_turn else "")
                        if last
                        else (f"reading {focus} and deciding what is still open" if focus
                              else "reading your question and deciding what to look at")
                        if i == 0
                        else (f"weighing what came back about {focus}" if focus
                              else "weighing what came back, and what to check next")
                    )
                except Exception:  # noqa: BLE001 — never fail a turn to report progress
                    pass
            # RETRY THE MODEL CALL, NOT THE TURN.
            #
            # The chat path used to wrap this whole method in a 529 retry. Redeploy,
            # start_preview, write_db and decide execute directly when allow_writes is on, so a
            # capacity blip WHILE COMPOSING THE REPLY restarted the turn and ran the write
            # again. A redeploy is a new deployment record at the same host; a second
            # start_preview boots a second copy of the app. A hiccup is not a reason to ship
            # twice. The model call is the thing that 529'd; that is what gets another try.
            from .transient import _with_transient_retry

            from ..services.call_deadline import TurnDeadlineExceeded, until

            if budget is not None:
                budget.spend_call()
            try:
                # NO CALL OUTLIVES THE TURN (services/call_deadline.py). The budget used to be
                # consulted only BETWEEN calls, so one call that started at 170 s of a 180 s
                # turn ran on the CLI's own 240 s timeout — and was then retried and handed to
                # the fallback brain. A tool round-trip must end by the turn's time; the answer
                # gets the answer window after it, and not a second more.
                with until(budget.deadline(answering=last)) if budget is not None \
                        else _no_deadline():
                    msg, _usage = _with_transient_retry(
                        lambda: self._adapter_obj().create_chat_completion(
                            messages, tools=(offer_schemas if closing_offer
                                             else None if last else schemas)
                        )
                    )
            except TurnDeadlineExceeded:
                if not last:
                    # Cut off while still investigating: answer now from what is in hand.
                    out_of_time = True
                    continue
                # Cut off while answering: the trace still holds the findings and the fix.
                found, fix = _findings_from_trace(trace)
                if fix and not any(step.get("tool") == "propose_action" for step in trace):
                    offer = {"tool": fix["tool"], "args": fix.get("args") or {},
                             "label": fix.get("label") or fix["tool"],
                             "why": fix.get("why") or ""}
                    trace.append({"tool": "propose_action", "args": offer,
                                  "result": self._run_tool(
                                      "propose_action", offer, project_id=project_id,
                                      actor=actor, allow_writes=allow_writes,
                                      pending=pending)})
                final_text = _out_of_time_report(
                    trace, int(budget.elapsed) if budget is not None else 0, fix, found)
                break
            messages.append(msg["raw"])
            if closing_offer:
                # THE ANSWER AND ITS BUTTON, FROM ONE CALL. Only propose_action runs; whatever
                # else the model reached for is ignored. No further model call is made: the
                # text is the answer, and an empty one falls to the report from the trace below.
                offer_spent = True
                for tc in (msg.get("tool_calls") or []):
                    if tc.get("name") != "propose_action":
                        continue
                    try:
                        offer_args = json.loads(tc.get("arguments") or "{}")
                    except Exception:
                        offer_args = {}
                    result = self._run_tool(
                        "propose_action", offer_args, project_id=project_id, actor=actor,
                        allow_writes=allow_writes, pending=pending,
                    )
                    trace.append({"tool": "propose_action", "args": offer_args,
                                  "result": result})
                    break
                final_text = msg.get("content") or ""
                break
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
                if empty_completions <= 2 and (not last or closing_offer):
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
                # NO TOOL OUTLIVES THE TURN EITHER (services/call_deadline.py, tool_timeout).
                # Review of the 240 s fix: only the model calls were bounded, so a slow host,
                # URL or query inside a turn still carried it past the console's stall line and
                # the out-of-time report arrived after the console had said "Still working".
                # A tool is not started once the turn's tool time is spent — it is recorded as
                # not run, and the next call is the answer — and one that is started waits no
                # longer than that time.
                from ..services.call_deadline import MIN_TOOL_SECONDS
                from ..services.call_deadline import until as _until

                tool_left = (budget.seconds - budget.elapsed) if budget is not None else None
                if tool_left is not None and tool_left < MIN_TOOL_SECONDS:
                    out_of_time = True
                    result = {"ok": False, "not_run": True,
                              "error": (f"{name} was not run: this turn's time for tools is "
                                        "spent. Answer from what is already established and "
                                        "name this check as the next thing to do.")}
                    trace.append({"tool": name, "args": args, "result": result})
                    messages.append({"role": "tool", "tool_call_id": tc.get("id"),
                                     "content": json.dumps(result)})
                    continue
                if on_step is not None:
                    try:
                        on_step(_step_label(name, args))
                    except Exception:  # noqa: BLE001 — never fail a turn to report progress
                        pass
                with _until(budget.deadline(answering=False)) if budget is not None \
                        else _no_deadline():
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

                # THE SAME TOOL FAILING OVER AND OVER IS A FACT ABOUT BOSS, NOT ABOUT THE TASK.
                #
                # In one real thread `query_db` failed SEVEN times in eighteen calls —
                # `workflows`, `work_class`, `workflow_key`, `graph`, `run_id`,
                # `priority_band`, none of which exist — and the operator's question ("what is
                # the status?") was never answered. Reported as "it is going into a rabbit hole
                # and never coming out of the loop".
                #
                # The model could not see the pattern. Each failure arrives alone, looks like a
                # small mistake it can correct, and correcting it is always locally reasonable;
                # nothing in the turn ever said "this is the fourth time". So it retried
                # forever, because the harness never told it it was in a loop.
                #
                # BOSS can already read its own source (search_code) and raise a fix
                # (create_code_task). Asked why BOSS does not repair BOSS, this is the answer:
                # it had the tools and was never given the trigger. This is the trigger.
                if isinstance(result, dict) and (
                        result.get("ok") is False or result.get("error")):
                    tool_failures[name] = tool_failures.get(name, 0) + 1
                    if tool_failures[name] == 3:
                        messages.append({
                            "role": "user",
                            "content": (
                                f"SYSTEM: `{name}` has now failed {tool_failures[name]} times "
                                "this turn. Stop retrying it — you are in a loop and cannot "
                                "see it from inside.\n\n"
                                "Do this instead, in order:\n"
                                "1. Is there a PURPOSE-BUILT tool for what you are trying to "
                                "learn? Hand-written SQL is the fallback, not the first "
                                "resort — e.g. a workflow run (wfr_) is `describe_workflow_run`"
                                ", a code-task run (crun_) is `diagnose_run`.\n"
                                "2. If there is genuinely no tool for this, that is a GAP IN "
                                "BOSS. Use `search_code` to find the relevant platform code, "
                                "and `create_code_task` to raise the fix. That is a real, "
                                "expected part of your job.\n"
                                "3. Either way, ANSWER THE OPERATOR NOW with what you already "
                                "know, say plainly what you could not determine and why, and "
                                "give them a concrete next step. Do not end the turn with an "
                                "apology about your own tooling and nothing else."
                            ),
                        })
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
        "create_code_task",
        # A whole workflow — research, panel, gates, document — is the most expensive thing
        # BOSS can start from a sentence. It is offered; the operator presses it.
        #
        # `create_discovery` and `request_meeting` used to be here. They are not offerable at
        # all any more: research and the panel are STEPS, and one started on its own produces
        # an artefact no run links to. See _WORKFLOW_ONLY_TOOLS in tools/builtin.py.
        "start_workflow",
        "retry_code_task", "extend_code_task",
        # EVERY CALL COSTS MONEY. An image is billed per generation, and an agent that decides
        # on its own that a page "needs a hero image" can spend real money on a hunch. The
        # operator sees the prompt and the destination and clicks, or does not.
        "generate_image",
        # Arbitrary shell on a registered host always needs a visible button press.
        "run_on_host",
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
        # Removes a workflow a person authored. Drafting one is conversational; deleting one is
        # a press, for the same reason cancel_job is.
        "delete_workflow",
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
        if name in _UNSCOPED_SQL_TOOLS:
            return {"ok": False, "error": "raw SQL is unavailable in project-scoped agent turns"}
        # AN ESCALATION NOBODY ASKED FOR.
        #
        # Cancelling the build, the model passed `force: true` — a flag that overrides the very
        # checks that would have stopped it — and nothing in the operator's message asked for
        # force. A model reaching for the strongest form of an action it was not asked to take
        # at all is the failure twice over.
        #
        # These flags are dropped unless the operator's own words asked for them. Dropped, not
        # refused: the tool still runs in its ordinary form, and if the gentle form is genuinely
        # blocked the tool says so and the operator can ask for force in as many words.
        escalated = _escalation_flags(args)
        if escalated and not _asked_for_force(self._operator_text):
            args = {k: v for k, v in args.items() if k not in escalated}
            self._notes.append(
                f"dropped {', '.join(sorted(escalated))} from {name}: nothing in the request "
                "asked to force it")

        # DECLARED ON THE TOOL, NOT LISTED HERE.
        #
        # `DESTRUCTIVE` and `PROPOSE_ONLY` are hand-maintained name sets, which means a tool
        # added tomorrow is silently in neither — and `cancel_job` was exactly that: an
        # ordinary write until it destroyed twenty-seven minutes of a build. Every write now
        # declares what pressing it costs (`reversibility` on the ToolSpec), a registry test
        # requires the declaration, and this reads it. The sets remain as a belt-and-braces
        # backstop for anything that predates the declaration.
        if not spec.self_executable or name in self.DESTRUCTIVE:
            pending.append({"tool": name, "args": args})
            return {
                "ok": True, "proposed": True, "tool": name, "args": args,
                "label": _propose_label(name, args),
                "why": _WHY_NOT_SELF_EXECUTED.get(
                    spec.reversibility,
                    "destroys work or removes a safeguard — the operator decides"),
                "note": (f"NOT run. {name} is declared '{spec.reversibility}', so it is ALWAYS "
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
            # AMBIENT CONTEXT THE MODEL CANNOT KNOW AND MUST NOT HAVE TO GUESS.
            #
            # An investigation posts its result back into the conversation that asked for it,
            # which means it needs the thread id — a value that exists in the router and never
            # in the model's head. Asking the model to pass one would produce an invented
            # `athr_...`, which is the exact failure mode `_placeholder_argument` exists for.
            name, self.ctx, project_id=project_id, actor=actor,
            # `_writes_allowed` records WHO decided, which a tool cannot otherwise tell. The
            # write gate above has already passed, so reaching this line means the operator
            # instructed an action rather than asking a question — the distinction a tool that
            # escalates its own permissions (a fixing investigation) has to be able to make.
            args={**(self.ambient or {}), **args, "_writes_allowed": bool(allow_writes)},
        )
