#!/usr/bin/env python3
"""Build the "Competitive teardown" workflow on a BOSS install.

An EXAMPLE. Nothing here ships with BOSS and nothing runs it on install. README.md does the
same thing by hand in the console.

WHAT IT BUILDS
--------------
    ┌ them (research) ┐  →  compare  →  write  →  send
    └ us   (research) ┘
    (research, parallel)    program_manager         NEEDS A PERSON

Name a competitor and say what you sell. BOSS researches both AT THE SAME TIME — two cited
dossiers — compares them, writes the teardown, and emails it to a reviewer once a person
approves. Thin evidence comes back thin: a competitor with little public material produces a
short teardown that says so, not a confident one.

WHY TWO RESEARCH RUNS. A teardown is a comparison, and a comparison needs both sides
researched to the same standard. They do not depend on each other, so they run in parallel.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from _common import (  # noqa: E402
    connect, define_steps, enable_skills, from_input, from_step, grant, parser, publish, step,
)

NAME = "Competitive teardown"
DESCRIPTION = (
    "Research a competitor and your own product in parallel, compare them from the evidence, "
    "write the teardown, and email it to a reviewer once you approve."
)

STEPS = [
    {"label": "Compare the two", "skill_id": "model.generate_text",
     "summary": "Where they win, where we win, and what the evidence does not say.",
     "inputs": [{"name": "prompt", "required": True},
                {"name": "context", "required": False},
                {"name": "system", "required": False}],
     "agent": "program_manager"},
    {"label": "Write the teardown", "skill_id": "docs.draft",
     "summary": "The teardown as a document a product team would read.",
     "inputs": [{"name": "brief", "required": True},
                {"name": "context", "required": False},
                {"name": "audience", "required": False}],
     "agent": "program_manager"},
    {"label": "Email the teardown", "skill_id": "email.send_real_guarded",
     "summary": "Send it to the reviewer, once a person has read it.",
     "inputs": [{"name": "to", "required": True}, {"name": "body", "required": True},
                {"name": "subject", "required": False}],
     "agent": "program_manager"},
]

COMPARE_BRIEF = (
    "The main text is a researched dossier on a competitor; the material is a dossier on our "
    "own product. Compare them under: Positioning, Pricing, Features they have that we lack, "
    "Features we have that they lack, Customers and segments, Recent moves, and Unknowns. "
    "Every claim names the source it rests on. Where the dossiers do not establish something, "
    "write 'not established' — a guessed price or a guessed customer is worse than a gap, "
    "because the team will plan around it. No marketing language."
)

WRITE_BRIEF = (
    "Write a competitive teardown from the comparison below. Open with a five-line summary a "
    "product lead can read in a minute. Then one section per heading in the comparison, "
    "keeping the sources. Close with three questions the team should answer before acting. "
    "Markdown, no preamble."
)


def build_spec(keys: dict[str, str], args) -> dict:
    """The workflow definition, built from the custom step keys the install assigned."""
    return {
        "entry_step": "fork",
        "input_schema": {
            "required": ["competitor", "ours", "to"],
            "properties": {
                "competitor": {"title": "Competitor",
                               "description": "Name and website.",
                               "examples": ["Acme Analytics, acme-analytics.com"]},
                "ours": {"title": "Our product",
                         "description": "Name, website and one line on what it does.",
                         "examples": ["Example Insights, example.com — dashboards for "
                                      "small retailers"]},
                "to": {"title": "Reviewer email", "format": "email",
                       "description": "Who receives the teardown."},
                "compare_brief": {"title": "Comparison brief", "format": "textarea",
                                  "description": "Optional; leave blank for the stored brief.",
                                  "examples": [COMPARE_BRIEF]},
            },
        },
        "steps": [
            # BOTH SIDES AT ONCE. Each branch is BOSS's own research capability: a cited
            # dossier, gated on evidence. The join waits for both before the comparison.
            {"id": "fork", "kind": "parallel", "branches": ["them", "us"], "join": "join"},
            {"id": "them", "kind": "capability",
             "binding": {"capability": "research",
                         "inputs": {"seed": from_input("competitor")}},
             "approval": {"mode": "auto"}, "next": ["join"]},
            {"id": "us", "kind": "capability",
             "binding": {"capability": "research",
                         "inputs": {"seed": from_input("ours")}},
             "approval": {"mode": "auto"}, "next": ["join"]},
            {"id": "join", "kind": "join", "wait_for": ["them", "us"],
             "success_policy": "all", "next": ["compare"]},
            step("compare", keys["Compare the two"],
                 {"system": from_input("compare_brief", default=COMPARE_BRIEF),
                  "prompt": from_step("them", "dossier"),
                  "context": from_step("us", "dossier")},
                 ["write"], agent="program_manager", args=args),
            step("write", keys["Write the teardown"],
                 {"brief": WRITE_BRIEF,
                  "context": from_step("compare", "text"),
                  "audience": "the product team"},
                 ["send"], agent="program_manager", args=args),
            step("send", keys["Email the teardown"],
                 {"to": from_input("to"),
                  "subject": from_input("competitor"),
                  "body": from_step("write", "markdown")},
                 [], agent="program_manager", approval="manual", args=args),
        ],
    }


def main() -> None:
    args = parser("Build the competitive teardown workflow.").parse_args()
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

    competitor   name and website
    ours         our product: name, website, one line
    to           who reviews the teardown

  Two research runs start at once; the comparison waits for both. The last step
  stops and waits — read the teardown before it goes anywhere.
""")


if __name__ == "__main__":
    main()
