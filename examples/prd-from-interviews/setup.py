#!/usr/bin/env python3
"""Build the "PRD from customer interviews" workflow on a BOSS install.

An EXAMPLE. Nothing here ships with BOSS and nothing runs it on install. README.md does the
same thing by hand in the console.

WHAT IT BUILDS
--------------
    themes  →  route ─ enough evidence ──→  prd  →  send     (NEEDS A PERSON)
                     └ not enough ───────→  tell_gap         (NEEDS A PERSON)
    program_manager                          program_manager

Paste the notes from your customer interviews and name the product area. A model pulls out
the themes — what people actually said, how often, in their words — and says plainly whether
there is enough evidence to write a PRD. If there is, the PRD is drafted and emailed to a
reviewer once a person approves. If there is not, the reviewer gets the themes and the gaps
instead, once a person approves that.

THE HONEST NO IS THE POINT. Three interviews that all say different things do not justify a
requirements document, and a PRD written anyway would read as though they did. The branch is a
`condition` on the first line of the themes step's own text.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from _common import (  # noqa: E402
    connect, define_steps, enable_skills, from_input, from_step, grant, parser, publish, step,
)

NAME = "PRD from customer interviews"
DESCRIPTION = (
    "Paste interview notes. BOSS extracts the themes, says whether there is enough evidence "
    "for a PRD, and drafts one only if there is — emailed to a reviewer once you approve."
)

STEPS = [
    {"label": "Find the themes", "skill_id": "model.generate_text",
     "summary": "What the interviews actually said, how often, and whether it is enough.",
     "inputs": [{"name": "prompt", "required": True},
                {"name": "context", "required": False},
                {"name": "system", "required": False}],
     "agent": "program_manager"},
    {"label": "Draft the PRD", "skill_id": "docs.draft",
     "summary": "A requirements document grounded in the themes, nothing more.",
     "inputs": [{"name": "brief", "required": True},
                {"name": "context", "required": False},
                {"name": "audience", "required": False}],
     "agent": "program_manager"},
    {"label": "Email the reviewer", "skill_id": "email.send_real_guarded",
     "summary": "Send the PRD, or the gaps, to the reviewer once a person has read it.",
     "inputs": [{"name": "to", "required": True}, {"name": "body", "required": True},
                {"name": "subject", "required": False}],
     "agent": "program_manager"},
]

#: The marker the condition reads. The brief makes it the FIRST LINE of the output so the
#: branch is decided by something a person can see at the top of the step's result.
INSUFFICIENT = "EVIDENCE: insufficient"

THEMES_BRIEF = (
    "The main text names a product area; the material is a set of customer interview notes. "
    "Pull out the themes relevant to that area. For each theme: what people said, in their "
    "words where possible; how many interviews raised it; and how strongly. Separate what they "
    "asked for from what they were trying to do. Then judge the evidence: write the FIRST line "
    "of your answer as exactly 'EVIDENCE: sufficient' if at least three interviews converge on "
    "a problem worth specifying, or exactly '" + INSUFFICIENT + "' if they do not — and in "
    "that case say what is missing and which questions the next interviews should ask. Do not "
    "pad a thin set of notes into a strong theme."
)

PRD_BRIEF = (
    "Write a product requirements document from the interview themes below. Sections: "
    "Problem (in the customers' words, with how many raised it), Who it is for, What we will "
    "build (requirements, each traceable to a theme), What we will not build, Success "
    "measures, Open questions. A requirement with no theme behind it does not belong here. "
    "Markdown, no preamble."
)


def build_spec(keys: dict[str, str], args) -> dict:
    """The workflow definition, built from the custom step keys the install assigned."""
    return {
        "entry_step": "themes",
        "input_schema": {
            "required": ["notes", "area", "to"],
            "properties": {
                "notes": {"title": "Interview notes", "format": "textarea",
                          "description": "The notes or transcripts, pasted. Mark where one "
                                         "interview ends and the next begins."},
                "area": {"title": "Product area",
                         "description": "What the PRD is about.",
                         "examples": ["invoice export"]},
                "to": {"title": "Reviewer email", "format": "email",
                       "description": "Who receives the PRD, or the gaps."},
                "themes_brief": {"title": "Themes brief", "format": "textarea",
                                 "description": "Optional; leave blank for the stored brief.",
                                 "examples": [THEMES_BRIEF]},
                "prd_brief": {"title": "PRD brief", "format": "textarea",
                              "description": "Optional; leave blank for the stored brief.",
                              "examples": [PRD_BRIEF]},
            },
        },
        "steps": [
            step("themes", keys["Find the themes"],
                 {"system": from_input("themes_brief", default=THEMES_BRIEF),
                  "prompt": from_input("area"),
                  "context": from_input("notes")},
                 ["route"], agent="program_manager", args=args),
            # THE HONEST NO. `contains` reads the text the step wrote, ignoring case.
            {"id": "route", "kind": "condition",
             "expression": {"path": "steps.themes.output.text", "op": "contains",
                            "value": INSUFFICIENT},
             "on_true": "tell_gap", "on_false": "prd"},
            step("tell_gap", keys["Email the reviewer"],
                 {"to": from_input("to"),
                  "subject": "PRD not written: not enough evidence yet",
                  "body": from_step("themes", "text")},
                 [], agent="program_manager", approval="manual", args=args),
            # THE PRD IS WRITTEN FROM THE THEMES, NOT THE RAW NOTES, so a requirement can be
            # traced to something a customer said.
            step("prd", keys["Draft the PRD"],
                 {"brief": from_input("prd_brief", default=PRD_BRIEF),
                  "context": from_step("themes", "text"),
                  "audience": "the product and engineering team"},
                 ["send"], agent="program_manager", args=args),
            step("send", keys["Email the reviewer"],
                 {"to": from_input("to"),
                  "subject": from_input("area"),
                  "body": from_step("prd", "markdown")},
                 [], agent="program_manager", approval="manual", args=args),
        ],
    }


def main() -> None:
    args = parser("Build the PRD from interviews workflow.").parse_args()
    api = connect(args, NAME)
    enable_skills(api, sorted({s["skill_id"] for s in STEPS}), args)
    for agent in sorted({s["agent"] for s in STEPS if s.get("agent")}):
        grant(api, agent,
              sorted({s["skill_id"] for s in STEPS if s.get("agent") == agent}), args)
    keys = define_steps(api, STEPS, args)
    if args.dry_run:
        print(f"  WORKFLOW\n    would create {NAME!r}\n")
        return

    publish(api, name=NAME, description=DESCRIPTION, args=args, spec=build_spec(keys, args))

    print(f"""  Done.

  Start a run: Workflows → Library → "{NAME}" → ⋯ → Start.

    notes   the interview notes, pasted
    area    what the PRD is about
    to      who reviews it

  If the evidence is thin, the run says so and emails the gaps instead of a PRD.
  Either way the last step stops and waits for you.
""")


if __name__ == "__main__":
    main()
