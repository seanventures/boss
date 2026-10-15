"""Agents hold skills. Until now they were only told about them.

A role spec declares `permissions.allowed_skills`. That list went exactly one place — into the
agent's PROMPT, as "Role allowed skills: …". The runtime grants on `role_skills`, whose own
comment reads "GIVING A SKILL TO AN AGENT IS THE GRANT, and this is where it is cashed in."

Nothing joined them. So every install had agents that were told what they could use and held
nothing, and the Agents page showed exactly that.
"""
import inspect
import os

import pytest
from sqlalchemy import create_engine, text

from boss_engine.services import skill_definitions as sd
from boss_engine.services import workflow_custom_steps as cs

DATABASE_URL = os.environ.get("DATABASE_URL")
needs_db = pytest.mark.skipif(not DATABASE_URL, reason="no database configured")
PROJECT = "boss-dogfood"


def test_there_is_something_that_joins_the_two():
    assert hasattr(sd, "sync_role_skills")


def test_it_reads_the_role_spec_and_writes_the_grant():
    source = inspect.getsource(sd.sync_role_skills)
    assert "allowed_skills" in source, "it must read what the role declares"
    assert "assign_skill_to_role(" in source, "it must write the grant the runtime reads"


def test_a_wildcard_is_a_description_not_a_grant():
    """`infra.*` cannot be cashed in, and expanding it would hand an agent whatever happens to
    match it next week."""
    source = inspect.getsource(sd.sync_role_skills)
    assert '"*" in skill_id' in source


def test_it_never_takes_a_skill_away():
    """An operator who granted something by hand made a decision, and a sync that reverts it on
    the next run is a sync nobody can trust. Removing stays deliberate."""
    body = inspect.getsource(sd.sync_role_skills).split('"""')[-1]   # past the docstring
    assert "DELETE" not in body.upper()
    assert "unassign_skill_from_role(" not in body


def test_a_skill_the_project_does_not_have_is_reported_not_swallowed():
    """It is usually the real problem: the bundle it lives in was never enabled here."""
    source = inspect.getsource(sd.sync_role_skills)
    assert "unavailable" in source


@needs_db
def test_syncing_actually_gives_an_agent_its_skills():
    """EXERCISED. The claim is about rows in a table, so it is checked there."""
    engine = create_engine(DATABASE_URL)
    with engine.connect() as connection:
        outer = connection.begin()
        try:
            # A booted install has synced already, so take one grant its spec declares away (in
            # this transaction, rolled back below) and prove the sync gives it back.
            taken = connection.execute(text(
                "DELETE FROM role_skills WHERE ctid IN (SELECT g.ctid FROM role_skills g "
                "JOIN roles r ON r.project_id = g.project_id AND r.role_key = g.role_key "
                "WHERE g.project_id=:p AND (r.spec->'permissions'->'allowed_skills') ? g.skill_id "
                "LIMIT 1) RETURNING role_key, skill_id"),
                {"p": PROJECT}).first()
            result = sd.sync_role_skills(connection, project_id=PROJECT, actor="test")
            assert result["assigned_count"] > 0, "no agent was given anything"
            held = connection.execute(
                text("SELECT count(*) FROM role_skills WHERE project_id=:p"), {"p": PROJECT},
            ).scalar()
            assert held > 0, "role_skills is still empty, so agents still hold nothing"
            assert taken is not None, "no agent holds a skill its spec declares"
            back = connection.execute(text(
                "SELECT 1 FROM role_skills WHERE project_id=:p AND role_key=:r "
                "AND skill_id=:s"), {"p": PROJECT, "r": taken[0], "s": taken[1]}).first()
            assert back, f"the sync did not give {taken[0]} back {taken[1]}"
        finally:
            outer.rollback()
    engine.dispose()


@needs_db
def test_running_it_twice_changes_nothing_the_second_time():
    engine = create_engine(DATABASE_URL)
    with engine.begin() as connection:
        sd.sync_role_skills(connection, project_id=PROJECT, actor="test")
        before = connection.execute(
            text("SELECT count(*) FROM role_skills WHERE project_id=:p"), {"p": PROJECT}).scalar()
        sd.sync_role_skills(connection, project_id=PROJECT, actor="test")
        after = connection.execute(
            text("SELECT count(*) FROM role_skills WHERE project_id=:p"), {"p": PROJECT}).scalar()
    assert before == after


def test_the_save_time_check_accepts_an_agent_that_HOLDS_the_skill():
    """Two ways to be allowed, and the check must know both.

    A skill lists the callers that existed when it was written; giving the skill to the agent is
    the other grant, and the one the product is built on. Checking only the first is how
    "assign an agent to a step" became a thing you could be refused for doing correctly — and
    how I came to widen a skill's allowed_callers when the right move was to give the agent the
    skill.
    """
    # validate_step reads its sentences from validate_step_fields, where the check lives.
    source = inspect.getsource(cs.validate_step_fields)
    assert "_agent_holds(" in source
    holds = inspect.getsource(cs._agent_holds)
    assert "role_skills" in holds
    # Fails closed, unlike the allowed_callers lookup: an unreadable grant must not be invented.
    assert "return False" in holds
