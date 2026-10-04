"""A rendered edit kept in BOSS's own storage opens from the console — for its project only.

Intermediate media (a fetched source, a rendered edit) is kept on the install's artifacts volume
when the workflow asks for no Drive. The person approving the publish has to be able to watch the
exact file first, so `GET /v1/artifacts/{id}/media` streams it — with the project's read check,
and with the stored path re-checked against that project's media directory.
"""

import json
from uuid import uuid4

from sqlalchemy import text

from apps.api.app import main as api_main
from boss_engine.services import media_storage


def test_the_media_route_serves_its_project_and_refuses_others(
        client, engine, monkeypatch, tmp_path):
    suffix = uuid4().hex[:10]
    reader = f"media_reader_{suffix}"
    key = f"media-key-{suffix}"
    home = "boss-dogfood"
    foreign = f"media_foreign_{suffix}"
    monkeypatch.setattr(api_main.settings, "require_api_auth", True)
    monkeypatch.setattr(api_main.settings, "api_keys", key)
    monkeypatch.setattr(api_main.settings, "api_key_principals", f"{key}:{reader}")
    monkeypatch.setattr(api_main.settings, "artifact_dir", tmp_path / "artifacts")
    headers = {"Authorization": f"Bearer {key}"}

    source = tmp_path / "edit.mp4"
    source.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"rendered" * 32)
    ids = {name: f"art_media_{name}_{suffix}"
           for name in ("home", "foreign", "drive", "escaped")}

    with engine.begin() as connection:
        workspace = connection.execute(
            text("SELECT workspace_id FROM projects WHERE id = :id"), {"id": home}).scalar()
        connection.execute(
            text("""INSERT INTO projects (id, workspace_id, name, slug)
                    VALUES (:id, :w, 'Foreign media test', :id)"""),
            {"id": foreign, "w": workspace})
        api_main.members.upsert_member(
            connection, project_id=home, member_id=reader, display_name="Media reader",
            member_type="human", role_keys=[], permissions=[], actor="ceo")
        rows = []
        for name, project_id in (("home", home), ("foreign", foreign)):
            stored = media_storage.store_local(api_main.settings, project_id=project_id,
                                               local_path=str(source), name="rain_final.mp4")
            rows.append((name, project_id, {**stored, "storage": "boss"}))
        rows.append(("drive", home, {"drive_file_id": "1abc", "storage": "google",
                                     "view_url": "https://drive.google.com/file/d/1abc/view"}))
        # A row whose path climbs into ANOTHER project's media. Never served.
        rows.append(("escaped", home, {"storage": "boss", "mime_type": "video/mp4",
                                       "local_path": rows[1][2]["local_path"]}))
        for name, project_id, content in rows:
            connection.execute(
                text("""INSERT INTO artifacts (id, project_id, artifact_type, title, content)
                        VALUES (:id, :p, 'media_video', 'rain_final.mp4',
                                CAST(:content AS JSONB))"""),
                {"id": ids[name], "p": project_id, "content": json.dumps(content)})

    try:
        served = client.get(f"/v1/artifacts/{ids['home']}/media", headers=headers)
        assert served.status_code == 200, served.text
        assert served.content == source.read_bytes()
        assert served.headers["content-type"] == "video/mp4"
        assert served.headers["content-disposition"].startswith("inline")
        # A player seeks; the file response answers a range.
        ranged = client.get(f"/v1/artifacts/{ids['home']}/media",
                            headers={**headers, "Range": "bytes=0-3"})
        assert ranged.status_code == 206 and ranged.content == source.read_bytes()[:4]

        refused = client.get(f"/v1/artifacts/{ids['foreign']}/media", headers=headers)
        assert refused.status_code == 403, refused.text

        drive = client.get(f"/v1/artifacts/{ids['drive']}/media", headers=headers)
        assert drive.status_code == 409 and "Drive" in drive.text

        escaped = client.get(f"/v1/artifacts/{ids['escaped']}/media", headers=headers)
        assert escaped.status_code == 404, escaped.text
        assert b"rendered" not in escaped.content

        missing = client.get(f"/v1/artifacts/art_nope_{suffix}/media", headers=headers)
        assert missing.status_code == 404
    finally:
        with engine.begin() as connection:
            connection.execute(text("DELETE FROM artifacts WHERE id = ANY(:ids)"),
                               {"ids": list(ids.values())})
            connection.execute(text("DELETE FROM events WHERE project_id = :id"),
                               {"id": foreign})
            connection.execute(text("DELETE FROM projects WHERE id = :id"), {"id": foreign})
