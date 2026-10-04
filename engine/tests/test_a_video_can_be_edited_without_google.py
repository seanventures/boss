"""Nobody could try the video example without connecting Google first.

THE INCIDENT. The bundled "Video edit and publish" example, started on an install with no Google
connection and a DIRECT .mp4 link: `media.fetch_link` downloaded the link fine, then storing the
fetched video defaulted to Google — "no active 'google_default' connection on this project" — and
the edit step stopped. Google is only needed to publish to YouTube (and to read a Drive link), so
the operator could not even trim a clip and look at the result.

THE FIX, AS A CLASS. Intermediate media is kept in BOSS's own artifact storage unless the
workflow asks for Drive AND Drive is connected (`media_storage.store`). Readiness asks for Google
only where a step publishes to YouTube or touches a Drive link. The rendered file is reviewable
through an authenticated API route, and the publish gate's preview links to it.

Storage, readiness and the database are real here. Only the network fetch and ffmpeg are
stood in for — the render is not what is under test, and ffmpeg is not on every machine.
"""
from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from boss_engine.config import get_settings
from boss_engine.services import media_storage
from boss_engine.services import skill_executors as se
from boss_engine.services import workflow_readiness as ready

DATABASE_URL = os.environ.get("BOSS_TEST_DATABASE_URL") or os.environ.get("DATABASE_URL")
needs_db = pytest.mark.skipif(not DATABASE_URL, reason="no database configured")

DIRECT = "https://samplelib.com/lib/preview/mp4/sample-5s.mp4"
DRIVE = "https://drive.google.com/file/d/1Z5MWSBKdNni1TTVTa4sAi2rYYrli5pVQ/view"
VIDEO_INFO = {"duration_seconds": 5.0, "width": 640, "height": 360, "has_audio": True}


@pytest.fixture()
def settings(tmp_path):
    return get_settings().model_copy(update={"artifact_dir": tmp_path / "artifacts"})


@pytest.fixture()
def db():
    engine = create_engine(DATABASE_URL, future=True)
    connection = engine.connect()
    transaction = connection.begin()
    try:
        yield connection
    finally:
        transaction.rollback()
        connection.close()
        engine.dispose()


@pytest.fixture()
def project(db):
    """A project with NO Google connection — the install from the incident."""
    pid = f"p_media_{uuid.uuid4().hex[:10]}"
    wid = db.execute(text("SELECT workspace_id FROM projects LIMIT 1")).scalar()
    if wid is None:
        pytest.skip("the test database has no workspace")
    db.execute(text("INSERT INTO projects (id, workspace_id, name, slug) "
                    "VALUES (:i, :w, 'media without google', :i)"), {"i": pid, "w": wid})
    return pid


def _context(db, settings, project):
    return se.SkillExecutorContext(settings=settings, connection=db, project_id=project,
                                   actor="video_editor")


def _stand_in_for_the_network_and_ffmpeg(monkeypatch, tmp_path):
    from boss_engine.services import video_edit

    def fake_fetch(target, *, dest_dir):
        os.makedirs(dest_dir, exist_ok=True)
        path = os.path.join(dest_dir, "sample-5s.mp4")
        Path(path).write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"source-video" * 64)
        return {"path": path, "name": "sample-5s.mp4", "bytes": os.path.getsize(path),
                "provider": "http", "source_url": target.original_url}

    def fake_trim(src, *, start, end, work_dir):
        out = os.path.join(work_dir, "trimmed.mp4")
        Path(out).write_bytes(Path(src).read_bytes()[: 200])
        return out

    # The guard against private addresses resolves the host; this host is public, but a test
    # must not depend on DNS.
    monkeypatch.setattr("boss_engine.services.media_providers._refuse_unroutable",
                        lambda host: None)
    monkeypatch.setattr(media_storage, "_fetch_direct", fake_fetch)
    monkeypatch.setattr(video_edit, "probe", lambda path: dict(VIDEO_INFO))
    monkeypatch.setattr(video_edit, "verify_playable",
                        lambda path, expect_audio=True: dict(VIDEO_INFO))
    monkeypatch.setattr(video_edit, "trim", fake_trim)

    def no_google(*_a, **_k):
        raise AssertionError("Google was called on a run that never asked for it")

    monkeypatch.setattr("boss_engine.services.drive_media.upload", no_google)
    monkeypatch.setattr("boss_engine.services.drive_media.download", no_google)


def _content(db, artifact_id):
    row = db.execute(text("SELECT uri, title, content FROM artifacts WHERE id = :i"),
                     {"i": artifact_id}).mappings().one()
    content = row["content"] if isinstance(row["content"], dict) else json.loads(row["content"])
    return row, content


# --- direct link, no Google: fetch -> store in BOSS -> edit -> publish gate ------------------

@needs_db
def test_a_direct_link_is_fetched_edited_and_ready_to_publish_without_google(
        db, settings, project, monkeypatch, tmp_path):
    _stand_in_for_the_network_and_ffmpeg(monkeypatch, tmp_path)
    context = _context(db, settings, project)

    fetched = se.execute_media_fetch_link(context, {"url": DIRECT})
    assert fetched["stored_in"] == "boss"
    source_id = fetched["media_artifact_id"]
    assert fetched["view_url"] == f"/v1/artifacts/{source_id}/media"
    row, content = _content(db, source_id)
    assert content["storage"] == "boss" and content["drive_file_id"] is None
    assert row["uri"] == fetched["view_url"]
    stored = Path(settings.artifact_root) / content["local_path"]
    assert stored.is_file() and stored.parent == Path(settings.artifact_root) / project / "media"
    assert content["mime_type"] == "video/mp4"

    edited = se.execute_video_trim(context, {"media_artifact_id": source_id,
                                             "start": "0", "end": "2"})
    edit_id = edited["media_artifact_id"]
    assert edit_id != source_id and edited["stored_in"] == "boss"
    _row, edit_content = _content(db, edit_id)
    assert edit_content["source_artifact_id"] == source_id
    edit_file = Path(settings.artifact_root) / edit_content["local_path"]
    assert edit_file.read_bytes() == stored.read_bytes()[:200]
    # The source is untouched: a new file, never an overwrite.
    assert stored.stat().st_size > edit_file.stat().st_size

    # THE PUBLISH GATE'S PREVIEW, resolved the way the runner resolves it when the gate opens.
    from boss_engine.services import workflow_capability_runner as runner

    db.execute(text(
        "INSERT INTO workflow_custom_steps (id, project_id, step_key, label, binding_kind, "
        "skill_id, inputs, created_by) VALUES (:i, :p, 'publish_to_youtube', "
        "'Publish to YouTube', 'skill', 'publish_youtube', CAST(:inputs AS JSONB), 'ceo')"),
        {"i": f"wcs_{uuid.uuid4().hex[:10]}", "p": project,
         "inputs": json.dumps([{"name": "media_artifact_id", "required": True},
                               {"name": "title", "required": True}])})
    step = {"id": "publish", "kind": "capability",
            "binding": {"custom_step": "publish_to_youtube", "inputs": {
                "media_artifact_id": {"from_step": "edit", "field": "media_artifact_id"},
                "title": {"from_step": "edit", "field": "title"}}}}
    preview = runner._action_preview_for(
        db, project, step,
        {"input": {}, "skipped": {}, "steps": {"edit": {**edited, "title": "Five seconds"}}},
        "Publish to YouTube")
    assert preview["kind"] == "publish", preview
    assert preview["media"]["id"] == edit_id
    assert preview["media"]["link"] == f"/v1/artifacts/{edit_id}/media"
    assert preview["media"]["name"] == edit_content["name"]


@needs_db
def test_asking_for_drive_without_google_keeps_the_file_in_boss_and_says_so(
        db, settings, project, monkeypatch, tmp_path):
    _stand_in_for_the_network_and_ffmpeg(monkeypatch, tmp_path)
    fetched = se.execute_media_fetch_link(_context(db, settings, project),
                                          {"url": DIRECT, "store_provider": "google"})
    assert fetched["stored_in"] == "boss"
    assert "Google is not connected" in fetched["storage_note"]


def _connect_google(db, project):
    definition = f"idef_{uuid.uuid4().hex[:10]}"
    db.execute(text(
        "INSERT INTO integration_definitions (id, project_id, integration_key, version, name, "
        "category, status, skill_contracts, provenance, content_hash, created_by) VALUES "
        "(:i, :p, 'google', 1, 'Google', 'media', 'active', '[]'::jsonb, '{}'::jsonb, 'x', "
        "'ceo')"), {"i": definition, "p": project})
    db.execute(text(
        "INSERT INTO integration_connections (id, project_id, integration_definition_id, "
        "connection_key, scope_type, scope_id, owner_ref, status, created_by) VALUES "
        "(:i, :p, :d, 'youtube_default', 'project', :p, 'ceo', 'active', 'ceo')"),
        {"i": f"icon_{uuid.uuid4().hex[:10]}", "p": project, "d": definition})


def _drive_source(db, project, monkeypatch, tmp_path):
    """A video that came out of Drive, as `drive.import_video` records it, with Drive itself
    stood in for: downloads hand back a file, uploads are recorded."""
    from boss_engine.services import drive_media

    _stand_in_for_the_network_and_ffmpeg(monkeypatch, tmp_path)
    uploads = []

    def fake_download(connection, settings, *, project_id, file_id, dest_dir,
                      connection_key="youtube_default"):
        os.makedirs(dest_dir, exist_ok=True)
        path = os.path.join(dest_dir, "from-drive.mp4")
        Path(path).write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"drive-video" * 64)
        return {"path": path, "name": "from-drive.mp4", "bytes": os.path.getsize(path),
                "file_id": file_id}

    def fake_upload(connection, settings, *, project_id, local_path, name, folder_id="",
                    connection_key="youtube_default", actor="system"):
        uploads.append({"name": name, "connection_key": connection_key})
        return {"file_id": "1EditedInDrive", "name": name, "bytes": 200,
                "view_url": "https://drive.google.com/file/d/1EditedInDrive/view"}

    monkeypatch.setattr(drive_media, "download", fake_download)
    monkeypatch.setattr(drive_media, "upload", fake_upload)
    source_id = drive_media.record_artifact(
        db, project_id=project, actor="video_editor", kind="media_video",
        drive_file={"file_id": "1SourceInDrive", "name": "MS_latest.mp4",
                    "view_url": "https://drive.google.com/file/d/1SourceInDrive/view"},
        detail=dict(VIDEO_INFO))
    return source_id, uploads


@needs_db
def test_an_edit_of_a_drive_source_goes_back_to_drive_when_google_is_connected(
        db, settings, project, monkeypatch, tmp_path):
    """The operator's own workflow: source in Drive, edits in Drive. Unchanged."""
    _connect_google(db, project)
    source_id, uploads = _drive_source(db, project, monkeypatch, tmp_path)
    edited = se.execute_video_trim(_context(db, settings, project),
                                   {"media_artifact_id": source_id, "start": "0", "end": "2"})
    assert edited["stored_in"] == "google" and "storage_note" not in edited
    assert edited["drive_file_id"] == "1EditedInDrive"
    assert edited["view_url"] == "https://drive.google.com/file/d/1EditedInDrive/view"
    assert len(uploads) == 1
    _row, content = _content(db, edited["media_artifact_id"])
    assert content["storage"] == "google" and not content.get("local_path")
    assert not (Path(settings.artifact_root) / project / "media").exists()

    # ASKING OTHERWISE WINS: the step names BOSS's storage, and Drive is not written.
    kept = se.execute_video_trim(_context(db, settings, project),
                                 {"media_artifact_id": source_id, "start": "0", "end": "2",
                                  "store_provider": "boss"})
    assert kept["stored_in"] == "boss" and len(uploads) == 1


@needs_db
def test_an_edit_of_a_drive_source_stays_in_boss_when_google_is_not_connected(
        db, settings, project, monkeypatch, tmp_path):
    source_id, uploads = _drive_source(db, project, monkeypatch, tmp_path)
    edited = se.execute_video_trim(_context(db, settings, project),
                                   {"media_artifact_id": source_id, "start": "0", "end": "2"})
    assert edited["stored_in"] == "boss" and uploads == []
    assert "Google is not connected" in edited["storage_note"]


def test_a_stored_file_never_overwrites_and_never_leaves_its_project(settings, tmp_path):
    source = tmp_path / "out.mp4"
    source.write_bytes(b"video")
    first = media_storage.store_local(settings, project_id="p1", local_path=str(source),
                                      name="../../etc/passwd")
    second = media_storage.store_local(settings, project_id="p1", local_path=str(source),
                                       name="../../etc/passwd")
    assert first["local_path"] != second["local_path"]
    for stored in (first, second):
        path = media_storage.local_file_for(settings, "p1", stored)
        assert path.parent == Path(settings.artifact_root).resolve() / "p1" / "media"
    # Another project's file, or a path that climbs out, is refused by the reader.
    with pytest.raises(media_storage.MediaStorageError, match="outside"):
        media_storage.local_file_for(settings, "p2", first)
    with pytest.raises(media_storage.MediaStorageError, match="outside"):
        media_storage.local_file_for(settings, "p1", {"local_path": "../../secrets"})
    with pytest.raises(media_storage.MediaStorageError):
        media_storage.store_local(settings, project_id="../p1", local_path=str(source),
                                  name="x.mp4")


def test_the_size_limit_holds_for_media_boss_keeps(settings, tmp_path, monkeypatch):
    source = tmp_path / "big.mp4"
    source.write_bytes(b"0123456789")
    monkeypatch.setattr(media_storage, "MAX_DIRECT_BYTES", 5)
    with pytest.raises(media_storage.MediaStorageError, match="limit"):
        media_storage.store_local(settings, project_id="p1", local_path=str(source),
                                  name="big.mp4")


# --- readiness: Google only where YouTube or Drive is actually involved ----------------------

def _agent_edit(video_url):
    return {"id": "edit", "kind": "capability", "captain": "video_editor",
            "binding": {"agent_work": True, "inputs": {
                "video_url": {"from_input": "video_url"},
                "instructions": {"from_input": "instructions"}}},
            "approval": {"mode": "auto"}, "next": ["publish"]}


SPEC = {"entry_step": "edit", "steps": [
    _agent_edit(None),
    {"id": "publish", "kind": "capability", "captain": "media_publisher",
     "binding": {"custom_step": "publish_to_youtube", "inputs": {
         "media_artifact_id": {"from_step": "edit", "field": "media_artifact_id"}}},
     "approval": {"mode": "manual"}, "next": []},
]}


def _google_needs(spec, input_data, **kw):
    return {(item["key"], item["step_id"])
            for item in ready.requirements(spec, input_data=input_data, **kw)
            if item["key"].startswith("google")}


def test_an_edit_from_a_direct_link_needs_no_google():
    assert _google_needs(SPEC, {"video_url": DIRECT, "instructions": "keep 0:01 to 0:03"}) \
        == set()


def test_a_drive_link_needs_google_on_the_step_that_reads_it():
    assert _google_needs(SPEC, {"video_url": DRIVE, "instructions": "trim it"}) == {
        ("google_drive", "edit")}
    # A Drive link written into the instruction is the same fact.
    assert _google_needs(SPEC, {"instructions": f"Trim {DRIVE} to the first minute."}) == {
        ("google_drive", "edit")}
    assert ready.need_for("google_drive").label == "Google (Drive link)"


def test_the_editing_skills_no_longer_claim_google():
    specs = ready._skill_specs(ready._specpack_root(None))
    for skill in ("video.trim", "video.cut", "video.caption", "video.apply_plan",
                  "media.fetch_link"):
        assert "google" not in ready.skill_needs(skill, specs.get(skill)), skill
    assert "google" in ready.skill_needs("publish_youtube", specs.get("publish_youtube"))


@needs_db
def test_publishing_needs_google_on_the_publish_step_only(db, project):
    db.execute(text(
        "INSERT INTO workflow_custom_steps (id, project_id, step_key, label, binding_kind, "
        "skill_id, created_by) VALUES (:i, :p, 'publish_to_youtube', 'Publish to YouTube', "
        "'skill', 'publish_youtube', 'ceo')"),
        {"i": f"wcs_{uuid.uuid4().hex[:10]}", "p": project})
    data = {"video_url": DIRECT, "instructions": "keep 0:01 to 0:03"}
    assert _google_needs(SPEC, data, connection=db, project_id=project) == {
        ("google", "publish")}

    result = ready.preflight(db, None, project, SPEC, data)
    google = [m for m in result["missing"] if m["key"].startswith("google")]
    assert [(m["key"], [n["step_id"] for n in m["needed_by"]]) for m in google] == [
        ("google", ["publish"])]
    # The run stops for a person before it reaches publish, so it can be started and Google
    # connected while the edit runs.
    assert google[0]["after_a_person"] is True

    with_drive = ready.preflight(db, None, project, SPEC,
                                 {"video_url": DRIVE, "instructions": "trim"})
    keys = {m["key"]: [n["step_id"] for n in m["needed_by"]] for m in with_drive["missing"]}
    assert keys.get("google_drive") == ["edit"] and keys.get("google") == ["publish"]
