"""Easy Mode (BOSSAPP-1274) and the attachments it reads (BOSSAPP-1275).

The three routes are the seam. The builder is a stand-in: no model, no GitHub, no Drive.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import text

from apps.api.app.config import get_settings
from boss_engine.services.budget import BudgetGovernor
from boss_engine.services.factory_launch import MAX_ATTACHMENT_BYTES

_CONNECT = (
    "Connect GitHub first: a new project reaches a repository through the "
    "organisation's GitHub App (Setup › GitHub)."
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
toolchain: node
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
toolchain: node
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


@pytest.fixture(autouse=True)
def _local_repos_in_tmp(monkeypatch, tmp_path):
    """A launch with no GitHub repository makes the project a local repository: on disk, under
    this test's own folder, never the install's."""
    from apps.api.app.routers import factory_launch as route

    monkeypatch.setattr(route._settings, "local_repo_root", str(tmp_path / "repos"))


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


def test_consent_keeps_the_project_and_the_same_launch_builds_in_it(
        client, engine, monkeypatch, tmp_path):
    """EASY MODE UNDER CONSENT (2026-11-17): the build start's 409 consent_required used to roll
    back the project and the agents it named, so every retry made a new project and asked again.
    The route now commits the kept project, answers the flat consent body with its id, and the
    same launch sent after the grant builds in it: one project, one receipt, idempotent after."""
    from boss_engine.identity.consent import ConsentRequired

    monkeypatch.setattr(
        "boss_engine.services.factory_launch.TEMPLATE_DIR", tmp_path / "absent")
    granted = {"yes": False}
    calls: list = []

    def build(connection, *, project_id, spec, actor, settings, jobs, reservation_id):
        # What the start wrote before it asked goes back with the savepoint.
        connection.execute(text("UPDATE projects SET description = 'started' WHERE id = :p"),
                           {"p": project_id})
        if not granted["yes"]:
            raise ConsentRequired({
                "error": "consent_required", "task": spec["title"], "task_run_id": "pending:abc",
                "agents": [{"id": "agt_forge_x", "name": "Software Engineer",
                            "role_key": "software_engineer"}],
                "permissions": ["code_task:read"], "projects": [project_id], "github": None,
                "durations": [{"kind": "task", "label": "For this build only", "days": None}],
                "max_days": 90})
        calls.append({"project_id": project_id, "reservation_id": reservation_id})
        return {"project_id": project_id, "run_id": "run_after_consent"}

    monkeypatch.setattr("boss_engine.services.factory_launch.build_run", build)
    payload = {"prompt": "Build a CSV quality desk that flags bad rows.", "mode": "build"}
    projects_before = _count(engine, "projects")

    asked = client.post("/v1/factory/launch", json=payload)

    assert asked.status_code == 409, asked.text
    body = asked.json()
    assert body["error"] == "consent_required"                     # flat, as every start sends it
    assert body["agents"][0]["name"] == "Software Engineer"
    project_id = body["project_id"]
    assert body["retry"] == {"project_id": project_id, "dedupe_key": body["dedupe_key"]}
    # KEPT: the project and its workflows are committed; the start's own writes are not.
    project = _one(engine, "SELECT id, description FROM projects WHERE id = :p", p=project_id)
    assert project is not None and project["description"] != "started"
    assert _count(engine, "projects") == projects_before + 1
    assert _count_where(engine, "SELECT count(*) FROM workflow_definitions WHERE project_id = :p "
                        "AND workflow_key LIKE 'launch-%'", p=project_id) == 1
    # The reservation went back with the start (nothing held while the person decides).
    assert _count_where(engine, "SELECT count(*) FROM budget_reservations WHERE project_id = :p",
                        p=project_id) == 0
    assert _count_where(engine, "SELECT count(*) FROM receipts WHERE dedupe_key = :k AND "
                        "status = 'in_progress'", k=body["dedupe_key"]) == 1

    # Pressed again before consenting: the same project is asked about, never a new one.
    again = client.post("/v1/factory/launch", json=payload)
    assert again.status_code == 409 and again.json()["project_id"] == project_id
    assert _count(engine, "projects") == projects_before + 1

    granted["yes"] = True
    built = client.post("/v1/factory/launch", json=payload)

    assert built.status_code == 201, built.text
    done = built.json()
    assert done["project_id"] == project_id and done["run_id"] == "run_after_consent"
    assert done["reused"] is False
    assert calls == [{"project_id": project_id, "reservation_id": done["reservation_id"]}]
    assert _count(engine, "projects") == projects_before + 1
    rows = _count_where(engine, "SELECT count(*) FROM receipts WHERE dedupe_key = :k",
                        k=body["dedupe_key"])
    assert rows == 1
    assert _count_where(engine, "SELECT count(*) FROM receipts WHERE dedupe_key = :k AND "
                        "status = 'succeeded' AND result->>'run_id' = 'run_after_consent'",
                        k=body["dedupe_key"]) == 1
    assert done["receipt_id"] == body["receipt_id"]

    repeat = client.post("/v1/factory/launch", json=payload)
    assert repeat.status_code == 201
    assert repeat.json()["reused"] is True and repeat.json()["run_id"] == "run_after_consent"
    assert len(calls) == 1
    assert _count(engine, "projects") == projects_before + 1


def test_a_launch_with_no_github_repo_builds_in_a_real_local_repository(
        client, engine, monkeypatch, tmp_path):
    """EASY MODE NEVER BUILDS ON A PLACEHOLDER (2026-11-18): with no GitHub repository chosen it
    built against `_local/lucky`, which nothing could clone, and the build died with a 500. The
    project now gets its own local primary repository (the helper samples use) and the build is
    started against that repository's real name."""
    from apps.api.app.routers import factory_launch as route
    from boss_engine.services import local_repo

    monkeypatch.setattr(
        "boss_engine.services.factory_launch.TEMPLATE_DIR", tmp_path / "absent")
    calls: list = []
    _patch_builder(monkeypatch, calls, run_id="run_local_1")

    response = client.post("/v1/factory/launch", json={"prompt": "A CSV quality desk, locally."})

    assert response.status_code == 201, response.text
    body = response.json()
    project_id = body["project_id"]
    repo = body["spec"]["local_repo"]
    owner, name = repo.split("/", 1)
    assert owner == local_repo.owner_for(project_id)
    assert "lucky" not in repo and not repo.startswith("_local")
    assert calls[0]["spec"]["local_repo"] == repo
    assert calls[0]["spec"].get("github_repo") is None
    binding = _one(engine, "SELECT full_name, role, host FROM github_repository_bindings "
                   "WHERE project_id = :p AND status = 'active'", p=project_id)
    assert dict(binding) == {"full_name": repo, "role": "primary", "host": "local"}
    assert local_repo.exists(owner, name, route._settings)
    assert local_repo.bare_path(owner, name, route._settings).is_relative_to(tmp_path)


def test_the_real_build_start_names_the_local_repository(client, engine, monkeypatch, tmp_path):
    """The real build_run (start_code_task, no model): the run's spec is the local repository,
    with repo_host local, so its clone never asks GitHub."""
    from boss_engine.services import factory_launch

    seen: dict = {}

    def start(connection, **kw):
        seen.update(kw)
        return "crun_local_1"

    monkeypatch.setattr("boss_engine.services.code_task_service.start_code_task", start)
    out = factory_launch.build_run(
        None, project_id="prj_x", spec={"title": "CSV desk", "local_repo": "local_prj_x/csv-desk"},
        actor="ceo", settings=get_settings(), jobs=None, reservation_id="res_1")
    assert out == {"project_id": "prj_x", "run_id": "crun_local_1"}
    assert (seen["owner"], seen["repo"], seen["repo_host"]) == ("local_prj_x", "csv-desk", "local")

    with pytest.raises(factory_launch.FactoryLaunchError) as refused:
        factory_launch.build_run(None, project_id="prj_x", spec={"title": "CSV desk"},
                                 actor="ceo", settings=get_settings(), jobs=None,
                                 reservation_id="res_1")
    assert refused.value.code == "no_repository" and " " in refused.value.message


def test_a_refused_launch_leaves_no_repository_folder_behind(client, engine, monkeypatch, tmp_path):
    """The personal-data check runs after the project's local repository is made; when it blocks
    (or asks), the project rolls back and the folder made for it on this call is removed."""
    monkeypatch.setattr(
        "boss_engine.services.factory_launch.TEMPLATE_DIR", tmp_path / "absent")
    calls: list = []
    _patch_builder(monkeypatch, calls)
    before = _count(engine, "projects")

    for prompt, code in (("Bill card 4111 1111 1111 1111 every month.", "personal_data_blocked"),
                         ("Email the summary to ada@example.com daily.", "personal_data_ask")):
        refused = client.post("/v1/factory/launch", json={"prompt": prompt})
        assert refused.status_code == 409, refused.text
        assert refused.json()["error"] == code

    assert calls == []
    assert _count(engine, "projects") == before
    repos = tmp_path / "repos"
    assert not repos.exists() or list(repos.iterdir()) == []


_GO_DESK = """\
id: fixture-clock
title: Fixture clock
blurb: A frozen clock for demos.
category: internal
prompt: Build a Go API named Fixture Clock.
stack: Go API
toolchain: go
resources: []
workflows:
  - name: build
    steps:
      - scaffold
"""


def _gitignore_of(spec_repo: str) -> str:
    import subprocess

    from apps.api.app.routers import factory_launch as route
    from boss_engine.services import local_repo

    owner, name = spec_repo.split("/", 1)
    bare = local_repo.bare_path(owner, name, route._settings)
    return subprocess.run(["git", "--git-dir", str(bare), "show", "HEAD:.gitignore"],
                          capture_output=True, text=True, check=True).stdout


def test_a_picked_template_names_the_project_and_brings_its_stack(
        client, engine, monkeypatch, tmp_path):
    """Picking a card fills the brief AND sends template_id: the template is the spec (its title,
    its stack), the person's words are its prompt. The brief used to win outright: the project
    was named after its first 80 characters and the stack never reached .gitignore."""
    catalogue = tmp_path / "factory_templates"
    catalogue.mkdir()
    (catalogue / "go.yaml").write_text(_GO_DESK, encoding="utf-8")
    monkeypatch.setattr("boss_engine.services.factory_launch.TEMPLATE_DIR", catalogue)
    _toolchains(monkeypatch, tmp_path, '{"go": {"go": "1.27"}}')
    calls: list = []
    _patch_builder(monkeypatch, calls)
    brief = ("Build a Go API named Fixture Clock that listens on localhost and serves a frozen "
             "instant at GET /time, plus seeded users at GET /users.")

    response = client.post("/v1/factory/launch",
                           json={"prompt": brief, "template_id": "fixture-clock"})

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["mode"] == "template" and body["template_id"] == "fixture-clock"
    assert body["spec"]["title"] == "Fixture clock"
    assert body["spec"]["stack"] == "Go API"
    assert calls[0]["spec"]["prompt"] == brief                  # the person's words are built
    project = _one(engine, "SELECT name FROM projects WHERE id = :p", p=body["project_id"])
    assert project["name"] == "Fixture clock"
    repo = body["spec"]["local_repo"]
    assert repo.endswith("/fixture-clock")
    assert "/bin/" in _gitignore_of(repo).splitlines()          # Go's build output, ignored


def _toolchains(monkeypatch, tmp_path, manifest: str) -> None:
    """This install's toolchain manifest, as the image writes it ({} when none is turned on)."""
    from boss_engine.services import toolchains

    path = tmp_path / "toolchains-manifest.json"
    path.write_text(manifest, encoding="utf-8")
    monkeypatch.setenv(toolchains.MANIFEST_ENV, str(path))


def test_a_template_that_needs_go_is_listed_as_such_and_refused_in_words_without_go(
        client, engine, monkeypatch, tmp_path):
    """Stranger test 2026-11-18: the gallery offered templates the install could not build. A Go
    template on an install without Go is shown, marked, and its launch is refused in words before
    anything is made or reserved."""
    catalogue = tmp_path / "factory_templates"
    catalogue.mkdir()
    (catalogue / "go.yaml").write_text(_GO_DESK, encoding="utf-8")
    (catalogue / "b.yaml").write_text(_BETA, encoding="utf-8")
    monkeypatch.setattr("boss_engine.services.factory_launch.TEMPLATE_DIR", catalogue)
    _toolchains(monkeypatch, tmp_path, "{}")
    calls: list = []
    _patch_builder(monkeypatch, calls)

    listed = {t["id"]: t for t in client.get("/v1/factory/templates").json()["templates"]}
    assert listed["beta-board"]["available"] is True and listed["beta-board"]["needs"] is None
    go = listed["fixture-clock"]
    assert go["available"] is False and go["toolchain"] == "go"
    assert go["needs"]["label"] == "Needs Go"
    assert go["needs"]["enable"] == "boss toolchains enable go"

    projects, receipts = _count(engine, "projects"), _count(engine, "receipts")
    refused = client.post("/v1/factory/launch", json={"template_id": "fixture-clock"})
    assert refused.status_code == 409, refused.text
    body = refused.json()
    assert body["error"] == "toolchain_not_enabled"
    assert body["message"].startswith("Not started: Fixture clock is built in Go")
    assert "`boss toolchains enable go`" in body["message"]
    assert calls == []
    assert _count(engine, "projects") == projects and _count(engine, "receipts") == receipts

    lucky = client.post("/v1/factory/launch", json={})                # never lands on Go
    assert lucky.status_code == 201, lucky.text
    assert lucky.json()["template_id"] == "beta-board"          # fixture-clock sorts first

    _toolchains(monkeypatch, tmp_path, '{"go": {"go": "1.27"}}')
    listed = {t["id"]: t for t in client.get("/v1/factory/templates").json()["templates"]}
    assert listed["fixture-clock"]["available"] is True


def test_a_free_text_brief_names_the_project_by_its_first_words(client, engine, monkeypatch,
                                                                 tmp_path):
    from boss_engine.services.factory_launch import TITLE_CHARS

    monkeypatch.setattr("boss_engine.services.factory_launch.TEMPLATE_DIR", tmp_path / "absent")
    _patch_builder(monkeypatch, [])
    brief = ("Build a Python app named CSV Quality Desk with a command that reads a CSV path and "
             "reports bad rows.")

    response = client.post("/v1/factory/launch", json={"prompt": brief})

    assert response.status_code == 201, response.text
    title = response.json()["spec"]["title"]
    assert title == "Build a Python app named CSV Quality"
    assert len(title) <= TITLE_CHARS and brief.startswith(title)
    assert brief[len(title)] == " "                              # cut between words
    project = _one(engine, "SELECT name FROM projects WHERE id = :p",
                   p=response.json()["project_id"])
    assert project["name"] == title
    assert response.json()["spec"]["local_repo"].endswith("/build-a-python-app-named-csv-quality")
    assert "/bin/" not in _gitignore_of(response.json()["spec"]["local_repo"]).splitlines()


def test_prose_titles_cut_at_a_word_boundary():
    from boss_engine.services.factory_launch import prose_title

    assert prose_title("A pantry list.") == "A pantry list"
    assert prose_title("x") == "Easy Mode"
    assert prose_title("Supercalifragilisticexpialidociousandmoreandmore letters") == \
        "Supercalifragilisticexpialidociousandmor"
    assert prose_title("Track the oncall rotation, page whoever is up, and post a weekly summary") \
        == "Track the oncall rotation, page whoever"
