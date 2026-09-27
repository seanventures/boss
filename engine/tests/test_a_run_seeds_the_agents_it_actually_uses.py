"""A run seeded six agents with no connection to the work, and omitted the two doing it.

`default_roles` answered with STAGE_ROLES — the map BOSS attributes model calls to — for every
run of every workflow. Right for the software factory, whose stages ARE those roles. Nonsense
for anything else: the video workflow, whose steps name `video_editor` and `media_publisher`,
seeded an Adversarial Reviewer, a Chief Architect, a QA Lead, a Security Reviewer, a Software
Engineer and a UX Engineer.

Not cosmetic. The panel under a run reads "Only these agents may sign off this app's gates.
Anyone else is refused" — so this decides who can approve, and it was naming six strangers while
leaving out the two agents actually on the job.
"""
from boss_engine.services.agent_assignment import default_roles

VIDEO = {
    "steps": [
        {"id": "fetch-link", "kind": "capability", "captain": "video_editor"},
        {"id": "edit", "kind": "capability", "captain": "video_editor"},
        {"id": "publish", "kind": "capability", "captain": "media_publisher",
         "approval": {"mode": "manual"}},
    ]
}


def test_a_workflow_that_names_its_agents_gets_those_agents():
    assert default_roles(VIDEO) == ["media_publisher", "video_editor"]


def test_none_of_the_software_factory_roles_come_along():
    """The six that were seeded onto a video run, by name, so this cannot quietly regress."""
    seeded = set(default_roles(VIDEO))
    for stranger in ("adversarial_reviewer", "chief_architect", "qa", "security_reviewer",
                     "software_engineer", "ux_engineer"):
        assert stranger not in seeded, f"{stranger} has nothing to do with this workflow"


def test_an_approver_is_on_the_run_even_if_no_step_is_captained_by_them():
    """A gate naming somebody the project has not assigned is the refusal this seeding exists
    to avoid — that reasoning was right and is kept."""
    spec = {"steps": [
        {"id": "edit", "captain": "video_editor"},
        {"id": "gate", "kind": "decision",
         "approval": {"mode": "manual", "approver_role": "release_manager"}},
    ]}
    assert default_roles(spec) == ["release_manager", "video_editor"]


def test_a_workflow_that_names_nobody_still_gets_a_team():
    """Every catalogue workflow. Falling back to STAGE_ROLES is right where the spec is silent —
    an empty team would mean nobody may approve anything."""
    roles = default_roles({"steps": [{"id": "a", "kind": "capability"}]})
    assert len(roles) > 1
    assert "software_engineer" in roles


def test_no_spec_at_all_behaves_as_before():
    assert default_roles() == default_roles(None) != []


# --------------------------------------------------------------------------- #
# The path that actually runs, which is not the one I fixed first.
# --------------------------------------------------------------------------- #
import os

import pytest
from sqlalchemy import create_engine, text

DATABASE_URL = os.environ.get("DATABASE_URL")
needs_db = pytest.mark.skipif(not DATABASE_URL, reason="no database configured")
PROJECT = "boss-dogfood"


@needs_db
def test_the_workflows_agents_are_seeded_even_when_the_project_has_a_team():
    """FIXING `default_roles` WAS NOT ENOUGH, AND THE LIVE RUN SAID SO.

    `ensure_defaults` prefers the project's stored default team, so `default_roles(spec)` is never
    reached on a project that has one — which is every real project. Only the spec's APPROVER
    roles were added on top. A video run therefore seeded the software team plus `human`, and
    still omitted `video_editor` and `media_publisher`.

    I checked the live run after deploying the first fix and the strangers were still there.
    This is the path that matters.
    """
    from boss_engine.services import agent_assignment

    engine = create_engine(DATABASE_URL)
    app_name = f"seed probe {os.urandom(4).hex()}"
    spec = {"steps": [
        {"id": "edit", "captain": "video_editor"},
        {"id": "gate", "kind": "decision",
         "approval": {"mode": "manual", "approver_role": "human"}},
        {"id": "publish", "captain": "media_publisher"},
    ]}
    with engine.begin() as connection:
        existing = connection.execute(
            text("SELECT role_key FROM project_agents WHERE project_id=:p AND app_key=:a "
                 "AND source='operator'"),
            {"p": PROJECT, "a": agent_assignment.PROJECT_DEFAULT}).scalars().all()
        # THE CONDITION IS THE POINT, SO IT IS ARRANGED RATHER THAN WAITED FOR.
        #
        # This first skipped when the project had no default team — which is exactly the branch
        # that was NOT broken. A test that skips past the defect it was written for is worse
        # than no test: it reports green for the thing it never ran.
        planted = []
        if not existing:
            from boss_engine.services.core import make_id

            for role in ("software_engineer", "qa"):
                # `operator`, because that is the case this test is about: a team a PERSON
                # chose, which the workflow adds to rather than replaces. A `default` team is
                # one BOSS seeded for itself and is covered by its own test below.
                connection.execute(
                    text("INSERT INTO project_agents (id, project_id, app_key, app_name, "
                         "role_key, source, assigned_by) "
                         "VALUES (:i, :p, :a, :a, :r, 'operator', 'test') "
                         "ON CONFLICT DO NOTHING"),
                    {"i": make_id("pag"), "p": PROJECT,
                     "a": agent_assignment.PROJECT_DEFAULT, "r": role})
                planted.append(role)
        team = existing or planted

        result = agent_assignment.ensure_defaults(
            connection, project_id=PROJECT, app_name=app_name, actor="test", spec=spec)
        seeded = set(result.get("seeded") or [])

        # The project's team is kept — somebody chose it.
        assert set(team) <= seeded
        # AND the agents this workflow names are on it, or they are refused at its own gate.
        for named in ("video_editor", "media_publisher", "human"):
            assert named in seeded, f"{named} is named by the workflow and was not seeded"

        connection.execute(
            text("DELETE FROM project_agents WHERE project_id=:p AND app_key=:a"),
            {"p": PROJECT, "a": agent_assignment.app_key(app_name)})
        if planted:
            connection.execute(
                text("DELETE FROM project_agents WHERE project_id=:p AND app_key=:a "
                     "AND role_key = ANY(:r) AND source='operator'"),
                {"p": PROJECT, "a": agent_assignment.PROJECT_DEFAULT, "r": planted})


@needs_db
def test_a_team_nobody_chose_does_not_outrank_the_workflow():
    """The strangers came from a team BOSS seeded for itself and called a decision.

    `project_agents.source` exists for this exact question — `default` means BOSS seeded it,
    `operator` means a person picked it. The lookup ignored the column, so an auto-seeded
    software team was treated as somebody's choice and every video run inherited five agents
    with no connection to the work and the right to sign off its gates.
    """
    from boss_engine.services import agent_assignment
    from boss_engine.services.core import make_id

    engine = create_engine(DATABASE_URL)
    app_name = f"unchosen probe {os.urandom(4).hex()}"
    spec = {"steps": [{"id": "edit", "captain": "video_editor"},
                      {"id": "publish", "captain": "media_publisher"}]}
    planted = []
    with engine.begin() as connection:
        for role in ("software_engineer", "qa"):
            row = connection.execute(
                text("INSERT INTO project_agents (id, project_id, app_key, app_name, role_key, "
                     "source, assigned_by) VALUES (:i, :p, :a, :a, :r, 'default', 'boss') "
                     "ON CONFLICT DO NOTHING RETURNING id"),
                {"i": make_id("pag"), "p": PROJECT,
                 "a": agent_assignment.PROJECT_DEFAULT, "r": role}).first()
            if row:
                planted.append(role)

        seeded = set(agent_assignment.ensure_defaults(
            connection, project_id=PROJECT, app_name=app_name, actor="test",
            spec=spec).get("seeded") or [])

        assert seeded == {"video_editor", "media_publisher"}, (
            f"a team nobody chose still outranked the workflow: {sorted(seeded)}")

        connection.execute(
            text("DELETE FROM project_agents WHERE project_id=:p AND app_key=:a"),
            {"p": PROJECT, "a": agent_assignment.app_key(app_name)})
        if planted:
            connection.execute(
                text("DELETE FROM project_agents WHERE project_id=:p AND app_key=:a "
                     "AND role_key = ANY(:r) AND source='default'"),
                {"p": PROJECT, "a": agent_assignment.PROJECT_DEFAULT, "r": planted})


@needs_db
def test_a_team_a_person_chose_is_still_respected():
    """The reasoning that put this branch here was right: overriding somebody's choice would
    ignore the one control the Command Center offers."""
    from boss_engine.services import agent_assignment
    from boss_engine.services.core import make_id

    engine = create_engine(DATABASE_URL)
    app_name = f"chosen probe {os.urandom(4).hex()}"
    spec = {"steps": [{"id": "edit", "captain": "video_editor"}]}
    with engine.begin() as connection:
        connection.execute(
            text("INSERT INTO project_agents (id, project_id, app_key, app_name, role_key, "
                 "source, assigned_by) VALUES (:i, :p, :a, :a, 'design_critic', "
                 "'operator', 'ceo') ON CONFLICT DO NOTHING"),
            {"i": make_id("pag"), "p": PROJECT, "a": agent_assignment.PROJECT_DEFAULT})

        seeded = set(agent_assignment.ensure_defaults(
            connection, project_id=PROJECT, app_name=app_name, actor="test",
            spec=spec).get("seeded") or [])

        assert "design_critic" in seeded, "a person's choice was discarded"
        assert "video_editor" in seeded, "the workflow's own agent must still be added"

        connection.execute(
            text("DELETE FROM project_agents WHERE project_id=:p AND app_key IN (:a, :b)"),
            {"p": PROJECT, "a": agent_assignment.app_key(app_name),
             "b": agent_assignment.PROJECT_DEFAULT})
