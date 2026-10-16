#!/usr/bin/env python3
"""Build the "Pipeline digest" workflow on a BOSS install.

An EXAMPLE. Nothing here ships with BOSS and nothing runs it on install. README.md does the
same thing by hand in the console.

WHAT IT BUILDS
--------------
    digest  →  send
    program_manager   NEEDS A PERSON

Paste the pipeline export from your CRM. A model writes the digest a sales lead wants on a
Monday: what moved, what stalled, what closes this month and what is at risk — in numbers from
the export, not adjectives. It is emailed once a person approves.

THE SIMPLEST EXAMPLE IN THE LIBRARY, on purpose: two steps, one of them a stop. If you are
building your first workflow by hand, start from this one.

PASTED, NOT PULLED. BOSS has no CRM connector, so the export is an input rather than a fetch —
which is also why this is not scheduled: a weekly run with last week's paste would be a lie.
Connect a CRM later and the first step changes; nothing else does.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from _common import (  # noqa: E402
    connect, define_steps, enable_skills, from_input, from_step, grant, parser, publish, step,
)

NAME = "Pipeline digest"
DESCRIPTION = (
    "Paste a pipeline export. BOSS writes the weekly digest — moved, stalled, closing, at "
    "risk — and emails it once you approve."
)

STEPS = [
    {"label": "Write the digest", "skill_id": "model.generate_text",
     "summary": "The weekly pipeline digest, in numbers from the export.",
     "inputs": [{"name": "prompt", "required": True},
                {"name": "context", "required": False},
                {"name": "system", "required": False}],
     "agent": "program_manager"},
    {"label": "Email the digest", "skill_id": "email.send_real_guarded",
     "summary": "Send it, once a person has read it.",
     "inputs": [{"name": "to", "required": True}, {"name": "body", "required": True},
                {"name": "subject", "required": False}],
     "agent": "program_manager"},
]

DIGEST_BRIEF = (
    "The main text is a pipeline export (CSV or a table) from a CRM; the material, if any, "
    "says what the reader cares about this week. Write the weekly digest: total pipeline and "
    "weighted pipeline if the columns allow it; deals that moved stage, with from and to; deals "
    "that have not moved in 14 days or more; deals closing this month, with amount and owner; "
    "deals at risk, and why, from the notes in the export only. Numbers come from the export — "
    "if a column is missing, say 'not in the export' rather than estimating. Plain text, short "
    "sections, no preamble."
)


def build_spec(keys: dict[str, str], args) -> dict:
    """The workflow definition, built from the custom step keys the install assigned."""
    return {
        "entry_step": "digest",
        "input_schema": {
            "required": ["pipeline", "to"],
            "properties": {
                "pipeline": {"title": "Pipeline export", "format": "textarea",
                             "description": "The CRM export, pasted as CSV or a table, with "
                                            "stage, amount, owner, close date and last "
                                            "activity if you have them."},
                "focus": {"title": "This week's focus", "format": "textarea",
                          "description": "Optional. What the reader cares about this week."},
                "to": {"title": "Recipient email", "format": "email",
                       "description": "Who receives the digest."},
                "subject": {"title": "Email subject",
                            "description": "Optional; defaults to Weekly pipeline digest."},
                "digest_brief": {"title": "Digest brief", "format": "textarea",
                                 "description": "Optional; leave blank for the stored brief.",
                                 "examples": [DIGEST_BRIEF]},
            },
        },
        "steps": [
            step("digest", keys["Write the digest"],
                 {"system": from_input("digest_brief", default=DIGEST_BRIEF),
                  "prompt": from_input("pipeline"),
                  "context": from_input("focus", default="")},
                 ["send"], agent="program_manager", args=args),
            step("send", keys["Email the digest"],
                 {"to": from_input("to"),
                  "subject": from_input("subject", default="Weekly pipeline digest"),
                  "body": from_step("digest", "text")},
                 [], agent="program_manager", approval="manual", args=args),
        ],
    }


def main() -> None:
    args = parser("Build the pipeline digest workflow.").parse_args()
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

    pipeline   the CRM export, pasted
    focus      optional — what matters this week
    to         who receives the digest

  The send step stops and waits. Read it first.
""")


if __name__ == "__main__":
    main()
