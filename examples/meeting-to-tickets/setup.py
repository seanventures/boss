#!/usr/bin/env python3
"""Build the "Meeting to tickets" workflow on a BOSS install.

An EXAMPLE. Nothing here ships with BOSS and nothing runs it on install. README.md does the
same thing by hand in the console, which is worth reading first.

WHAT IT BUILDS
--------------
    extract  →  create-tickets
    program_manager   program_manager
    auto              NEEDS A PERSON

Paste a transcript. A model writes down what was decided and what somebody has to do; you read
that list as sentences and approve it; the tickets are created in your tracker.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from _common import (  # noqa: E402
    connect, define_steps, enable_skills, from_input, from_step, parser, publish, step,
)

NAME = "Meeting to tickets"
DESCRIPTION = (
    "Paste a meeting transcript. BOSS writes down what was decided and what somebody has to "
    "do, shows you the list, and creates the tickets once you approve."
)

STEPS = [
    {"label": "Read the meeting", "skill_id": "meeting.extract_actions",
     "summary": "Write down what was decided and what somebody has to do. Creates nothing.",
     "inputs": [{"name": "transcript", "required": True}],
     "agent": "program_manager"},
    {"label": "Create the tickets", "skill_id": "tracker.issues.create",
     "summary": "Create the approved actions as issues in the project's work tracker.",
     "inputs": [{"name": "actions", "required": True}],
     "agent": "program_manager"},
]


def main() -> None:
    args = parser("Build the meeting-to-tickets workflow.").parse_args()
    api = connect(args, NAME)
    enable_skills(api, [s["skill_id"] for s in STEPS], args)
    keys = define_steps(api, STEPS, args)
    if args.dry_run:
        print(f"  WORKFLOW\n    would create {NAME!r}\n")
        return

    publish(api, name=NAME, description=DESCRIPTION, args=args, spec={
        "entry_step": "extract",
        "steps": [
            step("extract", keys["Read the meeting"],
                 {"transcript": from_input("transcript"),
                  # Optional. Who the team are, what the project is called — things the
                  # transcript assumes and a reader would not know.
                  "context": from_input("context")},
                 ["create-tickets"], agent="program_manager", args=args),
            # No separate review step: tracker.issues.create is risk tier 2, which is the
            # approval floor, so BOSS stops here whatever the workflow asks for. THIS is where
            # you read the actions as sentences before any of them become real.
            step("create-tickets", keys["Create the tickets"],
                 {"actions": from_step("extract", "actions")},
                 [], agent="program_manager", approval="manual", args=args),
        ],
    })

    print("""  Done.

  Start a run: Delivery → Workflows → "Meeting to tickets" → Start one.

    transcript   paste the meeting notes or transcript
    context      optional — who the team are, what the project is called

  The second step stops and waits. Open it to read what was decided, the actions with
  their owners, and anything it heard and could not place — then approve or reject.
""")


if __name__ == "__main__":
    main()
