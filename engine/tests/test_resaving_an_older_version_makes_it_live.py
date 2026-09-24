"""Reverting a workflow by re-saving a previous configuration.

Found by doing it: a bad version was published, the correct spec was submitted again, the API
returned 200 with the old version's number — and the bad version stayed active. The operator
is told their save succeeded and nothing about the workflow has changed.

EXERCISED AGAINST A REAL DATABASE. Twice today a test that read the source passed while the
code did nothing, so this one publishes versions and asks which is live.
"""
import os
import uuid

import pytest
from sqlalchemy import create_engine, text

from boss_engine.services import workflows

DATABASE_URL = os.environ.get("DATABASE_URL")
pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="no database configured")

PROJECT = "boss-dogfood"


def _spec(seconds: int) -> dict:
    return {"entry_step": "hold",
            "steps": [{"id": "hold", "kind": "timer", "wait_seconds": seconds,
                       "binding": {}, "next": []}]}


@pytest.fixture()
def definition():
    """A throwaway workflow, removed however the test ends."""
    engine = create_engine(DATABASE_URL)
    key = f"revert_probe_{uuid.uuid4().hex[:8]}"
    with engine.begin() as connection:
        made = workflows.create_definition(
            connection, project_id=PROJECT, workflow_key=key, name=key,
            description=None, actor="test", namespace=workflows.PRODUCT_NAMESPACE,
            is_catalogue=False)
    yield engine, made["id"]
    with engine.begin() as connection:
        connection.execute(
            text("DELETE FROM workflow_definitions WHERE id=:i"), {"i": made["id"]})


def _active(engine, definition_id: str) -> str:
    with engine.begin() as connection:
        return str(connection.execute(
            text("SELECT active_version_id FROM workflow_definitions WHERE id=:i"),
            {"i": definition_id}).scalar_one() or "")


def test_re_saving_an_earlier_spec_makes_that_version_live_again(definition):
    engine, definition_id = definition
    with engine.begin() as connection:
        first = workflows.publish_version(
            connection, project_id=PROJECT, definition_id=definition_id,
            spec=_spec(60), actor="test")
    with engine.begin() as connection:
        second = workflows.publish_version(
            connection, project_id=PROJECT, definition_id=definition_id,
            spec=_spec(120), actor="test")
    assert _active(engine, definition_id) == second["id"]

    # Submit the FIRST spec again. This is a revert, and it must take effect.
    with engine.begin() as connection:
        again = workflows.publish_version(
            connection, project_id=PROJECT, definition_id=definition_id,
            spec=_spec(60), actor="test")

    assert again["duplicate"] is True, "publishing the same content must not make a new version"
    assert again["id"] == first["id"]
    assert again.get("reactivated") is True, "the caller has to be able to tell it took effect"
    assert _active(engine, definition_id) == first["id"], \
        "the workflow is still running the version the operator just replaced"


def test_re_saving_what_is_already_live_changes_nothing(definition):
    """The case the original behaviour was written for, which must keep working."""
    engine, definition_id = definition
    with engine.begin() as connection:
        first = workflows.publish_version(
            connection, project_id=PROJECT, definition_id=definition_id,
            spec=_spec(60), actor="test")
    with engine.begin() as connection:
        again = workflows.publish_version(
            connection, project_id=PROJECT, definition_id=definition_id,
            spec=_spec(60), actor="test")
    assert again["duplicate"] is True
    assert again.get("reactivated") is not True, "nothing moved, so nothing to report"
    assert _active(engine, definition_id) == first["id"]
