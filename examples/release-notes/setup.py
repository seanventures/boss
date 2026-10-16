#!/usr/bin/env python3
"""Build the "Release notes" workflow on a BOSS install.

An EXAMPLE. Nothing here ships with BOSS and nothing runs it on install. README.md does the
same thing by hand in the console.

WHAT IT BUILDS
--------------
    collect  →  ┌ customer ┐  →  combine  →  send
                └ internal ┘
    release_manager   docs_writer     docs_writer   docs_writer
    auto              auto (parallel) auto          NEEDS A PERSON

An agent reads the repository's merged pull requests since a tag or a date and lists them.
Two drafts are written AT THE SAME TIME — one for customers, one for the team — then joined
into a single note that is emailed to a reviewer once a person approves. Nothing is published:
the reviewer pastes the approved notes wherever releases are announced.

WHY TWO BRANCHES. Customer notes and internal notes are written from the same list to different
readers, and neither depends on the other, so the workflow runs them in parallel and waits for
both (`parallel` → `join`). That is the shape to copy whenever two things can be done at once.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from _common import (  # noqa: E402
    agent_step, connect, define_steps, enable_skills, from_input, from_step, grant, parser,
    publish, step,
)

NAME = "Release notes"
DESCRIPTION = (
    "Read the pull requests merged since a tag or date, write customer-facing and internal "
    "notes in parallel, and email the combined notes to a reviewer once you approve."
)

#: The agent that reads the repository. `repo.read` is the engine's own read tools — git
#: history, pull requests, files — scoped to the project's bound repository.
COLLECTOR = "release_manager"
COLLECTOR_SKILLS = ["repo.read"]

STEPS = [
    {"label": "Write the customer notes", "skill_id": "docs.draft",
     "summary": "Release notes a customer would read: what changed for them, in their words.",
     "inputs": [{"name": "brief", "required": True},
                {"name": "context", "required": False},
                {"name": "audience", "required": False}],
     "agent": "docs_writer"},
    {"label": "Write the internal notes", "skill_id": "docs.draft",
     "summary": "Notes for the team: every change, risks, follow-ups, who to thank.",
     "inputs": [{"name": "brief", "required": True},
                {"name": "context", "required": False},
                {"name": "audience", "required": False}],
     "agent": "docs_writer"},
    {"label": "Combine the drafts", "skill_id": "model.generate_text",
     "summary": "Put both drafts into one note, customer section first.",
     # THREE SLOTS, NOT ONE. A step wires whole values, so a step that needs two earlier
     # outputs reads one as `prompt` and the other as `context`; the standing instruction
     # travels as `system`.
     "inputs": [{"name": "prompt", "required": True},
                {"name": "context", "required": False},
                {"name": "system", "required": False}],
     "agent": "docs_writer"},
    {"label": "Email the notes", "skill_id": "email.send_real_guarded",
     "summary": "Send the combined notes to the reviewer, once a person has read them.",
     "inputs": [{"name": "to", "required": True}, {"name": "body", "required": True},
                {"name": "subject", "required": False}],
     "agent": "docs_writer"},
]

COLLECT_BRIEF = (
    "List the pull requests merged into the project's repository since the point named in "
    "`since` (a tag, a date or a commit — given alongside this instruction). Use the repository "
    "tools you hold: git history and pull requests. For each merged PR record its number, title, "
    "author, one-sentence summary and a kind: feature, fix, docs, chore or breaking. Do not "
    "invent a PR, a number or an author. If you cannot read the repository or `since` cannot be "
    "found, say so and declare the step blocked rather than guessing.\n"
    "Return `changes` as a JSON list of {number, title, author, kind, summary} and `highlights` "
    "as the three to five changes a reader would care about most, as short sentences."
)

CUSTOMER_BRIEF = (
    "Write customer-facing release notes from the list of merged changes below. Group by "
    "feature, fix and breaking change. Say what a customer can now do differently, not what the "
    "code does. Leave out chores, refactors and anything internal. Mark breaking changes with "
    "what the customer has to do. Markdown, short, no preamble."
)

INTERNAL_BRIEF = (
    "Write internal release notes for the engineering team from the list of merged changes "
    "below. Include every change with its PR number and author. Call out risks, follow-ups and "
    "anything that needs watching after release. Markdown, no preamble."
)

COMBINE_BRIEF = (
    "You are given two drafts of release notes for the same release: the customer-facing notes "
    "as the main text and the internal notes as material. Produce one document with a "
    "'Customer notes' section first and an 'Internal notes' section second. Do not change the "
    "substance of either; fix only duplication and inconsistent wording. Markdown, no preamble."
)


def build_spec(keys: dict[str, str], args) -> dict:
    """The workflow definition, built from the custom step keys the install assigned."""
    return {
        "entry_step": "collect",
        "input_schema": {
            "required": ["since", "to"],
            "properties": {
                "since": {"title": "Since",
                          "description": "A tag, a date or a commit — the start of the release.",
                          "examples": ["v1.4.0"]},
                "to": {"title": "Reviewer email", "format": "email",
                       "description": "Who receives the combined notes for review."},
                "repository": {"title": "Repository",
                               "description": "Optional. owner/name, when the project has more "
                                              "than one repository bound."},
                "subject": {"title": "Email subject",
                            "description": "Optional; defaults to Release notes for review."},
                "collect_brief": {"title": "Collection brief", "format": "textarea",
                                  "description": "Optional; leave blank for the stored brief.",
                                  "examples": [COLLECT_BRIEF]},
            },
        },
        "steps": [
            # AN AGENT WITH THE REPOSITORY TOOLS. It reads git history and pull requests and
            # decides the two values the later steps wire to. Blocked or failed is a visible
            # outcome: a step that cannot read the repository says so instead of inventing PRs.
            agent_step("collect", from_input("collect_brief", default=COLLECT_BRIEF), ["fork"],
                       agent=COLLECTOR, args=args,
                       produces=["changes", "highlights"],
                       context={"since": from_input("since"),
                                "repository": from_input("repository", default="")}),
            # TWO DRAFTS AT ONCE. The parallel step starts both branches; the join waits for
            # both to succeed before the combine step runs.
            {"id": "fork", "kind": "parallel", "branches": ["customer", "internal"],
             "join": "join"},
            step("customer", keys["Write the customer notes"],
                 {"brief": CUSTOMER_BRIEF,
                  "context": from_step("collect", "changes"),
                  "audience": "customers"},
                 ["join"], agent="docs_writer", args=args),
            step("internal", keys["Write the internal notes"],
                 {"brief": INTERNAL_BRIEF,
                  "context": from_step("collect", "changes"),
                  "audience": "the engineering team"},
                 ["join"], agent="docs_writer", args=args),
            {"id": "join", "kind": "join", "wait_for": ["customer", "internal"],
             "success_policy": "all", "next": ["combine"]},
            step("combine", keys["Combine the drafts"],
                 {"system": COMBINE_BRIEF,
                  "prompt": from_step("customer", "markdown"),
                  "context": from_step("internal", "markdown")},
                 ["send"], agent="docs_writer", args=args),
            # email.send_real_guarded is tier 2 AND requires an approval in its own policy, so
            # this stops for a person whatever the workflow asks for.
            step("send", keys["Email the notes"],
                 {"to": from_input("to"),
                  "subject": from_input("subject", default="Release notes for review"),
                  "body": from_step("combine", "text")},
                 [], agent="docs_writer", approval="manual", args=args),
        ],
    }


def main() -> None:
    args = parser("Build the release notes workflow.").parse_args()
    api = connect(args, NAME)
    enable_skills(api, sorted({s["skill_id"] for s in STEPS} | set(COLLECTOR_SKILLS)), args)
    # AND THE AGENTS MUST HOLD THEM. Enabling puts a skill in the project's catalogue; the
    # runtime checks `role_skills` before letting an agent call one.
    grant(api, COLLECTOR, COLLECTOR_SKILLS, args)
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

    since        a tag, a date or a commit — "v1.4.0"
    to           who reviews the notes
    repository   optional — owner/name when more than one is bound

  The two drafts are written at the same time. The last step stops and waits: read
  the combined notes, then approve the email or reject it. Nothing is published.
""")


if __name__ == "__main__":
    main()
