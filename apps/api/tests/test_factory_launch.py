"""I'm feeling lucky (BOSSAPP-1274) and the attachments it reads (BOSSAPP-1275).

The three routes are the seam. The builder is a stand-in: no model, no GitHub, no Drive.
"""
from __future__ import annotations

from pathlib import Path

from sqlalchemy import text

from apps.api.app.config import get_settings
from boss_engine.services.budget import BudgetGovernor
from boss_engine.services.factory_launch import MAX_ATTACHMENT_BYTES

_CONNECT = (
    "Connect GitHub first: a new project reaches a repository through the "
    "organisation's GitHub App (Repo and models › GitHub)."
)
_DRIVE = (
    "That Drive source is not connected. No Google account is connected for this "
    "organization. BOSS did not fetch it."
)

_ALPHA = """\
id: alpha-notes
title: Alpha notes
blurb: A list and an editor.
category: app
prompt: Build alpha notes.
stack: vite-react
resources:
  - postgres
workflows:
  - name: Build alpha
    steps:
      - Draft the page
      - Save a note
"""

_BETA = """\
id: beta-board
title: Beta board
blurb: Three columns.
category: app
prompt: Build a beta board.
stack: vite-react
resources:
  - redis
workflows:
  - name: Build beta
    steps:
      - Draft the columns
"""

_CHECKLIST = """\
id: checklist
title: Ship a checklist
blurb: Two columns.
category: app
prompt: Ship a checklist with two columns.
stack: vite
resources:
  - postgres
workflows:
  - name: Build the checklist
    steps:
      - Draft the columns
"""


def _write_catalogue(directory: Path) -> None:
    directory.mkdir()
    (directory / "a.yaml").write_text(_ALPHA, encoding="utf-8")
    (directory / "b.yaml").write_text(_BETA, encoding="utf-8")
    (directory / "c-broken.yaml").write_text(": : :\n", encoding="utf-8")
    (directory / "d-partial.yaml").write_text("id: gamma\ntitle: Gamma\n", encoding="utf-8")
    (directory / "z-dup.yaml").write_text(_ALPHA, encoding="utf-8")


def _count(engine, table: str) -> int:
    with engine.connect() as connection:
        return int(connection.execute(text(f"SELECT count(*) FROM {table}")).scalar() or 0)


def _one(engine, sql: str, **params):
    with engine.connect() as connection:
        return connection.execute(text(sql), params).mappings().first()


def _exec(engine, sql: str, **params) -> None:
    with engine.begin() as connection:
        connection.execute(text(sql), params)


def _patch_builder(monkeypatch, calls: list, run_id: str = "run_lucky_1"):
    def build(connection, *, project_id, spec, actor, settings, jobs, reservation_id):
        calls.append({
            "project_id": project_id,
            "spec": spec,
            "reservation_id": reservation_id,
        })
        return {"project_id": project_id, "run_id": run_id}

    monkeypatch.setattr("boss_engine.services.factory_launch.build_run", build)


def _patch_order(monkeypatch, order: list) -> None:
    original = BudgetGovernor.reserve

    def reserve(self, connection, project_id, category, amount_usd_micros, reason,
                expires_in_minutes, job_id=None, allocation_id=None, risk_tier=None):
        order.append("reserve")
        return original(
            self, connection, project_id, category, amount_usd_micros, reason,
            expires_in_minutes, job_id=job_id, allocation_id=allocation_id,
            risk_tier=risk_tier)

    monkeypatch.setattr(BudgetGovernor, "reserve", reserve)


def test_templates_skip_malformed_files_and_count_them(client, monkeypatch, tmp_path):
    catalogue = tmp_path / "factory_templates"
    _write_catalogue(catalogue)
    monkeypatch.setattr("boss_engine.services.factory_launch.TEMPLATE_DIR", catalogue)

    response = client.get("/v1/factory/templates")

    assert response.status_code == 200
    body = response.json()
    assert body["skipped"] == 3
    assert [item["id"] for item in body["templates"]] == ["alpha-notes", "beta-board"]
    alpha = body["templates"][0]
    assert alpha["title"] == "Alpha notes"
    assert alpha["resources"] == ["postgres"]
    assert alpha["workflows"] == [
        {"name": "Build alpha", "steps": ["Draft the page", "Save a note"]},
    ]


def test_missing_catalogue_is_an_empty_list(client, monkeypatch, tmp_path):
    monkeypatch.setattr(
        "boss_engine.services.factory_launch.TEMPLATE_DIR", tmp_path / "absent")

    response = client.get("/v1/factory/templates")

    assert response.status_code == 200
    assert response.json() == {"templates": [], "skipped": 0}


def test_oversize_attachment_is_refused_and_stores_nothing(client, engine):
    before = _count(engine, "factory_launch_attachments")
    big = b"x" * (MAX_ATTACHMENT_BYTES + 1)

    response = client.post(
        "/v1/factory/attachments",
        files=[
            ("file", ("ok.txt", b"small", "text/plain")),
            ("file", ("big.bin", big, "application/octet-stream")),
        ],
    )

    assert response.status_code == 413
    body = response.json()
    assert body["error"] == "file_too_large"
    assert "big.bin" in body["message"]
    assert str(MAX_ATTACHMENT_BYTES) in body["message"]
    assert _count(engine, "factory_launch_attachments") == before


def test_empty_attachment_is_refused(client, engine):
    before = _count(engine, "factory_launch_attachments")

    response = client.post(
        "/v1/factory/attachments",
        files=[("file", ("empty.txt", b"", "text/plain"))],
    )

    assert response.status_code == 422
    assert response.json()["error"] == "empty_attachment"
    assert _count(engine, "factory_launch_attachments") == before


def test_attachment_post_without_anything_is_refused(client):
    response = client.post("/v1/factory/attachments", data={"drive_source_id": ""})

    assert response.status_code == 422
    assert response.json()["error"] == "attachment_required"


def test_unconnected_drive_source_is_refused_and_not_fetched(client, engine):
    before = _count(engine, "factory_launch_attachments")

    response = client.post(
        "/v1/factory/attachments",
        data={"drive_source_id": "drive-source-not-connected"},
    )

    assert response.status_code == 409
    assert response.json() == {"error": "drive_not_connected", "message": _DRIVE}
    assert _count(engine, "factory_launch_attachments") == before


def test_github_repo_that_is_not_connected_is_the_existing_connect_error(
        client, engine, monkeypatch):
    calls: list = []
    _patch_builder(monkeypatch, calls)
    before = _count(engine, "projects")

    response = client.post(
        "/v1/factory/launch",
        json={"prompt": "Import the repo", "github_repo": "acme/not-connected"},
    )

    assert response.status_code == 409
    assert response.json() == {"error": "github_not_connected", "message": _CONNECT}
    assert calls == []
    assert _count(engine, "projects") == before


def test_unknown_template_is_not_a_launch(client, engine, monkeypatch, tmp_path):
    catalogue = tmp_path / "factory_templates"
    _write_catalogue(catalogue)
    monkeypatch.setattr("boss_engine.services.factory_launch.TEMPLATE_DIR", catalogue)
    calls: list = []
    _patch_builder(monkeypatch, calls)
    before = _count(engine, "projects")

    response = client.post("/v1/factory/launch", json={"template_id": "no-such-template"})

    assert response.status_code == 404
    assert response.json()["error"] == "unknown_template"
    assert calls == []
    assert _count(engine, "projects") == before


def test_attachment_from_another_org_is_unknown(client, engine, monkeypatch):
    other = "org_northpeak_bank_b"
    _exec(
        engine,
        """
        INSERT INTO factory_launch_attachments (
            id, org_id, kind, name, media_type, byte_size, text_extract
        ) VALUES (
            'fatt_other_org_row', :org, 'file', 'secret.txt', 'text/plain', 4, 'nope'
        )
        """,
        org=other,
    )
    calls: list = []
    _patch_builder(monkeypatch, calls)
    before = _count(engine, "projects")

    response = client.post(
        "/v1/factory/launch",
        json={"prompt": "Use the other org file", "attachment_ids": ["fatt_other_org_row"]},
    )

    assert response.status_code == 404
    assert response.json() == {
        "error": "unknown_attachment",
        "message": "That attachment is not in this organization.",
    }
    assert calls == []
    assert _count(engine, "projects") == before


def test_launch_without_a_run_is_a_failure_and_rolls_back(client, engine, monkeypatch, tmp_path):
    monkeypatch.setattr(
        "boss_engine.services.factory_launch.TEMPLATE_DIR", tmp_path / "absent")

    def no_run(connection, *, project_id, spec, actor, settings, jobs, reservation_id):
        return {"project_id": project_id}

    monkeypatch.setattr("boss_engine.services.factory_launch.build_run", no_run)
    before = _count(engine, "projects")
    receipts_before = _count(engine, "receipts")
    payload = {"prompt": "This build returns nothing"}

    refused = client.post("/v1/factory/launch", json=payload)

    assert refused.status_code == 502
    assert refused.json()["error"] == "launch_incomplete"
    assert "project_id" not in refused.json()
    assert _count(engine, "projects") == before
    assert _count(engine, "receipts") == receipts_before

    calls: list = []
    _patch_builder(monkeypatch, calls, run_id="run_after_rollback")
    accepted = client.post("/v1/factory/launch", json=payload)

    assert accepted.status_code == 201
    assert accepted.json()["run_id"] == "run_after_rollback"
    assert _count(engine, "projects") == before + 1
    assert calls and calls[0]["project_id"] == accepted.json()["project_id"]


def test_lucky_with_no_templates_uses_the_builtin_spec_and_one_receipt(
        client, engine, monkeypatch, tmp_path):
    monkeypatch.setattr(
        "boss_engine.services.factory_launch.TEMPLATE_DIR", tmp_path / "absent")
    order: list = []
    calls: list = []
    _patch_order(monkeypatch, order)
    _patch_builder(monkeypatch, calls)
    payload = {"prompt": "", "mode": "", "template_id": None, "attachment_ids": []}

    first = client.post("/v1/factory/launch", json=payload)
    second = client.post("/v1/factory/launch", json=payload)

    assert first.status_code == 201, first.text
    body = first.json()
    assert body["mode"] == "lucky"
    assert body["template_id"] == "lucky-notes"
    assert body["reused"] is False
    assert body["project_id"]
    assert body["run_id"] == "run_lucky_1"
    assert body["receipt_id"].startswith("rcpt_")
    assert body["reservation_id"]
    assert body["dedupe_key"].startswith("factory.launch:")
    assert body["resources"] == ["postgres"]
    assert body["workflows"][0]["name"] == "Build the notes app"
    assert body["workflows"][0]["workflow_key"].startswith("launch-")
    assert body["spec"]["title"] == "A small notes app"
    assert body["spec"]["stack"] == "vite-react"
    assert body["spec"]["github_repo"] is None
    assert order[0] == "reserve"
    assert calls[0]["reservation_id"] == body["reservation_id"]
    hold = _one(
        engine,
        "SELECT category, status FROM budget_reservations WHERE id = :id",
        id=body["reservation_id"],
    )
    assert hold["category"] == "code_task"
    assert hold["status"] == "active"
    quota = _one(
        engine,
        """
        SELECT resource_kind FROM resource_quotas
        WHERE project_id = :project AND scope_type = 'project' AND resource_kind = 'postgres'
        """,
        project=body["project_id"],
    )
    assert quota["resource_kind"] == "postgres"
    workflow = _one(
        engine,
        """
        SELECT name FROM workflow_definitions
        WHERE project_id = :project AND workflow_key = :key
        """,
        project=body["project_id"], key=body["workflows"][0]["workflow_key"],
    )
    assert workflow["name"] == "Build the notes app"
    receipts = _count_where(
        engine, "SELECT count(*) FROM receipts WHERE dedupe_key = :key AND status = 'succeeded'",
        key=body["dedupe_key"])
    assert receipts == 1

    assert second.status_code == 201
    again = second.json()
    assert again["reused"] is True
    assert again["project_id"] == body["project_id"]
    assert again["run_id"] == body["run_id"]
    assert again["receipt_id"] == body["receipt_id"]
    assert len(calls) == 1


def _count_where(engine, sql: str, **params) -> int:
    with engine.connect() as connection:
        return int(connection.execute(text(sql), params).scalar() or 0)


def test_empty_prompt_picks_the_first_template_and_template_id_picks_that_one(
        client, engine, monkeypatch, tmp_path):
    catalogue = tmp_path / "factory_templates"
    _write_catalogue(catalogue)
    monkeypatch.setattr("boss_engine.services.factory_launch.TEMPLATE_DIR", catalogue)
    calls: list = []
    _patch_builder(monkeypatch, calls, run_id="run_alpha")

    lucky = client.post("/v1/factory/launch", json={})
    chosen = client.post("/v1/factory/launch", json={"template_id": "beta-board"})

    assert lucky.status_code == 201, lucky.text
    assert lucky.json()["template_id"] == "alpha-notes"
    assert lucky.json()["mode"] == "lucky"
    assert lucky.json()["spec"]["title"] == "Alpha notes"
    assert lucky.json()["resources"] == ["postgres"]
    assert lucky.json()["workflows"][0]["name"] == "Build alpha"
    assert chosen.status_code == 201, chosen.text
    assert chosen.json()["template_id"] == "beta-board"
    assert chosen.json()["mode"] == "template"
    assert chosen.json()["resources"] == ["redis"]
    assert chosen.json()["project_id"] != lucky.json()["project_id"]
    assert len(calls) == 2


def test_prompt_is_the_spec_and_attachments_are_folded_in(
        client, engine, monkeypatch, tmp_path):
    monkeypatch.setattr(
        "boss_engine.services.factory_launch.TEMPLATE_DIR", tmp_path / "absent")
    stored = client.post(
        "/v1/factory/attachments",
        files=[
            ("spec", ("brief.md", b"Remember the milk\n", "text/markdown")),
            ("image", ("shot.png", b"\x89PNG\r\n\x1a\n\x00extra", "image/png")),
        ],
    )
    assert stored.status_code == 201, stored.text
    attachments = stored.json()["attachments"]
    assert [item["kind"] for item in attachments] == ["spec", "image"]
    assert attachments[0]["text_extract"] == "Remember the milk"
    assert attachments[0]["name"] == "brief.md"
    assert attachments[1]["text_extract"] == ""
    assert attachments[1]["byte_size"] > 0
    org = get_settings().default_org_id
    row = _one(
        engine,
        "SELECT org_id FROM factory_launch_attachments WHERE id = :id",
        id=attachments[0]["id"],
    )
    assert row["org_id"] == org

    calls: list = []
    _patch_builder(monkeypatch, calls, run_id="run_checklist")
    response = client.post("/v1/factory/launch", json={
        "prompt": _CHECKLIST,
        "template_id": "no-such-template",
        "attachment_ids": [item["id"] for item in attachments],
    })

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["mode"] == "prompt"
    assert body["template_id"] == "checklist"
    assert body["spec"]["title"] == "Ship a checklist"
    assert body["resources"] == ["postgres"]
    assert body["workflows"][0]["name"] == "Build the checklist"
    prompt = body["spec"]["prompt"]
    assert "Remember the milk" in prompt
    assert "brief.md (spec)" in prompt
    assert "shot.png (image)" in prompt
    names = [item["name"] for item in body["spec"]["attachments"]]
    assert names == ["brief.md", "shot.png"]
    assert calls[0]["spec"]["prompt"] == prompt
    assert "no-such-template" not in prompt


def test_connected_github_repo_is_passed_to_the_build(client, engine, monkeypatch, tmp_path):
    monkeypatch.setattr(
        "boss_engine.services.factory_launch.TEMPLATE_DIR", tmp_path / "absent")
    org = get_settings().default_org_id
    _exec(
        engine,
        """
        INSERT INTO github_repos (id, org_id, source, full_name, allowed, reachable, archived)
        VALUES ('gh_launch_acme_notes', :org, 'app', 'Acme/Notes', true, true, false)
        """,
        org=org,
    )
    calls: list = []
    _patch_builder(monkeypatch, calls, run_id="run_github")

    response = client.post("/v1/factory/launch", json={
        "prompt": "Add a dark mode toggle",
        "github_repo": "Acme/Notes",
    })

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["spec"]["github_repo"] == "Acme/Notes"
    assert body["spec"]["id"] == "prompt"
    assert body["spec"]["title"] == "Add a dark mode toggle"
    assert body["resources"] == []
    assert calls[0]["spec"]["github_repo"] == "Acme/Notes"
    assert calls[0]["project_id"] == body["project_id"]
