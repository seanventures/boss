#!/usr/bin/env python3
"""Build the "Oncall handoff" workflow, and optionally schedule it.

An EXAMPLE. Nothing here ships with BOSS and nothing runs it on install. README.md does the
same thing by hand in the console.

WHAT IT BUILDS
--------------
    gather  →  send
    sre        NEEDS A PERSON

The SRE agent reads BOSS itself — the runs that failed, the decisions still waiting, the
deployments and their health, what is running now — over the handoff period, adds the notes
the outgoing on-call typed, and writes the handoff. It is emailed to the incoming on-call once a
person approves.

WHAT IT READS IS WHAT BOSS KNOWS. The agent's tools are BOSS's own read tools (run logs,
decisions, deployments, health). It is not a pager and reads no alerting system; if your
on-call is about services BOSS does not run, the `notes` input is where that goes.

WITH --schedule IT PREPARES THE HANDOFF EVERY MONDAY. The send step still waits for a person.
"""
import sys
from email.utils import parseaddr
from pathlib import Path
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from _common import (  # noqa: E402
    agent_step, connect, define_steps, die, enable_skills, from_input, from_step, grant,
    parser, publish, step,
)

NAME = "Oncall handoff"
DESCRIPTION = (
    "The SRE agent reads what BOSS ran, what failed and what is still waiting over the "
    "handoff period, and writes the handoff — emailed to the incoming on-call once you approve."
)

GATHERER = "sre"
#: BOSS's own read tools, as one grantable skill. Every agent holds it already; granting is
#: idempotent and makes the dependency explicit.
GATHERER_SKILLS = ["boss.fundamentals"]

STEPS = [
    {"label": "Email the handoff", "skill_id": "email.send_real_guarded",
     "summary": "Send the handoff to the incoming on-call, once a person has read it.",
     "inputs": [{"name": "to", "required": True}, {"name": "body", "required": True},
                {"name": "subject", "required": False}],
     "agent": GATHERER},
]

GATHER_BRIEF = (
    "Write the on-call handoff for the period in `period`. Using the BOSS read tools you hold, "
    "look at: workflow runs that failed or were dead-lettered, and why; decisions still waiting "
    "for a person; deployments made and their current health; anything running right now that "
    "started before the period ended. Add whatever the outgoing on-call wrote in `notes`. "
    "Write `handoff` as plain text under the headings: Still open, What broke and what was "
    "done, Deployed this period, Watch for, Notes from the outgoing on-call. Every item names "
    "the run, decision or deployment it refers to. Write `open_items` as a JSON list of the "
    "things the incoming on-call has to act on, one line each. Report only what the tools "
    "returned; if a tool could not be read, say so under Watch for."
)


def build_spec(keys: dict[str, str], args) -> dict:
    """The workflow definition, built from the custom step keys the install assigned."""
    return {
        "entry_step": "gather",
        "input_schema": {
            "required": ["to"],
            "properties": {
                "period": {"title": "Handoff period",
                           "description": "Optional; defaults to the last 7 days.",
                           "examples": ["the last 7 days"]},
                "notes": {"title": "Outgoing on-call notes", "format": "textarea",
                          "description": "Optional. Anything BOSS cannot see: pages, vendor "
                                         "tickets, things to watch."},
                "to": {"title": "Incoming on-call email", "format": "email",
                       "description": "Who receives the handoff."},
                "gather_brief": {"title": "Handoff brief", "format": "textarea",
                                 "description": "Optional; leave blank for the stored brief.",
                                 "examples": [GATHER_BRIEF]},
            },
        },
        "steps": [
            agent_step("gather", from_input("gather_brief", default=GATHER_BRIEF), ["send"],
                       agent=GATHERER, args=args,
                       produces=["handoff", "open_items"],
                       context={"period": from_input("period", default="the last 7 days"),
                                "notes": from_input("notes", default="")}),
            step("send", keys["Email the handoff"],
                 {"to": from_input("to"),
                  "subject": "On-call handoff",
                  "body": from_step("gather", "handoff")},
                 [], agent=GATHERER, approval="manual", args=args),
        ],
    }


def main() -> None:
    p = parser("Build the on-call handoff workflow.")
    p.add_argument("--schedule", metavar="TZ", nargs="?", const="UTC",
                   help="also schedule it for Monday 09:00 in this IANA zone "
                        "(e.g. --schedule Europe/London; bare --schedule means UTC)")
    p.add_argument("--to", default="", metavar="EMAIL",
                   help="the incoming on-call address for scheduled runs")
    args = p.parse_args()
    args.to = args.to.strip()
    if args.schedule and not args.to:
        die("--schedule needs --to. The recurring run stores the recipient when it is "
            "created; it cannot be filled in afterward.")
    if args.schedule:
        display_name, address = parseaddr(args.to)
        if display_name or address != args.to or "@" not in address:
            die("--to must be one email address, such as oncall@example.com.")
    api = connect(args, NAME)
    existing_schedule = find_schedule(api, args) if args.schedule else None
    enable_skills(api, sorted({s["skill_id"] for s in STEPS} | set(GATHERER_SKILLS)), args)
    grant(api, GATHERER, sorted(set(GATHERER_SKILLS) | {s["skill_id"] for s in STEPS}), args)
    keys = define_steps(api, STEPS, args)
    if args.dry_run:
        print(f"  WORKFLOW\n    would create {NAME!r}")
        if args.schedule:
            if existing_schedule:
                print("    schedule already exists with these inputs")
            else:
                print(f"    would schedule it Monday 09:00 {args.schedule} for {args.to}")
        print()
        return

    publish(api, name=NAME, description=DESCRIPTION, args=args, spec=build_spec(keys, args))

    if args.schedule:
        if existing_schedule:
            print("  SCHEDULE\n    unchanged — the existing schedule keeps its enabled state.\n")
        else:
            schedule(api, args)

    print(f"""  Done.

  Start a run: Workflows → Library → "{NAME}" → ⋯ → Start.

    period   optional — defaults to the last 7 days
    notes    optional — what BOSS cannot see
    to       the incoming on-call

  The send step stops and waits. Read the handoff before it goes.
""")


def find_schedule(api, args) -> dict | None:
    """A rerun must never duplicate the schedule or silently keep a stale recipient."""
    query = urlencode({"project_id": api.project, "actor": api.actor})
    code, body = api("GET", f"/v1/delivery-workflows/schedules?{query}")
    if code >= 400:
        die(f"could not read this project's schedules: {body.get('detail') or body}")
    name = f"{NAME} — Monday"
    existing = next((item for item in body.get("schedules", [])
                     if item.get("name") == name), None)
    if existing is None:
        return None
    inputs = existing.get("input") or {}
    same = (existing.get("workflow_key") == NAME.lower().replace(" ", "_")
            and existing.get("cadence") == "weekly"
            and existing.get("weekday") == 0
            and existing.get("hour") == 9
            and existing.get("minute") == 0
            and existing.get("timezone") == args.schedule
            and inputs.get("to") == args.to)
    if not same:
        die("a Monday handoff schedule already exists with a different recipient or timing. "
            "Remove it in Workflows → Library → ⋯ → Schedule…, then run this setup again.")
    return existing


def schedule(api, args) -> None:
    """Monday 09:00, in the zone the operator named."""
    print("  SCHEDULE")
    code, body = api("POST", "/v1/delivery-workflows/schedules", {
        "project_id": api.project, "actor": api.actor,
        "workflow_key": NAME.lower().replace(" ", "_"),
        "name": f"{NAME} — Monday",
        "cadence": "weekly", "weekday": 0, "hour": 9, "minute": 0,
        "timezone": args.schedule,
        "input": {"period": "the last 7 days", "to": args.to},
    })
    if code >= 400:
        die(f"could not schedule it: {body.get('detail') or body}")
    print(f"    {body.get('describes')}")
    print(f"    next run {body.get('next_run_at')}")
    print("    The send step waits for your approval before email delivery.")
    print()


if __name__ == "__main__":
    main()
