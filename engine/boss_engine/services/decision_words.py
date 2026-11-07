"""What a decision is about, in the operator's words: a short title and where it came from.

THE RAW CODE WAS THE CARD. A spend guard's question reached the Command Center as "Missing
active reservation for OpenAI call" with `missing_reservation` as its detail — the producer's
log line and its reason code, shown to a person as if they were a sentence. Every producer that
raises a decision is mapped here once (by its recommendation action or reason code) to what it
asks, in plain words, and the place it comes from ("Spend guard", "Deploys"). Anything not
mapped still never shows a code: `humanize_code` turns `some_code` into "Some code".

Pure: no I/O, so every card, the pill and the tests share one answer.
"""
from __future__ import annotations

import json
import re
from typing import Any, Mapping, Optional

from . import decision_actions

#: A reason code, never a sentence: lower-case words joined by underscores.
CODE = re.compile(r"^[a-z0-9]+(?:_[a-z0-9]+)+$")

#: By recommendation action: (title, source).
_BY_ACTION: dict[str, tuple[str, str]] = {
    "stack_deploy_requires_approval": ("Approve a deploy", "Deploys"),
    "agent_tool_requires_approval": ("Let an agent use a tool", "Agent tools"),
    "execute_agent_tool": ("Let an agent use a tool", "Agent tools"),
    "activate_authored_skill": ("Approve a skill draft", "Skills"),
    "review_cost_anomaly": ("Review unusual spend", "Spend guard"),
    "approve_initiative_budget": ("Approve an initiative budget", "Budget"),
    "publish_document": ("Publish a document", "Documents"),
    "start_code_task": ("Start a build", "Factory"),
    "resume_code_task": ("Approve the plan", "Factory"),
    "grant_builder_authority": ("Let the builder work for you", "Identity"),
    "stuck_work_needs_person": ("A build needs you to unblock it", "Shepherd"),
    "review_pull_request": ("Review a pull request", "Factory"),
}

#: Refusal families (decision_actions.REFUSALS): (title, source).
_BY_REFUSAL: dict[str, tuple[str, str]] = {
    "budget": ("Budget refused work", "Budget"),
    "tier2_frozen": ("Budget froze high-risk work", "Budget"),
    "autoscale": ("Budget held back new workers", "Budget"),
    "deferred": ("Budget deferred a job", "Budget"),
    "capacity": ("A model ran out of capacity", "Models"),
}

#: What a spend guard's "Missing active reservation for <thing>" was about.
_RESERVED_THING: dict[str, str] = {
    "openai call": "an OpenAI call",
    "pr proposal": "a PR proposal",
    "outbound communication": "an outbound message",
    "infrastructure provisioning": "infrastructure provisioning",
}


def _rec(decision: Mapping[str, Any]) -> dict[str, Any]:
    rec = decision.get("recommendation")
    if isinstance(rec, str):
        try:
            rec = json.loads(rec)
        except ValueError:
            rec = {}
    return rec if isinstance(rec, dict) else {}


def humanize_code(value: Any) -> str:
    """`missing_reservation` -> "Missing reservation"; a sentence comes back as it was."""
    text = str(value or "").strip()
    if not CODE.match(text):
        return text
    words = text.replace("_", " ")
    return words[:1].upper() + words[1:]


def _codes(decision: Mapping[str, Any]) -> list[str]:
    rec = _rec(decision)
    return [str(v).strip() for v in (rec.get("action"), rec.get("reason_code"), rec.get("reason"),
                                     decision.get("reason")) if isinstance(v, str) and v.strip()]


def _reservation_title(question: str) -> str:
    m = re.search(r"reservation for (.+?)\.?$", question.strip(), re.IGNORECASE)
    thing = (m.group(1).strip() if m else "").lower()
    named = _RESERVED_THING.get(thing) or (f"a {thing}" if thing else "this")
    return f"Allow {named} without a reservation"


def headline(decision: Mapping[str, Any]) -> dict[str, str]:
    """{title, source} for a decision. Never a raw code; the question when nothing better."""
    question = str(decision.get("question") or "").strip()
    codes = _codes(decision)
    action = str(_rec(decision).get("action") or "")
    if "missing_reservation" in codes or action == "create_budget_reservation" or \
            question.lower().startswith("missing active reservation"):
        return {"title": _reservation_title(question), "source": "Spend guard"}
    if action in _BY_ACTION:
        title, source = _BY_ACTION[action]
        return {"title": title, "source": source}
    family = decision_actions._refusal_family({**dict(decision), "recommendation": _rec(decision)})
    if family in _BY_REFUSAL:
        title, source = _BY_REFUSAL[family]
        return {"title": title, "source": source}
    first = question.splitlines()[0].strip() if question else ""
    title = humanize_code(first) or humanize_code(action) or "A decision is waiting"
    return {"title": title[:160], "source": ""}


def plain_detail(detail: Any) -> str:
    """A card's detail is a sentence or nothing: a bare code is humanized."""
    return humanize_code(detail)


def source_of(decision: Mapping[str, Any]) -> Optional[str]:
    return headline(decision)["source"] or None
