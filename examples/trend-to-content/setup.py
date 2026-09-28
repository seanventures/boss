#!/usr/bin/env python3
"""Build the "Trend to content" workflow on a BOSS install.

An EXAMPLE. Nothing here ships with BOSS and nothing runs it on install.

WHAT IT BUILDS
--------------
    research  →  assess  →  script  →  review
    (research)   program_manager      program_manager   program_manager
    auto         auto        auto     NEEDS A PERSON

A trend in; the context, an honest verdict on whether it is yours to talk about, and a script
out — emailed to you rather than posted anywhere.

YOU SUPPLY THE TREND. The sketch everybody draws starts with "detect trend", which needs a feed
from X, TikTok or Google Trends. BOSS connects to none of them, and pretending otherwise would
make the example a demo of something that does not work. Detection is also the part you
probably want a human doing: you know which trends are yours.

IT ENDS AT A SCRIPT ON PURPOSE. The script is what the video workflow takes as its
instructions, so these two chain: trend in here, video out of there, both stopping for you.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from _common import (  # noqa: E402
    connect, define_steps, enable_skills, from_input, from_step, grant, parser, publish,
    step,
)

NAME = "Trend to content"
DESCRIPTION = (
    "Give it a trend. BOSS researches the context, says honestly whether it is yours to talk "
    "about, and writes a script — which you read before anything is made."
)

STEPS = [
    {"label": "Assess the trend", "skill_id": "model.generate_text",
     "summary": "Is this ours to talk about, and what is the angle nobody else has?",
     "inputs": [{"name": "prompt", "required": True}],
     "agent": "program_manager"},
    {"label": "Write the script", "skill_id": "model.generate_text",
     "summary": "A short script, in the voice the channel actually uses.",
     "inputs": [{"name": "prompt", "required": True}],
     "agent": "program_manager"},
    {"label": "Send it for review", "skill_id": "email.send_real_guarded",
     "summary": "Email the verdict and the script to whoever decides.",
     "inputs": [{"name": "to", "required": True}, {"name": "body", "required": True}],
     "agent": "program_manager"},
]

ASSESS_BRIEF = (
    "Decide whether this trend is worth us saying anything about. From the research below:\n"
    "  WHAT IT IS: two sentences, for somebody who has not seen it.\n"
    "  OURS OR NOT: say plainly whether we have standing to talk about this, and why.\n"
    "  THE ANGLE: what we could say that is not what everyone else is saying. If there is "
    "nothing, say 'no angle' — that is a useful answer and a generic take is not.\n"
    "  HOW LONG IT LASTS: is this a week or a day?\n"
    "Do not manufacture relevance. Most trends are not ours, and saying so saves a video."
)

SCRIPT_BRIEF = (
    "Write a script for a short vertical video, 45 seconds at most, from the angle below. "
    "Open with the thing that makes somebody stop scrolling — not a greeting, not the channel "
    "name. Plain spoken sentences, the way a person talks. Mark where a caption or a diagram "
    "would help as [ON SCREEN: ...]. End with one specific thing to do or think about, never "
    "'like and subscribe'. If the assessment said there is no angle, write one line saying so "
    "instead of a script."
)


def main() -> None:
    args = parser("Build the trend-to-content workflow.").parse_args()
    api = connect(args, NAME)
    enable_skills(api, sorted({s["skill_id"] for s in STEPS}), args)
    # AND THE AGENTS MUST HOLD THEM. Enabling puts a skill in the project's catalogue; the
    # runtime checks `role_skills` before letting an agent call one, and a role spec's
    # `allowed_skills` reaches the agent's prompt and nowhere else. Every example installed here
    # once had agents that had been TOLD what they could use and held nothing — so every step
    # naming one was refused at its first call, twenty minutes into a run.
    for agent in sorted({s["agent"] for s in STEPS if s.get("agent")}):
        grant(api, agent,
              sorted({s["skill_id"] for s in STEPS if s.get("agent") == agent}), args)
    keys = define_steps(api, STEPS, args)
    if args.dry_run:
        print(f"  WORKFLOW\n    would create {NAME!r}\n")
        return

    publish(api, name=NAME, description=DESCRIPTION, args=args, spec={
        "entry_step": "research",
        "input_schema": {
            "required": ["trend", "to"],
            "properties": {
                "trend": {"title": "Trend", "description": "The trend you want assessed.",
                          "examples": ["Everyone is posting their AI coding setup"]},
                "to": {"title": "Reviewer email", "description": "Who receives the script."},
                "assess_brief": {"title": "Assessment brief", "format": "textarea",
                                 "description": "Optional; leave blank for the stored relevance brief.",
                                 "examples": [ASSESS_BRIEF]},
                "script_brief": {"title": "Script brief", "format": "textarea",
                                 "description": "Optional; leave blank for the stored script brief.",
                                 "examples": [SCRIPT_BRIEF]},
            },
        },
        "steps": [
            {"id": "research", "kind": "capability",
             "binding": {"capability": "research",
                         "inputs": {"seed": from_input("trend")}},
             "approval": {"mode": "auto"}, "next": ["assess"]},
            step("assess", keys["Assess the trend"],
                 {"prompt": from_input("assess_brief", default=ASSESS_BRIEF),
                  "context": from_step("research", "dossier")},
                 ["script"], agent="program_manager", args=args),
            # The script is written from the ASSESSMENT, not the raw research — so a trend
            # judged "not ours" produces one honest line rather than a polished script for a
            # video nobody should make.
            step("script", keys["Write the script"],
                 {"prompt": from_input("script_brief", default=SCRIPT_BRIEF),
                  "context": from_step("assess", "text")},
                 ["review"], agent="program_manager", args=args),
            step("review", keys["Send it for review"],
                 {"to": from_input("to"),
                  "subject": from_input("trend"),
                  "body": from_step("script", "text")},
                 [], agent="program_manager", approval="manual", args=args),
        ],
    })

    print(f"""  Done.

  Start a run: Delivery → Workflows → "{NAME}" → Start one.

    trend          what is happening — "everyone is posting their AI coding setup"
    to             who reads the script
    assess_brief   leave blank for the default
    script_brief   leave blank for the default

  It ends at a SCRIPT. Paste that into "Video edit and publish" as the editing
  instructions and the two workflows chain — both stopping for you.
""")


if __name__ == "__main__":
    main()
