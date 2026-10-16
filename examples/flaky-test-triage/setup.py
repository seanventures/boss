#!/usr/bin/env python3
"""Build the "Flaky test triage" workflow on a BOSS install.

An EXAMPLE. Nothing here ships with BOSS and nothing runs it on install. README.md does the
same thing by hand in the console.

WHAT IT BUILDS
--------------
    diagnose  →  route ─ flaky ───→  file       (tickets, NEEDS A PERSON)
                       └ otherwise → escalate   (email,   NEEDS A PERSON)
    engineering_captain              program_manager / engineering_captain

Paste a failing test and its output. An agent reads the test, the code it exercises, the recent
history of that file and the CI results, and says whether the failure is flaky or real. A flaky
test becomes a ticket to quarantine or fix it — once you approve the ticket. A real failure, or
one the agent cannot classify, is escalated to the owner by email — once you approve the email.

WHY A BRANCH. The two outcomes need different people and different effects. A `condition`
reads the agent's own `classification` and takes one path; the other is recorded as not taken.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from _common import (  # noqa: E402
    agent_step, connect, define_steps, enable_skills, from_input, from_step, grant, parser,
    publish, step,
)

NAME = "Flaky test triage"
DESCRIPTION = (
    "Paste a failing test and its output. An agent decides whether it is flaky or real; a "
    "flaky test becomes a ticket, a real failure is escalated — each after your approval."
)

DIAGNOSER = "engineering_captain"
DIAGNOSER_SKILLS = ["repo.read", "ci.status"]

STEPS = [
    {"label": "File the ticket", "skill_id": "tracker.issues.create",
     "summary": "Create the quarantine-or-fix ticket in the project's work tracker.",
     "inputs": [{"name": "actions", "required": True}],
     "agent": "program_manager"},
    {"label": "Escalate to the owner", "skill_id": "email.send_real_guarded",
     "summary": "Email the diagnosis to whoever owns the area.",
     "inputs": [{"name": "to", "required": True}, {"name": "body", "required": True},
                {"name": "subject", "required": False}],
     "agent": "engineering_captain"},
]

DIAGNOSE_BRIEF = (
    "A test is failing; its name and output are in `test`. Using the repository tools you "
    "hold, read the test, the code it exercises, the recent git history of both, and the CI "
    "check results. Decide `classification`: 'flaky' if the same test passes and fails on the "
    "same code (timing, ordering, shared state, network), 'real' if the failure follows from a "
    "change in the code, or 'unknown' if you cannot tell. Write `summary` for the owner: what "
    "fails, the evidence for your classification, and the next step — five sentences at most. "
    "Write `actions` as a JSON list with ONE item, {title, detail}: for a flaky test, the "
    "ticket to quarantine or fix it, with the evidence in `detail`; for anything else, an empty "
    "list. Do not invent a cause you did not find evidence for."
)


def build_spec(keys: dict[str, str], args) -> dict:
    """The workflow definition, built from the custom step keys the install assigned."""
    return {
        "entry_step": "diagnose",
        "input_schema": {
            "required": ["test", "to"],
            "properties": {
                "test": {"title": "Failing test", "format": "textarea",
                         "description": "The test's name and its failure output, pasted."},
                "to": {"title": "Owner email", "format": "email",
                       "description": "Who is emailed when the failure is real or unclear."},
                "repository": {"title": "Repository",
                               "description": "Optional. owner/name, when the project has more "
                                              "than one repository bound."},
                "diagnose_brief": {"title": "Diagnosis brief", "format": "textarea",
                                   "description": "Optional; leave blank for the stored brief.",
                                   "examples": [DIAGNOSE_BRIEF]},
            },
        },
        "steps": [
            agent_step("diagnose", from_input("diagnose_brief", default=DIAGNOSE_BRIEF),
                       ["route"], agent=DIAGNOSER, args=args,
                       produces=["classification", "summary", "actions"],
                       context={"test": from_input("test"),
                                "repository": from_input("repository", default="")}),
            {"id": "route", "kind": "condition",
             "expression": {"path": "steps.diagnose.output.classification", "op": "eq",
                            "value": "flaky"},
             "on_true": "file", "on_false": "escalate"},
            # tracker.issues.create is risk tier 2, which is the approval floor, so BOSS stops
            # here whatever the workflow asks for. You read the ticket as a sentence first.
            step("file", keys["File the ticket"],
                 {"actions": from_step("diagnose", "actions")},
                 [], agent="program_manager", approval="manual", args=args),
            step("escalate", keys["Escalate to the owner"],
                 {"to": from_input("to"),
                  "subject": "A failing test needs a person",
                  "body": from_step("diagnose", "summary")},
                 [], agent="engineering_captain", approval="manual", args=args),
        ],
    }


def main() -> None:
    args = parser("Build the flaky test triage workflow.").parse_args()
    api = connect(args, NAME)
    enable_skills(api, sorted({s["skill_id"] for s in STEPS} | set(DIAGNOSER_SKILLS)), args)
    grant(api, DIAGNOSER, sorted(set(DIAGNOSER_SKILLS) | {s["skill_id"] for s in STEPS
                                                          if s.get("agent") == DIAGNOSER}), args)
    for agent in sorted({s["agent"] for s in STEPS if s.get("agent") and
                         s["agent"] != DIAGNOSER}):
        grant(api, agent,
              sorted({s["skill_id"] for s in STEPS if s.get("agent") == agent}), args)
    keys = define_steps(api, STEPS, args)
    if args.dry_run:
        print(f"  WORKFLOW\n    would create {NAME!r}\n")
        return

    publish(api, name=NAME, description=DESCRIPTION, args=args, spec=build_spec(keys, args))

    print(f"""  Done.

  Start a run: Workflows → Library → "{NAME}" → ⋯ → Start.

    test         the test name and its failure output, pasted
    to           who is emailed when the failure is real or unclear
    repository   optional — owner/name when more than one is bound

  One of two steps then stops and waits: a ticket to approve, or an email to
  approve. Nothing is filed or sent until you do.
""")


if __name__ == "__main__":
    main()
