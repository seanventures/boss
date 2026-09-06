from __future__ import annotations

import hashlib
import json
import logging
import re
import time
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
#: Tools whose use counts as BOSS having DONE something, rather than looked at something.
#:
#: This was a hand-written list and it drifted, as hand-written lists of a growing set do:
#: `deploy_app`, `merge_apps` and `retarget_app` were all missing, so a turn that really did
#: retarget an app recorded `actions: []` — the console showed nothing, and the guard that
#: checks "did it do what it said" would have called a genuine fix a fabrication.
#:
#: The registry already knows: a tool is declared `kind="write"` or it is not. Reading it there
#: means a new write is an action the day it is added, with nobody having to remember.
def _is_acting(tool_name: str) -> bool:
    return tool_name in _acting_tools()


def _acting_tools() -> frozenset[str]:
    global _ACTING_CACHE
    if _ACTING_CACHE is None:
        try:
            from ..tools import build_default_registry

            _ACTING_CACHE = frozenset(
                spec.name for spec in build_default_registry().specs() if spec.kind == "write")
        except Exception:  # noqa: BLE001 — never let this break a turn
            _ACTING_CACHE = _ACTING_FALLBACK
    return _ACTING_CACHE


_ACTING_CACHE: "frozenset[str] | None" = None

#: Only if the registry cannot be read at all.
_ACTING_FALLBACK = frozenset({
    "redeploy", "retry_code_task", "extend_code_task", "steer_code_task", "cancel_code_task",
    "create_code_task", "create_discovery", "request_meeting", "register_repository",
    "start_preview", "stop_preview", "cancel_job", "set_job_priority", "close_orphaned_runs",
    "link_run_origin", "decide", "waive_gates", "send_feedback", "write_db",
    "deploy_app", "merge_apps", "retarget_app", "generate_image", "run_on_host",
})

_ACTING_TOOLS = _ACTING_FALLBACK   # kept for any caller that imports the name directly


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


def _json_objects_in(text: str):
    r"""Every balanced {...} in the text, with its span. Nested braces and all.

    A non-greedy `\{.*?\}` stops at the FIRST closing brace, which truncates every proposal
    that has an args object inside it — i.e. all of them. Counting braces is the only way to
    take the whole object, and doing it here means the four places a model might write one can
    share a single scanner.
    """
    depth = start = 0
    in_string = escape = False
    for i, ch in enumerate(text):
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth:
                depth -= 1
                if depth == 0:
                    yield start, i + 1, text[start:i + 1]


def _unwrap_proposal(tool: str, args: dict) -> tuple[str, dict]:
    """A proposal OF a proposal is the inner one.

    `propose_action` is itself a tool, so a model writing out its own call — as a JSON block, a
    tag or a table row — describes `{"tool": "propose_action", "args": {"tool": "retry_code_task",
    …}}`. Recovered literally that produces a button whose tool is `propose_action`, which on
    click proposes something instead of doing it. Observed live: the offer read "Retry Interview
    Scorecard App" and would have run nothing.
    """
    seen = 0
    while tool == "propose_action" and isinstance(args, dict) and seen < 3:
        inner = args.get("args") if isinstance(args.get("args"), dict) else {}
        tool = str(args.get("tool") or "").strip()
        args = inner
        seen += 1
    return tool, (args if isinstance(args, dict) else {})


#: `propose_action({...})` or `retry_code_task({...})` written out in prose. The fourth shape a
#: model reached for after the tag, the table and the fenced block were each recovered — which
#: is why this is a scanner over shapes rather than another special case.
_CALL_LITERAL = re.compile(r"\b([a-z][a-z0-9_]{2,40})\s*\(\s*(?=\{)")


def _recover_printed_actions(reply: str, known_tools, existing) -> tuple[list, str]:
    """Turn a proposal the model WROTE OUT into a real button, and take the text out.

    Observed live, four different ways, on the same question:

        ```json {"tool": "retry_code_task", …}```          — a fenced block
        propose_action({"tool": "retry_code_task", …})     — a call literal in prose
        <propose_action tool="…" />                        — a tag  (sibling recovery)
        | Tool | Arguments |                               — a table (sibling recovery)

    Every one is the shape of a perfect offer and none of them is one: the operator gets markup
    where a button should be, and the fix they were just promised is something they have to ask
    for again. Chasing the shapes one at a time is how this recurs, so this scans for a JSON
    object that NAMES A REAL TOOL wherever it appears, in a fence or bare.

    Validation is the same as its siblings — the tool must exist, args must be an object, no
    placeholder values — and a matched proposal is stripped either way, because unparseable
    markup is never something the operator should be reading. An object that does not name a
    real tool is left exactly as written, so a JSON sample the operator asked to see survives.
    """
    if not reply or "{" not in reply:
        return [], reply
    from ..tools.builtin import _placeholder_argument

    found: list = []
    have = {(a.get("tool"), json.dumps(a.get("args") or {}, sort_keys=True)) for a in existing}
    cuts: list[tuple[int, int]] = []

    for start, end, blob in _json_objects_in(reply):
        try:
            payload = json.loads(blob)
        except Exception:  # noqa: BLE001 — not JSON; leave it alone
            continue
        if not isinstance(payload, dict):
            continue
        tool = str(payload.get("tool") or "").strip()
        args = payload.get("args")
        if tool not in known_tools or not isinstance(args, dict):
            continue
        tool, args = _unwrap_proposal(tool, args)
        if tool not in known_tools or tool == "propose_action":
            cuts.append((start, end))
            continue
        if not _placeholder_argument(args):
            key = (tool, json.dumps(args, sort_keys=True))
            if key not in have:
                have.add(key)
                found.append({
                    "id": make_id("act"), "tool": tool, "args": args,
                    "label": str(payload.get("label") or tool.replace("_", " ")),
                    "why": str(payload.get("why") or ""),
                })
        cuts.append((start, end))

    if not cuts:
        return [], reply

    # Cut the objects, then tidy what they were written inside. Doing the tidy with a couple of
    # passes over the finished string beats index arithmetic against the original: the wrapper
    # can be a fence, a call literal or nothing at all, and an off-by-one leaves the operator
    # looking at `propose_action(` with the useful part removed.
    kept, last = [], 0
    for cut_start, cut_end in cuts:
        kept.append(reply[last:cut_start])
        last = cut_end
    kept.append(reply[last:])
    out = "".join(kept)
    out = re.sub(r"\b[a-z][a-z0-9_]{2,40}\s*\(\s*\)", "", out)   # `propose_action()`
    # And the bare name when its arguments were the thing cut out — `propose_action` alone on
    # a line is meaningless to the operator and looks like a leaked internal.
    out = re.sub(r"(?m)^\s*propose_action\s*\(?\s*$", "", out)
    out = re.sub(r"\bpropose_action\s*\(?(?=\s|$)", "", out)
    out = re.sub(r"```(?:json)?\s*```", "", out)                    # an emptied fence
    out = re.sub(r"[ \t]*\)\s*$", "", out, flags=re.MULTILINE)      # a dangling paren
    out = re.sub(r"\n{3,}", "\n\n", out).strip()

    if not out:
        # The whole reply WAS the proposal. Returning the raw markup instead would undo the
        # point of recovering it, so say the one sentence the button cannot say for itself.
        out = ("Here is what I can do about it — nothing runs until you choose:"
               if found else reply)
    return found, out


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


#: Claims to have CHANGED something. Present tense of completion, not of intent — "I will
#: update the target" is a plan and fine; "I've corrected the target" is a statement of fact
#: that has to be true.
_ACTION_CLAIM = re.compile(
    r"(?:^|\b)(?:i(?:'ve| have)\s+(?:now\s+)?(?:corrected|updated|changed|fixed|set|"
    r"retargeted|rebound|deployed|redeployed|opened|created|cancelled|restarted)"
    r"|action\s+taken"
    r"|(?:has|have)\s+been\s+(?:corrected|updated|changed|fixed|set|retargeted|deployed))",
    re.I,
)


def _no_action_was_claimed(reply: str, actions: list) -> str:
    """Refuse to let BOSS say it did something when the trace shows it did not.

    Asked to diagnose and fix a deployment, BOSS read the target, got the diagnosis exactly
    right — "the health check was probing port 8000 while the container publishes on 3020" —
    and then wrote:

        Action taken: retargeted HookWatch to publish on port 3020.

    It had called one tool, a READ. The target was unchanged. An operator reading that has been
    told their problem is solved, will go and look at a still-broken app, and has no reason to
    trust the next answer either. It is worse than refusing, because refusing is honest.

    Narrow on purpose, and the same shape as `_no_button_was_promised`: it does not rewrite the
    diagnosis — which was correct and valuable — it appends the correction, because the model's
    own words are evidence and deleting them hides that this happened.
    """
    if actions or not reply or not _ACTION_CLAIM.search(reply):
        return reply
    return reply.rstrip() + (
        "\n\n(Correction, added by BOSS: nothing above was actually changed — this turn only "
        "READ. The diagnosis stands, but the fix has not been applied. Tell me to go ahead and "
        "I will run it, or ask why it did not run.)"
    )


#: An explicit instruction to DO something. Deliberately imperative forms only — the verb at
#: the start of a clause, or asked of BOSS directly — because the words themselves are all over
#: ordinary questions. "Why did the deploy fail?" contains "deploy" and is not a request to
#: deploy anything.
_ASKED_TO_ACT = re.compile(
    # `?` and `!` end a clause too: "why is it stuck? fix it" is a question followed by an
    # order, and the order is the part that must be honoured.
    r"(?:^|[.;?!\n]\s*|\b(?:and|then|also|now)\s+)(?:please\s+)?"
    r"(?:fix|cancel|retry|restart|re-?deploy|deploy|kill|stop|delete|remove|revert|roll\s?back|"
    r"merge|apply|unstick|resume|clear|rerun|re-?run)\b"
    r"|\b(?:can|could|would|will)\s+you\s+(?:please\s+)?"
    r"(?:fix|cancel|retry|restart|re-?deploy|deploy|kill|stop|delete|remove|revert|roll\s?back|"
    r"merge|apply|unstick|resume|clear|rerun|re-?run)\b"
    r"|\b(?:go ahead|do it|just do|make it so|proceed|sort it out)\b",
    re.IGNORECASE,
)

#: A request to LOOK. Question marks, question words, and the verbs of investigation.
_ASKED_TO_LOOK = re.compile(
    r"\?|\b(?:why|what|which|when|where|who|how come|how long|whats|what's|"
    r"check|checking|investigate|diagnose|explain|look into|looking into|find out|"
    r"any idea|status of|tell me)\b",
    re.IGNORECASE,
)


#: A question about a specific piece of running or failed work. These are the ones the operator
#: needs RESOLVED, and the ones where answering from two lookups is how a normal build gets
#: called a stall.
#: An id names one piece of work outright.
_WORK_ID = re.compile(r"\b(crun_[a-z0-9]+|wfr_[a-z0-9]+|job_[a-z0-9]+)\b", re.IGNORECASE)

#: The thing being asked about.
_WORK_NOUN = re.compile(
    r"\b(build|builds|run|runs|task|tasks|job|jobs|deploy(?:ment)?|preview|workflow|project|"
    r"app|pipeline|step|phase|engineer|implement(?:ing|ation)?)\b", re.IGNORECASE)

#: What is wrong with it.
_SYMPTOM = re.compile(
    r"\b(stuck|stall(?:ed|ing)?|hang(?:ing|s)?|slow|latency|fail(?:s|ed|ing|ure)?|"
    r"broken|error|crash(?:ed|ing)?|"
    r"not working|no preview|timed? ?out|why is|what happened|progress|taking (?:so )?long)\b",
    re.IGNORECASE)

#: Counting and listing. "How many runs failed last week" is a reporting question about a SET,
#: not a complaint about one piece of work — and pushing it through an investigation aimed at a
#: single run produces "please give me a run id", which is the hijack again in another costume.
_AGGREGATE = re.compile(
    r"\b(how many|how much|count|list|show me all|which ones|summar(?:y|ise|ize)|"
    r"across (?:all|the) (?:projects|runs|apps)|last (?:week|month|day))\b", re.IGNORECASE)

#: The operator driving a tool directly. "Use query_db and run this SELECT" is an instruction
#: about HOW to answer, and forcing a six-lookup investigation onto it produces the opposite of
#: what was asked — observed live, where a plain SQL request came back as "please provide the
#: run ID and I will run the full diagnosis".
_DRIVING_A_TOOL = re.compile(
    r"\b(use|call|run)\s+(query_db|read_logs|diagnose_run|read_run_turns|search_code|"
    r"explain_run|describe_schema)\b|\bselect\b.+\bfrom\b", re.IGNORECASE)

#: How many lookups an operational answer must be built on before it is allowed to be an answer.
#:
#: Measured over 79 assistant messages: the average was 2.86 tool calls against a budget of 12,
#: and 5% called none at all. The budget was raised to twelve precisely so BOSS could chain a
#: real investigation — find the run, read its timings, read the code that explains them — and
#: it was using under a quarter of it. Six is the floor, not the target.
_MIN_LOOKUPS_FOR_OPERATIONAL = 6

#: Endings that hand the operator their own problem back.
_GAVE_UP = re.compile(
    r"(could not determine|couldn't determine|unable to determine|not enough information"
    r"|no logs (?:were )?available|cannot tell|can't tell|unclear why|not clear why)",
    re.IGNORECASE,
)

#: A question back to the operator that does their thinking for them in the wrong direction.
_YES_NO_BACK = re.compile(
    r"(would you like me to|do you want me to|shall i|should i (?:go ahead|proceed|investigate)"
    r"|let me know if you(?:'d| would) like|if you want me to"
    # ASKING FOR SOMETHING BOSS CAN LOOK UP. Observed live: "we need the specific run
    # identifier so we can run diagnose_run" — about a run it had listed one tool call earlier.
    # Every id in this system is discoverable, so asking for one is a refusal wearing a
    # question mark.
    r"|we need the (?:specific|exact)|please provide|could you (?:provide|share|tell me)"
    r"|provide the (?:run|job|task) (?:id|identifier)|what is the (?:run|job) id)",
    re.IGNORECASE,
)


def _progress_reporter(project_id: str, thread_id: Optional[str]):
    """A callback that records what BOSS is doing, visible WHILE it is doing it.

    On its own connection and committed per row. The turn's transaction has not committed yet,
    so a row written on it is invisible to the poller until the answer has already arrived —
    which is exactly too late to be progress.

    Returns None when there is nothing to report to, so callers need no branch.
    """
    if not thread_id:
        return None

    def report(label: str) -> None:
        try:
            from ..db import get_engine

            with get_engine().begin() as c:
                c.execute(
                    text("INSERT INTO agent_progress (thread_id, project_id, label) "
                         "VALUES (:t, :p, :l)"),
                    {"t": thread_id, "p": project_id, "l": str(label)[:200]},
                )
        except Exception:  # noqa: BLE001 — an indicator must never take down the answer
            pass

    return report


def _sentences(text: str) -> list[str]:
    return [x.strip() for x in re.split(r"(?<=[.!?])\s+|\n+", text or "") if x.strip()]


def without_what_was_already_said(reply: str, history: list) -> str:
    """Strip sentences this thread has already been told, and say so once.

    "No duplicate answers, no answering the same thing again and again." A prompt instruction
    does not hold this — the model cannot see its own repetition across turns as clearly as a
    diff can. Sentences are the unit because a repeated PARAGRAPH is usually a repeated
    argument, while a repeated clause is often just English.

    Deliberately conservative: only long sentences (short ones repeat innocently — "Here is what
    I found."), only exact matches after normalisation, and never the whole reply. A
    de-duplicator that eats an answer is worse than one that misses a repetition.
    """
    if not reply or not history:
        return reply
    seen = set()
    for message in history:
        if str(message.get("role")) != "assistant":
            continue
        for sentence in _sentences(str(message.get("content") or "")):
            seen.add(" ".join(sentence.lower().split()))
    if not seen:
        return reply

    kept, dropped = [], 0
    for sentence in _sentences(reply):
        norm = " ".join(sentence.lower().split())
        if len(norm) > 80 and norm in seen:
            dropped += 1
            continue
        kept.append(sentence)
    if not dropped or not kept:
        return reply
    out = " ".join(kept)
    # Say that something was removed rather than leaving a reply that reads as oddly terse.
    return out + (f"\n\n(BOSS left out {dropped} point{'s' if dropped > 1 else ''} already "
                  "covered above rather than repeating them.)")


def is_operational_question(message: str) -> bool:
    """Is this about a specific piece of work that is running, slow or broken?

    BOTH a subject and a symptom, not either. A single keyword is too loose: asked to run
    `SELECT statements, failed FROM schema_apply_runs`, the word "failed" alone made this an
    operational question, the investigation floor fired, and BOSS answered a direct SQL request
    with "please provide the run ID and I'll run the full diagnosis". A guard that hijacks a
    plain instruction is worse than no guard.
    """
    text = message or ""
    if _DRIVING_A_TOOL.search(text) or _AGGREGATE.search(text):
        return False
    if _WORK_ID.search(text):
        return True
    return bool(_WORK_NOUN.search(text) and _SYMPTOM.search(text))


def under_investigated(tool_calls: list, message: str) -> bool:
    """Did an operational answer get built on too little looking?

    A floor as well as a ceiling. The incident answer used three tools and the fourth was the
    one that mattered; nothing in the loop asked whether that was enough.
    """
    if not is_operational_question(message):
        return False
    return len([c for c in (tool_calls or []) if c.get("tool")]) < _MIN_LOOKUPS_FOR_OPERATIONAL


#: A reply that PRESCRIBES rather than performs. "The fix is to retarget the app to an unused
#: port and redeploy" is a correct diagnosis and a completed sentence, and it is not a fix.
_PRESCRIBES_A_FIX = re.compile(
    r"\b(the fix is|the solution is|you (?:should|need to|can|could)|"
    r"it (?:should|needs to) be|we (?:should|need to)|"
    r"(?:can|could) be (?:fixed|resolved|solved) by|"
    r"recommend(?:ed|ation)?\s+(?:is\s+)?to|next step is to)\b",
    re.IGNORECASE,
)


def described_a_fix_without_doing_it(reply: str, actions: list, proposed: list) -> bool:
    """Did it write the prescription and not fill it?

    The transcript this exists for, three turns running, each after the operator asked for the
    deployment to be FIXED:

        "The fix is to retarget the app to an unused port (e.g., 8001) and redeploy."
        "The deployment failed because the host already has port 8000 in use... port 8001 is
         free."
        "The deployment was stuck because the host already has port 8000 bound. I verified the
         host's sockets..."

    The diagnosis was right every time. `retarget_app` and `redeploy` were both in its hands.
    Nothing ran, nothing was offered, and the operator said "I told you already" — which is what
    a person says to a system that answers instead of working.

    An offer counts as doing something: a button carries the arguments and one click runs it.
    Prose does not.
    """
    if not reply or actions or proposed:
        return False
    return bool(_PRESCRIBES_A_FIX.search(reply))


#: The reply owning ITS OWN failed attempt — first person, or the attempt as the subject.
#:
#: A bare "failed" is not enough, and testing for one is worse than testing for nothing: the
#: reply that hid the failed INSERT opened "The Deploy step failed because BOSS could not pick a
#: unique staging target". Every failure word in that sentence is about the thing being
#: diagnosed. What is missing is any mention of what BOSS had just tried and what happened.
_OWNS_A_FAILURE = re.compile(
    r"\b(i (?:tried|attempted|ran|called|issued)|my (?:\w+ )?(?:attempt|insert|write|call|query)|"
    r"(?:was|were) (?:rejected|refused)|i could not|i couldn'?t|i was unable|"
    r"that (?:attempt|call|insert) failed)\b", re.I)


def hid_a_failed_action(reply: str, tool_calls: list) -> bool:
    """Did this turn try something, have it fail, and then not mention it?

    From the transcript: BOSS called `propose_action` twice and both were refused, so the
    operator got no button and no idea one had been attempted. Next turn it ran `write_db`,
    the INSERT failed on a table name it had guessed wrong, and the reply again described the
    situation as though nothing had been tried — the operator only learned about it because the
    console renders failed tool calls beside the message.

    An agent that acts must report what its actions did. Silence about a failed attempt is
    worse than the failure: it makes the next answer look like a fresh analysis when it is the
    same one with a hidden dead end behind it, and it is how a loop stays invisible from inside.
    """
    if not reply:
        return False
    failed = [c for c in (tool_calls or [])
              if isinstance(c, dict) and c.get("ok") is False and c.get("tool")]
    if not failed:
        return False
    low = reply.lower()
    for call in failed:
        name = str(call.get("tool") or "").lower()
        # Named outright, or named the way a person writes it ("write_db" / "write db").
        if name in low or name.replace("_", " ") in low:
            return False
    return not _OWNS_A_FAILURE.search(reply)


def going_in_circles(reply: str, history: list) -> bool:
    """Is this the same answer again?

    Repetition after an instruction is the signature of an agent that has stopped making
    progress and has not noticed. The words differ each time — "the fix is to retarget" becomes
    "the deployment failed because port 8000 is in use" — so exact matching sees three distinct
    replies; what repeats is the CONTENT.

    Compared on significant words rather than sentences, and only against the immediately
    previous answer, because a conversation that returns to a subject is normal and only
    consecutive restatement is a loop.
    """
    if not reply or not history:
        return False
    previous = ""
    for message in reversed(history):
        if str(message.get("role")) == "assistant":
            previous = str(message.get("content") or "")
            break
    if not previous:
        return False

    def bag(text: str) -> set:
        return {w for w in re.findall(r"[a-z0-9]{5,}", text.lower())}

    now, before = bag(reply), bag(previous)
    if len(now) < 8 or len(before) < 8:
        return False
    # CONTAINMENT, NOT JACCARD, and the threshold comes from the real pair rather than a guess.
    # The two consecutive port-8000 answers share half their significant words but score only
    # 0.32 on Jaccard, because each adds detail the other lacks — which is exactly what a
    # rephrased repeat looks like. Two answers about genuinely different things scored 0.00, so
    # there is a wide gap to sit in.
    overlap = len(now & before) / min(len(now), len(before))
    return overlap >= 0.45


def hands_the_problem_back(reply: str, proposed: list) -> bool:
    """Does this reply end without a direction the operator can act on?

    "I could not determine it" is an acceptable FINDING when it names the next specific step.
    It is never an acceptable ending, and neither is "would you like me to investigate
    further?" — which asks the operator to do the thinking they came here to avoid.
    """
    if not reply:
        return False
    if proposed:
        return False           # there is a button; that is a direction
    return bool(_GAVE_UP.search(reply) or _YES_NO_BACK.search(reply))


#: The operator handing over authority in as many words. When any of these appears, the message
#: is an INSTRUCTION however it is phrased around it.
#:
#: From the transcript this exists for:
#:
#:     "I told you already to redeploy to different port, check yourself before applying to the
#:      new port and ensure that it's good.. dont keep asking me, you have full freedom on the
#:      host"
#:
#: — classified as look-only, because the imperative sat after "to" rather than at the start of
#: a clause and the word "check" appeared. Writes were withheld, BOSS re-described the problem
#: it had already diagnosed, and the operator said it again. A safety gate that turns "you have
#: full freedom" into a refusal is not being careful; it is being deaf.
_AUTHORISED = re.compile(
    r"\b(you have full freedom|whatever it takes|do what(?:ever)?(?:\s+\w+){0,2}\s+need(?:ed|s)?"
    r"|don'?t (?:keep )?ask(?:ing)? me|do not (?:keep )?ask|stop asking"
    r"|i told you|i already told you|as i said|like i said"
    r"|without asking|no need to ask|just get it done|make it work)\b",
    re.IGNORECASE,
)

#: An instruction phrased as a report of one — "I told you to redeploy", "I want you to fix it".
#: The imperative is there; it is simply not at the start of a clause.
_INSTRUCTED_TO = re.compile(
    r"\b(?:told|asked|want|need|expect)(?:ed)?\s+(?:you\s+)?to\s+"
    r"(?:fix|cancel|retry|restart|re-?deploy|deploy|kill|stop|delete|remove|revert|roll\s?back|"
    r"merge|apply|unstick|resume|clear|rerun|re-?run|change|move|switch|update|set)\b",
    re.IGNORECASE,
)


def asked_only_to_look(message: str) -> bool:
    """Is this a question rather than an instruction?

    THE 27 MINUTES THIS EXISTS FOR.

    Operator: "This project Interview Scorecard App stuck for 27m for Engineer implementing
    can you check why ?"

    BOSS: get_code_task, find_starved_work, inspect_self — then cancel_job(force=True), and
    "I cancelled the holding job to free the worker slot."

    Nobody asked it to cancel anything. Twenty-seven minutes of a build were destroyed to
    answer a question about why it was slow, and the operator found out by reading the answer.

    The turn ran with `allow_writes=True` unconditionally, and that flag was set True for a
    good reason — an operator who says "fix and retry the deployment" and receives an essay
    ending in "let me know if you would like me to" has been ignored by a system whose whole
    premise is that it acts. That reasoning is correct for an instruction and has nothing to
    say about a question.

    So the distinction is drawn here, and only here: a QUESTION never authorises a write. BOSS
    still investigates with every read tool it has, and a write it wants to make comes back as
    `confirmation_required` — which the console already renders as a button. The operator gets
    the diagnosis AND the fix, one click away, instead of a fait accompli.

    Ambiguity resolves toward looking. An action not taken costs one more sentence; an action
    taken wrongly cost 27 minutes of work that nobody can get back.
    """
    text = (message or "").strip()
    if not text:
        return False
    # An explicit instruction wins even when it is phrased as a question: "can you fix it?"
    # is a request, and "check the logs and retry it" is both — the instruction is the point.
    if _ASKED_TO_ACT.search(text) or _AUTHORISED.search(text) or _INSTRUCTED_TO.search(text):
        return False
    return bool(_ASKED_TO_LOOK.search(text))


def _role_identity(role: Optional[str]) -> str:
    """Answer as a named member of the workforce, from what that member has actually done.

    The Agents page's "Ask" put "@QA Lead: " into the general agent's box. The name was prose
    in a prompt, so the reply came from BOSS-in-general: no lens, no remit, and no memory of
    anything that role had argued. An operator asking their QA lead a question got a generic
    answer wearing a name badge, which is worse than no expert at all — it looks like expertise.

    The material for a real answer is already recorded. `meeting_roles` holds the lens each
    role argues from, `agent_versions` holds the rules and skills its definition carries, and
    `meeting_runs.state` holds every position every role has taken on this project's panels,
    keyed by role title, in full. So this does not ask the model to invent a persona: it tells
    it to go and read who it is and what it has said, with the tools to do both.
    """
    if not role:
        return ""
    return (
        "\n\n=== YOU ARE ANSWERING AS A NAMED MEMBER OF THIS WORKFORCE ===\n"
        f"The operator is asking {role}, not BOSS in general.\n"
        f"FIRST call describe_role(role=\"{role}\") to read the lens that role argues from and "
        "the rules and skills its agent definition carries. Then call "
        f"recall_role_experience(role=\"{role}\") to read what it has actually argued on this "
        "project's panels, in its own words.\n"
        "Answer from THAT: the specifics this role has seen here, referring to the briefs and "
        "positions by name. An SME's whole value is what it has lived through — a generic "
        "answer from a named expert is worse than no expert, because it reads as authority.\n"
        "If nothing is recorded for this role yet, say so plainly and answer from the lens and "
        "rules instead. Do not imply experience you cannot point at.\n"
        "Stay in role: give the view that role is in the room to give, and name where you "
        "disagree with what the rest of the panel decided."
    )


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


_log = logging.getLogger(__name__)

#: Provider hiccups worth trying again, rather than reporting to a customer as an answer.
#: Anthropic's 529 is `overloaded_error` — capacity, not a bad request — and the coding
#: pipeline has always retried it. Kept in the same words as code_agent's list so the two
#: cannot drift into disagreeing about what "transient" means.
_TRANSIENT_MARKERS = (
    "529", "503", "overloaded", "capacity", "bad gateway", "gateway timeout",
    "connection reset", "connection closed", "connection error", "timed out",
)


def _looks_transient(error: BaseException) -> bool:
    text = f"{type(error).__name__}: {error}".lower()
    return any(marker in text for marker in _TRANSIENT_MARKERS)


def _with_transient_retry(call, attempts: int = 3, backoff: float = 2.0):
    """Run `call`, retrying a provider hiccup a couple of times before giving up.

    Short and bounded on purpose: a person is waiting on this turn, so two extra attempts a few
    seconds apart is the difference between "the provider was busy for a moment" and a dead end
    the operator has to notice and work around. Anything that does not look transient is raised
    immediately — retrying a bad request just spends the wait twice.
    """
    last: BaseException
    for attempt in range(attempts):
        try:
            return call()
        except BaseException as exc:  # noqa: BLE001 — re-raised below unless transient
            last = exc
            if attempt == attempts - 1 or not _looks_transient(exc):
                raise
            _log.warning(
                "answer attempt %d/%d hit a transient provider error (%s) — retrying in %.0fs",
                attempt + 1, attempts, type(exc).__name__, backoff * (attempt + 1),
            )
            time.sleep(backoff * (attempt + 1))
    raise last


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
        #: Answer AS this member of the workforce, rather than as BOSS-in-general.
        role: Optional[str] = None,
        #: The conversation this turn belongs to, so progress can be reported while it runs.
        thread_id: Optional[str] = None,
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
                return self._answer(connection, project_id, messages, external_ref=external_ref,
                                thread_id=thread_id,
                                role=role)
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
            return self._answer(connection, project_id, messages, external_ref=external_ref,
                                thread_id=thread_id,
                                role=role)

        # No model available — fall back to keywords rather than doing nothing.
        name, arguments = self._deterministic_intent(latest)
        if name is None:
            # DISCUSS BY DEFAULT: an unclassified message is a question / discussion, NOT a build
            # order. Answer it conversationally and DON'T spawn a task (that spawned junk tasks for
            # every greeting, question, or test string). Work starts only on an explicit intent.
            return self._answer(connection, project_id, messages, external_ref=external_ref,
                                thread_id=thread_id,
                                role=role)

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

    def _scope_note(self, connection, project_id: str, external_ref: Optional[str]) -> str:
        """One paragraph naming the app under discussion, or nothing.

        Nothing is the right answer for a general thread: inventing a subject makes every
        unrelated question about the last app that happened to be open.
        """
        if not external_ref:
            return ""
        try:
            row = connection.execute(
                text("SELECT id, title, status, pull_request_url, task_id "
                     "FROM code_task_runs WHERE (id = :r OR task_id = :r) AND project_id = :p "
                     "ORDER BY created_at DESC LIMIT 1"),
                {"r": external_ref, "p": project_id},
            ).mappings().first()
        except Exception:
            return ""
        if row is None:
            return ""
        pr = row["pull_request_url"] or "none opened"
        return (
            "THIS CONVERSATION IS ABOUT ONE APP.\n"
            f"App: {row['title']}\nRun id: {row['id']}\nStatus: {row['status']}\nPR: {pr}\n"
            "Questions like \"what should we build next\", \"what did we miss\", \"which "
            "criteria are untested\" or \"read the PRD\" refer to THIS app unless they name "
            "another one. Call read_spec with this run id FIRST — it returns the PRD, the "
            "acceptance criteria, what was implemented and what the reviewer said, in one "
            "call. Do not answer scope questions about this app without reading it, and do "
            "not report a PRD or diff as unavailable until read_spec has said so.\n"
            "This run is a STARTING POINT, not a restriction. The app has other runs, and if "
            "the operator points you at a different one (\"use the latest working one\"), do "
            "that — their correction outranks this block."
        )

    def _answer(self, connection, project_id: str, messages: list,
                *, external_ref: Optional[str] = None,
                role: Optional[str] = None,
                thread_id: Optional[str] = None) -> RouterTurn:
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
            "YOUR JOB IS TO END THE PROBLEM, NOT TO DESCRIBE IT.\n"
            "An answer that explains a failure perfectly and leaves the operator to fix it has "
            "done a fraction of the job. Every diagnosis that has a known fix ENDS with that "
            "fix offered — the exact tool with real arguments, via propose_action — so the "
            "operator is one click from done. A build that hung: offer the retry. A target "
            "pointing at the wrong port: offer the correction.\n"
            "AN OPTION YOU NAME IS AN OPTION YOU OFFER. If you list what the operator could do, "
            "every row of that list is a propose_action call with real arguments. Never write an "
            "options table with an empty button column, and never end with \"tell me to go ahead\" "
            "— that is asking them to type the thing you could have handed them. propose_action "
            "changes nothing by itself, so it is available even when you have been asked only to "
            "look.\n\n"
            "ANSWER THE QUESTION THAT WAS ASKED.\n"
            "The first sentence is the answer, not the preamble. Asked why something is stuck, "
            "the first sentence names the cause. Evidence goes underneath. Do not deliver a "
            "standard tour of the subject when a specific thing was asked — and do not repeat "
            "anything you have already said in this conversation; build on it.\n\n"
            "NEVER HAND THE PROBLEM BACK.\n"
            "NEVER ASK THE OPERATOR FOR AN IDENTIFIER. Every run, job, task and target id in "
            "this system is discoverable — list_code_tasks, list_running_work, query_db. If "
            "they named an app, find its run yourself. \"We need the specific run identifier\" "
            "is a refusal wearing a question mark.\n"
            "\"I could not determine it\" is a finding only when it names the next specific "
            "step; it is never an ending. And never ask the operator a yes/no question — no "
            "\"would you like me to investigate further?\". If you need a decision, present "
            "the options you have already worked out with a recommendation. If you are stuck, "
            "give the single most likely cause, the ONE check that would confirm it, and a "
            "button that runs it.\n\n"
            "LOOK ACROSS EVERYTHING BEFORE YOU ANSWER.\n"
            "You have the run's own turns, the service logs, the customer's app code on its PR "
            "branch, BOSS's own source, every table in the database, the hosts, and this "
            "project's history. An operational question is worth SIX or more lookups, not two; "
            "you have twelve steps and stopping early is how a normal build gets called a "
            "stall. Name in your answer what you actually consulted, and say plainly what you "
            "could not check and why.\n\n"
            "PROGRESS, LATENCY AND \"IS IT STUCK\" QUESTIONS — READ THE WORK FIRST.\n"
            "  diagnose_run(run_id) does the whole sequence in one call — turns, silence, the "
            "last command that never returned, the phase breakdown, AND how this run compares "
            "to other builds on this project. Prefer it over assembling the same picture by "
            "hand.\n"
            "When an operator asks how a build is going, why it is slow, why it is taking so "
            "long, or whether it is stuck, your FIRST call is read_run_turns(run_id) — the "
            "turn-by-turn record of what the coding agent actually did, and how long it has "
            "been silent. Then read_logs for the service if the turns do not explain it, then "
            "explain_run for the phase breakdown. ONLY THEN anything about queues, hosts or "
            "capacity.\n"
            "  That order is not a preference. Asked why a build had been running 27 minutes, "
            "BOSS read the job queue and the container, found nothing starved, concluded the "
            "phase was stalled and cancelled it — while 101 recorded turns said the engineer "
            "had worked normally for nine minutes and then issued one command that never "
            "returned. Queue statistics describe the MACHINE. The turns describe the WORK, and "
            "the question was about the work.\n"
            "  A run whose last turn is minutes old while its status is still running has gone "
            "QUIET, which is a different fault from being slow — read_run_turns reports that "
            "silence as a finding. Never call a build stalled without having read its turns, "
            "and never call a duration abnormal without comparing it to what this project's "
            "other builds took (query_db over code_task_runs); a first-time reader has no idea "
            "whether 27 minutes is long here, and neither do you until you look.\n\n"
            "WHAT YOU CAN REACH:\n"
            "• query_db — arbitrary SELECT over EVERY table. describe_schema first if you do "
            "not know the table or columns; there are ~244 tables, including events (the full "
            "audit stream), receipts, llm_calls, and deployment_health_samples (uptime, "
            "latency, availability).\n"
            "• search_code / read_code — BOSS's own source AND the customer's application. "
            "Read the implementation rather than describing what it probably does.\n"
            "  WHEN AN OPERATOR QUOTES AN ERROR MESSAGE, SEARCHING FOR IT IS YOUR FIRST MOVE, "
            "before logs and before the host. Search the literal text in BOTH the app "
            "(search_code with run_id=<the crun_ they mentioned>) and the platform "
            "(repo='boss'). Whichever source contains that string is where it is raised, and "
            "it is REGULARLY the platform, not the app — a header the proxy strips, a gateway "
            "rule, a preview shim. Do not tell an operator you cannot determine the cause "
            "until you have searched for the words they pasted.\n"
            "  App code lives on the run's PR BRANCH, not on the default branch, so pass "
            "run_id — without it you are searching a repository that does not contain the "
            "app, and an empty result means nothing.\n"
            "  A PREVIEW IS SERVED THROUGH BOSS, so the request the app receives is NOT the "
            "request the browser sent. BOSS proxies it, rewrites HTML paths, and filters "
            "headers. When a client demonstrably sends something and the server says it is "
            "missing, SOMETHING IN BETWEEN REMOVED IT — and in a preview, BOSS is what is in "
            "between. Read the proxy (search repo='boss' for the header or behaviour) BEFORE "
            "concluding the app is at fault. Blaming the customer's app for a platform bug is "
            "the worst answer you can give: it is wrong, it sends someone to change working "
            "code, and it hides a defect that affects every app on the platform. When the "
            "evidence does not distinguish the two, say so and name the check that would.\n"
            "• read_logs — what a service actually logged.\n"
            "  NEVER REPORT THE OUTPUT OF A COMMAND YOU DID NOT RUN. Asked to curl a URL on a "
            "host, inspect_host refused it — the command set is read-only — and the answer "
            "reported 'connection refused' anyway, as though it had run. The app was serving "
            "200 the whole time. An invented result is worse than no result: it is acted on. "
            "If a tool refuses, say which tool refused and what it allows; if you want an HTTP "
            "check, http_probe is the tool for it.\n"
            "• inspect_host — read-only commands on a deployment host (docker ps, container "
            "logs, disk). The database says what BOSS INTENDED; this says what the machine is "
            "doing.\n"
            "• http_probe — is a URL up right now.\n"
            "  A RECORDED DIAGNOSIS IS A CLAIM, NOT A FACT — TEST IT BEFORE REPEATING IT. A "
            "deployment's failure_reason contains whatever the health checker CONCLUDED, and "
            "it can be wrong: one said 'port 3020 is blocked by a firewall' when the check had "
            "actually probed port 8000, and an operator was sent four times to open a rule "
            "that would have changed nothing. Before passing a diagnosis on, look for the "
            "cheapest thing that would DISPROVE it — here, another app on the same host "
            "answering on its own port from outside, which http_probe shows in one call. If "
            "you cannot test it, say it is the checker's conclusion rather than stating it as "
            "the cause.\n"
            "  AND WHEN AN OPERATOR SAYS 'GO AHEAD', DO NOT RESTATE THE PROBLEM. Either act, "
            "or say plainly which tool you lack and what you need from them. Repeating the "
            "same paragraph after approval is the most annoying thing you can do.\n"
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
            + _role_identity(role)
        )
        # WHICH APP THIS CONVERSATION IS ABOUT — in the SYSTEM prompt, not in their message.
        #
        # The thread carries the run id in `external_ref` and it stopped at thread lookup: the
        # model never saw it. So the console compensated by prepending a block of run facts to
        # whatever the operator typed, which (a) they could see in their own chat bubble, and
        # (b) still did not help, because the facts were a summary and the model wanted the
        # PRD. Scope belongs here, where it is invisible and durable, and the model can go and
        # READ the spec itself.
        scope = self._scope_note(connection, project_id, external_ref)
        if scope:
            system = system + "\n\n" + scope
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
                    # A 529 IS THE PROVIDER SAYING "NOT NOW", NOT AN ANSWER.
                    #
                    # Anthropic returns 529 overloaded_error when its capacity is saturated. The
                    # coding pipeline has always treated that as transient and retried it
                    # (code_agent's transient list names "529" explicitly); this path did not,
                    # so a capacity blip reached the operator as "I could not answer that" and
                    # they had to notice and ask again. The customer's only interface should not
                    # hand them a provider's load-shedding as a dead end.
                    _looking_only = asked_only_to_look(latest)
                    _report = _progress_reporter(project_id, thread_id)
                    turn = _with_transient_retry(lambda: self._answer_agent().chat(
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
                        # A QUESTION IS NOT AN INSTRUCTION. See `asked_only_to_look`: asked
                        # why a build was slow, BOSS cancelled it. A write wanted here comes
                        # back as `confirmation_required`, which the console renders as a
                        # button — so the operator gets the diagnosis and the fix, one click
                        # away, instead of a fait accompli.
                        allow_writes=not _looking_only, max_steps=12, on_step=_report,
                    ))
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
                        if _is_acting(str(step.get("tool"))) and not result.get("proposed"):
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
                    # ANSWERED AS A ROLE WITHOUT READING WHAT THAT ROLE HAS SEEN.
                    #
                    # Asked the same question as its colleagues — "what is your biggest concern
                    # about shipping HookWatch?" — the Operations agent called describe_role,
                    # skipped recall_role_experience, and wrote: "the deployment target record
                    # shows the container publishing on port 8080, but the health-check URL
                    # points to 8000 ... logged for the last three builds". The real value is
                    # 8000 on both. It invented a number and claimed to have inspected records
                    # it never opened.
                    #
                    # Its colleagues did not: QA read five deployments and quoted the actual
                    # error strings, and Security found nothing recorded and SAID so. The
                    # difference was whether the tool ran, so that is what is checked — not
                    # whether the prose looks specific, which is a judgement the same model
                    # would be making about its own output.
                    # ...UNLESS IT ALREADY READ THE RECORD EARLIER IN THIS CONVERSATION.
                    #
                    # The first version demanded the tool on EVERY turn, which produced this,
                    # live: asked "what did you just tell me was your biggest worry — quote
                    # yourself", Security quoted itself exactly, and the answer was stamped
                    # "NOT checked against Security's record on this project". The quote came
                    # from two messages up; there was nothing to check it against and nothing
                    # wrong with it. A caveat attached to a correct answer is not caution — it
                    # teaches the operator that the caveat means nothing, which spends the
                    # credibility the note needs on the turn that IS invented.
                    #
                    # The record is read once and stays in the thread's context, so the
                    # question is whether this CONVERSATION is grounded, not this turn.
                    read_before = any(
                        isinstance(call, dict)
                        and call.get("tool") == "recall_role_experience"
                        for message in (messages or [])
                        for call in (message.get("tool_calls") or [])
                    )
                    if role and reply and not read_before and not any(
                        c.get("tool") == "recall_role_experience" for c in tool_calls
                    ):
                        try:
                            reread = _with_transient_retry(lambda: self.agent.chat(
                                project_id=project_id, actor=actor,
                                message=(
                                    f"You answered as {role} without calling "
                                    "recall_role_experience, so nothing you said about this "
                                    "project came from its record. Call it NOW, then give the "
                                    "answer again: cite the briefs and positions you actually "
                                    "find, and if there are none, say plainly that you have no "
                                    "history here and are answering from the lens alone. Do "
                                    "not repeat any specific number or record you did not read."
                                ),
                                history=[{"role": "assistant", "content": reply}],
                                system=system, allow_writes=False, max_steps=4,
                            ))
                        except Exception:  # noqa: BLE001 — the first answer still stands
                            reread = None
                        for step in (getattr(reread, "trace", None) or []):
                            tool_calls.append({"tool": step.get("tool"),
                                               "args": step.get("args"),
                                               "ok": bool((step.get("result") or {})
                                                          .get("ok", True))})
                        grounded = getattr(reread, "reply", "")
                        if grounded and any(c.get("tool") == "recall_role_experience"
                                            for c in tool_calls):
                            reply = str(grounded).strip()
                        else:
                            # Could not ground it. Say which kind of answer this is rather
                            # than letting it pass as experience.
                            reply = reply.rstrip() + (
                                "\n\n(Note, added by BOSS: this answer was NOT checked against "
                                f"{role}'s record on this project — treat any specific figure "
                                "in it as unverified.)")

                    # LOOKED TOO LITTLE, OR HANDED THE PROBLEM BACK.
                    #
                    # The turn currently ends when the model produces text. For an operational
                    # question that is the wrong stopping condition: the incident answer was
                    # built on three lookups and the fourth was the one that mattered, and the
                    # measured average across 79 answers was 2.86 against a budget of 12.
                    #
                    # So one bounded continuation — not an open loop — when either is true:
                    # the answer rests on too little looking, or it ends by handing the problem
                    # back ("could not determine", "would you like me to investigate further").
                    # It is told exactly which fault it committed and what to do about it,
                    # because "try harder" is not an instruction a model can act on.
                    _thin = under_investigated(tool_calls, latest)
                    _handed_back = hands_the_problem_back(reply, proposed)
                    # A PRESCRIPTION IS NOT A FIX, AND A REPEAT IS NOT AN ANSWER.
                    #
                    # Three turns running, each after the operator asked for the deployment to be
                    # fixed, BOSS wrote a correct diagnosis and did nothing: "the fix is to
                    # retarget the app to an unused port and redeploy", with retarget_app and
                    # redeploy both in its hands. The operator replied "I told you already" —
                    # what a person says to a system that answers instead of working.
                    #
                    # AND ON A QUESTION TURN THE FAULT IS NOT ACTING, IT IS NOT OFFERING.
                    # Measured on the deployed build, asked why a Deploy step failed: a correct
                    # diagnosis, then a two-row "What you can do" table with an EMPTY Button
                    # column and "tell me to go ahead". `propose_action` is a READ tool and was
                    # available the whole time. Suppressing this fault on look-only turns left
                    # the operator holding a table of options they could not click.
                    _prescribed = described_a_fix_without_doing_it(reply, actions, proposed)
                    _repeating = going_in_circles(reply, history)
                    _hid_failure = hid_a_failed_action(reply, tool_calls)
                    if reply and (_thin or _handed_back or _prescribed or _repeating
                                  or _hid_failure):
                        gaps = []
                        if _hid_failure:
                            _names = ", ".join(sorted({
                                str(c.get("tool")) for c in tool_calls
                                if isinstance(c, dict) and c.get("ok") is False and c.get("tool")
                            }))
                            gaps.append(
                                f"You called {_names} this turn and it FAILED, and your answer "
                                "does not mention it. Say what you tried, quote what came back, "
                                "and say what you are doing about it. Never describe a situation "
                                "as though you had not touched it — the operator can see the "
                                "failed call next to your message, so omitting it does not hide "
                                "anything, it only makes you look like you did not notice. If "
                                "the failure was a rejected argument or a wrong table name, that "
                                "is information: fix it and try again this turn.")
                        if _thin:
                            gaps.append(
                                f"You answered an operational question after "
                                f"{len([c for c in tool_calls if c.get('tool')])} lookups. "
                                "That is not enough to be sure. Call diagnose_run on the run in "
                                "question if you have not, then read_logs, then the code — you "
                                "have a budget of twelve steps and answering early is how a "
                                "normal build gets called a stall.")
                        if _prescribed and _looking_only:
                            gaps.append(
                                "You described a fix and attached no button. This turn was a "
                                "question, so you were right not to change anything — but naming "
                                "an option the operator cannot click is the same as not offering "
                                "it. For EACH option you named, call propose_action with the real "
                                "tool and the real arguments you looked up (propose_action changes "
                                "nothing; it is available on a question turn). Never write an "
                                "options table with an empty button column, and never say 'tell me "
                                "to go ahead' in place of an offer.")
                        elif _prescribed:
                            gaps.append(
                                "You described a fix and did not apply it. The operator asked "
                                "you to fix this; a sentence beginning 'the fix is to' is a "
                                "prescription, not a repair. CALL the tool that performs it now "
                                "— retarget_app, redeploy, retry_code_task, whichever it is — "
                                "with concrete arguments you have looked up. If you genuinely "
                                "cannot, offer it with propose_action so it is one click away, "
                                "and say which tool you lack.")
                        if _repeating:
                            gaps.append(
                                "This is the same answer you just gave. Repeating a diagnosis "
                                "the operator has already read is not progress — they can see "
                                "you are stuck even if you cannot. Do something DIFFERENT this "
                                "turn: run a tool you have not run, read a source you have not "
                                "read (the host, the container, the app's code on its branch, "
                                "the service logs), or ACT on the conclusion you have already "
                                "reached. Do not restate it a third time.")
                        if _handed_back:
                            # THIS TEXT WAS BEING COPIED AS A TEMPLATE, NOT FOLLOWED.
                            #
                            # The previous wording — "state the single most likely cause, the ONE
                            # check that would confirm it" — came back twice as literal section
                            # headings: "**What I FOUND** / **What I COULD NOT DETERMINE** / **One
                            # check that would settle it** / **Single next action**", with the
                            # action described and never taken. The operator answered "yes, please
                            # do anything to fix it" and got the same four headings again. Naming
                            # a check instead of RUNNING it is the same failure the instruction
                            # was written to stop, wearing the instruction's own words.
                            gaps.append(
                                "Your answer hands the problem back. Do not reply with headed "
                                "sections, and never write a heading like 'What I could not "
                                "determine' or 'One check that would settle it' — if a check "
                                "would settle it, RUN THE CHECK; you have http_probe, "
                                "inspect_host, read_logs, query_db and find_free_port, and every "
                                "one of them answers in this turn. Only say something cannot be "
                                "determined after a tool has actually failed to determine it, and "
                                "then say which tool and what it returned. End with the thing you "
                                "DID, or a propose_action button — never with a plan, and never "
                                "with a yes/no question.")
                        try:
                            better = _with_transient_retry(lambda: self._answer_agent().chat(
                                project_id=project_id, actor="orchestrator",
                                message="\n\n".join(gaps) + (
                                    "\n\nAnswer the operator's original question again, better. "
                                    "Do not restate what you already said — replace it."),
                                history=history + [{"role": "assistant", "content": reply}],
                                system=system,
                                # Same permission the first pass had. A continuation must not be
                                # a way to acquire writes the turn was not entitled to.
                                allow_writes=not _looking_only, max_steps=8, on_step=_report,
                            ))
                        except Exception:  # noqa: BLE001 — the first answer still stands
                            better = None
                        for step in (getattr(better, "trace", None) or []):
                            result = step.get("result") or {}
                            tool_calls.append({"tool": step.get("tool"),
                                               "args": step.get("args"),
                                               "ok": bool(result.get("ok", True))})
                            if _is_acting(str(step.get("tool"))) and not result.get("proposed"):
                                actions.append({"kind": str(step.get("tool")),
                                                "args": step.get("args"), "result": result})
                            if result.get("proposed"):
                                proposed.append({
                                    "id": make_id("act"),
                                    "tool": result.get("tool") or step.get("tool"),
                                    "args": result.get("args") or {},
                                    "label": result.get("label"),
                                    "why": result.get("why"),
                                })
                        improved = str(getattr(better, "reply", "") or "").strip()
                        if improved:
                            reply = improved

                    # IT SAID IT DID IT. MAKE IT DO IT.
                    #
                    # Given the tool and explicit approval, BOSS read HookWatch's target, got
                    # the diagnosis right, and wrote "Action taken: retarget_app — set the
                    # health-check address to port 3020" without calling anything. The tool was
                    # in its schema list. It simply narrated the work instead of doing it.
                    #
                    # A correction note tells the operator the truth but still leaves them to
                    # ask again for something they already approved. So the claim is treated as
                    # the intent it obviously is: one more turn, told plainly that nothing ran
                    # and that it must call the tool now. If it acts, the operator gets what
                    # they asked for; if it does not, the correction below still fires and they
                    # are told, rather than being quietly left with a broken app.
                    # ...BUT NOT WHEN NOBODY ASKED FOR IT.
                    #
                    # This retry says "they have already approved it" and tells the model to
                    # call the tool NOW. On a question that sentence is false, and this path
                    # would take the destructive action the gate above just declined to allow —
                    # the same 27 minutes, one turn later. On a looking-only turn a claim of
                    # action is a false claim to CORRECT, which is what the reconciler below
                    # does, not an intent to carry out.
                    if (reply and not actions and not _looking_only
                            and _ACTION_CLAIM.search(reply)):
                        try:
                            again = _with_transient_retry(lambda: self.agent.chat(
                                project_id=project_id, actor=actor,
                                message=(
                                    "You just told the operator you had made a change, and no "
                                    "tool ran — the trace shows reads only. They have already "
                                    "approved it. Call the tool that performs it NOW, with "
                                    "concrete arguments. If you cannot, name the tool you "
                                    "lack; do not describe the change again."
                                ),
                                history=[{"role": "assistant", "content": reply}],
                                system=system, allow_writes=True, max_steps=4,
                            ))
                        except Exception:  # noqa: BLE001 — the first answer still stands
                            again = None
                        for step in (getattr(again, "trace", None) or []):
                            result = step.get("result") or {}
                            tool_calls.append({"tool": step.get("tool"),
                                               "args": step.get("args"),
                                               "ok": bool(result.get("ok", True))})
                            if _is_acting(str(step.get("tool"))) and not result.get("proposed"):
                                actions.append({"kind": str(step.get("tool")),
                                                "args": step.get("args"), "result": result})
                        if actions and getattr(again, "reply", ""):
                            reply = f"{reply}\n\n{str(again.reply).strip()}"

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
        printed, reply = _recover_printed_actions(reply, _known, proposed)
        proposed.extend(printed)
        proposed.extend(_recover_described_actions(reply, _known, proposed))
        reply = _no_button_was_promised(reply, proposed)
        # AND THAT IT DID NOT CLAIM TO HAVE ACTED. `actions` holds the writes that really ran;
        # a claim of completion with an empty list is a fabrication, and the trace is right
        # here to check it against.
        reply = _no_action_was_claimed(reply, actions)
        reply = _boss_cannot_call_you_back(reply)
        # SAY IT ONCE. Applied last, after every reconciler has had its say, so a correction
        # note is never mistaken for a repetition and stripped.
        reply = without_what_was_already_said(reply, history)
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
