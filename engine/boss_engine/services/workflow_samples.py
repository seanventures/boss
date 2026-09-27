"""A real piece of work to demonstrate a workflow with, chosen for the workflow's own kind.

"Start one" on a catalogue card sent this, verbatim:

    {"directive": "Describe the work here", "title": "New work"}

So the panel convened and debated the phrase "Describe the work here", the spec review approved
it, and the builder tried to build it. Every step ran and none of them did anything, which is
worse than the button not existing: it demonstrates the workflow by showing it produce nothing.

Two rules decide what belongs here.

FIRST, THE SAMPLE MUST FIT THE WORK CLASS. `project.new` wants a self-contained product idea.
`feature.minor` wants a small change TO AN APP THAT EXISTS — "build a lead triage inbox" is not
a minor feature, and a minor-feature workflow handed one produces a whole product in a lane
with no research and no spec review. A sample that ignores the class demonstrates the wrong
thing convincingly.

SECOND, WHERE THE CLASS NEEDS SOMETHING REAL, IT MUST COME FROM THE PROJECT. A follow-up
belongs to an app this project actually has. When there is no such app, this returns None and
the caller refuses — starting a run against an invented app is how you get a fifth broken
project card instead of a demonstration.
"""
from __future__ import annotations

import random
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

#: New-product briefs. Each is a whole small product: a real user, a real irritation, and
#: acceptance criteria concrete enough that a panel can argue about scope and a builder can
#: finish. Deliberately unglamorous — the ones that make good demos are the ones somebody would
#: actually use on a Monday.
NEW_PRODUCT_SAMPLES: list[dict[str, Any]] = [
    {
        "title": "Meeting cost meter",
        "directive": (
            "Build a meeting cost meter.\n"
            "Who it is for: a team lead who suspects the weekly sync is not worth it.\n"
            "What it does: paste a meeting title, attendee count and duration, and it shows "
            "the cost in salary-minutes and what else that money buys. Keeps a running total "
            "per week so the trend is visible."
        ),
        "acceptance_criteria": [
            "a lead can enter attendees and duration and see a cost immediately",
            "the weekly total is visible without re-entering anything",
            "costs use a configurable hourly rate, not a hardcoded one",
        ],
    },
    {
        "title": "Broken link sweeper",
        "directive": (
            "Build a broken link sweeper.\n"
            "Who it is for: whoever owns a docs site nobody has audited in a year.\n"
            "What it does: takes a base URL, crawls the pages it links to, and reports every "
            "link that 404s or times out, with the page it was found on so it can be fixed."
        ),
        "acceptance_criteria": [
            "given a URL it reports every broken link with its source page",
            "a slow site does not hang the sweep — timeouts are reported as failures",
            "re-running shows what changed since the last sweep",
        ],
    },
    {
        "title": "On-call handover note",
        "directive": (
            "Build an on-call handover note.\n"
            "Who it is for: the engineer going off shift at 6pm who has three minutes.\n"
            "What it does: collects what happened this shift — incidents, deploys, anything "
            "still open — into one page the next person can read in two minutes, and keeps "
            "the history so a pattern across shifts is visible."
        ),
        "acceptance_criteria": [
            "an engineer can record an event in under thirty seconds",
            "the handover page shows what is still open, separately from what is closed",
            "previous handovers are readable, newest first",
        ],
    },
    {
        "title": "Expense receipt matcher",
        "directive": (
            "Build an expense receipt matcher.\n"
            "Who it is for: a founder reconciling a card statement the night before a deadline.\n"
            "What it does: takes a statement export and a pile of receipts, matches them by "
            "amount and date, and shows exactly which charges have no receipt."
        ),
        "acceptance_criteria": [
            "a statement row with no matching receipt is clearly listed as unmatched",
            "near-matches (same amount, a day apart) are offered rather than silently ignored",
            "matching twice does not double-count a receipt",
        ],
    },
    {
        "title": "Standup blocker board",
        "directive": (
            "Build a standup blocker board.\n"
            "Who it is for: a team whose blockers are mentioned in standup and then forgotten.\n"
            "What it does: records each blocker with who is stuck and who can unstick them, "
            "and shows how long each has been open so the old ones stop being invisible."
        ),
        "acceptance_criteria": [
            "a blocker records who is blocked and who is needed to clear it",
            "age is shown for every open blocker",
            "clearing a blocker keeps it in history rather than deleting it",
        ],
    },
    {
        "title": "Feature flag expiry tracker",
        "directive": (
            "Build a feature flag expiry tracker.\n"
            "Who it is for: a team whose 'temporary' flags are two years old.\n"
            "What it does: records each flag with the date it was meant to be removed and who "
            "owns it, and shows the overdue ones first so cleanup has a worklist."
        ),
        "acceptance_criteria": [
            "every flag has an owner and an intended removal date",
            "overdue flags are listed first and are visually distinct",
            "removing a flag records when and by whom",
        ],
    },
]

#: Small changes. These are phrased as additions to something already built, because that is
#: what the minor-feature lane is for — it has no research step and no spec review.
FOLLOW_UP_SAMPLES: list[str] = [
    "Add a CSV export so the current view can be taken into a spreadsheet. Include the same "
    "columns that are on screen, in the same order.",
    "Add an empty state that explains what to do first, instead of showing a blank list to "
    "somebody who has just arrived.",
    "Add keyboard shortcuts for the two most common actions, and a '?' overlay listing them.",
    "Add a search box that filters the list as you type, and keeps the filter in the URL so a "
    "filtered view can be shared.",
    "Add pagination so the page stays fast once there are a few thousand rows.",
]

#: Research questions, for the discovery lane. A question, not a build instruction.
DISCOVERY_SAMPLES: list[str] = [
    "Who already sells software for tracking on-call handovers, what do they charge, and what "
    "do their reviews say is missing?",
    "What do small teams currently use to reconcile expense receipts, and where does it break "
    "down for them?",
    "Is there a real market for meeting-cost tooling, or is it a novelty people install once?",
]


def _existing_app_names(connection: Connection, project_id: str) -> list[str]:
    """Apps this project has actually built, newest first."""
    try:
        rows = connection.execute(
            text("SELECT DISTINCT ON (finalize_ctx->>'app_id') title "
                 "FROM code_task_runs "
                 "WHERE project_id = :p AND status = 'completed' "
                 "  AND NULLIF(finalize_ctx->>'app_id', '') IS NOT NULL "
                 "ORDER BY finalize_ctx->>'app_id', created_at DESC"),
            {"p": project_id},
        ).scalars().all()
    except Exception:  # noqa: BLE001 — a sampler that cannot read still answers for new work
        return []
    from .naming import project_name

    out = []
    for title in rows:
        name, _blurb, _label = project_name(str(title or ""))
        if name and name != "Untitled app" and name not in out:
            out.append(name)
    return out


def _unused_product(connection: Connection, project_id: str,
                    rng: random.Random) -> Optional[dict[str, Any]]:
    """A product brief this project has not already built.

    Repeating one is not merely untidy: BOSS refuses duplicate work by app identity, so a
    second run of the same idea would be blocked at the build and the demonstration would end
    on an error message.
    """
    from .naming import identity_key

    taken = {identity_key(n) for n in _existing_app_names(connection, project_id)}
    fresh = [s for s in NEW_PRODUCT_SAMPLES if identity_key(s["title"]) not in taken]
    if not fresh:
        return None
    return dict(rng.choice(fresh))


def catalogue_work_class(workflow_key: str) -> str:
    """The class a shipped workflow is for, or "" when the key is the customer's own.

    "Start one" on a workflow the customer made used to read
    ``workflow_definitions.work_class``. That column was never created, so the click
    raised an undefined-column error and the run never began. The class lives on the
    catalogue entry. A key that is not in the catalogue has no class; ``sample_for``
    treats that as a self-contained product brief.
    """
    from .workflow_catalogue import CATALOGUE

    key = (workflow_key or "").strip()
    for entry in CATALOGUE:
        if entry.get("key") == key:
            return str(entry.get("work_class") or "")
    return ""


def sample_for(connection: Connection, project_id: str, work_class: str,
               *, seed: Optional[int] = None) -> Optional[dict[str, Any]]:
    """A runnable input for this work class, or None when the project cannot supply one.

    None is a real answer and the caller must respect it: a minor-feature demonstration needs
    an app to add the feature TO, and inventing one produces a broken project card rather than
    a demonstration.
    """
    rng = random.Random(seed)
    cls = (work_class or "").strip().lower()

    if cls.startswith("project") or cls in ("", "product.new"):
        return _unused_product(connection, project_id, rng)

    if cls.startswith("feature") or cls.startswith("bug") or cls.startswith("fix"):
        apps = _existing_app_names(connection, project_id)
        if not apps:
            return None
        app = rng.choice(apps[:5])          # something recent, so the demo is about live work
        change = rng.choice(FOLLOW_UP_SAMPLES)
        return {
            "title": f"{app} — {change.split('.')[0].strip()}",
            "directive": f"In the existing app '{app}': {change}",
            "acceptance_criteria": [
                "the change works on the existing app without breaking what is there",
                "existing tests still pass, and the new behaviour has its own test",
            ],
        }

    if cls.startswith("research") or cls.startswith("discovery"):
        question = rng.choice(DISCOVERY_SAMPLES)
        return {"title": question[:60].rstrip(" ?") + "?", "directive": question,
                "acceptance_criteria": ["the answer cites its sources"]}

    # An unknown class gets a product brief rather than a placeholder: it is the safest
    # self-contained thing to run, and it is still real work.
    return _unused_product(connection, project_id, rng)


def why_no_sample(work_class: str) -> str:
    """What to tell an operator when this project cannot supply an example."""
    cls = (work_class or "").lower()
    if cls.startswith("feature") or cls.startswith("bug") or cls.startswith("fix"):
        return ("this workflow changes an app that already exists, and this project has not "
                "shipped one yet. Run Standard Product Delivery first, then come back.")
    return ("every built-in example has already been built in this project. Describe the work "
            "yourself in the box above and it will run on this workflow.")
