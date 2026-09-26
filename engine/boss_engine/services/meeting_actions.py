"""Turning a meeting transcript into decisions and action items.

THE SAME SHAPE AS THE VIDEO LANE, AND FOR THE SAME REASON. A model reads the prose and writes
a structured list; BOSS validates the list; a separate step performs the effect. The model
decides WHAT was agreed. It does not get to create anything.

Splitting extraction from ticket creation is what makes the approval useful: the person
approving sees the actions as sentences, with who each is for and what was said to justify it,
and approves that — rather than approving "run a step" and finding out afterwards what landed
in the tracker.
"""
from __future__ import annotations

import json
import re
from typing import Any, Optional

MAX_ACTIONS = 40
MAX_DECISIONS = 40


class ExtractionError(RuntimeError):
    """The transcript could not be turned into actions, in terms of what to do about it."""


def build_prompt(*, transcript: str, context: str = "") -> tuple[str, str]:
    """System and user prompts, separated from the call so the wording is testable."""
    system = f"""\
You read a meeting transcript and write down what was DECIDED and what somebody has to DO.

You output JSON and nothing else. No prose before or after, no markdown fences.

OUTPUT SHAPE
{{
  "decisions": [
    {{"what": "the decision, in one sentence", "why": "the reason given, if any"}}
  ],
  "actions": [
    {{"title": "a short imperative — 'Send the revised quote to Acme'",
      "detail": "what was actually said about it, enough for somebody who missed the meeting",
      "owner": "the name as spoken, or null if nobody was named",
      "due": "what was said about timing, verbatim, or null"}}
  ],
  "unclear": ["anything discussed that you could not resolve into either list"]
}}

RULES
  - An action is something a PERSON must do. "We should think about pricing" is not an action;
    "Ravi to draft three pricing options by Friday" is.
  - Do not invent an owner. If nobody was named, owner is null — a ticket assigned to the wrong
    person is worse than one assigned to nobody.
  - Do not invent a due date. Put what was actually said in `due`, in their words.
  - Quote or closely paraphrase in `detail`. The person reading the ticket was not there.
  - If the transcript is chatter with no decisions and no actions, return empty lists. Inventing
    work from a conversation that produced none is the worst outcome here.
  - At most {MAX_ACTIONS} actions. A meeting with more than that was not one meeting.

{context.strip()}"""
    return system, f"TRANSCRIPT:\n{transcript.strip()}"


def parse(raw: str) -> dict[str, Any]:
    """Get the JSON out, however the model wrapped it.

    Same tolerance as the video planner, for the same reason: reliability should not be a
    function of whether a model felt like adding a sentence first. What is NOT relaxed is the
    content — everything below is still checked.
    """
    text = str(raw or "").strip()
    if not text:
        raise ExtractionError("the model returned nothing.")
    fenced = re.search(r"```(?:json)?\s*(.+?)```", text, re.S)
    if fenced:
        text = fenced.group(1).strip()
    try:
        body = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            raise ExtractionError(
                f"the model did not return decisions and actions. It said: "
                f"{str(raw)[:300]}") from None
        try:
            body = json.loads(text[start:end + 1])
        except json.JSONDecodeError as error:
            raise ExtractionError(f"the model's answer was not valid JSON ({error}).") from None
    if not isinstance(body, dict):
        raise ExtractionError("the model's answer was not an object.")
    return body


def validate(body: dict[str, Any]) -> dict[str, Any]:
    """Normalise what came back, refusing anything that would make a bad ticket.

    An EMPTY RESULT IS VALID. A stand-up that decided nothing produced nothing, and a model
    that invents two actions to look useful is the failure this guards against — so empty is
    passed through rather than treated as an error.
    """
    actions: list[dict[str, Any]] = []
    for index, raw in enumerate((body.get("actions") or [])[:MAX_ACTIONS]):
        if not isinstance(raw, dict):
            raise ExtractionError(f"actions[{index}] is not an object.")
        title = str(raw.get("title") or "").strip()
        if not title:
            raise ExtractionError(f"actions[{index}] has no title, so it cannot be a ticket.")
        actions.append({
            "title": title[:200],
            "detail": str(raw.get("detail") or "").strip()[:4000],
            # NULL, NOT A GUESS. A ticket assigned to the wrong person is worse than one
            # assigned to nobody, and "unassigned" is a state every tracker has.
            "owner": (str(raw.get("owner")).strip()[:120]
                      if raw.get("owner") not in (None, "", "null") else None),
            "due": (str(raw.get("due")).strip()[:200]
                    if raw.get("due") not in (None, "", "null") else None),
        })

    decisions = []
    for raw in (body.get("decisions") or [])[:MAX_DECISIONS]:
        if isinstance(raw, dict) and str(raw.get("what") or "").strip():
            decisions.append({"what": str(raw["what"]).strip()[:500],
                              "why": str(raw.get("why") or "").strip()[:500] or None})
    return {
        "decisions": decisions,
        "actions": actions,
        "unclear": [str(u)[:300] for u in (body.get("unclear") or [])][:20],
    }


def extract(adapter, *, transcript: str, context: str = "", attempts: int = 2) -> dict[str, Any]:
    """Ask, check, and ask once more with the reason if the answer was unusable."""
    if not str(transcript or "").strip():
        raise ExtractionError(
            "no transcript was given, so there is nothing to read. Paste the meeting notes or "
            "transcript as the workflow's input.")

    system, user = build_prompt(transcript=transcript, context=context)
    last_error = ""
    for attempt in range(max(1, attempts)):
        ask = user if not last_error else (
            f"{user}\n\nYour previous answer was rejected: {last_error}\n"
            f"Return corrected JSON in the same shape.")
        _ref, text_out, _usage = adapter.create_text_response(ask, system_prompt=system)
        try:
            result = validate(parse(text_out))
        except ExtractionError as error:
            last_error = str(error)
            continue
        result["attempts"] = attempt + 1
        result["summary"] = summarise(result)
        return result
    raise ExtractionError(
        f"could not read decisions and actions out of that transcript after {attempts} "
        f"attempts. The last problem was: {last_error}")


def summarise(result: dict[str, Any]) -> str:
    """What the person approving the tickets reads.

    They are deciding whether these become real tickets in a tracker other people use, and a
    JSON blob is not a thing anybody checks carefully at that moment.
    """
    lines: list[str] = []
    if result.get("decisions"):
        lines.append("Decided:")
        for decision in result["decisions"]:
            why = f" — {decision['why']}" if decision.get("why") else ""
            lines.append(f"  · {decision['what']}{why}")
        lines.append("")
    if result.get("actions"):
        lines.append(f"Tickets to create ({len(result['actions'])}):")
        for action in result["actions"]:
            who = action.get("owner") or "unassigned"
            when = f", {action['due']}" if action.get("due") else ""
            lines.append(f"  · {action['title']}  [{who}{when}]")
    else:
        lines.append("No action items — nothing will be created.")
    if result.get("unclear"):
        lines.append("")
        lines.append("Could not place:")
        lines += [f"  · {item}" for item in result["unclear"]]
    return "\n".join(lines)
