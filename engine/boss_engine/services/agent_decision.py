"""THE AGENT DECIDES WITH A MODEL. Lists and rules are candidates and evidence, never the decider.

THE MANDATE (operator, 2026-10-18). "If no model is present our product won't work, everyone
knows that. If we have a predefined list we can ask decision models like Jev to confirm the
decision or an accuracy threshold, but it cannot / should not work just on keywords without
context or models. Not everyone will ask the same question."

THE CASES. Every one of these was a word in a sentence picking an action with no model reading
the state it was about:
  - "please retry the Build step of the Order API run (wfr_…)" matched `build` and became a NEW
    governed code task titled with the sentence (crun_61720b80), reported as started;
  - "why did this take so long, can you investigate?" matched `investigate` and started a
    discovery that hit the burst cap;
  - two `and`s in a pasted WHERE clause became a three-phase plan;
  - a failed step's coded triage chose fix/retry/clean with no model reading the evidence.

THE SHAPE. One seam, `decide`, that every agent-facing decision goes through:

  1. The CALLER names the candidate actions (`Candidate`): what each does, whether it is
     available here and, if not, where it is done instead. A keyword reading of the message, a
     coded triage, a pattern on a log line — those are passed as `hints`: evidence the model
     weighs, labelled as BOSS's guess.
  2. The context the model needs is handed over in full: the request and the recent thread,
     the run/step/records it is about, what the agent can and cannot do.
  3. THE DECISION MODEL decides: Jev (TypeSafe) when it is chosen, keyed and not held
     (BOSS-451), otherwise the project's routed model (`model_routing.project_chat_adapter`,
     governed: reserved, receipted, settled). Model-agnostic: every engine gets the same prompt
     and the same JSON contract; nothing here depends on one provider.
  4. The answer is an action and a confidence. Below the threshold the agent asks the person
     ONE clarifying question and takes no action. An answer outside the candidates is refused.
  5. NO USABLE MODEL: `status == "no_model"`. The caller says so honestly (`NO_MODEL_REPLY`) and
     takes no action. There is no keyword fallback behind this function, on purpose.

Pure over its seams (`adapter`, `decider`), so tests drive it with a stub model.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Sequence

log = logging.getLogger(__name__)

#: Said whenever an agent cannot reach a model. Honest, specific, and names where to fix it.
NO_MODEL_REPLY = (
    "BOSS needs a connected model to understand this, and none is answering right now, so I "
    "have not done anything. Connect one in Repo and models (a subscription, an API key or a "
    "local model), then ask again.")

#: Below this confidence the agent asks ONE question instead of acting. An operator can lower
#: or raise it per install (`agent_decision_confidence`).
DEFAULT_THRESHOLD = 0.7

#: The decision a person gets when the model is unsure and wrote no question of its own.
_FALLBACK_QUESTION = "I'm not sure enough to act alone. Should I {first}, or {second}?"

SKILL_ID = "model.agent_decision"
ROUTE_KIND = "agent"
ROUTE_STEP = "agent_decision"

DECIDED = "decided"
CLARIFY = "clarify"
NO_MODEL = "no_model"


@dataclass(frozen=True)
class Candidate:
    """One action the agent may take. `means` is what choosing it DOES, in words."""

    key: str
    means: str
    available: bool = True
    #: When not available: why, and where a person does it instead.
    why_not: str = ""
    #: Whether choosing it CHANGES anything. A candidate that does not (answer a question, look
    #: into something) is taken even below the confidence bar: there is no action to guess,
    #: and asking "did you want me to answer or to look?" helps nobody.
    acts: bool = True
    #: A short verb phrase for a button and a question ("Open its own incident"). The question
    #: the agent asks is built from these, never from `means` pasted mid-sentence.
    label: str = ""


@dataclass
class AgentDecision:
    status: str
    action: Optional[str] = None
    confidence: float = 0.0
    reason: str = ""
    #: The ONE question for the person, when status == clarify.
    question: str = ""
    by: dict = field(default_factory=dict)
    cost_usd_micros: int = 0
    error: str = ""

    @property
    def decided(self) -> bool:
        return self.status == DECIDED and bool(self.action)

    def to_dict(self) -> dict:
        return {"status": self.status, "action": self.action,
                "confidence": round(float(self.confidence or 0.0), 3), "reason": self.reason,
                "question": self.question, "by": dict(self.by),
                "cost_usd_micros": int(self.cost_usd_micros or 0),
                **({"error": self.error} if self.error else {})}


def threshold(connection: Any, settings: Any, project_id: Optional[str]) -> float:
    """The confidence an action needs. Settings knob, else the default."""
    value: Any = getattr(settings, "agent_decision_confidence", None)
    if connection is not None and project_id:
        try:
            from . import project_settings

            point = connection.begin_nested() if hasattr(connection, "begin_nested") else None
            try:
                stored = project_settings.get_all(connection, project_id).get(
                    "agent_decision_confidence")
            finally:
                if point is not None:
                    point.commit()
            if stored not in (None, ""):
                value = stored
        except Exception:  # noqa: BLE001 — an unreadable knob is the default, not a failure
            pass
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return DEFAULT_THRESHOLD


# ── The prompt ────────────────────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are the decision step of {agent}, an agent inside BOSS (a governed \
software factory). Read the person's request IN CONTEXT and decide which ONE of the candidate \
actions it asks for. Judge what they want done to the state in front of you, not which words \
they used: people phrase the same request tersely, at length, in non-native English, with typos.

Rules:
- A request about work that EXISTS (a run, a step, a build, a deployment named or in view) is \
carried out on that work. Retrying, resuming or fixing an existing step is never new work.
- A question, a report of a problem, or "why/what happened" is answered, not acted on.
- BOSS's hints are guesses from patterns; weigh them against the context, never obey them.
- If two candidates are both reasonable readings and they lead to different actions, or the \
request does not say which thing it is about, your confidence is low and you write ONE short \
question that would settle it.
- Choose an unavailable candidate if that is what they asked for; BOSS will say where it is done.
- confidence is your probability that the chosen action is what the person wants.

Answer with ONLY a JSON object:
{{"action": "<one candidate key>", "confidence": 0.0, "reason": "<one sentence>", \
"question": "<one clarifying question, or empty>"}}"""


def _candidates_block(candidates: Sequence[Candidate]) -> str:
    rows = []
    for c in candidates:
        tail = "" if c.available else f"  [NOT AVAILABLE HERE: {c.why_not or 'not enabled'}]"
        rows.append(f"- {c.key}: {c.means}{tail}")
    return "\n".join(rows)


def _context_block(context: Mapping[str, Any], limit: int = 9000) -> str:
    try:
        from .redaction import redact_secrets
    except Exception:  # noqa: BLE001
        def redact_secrets(value: str) -> str:  # type: ignore[misc]
            return value
    body = redact_secrets(json.dumps(dict(context), default=str, indent=1))
    return body if len(body) <= limit else body[:limit] + "\n… (truncated)"


def _history_block(history: Sequence[Mapping[str, Any]], turns: int = 6) -> str:
    out = []
    for m in list(history or [])[-turns:]:
        role = str(m.get("role") or "")
        if role not in ("user", "assistant"):
            continue
        content = m.get("content")
        if not isinstance(content, str):
            continue
        out.append(f"{'PERSON' if role == 'user' else 'BOSS'}: {content.strip()[:1200]}")
    return "\n".join(out)


def render(*, situation: str, message: str, history: Sequence[Mapping[str, Any]] = (),
           context: Mapping[str, Any] | None = None, candidates: Sequence[Candidate],
           hints: Sequence[str] = ()) -> str:
    parts = [f"SITUATION: {situation}"]
    said = _history_block(history)
    if said:
        parts.append("RECENT CONVERSATION (oldest first):\n" + said)
    parts.append("THE REQUEST:\n<<<\n" + (message or "").strip()[:6000] + "\n>>>")
    if context:
        parts.append("CONTEXT (what BOSS can see):\n" + _context_block(context))
    if hints:
        parts.append("BOSS'S HINTS (pattern guesses, not decisions):\n"
                     + "\n".join(f"- {h}" for h in hints))
    parts.append("CANDIDATE ACTIONS:\n" + _candidates_block(candidates))
    return "\n\n".join(parts)


def parse(answer: str) -> Optional[dict]:
    """The first JSON object holding an action, from a reply that may wrap it in prose."""
    body = str(answer or "")
    start = body.find("{")
    while start >= 0:
        depth = 0
        for i in range(start, len(body)):
            if body[i] == "{":
                depth += 1
            elif body[i] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        value = json.loads(body[start:i + 1])
                    except ValueError:
                        break
                    if isinstance(value, dict) and value.get("action"):
                        return value
                    break
        start = body.find("{", start + 1)
    return None


def _confidence(value: Any) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return 0.0


def _question_for(candidates: Sequence[Candidate], first: Optional[str], given: str) -> str:
    given = (given or "").strip()
    if len(given) >= 8:
        return given[:600]
    keys = [c for c in candidates if c.key == first] + [c for c in candidates if c.key != first]
    keys = [c for c in keys if c.acts and c.available] or keys
    if len(keys) >= 2:
        return _FALLBACK_QUESTION.format(first=_phrase(keys[0]), second=_phrase(keys[1]))
    return "Before I do anything: what exactly do you want done?"


def _phrase(c: Candidate) -> str:
    """A candidate as the tail of "should I …": its label, lower-cased; else its `means` from
    after the reason ("It is noise (…): mark it noise" → "mark it noise")."""
    text = (c.label or c.means).strip().rstrip(".")
    if not c.label and ": " in text:
        text = text.split(": ", 1)[1]
    return text[:1].lower() + text[1:] if text else c.key


# ── The backends ──────────────────────────────────────────────────────────────────────────────

def _jev_ready(settings: Any) -> bool:
    try:
        from .model_router.classifier import jev_state

        return jev_state(settings) == "ready"
    except Exception:  # noqa: BLE001
        return False


def _ask_jev(decider: Any, prompt: str, candidates: Sequence[Candidate]) -> dict:
    from .typed_decisions import Question

    question = Question(
        key="action",
        instructions="Which candidate action does the person's request ask for, in context?",
        options={c.key: c.means + ("" if c.available else f" (not available: {c.why_not})")
                 for c in candidates})
    batch = decider.decide(prompt, [question])
    got = batch.decisions.get("action")
    return {"raw": {"action": got.answer if got else None,
                    "confidence": got.confidence if got else 0.0,
                    "reason": got.reason if got else "", "question": ""},
            "by": {"backend": getattr(batch, "backend", "typesafe"),
                   "model": getattr(batch, "model", "")},
            "cost": int(getattr(batch, "cost_usd_micros", 0) or 0),
            "error": (got.error if got else "no answer")}


def _routed(connection: Any, settings: Any, project_id: str, ref: str, text: str):
    from . import model_routing
    from .model_router.router import RouteRequest

    purpose = RouteRequest(kind=ROUTE_KIND, step=ROUTE_STEP, project_id=project_id, ref=ref,
                           text=text[:2000])
    raw = model_routing.project_chat_adapter(connection, settings, project_id, purpose=purpose)
    if raw is None:
        raise RuntimeError("no model is connected")
    try:
        return model_routing.governed(raw, connection, settings, project_id, skill_id=SKILL_ID,
                                      ref=ref or "agent_decision"), raw
    except Exception:  # noqa: BLE001 — an ungovernable stub (tests) is still the model
        return raw, raw


def _ask_model(adapter: Any, system: str, prompt: str) -> dict:
    result = adapter.create_text_response(prompt, system_prompt=system)
    if isinstance(result, tuple):
        text_out = result[1] if len(result) > 1 else ""
        usage = result[2] if len(result) > 2 else {}
    else:
        text_out, usage = str(result or ""), {}
    usage = usage if isinstance(usage, Mapping) else {}
    raw = parse(str(text_out or ""))
    return {"raw": raw, "by": {"backend": "model", "model": str(usage.get("model") or "")},
            "cost": int(usage.get("cost_usd_micros") or 0),
            "error": "" if raw else "the model did not answer with the JSON object asked for"}


# ── The seam ──────────────────────────────────────────────────────────────────────────────────

def decide(connection: Any, settings: Any, project_id: str, *, situation: str, message: str,
           candidates: Sequence[Candidate], history: Sequence[Mapping[str, Any]] = (),
           context: Mapping[str, Any] | None = None, hints: Sequence[str] = (),
           agent: str = "BOSS", ref: str = "", min_confidence: Optional[float] = None,
           adapter: Any = None, decider: Any = None) -> AgentDecision:
    """One decision by a model, checked against a confidence threshold. Never raises.

    `adapter` (a chat adapter) or `decider` (a typed-decision backend) override the backend —
    the seams tests and the battle-test runner use. With neither, Jev when ready, else the
    project's routed model. With no model at all: status `no_model`, and no action.
    """
    keys = {c.key for c in candidates}
    if not keys:
        return AgentDecision(NO_MODEL, error="no candidates")
    bar = min_confidence if min_confidence is not None else threshold(connection, settings,
                                                                     project_id)
    prompt = render(situation=situation, message=message, history=history, context=context,
                    candidates=candidates, hints=hints)
    system = SYSTEM_PROMPT.format(agent=agent)
    try:
        if decider is not None or (adapter is None and _jev_ready(settings)):
            if decider is None:
                from .typed_decisions import TypeSafeDecider

                decider = TypeSafeDecider(settings)
            got = _ask_jev(decider, system + "\n\n" + prompt, candidates)
        else:
            if adapter is None:
                adapter, _raw = _routed(connection, settings, project_id, ref, message)
            got = _ask_model(adapter, system, prompt)
            if got["raw"] is None:
                # Asked once more, plainly: a model that wrapped its answer in prose has still
                # answered; one that did not gets one chance to.
                again = _ask_model(adapter, system, prompt + "\n\nAnswer with ONLY the JSON "
                                                              "object.")
                again["cost"] += got["cost"]
                got = again
    except Exception as exc:  # noqa: BLE001 — no model is an answer, never a crash
        log.info("agent decision had no model: %s", exc)
        return AgentDecision(NO_MODEL, error=f"{type(exc).__name__}: {exc}"[:300])

    raw = got.get("raw")
    by, cost = dict(got.get("by") or {}), int(got.get("cost") or 0)
    if not isinstance(raw, Mapping) or not raw.get("action"):
        return AgentDecision(NO_MODEL, by=by, cost_usd_micros=cost,
                             error=str(got.get("error") or "no answer")[:300])
    action = str(raw.get("action") or "").strip()
    confidence = _confidence(raw.get("confidence"))
    reason = str(raw.get("reason") or "")[:500]
    if action not in keys:
        # Refused, not coerced: a near-miss key is a guess about what the model meant.
        return AgentDecision(CLARIFY, action=None, confidence=0.0, reason=(
            f"the model answered '{action}', which is not one of the candidates"),
            question=_question_for(candidates, None, str(raw.get("question") or "")),
            by=by, cost_usd_micros=cost)
    chosen = next(c for c in candidates if c.key == action)
    if confidence < bar and chosen.acts:
        return AgentDecision(CLARIFY, action=action, confidence=confidence, reason=reason,
                             question=_question_for(candidates, action,
                                                    str(raw.get("question") or "")),
                             by=by, cost_usd_micros=cost)
    return AgentDecision(DECIDED, action=action, confidence=confidence, reason=reason,
                         question=str(raw.get("question") or "")[:600], by=by,
                         cost_usd_micros=cost)


__all__ = ["AgentDecision", "Candidate", "CLARIFY", "DECIDED", "DEFAULT_THRESHOLD", "NO_MODEL",
           "NO_MODEL_REPLY", "decide", "parse", "render", "threshold"]
