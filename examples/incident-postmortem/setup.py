#!/usr/bin/env python3
"""Build the "Incident postmortem" workflow on a BOSS install.

An EXAMPLE. Nothing here ships with BOSS and nothing runs it on install. README.md does the
same thing by hand in the console.

WHAT IT BUILDS
--------------
    ┌ timeline ┐  →  write  →  send  →  file
    └ actions  ┘
    program_manager  (parallel)   program_manager   NEEDS A PERSON   NEEDS A PERSON

Paste the incident notes and, if you have it, the chat log. Two things happen at once: a model
writes the timeline, and the action-item reader writes down what somebody has to do. Both feed
one postmortem document. The document is emailed to the reviewer once a person approves, and
the action items become tickets once a person approves those.

WHY TWO BRANCHES AND TWO STOPS. Timeline and actions are independent readings of the same
notes, so they run in parallel. The email and the tickets are separate effects on separate
systems, and each is approved on its own — rejecting the tickets does not unsend the email.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from _common import (  # noqa: E402
    connect, define_steps, enable_skills, from_input, from_step, grant, parser, publish, step,
)

NAME = "Incident postmortem"
DESCRIPTION = (
    "Paste incident notes. BOSS writes the timeline and the action items in parallel, drafts "
    "the postmortem, emails it for review and files the action items — each after approval."
)

STEPS = [
    {"label": "Write the timeline", "skill_id": "model.generate_text",
     "summary": "A timeline in the notes' own words: what happened, when, who noticed.",
     "inputs": [{"name": "prompt", "required": True},
                {"name": "context", "required": False},
                {"name": "system", "required": False}],
     "agent": "program_manager"},
    {"label": "Read the action items", "skill_id": "meeting.extract_actions",
     "summary": "What somebody has to do after this incident. Creates nothing.",
     "inputs": [{"name": "transcript", "required": True},
                {"name": "context", "required": False}],
     "agent": "program_manager"},
    {"label": "Write the postmortem", "skill_id": "model.generate_text",
     "summary": "The postmortem document, from the timeline and the action items.",
     "inputs": [{"name": "prompt", "required": True},
                {"name": "context", "required": False},
                {"name": "system", "required": False}],
     "agent": "program_manager"},
    {"label": "Email the postmortem", "skill_id": "email.send_real_guarded",
     "summary": "Send the postmortem to the reviewer, once a person has read it.",
     "inputs": [{"name": "to", "required": True}, {"name": "body", "required": True},
                {"name": "subject", "required": False}],
     "agent": "program_manager"},
    {"label": "File the action items", "skill_id": "tracker.issues.create",
     "summary": "Create the approved action items as issues in the work tracker.",
     "inputs": [{"name": "actions", "required": True}],
     "agent": "program_manager"},
]

TIMELINE_BRIEF = (
    "The main text is a set of incident notes; the material, if any, is the chat log from the "
    "incident channel. Write the timeline as a list of timestamped lines in the order things "
    "happened: detection, escalation, mitigation, resolution. Use the times and names as "
    "written; if a time is not stated, say 'time not recorded' rather than estimating. Then "
    "write two sentences on impact: who was affected and for how long, from the notes only."
)

POSTMORTEM_BRIEF = (
    "Write a blameless postmortem from the timeline (main text) and the action items "
    "(material, as JSON). Sections: Summary, Impact, Timeline, What went well, What went "
    "wrong, Where we got lucky, Action items (one line each, with owner as given or "
    "'unassigned'). Do not add causes or fixes that the material does not support; where the "
    "cause is not established, say so under What went wrong. Markdown, no preamble."
)


def build_spec(keys: dict[str, str], args) -> dict:
    """The workflow definition, built from the custom step keys the install assigned."""
    return {
        "entry_step": "fork",
        "input_schema": {
            "required": ["notes", "to"],
            "properties": {
                "notes": {"title": "Incident notes", "format": "textarea",
                          "description": "What happened, as written during or after the "
                                         "incident. Times and names as recorded."},
                "chat_log": {"title": "Chat log", "format": "textarea",
                             "description": "Optional. The incident channel, pasted."},
                "to": {"title": "Reviewer email", "format": "email",
                       "description": "Who receives the postmortem for review."},
                "subject": {"title": "Email subject",
                            "description": "Optional; defaults to Postmortem for review."},
            },
        },
        "steps": [
            # TWO READINGS AT ONCE. The timeline and the action items are read from the same
            # notes by different steps that do not depend on each other.
            {"id": "fork", "kind": "parallel", "branches": ["timeline", "actions"],
             "join": "join"},
            step("timeline", keys["Write the timeline"],
                 {"system": TIMELINE_BRIEF,
                  "prompt": from_input("notes"),
                  "context": from_input("chat_log", default="")},
                 ["join"], agent="program_manager", args=args),
            step("actions", keys["Read the action items"],
                 {"transcript": from_input("notes"),
                  "context": from_input("chat_log", default="")},
                 ["join"], agent="program_manager", args=args),
            {"id": "join", "kind": "join", "wait_for": ["timeline", "actions"],
             "success_policy": "all", "next": ["write"]},
            # ONE DOCUMENT FROM TWO OUTPUTS. The timeline is the prompt, the action list is
            # the material, and the standing brief is the system instruction.
            step("write", keys["Write the postmortem"],
                 {"system": POSTMORTEM_BRIEF,
                  "prompt": from_step("timeline", "text"),
                  "context": from_step("actions", "actions")},
                 ["send"], agent="program_manager", args=args),
            step("send", keys["Email the postmortem"],
                 {"to": from_input("to"),
                  "subject": from_input("subject", default="Postmortem for review"),
                  "body": from_step("write", "text")},
                 ["file"], agent="program_manager", approval="manual", args=args),
            # The tickets are approved SEPARATELY from the email. tracker.issues.create is
            # tier 2 — the approval floor — so this stops whatever the workflow asks for.
            step("file", keys["File the action items"],
                 {"actions": from_step("actions", "actions")},
                 [], agent="program_manager", approval="manual", args=args),
        ],
    }


def main() -> None:
    args = parser("Build the incident postmortem workflow.").parse_args()
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

    notes      the incident notes, pasted
    chat_log   optional — the incident channel, pasted
    to         who reviews the postmortem

  The run stops twice: once with the postmortem before it is emailed, once with the
  action items before they become tickets. Reject either on its own.
""")


if __name__ == "__main__":
    main()
