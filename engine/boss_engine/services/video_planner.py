"""Turning "cut the boring bit and make it vertical" into an edit plan BOSS can run.

WHERE THE JUDGEMENT LIVES, AND WHERE IT DOES NOT
------------------------------------------------
This is the only step of the media lane that needs a model. Fetching, editing and publishing
are mechanical; reading somebody's prose and deciding it means `cut 3:10-3:40` then `reframe
9:16` is not.

So the model writes a PLAN and never performs an edit. Everything it produces goes through
`video_edit.validate_plan`, which knows the operation vocabulary, checks every timecode and
refuses the whole plan if any part of it is wrong. The model chooses what to do; it cannot
widen what is possible, name a file, or reach anything outside the fixed set of operations.

TWO SOURCES OF INSTRUCTION, WHICH IS HOW PEOPLE ACTUALLY WORK
-------------------------------------------------------------
* HOUSE STYLE — standing rules that apply to every video: where the logo goes, how captions are
  styled, whether shorts are 9:16. Stored as an ordinary editable skill so it appears in the
  Skills page and changes without a deploy, exactly like the YouTube publishing standards.
* THIS VIDEO'S INSTRUCTIONS — what to do with this one, typed per run.

The per-run instructions win where the two disagree. A standing rule is a default, and somebody
writing a specific instruction is overriding the default on purpose.

AMBIGUITY IS GUESSED AT, NOT ASKED ABOUT
-----------------------------------------
A deliberate choice, and the reason it is safe: the workflow already has a human review step
before anything is published. Stopping to ask costs a round trip on every video; guessing costs
a render the reviewer rejects, and the reviewer was going to look anyway. What the model must
NOT do is guess silently — every operation carries a `why`, and anything it was unsure of goes
in `assumptions`, so the reviewer reads the reasoning rather than inferring it from the video.
"""
from __future__ import annotations

import json
import re
from typing import Any, Optional

from .video_edit import (
    ASPECTS,
    CAPTION_STYLES,
    PLAN_OPERATIONS,
    POSITIONS,
    VideoEditError,
    plan_timeline_warning,
    validate_plan,
)

#: The editable standing rules, as a skill key. Seeded once; the operator owns it after that.
HOUSE_STYLE_SKILL_KEY = "video_editing_standards"

DEFAULT_HOUSE_STYLE = """\
HOW THIS CHANNEL EDITS

These are standing rules applied to every video. The instructions given for a particular video
override anything here — this is the default, not the law.

Shape
* A "short" means 9:16, cropped from the middle. Never letterboxed.
* The long-form video keeps its original shape unless asked otherwise.

Captions
* Use the lower third. Keep them under about 60 characters so they fit on a phone.
* Caption the opening line of a short. A long video only gets captions where asked.

Cuts
* Remove dead air and false starts when asked to "tidy" or "clean up".
* Never cut more than asked. If an instruction is vague about where something ends, take less
  rather than more — a reviewer can ask for a second pass, but cannot recover what was removed.

Edit this in the Skills page. It is knowledge, not code: changing it changes the next video.
"""


class PlanningError(RuntimeError):
    """The instructions could not be turned into a plan, in terms of what to do about it."""


def build_prompt(
    *,
    instructions: str,
    house_style: str,
    duration_seconds: float,
    width: int,
    height: int,
    assets: Optional[dict[str, str]] = None,
) -> tuple[str, str]:
    """The system and user prompts. Separated from the call so the wording is testable.

    THE VIDEO'S OWN FACTS GO IN. A model asked to "cut the bit near the end" without knowing the
    video is 12 minutes long will invent a timecode, and an invented timecode is a valid plan
    that edits the wrong part — the one failure mode validation cannot catch.
    """
    known_assets = assets or {}
    asset_lines = (
        "\n".join(f"  {name} -> {artifact}" for name, artifact in known_assets.items())
        or "  (none — do not use overlay_image or watermark)")

    system = f"""\
You turn a person's editing instructions into a precise edit plan for a video tool.

You output JSON and nothing else. No prose before or after, no markdown fences.

THE VIDEO YOU ARE PLANNING FOR
  duration: {duration_seconds:.2f} seconds
  frame:    {width}x{height}

OPERATIONS YOU MAY USE — there are no others:
  cut            {{"op":"cut","remove":[{{"start":"M:SS","end":"M:SS"}}]}}
                 removes those ranges and joins what is left
  trim           {{"op":"trim","start":"M:SS","end":"M:SS"}}
                 keeps ONLY that range
  reframe        {{"op":"reframe","aspect":"9:16"}}   aspect one of: {", ".join(ASPECTS)}
  caption        {{"op":"caption","text":"...","start":"M:SS","end":"M:SS","style":"lower_third"}}
                 style one of: {", ".join(sorted(CAPTION_STYLES))}
  overlay_image  {{"op":"overlay_image","image_artifact_id":"...","start":"M:SS","end":"M:SS",
                  "position":"center"}}   position one of: {", ".join(sorted(POSITIONS))}
  watermark      {{"op":"watermark","logo_artifact_id":"...","corner":"top_right"}}

IMAGES YOU MAY REFER TO, by artifact id:
{asset_lines}

TIMECODES
  Seconds ("90", "90.5"), MM:SS ("2:10") or HH:MM:SS ("01:02:03.250").
  Never "90s", never "1m30s", never "2m10".
  Every timecode must be inside the video's duration above.

THE ORDER MATTERS AND IT IS NOT OBVIOUS
  Each operation applies to the RESULT of the one before it. If you cut 1:00-2:00 and then
  caption at 5:00, that caption lands at 6:00 of the original recording.
  So: put timed operations (caption, overlay_image) BEFORE any cut or trim whenever the
  person's timecodes were read off the original video, which is almost always.

OUTPUT SHAPE
{{
  "plan": [ {{ "op": "...", ...arguments..., "why": "the instruction this came from" }} ],
  "assumptions": ["anything you had to guess, in plain words"],
  "refused": ["anything asked for that no operation above can do"]
}}

RULES
  - Every operation carries "why", quoting or paraphrasing the instruction it came from.
  - If an instruction is ambiguous, make the most reasonable choice AND record it in
    "assumptions". A person reviews this before anything is published, so a recorded guess is
    useful and a silent one is not.
  - If something is asked for that no operation can do, put it in "refused" and leave it out of
    the plan. Do not approximate it with a different operation.
  - When an instruction is vague about how much to remove, remove LESS. A second pass is cheap;
    footage cut away is not recoverable from the result.
  - An empty plan is a valid answer if nothing was actually asked for."""

    user = f"""\
STANDING HOUSE RULES (defaults; the instructions below override them):
{house_style.strip() or "(none)"}

INSTRUCTIONS FOR THIS VIDEO:
{instructions.strip()}"""
    return system, user


def parse_plan(raw: str) -> dict[str, Any]:
    """Get the JSON out of a model's answer, however it decided to wrap it.

    Models are told to emit bare JSON and sometimes emit a fenced block, or a sentence first.
    Failing on that would make the step's reliability a function of the model's mood, so the
    object is extracted rather than demanded. What is NOT relaxed is the content: whatever comes
    out still goes through validate_plan.
    """
    text = str(raw or "").strip()
    if not text:
        raise PlanningError("the model returned nothing.")
    fenced = re.search(r"```(?:json)?\s*(.+?)```", text, re.S)
    if fenced:
        text = fenced.group(1).strip()
    try:
        body = json.loads(text)
    except json.JSONDecodeError:
        # PROSE ON EITHER SIDE, not just before. Extracting only when the text did not begin
        # with "{" missed the commoner habit — a correct object followed by "Let me know if
        # you want changes!" — and failed a perfectly good plan.
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            raise PlanningError(
                f"the model did not return a plan. It said: {str(raw)[:300]}")
        try:
            body = json.loads(text[start:end + 1])
        except json.JSONDecodeError as exc:
            raise PlanningError(f"the model's plan was not valid JSON ({exc}).") from exc
    if not isinstance(body, dict):
        raise PlanningError("the model's answer was not an object.")
    return body


def plan_from_instructions(
    adapter,
    *,
    instructions: str,
    house_style: str,
    duration_seconds: float,
    width: int,
    height: int,
    assets: Optional[dict[str, str]] = None,
    attempts: int = 2,
) -> dict[str, Any]:
    """Ask for a plan, check it, and ask again once with the error if it was wrong.

    ONE RETRY, AND IT CARRIES THE REASON. A model that used "90s" for a timecode or invented an
    operation will usually fix it when told exactly what was rejected; retrying with the same
    prompt just spends money on the same mistake. Two failures is a real one, and the error names
    what was wrong rather than "planning failed".
    """
    if not str(instructions or "").strip():
        raise PlanningError(
            "no instructions were given, so there is nothing to plan. Say what should happen to "
            "this video — for example: make a 30-second vertical short from 2:10 to 2:40.")

    system, user = build_prompt(
        instructions=instructions, house_style=house_style,
        duration_seconds=duration_seconds, width=width, height=height, assets=assets)

    last_error = ""
    for attempt in range(max(1, attempts)):
        ask = user if not last_error else (
            f"{user}\n\nYour previous answer was rejected: {last_error}\n"
            f"Return corrected JSON in the same shape. Change only what was wrong.")
        _ref, text_out, _usage = adapter.create_text_response(ask, system_prompt=system)
        try:
            body = parse_plan(text_out)
            raw_plan = body.get("plan") or []
            # "Have a look at this" asks for no edits, and inventing some would be worse than
            # none. validate_plan refuses an empty list because APPLYING one is pointless; here
            # it is information the reviewer should see, so it is passed through untouched.
            steps = validate_plan(raw_plan) if raw_plan else []
        except (PlanningError, VideoEditError) as exc:
            last_error = str(exc)
            continue
        return {
            "plan": steps,
            "assumptions": [str(a)[:300] for a in (body.get("assumptions") or [])][:20],
            "refused": [str(r)[:300] for r in (body.get("refused") or [])][:20],
            # Raised here rather than at render time so it reaches the REVIEWER, who is the only
            # one who can say whether the timecodes were read off the original video.
            "timeline_note": plan_timeline_warning(steps),
            "attempts": attempt + 1,
        }

    raise PlanningError(
        f"could not turn those instructions into a valid plan after {attempts} attempts. "
        f"The last problem was: {last_error}")


def summarise(result: dict[str, Any]) -> str:
    """What the review step shows a person.

    A reviewer approving a publish is deciding about a video, and a JSON plan is not a thing
    anybody reads carefully at that moment. This is the same information as a short list of
    sentences — including, deliberately, what the model GUESSED and what it could not do, which
    are the two things most likely to make the answer wrong.
    """
    lines: list[str] = []
    for index, step in enumerate(result.get("plan") or [], start=1):
        detail = {k: v for k, v in step.items() if k not in ("op", "why")}
        why = step.get("why")
        lines.append(f"{index}. {step['op']} {json.dumps(detail, default=str)}"
                     + (f"  — {why}" if why else ""))
    if not lines:
        lines.append("(nothing to do — the instructions asked for no changes)")
    if result.get("assumptions"):
        lines.append("")
        lines.append("Assumed:")
        lines += [f"  - {a}" for a in result["assumptions"]]
    if result.get("refused"):
        lines.append("")
        lines.append("Could NOT do:")
        lines += [f"  - {r}" for r in result["refused"]]
    if result.get("timeline_note"):
        lines.append("")
        lines.append(f"Note: {result['timeline_note']}")
    return "\n".join(lines)


def seed_house_style(connection, *, project_id: str, actor: str = "system") -> dict[str, Any]:
    """Put the standing rules in the Skills page, once, so they can be edited there.

    Seeded rather than hardcoded for the same reason the publishing standards are: what belongs
    on a channel is the operator's business and changing it must not need a deploy. Tier 0 — it
    performs nothing, it is knowledge a step carries.
    """
    from .skill_definitions import get_skill, save_skill

    if get_skill(connection, project_id, HOUSE_STYLE_SKILL_KEY) is not None:
        return {"skill_key": HOUSE_STYLE_SKILL_KEY, "seeded": False}
    save_skill(
        connection, project_id=project_id, actor=actor,
        skill_key=HOUSE_STYLE_SKILL_KEY,
        label="Video editing standards",
        behaviour_kind="builtin",
        summary="Standing rules for how this channel edits — editable house style.",
        knowledge=DEFAULT_HOUSE_STYLE,
        config={"executor_id": "knowledge.only"},
        risk_tier=0,
    )
    return {"skill_key": HOUSE_STYLE_SKILL_KEY, "seeded": True}


def house_style_for(connection, project_id: str) -> str:
    """The operator's standing rules, or the defaults if they have not been seeded yet.

    Never fails the step. An edit plan with no house style is a worse plan, not a broken one,
    and a missing knowledge row must not stop somebody publishing a video.
    """
    try:
        from .skill_definitions import get_skill

        row = get_skill(connection, project_id, HOUSE_STYLE_SKILL_KEY) or {}
        return str(row.get("knowledge") or "").strip() or DEFAULT_HOUSE_STYLE
    except Exception:  # noqa: BLE001
        return DEFAULT_HOUSE_STYLE
