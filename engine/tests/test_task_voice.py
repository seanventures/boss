"""A person files a browser or phone transcript on a task.

BOSS stores the words they submit and writes one receipt. It does not open a
phone line and it does not call a speech vendor. The same transcript id files
once.
"""

from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import create_engine, text

from boss_engine.services.receipts import ReceiptService
from boss_engine.services.task_voice import TaskVoiceError, TaskVoiceService


def _engine():
    url = os.environ.get(
        "DATABASE_URL",
        "postgresql+psycopg://boss:boss@127.0.0.1:55814/boss_test_voice",
    )
    return create_engine(url, future=True)


@pytest.fixture
def db():
    engine = _engine()
    try:
        with engine.connect() as probe:
            probe.execute(text("SELECT 1"))
    except Exception:
        pytest.skip("Postgres is not available for task voice tests.")
    with engine.connect() as connection:
        trans = connection.begin()
        try:
            yield connection
        finally:
            trans.rollback()


@pytest.fixture
def world(db):
    suffix = uuid.uuid4().hex[:12]
    org_id = f"org_voice_{suffix}"
    workspace_id = f"ws_voice_{suffix}"
    project_id = f"proj_voice_{suffix}"
    task_id = f"tsk_voice_{suffix}"
    db.execute(text("INSERT INTO orgs (id, name) VALUES (:id, :n)"), {"id": org_id, "n": "Voice"})
    db.execute(
        text("INSERT INTO workspaces (id, org_id, name) VALUES (:id, :o, :n)"),
        {"id": workspace_id, "o": org_id, "n": "Voice"},
    )
    db.execute(
        text(
            "INSERT INTO projects (id, workspace_id, name, slug) "
            "VALUES (:id, :w, :n, :s)"
        ),
        {"id": project_id, "w": workspace_id, "n": "Voice", "s": f"voice-{suffix}"},
    )
    db.execute(
        text(
            "INSERT INTO tasks (id, project_id, title, status, dedupe_key) "
            "VALUES (:id, :p, :title, 'open', :key)"
        ),
        {
            "id": task_id,
            "p": project_id,
            "title": "Second floor door",
            "key": f"task-voice-{suffix}",
        },
    )
    return {
        "org_id": org_id,
        "workspace_id": workspace_id,
        "project_id": project_id,
        "task_id": task_id,
    }


def _attach(db, world, **overrides):
    payload = {
        "org_id": world["org_id"],
        "project_id": world["project_id"],
        "task_id": world["task_id"],
        "transcript_id": "tr_door",
        "source": "phone",
        "body": "The door sticks on the second floor.",
        "submitted_by": "ada",
    }
    payload.update(overrides)
    return TaskVoiceService().attach(db, **payload)


def _voice_receipts(db, project_id: str) -> list[dict]:
    rows = ReceiptService().list_by_project(db, project_id, 20)
    return [row for row in rows if row["effect_kind"] == "task.voice_transcript"]


def test_a_submitted_transcript_is_stored_on_the_task_with_one_receipt(db, world):
    filed = _attach(db, world)
    assert filed["created"] is True
    assert filed["transcript_id"] == "tr_door"
    assert filed["source"] == "phone"
    assert filed["body"] == "The door sticks on the second floor."
    assert filed["submitted_by"] == "ada"
    assert filed["task_id"] == world["task_id"]
    assert str(filed["receipt_id"]).startswith("rcpt_")

    shown = TaskVoiceService().list_for_task(
        db,
        org_id=world["org_id"],
        project_id=world["project_id"],
        task_id=world["task_id"],
    )
    assert [row["transcript_id"] for row in shown] == ["tr_door"]
    assert shown[0]["receipt_id"] == filed["receipt_id"]
    assert shown[0]["body"] == "The door sticks on the second floor."

    receipts = _voice_receipts(db, world["project_id"])
    assert len(receipts) == 1
    assert receipts[0]["id"] == filed["receipt_id"]
    assert receipts[0]["status"] == "succeeded"
    assert receipts[0]["dedupe_key"] == f"task.voice:{world['org_id']}:tr_door"
    assert receipts[0]["provider_ref"] is None


def test_a_second_submit_of_the_same_transcript_id_does_not_duplicate_the_receipt(db, world):
    first = _attach(db, world)
    second = _attach(db, world, body="A later telling that must not replace the first.")
    assert second["created"] is False
    assert second["receipt_id"] == first["receipt_id"]
    assert second["body"] == "The door sticks on the second floor."

    shown = TaskVoiceService().list_for_task(
        db,
        org_id=world["org_id"],
        project_id=world["project_id"],
        task_id=world["task_id"],
    )
    assert len(shown) == 1
    assert len(_voice_receipts(db, world["project_id"])) == 1


def test_a_missing_task_is_a_visible_error_and_writes_no_receipt(db, world):
    with pytest.raises(TaskVoiceError) as caught:
        _attach(db, world, task_id="tsk_missing", transcript_id="tr_missing")
    assert caught.value.code == "task_not_found"
    assert "task" in str(caught.value).lower()
    assert _voice_receipts(db, world["project_id"]) == []


def test_an_empty_transcript_is_refused_and_names_that_no_vendor_is_called(db, world):
    with pytest.raises(TaskVoiceError) as caught:
        _attach(db, world, body="   ", transcript_id="tr_empty")
    assert caught.value.code == "transcript_empty"
    assert "speech vendor" in str(caught.value).lower()
    assert _voice_receipts(db, world["project_id"]) == []


def test_a_dial_request_is_refused_because_no_phone_line_opens(db, world):
    with pytest.raises(TaskVoiceError) as caught:
        _attach(db, world, source="dial", transcript_id="tr_dial")
    assert caught.value.code == "source_not_accepted"
    assert "phone line" in str(caught.value).lower()
    assert _voice_receipts(db, world["project_id"]) == []


def test_the_same_transcript_id_stays_on_the_first_task(db, world):
    first = _attach(db, world)
    other_task = f"tsk_other_{world['task_id'][-8:]}"
    db.execute(
        text(
            "INSERT INTO tasks (id, project_id, title, status, dedupe_key) "
            "VALUES (:id, :p, 'Other', 'open', :key)"
        ),
        {"id": other_task, "p": world["project_id"], "key": f"other-{other_task}"},
    )
    second = _attach(
        db,
        world,
        task_id=other_task,
        body="A later telling that belongs on the first task only.",
    )
    assert second["created"] is False
    assert second["task_id"] == world["task_id"]
    assert second["receipt_id"] == first["receipt_id"]
    assert second["body"] == "The door sticks on the second floor."
    elsewhere = TaskVoiceService().list_for_task(
        db,
        org_id=world["org_id"],
        project_id=world["project_id"],
        task_id=other_task,
    )
    assert elsewhere == []
    assert len(_voice_receipts(db, world["project_id"])) == 1


def test_a_browser_transcript_is_listed_with_the_phone_one(db, world):
    _attach(db, world)
    browser = _attach(
        db,
        world,
        transcript_id="tr_browser",
        source="browser",
        body="The hinge is the part that sticks.",
        submitted_by="bao",
    )
    assert browser["created"] is True
    assert browser["source"] == "browser"
    shown = TaskVoiceService().list_for_task(
        db,
        org_id=world["org_id"],
        project_id=world["project_id"],
        task_id=world["task_id"],
    )
    assert [row["transcript_id"] for row in shown] == ["tr_door", "tr_browser"]
    assert len(_voice_receipts(db, world["project_id"])) == 2
