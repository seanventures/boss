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
    "apply_agent_prompt_diff": ("Approve an instruction change", "Agents"),
    "apply_wiki_patch": ("Approve a wiki change", "Wiki"),
    "review_cost_anomaly": ("Review unusual spend", "Spend guard"),
    "approve_initiative_budget": ("Approve an initiative budget", "Budget"),
    "publish_document": ("Publish a document", "Documents"),
    "start_code_task": ("Start a build", "Factory"),
    "resume_code_task": ("Approve the plan", "Factory"),
    "grant_builder_authority": ("Let the builder work for you", "Identity"),
    "review_always_on_build": ("Review an always-on build", "Autonomy"),
    "stuck_work_needs_person": ("A build needs you to unblock it", "Shepherd"),
    "incident_needs_owner": ("An acknowledged incident needs an owner", "Shepherd"),
    "sign_in_to_subscription_again": ("Sign in to a subscription again", "Models"),
    "review_pull_request": ("Review a pull request", "Factory"),
    "change_sync_branch": ("Sync a change's branch", "Changes"),
    "gateway_tool_call": ("Let a build agent use a tool", "Build agents"),
    "gateway_ask_person": ("A build agent has a question", "Build agents"),
    "gateway_request_approval": ("A build agent asks for approval", "Build agents"),
}

#: Refusal families (decision_actions.REFUSALS): (title, source).
_BY_REFUSAL: dict[str, tuple[str, str]] = {
    "budget": ("Budget refused work", "Budget"),
    "tier2_frozen": ("Budget froze high-risk work", "Budget"),
    "autoscale": ("Budget held back new workers", "Budget"),
    "deferred": ("Budget deferred a job", "Budget"),
    "capacity": ("A model ran out of capacity", "Models"),
    "rules_forbidden": ("Egress refused the clone", "Factory"),
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


#: What a budget category pays for, said the way a person says it.
_SPEND_SUBJECT = {
    "knowledge": "Knowledge indexing", "code_task": "A build", "model": "An agent's model call",
    "model_gateway": "An agent's model call", "deployment": "A deploy", "deploy": "A deploy",
    "comms": "A message", "communication": "A message", "infra": "Infrastructure",
    "infrastructure": "Infrastructure", "discovery": "Research", "meeting": "A meeting",
    "voice": "A voice call", "architecture": "Architecture review",
}


def key_words(value: Any) -> str:
    """A key said as words: `code.task` -> "Code task", `checkout_error_rate` -> "Checkout error
    rate". For the places a key is all there is to name the thing by."""
    words = " ".join(str(value or "").replace(".", " ").replace("_", " ").replace("-", " ").split())
    return (words[:1].upper() + words[1:]) if words else ""


def spend_subject(category: Any, reason: Any = None) -> str:
    """The work a reservation pays for, in words: "Knowledge indexing", never `knowledge.embed`."""
    key = str(category or "").strip().lower()
    if key in _SPEND_SUBJECT:
        return _SPEND_SUBJECT[key]
    skill = str(reason or "").split(":", 1)[0].strip()
    words = (skill or key).replace(".", " ").replace("_", " ").strip()
    return (words[:1].upper() + words[1:]) if words else "This work"


#: An identifier a person must never read on a card: BOSS's own ids (`crun_9f2c…`,
#: `act_01M6…`, `res_4a23…`) and a skill's dotted key (`knowledge.embed`).
RAW_ID = re.compile(r"\b(?:[a-z]{2,6}_[0-9A-Za-z]{6,}|[a-z][a-z_]+\.[a-z][a-z_]+(?:\.[a-z_]+)*)\b")
#: Dotted words that are prose, not keys (file names and hosts are allowed by their extension).
_PROSE_DOTTED = re.compile(r"\.(?:yaml|yml|json|md|py|ts|tsx|js|txt|toml|com|org|io|dev|ai)\b")


def card_problems(decision: Mapping[str, Any]) -> list[str]:
    """Why a person-facing decision breaks the card rule, or [] (THE guard: every card names
    its subject in words, shows no raw identifier, and gives either a recommended answer or
    the reason only a person can answer). Pure."""
    problems: list[str] = []
    question = str(decision.get("question") or "").strip()
    rec = _rec(decision)
    if not question or CODE.match(question):
        problems.append("no subject in words")
    for m in RAW_ID.finditer(question):
        token = m.group(0)
        if "." in token and _PROSE_DOTTED.search(question[m.start():m.end() + 6]):
            continue
        problems.append(f"raw identifier {token!r}")
    answer = next((rec.get(k) for k in ("recommended", "next", "why_a_person", "why_not",
                                        "summary", "what_to_do") if rec.get(k)), None)
    if not answer:
        try:
            answer = (decision_actions.guidance({**dict(decision), "recommendation": rec})
                      or {}).get("what_to_do")
        except Exception:  # noqa: BLE001 — a card guidance cannot read has none
            answer = None
    if not answer:
        problems.append("no recommended answer and no reason it needs a person")
    return problems


def plain_detail(detail: Any) -> str:
    """A card's detail is a sentence or nothing: a bare code is humanized."""
    return humanize_code(detail)


def source_of(decision: Mapping[str, Any]) -> Optional[str]:
    return headline(decision)["source"] or None
