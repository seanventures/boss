"""Who is on this piece of work.

BOSS could not answer that. `roles` says an agent exists in a project; a workflow step names an
`approver_role` and nothing else; `workflow_runs` and `workflow_step_runs` have no role column
at all. The only implicit assignment is `llm_telemetry.STAGE_ROLES`, which maps a pipeline stage
to a role for COST ATTRIBUTION at write time and is never consulted for scheduling or for
permission. So the console listed 26 agents and could not say which of them were on the app in
front of you, and a workflow ran whatever agent its stage happened to imply.

TWO DECISIONS WORTH STATING.

Assignment is keyed on the APP, not on a run. An app gets re-run, retried and re-deployed, and
an assignment that evaporated on the second run would be worse than no assignment — the
operator would set it, watch it work, and find it gone. `app_key` is `identity_key` of the app's
name, the same comparison the rest of BOSS uses to decide two names are one app.

Assignment is ENFORCED, and it therefore ships with defaults. If a project started with nobody
assigned, every run would refuse and the feature would read as breakage rather than governance.
So a new app is seeded with exactly the roles that would have done the work anyway — the
STAGE_ROLES set plus whatever the workflow names as an approver — and enforcement applies on top
of that. Assignment then CHANGES behaviour without breaking the default path, which is the only
version of enforcement worth having.
"""

from __future__ import annotations

import logging
from typing import Any, Iterable, Optional

from sqlalchemy import text as sql

from .core import make_id
from .naming import identity_key, project_name

log = logging.getLogger(__name__)

#: Where a row came from. `default` was seeded; `operator` was chosen by a person, and is never
#: removed by re-seeding. `builtin` is not stored at all — it is what BOSS runs when nothing has
#: been recorded, computed on read.
DEFAULT, OPERATOR, BUILTIN = "default", "operator", "builtin"

#: The project's own default team, kept as an assignment against this pseudo-app.
#:
#: The Command Center's Build does not create a project — it sends the directive down panel →
#: PRD → code — so at the moment of pressing it there is no app to assign anyone to. The choice
#: that IS available there is the same shape as the workflow picker beside it: what this project
#: does by default. Per-app customisation then happens on the run, where an app exists.
PROJECT_DEFAULT = "*"


def app_key(app_name: str) -> str:
    """The key an app is filed under, from any of the names it goes by.

    THE MISMATCH THAT MADE EVERY SEEDED ASSIGNMENT INVISIBLE. `start_run` seeded using the run's
    raw input title — "Build a pricing experiment log.\nWho it is for: a team changing prices…"
    — and the console asked for the app by its DISPLAY name, "Pricing Experiment Log", because
    that is what `appIdentity` gives every other card. Two different identity keys for one app:

        raw title  -> "build a pricing experiment log who it is for a team changing prices"
        display    -> "pricing experiment log"

    So the row was written and never found, and the panel reported "nobody is assigned" over a
    live run that had seven agents seeded onto it. Normalising HERE rather than at each call site
    is the fix: every caller converges on the same key whichever name it happens to hold.
    """
    name = str(app_name or "").strip()
    if name == PROJECT_DEFAULT:
        return PROJECT_DEFAULT
    return identity_key(project_name(name)[0] if name else "")


def _names_anybody(spec: Optional[dict[str, Any]]) -> bool:
    """Whether this workflow says who should work on it, in any step."""
    for step in ((spec or {}).get("steps") or []):
        if not isinstance(step, dict):
            continue
        if str(step.get("captain") or "").strip():
            return True
        if str((step.get("approval") or {}).get("approver_role") or "").strip():
            return True
    return False


def default_roles(spec: Optional[dict[str, Any]] = None) -> list[str]:
    """The agents that will actually work on this run.

    WHEN THE WORKFLOW SAYS WHO, THE WORKFLOW WINS.
    ----------------------------------------------
    This used to answer with STAGE_ROLES — the map BOSS attributes model calls to — for every
    run of every workflow. That is the honest answer for the software factory, whose stages
    ARE those roles. It is nonsense anywhere else: a video workflow whose three steps name
    `video_editor` and `media_publisher` seeded an Adversarial Reviewer, a Chief Architect, a
    QA Lead, a Security Reviewer, a Software Engineer and a UX Engineer, and named neither of
    the two agents actually doing the work.

    That is not cosmetic. The panel under a run reads "Only these agents may sign off this
    app's gates. Anyone else is refused" — so the seeding decides who can approve, and it was
    seeding six agents with no connection to the work while omitting the two doing it.

    A step names its agent in `captain`; a gate names one in `approval.approver_role`. Where a
    spec names anybody at all, those are the agents on the run. STAGE_ROLES remains the answer
    for a workflow that names nobody, which is every catalogue one.
    """
    from .llm_telemetry import STAGE_ROLES, canonical_role

    named: set[str] = set()
    for step in ((spec or {}).get("steps") or []):
        if not isinstance(step, dict):
            continue
        captain = str(step.get("captain") or "").strip()
        if captain:
            named.add(captain)
        approver = str((step.get("approval") or {}).get("approver_role") or "").strip()
        if approver:
            named.add(approver)
    if named:
        return sorted(r for r in named if r)

    roles = {canonical_role(r) for r in STAGE_ROLES.values()}
    return sorted(r for r in roles if r)


def project_team(connection, project_id: str) -> list[str]:
    """The project's default team, or BOSS's built-in set if nobody has chosen one."""
    rows = connection.execute(
        sql("SELECT role_key FROM project_agents WHERE project_id = :p AND app_key = :a "
            "ORDER BY role_key"),
        {"p": project_id, "a": PROJECT_DEFAULT},
    ).mappings().all()
    return [r["role_key"] for r in rows] or default_roles()


def effective(connection, project_id: str, app_key: str) -> list[dict[str, Any]]:
    """Who runs this app — recorded if anything was, and otherwise who runs it ANYWAY.

    "NOBODY IS ASSIGNED" WAS A LIE, and the worst kind: it reported an empty table as a fact
    about the world. Agents unquestionably ran those workflows — research has a research agent,
    a panel has five seats, a build has software_engineer, qa and the reviewers — and the panel
    said none of them were there because `project_agents` had no row.

    An app with no record is not unstaffed. It is running the project's default team, and that
    is what this returns, marked `builtin` so the screen can say "by default" rather than
    claiming somebody chose.
    """
    rows = assigned(connection, project_id, app_key)
    if rows:
        return rows
    return [{"role_key": key, "source": BUILTIN, "assigned_by": None,
             "created_at": None, "app_name": ""}
            for key in project_team(connection, project_id)]


def assigned(connection, project_id: str, app_key: str) -> list[dict[str, Any]]:
    """Everyone RECORDED on this app. Empty means nothing was recorded, not that nobody runs it —
    see `effective`, which is what a screen should show."""
    rows = connection.execute(
        sql("SELECT role_key, source, assigned_by, created_at, app_name FROM project_agents "
            "WHERE project_id = :p AND app_key = :a ORDER BY role_key"),
        {"p": project_id, "a": app_key},
    ).mappings().all()
    return [dict(r) for r in rows]


def assign(connection, *, project_id: str, app_name: str, role_keys: Iterable[str],
           actor: str, source: str = OPERATOR,
           establishes_team: bool = False) -> dict[str, Any]:
    """Put agents on an app. Idempotent, and it never demotes an operator's choice.

    A re-seed that overwrote `operator` with `default` would quietly undo a decision somebody
    made — the same class of bug as a target picker that forgets what it was told.
    """
    key = app_key(app_name)
    if not key:
        return {"ok": False, "error": "an assignment needs an app to be about"}

    # ADDING ONE MUST NOT DROP THE REST.
    #
    # An app with nothing recorded SHOWS the effective team — BOSS's built-in set, or the
    # project's — because "no row" is not "no one". Assigning a single agent to it then wrote
    # one row, and from that moment `effective` returned that row alone: the screen went from
    # five agents to one, which reads as "adding an agent cleared the others". It did not clear
    # anything; the fallback stopped applying the instant something real existed.
    #
    # So the first write to an app materialises what it was already running. The list the
    # operator was looking at is the list they keep, plus whoever they just added.
    # Same rule for the project's own team: the Command Center shows BOSS's built-in set when
    # nobody has chosen one, and adding a seventh agent must not leave a team of one.
    # EXCEPT WHEN THE CALLER IS STATING THE TEAM RATHER THAN ADDING TO IT.
    #
    # Materialising first is right for a PERSON adding one agent to an app that was showing an
    # effective team — the list they were looking at is the list they keep. It is wrong for the
    # seeder that establishes a run's team: there is nothing on screen to preserve, and
    # materialising writes a team the workflow never asked for and then adds the real agents on
    # top of it.
    #
    # That is where the strangers came from, and it survived two fixes aimed at the wrong
    # layer. The run showed ten agents: seven materialised here as `assigned_by: boss`, and the
    # three the workflow names added afterwards as `ceo`. Both writes in one transaction, which
    # is why they carried the same timestamp and looked like a single decision.
    if not establishes_team and not assigned(connection, project_id, key):
        already = (default_roles() if key == PROJECT_DEFAULT
                   else project_team(connection, project_id))
        for existing in already:
            connection.execute(
                sql("INSERT INTO project_agents "
                    "(id, project_id, app_key, app_name, role_key, source, assigned_by) "
                    "VALUES (:i, :p, :a, :n, :r, :s, :by) "
                    "ON CONFLICT (project_id, app_key, role_key) DO NOTHING"),
                {"i": make_id("pag"), "p": project_id, "a": key, "n": app_name.strip(),
                 "r": existing, "s": DEFAULT, "by": "boss"},
            )

    added: list[str] = []
    for role_key in role_keys:
        role_key = str(role_key or "").strip()
        if not role_key:
            continue
        result = connection.execute(
            sql("""
                INSERT INTO project_agents
                    (id, project_id, app_key, app_name, role_key, source, assigned_by)
                VALUES (:i, :p, :a, :n, :r, :s, :by)
                ON CONFLICT (project_id, app_key, role_key) DO UPDATE
                    SET source = CASE WHEN project_agents.source = 'operator'
                                      THEN 'operator' ELSE EXCLUDED.source END,
                        app_name = EXCLUDED.app_name
                RETURNING (xmax = 0) AS inserted
                """),
            {"i": make_id("pag"), "p": project_id, "a": key, "n": app_name.strip(),
             "r": role_key, "s": source, "by": actor},
        ).mappings().first()
        if result and result["inserted"]:
            added.append(role_key)
    return {"ok": True, "app_key": key, "added": added,
            "assigned": [r["role_key"] for r in assigned(connection, project_id, key)]}


def unassign(connection, *, project_id: str, app_name: str, role_key: str) -> dict[str, Any]:
    """Take an agent off an app.

    Allowed even for a seeded default: a project that genuinely does not want a security
    reviewer should be able to say so, and the refusal it causes later will name the reason.
    """
    key = app_key(app_name)
    removed = connection.execute(
        sql("DELETE FROM project_agents WHERE project_id = :p AND app_key = :a "
            "AND role_key = :r RETURNING role_key"),
        {"p": project_id, "a": key, "r": role_key},
    ).first()
    return {"ok": bool(removed), "app_key": key, "role_key": role_key,
            "assigned": [r["role_key"] for r in assigned(connection, project_id, key)]}


def ensure_defaults(connection, *, project_id: str, app_name: str, actor: str = "boss",
                    spec: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Seed an app that has nobody on it. Does nothing to one that already has somebody.

    Called when a run starts rather than at project creation, so an app made before this feature
    existed is not stuck permanently unassignable — and so enforcement cannot refuse work on the
    grounds that a table was added after the app was.
    """
    key = app_key(app_name)
    if not key:
        return {"ok": False, "error": "no app name"}
    existing = assigned(connection, project_id, key)
    if existing:
        return {"ok": True, "seeded": [], "assigned": [r["role_key"] for r in existing]}
    # THE PROJECT'S OWN TEAM FIRST, IF SOMEBODY ACTUALLY CHOSE IT.
    #
    # `source` exists for exactly this question — the schema says so: "`default` was seeded from
    # the stages that would have run anyway; `operator` was chosen by a person. Kept apart so
    # 'nobody picked these' is answerable." This read every row regardless, so a team BOSS had
    # seeded for itself was treated as a decision somebody made.
    #
    # The result was a video run listing an Adversarial Reviewer, a Chief Architect, a QA Lead,
    # a Security Reviewer and a Software Engineer — none of them chosen by anyone, none of them
    # relevant, all of them able to sign off its gates. Where nobody picked the team and the
    # workflow names its own agents, the workflow is the better answer.
    chosen = connection.execute(
        sql("SELECT role_key FROM project_agents WHERE project_id = :p AND app_key = :a "
            "AND source = 'operator'"),
        {"p": project_id, "a": PROJECT_DEFAULT},
    ).mappings().all()
    if not chosen and not _names_anybody(spec):
        # Nobody chose a team AND the workflow names nobody: fall back to whatever the project
        # has, seeded or not, because an empty team means nobody may approve anything.
        chosen = connection.execute(
            sql("SELECT role_key FROM project_agents WHERE project_id = :p AND app_key = :a"),
            {"p": project_id, "a": PROJECT_DEFAULT},
        ).mappings().all()
    seeds = [r["role_key"] for r in chosen] or default_roles(spec)
    if chosen and spec:
        # THE AGENTS THE WORKFLOW NAMES GO ON TOP OF THE PROJECT'S TEAM.
        #
        # Approvers were already added here, for a good reason: a gate naming `release_manager`
        # with nobody assigned to it is the refusal this seeding exists to avoid. The agents
        # that DO THE WORK were not, and they need it for exactly the same reason — a run of
        # the video workflow was seeded with the project's software team and neither
        # `video_editor` nor `media_publisher`, the two agents its steps actually name. The
        # panel under a run reads "Only these agents may sign off this app's gates. Anyone else
        # is refused", so leaving them off is not cosmetic.
        for step in (spec.get("steps") or []):
            if not isinstance(step, dict):
                continue
            for named in (str(step.get("captain") or "").strip(),
                          str((step.get("approval") or {}).get("approver_role") or "").strip()):
                if named and named not in seeds:
                    seeds.append(named)
    # ESTABLISHING, NOT ADDING: this call decides the run's team, so nothing underneath it
    # should be materialised first.
    return {**assign(connection, project_id=project_id, app_name=app_name,
                     role_keys=seeds, actor=actor, source=DEFAULT,
                     establishes_team=True),
            "seeded": seeds}


def may_act(connection, *, project_id: str, app_name: str, role_key: str) -> dict[str, Any]:
    """Is this agent on this app?

    An app with NO assignment at all answers yes. That is deliberate: enforcement should stop
    work that contradicts a decision somebody made, not work on something nobody has decided
    about yet. Refusing by default would turn every pre-existing app into a broken one.
    """
    key = app_key(app_name)
    role_key = str(role_key or "").strip()
    if not key or not role_key:
        return {"allowed": True, "reason": "nothing to check"}
    rows = assigned(connection, project_id, key)
    if not rows:
        return {"allowed": True, "reason": "no agents are assigned to this app yet"}
    if any(r["role_key"] == role_key for r in rows):
        return {"allowed": True, "reason": "assigned"}
    return {
        "allowed": False,
        "reason": (f"'{role_key}' is not assigned to {app_name}. Assign them on the workflow, "
                   f"or choose one of: {', '.join(r['role_key'] for r in rows[:8])}."),
        "assigned": [r["role_key"] for r in rows],
    }


def apps_for_role(connection, project_id: str, role_key: str) -> list[dict[str, Any]]:
    """Every app this agent is assigned to. The other half of the agent detail page."""
    rows = connection.execute(
        sql("SELECT app_key, app_name, source, created_at FROM project_agents "
            "WHERE project_id = :p AND role_key = :r ORDER BY app_name"),
        {"p": project_id, "r": role_key},
    ).mappings().all()
    return [dict(r) for r in rows]


__all__ = ["assign", "unassign", "assigned", "effective", "ensure_defaults", "may_act",
           "apps_for_role", "project_team", "default_roles", "app_key",
           "DEFAULT", "OPERATOR", "BUILTIN", "PROJECT_DEFAULT"]
