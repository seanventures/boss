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
from typing import Any, Callable, Iterator, Mapping, Optional, Sequence

from .tool_result_text import clip_for_model

log = logging.getLogger(__name__)

#: Said whenever an agent cannot reach a model AND BOSS cannot tell why. Honest, specific, and
#: names where to fix it. When the provider's own words say it is specifically out of credit,
#: `no_model_reply` says THAT instead (BOSSAPP-604): "no model is answering" reads as "nothing
#: is connected" and sends a person to re-check a connection that was never the problem.
NO_MODEL_REPLY = (
    "BOSS needs a connected model to understand this, and none is answering right now, so I "
    "have not done anything. Connect one in Repo and models (a subscription, an API key or a "
    "local model), then ask again.")


def no_model_reply(decision: "AgentDecision") -> str:
    """What to tell a person when `decision.status == NO_MODEL`: the generic NO_MODEL_REPLY,
    unless the provider's own words said plainly that the account is out of credit — then
    exactly that, with the provider named when it is known, up front rather than buried in a
    truncated error nobody reads."""
    from . import provider_limits

    if not provider_limits.credit_exhausted(decision.error):
        return NO_MODEL_REPLY
    who = decision.provider or "its provider"
    return (f"BOSS could not get an answer: credit balance too low on {who}. Add credit there "
            "(or connect a different model in Repo and models), then ask again.")

#: Below this confidence the agent asks ONE question instead of acting. An operator can lower
#: or raise it per install (`agent_decision_confidence`).
DEFAULT_THRESHOLD = 0.7

#: The decision a person gets when the model is unsure and wrote no question of its own.
_FALLBACK_QUESTION = "I'm not sure enough to act alone. Should I {first}, or {second}?"

SKILL_ID = "model.agent_decision"
ROUTE_KIND = "agent"
ROUTE_STEP = "agent_decision"

#: K2 (BOSSAPP-961, HARNESS_KERNEL_DESIGN.md §8.1): with this set, the READ PHASE and its final
#: choice run on the harness kernel (`kernel.run` with presets.decision) instead of
#: `_ask_reading`'s own loop. ON BY DEFAULT since AP-P1b (kernel/flags.py); an off word ("0") is
#: byte-identical to the old loop. Jev, the candidates, the no-model answer and the confidence
#: bar stay caller-side either way.
KERNEL_FLAG = "BOSS_KERNEL_DECISIONS"


def kernel_decisions() -> bool:
    """K2 is ON by default (AP-P1b, kernel/flags.py); BOSS_KERNEL_DECISIONS=0 is the escape
    hatch back to `_ask_reading`."""
    from ..kernel.flags import on

    return on(KERNEL_FLAG)

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
    #: What choosing it must come with (BOSSAPP-759): a JSON-schema-shaped object
    #: ({"properties": {name: {"type", "description"}}, "required": [...]}) the model fills as
    #: `arguments`. The decision returns them validated (`AgentDecision.arguments`); a missing
    #: required one, or one of the wrong shape, is refused like any other bad answer.
    arguments: Optional[Mapping[str, Any]] = field(default=None, compare=False, hash=False)


@dataclass(frozen=True)
class ReadPhase:
    """READS BEFORE THE DECISION (BOSSAPP-759): read tools the agent may call to look for more
    evidence before it answers, bounded by the caller in turns and in model cost. Their results
    join the corpus the caller's `check` cites against. `tools` are OpenAI-shaped schemas (none
    offers none: the decision is still asked as a conversation, and re-asked in it);
    `run(name, args)` runs one and returns its result. Reads only: a read phase never writes."""

    tools: Sequence[Mapping[str, Any]] = ()
    run: Optional[Callable[[str, dict], Any]] = None
    max_turns: int = 4
    max_usd_micros: int = 300_000
    result_chars: int = 8_000


@dataclass(frozen=True)
class SpendEnvelope:
    """A CALLER'S SPEND ENVELOPE (BOSSAPP-759): the model attempt a caller already governs —
    one pinned provider, a reservation and a model lease held across a live stream, a settled
    receipt (conversation_spend.ConversationCallSpend is one). The pipeline's model call runs
    INSIDE it, in place of the pipeline's own governance:

        run(produce, prompt=…, system_prompt=…) yields ("chunk", text)… then
        ("done", (text, usage)); `produce(pinned_adapter)` is the pipeline's own call
        (engine_agent.stream_answer), started lazily inside the envelope's scope.

    An envelope admits ONE attempt, so a turn under it is never asked twice, and its failures
    (a reconciliation it must hold, a stopped stream) are its owner's to settle: they are
    raised, never folded into "no model"."""

    run: Callable[..., Iterator[tuple[str, Any]]]


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
    #: The provider BOSS tried to reach (its engine name), when status is no_model and that is
    #: known — so a credit message can name it instead of saying "the model's provider".
    provider: str = ""
    #: The chosen action's arguments, validated against its schema (and the caller's check).
    arguments: dict = field(default_factory=dict)
    #: What the model cited for its choice, as it wrote it.
    evidence: list = field(default_factory=list)
    #: Why the last answer was refused, when it was (status no_model: nothing was decided).
    refused: str = ""
    #: Model turns spent (a read phase takes several).
    turns: int = 0

    @property
    def decided(self) -> bool:
        return self.status == DECIDED and bool(self.action)

    def to_dict(self) -> dict:
        return {"status": self.status, "action": self.action,
                "confidence": round(float(self.confidence or 0.0), 3), "reason": self.reason,
                "question": self.question, "by": dict(self.by),
                "cost_usd_micros": int(self.cost_usd_micros or 0),
                **({"arguments": dict(self.arguments)} if self.arguments else {}),
                **({"evidence": list(self.evidence)} if self.evidence else {}),
                **({"refused": self.refused} if self.refused else {}),
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

#: The contract when the candidates carry arguments, the caller checks the answer, or the agent
#: reads first: the same object, with the chosen action's arguments and the evidence it cites.
STRUCTURED_CONTRACT = """Answer with ONLY a JSON object:
{"action": "<one candidate key>", "confidence": 0.0, "reason": "<one sentence>", \
"question": "<one clarifying question, or empty>", \
"arguments": {<the chosen action's arguments, as its schema says>}, \
"evidence": ["<what you were shown that supports this: quote a log line, an id or a tool \
result>"]}"""

#: Said when the agent may read before it decides.
READ_NOTE = ("You may call the read tools offered to look for more evidence before you answer "
             "(at most {turns} model turns in all); then answer.")

#: Where the caller's own brief for this decision begins in the system prompt.
BRIEF_HEADING = "THIS DECISION (from the caller, who carries out what you choose):"

#: Said on the last turn of a read phase that is still reading.
ANSWER_NOW = "Stop investigating and answer now with ONLY the JSON object."


def _candidates_block(candidates: Sequence[Candidate]) -> str:
    rows = []
    for c in candidates:
        tail = "" if c.available else f"  [NOT AVAILABLE HERE: {c.why_not or 'not enabled'}]"
        rows.append(f"- {c.key}: {c.means}{tail}")
        if c.arguments:
            rows.append("  arguments: " + json.dumps(dict(c.arguments), default=str))
    return "\n".join(rows)


def system_prompt(agent: str, *, structured: bool = False, brief: str = "",
                  read_turns: int = 0) -> str:
    """The decision's system prompt: its rules, its contract, and the caller's brief."""
    body = SYSTEM_PROMPT.format(agent=agent)
    if structured:
        body = body[:body.rindex("Answer with ONLY a JSON object:")] + STRUCTURED_CONTRACT
    if read_turns:
        body += "\n\n" + READ_NOTE.format(turns=read_turns)
    if brief.strip():
        body += "\n\n" + BRIEF_HEADING + "\n" + brief.strip()
    return body


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
           hints: Sequence[str] = (), context_limit: int = 9000) -> str:
    parts = [f"SITUATION: {situation}"]
    said = _history_block(history)
    if said:
        parts.append("RECENT CONVERSATION (oldest first):\n" + said)
    parts.append("THE REQUEST:\n<<<\n" + (message or "").strip()[:6000] + "\n>>>")
    if context:
        parts.append("CONTEXT (what BOSS can see):\n" + _context_block(context, context_limit))
    if hints:
        parts.append(_hints_block(hints))
    parts.append("CANDIDATE ACTIONS:\n" + _candidates_block(candidates))
    return "\n\n".join(parts)


def _hints_block(hints: Sequence[str]) -> str:
    return ("BOSS'S HINTS (pattern guesses, not decisions):\n"
            + "\n".join(f"- {h}" for h in hints))


def shown(*, context: Mapping[str, Any] | None, hints: Sequence[str] = (),
          context_limit: int = 9000) -> str:
    """What the agent was shown as evidence — the context and BOSS's hints, as rendered: the
    start of the corpus a caller's `check` cites against (a read phase adds its results)."""
    parts = [_context_block(context, context_limit)] if context else []
    if hints:
        parts.append(_hints_block(hints))
    return "\n".join(parts)


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
    # A brace inside a string ("use ${VAR}") unbalances the scan: the whole outer object, from
    # the first brace to the last, is still the answer when it parses.
    first, last = body.find("{"), body.rfind("}")
    if 0 <= first < last:
        try:
            value = json.loads(body[first:last + 1])
        except ValueError:
            return None
        if isinstance(value, dict) and value.get("action"):
            return value
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


def _ask_structured_jev(decider: Any, adapter: Any, system: str, prompt: str,
                        candidates: Sequence[Candidate]) -> dict:
    """Jev picks the CHOICE (BOSSAPP-579); the routed model fills only that action's
    arguments. A structured decision is still one decision — Jev answers a choice among
    fixed options, never free-form arguments, so the routed model is asked for those alone,
    narrowed to the schema of the action Jev already chose."""
    from .typed_decisions import Question

    question = Question(
        key="action",
        instructions="Which candidate action does the situation call for?",
        options={c.key: c.means + ("" if c.available else f" (not available: {c.why_not})")
                 for c in candidates})
    batch = decider.decide(system + "\n\n" + prompt, [question])
    by = {"backend": getattr(batch, "backend", ""), "model": getattr(batch, "model", "")}
    cost = int(getattr(batch, "cost_usd_micros", 0) or 0)
    got = batch.decisions.get("action")
    if got is None or got.answer is None:
        return {"answer": None, "refused": (got.error if got else "no answer"),
                "cost": cost, "by": by, "turns": 1}
    chosen = next((c for c in candidates if c.key == got.answer), None)
    if chosen is None:
        return {"answer": None,
                "refused": f"'{got.answer}' is not one of the candidate actions",
                "cost": cost, "by": by, "turns": 1}
    confidence, reason = got.confidence if got.confidence is not None else 0.0, got.reason or ""
    schema = dict(chosen.arguments or {})
    if not dict(schema.get("properties") or {}):
        answer = {"action": chosen.key, "confidence": confidence, "reason": reason,
                  "arguments": {}, "evidence": []}
        return {"answer": answer, "cost": cost, "by": by, "turns": 1}
    arg_prompt = prompt + (f"\n\nThe action has been chosen: '{chosen.key}'. Answer ONLY with "
                           "the JSON object asked for, filling that action's arguments.")
    arg_got = _ask_model(adapter, system, arg_prompt)
    cost += int(arg_got.get("cost") or 0)
    raw = arg_got.get("raw") if isinstance(arg_got.get("raw"), Mapping) else {}
    args, why = _arguments(chosen, raw)
    if why:
        return {"answer": None, "refused": why, "cost": cost, "by": by, "turns": 2}
    answer = {"action": chosen.key, "confidence": confidence, "reason": reason,
             "arguments": args, "evidence": _cited(raw.get("evidence"))}
    return {"answer": answer, "cost": cost, "by": by, "turns": 2}


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


def _provider_name(adapter: Any) -> str:
    """The engine `project_chat_adapter` routed to (model_routing.project_chat_adapter stamps
    `_boss_choice` on every adapter it returns), or "" when none was resolved — the exception
    happened before routing picked one."""
    choice = getattr(adapter, "_boss_choice", None)
    return str(getattr(choice, "engine", "") or "").strip()


def _through(envelope: SpendEnvelope, system: str, prompt: str) -> tuple[str, dict]:
    """One call inside the caller's envelope: the pipeline's own answer (stream_answer), pinned,
    admitted, leased and settled by the envelope."""
    from ..agent.engine_agent import stream_answer

    text_out, usage = "", {}
    for kind, value in envelope.run(
            lambda pinned: stream_answer(pinned, prompt=prompt, system_prompt=system),
            prompt=prompt, system_prompt=system):
        if kind == "done":
            text_out, usage = value
    return str(text_out or ""), (usage if isinstance(usage, Mapping) else {})


def _ask_model(adapter: Any, system: str, prompt: str,
               envelope: Optional[SpendEnvelope] = None) -> dict:
    if envelope is not None:
        text_out, usage = _through(envelope, system, prompt)
    else:
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


# ── A structured decision: arguments, a caller's check, a read phase ──────────────────────────

_NUMBER = ("integer", "number")


def _shaped(kind: str, value: Any) -> tuple[bool, Any]:
    """(fits, the value as that type) for one argument's declared JSON type."""
    if kind in _NUMBER:
        if isinstance(value, bool):
            return False, value
        if isinstance(value, (int, float)):
            return True, int(value) if kind == "integer" and float(value).is_integer() else value
        try:
            number = float(str(value).strip())
        except ValueError:
            return False, value
        return True, int(number) if kind == "integer" and number.is_integer() else number
    if kind == "string":
        if isinstance(value, str):
            return True, value
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return True, str(value)
        return False, value
    if kind == "array":
        return isinstance(value, list), value
    if kind == "object":
        return isinstance(value, Mapping), (dict(value) if isinstance(value, Mapping) else value)
    if kind == "boolean":
        return isinstance(value, bool), value
    return True, value


def _arguments(chosen: Candidate, raw: Mapping[str, Any]) -> tuple[dict, str]:
    """The chosen action's arguments from an answer, checked against its schema. A model that
    put them beside `action` instead of under `arguments` has still given them."""
    given = raw.get("arguments") if isinstance(raw.get("arguments"), Mapping) else {}
    schema = dict(chosen.arguments or {})
    props = dict(schema.get("properties") or {})
    if not props:
        return dict(given), ""
    args = {k: raw[k] for k in props if raw.get(k) not in (None, "")}
    args.update({k: v for k, v in given.items() if k in props and v is not None})
    for name in schema.get("required") or ():
        if args.get(name) in (None, "", [], {}):
            return {}, f"'{chosen.key}' needs \"{name}\": {props.get(name, {}).get('description') or 'see its arguments'}"
    for name, value in list(args.items()):
        kind = str((props.get(name) or {}).get("type") or "")
        fits, shaped = _shaped(kind, value)
        if not fits:
            return {}, f"argument \"{name}\" of '{chosen.key}' must be a JSON {kind}"
        args[name] = shaped
    return args, ""


def _cited(value: Any) -> list:
    if isinstance(value, list):
        return list(value)[:12]
    return [value] if value not in (None, "") else []


def _accept(raw: Mapping[str, Any], candidates: Sequence[Candidate],
            check: Optional[Callable[[dict, str], str]], corpus: str) -> tuple[Optional[dict], str]:
    """(the answer, "") or (None, why it is refused): one of the candidates, its arguments as
    its schema says, and — when the caller gave one — passing the caller's check against what
    the agent was shown."""
    action = str(raw.get("action") or "").strip()
    chosen = next((c for c in candidates if c.key == action), None) or next(
        (c for c in candidates if c.key.lower() == action.lower()), None)
    if chosen is None:
        return None, (f"'{action}' is not one of the candidate actions "
                      f"({', '.join(c.key for c in candidates)})")
    args, why = _arguments(chosen, raw)
    if why:
        return None, why
    answer = {**dict(raw), "action": chosen.key, "arguments": args}
    if check is not None:
        try:
            why = str(check(answer, corpus) or "")
        except Exception as exc:  # noqa: BLE001 — a check that breaks refuses, never accepts
            why = f"the answer could not be checked ({type(exc).__name__})"
        if why:
            return None, why[:600]
    return answer, ""


def _refused_again(why: str) -> str:
    return f"Refused: {why}. Answer again with ONLY the JSON object."


def _ask_structured(adapter: Any, system: str, prompt: str, *, candidates: Sequence[Candidate],
                    check: Optional[Callable[[dict, str], str]], corpus: str,
                    envelope: Optional[SpendEnvelope]) -> dict:
    """One answer, re-asked ONCE when it is refused — except under an envelope, which admits
    one attempt and so one answer."""
    asks = 1 if envelope is not None else 2
    cost, by, why, asked = 0, {}, "", prompt
    for n in range(asks):
        got = _ask_model(adapter, system, asked, envelope)
        cost += int(got.get("cost") or 0)
        by = dict(got.get("by") or {})
        raw = got.get("raw")
        if raw is None:
            why = "not the JSON object asked for"
        else:
            answer, why = _accept(raw, candidates, check, corpus)
            if answer is not None:
                return {"answer": answer, "cost": cost, "by": by, "turns": n + 1}
        asked = prompt + "\n\n" + _refused_again(why)
    return {"answer": None, "refused": why, "cost": cost, "by": by, "turns": asks}


def _ask_reading(adapter: Any, system: str, prompt: str, *, read: ReadPhase,
                 candidates: Sequence[Candidate], check: Optional[Callable[[dict, str], str]],
                 corpus: str) -> dict:
    """THE READ PHASE, then the answer: the model may call the read tools it is offered for up
    to `read.max_turns` turns and `read.max_usd_micros`, the last turn without tools; an answer
    refused is re-asked once, in the same conversation. Tool results join the corpus `check`
    cites against. Never raises: a model that fails is a result."""
    from .redaction import redact_secrets
    from .workflow_precheck_agent import _turn_cost, _who_answered, deferred_wait

    who = _who_answered(adapter)
    seen = [corpus]
    messages: list[dict[str, Any]] = [{"role": "system", "content": system},
                                      {"role": "user", "content": prompt}]
    tools = [dict(t) for t in (read.tools or ())]
    can_tool = bool(tools) and read.run is not None
    spent, turns, asked_again, why, model_seen = 0, 0, 0, "", ""
    while turns < read.max_turns:
        # The LAST turn is always an answer without tools: the turn cap, or the spend cap
        # reached by the reads. A read phase that spent its budget used to end with no answer
        # at all (boss-dogfood 2026-11-07: a squad lead read, ran past $0.15, and "passed").
        final = turns == read.max_turns - 1 or spent >= read.max_usd_micros
        if final and messages[-1].get("role") == "tool":
            messages.append({"role": "user", "content": ANSWER_NOW})
        try:
            message, usage = adapter.create_chat_completion(
                messages, tools=(tools if (can_tool and not final) else None))
        except Exception as exc:  # noqa: BLE001 — a model failing is a result, not a crash
            # Never waited for here (a caller may be inside a runner's tick): a turn the
            # per-minute budget defers is said as such, and nothing is decided.
            deferred = deferred_wait(exc) is not None
            return {"answer": None, "error": ("the model budget deferred the turn: "
                                              if deferred else "")
                    + f"{type(exc).__name__}: {exc}"[:300], "cost": spent, "turns": turns,
                    "by": who}
        turns += 1
        spent += _turn_cost(usage)
        if isinstance(usage, Mapping) and usage.get("model"):
            model_seen = str(usage["model"])
        message = message if isinstance(message, Mapping) else {"content": str(message or "")}
        messages.append(message.get("raw") or {"role": "assistant",
                                               "content": message.get("content") or ""})
        calls = message.get("tool_calls") or []
        if calls and can_tool and not final:
            for call in calls:
                try:
                    args = json.loads(call.get("arguments") or "{}")
                except (TypeError, ValueError):
                    args = {}
                try:
                    result = read.run(str(call.get("name") or ""),
                                      args if isinstance(args, dict) else {})
                except Exception as exc:  # noqa: BLE001
                    result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:300]}
                body = clip_for_model(redact_secrets(json.dumps(result, default=str)),
                                      read.result_chars)
                seen.append(body)
                messages.append({"role": "tool", "tool_call_id": call.get("id"),
                                 "content": body})
            continue
        raw = parse(str(message.get("content") or ""))
        if raw is None:
            why = "not the JSON object asked for"
        else:
            answer, why = _accept(raw, candidates, check, "\n".join(seen))
            if answer is not None:
                return {"answer": answer, "cost": spent, "turns": turns,
                        "by": {**who, "model": model_seen or who.get("model") or ""}}
        if asked_again >= 1:
            break
        asked_again += 1
        messages.append({"role": "user", "content": _refused_again(why)})
    return {"answer": None, "refused": why or "no answer within the turn cap", "cost": spent,
            "turns": turns, "by": who}


def _ledger_sink(connection: Any) -> Any:
    """The run-ledger sink for a kernel decision run: DbSink on the caller's engine while
    BOSS_LEDGER_EVENTS is on, else None. Never raises: the ledger never stops a decision."""
    try:
        from ..kernel import ledger

        engine = getattr(connection, "engine", None)
        if connection is None or engine is None or not ledger.enabled():
            return None
        return ledger.DbSink(engine)
    except Exception:  # noqa: BLE001 — a decision without its ledger is still a decision
        return None


def _ask_reading_on_kernel(adapter: Any, system: str, prompt: str, *, read: ReadPhase,
                           candidates: Sequence[Candidate],
                           check: Optional[Callable[[dict, str], str]], corpus: str,
                           raw_adapter: Any = None, project_id: str = "",
                           sink: Any = None) -> dict:
    """`_ask_reading` ON THE HARNESS KERNEL (K2, BOSS_KERNEL_DECISIONS): the same read phase,
    caps and spend rule (presets.decision), the same acceptance (`_accept` against the corpus
    plus every read result, re-asked once), run by `kernel.run` — held equal to the old loop by
    test_kernel_conformance.py, except §8.4's intended differences (a transient provider error
    is retried; a malformed read call is refused before the tool runs). Returns _ask_reading's
    dict shape; never raises: a model that fails is a result.

    Governance: when `decide` routed the model itself, the kernel's GovernedEngine reserves,
    receipts and settles each call through the SAME project gate `_routed` built
    (GovernedChatAdapter.reserve_turn/settle_turn, kernel/governance.AdapterGate); a caller's
    own adapter keeps whatever governance it came with, as today."""
    from .. import kernel
    from ..kernel.governance import AdapterGate
    from ..kernel.durable import store_for
    from ..kernel.presets import decision as decision_preset
    from .workflow_precheck_agent import _who_answered

    raw = raw_adapter if raw_adapter is not None else adapter
    gate = (AdapterGate(adapter)
            if adapter is not raw and callable(getattr(adapter, "reserve_turn", None)) else None)
    engine = kernel.OpenAIChatEngine(raw if gate is not None else adapter)
    who = _who_answered(adapter)

    def accept(answered: Any, state: Any) -> Any:
        answer, why = _accept(answered, candidates, check,
                              "\n".join([corpus] + list(state.rendered)))
        return kernel.Acceptance(ok=answer is not None, why=why, structured=answer)

    can_tool = bool(read.tools) and read.run is not None
    tools = kernel.ReadOnlyToolSet(
        list(read.tools or ()) if can_tool else [],
        read.run or (lambda name, args: {"ok": False, "error": "no read tools offered"}))
    result = kernel.run(kernel.RunSpec(
        engine=engine, tools=tools,
        policies=decision_preset(max_turns=read.max_turns, max_usd_micros=read.max_usd_micros,
                                 result_chars=read.result_chars, accept=accept),
        project_id=project_id or "", system=system,
        messages=[{"role": "user", "content": prompt}],
        actor="agent_decision", stage=ROUTE_STEP, gate=gate, sink=sink,
        # A suspension lives in Postgres, never only in this process (AP-P2).
        store=store_for(getattr(sink, "engine", None))))
    cost, turns = int(result.usage.cost_usd_micros or 0), int(result.steps or 0)
    if result.status == "completed" and isinstance(result.structured, Mapping):
        return {"answer": dict(result.structured), "cost": cost, "turns": turns,
                "by": {**who, "model": result.usage.model or who.get("model") or ""}}
    if result.status == "completed":
        # The closing turn asked for tools it was not offered instead of answering.
        return {"answer": None, "refused": result.error or "not the JSON object asked for",
                "cost": cost, "turns": turns, "by": who}
    if result.stopped in ("refused", "steps", "cost"):
        return {"answer": None, "refused": result.error or "no answer within the turn cap",
                "cost": cost, "turns": turns, "by": who}
    # A model failure is a result, in _ask_reading's words: a budget deferral is said as such
    # (never waited for here — a caller may be inside a runner's tick), and nothing is decided.
    deferred = result.stopped == "budget_wait_limit"
    return {"answer": None,
            "error": (("the model budget deferred the turn: " if deferred else "")
                      + (result.error or result.stopped or "the model gave no answer")[:300]),
            "cost": cost, "turns": turns, "by": who}


# ── The seam ──────────────────────────────────────────────────────────────────────────────────

def decide(connection: Any, settings: Any, project_id: str, *, situation: str, message: str,
           candidates: Sequence[Candidate], history: Sequence[Mapping[str, Any]] = (),
           context: Mapping[str, Any] | None = None, hints: Sequence[str] = (),
           agent: str = "BOSS", ref: str = "", min_confidence: Optional[float] = None,
           adapter: Any = None, decider: Any = None, brief: str = "",
           check: Optional[Callable[[dict, str], str]] = None,
           read: Optional[ReadPhase] = None, envelope: Optional[SpendEnvelope] = None,
           context_limit: int = 9000) -> AgentDecision:
    """One decision by a model, checked against a confidence threshold. Never raises — except
    that a caller's `envelope` raises its own failures, for its owner to settle.

    `adapter` (a chat adapter) or `decider` (a typed-decision backend) override the backend —
    the seams tests and the battle-test runner use, and how a caller hands over the model its
    own route chose. With neither, Jev when ready, else the project's routed model. With no
    model at all: status `no_model`, and no action.

    THE STRUCTURED DECISION (BOSSAPP-759), for a caller whose decision is more than a choice:
      - `Candidate.arguments`: the chosen action comes back with its arguments, validated;
      - `check(answer, corpus) -> refusal`: the caller's own check of the answer against what
        the agent was shown (`corpus`: the context, the hints and every read result) — a
        refused answer is asked once more, with the refusal;
      - `read`: a bounded read phase before the answer (ReadPhase);
      - `brief`: the caller's own rules for this decision, after the decision's;
      - `envelope`: the call runs inside the caller's spend envelope (one attempt).
    A structured decision is the routed chat model's (Jev answers a choice, not arguments).
    Refused twice: status `no_model` with `refused` saying why — nothing was decided.
    """
    keys = {c.key for c in candidates}
    if not keys:
        return AgentDecision(NO_MODEL, error="no candidates")
    if read is not None and envelope is not None:
        raise ValueError("a read phase takes several governed turns; an envelope admits one")
    structured = bool(check is not None or read is not None
                      or any(c.arguments for c in candidates))
    bar = min_confidence if min_confidence is not None else threshold(connection, settings,
                                                                     project_id)
    prompt = render(situation=situation, message=message, history=history, context=context,
                    candidates=candidates, hints=hints, context_limit=context_limit)
    system = system_prompt(agent, structured=structured, brief=brief,
                           read_turns=read.max_turns if read is not None else 0)
    corpus = shown(context=context, hints=hints, context_limit=context_limit)
    try:
        if structured and envelope is None and read is None and (
                decider is not None or _jev_ready(settings)):
            if decider is None:
                from .typed_decisions import TypeSafeDecider

                decider = TypeSafeDecider(settings)
            if adapter is None:
                adapter, _raw = _routed(connection, settings, project_id, ref, message)
            got = _ask_structured_jev(decider, adapter, system, prompt, candidates)
        elif not structured and envelope is None and (
                decider is not None or (adapter is None and _jev_ready(settings))):
            if decider is None:
                from .typed_decisions import TypeSafeDecider

                decider = TypeSafeDecider(settings)
            got = _ask_jev(decider, system + "\n\n" + prompt, candidates)
        else:
            raw = adapter
            if adapter is None and envelope is None:
                adapter, raw = _routed(connection, settings, project_id, ref, message)
            if read is not None:
                if kernel_decisions():
                    got = _ask_reading_on_kernel(
                        adapter, system, prompt, read=read, candidates=candidates, check=check,
                        corpus=corpus, raw_adapter=raw, project_id=project_id,
                        sink=_ledger_sink(connection))
                else:
                    got = _ask_reading(adapter, system, prompt, read=read,
                                       candidates=candidates, check=check, corpus=corpus)
            elif structured:
                got = _ask_structured(adapter, system, prompt, candidates=candidates,
                                      check=check, corpus=corpus, envelope=envelope)
            else:
                got = _ask_model(adapter, system, prompt, envelope)
                if got["raw"] is None and envelope is None:
                    # Asked once more, plainly: a model that wrapped its answer in prose has
                    # still answered; one that did not gets one chance to.
                    again = _ask_model(adapter, system, prompt + "\n\nAnswer with ONLY the "
                                                                  "JSON object.")
                    again["cost"] += got["cost"]
                    got = again
    except Exception as exc:  # noqa: BLE001 — no model is an answer, never a crash
        if envelope is not None:
            raise
        log.info("agent decision had no model: %s", exc)
        return AgentDecision(NO_MODEL, error=f"{type(exc).__name__}: {exc}"[:300],
                             provider=_provider_name(adapter))

    if structured:
        return _structured_result(got, candidates, bar, adapter)
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


def _structured_result(got: Mapping[str, Any], candidates: Sequence[Candidate], bar: float,
                       adapter: Any) -> AgentDecision:
    by, cost = dict(got.get("by") or {}), int(got.get("cost") or 0)
    turns = int(got.get("turns") or 0)
    answer = got.get("answer")
    if got.get("error"):
        return AgentDecision(NO_MODEL, by=by, cost_usd_micros=cost, turns=turns,
                             error=str(got["error"])[:300], provider=_provider_name(adapter))
    if not isinstance(answer, Mapping):
        return AgentDecision(NO_MODEL, by=by, cost_usd_micros=cost, turns=turns,
                             refused=str(got.get("refused") or "no answer")[:600])
    action = str(answer["action"])
    chosen = next(c for c in candidates if c.key == action)
    confidence = _confidence(answer.get("confidence"))
    reason = str(answer.get("reason") or answer.get("reasoning") or "")[:500]
    said = dict(action=action, confidence=confidence, reason=reason, by=by,
                cost_usd_micros=cost, turns=turns, arguments=dict(answer.get("arguments") or {}),
                evidence=_cited(answer.get("evidence")))
    if confidence < bar and chosen.acts:
        return AgentDecision(CLARIFY, question=_question_for(
            candidates, action, str(answer.get("question") or "")), **said)
    return AgentDecision(DECIDED, question=str(answer.get("question") or "")[:600], **said)


__all__ = ["AgentDecision", "Candidate", "CLARIFY", "DECIDED", "DEFAULT_THRESHOLD", "NO_MODEL",
           "NO_MODEL_REPLY", "ReadPhase", "SpendEnvelope", "decide", "parse", "render",
           "shown", "system_prompt", "threshold"]
