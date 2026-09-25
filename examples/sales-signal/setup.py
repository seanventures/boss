#!/usr/bin/env python3
"""Build the "Sales signal" workflow on a BOSS install.

An EXAMPLE. Nothing here ships with BOSS and nothing runs it on install.

WHAT IT BUILDS
--------------
    research  →  assess  →  draft  →  notify
    (research)   program_manager      program_manager   program_manager
    auto         auto        auto     NEEDS A PERSON

A company name in; a researched, scored, drafted approach out — waiting for the salesperson to
read before anything is sent.

STARTED BY HAND, NOT BY A SIGNAL. The version everybody sketches begins with "a prospect
visits the pricing page". That needs an inbound webhook from whatever watches your site, and
BOSS has no opinion about what that is — so this starts from the company, which is what the
signal would have carried anyway. Wiring a real trigger later changes the first step and
nothing else.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from _common import (  # noqa: E402
    connect, define_steps, enable_skills, from_input, from_step, parser, publish, step,
)

NAME = "Sales signal"
DESCRIPTION = (
    "Give it a company. BOSS researches them, scores the opportunity against what you sell, "
    "drafts an approach, and holds it for the salesperson to read."
)

STEPS = [
    {"label": "Assess the opportunity", "skill_id": "model.generate_text",
     "summary": "Score the fit against what we sell, and say what the score rests on.",
     "inputs": [{"name": "prompt", "required": True}],
     "agent": "program_manager"},
    {"label": "Draft the approach", "skill_id": "model.generate_text",
     "summary": "Write the opening message, grounded in the research rather than in adjectives.",
     "inputs": [{"name": "prompt", "required": True}],
     "agent": "program_manager"},
    {"label": "Tell the salesperson", "skill_id": "email.send_real_guarded",
     "summary": "Send the assessment and the draft to whoever owns the account.",
     "inputs": [{"name": "to", "required": True}, {"name": "body", "required": True}],
     "agent": "program_manager"},
]

ASSESS_BRIEF = (
    "You are scoring a sales opportunity for a salesperson who will read this in thirty "
    "seconds. From the research below, write:\n"
    "  FIT: a score out of 10 and one sentence saying what it rests on.\n"
    "  WHY NOW: anything in the research that makes this a good moment, or 'nothing found'.\n"
    "  RISKS: what would make this a waste of their time.\n"
    "  UNKNOWNS: what the research did not establish.\n"
    "Do not infer budget, headcount or intent that is not in the material. A confident guess "
    "is worse than a gap, because the salesperson will repeat it to the prospect."
)

DRAFT_BRIEF = (
    "Write a short first email to this company, for the salesperson to edit and send. Ground "
    "every claim in the research below — name the thing you are referring to. No flattery, no "
    "'I hope this finds you well', no invented mutual connections. Four sentences at most, and "
    "a question they can answer in one line. If the research gives you nothing specific to "
    "open with, say so instead of writing a generic email."
)


def main() -> None:
    args = parser("Build the sales signal workflow.").parse_args()
    api = connect(args, NAME)
    enable_skills(api, sorted({s["skill_id"] for s in STEPS}), args)
    keys = define_steps(api, STEPS, args)
    if args.dry_run:
        print(f"  WORKFLOW\n    would create {NAME!r}\n")
        return

    publish(api, name=NAME, description=DESCRIPTION, args=args, spec={
        "entry_step": "research",
        "steps": [
            # BOSS's own research capability: a cited dossier, gated on real evidence. A
            # company with little public activity comes back thin rather than invented, which
            # is the correct answer to "should we spend time on them".
            {"id": "research", "kind": "capability",
             "binding": {"capability": "research",
                         "inputs": {"seed": from_input("company")}},
             "approval": {"mode": "auto"}, "next": ["assess"]},
            step("assess", keys["Assess the opportunity"],
                 {"prompt": from_input("assess_brief"),
                  "context": from_step("research", "dossier")},
                 ["draft"], agent="program_manager", args=args),
            step("draft", keys["Draft the approach"],
                 {"prompt": from_input("draft_brief"),
                  "context": from_step("research", "dossier")},
                 ["notify"], agent="program_manager", args=args),
            # The salesperson reads it before anything goes anywhere near the prospect. This
            # step emails THEM, not the company — the outreach is a draft, deliberately.
            step("notify", keys["Tell the salesperson"],
                 {"to": from_input("salesperson"),
                  "subject": from_input("company"),
                  "body": from_step("assess", "text")},
                 [], agent="program_manager", approval="manual", args=args),
        ],
    })

    print(f"""  Done.

  Start a run: Delivery → Workflows → "{NAME}" → Start one.

    company         who to look into — "Acme Corp, acme.com"
    salesperson     the address that gets the summary
    assess_brief    leave blank for the default scoring brief
    draft_brief     leave blank for the default outreach brief

  NOTHING IS SENT TO THE PROSPECT. The last step emails your salesperson the
  assessment; the drafted approach is theirs to edit and send.

  Default briefs this script assumes:

    assess: {ASSESS_BRIEF[:80]}...
    draft:  {DRAFT_BRIEF[:80]}...
""")


if __name__ == "__main__":
    main()
