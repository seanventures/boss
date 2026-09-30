"""What to ask THIS member of the workforce, as opposed to what to ask anybody.

Every chat surface offered the same three prompts — "What changed today?", "Which runs failed
and why?", "What is this run waiting on?" — including the panel that had just announced you were
talking to the Engineering Captain. They are status questions. They are the questions you ask a
dashboard, and a dashboard is exactly what a named expert is not: the reason to open a
conversation with Security rather than read a page is that Security has a lens, a remit, and a
record of what it has already argued on this project.

So a starter here has to satisfy two things.

IT MUST ONLY MAKE SENSE FOR THIS ROLE. If QA and Finance could both be asked it, it belongs on
the dashboard, not here. The test is blunt and it removes most generic phrasings.

IT MUST NAME SOMETHING THAT EXISTS. "Which of Hookwatch's ports are reachable from outside?"
is answerable; "which of your services are exposed?" invites the model to invent a service. The
project's real apps, its last failure and this role's own recorded positions are read first, and
a starter that has nothing real to fill in is dropped rather than asked hypothetically.

The role families below are matched on WORDS in the role's title and lens, not on a fixed list
of keys. Projects define their own roles — "Engineering Captain" is not in ROLE_CATALOG — and a
starter set that only works for the eleven built-in roles would be blank exactly where an
operator has bothered to build a workforce of their own.
"""
from __future__ import annotations

from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

#: A role family, the words that identify it, and what only that family would ask.
#:
#: `{app}` is filled with a real app from this project and `{run}` with a real failed run. A
#: template whose placeholder cannot be filled is dropped — see `_fill`.
FAMILIES: list[dict[str, Any]] = [
    {
        "family": "security",
        "words": ("security", "threat", "appsec", "infosec"),
        "asks": [
            "Which ports does {app} have open, and should any of them be reachable from "
            "outside?",
            "Where does {app} keep its secrets, and who or what can read them?",
            "What is the most likely way someone abuses {app}, and what stops them today?",
            "Which controls have I asked for on this project that never got built?",
        ],
    },
    {
        "family": "qa",
        "words": ("qa", "test", "quality", "verification"),
        "asks": [
            "Which of {app}'s acceptance criteria have no test behind them?",
            "What did the tests miss on {run}, and would they catch it a second time?",
            "Which tests on this project pass without actually asserting anything?",
            "If I had one hour to break {app}, where would I start?",
        ],
    },
    {
        "family": "operations",
        "words": ("operation", "deploy", "sre", "reliability", "infrastructure", "platform"),
        "asks": [
            "If {app} broke at 3am, what would tell me — and what would stay invisible?",
            "Can {app} be rolled back right now, and what exactly happens when I do it?",
            "Which deployed apps on this project have no metrics worth looking at?",
            "What is the first diagnostic step when {app} stops answering?",
        ],
    },
    {
        "family": "product",
        "words": ("product", "pm ", "product manager", "outcome"),
        "asks": [
            "Is {app} solving the problem we scoped, or the part of it that was easy to build?",
            "What did we cut from {app}, and does any of it still matter?",
            "What would make me say {app} succeeded — in a number, not an adjective?",
            "Which of this project's apps has no one asked for since it shipped?",
        ],
    },
    {
        "family": "ux",
        "words": ("ux", "design", "experience", "usability", "accessib"),
        "asks": [
            "Where does a first-time user of {app} get stuck?",
            "What does {app} show someone before they have any data in it?",
            "Which part of {app} would fail an accessibility review today?",
            "What in {app} did we build because it was easy to render rather than easy to use?",
        ],
    },
    {
        "family": "engineering",
        "words": ("engineer", "architect", "captain", "tech lead", "developer", "principal"),
        "asks": [
            "What in {app} will be expensive to change in six months?",
            "What actually caused {run} to fail — not what the error said?",
            "Which decision on this project are we still paying for?",
            "Where is {app} carrying complexity it does not need?",
        ],
    },
    {
        # ADVERSARIAL REVIEWER AND CONSISTENCY REVIEWER MATCHED NOTHING, so two of the roles
        # that actually run on every build fell through to the generic asks — which is how the
        # operator came to be offered "What is your biggest concern about this project?" by a
        # reviewer that had just read their code.
        "family": "review",
        "words": ("review", "critic", "adversar", "red team", "consistency", "auditor"),
        "asks": [
            "What did you flag on {app} that got shipped anyway?",
            "What would you attack first in {app}, and would the tests notice?",
            "Which of your findings on this project were closed without being fixed?",
            "What did {run} get wrong that a review should have caught before it ran?",
        ],
    },
    {
        "family": "finance",
        "words": ("finance", "finops", "cost", "budget"),
        "asks": [
            "What has {app} cost to build so far, and was it worth it?",
            "Which run on this project burned the most and returned the least?",
            "What is the cheapest thing we could stop doing without anyone noticing?",
        ],
    },
    {
        "family": "data",
        "words": ("data", "analytics", "measurement", "insight"),
        "asks": [
            "What is {app} instrumented to prove, and what is it silent about?",
            "Which number on this project do we quote without being able to verify it?",
            "What would we need to record now to answer next month's question?",
        ],
    },
    {
        "family": "growth",
        "words": ("growth", "marketing", "adoption", "positioning"),
        "asks": [
            "Who would actually use {app}, and how would they ever find it?",
            "What does {app} do that is worth telling somebody about?",
            "Which of this project's apps could be sold, and which is a demo?",
        ],
    },
    {
        "family": "privacy",
        "words": ("privacy", "safety", "compliance", "legal", "ethic"),
        "asks": [
            "What personal data does {app} touch, and how long does it keep it?",
            "What is the worst thing {app} could do to a user by accident?",
            "Which consent or retention question on this project has never been answered?",
        ],
    },
    {
        "family": "customer",
        "words": ("customer", "advocate", "support", "success"),
        "asks": [
            "What will people complain about first with {app}?",
            "What does {app} do when it fails, from the user's side?",
            "Which of this project's apps would create support work nobody is staffed for?",
        ],
    },
]

#: Asked of any role when its family has nothing left to offer. These are still ROLE questions —
#: they are about this role's own judgement and record — rather than status questions the
#: console already answers on a page.
#: "THIS PROJECT" AND "HERE" NAME NOTHING, which is the failure this module's own rule forbids
#: two paragraphs above. Worse, "project" means something specific in BOSS — the workspace that
#: CONTAINS the apps — so a reviewer that had just read one app's code was asked about a
#: container holding six of them, and the operator quite reasonably asked what "this project"
#: was supposed to mean.
#:
#: Grounded first: `{app}` is a real app and `_fill` drops the template when there is not one.
GENERIC_ROLE_ASKS: list[str] = [
    "What is your biggest concern about {app} right now?",
    "What have you argued for on {app} that the build did not honour?",
    "What would you refuse to sign off on today?",
]

#: When the project has built nothing yet there is no app to name — so say THAT, rather than
#: gesturing at a project the operator has to interpret.
NOTHING_BUILT_ASKS: list[str] = [
    "Nothing has been built in this workspace yet — what will you be looking for in the first "
    "thing that is?",
    "What would you refuse to sign off on, whatever it turned out to be?",
]


def _families_for(title: str, lens: str) -> list[dict[str, Any]]:
    """Which families this role belongs to, most specific first.

    Matched on the title FIRST and the lens second: a role called "Security" is a security role
    whatever its lens says, while a custom role's title ("Engineering Captain") often names no
    family at all and only the lens reveals what it is there to do.
    """
    low_title = f" {title.lower()} "
    low_lens = lens.lower()
    by_title = [f for f in FAMILIES if any(w in low_title for w in f["words"])]
    by_lens = [f for f in FAMILIES
               if f not in by_title and any(w in low_lens for w in f["words"])]
    return by_title + by_lens


#: Names that are not names. A run whose brief was never written keeps the catalogue's default
#: title, and it sails through every "is this a real app?" check because it IS a real row.
#:
#: Observed live: the Security starter came back as "Which ports does New work have open?" —
#: with HookWatch, Vendor-Status-Watcher and a licence-provisioning flow sitting directly
#: behind it in the same list. A starter naming a placeholder is worse than a generic one: it
#: sounds specific, so the operator believes there is an app called New work.
_NOT_A_NAME = frozenset({
    "new work", "untitled app", "untitled", "new app", "new project",
    "describe the work here", "sample", "test", "demo",
})

#: A follow-up run's title names a piece of REWORK, not an app — "Follow-up: deferred review
#: findings — HookWatch". The app it refers to has its own row; using this one produces a
#: question addressed to a sentence.
_NOT_AN_APP_PREFIXES = ("follow-up:", "follow‐up:", "re-build of", "rebuild of")


def _usable_name(title: str) -> Optional[str]:
    """The display name of the app this run built, or None if the title names no app."""
    from .naming import project_name

    raw = " ".join(str(title or "").split())
    low = raw.lower()
    if any(low.startswith(prefix) for prefix in _NOT_AN_APP_PREFIXES):
        return None
    name, _blurb, _label = project_name(raw)
    if not name or name.strip().lower() in _NOT_A_NAME:
        return None
    return name


def _recent_app(connection: Connection, project_id: str) -> Optional[str]:
    """An app this project actually built, by display name."""
    try:
        rows = connection.execute(
            text("SELECT title FROM code_task_runs "
                 "WHERE project_id = :p AND status = 'completed' "
                 "  AND NULLIF(finalize_ctx->>'app_id', '') IS NOT NULL "
                 # Deep enough that a run of placeholders and follow-ups at the top cannot
                 # starve the list — the real apps were four rows down when this was found.
                 "ORDER BY created_at DESC LIMIT 25"),
            {"p": project_id},
        ).scalars().all()
    except Exception:  # noqa: BLE001 — starters are a convenience; never fail a page for them
        return None
    for title in rows:
        name = _usable_name(str(title or ""))
        if name:
            return name
    return None


def _recent_failure(connection: Connection, project_id: str) -> Optional[str]:
    """The last run that did not ship, named the way the console names it."""
    try:
        rows = connection.execute(
            text("SELECT id, title FROM code_task_runs "
                 "WHERE project_id = :p AND status IN ('failed', 'blocked', 'error') "
                 "ORDER BY created_at DESC LIMIT 10"),
            {"p": project_id},
        ).mappings().all()
    except Exception:  # noqa: BLE001
        return None
    for row in rows:
        name = _usable_name(str(row["title"] or ""))
        if name:
            return name
    # A run with no usable title is still a real failure, so the question stays — but it is
    # asked by description rather than by id. "What actually caused crun_734b070b10ce48d6b71f
    # to fail" is grounded and unreadable; a starter is an invitation, and nobody is invited by
    # a 24-character token. The phrase resolves to exactly one run, and the role has the tools
    # to find it.
    return "the last failed run" if rows else None


def _last_position(connection: Connection, project_id: str, title: str) -> Optional[str]:
    """The most recent thing THIS role argued on a panel here, in its own words.

    This is the starter that no other role could be asked, and the one an operator cannot get
    from any page: the console shows meeting minutes as a whole, not one member's line through
    them.
    """
    try:
        rows = connection.execute(
            text("SELECT state FROM meeting_runs "
                 "WHERE project_id = :p AND state ? 'round1' "
                 "ORDER BY created_at DESC LIMIT 6"),
            {"p": project_id},
        ).scalars().all()
    except Exception:  # noqa: BLE001
        return None
    for state in rows:
        round1 = (state or {}).get("round1") if isinstance(state, dict) else None
        if not isinstance(round1, dict):
            continue
        for key, value in round1.items():
            if str(key).strip().lower() != title.strip().lower():
                continue
            said = value if isinstance(value, str) else str((value or {}).get("text", ""))
            first = " ".join(said.split())
            if len(first) > 20:
                return first[:110].rstrip(" ,.;")
    return None


def _fill(template: str, *, app: Optional[str], run: Optional[str]) -> Optional[str]:
    """Fill a template, or drop it.

    A starter with an unfilled placeholder is worse than one fewer starter: "Which of {app}'s
    ports are open?" reads as a bug, and "which of your apps' ports are open?" invites the model
    to answer about an app that does not exist.
    """
    out = template
    if "{app}" in out:
        if not app:
            return None
        out = out.replace("{app}", app)
    if "{run}" in out:
        if not run:
            return None
        out = out.replace("{run}", run)
    return out


def role_starters(connection: Connection, project_id: str, role: str,
                  *, limit: int = 4) -> list[str]:
    """Questions worth asking this role on this project. Never more than `limit`.

    Ordered by how much of this role's own record they draw on: the position it took here comes
    first, then its family's questions about real apps, then the honest generic ones.
    """
    role = (role or "").strip()
    if not role:
        return []
    try:
        row = connection.execute(
            text("SELECT title, lens FROM meeting_roles "
                 "WHERE project_id = :p "
                 "  AND (lower(role_key) = lower(:r) OR lower(title) = lower(:r)) LIMIT 1"),
            {"p": project_id, "r": role},
        ).mappings().first()
    except Exception:  # noqa: BLE001
        row = None
    title = str((row or {}).get("title") or role)
    lens = str((row or {}).get("lens") or "")

    app = _recent_app(connection, project_id)
    run = _recent_failure(connection, project_id)

    out: list[str] = []
    position = _last_position(connection, project_id, title)
    if position:
        out.append(f"You argued: “{position}” — did the build honour that?")

    for family in _families_for(title, lens):
        for template in family["asks"]:
            filled = _fill(template, app=app, run=run)
            if filled and filled not in out:
                out.append(filled)
            if len(out) >= limit:
                return out[:limit]

    for template in GENERIC_ROLE_ASKS:
        ask = _fill(template, app=app, run=run)
        if ask and ask not in out:
            out.append(ask)
        if len(out) >= limit:
            break
    # NOTHING TO NAME MEANS SAY SO, not gesture at a project the operator has to interpret.
    # The role-judgement asks still fill without an app, so this is not "when out is empty" —
    # it is "when there is no app", which is the case that needs the scope stated.
    if not app:
        out = [ask for ask in NOTHING_BUILT_ASKS if ask not in out] + out
    return out[:limit]
