"""An "Ask about this" turn answers FIRST, in one model call, from the thing's own record.

THE INCIDENTS (boss-1, 2026-10-02).
-----------------------------------
* "Ask the Qa" on a failed Preview step whose record said ImportError: 276 s, then an answer
  about something else.
* After the A-E fixes (1547f72), "Ask the Qa" on a failed Preview step whose record said
  `Could not resolve entry module "index.html"` (workflow_step:wfr_475bf68eb18f485b8c52:preview):
  347 s, and the reply opened "Budget spent; answering from evidence gathered…".

The owner: "these things should be quick to identify the issue … so pathetic and time
consuming … it really times out after spending 240s there? very annoying."

Both answers were in the record the turn already held. The time went on the multi-step operate
loop: every round-trip a `claude -p` call carrying ~48 tool schemas, a ~250-line operate persona
and Claude Code's own ~34k-token system prompt, each allowed up to 240 s, chained four to
fourteen times — to look up what the subject block had already handed it.

THE DESIGN, FOR THE WHOLE CLASS.
--------------------------------
A turn about one record is answered by ONE model call that gets the resolved record
(turn_subject: status, error, outputs, log tail, launch command, linked runs) and the operator's
literal question under a short answer-from-the-evidence prompt — no tools, no persona, no tool
schemas, and on the Claude CLI no Claude Code system prompt. It must state the cause, the
concrete fix, and what it could not confirm.

It has a hard wall-clock budget (`chat_subject_answer_seconds`, 45 s by default) enforced twice:
the adapter's own timeout is cut to it (call_deadline), and the call runs on a worker that is
abandoned at the budget whatever the adapter does. When the budget is hit, or the model fails,
the operator still gets an answer: what the record itself says, the checker's suggestion marked
as unverified, and the list of what was not checked — never "still working".

The long operate loop runs only when the operator asks for it — the dock's "Investigate
further" and "Do the fix" (`depth="investigate"` on the wire) — never automatically.

Pinned by engine/tests/test_an_ask_about_this_answers_first_in_one_call.py.
"""

from __future__ import annotations

import contextvars
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from .turn_subject import SubjectEvidence

_log = logging.getLogger(__name__)

#: The first answer's wall-clock budget. The owner's target: first answer under 60 s, typical
#: under 30 s. Overridable per install with `chat_subject_answer_seconds`.
DEFAULT_ANSWER_SECONDS = 45.0

#: What a caller sends to skip the first answer and run the investigating loop.
INVESTIGATE = "investigate"

#: The next actions the dock offers under a first answer. Each is a turn the operator starts with
#: one press; both run the full loop, which is the only path that reads beyond the record or acts.
NEXT_STEPS: tuple[dict[str, str], ...] = (
    {"id": "investigate", "label": "Investigate further", "depth": INVESTIGATE,
     "message": "Investigate further: check what that answer could not confirm, with the tools, "
                "and tell me if the cause or the fix changes."},
    {"id": "do_fix", "label": "Do the fix", "depth": INVESTIGATE,
     "message": "Do the fix you recommended."},
)

#: How much of the conversation before this question the first call sees. A follow-up ("and
#: why port_timeout?") needs the previous answer; a whole thread is what made calls slow.
_HISTORY_MESSAGES = 4
_HISTORY_CHARS = 1500


@dataclass
class QuickAnswer:
    reply: str
    seconds: float
    #: True when the model wrote the reply; False when it is the record-only answer.
    by_model: bool
    #: Why the model's answer is missing, when it is.
    reason: str = ""
    usage: dict[str, Any] = field(default_factory=dict)

    def next_steps(self) -> list[dict[str, str]]:
        return [dict(step) for step in NEXT_STEPS]


def answer_seconds(settings: Any) -> float:
    try:
        value = float(getattr(settings, "chat_subject_answer_seconds", 0) or 0)
    except (TypeError, ValueError):
        value = 0.0
    return value if value > 0 else DEFAULT_ANSWER_SECONDS


def wants_first_answer(message: str, evidence: Optional[SubjectEvidence],
                       depth: Optional[str], *, has_images: bool = False) -> bool:
    """Is this turn answered by the one-call first answer?

    Only a QUESTION about a record BOSS found. An instruction ("retry it", "do the fix") needs
    the loop — it acts — and so does an explicit "Investigate further". A screenshot goes to the
    loop too: the first call is text, and an image read as text is an empty question.
    """
    from .intent_router import asked_only_to_look

    if depth == INVESTIGATE or has_images:
        return False
    if evidence is None or not evidence.found:
        return False
    if asks_for_a_live_read(message or ""):
        return False
    return asked_only_to_look(message or "")


#: A question only a live look can answer: the app's logs, what is running on a host now. The
#: record holds what BOSS intended and last saw; "show me the container logs" is about the
#: machine, so it goes to the loop, which has read_app_logs and inspect_host.
_LIVE_READ = re.compile(
    r"\b(logs?|docker|container|journalctl|tail|stdout|stderr|running (on|now)|right now|"
    r"currently|disk|df -h|memory|cpu|process(es)?)\b", re.IGNORECASE)


def asks_for_a_live_read(message: str) -> bool:
    """Does answering this need a look at the machine rather than the record?

    Harbor was asked for HelpDesk's last 40 log lines. The turn took the one-call path, which
    has no tools by design, and the reply said "I have no tool to run docker logs" — true of
    that call, false of BOSS. A question about the live machine is answered by the loop."""
    return bool(_LIVE_READ.search(message or ""))


_SYSTEM = (
    "You are BOSS, answering an operator's question about one record in their software "
    "factory{role}. Everything known about it is in the RECORD in the next message. You have "
    "no tools and nothing else will be looked up for this answer, so answer from the record.\n"
    "\n"
    "Reply in plain text, under 180 words, in exactly this shape:\n"
    "Cause: what actually went wrong, in one or two sentences, quoting the line of the record "
    "that shows it (the error text, a log line, a file path).\n"
    "Fix: the concrete change that resolves it — name the file, command, setting or step to "
    "change, and how. If two fixes are plausible, give the more likely one first.\n"
    "Not confirmed: what the record does not show that would confirm the cause or the fix, one "
    "short line each — or \"nothing; the record settles it\".\n"
    "\n"
    "Rules: a checker's label or suggested_action is not the cause unless the error text "
    "agrees with it. Never invent a file, line or value the record does not contain. Do not ask "
    "the operator anything and do not offer to investigate — the console offers that next. "
    "Never say BOSS lacks a tool or cannot reach something: this answer reads only the "
    "record, and \"Investigate further\" checks the live system (logs, hosts, containers). "
    "If the question needs that, say what the record shows and that Investigate further "
    "fetches the rest."
)


def first_answer_prompt(evidence: SubjectEvidence, question: str,
                        history: Optional[list[dict]] = None,
                        role: Optional[str] = None) -> tuple[str, str]:
    """(system, user) for the one call. The record and the literal question; nothing else."""
    system = _SYSTEM.format(role=f", as its {role}" if role else "")
    parts = []
    recent = [m for m in (history or []) if isinstance(m, dict)
              and m.get("role") in ("user", "assistant") and m.get("content")]
    if recent:
        lines = []
        for m in recent[-_HISTORY_MESSAGES:]:
            content = m["content"] if isinstance(m["content"], str) else str(m["content"])
            lines.append(f"{m['role'].upper()}: {content[:_HISTORY_CHARS]}")
        parts.append("EARLIER IN THIS CONVERSATION:\n" + "\n".join(lines))
    parts.append("RECORD:\n" + evidence.render(question))
    parts.append(f'Answer the operator\'s question: "{question.strip()}"')
    return system, "\n\n".join(parts)


def _ask_model(adapter: Any, system: str, prompt: str) -> tuple[str, dict]:
    """One completion, no tools, on the lightest path the adapter has."""
    plain = getattr(adapter, "create_plain_answer", None)
    if callable(plain):
        return plain(system, prompt)
    msg, usage = adapter.create_chat_completion(
        [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
        tools=None)
    return str(msg.get("content") or ""), usage or {}


def record_only_answer(evidence: SubjectEvidence, reason: str,
                       seconds: Optional[float] = None, *, timed_out: bool = False) -> str:
    """The best answer BOSS can give without a model: what the record itself says.

    Used when the first call ran out of its budget or failed — so the operator always gets the
    cause as recorded, the checker's suggestion (labelled as unverified) and what was not
    checked, instead of a wait that ends in nothing.
    """
    lines = []
    lead = (f"The model did not answer within {int(seconds)}s"
            if timed_out and seconds is not None else f"The model could not answer ({reason})")
    lines.append(lead + ", so this is what the record of "
                 f"{evidence.ref.label} itself says.")
    if evidence.findings:
        lines.append("Cause (from the record): " + evidence.findings[0])
        lines.extend(evidence.findings[1:])
    if evidence.error:
        lines.append("Recorded error: " + evidence.error[:700])
    if not evidence.findings and not evidence.error:
        lines.append(f"Status: {evidence.status or 'unknown'} — the record holds no error text.")
    suggested = next((v for k, v in evidence.facts.items()
                      if k.startswith("suggested_action") and v), None)
    if suggested:
        lines.append(f"Fix the checker suggested (not verified against the error): {suggested}")
    if evidence.open_points:
        lines.append("Not checked: " + "; ".join(
            f"{point} ({tool})" for point, tool in evidence.open_points) + ".")
    lines.append("Press Investigate further to have BOSS check those and confirm the fix.")
    return "\n".join(lines)


def with_the_records_cause(reply: str, evidence: SubjectEvidence) -> str:
    """The reply, plus what the record shows when the reply did not use it.

    THE LABEL IS NOT THE CAUSE. The ImportError incident's first wrong answer repeated the
    checker's "ensure HOST/PORT are honored"; the loop re-asks such an answer, and a second call
    is exactly the time this path exists to save. So the record's own finding is appended
    instead — deterministically, at no cost — and the operator sees the real cause either way.
    """
    from .turn_subject import cites_the_record

    if not evidence.findings or cites_the_record(reply, evidence):
        return reply
    return reply.rstrip() + "\n\nWhat the record itself shows: " + evidence.findings[0]


def answer_first(adapter: Any, evidence: SubjectEvidence, question: str, *,
                 history: Optional[list[dict]] = None, role: Optional[str] = None,
                 seconds: float = DEFAULT_ANSWER_SECONDS) -> QuickAnswer:
    """Answer `question` about `evidence` in ONE call, inside `seconds`, whatever happens.

    Never raises and never takes materially longer than `seconds`: the call runs on a worker
    that is abandoned at the budget (its result discarded), and the adapter's own timeout is cut
    to the same budget so the abandoned worker's process ends with it.
    """
    from . import call_deadline

    started = time.monotonic()
    system, prompt = first_answer_prompt(evidence, question, history, role)
    box: dict[str, Any] = {}
    deadline = started + float(seconds)

    def work() -> None:
        try:
            with call_deadline.until(deadline):
                box["out"] = _ask_model(adapter, system, prompt)
        except BaseException as exc:  # noqa: BLE001 — reported, never raised into the turn
            box["error"] = exc

    # The worker inherits this context (call_deadline is a ContextVar) and then tightens it.
    ctx = contextvars.copy_context()
    worker = threading.Thread(target=ctx.run, args=(work,), daemon=True,
                              name="boss-first-answer")
    worker.start()
    # A second of grace for the adapter to raise its own TurnDeadlineExceeded cleanly.
    worker.join(max(0.0, deadline - time.monotonic()) + 1.0)
    elapsed = time.monotonic() - started

    reason, timed_out = "", False
    if worker.is_alive():
        reason, timed_out = f"its time ran out ({int(seconds)}s)", True
    elif "error" in box:
        exc = box["error"]
        if isinstance(exc, call_deadline.TurnDeadlineExceeded):
            reason, timed_out = f"its time ran out ({int(seconds)}s)", True
        else:
            reason = f"{type(exc).__name__}: {str(exc)[:200]}"
    else:
        text, usage = box.get("out") or ("", {})
        text = str(text or "").strip()
        if text:
            return QuickAnswer(reply=with_the_records_cause(text, evidence), seconds=elapsed,
                               by_model=True, usage=usage or {})
        reason = "it returned an empty answer"
    _log.warning("first answer for %s fell back to the record: %s", evidence.ref.label, reason)
    return QuickAnswer(reply=record_only_answer(evidence, reason, seconds, timed_out=timed_out),
                       seconds=elapsed,
                       by_model=False, reason=reason)
