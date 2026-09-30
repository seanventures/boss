"""The start form renders FROM the workflow's declaration, so the listing has to carry it.

WHAT WAS WRONG, AND HOW LONG IT HAD BEEN WRONG
-----------------------------------------------
`list_definitions` has selected `input_schema` since the day it was written — there is a comment
in the SQL saying exactly why: "the console's start form renders FROM this; without it a workflow
whose steps read `from_input` is startable only by a caller that already knows the field names."

The value was never copied onto the item. So the form drew nothing for every workflow that
declares inputs, and "Start one" — which decides whether to open the form by asking whether any
inputs were declared — always concluded "none" and fell through to the sample path. An operator
pressed the button on a workflow that needs a video link and editing instructions, and got a run
started on a software-delivery sample.

A test that counted the fields would have caught it in a second. This is that test.
"""
import os

import pytest
from sqlalchemy import create_engine, text

from boss_engine.services import workflows

DATABASE_URL = os.environ.get("DATABASE_URL")
needs_db = pytest.mark.skipif(not DATABASE_URL, reason="no database configured")
PROJECT = "boss-dogfood"


@pytest.fixture()
def db():
    engine = create_engine(DATABASE_URL)
    with engine.begin() as connection:
        yield connection


@needs_db
def test_the_listing_carries_what_the_workflow_asks_for(db):
    """Built through the real create/save path, then read back through the real listing."""
    import hashlib
    import json

    from boss_engine.services.core import make_id

    key = "probe_asks_for_things"
    db.execute(text("DELETE FROM workflow_definitions WHERE project_id=:p AND workflow_key=:k"),
               {"p": PROJECT, "k": key})
    definition_id = make_id("wfd")
    db.execute(
        text("INSERT INTO workflow_definitions "
             "(id, project_id, workflow_key, name, description, namespace, created_by) "
             "VALUES (:i, :p, :k, 'Probe', 'probe', 'product', 'test')"),
        {"i": definition_id, "p": PROJECT, "k": key})

    spec = {
        "entry_step": "only",
        "steps": [{"id": "only", "kind": "capability",
                   "binding": {"agent_work": True,
                               "inputs": {"instructions": {"from_input": "instructions"}}},
                   "approval": {"mode": "auto"}, "next": []}],
        "input_schema": {
            "required": ["video_url", "instructions"],
            "properties": {
                "video_url": {"title": "Video link", "format": "uri"},
                "instructions": {"title": "What you want done", "format": "textarea"},
                "asset_url": {"title": "A picture to use"},
            },
        },
    }
    version_id = make_id("wfv")
    db.execute(
        text("INSERT INTO workflow_versions "
             "(id, project_id, workflow_definition_id, version, status, spec, content_hash, "
             " created_by) "
             "VALUES (:i, :p, :d, 1, 'published', CAST(:s AS JSONB), :h, 'test')"),
        {"i": version_id, "p": PROJECT, "d": definition_id, "s": json.dumps(spec),
         "h": hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()})
    db.execute(text("UPDATE workflow_definitions SET active_version_id=:v WHERE id=:d"),
               {"v": version_id, "d": definition_id})

    listed = [w for w in workflows.list_definitions(db, PROJECT)
              if w["workflow_key"] == key]
    assert listed, "the probe workflow is not in the listing at all"
    schema = listed[0].get("input_schema")

    # THE WHOLE POINT. Without this the console has nothing to draw and starts the run blind.
    assert schema, "the listing dropped input_schema — the start form will render no fields"
    assert set(schema.get("properties") or {}) == {"video_url", "instructions", "asset_url"}
    assert set(schema.get("required") or []) == {"video_url", "instructions"}

    db.execute(text("DELETE FROM workflow_definitions WHERE id=:d"), {"d": definition_id})


@needs_db
def test_a_workflow_that_asks_for_nothing_says_so_rather_than_omitting_the_key(db):
    """`{}` and a missing key read the same to a careless caller and differently to a careful
    one. The console asks `workflowInputFields(w.input_schema).length`, so the key must exist."""
    for item in workflows.list_definitions(db, PROJECT):
        assert "input_schema" in item, f"{item['workflow_key']} has no input_schema key at all"
        assert isinstance(item["input_schema"], dict)
