#!/usr/bin/env python3
"""Build the "Support ticket triage" workflow on a BOSS install.

An EXAMPLE. Nothing here ships with BOSS and nothing runs it on install. README.md does the
same thing by hand in the console.

WHAT IT BUILDS
--------------
    split  →  each ──(reply, once per ticket)──→  digest  →  send
    program_manager   program_manager              program_manager   NEEDS A PERSON

Paste a batch of support tickets. An agent splits them into a list; the workflow then runs ONE
step once per ticket — category, priority, and a draft reply — a few at a time. The results
are collected into a digest for the support lead, who gets it by email once a person approves.

NOTHING IS SENT TO A CUSTOMER. The drafts go to your support lead, who edits and sends the
ones worth sending from the help desk. BOSS has no help-desk connector to read tickets from or
to reply through; that is why the tickets are pasted and the replies are drafts.

WHY FOR-EACH. The number of tickets is not known when the workflow is published, so a fixed
step list cannot handle "one reply per ticket". A `for_each` runs its body once per item and
finishes with every result, in order.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from _common import (  # noqa: E402
    agent_step, connect, define_steps, enable_skills, from_input, from_step, grant, parser,
    publish, step,
)

NAME = "Support ticket triage"
DESCRIPTION = (
    "Paste a batch of support tickets. BOSS categorises and prioritises each one and drafts a "
    "reply, then emails the digest to your support lead once you approve. Nothing goes to a "
    "customer."
)

TRIAGER = "program_manager"

STEPS = [
    {"label": "Write the digest", "skill_id": "model.generate_text",
     "summary": "One table of every ticket with category, priority and the draft reply.",
     "inputs": [{"name": "prompt", "required": True},
                {"name": "context", "required": False}],
     "agent": TRIAGER},
    {"label": "Email the support lead", "skill_id": "email.send_real_guarded",
     "summary": "Send the digest to the support lead, once a person has read it.",
     "inputs": [{"name": "to", "required": True}, {"name": "body", "required": True},
                {"name": "subject", "required": False}],
     "agent": TRIAGER},
]

SPLIT_BRIEF = (
    "`tickets` is a batch of support tickets pasted as text. Split it into individual tickets. "
    "Return `tickets` as a JSON list, one object per ticket: {reference, customer, subject, "
    "text}. Use the reference and customer as written; where one is not stated, use an empty "
    "string. Keep each ticket's text complete and unchanged. Do not merge tickets, do not "
    "drop any, do not add any."
)

REPLY_BRIEF = (
    "`ticket` is one support ticket; `product` is what we support. Decide `category` as one "
    "of: bug, how-to, billing, feature-request, account, other. Decide `priority` as urgent, "
    "high, normal or low — urgent only for an outage, data loss or a security concern. Write "
    "`draft` as the reply a support person could send with light edits: answer what was asked, "
    "say what happens next, and ask for exactly what is missing if something is. Do not promise "
    "a fix date, a refund or a feature; do not invent product behaviour you are not sure of — "
    "say the team will confirm."
)

DIGEST_BRIEF = (
    "The material is a JSON list of triaged support tickets, each with category, priority and "
    "a draft reply. Write a digest for the support lead: a count by priority, then one entry "
    "per ticket in priority order — reference, customer, category, priority, one line on the "
    "issue, then the draft reply indented. Plain text, no preamble. Do not change the drafts."
)


def build_spec(keys: dict[str, str], args) -> dict:
    """The workflow definition, built from the custom step keys the install assigned."""
    return {
        "entry_step": "split",
        "input_schema": {
            "required": ["tickets", "to"],
            "properties": {
                "tickets": {"title": "Tickets", "format": "textarea",
                            "description": "The batch, pasted. Separate tickets with a blank "
                                           "line or a reference such as #1234."},
                "product": {"title": "Product context", "format": "textarea",
                            "description": "Optional. What we support and how it behaves — "
                                           "anything the drafts should get right."},
                "to": {"title": "Support lead email", "format": "email",
                       "description": "Who receives the digest. Never a customer."},
                "reply_brief": {"title": "Reply brief", "format": "textarea",
                                "description": "Optional; leave blank for the stored brief.",
                                "examples": [REPLY_BRIEF]},
            },
        },
        "steps": [
            agent_step("split", SPLIT_BRIEF, ["each"], agent=TRIAGER, args=args,
                       produces=["tickets"],
                       context={"tickets": from_input("tickets")}),
            # ONE STEP, ONCE PER TICKET. `items` reads the list the split step declared;
            # `results` on this step is every reply, in ticket order. Two at a time, and no
            # more than fifty: a longer batch is refused with the count, never cut short.
            {"id": "each", "kind": "for_each", "items": "steps.split.output.tickets",
             "body": "reply", "max_items": 50, "concurrency": 2, "next": ["digest"]},
            agent_step("reply", from_input("reply_brief", default=REPLY_BRIEF), [],
                       agent=TRIAGER, args=args,
                       produces=["category", "priority", "draft"],
                       context={"ticket": {"from_item": ""},
                                "position": {"from_item_index": True},
                                "product": from_input("product", default="")}),
            step("digest", keys["Write the digest"],
                 {"prompt": DIGEST_BRIEF,
                  "context": from_step("each", "results")},
                 ["send"], agent=TRIAGER, args=args),
            step("send", keys["Email the support lead"],
                 {"to": from_input("to"),
                  "subject": "Support ticket triage",
                  "body": from_step("digest", "text")},
                 [], agent=TRIAGER, approval="manual", args=args),
        ],
    }


def main() -> None:
    args = parser("Build the support ticket triage workflow.").parse_args()
    api = connect(args, NAME)
    enable_skills(api, sorted({s["skill_id"] for s in STEPS}), args)
    grant(api, TRIAGER, sorted({s["skill_id"] for s in STEPS}), args)
    keys = define_steps(api, STEPS, args)
    if args.dry_run:
        print(f"  WORKFLOW\n    would create {NAME!r}\n")
        return

    publish(api, name=NAME, description=DESCRIPTION, args=args, spec=build_spec(keys, args))

    print(f"""  Done.

  Start a run: Workflows → Library → "{NAME}" → ⋯ → Start.

    tickets   the batch, pasted
    product   optional — what we support, so the drafts get it right
    to        the support lead — never a customer

  Each ticket is triaged as its own step run; open the for-each step to see them.
  The last step stops and waits. Nothing is sent to anyone until you approve it.
""")


if __name__ == "__main__":
    main()
