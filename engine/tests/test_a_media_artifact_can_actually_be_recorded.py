"""`record_artifact` named a column that has never existed, and nothing noticed.

    INSERT INTO artifacts (..., data_boundary, created_by)
    -> column "created_by" of relation "artifacts" does not exist

The table has `created_by_job_id` — a job id, a different fact. So EVERY media artifact insert
failed, which means `record_artifact` had never once worked, and neither had anything downstream
of it: `media.fetch_link`, every video edit, every publish. The whole video pipeline.

WHY IT SURVIVED. The failure was swallowed five layers deep. The executor's retry loop caught it
into `last_error`; the aborted transaction then made the invocation record, the step failure and
the tick all fail in turn, each reporting a poisoned transaction naming some other query. The
workflow just stopped, with no error recorded against it, and an operator watched it sit at step
one for half an hour.

Every test of this function had mocked the database. This one writes to it, because the defect
was entirely in the disagreement between the SQL and the schema — the one thing a mock cannot
check.
"""
import json
import os

import pytest
from sqlalchemy import create_engine, text

from boss_engine.services.drive_media import record_artifact

DATABASE_URL = os.environ.get("DATABASE_URL")
needs_db = pytest.mark.skipif(not DATABASE_URL, reason="no database configured")
PROJECT = "boss-dogfood"

DRIVE_FILE = {
    "file_id": "1Z5MWSBKdNni1TTVTa4sAi2rYYrli5pVQ",
    "name": "Long-video-FINAL-with-name.mp4",
    "view_url": "https://drive.google.com/file/d/1Z5MW/view",
    "bytes": 167064358,
}


@pytest.fixture()
def db():
    engine = create_engine(DATABASE_URL)
    with engine.begin() as connection:
        yield connection


@needs_db
def test_recording_a_media_artifact_writes_a_row(db):
    artifact_id = record_artifact(
        db, project_id=PROJECT, actor="video_editor",
        kind="media_video", drive_file=DRIVE_FILE,
        detail={"operation": "fetch_link", "duration_seconds": 706.775},
    )
    row = db.execute(
        text("SELECT artifact_type, title, uri, content FROM artifacts WHERE id = :i"),
        {"i": artifact_id}).mappings().first()
    assert row is not None, "the insert claimed to succeed and wrote nothing"
    assert row["artifact_type"] == "media_video"
    # The FILENAME is the title: "art_9f3c…" tells a reviewer nothing about which video it is.
    assert row["title"] == "Long-video-FINAL-with-name.mp4"
    assert row["uri"] == DRIVE_FILE["view_url"]

    content = row["content"] if isinstance(row["content"], dict) else json.loads(row["content"])
    # The handle the next step needs. Losing this is losing the whole pipeline.
    assert content["drive_file_id"] == DRIVE_FILE["file_id"]
    # WHO MADE IT still recorded, in the blob rather than in a column that does not exist.
    assert content["created_by"] == "video_editor"
    # And whatever the caller knew about it.
    assert content["operation"] == "fetch_link"
    assert content["duration_seconds"] == 706.775

    db.execute(text("DELETE FROM artifacts WHERE id = :i"), {"i": artifact_id})


@needs_db
def test_an_edited_artifact_records_what_it_came_from(db):
    """A published video must be traceable to the source it was cut from."""
    source = record_artifact(
        db, project_id=PROJECT, actor="video_editor", kind="media_video",
        drive_file=DRIVE_FILE)
    edited = record_artifact(
        db, project_id=PROJECT, actor="video_editor", kind="media_video",
        drive_file={**DRIVE_FILE, "file_id": "edited-id", "name": "short.mp4"},
        source_artifact_id=source, detail={"operation": "trim"})

    row = db.execute(text("SELECT content FROM artifacts WHERE id = :i"),
                     {"i": edited}).mappings().first()
    content = row["content"] if isinstance(row["content"], dict) else json.loads(row["content"])
    assert content["source_artifact_id"] == source

    db.execute(text("DELETE FROM artifacts WHERE id = ANY(:ids)"), {"ids": [source, edited]})


def test_the_insert_names_only_columns_the_table_has():
    """The defect in one line: SQL and schema disagreeing about a column name.

    Checked against db/schema.sql so it fails on the developer's machine rather than on a live
    install eight layers of error-masking away from the symptom.
    """
    import inspect
    import pathlib
    import re

    from boss_engine.services import drive_media

    schema = (pathlib.Path(__file__).resolve().parents[2] / "db" / "schema.sql").read_text()
    table = schema[schema.index("CREATE TABLE IF NOT EXISTS artifacts ("):]
    table = table[:table.index("\n);")]
    columns = set(re.findall(r"^\s{2}([a-z_]+)\s", table, re.MULTILINE))
    # Columns added later by ALTER are just as real.
    columns |= set(re.findall(r"ALTER TABLE artifacts ADD COLUMN IF NOT EXISTS ([a-z_]+)", schema))

    source = inspect.getsource(drive_media.record_artifact)
    named = source[source.index("INSERT INTO artifacts ("):]
    named = named[len("INSERT INTO artifacts ("):named.index(")")]
    used = {c.strip() for c in named.replace("\n", " ").split(",") if c.strip()}

    assert used <= columns, (
        f"record_artifact inserts columns the artifacts table does not have: "
        f"{sorted(used - columns)}")
