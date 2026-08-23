from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..config import Settings
from .agent_process import AgentProcessService
from .budget import BudgetGovernor
from .canonical import CanonicalObjectService
from .capability_manifest import CapabilityManifest
from .claude_cli_adapter import make_chat_adapter
from .code_task_service import start_code_task
from .core import stable_dedupe_key, emit_event, make_id
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


#: Tools whose use CHANGED something, as opposed to reading. Surfaced to the console as
#: `actions` so an operator can see what BOSS did to their system without reading prose — and
#: so "it says it fixed it" is falsifiable.
_ACTING_TOOLS = frozenset({
    "redeploy", "retry_code_task", "extend_code_task", "steer_code_task", "cancel_code_task",
    "create_code_task", "create_discovery", "request_meeting", "register_repository",
    "start_preview", "stop_preview", "cancel_job", "set_job_priority", "close_orphaned_runs",
    "link_run_origin", "decide", "waive_gates", "send_feedback", "write_db",
})


@dataclass
class RouterTurn:
    reply: str
    actions: list[dict[str, Any]] = field(default_factory=list)
    pending_confirmation: Optional[dict[str, Any]] = None
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    #: Offers the operator can accept with one click. Nothing here has run.
    proposed_actions: list[dict[str, Any]] = field(default_factory=list)


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
    {
        "type": "function",
        "function": {
            "name": "create_agent_process",
            "description": (
                "Compile natural-language process/workflow/handoff authoring into "
                "a validated agent-process template. Use this when the user is "
                "defining a role chain, not asking to build an app."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "name": {"type": "string"},
                },
                "required": ["text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "start_agent_process",
            "description": (
                "Start an agent-process run against a PRD. Simulated by default. "
                "Never enable auto_build unless the user explicitly asked."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "prd": {"type": "string"},
                    "process_key": {"type": "string"},
                    "simulated": {"type": "boolean"},
                },
                "required": ["prd"],
            },
        },
    },
]


# Asked to deploy an app that had never been deployed, the model wrote a neat table of
# choices and ended with "Please choose the appropriate button" — and emitted no
# propose_action, so the console rendered no buttons. The operator is then stuck reading an
# instruction they cannot follow, which is worse than a plain refusal.
#
# Promising a control is only truthful if the control exists, and whether it exists is known
# HERE, after the turn — not by the model while it is writing. So it is checked here.
# Each time one phrasing was blocked the model invented another. Verbatim, in order:
#   "Please choose the appropriate button."
#   "| Button label | Tool | Arguments |"          (a markdown table)
#   "**[Start deployment build]** (click to create a new build)"   (a bracket pseudo-button)
# So this matches the ACT of promising a control, not one wording: any instruction to click or
# choose, and any bracketed label sold as clickable.
_BUTTON_PROMISE = re.compile(
    r"(choose|click|select|press|tap|use)\s+(the\s+)?(appropriate\s+|relevant\s+|one\s+|"
    r"below\s+|above\s+)?(button|option)s?\b"
    r"|below\s+are\s+the\s+(two|three|following)\s+possible"
    r"|\bclick\s+(to|here|below|above)\b"
    r"|\*\*\[[^\]]+\]\*\*"          # **[Start deployment build]**
    r"|\[\s*(start|retry|create|deploy|run|cancel)[^\]]{0,40}\]\s*\(click",
    re.I,
)


# "I'll monitor the deployment and update you when it completes." BOSS has no way to speak
# again on its own — a turn ends when it replies, and nothing wakes it. So this promise is
# always false, and it is the most damaging kind of false: the operator waits instead of acting.
_WILL_COME_BACK = re.compile(
    r"\b(i(?:'| a)?m going to |i(?:'ll| will) )?(keep (an eye|watching)|monitor|watch|track|"
    r"check back|follow up|report back|keep you (posted|updated)|update you|let you know)\b"
    r"[^.\n]*\b(and|as soon as|once|when|then|until)?[^.\n]*",
    re.I,
)
_HAS_STATUS_PATH = re.compile(r"\b(watch_code_task|the (run|deployment) page|refresh|ask me again)\b", re.I)


def _boss_cannot_call_you_back(reply: str) -> str:
    """Replace a promise of future contact with the way to actually find out.

    BOSS is turn-scoped: it answers when asked and is then not running. Saying it will "monitor
    and update you" describes a capability that does not exist, so the sentence is rewritten
    rather than trimmed — the operator still needs to know how to learn the outcome.
    """
    if not reply or not _WILL_COME_BACK.search(reply):
        return reply
    kept, dropped = [], False
    for line in reply.splitlines():
        if _WILL_COME_BACK.search(line) and not _HAS_STATUS_PATH.search(line):
            dropped = True
            continue
        kept.append(line)
    if not dropped or not "\n".join(kept).strip():
        return reply
    return "\n".join(kept).rstrip() + (
        "\n\nI cannot watch this in the background or message you when it changes — I only run "
        "while answering you. To see where it got to, ask me again (\u201cwhat happened to that "
        "deployment?\u201d) and I will read the current state."
    )


# A table is not a button.
#
# Told five separate times, in the system prompt, to give a described option a button, the model
# keeps writing "Proposed action | Button label | Tool | Arguments" as MARKDOWN and calling
# nothing. The operator reads "Please click the button above" and there is no button.
#
# Five ignored instructions is a design answer, not a prompting problem. But the model has
# already emitted everything a button needs -- the tool name and the exact JSON arguments -- so
# the offer is recovered from its own table rather than asking it again.
#
# Recovery is deliberately strict. Anything uncertain produces NO button, which is the current
# behaviour, rather than a wrong one: a button is an authorisation, and a wrong button spends
# the operator's money on a click they thought meant something else.
_TABLE_ROW = re.compile(r"^\s*\|(?P<cells>.+)\|\s*$")


# The model also writes the tool call as PSEUDO-MARKUP inside prose:
#
#   <propose_action tool="redeploy" args='{"run_id":"crun_..."}' label="Deploy to Staging" why="..."/>
#
# which renders as raw angle brackets in the operator's chat and produces no button. It is the
# third shape invented after the previous two were handled, which is the argument for recovering
# by MEANING rather than blocking one syntax at a time. This form is the most reliable of the
# three to recover, because every field is named.
_PROPOSE_TAG = re.compile(
    r"<\s*propose_action\b(?P<attrs>[^>]*?)/?\s*>",
    re.I | re.S,
)
_TAG_ATTR = re.compile(r"""(\w+)\s*=\s*(?P<q>["'])(?P<val>.*?)(?P=q)""", re.S)


def _recover_tagged_actions(reply: str, known_tools, existing) -> tuple[list, str]:
    """Pull <propose_action .../> tags out of the reply and turn them into real offers.

    Returns the offers AND the reply with the tags removed — leaving them in shows the operator
    raw markup where a button should be, which is worse than the missing button.

    Validated exactly like the table form: the tool must exist, args must parse, and no
    placeholder values. A tag that fails any of those is still STRIPPED from the text, because
    unparseable markup is never something the operator should read.
    """
    if not reply or "propose_action" not in reply:
        return [], reply
    from ..tools.builtin import _placeholder_argument

    found: list = []
    have = {(a.get("tool"), json.dumps(a.get("args") or {}, sort_keys=True)) for a in existing}

    def _take(match: "re.Match[str]") -> str:
        attrs = {m.group(1).lower(): m.group("val")
                 for m in _TAG_ATTR.finditer(match.group("attrs") or "")}
        tool = _strip_md(attrs.get("tool", ""))
        args = _loads_object(attrs.get("args", "") or "{}")
        if tool in known_tools and args is not None and not _placeholder_argument(args):
            key = (tool, json.dumps(args, sort_keys=True))
            if key not in have:
                have.add(key)
                found.append({
                    "id": make_id("act"), "tool": tool, "args": args,
                    "label": (_strip_md(attrs.get("label", "")) or tool)[:80],
                    "why": (_strip_md(attrs.get("why", "")) or None),
                })
        return ""      # strip the markup either way

    return found, _PROPOSE_TAG.sub(_take, reply)


def _recover_described_actions(reply: str, known_tools, existing) -> list:
    """Turn a proposal TABLE the model wrote into real, validated offers.

    Every one of these must hold or the row is dropped:
      * the Tool cell names a tool that actually exists;
      * the Arguments cell is valid JSON object syntax;
      * no argument is a placeholder (the crun_placeholder / owner-name guard);
      * the tool is not already offered for real.
    """
    if existing or not reply or "|" not in reply:
        return []
    from ..tools.builtin import _placeholder_argument

    rows = []
    header: list[str] = []
    for line in reply.splitlines():
        m = _TABLE_ROW.match(line)
        if not m:
            continue
        cells = [c.strip() for c in m.group("cells").split("|")]
        low = [c.lower() for c in cells]
        if "tool" in low and any("argument" in c for c in low):
            header = low
            continue
        if not header or set("".join(cells)) <= set("-: "):
            continue
        row = dict(zip(header, cells))
        tool = _strip_md(row.get("tool", ""))
        if tool not in known_tools:
            continue
        raw = _strip_md(row.get("arguments", "") or row.get("argument", ""))
        args = _loads_object(raw)
        if args is None or _placeholder_argument(args):
            continue
        label = _strip_md(row.get("button label", "") or row.get("action", "")) or tool
        why = _strip_md(row.get("what it does (cost)", "") or row.get("what it does", ""))
        rows.append({"id": make_id("act"), "tool": tool, "args": args,
                     "label": label[:80], "why": why[:240] or None})
    seen, out = set(), []
    for r in rows:
        key = (r["tool"], json.dumps(r["args"], sort_keys=True))
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


def _strip_md(text: str) -> str:
    """Cells arrive as **bold**, `code`, or both."""
    return re.sub(r"[*`]", "", text or "").strip()


def _loads_object(raw: str):
    """Parse the Arguments cell, tolerating the smart quotes a model emits in prose."""
    text = _strip_md(raw)
    if not text.startswith("{"):
        return None
    text = text.replace("\u201c", '"').replace("\u201d", '"').replace("\u2019", "'")
    try:
        value = json.loads(text)
    except Exception:  # noqa: BLE001 — an unparseable cell is simply not an offer
        return None
    return value if isinstance(value, dict) else None


def _no_button_was_promised(reply: str, proposed: list) -> str:
    """Strip a promise of buttons when no button was actually produced.

    Deliberately narrow: it only removes the SENTENCE making the promise, leaving the
    explanation and the options table intact — the content is useful, the instruction is not.
    """
    if proposed or not reply or not _BUTTON_PROMISE.search(reply):
        return reply
    kept = [ln for ln in reply.splitlines() if not _BUTTON_PROMISE.search(ln)]
    out = "\n".join(kept).rstrip()
    if out.strip() == reply.strip() or not out.strip():
        return reply
    # State ONLY what is known here: that no button was produced. The first version of this
    # named a cause -- "BOSS has no tool that deploys an app for the first time" -- which was
    # true of the reply it was written for and false of the next one, where BOSS had described
    # a perfectly available retry button. It then contradicted the model's own "click the button
    # above". A reconciler that invents a reason is just a second way to be wrong.
    return out + (
        "\n\n(No button was attached to this answer, so there is nothing to click. Tell me to "
        "go ahead and I will run the action directly, or ask me why it could not be offered.)"
    )


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
        inside the method to avoid any services<->agent import cycle.

        Uses ``build_discuss_read_registry`` (not the full read set): unrestricted
        ``query_db`` and cross-project ``project_id`` overrides are excluded so a
        chat actor authorized for one project cannot read another tenant.
        """
        if self._answer_agent_obj is None:
            from ..agent.engine_agent import EngineAgent
            from ..context import EngineContext
            from ..db import get_engine
            from ..tools import build_discuss_read_registry

            ctx = EngineContext(settings=self.settings, engine=get_engine(self.settings))
            self._answer_agent_obj = EngineAgent(
                ctx, registry=build_discuss_read_registry()
            )
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

        # THE MODEL ROUTES. THE REGEX IS WHAT HAPPENS WHEN THERE IS NO MODEL.
        #
        # This used to run in the opposite order: a keyword matcher decided first, a classifier
        # second, and the AGENT — the only component holding the fifty tools and the run's
        # actual state — was the last resort, reached only when both abstained.
        #
        # Every misroute in this file's history comes from that inversion. Two occurrences of
        # the word "and" became a multi-phase plan. A pasted WHERE clause became a plan. The
        # bare word "fix" became a governed code task, so "the deployment failed, fix it and
        # retry" spawned a build — real money — when the answer was one redeploy call. Each was
        # patched by narrowing the pattern, which is the wrong repair: a regex cannot know that
        # "fix" means redeploy HERE and create_code_task THERE, because the difference is the
        # state of the run, and the regex has never seen it.
        #
        # The agent can. It holds every capability the deterministic branches below actuate —
        # create_code_task, request_meeting, create_discovery — plus the reads to decide
        # between them. Handing it the sentence IS the routing decision, and it is the same
        # design every competent coding agent uses: tools, a model, and no keyword layer in
        # front deciding what the model is allowed to consider.
        #
        # `_deterministic_intent` stays for two callers that genuinely need it: this method
        # when no model is configured, and `classify_message`, which community-facing surfaces
        # use so an untrusted GitHub issue cannot create work merely by being classified.
        # Gated on whether a MODEL EXISTS, not on `agent_router_llm_enabled`.
        #
        # I first gated this on that flag, which defaults to False and is unset in production —
        # so the inversion was dead code and the regex stayed in charge. Worse, my own
        # verification passed: the sentences I tested contained no build verbs, the matcher
        # abstained, and the turn reached the agent anyway. I read "the regex had no opinion"
        # as "the agent is routing". The next message that did contain one — "list the tools
        # you can call that CHANGE something" — spawned a governed code task.
        #
        # That flag governs the older LLM CLASSIFIER (`_model_intent`), a different mechanism
        # from the tool-calling agent. The agent is the same one that answers every question in
        # this console today; if a model is configured at all, it is available.
        if self.settings.openai_api_key or self.settings.azure_openai_key:
            return self._answer(connection, project_id, messages)

        # No model available — fall back to keywords rather than doing nothing.
        name, arguments = self._deterministic_intent(latest)
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
            "create_agent_process": "agent_process",
            "start_agent_process": "agent_process",
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
        # AN OPERATOR WITH CONSOLE ACCESS, not a menu of canned reports.
        #
        # The tools now reach everything: arbitrary SQL over every table, both source trees
        # (BOSS's own and the customer's app), service logs, live hosts over SSH, HTTP probes,
        # git history and effective config. That breadth is wasted if the model answers from
        # the conversation instead of looking — the previous prompt listed four example tools,
        # so anything outside those four got a plausible guess. Say plainly: look first, and
        # keep looking until the answer is grounded.
        system = (
            "You are BOSS, the operator console for an autonomous software factory. You have "
            "COMPLETE access to this system and you OPERATE it — you are expected to use it "
            "like an engineer with a terminal and root, not to answer from memory.\n\n"
            "WHAT YOU CAN REACH:\n"
            "• query_db — arbitrary SELECT over EVERY table. describe_schema first if you do "
            "not know the table or columns; there are ~244 tables, including events (the full "
            "audit stream), receipts, llm_calls, and deployment_health_samples (uptime, "
            "latency, availability).\n"
            "• search_code / read_code — BOSS's own source AND the customer's application "
            "(repo='owner/name'). Read the implementation rather than describing what it "
            "probably does.\n"
            "• read_logs — what a service actually logged.\n"
            "• inspect_host — read-only commands on a deployment host (docker ps, container "
            "logs, disk). The database says what BOSS INTENDED; this says what the machine is "
            "doing.\n"
            "• http_probe — is a URL up right now.\n"
            "• git_history — when something changed and why.\n"
            "• get_config / explain_run / get_run_activity — effective settings; where a run's "
            "wall clock and money went.\n\n"
            "WHAT YOU CAN CHANGE — you are not limited to describing problems:\n"
            "• find_starved_work — work that is QUEUED and not progressing, and what holds the "
            "worker slot. Starvation is the ABSENCE of progress, so nothing appears as failed "
            "and no error-driven check will show it. Reach for this whenever someone says "
            "something is stuck, slow, or has not started.\n"
            "• set_job_priority — move a job between bands (urgent / interactive / requested / "
            "background / bulk). Also clears priority_score, which overrides the bands entirely.\n"
            "• cancel_job — drop a queued job; force=true stops a RUNNING one that is wedged and "
            "holding a slot. It closes the run too, so nothing is left reported as in flight.\n"
            "• close_orphaned_runs — runs claiming to be running with no live job.\n"
            "• redeploy — SHIP THE SAME BUILD AGAIN when a deployment failed. This is the "
            "answer to \"fix and retry the deployment\": do it, then report what the health "
            "check returned. It re-ships an EXISTING build — no rebuild, no model, no spend — "
            "and its steps are idempotent. Run preflight_deployment_target first if the failure "
            "looks like the host was unreachable.\n"
            "• inspect_self — the container BOSS itself runs in: CPU load per core, cgroup "
            "throttling, memory against the container ceiling, OOM-KILL COUNT, free disk, and "
            "the heaviest processes. Reach for this FIRST when a process died without an "
            "explanation (`exited -9`, `exited 137`, SIGKILL) — the OOM count settles in one "
            "number what otherwise takes an hour of guessing — and whenever the API is slow "
            "while a build is running, because load-per-core says whether anything is actually "
            "contending. The database says what BOSS intended; this says what the machine did "
            "to it.\n"
            "• consistency_report — every invariant at once: runs working with no job, jobs "
            "for runs that ended, decisions blocking runs that are gone. Run it when someone "
            "reports something inconsistent, before forming a theory.\n\n"
            "WHEN ASKED TO FIX SOMETHING, FIX IT. You are an autonomous system, not an "
            "advisor. \"Fix and retry the deployment\" means CALL redeploy — it does not mean "
            "describe what a fix would look like and offer to do it. Never end a reply with "
            "\"let me know if you would like me to\" for something you can already do; do it "
            "and report the result.\n\n"
            "IF THE ACTION THEY ASKED FOR CANNOT BE DONE, SAY SO. NEVER SUBSTITUTE A BIGGER "
            "ONE.\n"
            "Asked to \"deploy Living Colors to live\", BOSS called redeploy, got back "
            "\"no deployment run for this task\" — the app had never deployed, its last build "
            "FAILED — and then quietly called retry_code_task instead, starting a fresh build "
            "worth minutes and real money. It reported this as \"I've retried the failed task, "
            "which will redeploy Living Colors\", conflating the two so the operator could not "
            "tell a build had begun.\n"
            "DEPLOY IS NOT BUILD. RETRY IS NOT DEPLOY. When the requested action returns "
            "no_action or fails, the answer is: what you asked for is not possible, THIS is "
            "why, and HERE is what would make it possible — as a propose_action button. The "
            "operator decides whether to spend. Substituting silently is worse than refusing, "
            "because they cannot see it happen.\n"
            "Describe what you did in the words of the tool you actually called.\n\n"
            "WHEN THE OPERATOR CORRECTS YOU, THE CORRECTION WINS.\n"
            "Told 'no, use the latest working one — the preview works fine, why are you only "
            "checking the last run', BOSS checked the newest run again and repeated that there "
            "was no build. The operator had just given the answer and been ignored twice.\n"
            "A correction is EVIDENCE, and usually better evidence than what you looked at "
            "first — they can see the console. Treat it as a fact to act on: widen the search, "
            "query for the run they described, and say what you found. If a preview is serving, "
            "a build EXISTS; 'there is no build' is then wrong, whatever the newest run says.\n"
            "NEVER return the same conclusion twice in one conversation. If you are about to "
            "repeat yourself, you have not done the extra lookup they asked for — do it. If you "
            "genuinely cannot find what they describe, say exactly where you looked and what "
            "you searched for, so they can point you at the right place.\n\n"
            "ANSWER THE QUESTION THEY ASKED, AT THE LENGTH IT DESERVES.\n"
            "Most answers are one to three sentences. Lead with the answer, not with what you "
            "checked to find it. Do not narrate your tool calls, do not restate the question, "
            "do not list what you ruled out, and do not add next steps nobody asked for.\n"
            "NEVER write a table of 'Proposed action | Button label | Tool | Arguments'. That "
            "table is not a button — the operator reads 'click the button above' and there is "
            "nothing to click. To offer an action you CALL the tool; the console turns it into "
            "a button. Prose about a tool is not an offer.\n"
            "Use a table only for real evidence with more than two rows. One fact is a "
            "sentence.\n\n"
            "OFFER, DO NOT ASSUME, WHEN THE FIX HAS A CHOICE IN IT.\n"
            "Call propose_action to put an action behind a BUTTON when it COSTS money or is "
            "hard to undo and they did not ask for that exact thing — and especially when a "
            "CHEAPER repair exists. A real case: told a deployment card showed a stale, wrong "
            "URL, the right answer was two buttons — 'Clear the stale URL' (free, instant) and "
            "'Redeploy the app' (minutes, real money) — because 'this link is wrong' does not "
            "say which one they want. Instead one expensive path was assumed and a made-up "
            "domain was written to the database.\n"
            "Lead with the CHEAPEST action that could satisfy the request. Propose several when "
            "several are reasonable; each becomes its own button.\n"
            "IF YOU DESCRIBE AN OPTION IN PROSE, GIVE IT A BUTTON. Listing 'clear the URL' and "
            "'redeploy' in a table and then proposing only the expensive one leaves the "
            "operator to ask for the cheap one by hand — which is the thing that made them ask "
            "for buttons in the first place.\n"
            "NEVER PROPOSE WITH AN ARGUMENT YOU INVENTED. A button carrying "
            "run_id='crun_placeholder' fails the moment it is trusted, and the operator finds "
            "out by pressing it. Look the value up first — query_db, list_code_tasks — and "
            "propose with the real one. propose_action REFUSES obvious placeholders.\n"
            "This is NOT licence to stop acting. If they said 'redeploy it', call redeploy. A "
            "button for something they already asked for is the same stalling as advice.\n\n"
            "TWO SUBJECTS, ONE RULE EACH:\n"
            "• THE CUSTOMER'S WORLD — their app, its code, deployment, preview, environment, "
            "observability, and the queue their work sits in. FIX IT. Completely, without "
            "asking. A bug in their app is repaired by WRITING CODE, so create_code_task and "
            "retry_code_task are fix tools; use them. Deployment failed → redeploy. Preview "
            "dead → start_preview. Gate blocking → decide or waive_gates. Job wedged → "
            "cancel_job. There is no request in this category you should answer with advice.\n"
            "• BOSS'S OWN FOUNDATION — this engine, the API, the console, the deploy and "
            "preview machinery. You do NOT patch yourself. Say plainly: \"this is a BOSS "
            "framework bug\", name the file and the exact cause, and state what a human must "
            "change. That is not a permission you lack — it is a different subject, and an "
            "agent that edits the floor it stands on is how a control plane loses its own "
            "control plane. Be specific enough that the fix is obvious to whoever picks it "
            "up.\n\n"
            "Telling the two apart is usually easy: if the defect is in the repo the run "
            "targets, it is theirs. If it is in how BOSS built, previewed, deployed or watched "
            "that repo, it is ours.\n\n"
            "NEVER INVENT A VALUE YOU WRITE. Every value that lands in the database must come "
            "from somewhere you READ — a row, a config, a log, the operator's own words. Asked "
            "to fix an incorrect deployment URL, a previous answer wrote "
            "`living-colors.example.com` into deployment_targets with the reason \"Fix domain\". "
            "That domain exists nowhere in the system: one wrong URL was replaced with a "
            "different wrong URL, and it became persisted state. If you do not KNOW the correct "
            "value, the honest repairs are to CLEAR the field, or to ask which value is right — "
            "both are better than a plausible invention, because a wrong value that looks real "
            "is harder to find later than an empty one.\n\n"
            "NEVER INVENT EVIDENCE. Asked why a deployment failed, a previous answer stated the "
            "app hard-coded `app.listen(3000, \"127.0.0.1\")` in `src/server.ts`. That file did "
            "not exist and the real server read $PORT/$HOST correctly — acting on it would have "
            "changed working code and left the real bug. If you have not READ the line, do not "
            "quote it. \"I could not find it, here is the one check that would\" is a better "
            "answer than a plausible one.\n\n"
            "When an operator reports something stuck, DIAGNOSE AND THEN FIX IT. Do not hand "
            "back an explanation and wait to be asked. Say what you changed and why. A real "
            "case: a PRD meeting sat queued for two hours behind benchmark jobs boosted past "
            "every priority band — one find_starved_work and one cancel_job would have ended it "
            "in seconds, and instead a human had to reach in from outside.\n\n"
            "The boundary: you change OPERATIONAL STATE — priorities, wedged jobs, orphaned "
            "runs. You do NOT edit BOSS's own source; that goes through the governed coding "
            "pipeline behind a PR. And starting NEW work that spends money stays a deliberate "
            "request from the operator.\n\n"
            "HOW TO ANSWER:\n"
            "1. Investigate before answering. Chain tools — find the run, read its timings, "
            "then read the code or logs that explain them. Several calls is normal and correct.\n"
            "2. If a dedicated tool does not exist, that is NOT a reason to give up: "
            "describe_schema + query_db can answer almost anything the system has recorded.\n"
            "3. Ground every factual claim in something you read. Quote the number, the log "
            "line, the file:line. Never present a guess as a finding.\n"
            "4. If you genuinely cannot determine something, say exactly what you looked at "
            "and what was missing — that is a useful answer; a confident invention is not.\n\n"
            "If they clearly want you to BUILD or CHANGE something, briefly say what you'd do "
            "and that you'll start once they confirm (e.g. 'build it' / 'go') — do NOT claim "
            "you already started anything, and do NOT invent facts."
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
        # WHY THE ANSWER IS MISSING, IF IT IS.
        #
        # Every failure here used to collapse into `reply = ""` and then into a friendly
        # sentence about telling BOSS what to do. A budget cap, a model outage, a tool raising
        # — all three produced the SAME text as a model deliberately choosing to be
        # conversational. So an agent that was broken and an agent that was working looked
        # identical from the outside, and the operator's reasonable conclusion was that the
        # agent is useless rather than that something failed. Say which it was.
        failure: Optional[str] = None
        # What BOSS DID this turn, not just what it said. Surfaced on the RouterTurn so the
        # console can render it and an operator can audit it.
        actions: list[dict[str, Any]] = []
        tool_calls: list[dict[str, Any]] = []
        proposed: list[dict[str, Any]] = []
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
                        # FOUR STEPS WAS THE BUG, not a tuning choice.
                        #
                        # A step is one model round-trip, and a step that calls tools produces
                        # no text. The system prompt above lists ~47 tools and says "chain
                        # tools — several calls is normal and correct"; four round-trips were
                        # consumed investigating and none was left to answer, so every real
                        # diagnostic question returned "(reached the step limit while working)"
                        # with none of the findings. Only trivial one-lookup questions worked,
                        # which is why it looked intermittent rather than broken.
                        #
                        # Twelve leaves room for a genuine chain — find the run, read its
                        # timings, then read the code or logs that explain them — and the agent
                        # reserves the last one for the answer, so exhausting the budget now
                        # costs completeness rather than the entire reply.
                        # ASKED TO FIX IT, BOSS FIXES IT.
                        #
                        # This was False, so even the operational tools BOSS is explicitly
                        # trusted with were parked as "confirmation_required" and the reply
                        # became advice. An operator who says "fix and retry the deployment"
                        # and receives an essay ending in "let me know if you would like me
                        # to" has not been kept safe; they have been ignored by a system whose
                        # entire premise is that it acts.
                        #
                        # The boundary is the REGISTRY, not this flag — and it is now
                        # customer-world vs BOSS-foundation, not spend vs no-spend. Every write
                        # is reachable, create_code_task included, because a bug in a customer
                        # app is repaired by writing code. What BOSS does not do is patch its
                        # own engine; that is a different subject, not a withheld permission.
                        allow_writes=True, max_steps=12,
                    )
                    reply = str(turn.reply or "").strip()
                    # WHAT IT DID, NOT JUST WHAT IT SAID.
                    #
                    # `turn.trace` was discarded, so when BOSS redeployed an app the console
                    # showed `actions: []` and the operator had only prose to go on. Now that
                    # BOSS actually changes things, every write it performed has to be visible
                    # and auditable — otherwise "it says it fixed it" is unfalsifiable.
                    # getattr, not turn.trace: an answerer without a trace (a stub, an older
                    # adapter) must not turn a perfectly good reply into "I could not answer
                    # that" via the outer except. Reporting the work is secondary to delivering
                    # the answer.
                    for step in (getattr(turn, "trace", None) or []):
                        result = step.get("result") or {}
                        tool_calls.append({"tool": step.get("tool"),
                                           "args": step.get("args"),
                                           "ok": bool(result.get("ok", True))})
                        if step.get("tool") in _ACTING_TOOLS and not result.get("proposed"):
                            actions.append({"kind": str(step.get("tool")),
                                            "args": step.get("args"),
                                            "result": result})
                        # An OFFER, not a deed. Two sources, one shape:
                        #   * propose_action — the agent CHOSE to offer rather than act;
                        #   * a PROPOSE_ONLY tool — the agent tried to act and the agent loop
                        #     turned it into an offer, because that tool starts expensive work.
                        # Keying on the RESULT rather than the tool name covers both, and
                        # covers the next tool added to either set.
                        if result.get("proposed"):
                            proposed.append({
                                "id": make_id("act"),
                                "tool": result.get("tool") or step.get("tool"),
                                "args": result.get("args") or {},
                                "label": result.get("label"),
                                "why": result.get("why"),
                            })
                    if not reply:
                        failure = "the model returned an empty answer"
                finally:
                    try:
                        self.budget.release(connection, project_id, reservation["reservation_id"], "answer")
                    except Exception:  # noqa: BLE001
                        pass
            else:
                # The single most likely cause, and previously invisible: no budget was
                # granted, so the model was never called at all.
                failure = (
                    f"no model budget was granted for this answer "
                    f"(reservation {reservation.get('status') or 'refused'})"
                )
        except Exception as exc:  # noqa: BLE001 — a hiccup must never break the chat
            failure = f"{type(exc).__name__}: {exc}"
        if not reply:
            if failure:
                try:
                    emit_event(connection, project_id, "agent.answer_unavailable", "orchestrator",
                               "project", project_id, {"reason": failure[:400]})
                except Exception:  # noqa: BLE001
                    pass
                reply = (
                    f"I could not answer that: {failure}. This is a failure on my side, not a "
                    "limit on what BOSS knows — the question is worth asking again once it is "
                    "fixed. You can still tell me to act (e.g. “build it”, “add a currency "
                    "picker”) and I will start the governed flow."
                )
            else:
                reply = ("I can help discuss this. When you want me to act, tell me explicitly — e.g. "
                         "“build it”, “add a currency picker”, or “deploy it” — and I’ll start the governed flow.")
        # The model described buttons instead of calling the tool -- again. Recover them.
        try:
            from ..tools import build_discuss_read_registry
            _known = set(build_discuss_read_registry().names())
        except Exception:  # noqa: BLE001 — recovery is a bonus, never a new failure mode
            _known = set()
        tagged, reply = _recover_tagged_actions(reply, _known, proposed)
        proposed.extend(tagged)
        proposed.extend(_recover_described_actions(reply, _known, proposed))
        reply = _no_button_was_promised(reply, proposed)
        reply = _boss_cannot_call_you_back(reply)
        return RouterTurn(reply=reply, actions=actions, tool_calls=tool_calls,
                          proposed_actions=proposed)

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
        # THE CHAT MAY BE SCOPED TO A RUN, NOT A TASK.
        #
        # The console's agent dock is opened from an app card, and an app card holds a RUN id
        # (crun_...) — that is what makes the conversation unambiguous. This lookup only knew
        # how to find an app by task_id, so a run-scoped chat found nothing, `extend` came out
        # False, and "add a currency picker" would have quietly REBUILT the app on main instead
        # of changing it. The most expensive possible misreading of a cheap request.
        if str(task_id).startswith("crun_"):
            row = connection.execute(
                text("SELECT id, finalize_ctx FROM code_task_runs "
                     "WHERE project_id = :p AND id = :r LIMIT 1"),
                {"p": project_id, "r": task_id},
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
                    dedupe_key=stable_dedupe_key("agent-code", project_id, title, instructions),
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
                dedupe_key=stable_dedupe_key("agent-prd", project_id, directive),
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
            # Say what will actually happen. This read "Publishing remains human-gated" long
            # after auto-approval shipped, so the console told an operator a human still stood
            # between the panel and a build while the PRD was approved thirty seconds later.
            gated = not getattr(self.settings, "meeting_prd_auto_approve", False)
            return RouterTurn(
                reply=("Convened a governed PRD panel. Publishing is human-gated — it will wait "
                       "for your approval." if gated else
                       "Convened a governed PRD panel. Its PRD publishes automatically and "
                       "starts a build; cancel the run if that is not what you wanted."),
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

        if name == "create_agent_process":
            self._require(connection, project_id, actor, "task_create")
            text_in = str(arguments.get("text") or "").strip()
            if not text_in:
                raise ValueError("process text is required")
            given_name = str(arguments.get("name") or "").strip()
            try:
                spec = self._compile_agent_process(text_in, name=given_name or None)
                template = AgentProcessService(self.settings).create_template(
                    connection,
                    project_id=project_id,
                    actor=actor,
                    process_key=str(spec.get("key") or given_name or "custom"),
                    name=given_name or str(spec.get("name") or self._short_title(text_in, "Custom process")),
                    description=str(spec.get("description") or text_in)[:1000],
                    spec=spec,
                )
            except ValueError:
                raise
            except Exception as error:  # noqa: BLE001 — fail closed into a user-visible reply
                raise ValueError(str(error) or "Could not create the agent process.") from error
            action = {
                "kind": "agent_process_template_created",
                "template_id": template["id"],
                "process_key": template.get("process_key"),
                "href": f"/?area=agent_processes&template={template['id']}",
            }
            return RouterTurn(
                reply=(
                    f"Created agent process template “{template.get('name')}” "
                    f"({template.get('process_key')})."
                ),
                actions=[action],
                tool_calls=[{"name": name, "arguments": arguments}],
            )

        if name == "start_agent_process":
            self._require(connection, project_id, actor, "task_create")
            prd = str(arguments.get("prd") or "").strip()
            if not prd:
                raise ValueError("prd is required")
            process_key = (
                str(arguments.get("process_key") or "prd_to_accepted_work").strip()
                or "prd_to_accepted_work"
            )
            simulated = self._tool_bool(arguments.get("simulated"), default=True)
            # Chat never starts a live factory build unless the caller set auto_build=true.
            auto_build = self._explicit_true(arguments.get("auto_build"))
            try:
                run = AgentProcessService(self.settings).start_run(
                    connection,
                    project_id=project_id,
                    actor=actor,
                    prd=prd,
                    title=self._short_title(prd, "PRD delivery"),
                    process_key=process_key,
                    simulated=simulated,
                    auto_build=auto_build,
                )
            except ValueError:
                raise
            except Exception as error:  # noqa: BLE001 — fail closed into a user-visible reply
                raise ValueError(str(error) or "Could not start the agent process.") from error
            mode = "simulated" if simulated else "live"
            build = "auto_build" if auto_build else "no auto_build"
            action = {
                "kind": "agent_process_started",
                "run_id": run["id"],
                "process_key": process_key,
                "simulated": simulated,
                "auto_build": auto_build,
                "href": f"/?area=agent_processes&run={run['id']}",
            }
            return RouterTurn(
                reply=f"Started agent process “{run.get('title') or process_key}” ({mode}, {build}).",
                actions=[action],
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
        # A QUESTION ANYWHERE MAKES THIS A QUESTION.
        #
        # This looked only at the START of the message or a trailing "?", so a question with
        # context in front of it — "the run is blocked, the console shows nothing, why?" — or
        # one followed by a detail line fell through to the work-spawning matchers below.
        anywhere_question = "?" in message and not polite_imperative
        # An interrogative can sit in the middle with no question mark at all:
        # "the build failed and the deploy failed, what happened". That is a report plus a
        # question, and it was being read as an order to build something.
        #
        # Scoped by how the message OPENS: a sentence that begins with an imperative verb is an
        # instruction whatever it mentions later ("build a page that shows what users did"),
        # while one that opens with a subject is describing a situation.
        starts_imperative = bool(re.match(
            r"^\s*(build|create|make|implement|add|fix|change|refactor|remove|update|deploy|"
            r"ship|write|generate|set up|research|compare|investigate|design|plan|start|run)\b",
            lowered))
        embedded_question = (
            not starts_imperative
            and not polite_imperative
            and bool(re.search(r"\b(why|what|how|when|which|who|whose)\b", lowered))
        )
        # Pasted SQL is evidence for a QUESTION, never an order. It arrives when someone is
        # investigating, and its ANDs and verbs are what tripped the phase detector.
        looks_like_sql = bool(re.search(r"\bselect\b.+\bfrom\b", lowered, re.S))
        lead_question = (
            starts_discussion
            or anywhere_question
            or embedded_question
            or looks_like_sql
            or (message.strip().endswith("?") and not polite_imperative)
        )

        # A QUESTION IS A QUESTION, whichever verb it happens to contain.
        #
        # This guard existed but was applied to `create_code_task` ALONE, while every other
        # work-spawning intent was matched above it — so "why did this take so long, can you
        # investigate?" hit the `investigate` keyword, dispatched run_discovery, reserved
        # budget, and came back as "I couldn't start that work: Burst cap exceeded ($/min)".
        # The operator asked a question about an existing run and was told their spend limit
        # was hit, which explains neither what happened nor what they asked.
        #
        # Returning None routes to `_answer`, which discusses and spends nothing beyond the
        # conversational budget. The cost of being wrong here is asymmetric: a missed build
        # order is one follow-up message, a spurious build is real money and a junk task.
        if lead_question:
            return None, {}

        # Process/workflow/handoff authoring and starts beat multi-phase and generic build.
        # "build a settings page" must stay create_code_task; a role-handoff process must not.
        if self._looks_process_start(lowered):
            arguments: dict[str, Any] = {
                "prd": self._extract_prd(message),
                "simulated": True,
            }
            process_key = self._extract_process_key(lowered)
            if process_key:
                arguments["process_key"] = process_key
            return "start_agent_process", arguments
        if self._looks_process_authoring(lowered):
            arguments = {"text": message}
            given_name = self._extract_process_name(message)
            if given_name:
                arguments["name"] = given_name
            return "create_agent_process", arguments
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
        # "FIX THE DEPLOYMENT" IS NOT A REQUEST TO WRITE CODE.
        #
        # This matched the bare word `fix` and spawned a code task, so
        # "the deployment for run X failed with health check 000, fix it and retry" became a
        # governed build — a new run, real money — when the correct action was one redeploy
        # call. The operator asked for a fix and got the most expensive possible interpretation
        # of the word.
        #
        # When the subject is something BOSS OPERATES rather than something it authors, hand
        # the turn to the agent instead of guessing here. The agent now holds every tool,
        # including create_code_task, so it can still choose to write code when that is the
        # real answer — with the run in front of it, which this regex does not have.
        operational_subject = re.search(
            r"\b(deploy(ment|ed|s)?|redeploy|rollback|rolled back|preview|health check|"
            r"job|queue|worker|gate|decision|approval|stuck|orphan(ed)?|priority|"
            r"container|host|infra(structure)?)\b", lowered)
        if re.search(r"\b(fix|implement|build|change|refactor|add|remove|update)\b", lowered) and (
                not lead_question) and not operational_subject:
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
    def _looks_process_start(message: str) -> bool:
        return bool(
            re.search(
                r"\b(start|run|launch|kick\s*off)\b\s+"
                r"(this\s+|the\s+|an?\s+|our\s+)?"
                r"(agent\s+)?(process|workflow)\b",
                message,
            )
            or re.search(r"\bstart_agent_process\b", message)
        )

    @staticmethod
    def _looks_process_authoring(message: str) -> bool:
        if re.search(
            r"\b(create|author|define|design|write|make|draft)\b\s+"
            r"(me\s+)?((an?|the)\s+)?(agent\s+)?(process|workflow)\b",
            message,
        ):
            return True
        if re.search(r"\b(process|workflow)\s+where\b", message):
            return True
        if re.search(r"\bhandoffs?\b", message) and re.search(
            r"\b(process|workflow|review|role|architect|captain)\b", message
        ):
            return True
        return False

    @staticmethod
    def _extract_prd(message: str) -> str:
        match = re.search(r"\bprd\s*[:\-]\s*(.+)$", message, re.IGNORECASE | re.DOTALL)
        if match and match.group(1).strip():
            return match.group(1).strip()
        return message.strip()

    @staticmethod
    def _extract_process_key(message: str) -> Optional[str]:
        match = re.search(r"\bprocess_key\s*[=:]\s*([a-z0-9_]+)", message)
        if match:
            return match.group(1)
        match = re.search(r"\bkey\s+([a-z][a-z0-9_]{2,79})\b", message)
        return match.group(1) if match else None

    @staticmethod
    def _extract_process_name(message: str) -> Optional[str]:
        match = re.search(
            r"\b(?:named|called)\s+[\"']?([^\"'\n,]{2,80})",
            message,
            re.IGNORECASE,
        )
        return match.group(1).strip() if match else None

    @staticmethod
    def _tool_bool(value: Any, *, default: bool) -> bool:
        if value is None:
            return default
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            lowered = value.strip().lower()
            if lowered in {"true", "1", "yes"}:
                return True
            if lowered in {"false", "0", "no"}:
                return False
        return default

    @staticmethod
    def _explicit_true(value: Any) -> bool:
        if value is True:
            return True
        return isinstance(value, str) and value.strip().lower() == "true"

    @staticmethod
    def _compile_agent_process(text: str, name: Optional[str] = None) -> dict[str, Any]:
        try:
            from .process_compiler import compile_process
        except ImportError as error:
            raise ValueError("Process compiler is not available.") from error
        try:
            spec = compile_process(text, name=name)
        except ValueError:
            raise
        except TypeError:
            spec = compile_process(text)
        except Exception as error:  # noqa: BLE001 — compiler failures must be user-visible
            raise ValueError(str(error) or "Process compilation failed.") from error
        if not isinstance(spec, dict):
            raise ValueError("Process compiler returned an invalid spec.")
        return spec

    @staticmethod
    def _looks_multi_phase(message: str) -> bool:
        """Is this an order to do several things in sequence?

        SEQUENCING IS SIGNALLED BY SEQUENCING WORDS, NOT BY COUNTING CONJUNCTIONS.

        This used to return True on `len(re.findall(r"\band\b", message)) >= 2` — two
        occurrences of the commonest conjunction in English. So

            "the meeting sat queued for two hours AND the jobs table shows deadlettered rows
             AND I need to know why"

        was answered with "I drafted 3 sequenced phases", and so was any pasted SQL:

            WHERE kind='meeting.step' AND status='queued' AND project_id='boss-dogfood'

        An operator asking why their work was stuck got a project plan. Observed twice.

        Counting verbs has the same flaw more subtly — "the build failed and the deploy failed"
        is two distinct action verbs and a conjunction, and is a report, not an order. So the
        burden is now explicit: a multi-phase command must SAY that it has phases. Being wrong
        the other way costs one follow-up message; being wrong this way spends money and buries
        the answer the operator asked for.
        """
        return bool(re.search(
            r"\b(first|then|after that|followed by|afterwards|next,|"
            r"phase \d|step \d|multiple phases|end[- ]to[- ]end)\b", message))

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
                "Decide whether this ask should START WORK, and if so which initiator.\n\n"
                "SELECT NO TOOL when the user is asking ABOUT work that already exists — why "
                "something failed, what the status is, what an app does, explain/investigate/"
                "diagnose/check, or any question answerable by looking. Questions are answered, "
                "not built. Starting a panel on 'why did this fail' spends real money, publishes "
                "a document, and queues a build that nobody asked for.\n\n"
                "Only when the user genuinely wants NEW WORK begun, pick exactly one enabled "
                "initiator, and never a disabled one: code for a narrow change, meeting for an "
                "open decision, PRD for a fuzzy feature, discovery for research, "
                "create_agent_process when they are authoring a role-handoff process or "
                "workflow, start_agent_process when they want to run a process against a PRD "
                "(simulated unless they say otherwise; never set auto_build), propose_command "
                "for two or more dependent delivery phases that are not a process definition.\n\n"
                "When it is unclear whether they want work started, select nothing. Answering a "
                "request to build is a cheap mistake; building an answer to a question is not."
                "\n\nCAPABILITY MANIFEST\n" + manifest.render_table()
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
