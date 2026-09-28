#!/usr/bin/env python3
"""Build the "Weekly newsletter" workflow, and optionally schedule it.

An EXAMPLE. Nothing here ships with BOSS and nothing runs it on install.

WHAT IT BUILDS
--------------
    gather        →  write          →  send
    (research)       program_manager    program_manager
    auto             auto               NEEDS A PERSON

`gather` is BOSS's own `research` capability: real web research into a CITED DOSSIER, gated on
evidence. It is a built-in capability step rather than one of your own — which is worth seeing,
because a workflow can mix both.

WITH --schedule IT ALSO ARRIVES ON ITS OWN. Monday 08:00 in a time zone you name, which is the
difference between a demo and something you receive.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from _common import (  # noqa: E402
    connect, define_steps, die, enable_skills, from_input, from_step, grant, parser,
    publish, step,
)

NAME = "Weekly newsletter"
DESCRIPTION = (
    "Research the sectors you care about, write them up as a newsletter, and email it once "
    "you have read it."
)

STEPS = [
    {"label": "Write the newsletter", "skill_id": "model.generate_text",
     "summary": "Turn the research into a newsletter somebody would actually read.",
     "inputs": [{"name": "prompt", "required": True}],
     "agent": "program_manager"},
    {"label": "Email the newsletter", "skill_id": "email.send_real_guarded",
     "summary": "Send it, once a person has read it.",
     "inputs": [{"name": "to", "required": True}, {"name": "body", "required": True}],
     "agent": "program_manager"},
]

HOUSE_BRIEF = (
    "Write a short weekly newsletter from the research below. One section per sector, in the "
    "order given. Three or four items per section, each a sentence or two, with the source "
    "named. Lead each section with whatever actually matters rather than whatever is newest. "
    "If a sector has thin material, say so in one line instead of padding it. Plain text, no "
    "markdown headers, no preamble about being an AI."
)


def main() -> None:
    p = parser("Build the weekly newsletter workflow.")
    p.add_argument("--schedule", metavar="TZ", nargs="?", const="UTC",
                   help="also schedule it for Monday 08:00 in this IANA zone "
                        "(e.g. --schedule Europe/London; bare --schedule means UTC)")
    args = p.parse_args()
    api = connect(args, NAME)
    enable_skills(api, [s["skill_id"] for s in STEPS], args)
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
        print(f"  WORKFLOW\n    would create {NAME!r}")
        if args.schedule:
            print(f"    would schedule it Monday 08:00 {args.schedule}")
        print()
        return

    publish(api, name=NAME, description=DESCRIPTION, args=args, spec={
        "entry_step": "gather",
        "input_schema": {
            "required": ["sectors", "to"],
            "properties": {
                "sectors": {"title": "Sectors to research", "format": "textarea",
                            "description": "Topics to cover, in the order you want them."},
                "brief": {"title": "Writing brief", "format": "textarea",
                          "description": "Optional; leave blank for the stored house brief.",
                          "examples": [HOUSE_BRIEF]},
                "to": {"title": "Recipient email", "description": "Who receives the approved newsletter."},
                "subject": {"title": "Email subject",
                            "description": "Optional; defaults to Your weekly newsletter."},
            },
        },
        "steps": [
            # A BUILT-IN CAPABILITY, not a step of your own. `research` produces a cited
            # dossier and is gated on real evidence — a sector with thin news comes back
            # sparse rather than padded, which is correct and will look empty.
            {"id": "gather", "kind": "capability",
             "binding": {"capability": "research",
                         "inputs": {"seed": from_input("sectors")}},
             "approval": {"mode": "auto"}, "next": ["write"]},
            step("write", keys["Write the newsletter"],
                 {"prompt": from_input("brief", default=HOUSE_BRIEF),
                  # The dossier arrives as `context` because a step wires whole values and
                  # cannot build "write from: {dossier}" out of two of them.
                  "context": from_step("gather", "dossier")},
                 ["send"], agent="program_manager", args=args),
            # email.send_real_guarded is tier 2 AND requires an approval in its own policy, so
            # this stops twice over. A newsletter is the one thing you want to read first.
            step("send", keys["Email the newsletter"],
                 {"to": from_input("to"),
                  "subject": from_input("subject", default="Your weekly newsletter"),
                  "body": from_step("write", "text")},
                 [], agent="program_manager", approval="manual", args=args),
        ],
    })

    if args.schedule:
        schedule(api, args)

    print(f"""  Done.

  Start a run: Delivery → Workflows → "{NAME}" → Start one.

    sectors   what to research, as one line — "UK politics, Premier League football,
              Chennai local news, AI industry moves"
    brief     how to write it. Leave blank to use the house brief this script set.
    to        where it goes
    subject   the email subject

  The send step stops and waits. Read it first — that is the point of a newsletter
  you did not write.
""")


def schedule(api, args) -> None:
    """Monday 08:00, in the zone the operator named.

    The sectors and the brief are stored WITH the schedule, so the Monday run needs nobody to
    type anything. A one-off run can still override them — which is why they are workflow
    inputs rather than baked into the steps.
    """
    print("  SCHEDULE")
    code, body = api("POST", "/v1/delivery-workflows/schedules", {
        "project_id": api.project, "actor": api.actor,
        "workflow_key": NAME.lower().replace(" ", "_"),
        "name": f"{NAME} — Monday",
        "cadence": "weekly", "weekday": 0, "hour": 8, "minute": 0,
        "timezone": args.schedule,
        "input": {
            "sectors": "world news, technology, business",
            "brief": HOUSE_BRIEF,
            "to": "",          # filled in by the operator; the send step will refuse without it
            "subject": "Your weekly newsletter",
        },
    })
    if code == 400 and "already" in str(body.get("detail", "")).lower():
        print(f"    already scheduled — {body.get('detail')}")
        return
    if code >= 400:
        die(f"could not schedule it: {body.get('detail') or body}")
    print(f"    {body.get('describes')}")
    print(f"    next run {body.get('next_run_at')}")
    print("    NOTE: set `to` on the schedule before Monday, or the send step will refuse.")
    print()


if __name__ == "__main__":
    main()
