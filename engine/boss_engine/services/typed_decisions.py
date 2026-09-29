"""Typed decisions — a model answering a question whose answers are fixed in advance.

BOSS makes judgements all through the pipeline (how much rigor a task needs, whether it touches
auth or money, whether a panel has converged), and most of them are keyword lists today. A
keyword list cannot tell "change the colour of the login button" from "change how login works".
A model can, but a model answering in prose is a parsing problem and an unanswerable audit.

So the question carries its answers. The caller names the options, the backend must pick one of
them, and anything else is rejected rather than guessed at. Several questions share one call,
because they usually share one context and the context is what costs.

Backends are interchangeable on purpose: the default runs on the Claude subscription the install
already has, and a hosted classifier (TypeSafe's Jev, say) can sit behind the same `decide` without
the caller knowing. Nothing here is allowed to unlock an irreversible action on its own — a
confidence is an input to a rule, not a permission.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Optional, Protocol

from ..config import Settings


@dataclass(frozen=True)
class Question:
    key: str
    instructions: str
    # label -> what that answer means. The label is what comes back; the meaning is for the model.
    options: dict[str, str]


@dataclass
class Decision:
    answer: Optional[str]
    confidence: Optional[float]
    reason: str = ""
    error: str = ""


@dataclass
class DecisionBatch:
    decisions: dict[str, Decision]
    backend: str
    model: str
    latency_ms: int
    cost_usd_micros: Optional[int] = None
    usage: dict = field(default_factory=dict)


GOVERNANCE_HOLD = (
    "Typed decision model calls stay paused (BOSS-451) unless this attempt already has a "
    "committed budget reservation, active model lease, deduplicated receipt, and cost "
    "settlement. Key proof is still paused."
)


class TypedDecisionGovernanceHold(RuntimeError):
    """A workflow must not spend before its model authority is durable."""


class Decider(Protocol):
    def decide(self, context: str, questions: list[Question]) -> DecisionBatch: ...


def _prompt(context: str, questions: list[Question]) -> str:
    spec = {
        q.key: {"question": q.instructions, "options": q.options}
        for q in questions
    }
    shape = {q.key: {"answer": "<one option label>", "confidence": "<0.0-1.0>",
                     "reason": "<one short sentence>"} for q in questions}
    return (
        "You are a classifier inside a software pipeline. Answer every question about the "
        "CONTEXT below by choosing exactly one of that question's option labels. Judge what the "
        "work actually does, not which words it happens to use.\n\n"
        f"QUESTIONS:\n{json.dumps(spec, indent=1)}\n\n"
        f"CONTEXT:\n<<<\n{context}\n>>>\n\n"
        "Reply with ONE JSON object and nothing else, shaped exactly like:\n"
        f"{json.dumps(shape)}\n"
        "confidence is your probability that the chosen answer is correct."
    )


def _parse(text: str, questions: list[Question]) -> dict[str, Decision]:
    from .claude_cli_adapter import _first_json_object

    raw = _first_json_object(text or "")
    obj = raw if isinstance(raw, dict) else {}
    out: dict[str, Decision] = {}
    for q in questions:
        item = obj.get(q.key)
        if not isinstance(item, dict):
            out[q.key] = Decision(None, None, error="no answer")
            continue
        answer = str(item.get("answer", "")).strip()
        if answer not in q.options:
            # Refused, not coerced. A near-miss label is still a guess about what was meant.
            out[q.key] = Decision(None, None, error=f"answer {answer!r} is not an option")
            continue
        try:
            conf = max(0.0, min(1.0, float(item.get("confidence"))))
        except (TypeError, ValueError):
            conf = None
        out[q.key] = Decision(answer, conf, reason=str(item.get("reason", ""))[:300])
    return out


class TypeSafeDecider:
    """Typed decisions on TypeSafe's hosted Jev model (https://docs.typesafe.ai/api.md).

    Optional and bring-your-own-key: Jev is proprietary and billed to the operator's TypeSafe
    account, so nothing in BOSS depends on it. Plain HTTP rather than their SDK keeps the open
    source core free of a dependency most installs will never use.

    Jev scores the offered options rather than writing text, so an answer outside the options
    cannot happen — which says nothing about whether the answer is right. Their own docs list
    prompt injection as a weakness: do not point it at untrusted text and act on the result.
    """

    backend = "typesafe"

    def __init__(self, settings: Settings, *, api_key: Optional[str] = None,
                 client=None, attempts: int = 3):
        self._key = api_key or _typesafe_key(settings)
        if not self._key:
            raise RuntimeError(
                "TYPESAFE_API_KEY is not set. Create a key at console.typesafe.ai, or set "
                "BOSS_DECISION_BACKEND=claude to decide on the Claude subscription."
            )
        self._url = settings.typesafe_api_url
        self.model = settings.typesafe_model
        self._client = client
        self._attempts = max(1, attempts)

    def _post(self, body: dict) -> dict:
        import httpx

        client = self._client or httpx.Client(timeout=30)
        try:
            for attempt in range(self._attempts):
                resp = client.post(self._url, json=body,
                                   headers={"Authorization": f"Bearer {self._key}"})
                # 429 rate limit and 529 overloaded are the two their docs say to back off on.
                if resp.status_code in (429, 529) and attempt + 1 < self._attempts:
                    time.sleep(float(resp.headers.get("retry-after") or 2 ** attempt))
                    continue
                if resp.status_code >= 400:
                    raise RuntimeError(f"TypeSafe {resp.status_code}: {resp.text[:300]}")
                return resp.json()
            raise RuntimeError("TypeSafe kept rate-limiting")
        finally:
            if self._client is None:
                client.close()

    def decide(self, context: str, questions: list[Question]) -> DecisionBatch:
        body = {
            "model": self.model,
            "state": context,
            "questions": {q.key: {"type": "choice", "instructions": q.instructions,
                                  "criteria": dict(q.options)} for q in questions},
        }
        started = time.monotonic()
        data = self._post(body)
        answers = data.get("answers") or {}
        decisions: dict[str, Decision] = {}
        for q in questions:
            a = answers.get(q.key) or {}
            choice = a.get("choice")
            if choice not in q.options:
                decisions[q.key] = Decision(None, None, error=f"answer {choice!r} is not an option")
                continue
            conf = a.get("confidence")
            decisions[q.key] = Decision(choice, float(conf) if conf is not None else None)
        usage = data.get("usage") or {}
        # $0.042 per million input tokens, output free (docs.typesafe.ai/models.md). An
        # estimate: the bill is TypeSafe's, and their price can change.
        cost = int(round(int(usage.get("input_tokens") or 0) * 0.042))
        return DecisionBatch(decisions=decisions, backend=self.backend,
                             model=str(data.get("model") or self.model),
                             latency_ms=int((time.monotonic() - started) * 1000),
                             cost_usd_micros=cost, usage=usage)


def _typesafe_key(settings: Settings) -> Optional[str]:
    if settings.typesafe_api_key:
        return settings.typesafe_api_key
    from .cli_harness import stored_credential

    return stored_credential(settings, "TYPESAFE_API_KEY")


class SecondOpinionDecider:
    """Ask the fast backend; re-ask the careful one when the fast one is unsure.

    The pattern LangChain describes for Jev in LangGraph: act on confident decisions, send the
    rest to an LLM. Only the unsure questions are re-asked, and the batch reports what both
    calls cost, so the saving is measured rather than assumed.
    """

    def __init__(self, fast: "Decider", careful: "Decider", below: float):
        self.fast, self.careful, self.below = fast, careful, below
        self.backend = f"{getattr(fast, 'backend', 'fast')}+{getattr(careful, 'backend', 'careful')}"

    def decide(self, context: str, questions: list[Question]) -> DecisionBatch:
        try:
            first = self.fast.decide(context, questions)
        except Exception:  # noqa: BLE001 — the fast backend is optional; the careful one is not
            return self.careful.decide(context, questions)
        unsure = [q for q in questions
                  if first.decisions[q.key].answer is None
                  or (first.decisions[q.key].confidence or 0.0) < self.below]
        if not unsure:
            return first
        second = self.careful.decide(context, unsure)
        merged = {**first.decisions, **second.decisions}
        return DecisionBatch(
            decisions=merged, backend=self.backend, model=f"{first.model}+{second.model}",
            latency_ms=first.latency_ms + second.latency_ms,
            cost_usd_micros=(first.cost_usd_micros or 0) + (second.cost_usd_micros or 0),
            usage={"fast": first.usage, "careful": second.usage,
                   "escalated": [q.key for q in unsure],
                   # WHAT THE FAST BACKEND SAID BEFORE IT WAS OVERRULED. Keeping only the final
                   # answer made "how often is Jev right on its own?" unanswerable, and an
                   # operator auditing a decision wants both opinions, not the winner.
                   "first_opinion": {q.key: {"answer": first.decisions[q.key].answer,
                                             "confidence": first.decisions[q.key].confidence}
                                     for q in unsure}},
        )


def make_decider(settings: Settings) -> "Decider":
    """The decider this install is configured for. TypeSafe only when asked for by name."""
    claude = ClaudeDecider(settings, model=settings.decision_model or None)
    if (settings.decision_backend or "claude").strip().lower() != "typesafe":
        return claude
    jev = TypeSafeDecider(settings)
    if settings.decision_fallback_below > 0:
        return SecondOpinionDecider(jev, claude, settings.decision_fallback_below)
    return jev


class ClaudeDecider:
    """Typed decisions on the Claude subscription, through the same CLI the chat uses.

    The confidence is the model's own estimate, not a calibrated probability — treat it as a
    ranking signal, and measure it before trusting it as a threshold.

    THE WORKER HAS NO `claude` BINARY. A workflow's decision step runs there, and a decider that
    only knew the CLI failed every one with "claude CLI not found" — including the second
    opinion behind Jev, so an unsure Jev answer failed the step instead of being checked. Where
    the CLI is absent this uses the same API model the worker's agent steps already use.
    """

    def __init__(self, settings: Settings, model: Optional[str] = None, attempts: int = 2):
        import shutil

        from .claude_cli_adapter import ClaudeCliAdapter, openai_fallback_usable

        cli = ClaudeCliAdapter(settings, model=model)
        if shutil.which(cli._claude_bin()):
            self._adapter, self.backend = cli, "claude_code"
            self.model = model or getattr(settings, "claude_cli_model", None) or ""
        elif openai_fallback_usable(settings):
            from .openai_adapter import OpenAIAdapter

            # A Claude alias ("opus") means nothing to this API, so its own configured model
            # answers.
            self._adapter, self.backend, self.model = OpenAIAdapter(settings), "openai", ""
        else:
            raise RuntimeError(
                "no model can answer decisions in this process: the claude CLI is not installed "
                "here and no API key is configured.")
        self._attempts = max(1, attempts)

    def decide(self, context: str, questions: list[Question]) -> DecisionBatch:
        prompt = _prompt(context, questions)
        started = time.monotonic()
        cost, usage, decisions = 0, {}, {}
        for _ in range(self._attempts):
            _, text, usage = self._adapter.create_text_response(prompt)
            cost += int(usage.get("cost_usd_micros") or 0)
            decisions = _parse(text, questions)
            if all(d.answer is not None for d in decisions.values()):
                break
        return DecisionBatch(
            decisions=decisions, backend=self.backend,
            model=str(usage.get("model") or self.model),
            latency_ms=int((time.monotonic() - started) * 1000),
            cost_usd_micros=cost, usage=usage,
        )


# --- The workflow step --------------------------------------------------------------------- #
# `binding.typed_decision` on a capability step:
#
#   - id: route_ticket
#     kind: capability
#     binding:
#       typed_decision:
#         question: Which team should handle this ticket?
#         options: {billing: "Payments, refunds", technical: "Bugs, outages"}
#         min_confidence: 0.8          # optional; below it, output.confident is false
#       inputs: {state: {from_input: ticket}}
#     next: [pick_team]
#   - id: pick_team
#     kind: condition
#     expression: {path: steps.route_ticket.output.answer, op: eq, value: billing}
#
# An unsure answer is still an answer, so the step succeeds and says so: a workflow routes
# `output.confident == false` to a `decision` step and a person picks. No answer at all fails
# the step, because a branch taken on nothing is a guess.

_MAX_OPTIONS = 255  # Jev's limit for a choice question; a list longer than this is a search.


def step_problems(step_id: str, binding: dict) -> list[str]:
    spec = binding.get("typed_decision")
    if not isinstance(spec, dict):
        return [f"step '{step_id}' typed_decision must be an object with a question and options."]
    problems = []
    if not str(spec.get("question") or "").strip():
        problems.append(f"step '{step_id}' must say what question it decides.")
    options = spec.get("options")
    if not isinstance(options, dict) or not 2 <= len(options) <= _MAX_OPTIONS:
        problems.append(f"step '{step_id}' needs between 2 and {_MAX_OPTIONS} options, each "
                        "a label mapped to what that answer means.")
    elif not all(str(k).strip() for k in options):
        problems.append(f"step '{step_id}' has an option with an empty label.")
    floor = spec.get("min_confidence")
    if floor is not None and not (isinstance(floor, (int, float)) and 0 <= floor <= 1):
        problems.append(f"step '{step_id}' min_confidence must be between 0 and 1.")
    wiring = binding.get("inputs") or {}
    if not isinstance(wiring, dict) or "state" not in wiring:
        problems.append(f"step '{step_id}' must wire 'state' — the thing being judged.")
    return problems


def _step_output(spec: dict, batch: DecisionBatch) -> dict:
    """Format a previously settled answer without making a provider call."""
    d = batch.decisions.get("decision")
    if d is None or d.answer is None:
        raise RuntimeError(f"the decision model gave no usable answer "
                           f"({d.error if d else 'nothing returned'}).")
    floor = spec.get("min_confidence")
    return {
        "kind": "typed_decision",
        "answer": d.answer,
        "confidence": d.confidence,
        "confident": floor is None or (d.confidence is not None and d.confidence >= float(floor)),
        "reason": d.reason,
        "backend": batch.backend, "model": batch.model,
        "latency_ms": batch.latency_ms, "cost_usd_micros": batch.cost_usd_micros,
        # Present only when a second opinion overruled or confirmed an unsure first answer.
        **({"first_opinion": batch.usage["first_opinion"]["decision"]}
           if "decision" in (batch.usage.get("first_opinion") or {}) else {}),
    }


def run_step(settings: Settings, binding: dict, state, *, spend=None) -> dict:
    """Answer one typed decision, or hold when the call has no spend authority.

    `spend` is a TypedDecisionSpend that has already committed a reservation,
    a model lease, and a receipt. Without it this function does not construct
    a provider client.
    """
    if spend is None:
        raise TypedDecisionGovernanceHold(GOVERNANCE_HOLD)
    return spend.execute(settings, binding, state)
