#!/usr/bin/env python3
"""Build the "Security finding triage" workflow on a BOSS install.

An EXAMPLE. Nothing here ships with BOSS and nothing runs it on install. README.md does the
same thing by hand in the console.

WHAT IT BUILDS
--------------
    judge  →  route ─ true positive ─→  file   (ticket, NEEDS A PERSON)
                    └ otherwise ─────→  tell   (email,  NEEDS A PERSON)
    security_reviewer                   program_manager / security_reviewer

Paste one finding from a scanner, a bug bounty report or a code review. The security reviewer
agent reads the code it points at and decides whether it is real. A true positive becomes a
ticket with the severity and the evidence — once you approve it. Anything else — a false
positive, or a finding that needs more information — is written up and emailed to the owner,
once you approve that.

ONE FINDING PER RUN, on purpose: each verdict is approved on its own, so you can accept four
and reject one.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from _common import (  # noqa: E402
    agent_step, connect, define_steps, enable_skills, from_input, from_step, grant, parser,
    publish, step,
)

NAME = "Security finding triage"
DESCRIPTION = (
    "Paste a security finding. The security reviewer reads the code it points at and decides "
    "if it is real; a true positive becomes a ticket, anything else a note — after approval."
)

JUDGE = "security_reviewer"
JUDGE_SKILLS = ["repo.read"]

STEPS = [
    {"label": "File the finding", "skill_id": "tracker.issues.create",
     "summary": "Create the confirmed finding as an issue in the work tracker.",
     "inputs": [{"name": "actions", "required": True}],
     "agent": "program_manager"},
    {"label": "Tell the owner", "skill_id": "email.send_real_guarded",
     "summary": "Email the verdict and the reasoning to the owner.",
     "inputs": [{"name": "to", "required": True}, {"name": "body", "required": True},
                {"name": "subject", "required": False}],
     "agent": "security_reviewer"},
]

JUDGE_BRIEF = (
    "A security finding is in `finding`. Using the repository tools you hold, read the code it "
    "points at, how that code is reached, and whether the input it worries about is validated "
    "or trusted. Decide `verdict`: 'true_positive' if the vulnerability is reachable as "
    "described, 'false_positive' if the code already prevents it or the path is unreachable, "
    "or 'needs_info' if you cannot tell from the repository alone. Decide `severity` as "
    "critical, high, medium, low or none. Write `summary` for the owner: the verdict, the "
    "evidence (file and line), and the recommended fix or the information still needed — six "
    "sentences at most. Write `actions` as a JSON list with ONE item {title, detail} for a "
    "true positive (the ticket to fix it, severity first in the title, evidence in detail), "
    "and an empty list otherwise. Never report a vulnerability you did not confirm in the code."
)


def build_spec(keys: dict[str, str], args) -> dict:
    """The workflow definition, built from the custom step keys the install assigned."""
    return {
        "entry_step": "judge",
        "input_schema": {
            "required": ["finding", "to"],
            "properties": {
                "finding": {"title": "Finding", "format": "textarea",
                            "description": "One finding: the scanner output, the report, or "
                                           "the review comment, with the file it points at."},
                "to": {"title": "Owner email", "format": "email",
                       "description": "Who is told the verdict."},
                "repository": {"title": "Repository",
                               "description": "Optional. owner/name, when the project has more "
                                              "than one repository bound."},
                "judge_brief": {"title": "Triage brief", "format": "textarea",
                                "description": "Optional; leave blank for the stored brief.",
                                "examples": [JUDGE_BRIEF]},
            },
        },
        "steps": [
            agent_step("judge", from_input("judge_brief", default=JUDGE_BRIEF), ["route"],
                       agent=JUDGE, args=args,
                       produces=["verdict", "severity", "summary", "actions"],
                       context={"finding": from_input("finding"),
                                "repository": from_input("repository", default="")}),
            {"id": "route", "kind": "condition",
             "expression": {"path": "steps.judge.output.verdict", "op": "eq",
                            "value": "true_positive"},
             "on_true": "file", "on_false": "tell"},
            step("file", keys["File the finding"],
                 {"actions": from_step("judge", "actions")},
                 [], agent="program_manager", approval="manual", args=args),
            step("tell", keys["Tell the owner"],
                 {"to": from_input("to"),
                  "subject": "Security finding triage",
                  "body": from_step("judge", "summary")},
                 [], agent="security_reviewer", approval="manual", args=args),
        ],
    }


def main() -> None:
    args = parser("Build the security finding triage workflow.").parse_args()
    api = connect(args, NAME)
    enable_skills(api, sorted({s["skill_id"] for s in STEPS} | set(JUDGE_SKILLS)), args)
    grant(api, JUDGE, sorted(set(JUDGE_SKILLS) | {s["skill_id"] for s in STEPS
                                                  if s.get("agent") == JUDGE}), args)
    for agent in sorted({s["agent"] for s in STEPS if s.get("agent") and s["agent"] != JUDGE}):
        grant(api, agent,
              sorted({s["skill_id"] for s in STEPS if s.get("agent") == agent}), args)
    keys = define_steps(api, STEPS, args)
    if args.dry_run:
        print(f"  WORKFLOW\n    would create {NAME!r}\n")
        return

    publish(api, name=NAME, description=DESCRIPTION, args=args, spec=build_spec(keys, args))

    print(f"""  Done.

  Start a run: Workflows → Library → "{NAME}" → ⋯ → Start.

    finding      one finding, pasted, with the file it points at
    to           who is told the verdict
    repository   optional — owner/name when more than one is bound

  One of two steps then stops and waits: a ticket to approve, or an email to
  approve. Nothing is filed or sent until you do.
""")


if __name__ == "__main__":
    main()
