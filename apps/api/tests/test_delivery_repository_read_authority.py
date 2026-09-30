"""Repository pickers use the caller's project before hosted repository reads."""

import json
from uuid import uuid4

from sqlalchemy import text

from apps.api.app import main as api_main
from boss_engine.services import delivery_sources


def test_repository_catalog_and_host_reads_are_project_scoped(client, engine, monkeypatch):
    suffix = uuid4().hex[:10]
    home = "boss-dogfood"
    foreign = f"repo_foreign_{suffix}"
    reader = f"repo_reader_{suffix}"
    key = f"repo-key-{suffix}"
    manager = f"repo_manager_{suffix}"
    manager_key = f"repo-manager-key-{suffix}"
    home_repo = f"home-{suffix}"
    foreign_repo = f"foreign-{suffix}"
    other_repo = f"other-{suffix}"
    monkeypatch.setattr(api_main.settings, "require_api_auth", True)
    monkeypatch.setattr(api_main.settings, "api_keys", f"{key},{manager_key}")
    monkeypatch.setattr(api_main.settings, "api_key_principals",
                        f"{key}:{reader},{manager_key}:{manager}")
    headers = {"Authorization": f"Bearer {key}"}
    manager_headers = {"Authorization": f"Bearer {manager_key}"}

    with engine.begin() as connection:
        workspace_id = connection.execute(
            text("SELECT workspace_id FROM projects WHERE id=:id"), {"id": home},
        ).scalar_one()
        connection.execute(
            text("INSERT INTO projects (id, workspace_id, name, slug) "
                 "VALUES (:id, :workspace_id, 'Foreign repo', :id)"),
            {"id": foreign, "workspace_id": workspace_id},
        )
        api_main.members.upsert_member(
            connection, project_id=home, member_id=reader,
            display_name="Repo reader", member_type="human", role_keys=[],
            permissions=[], actor="ceo",
        )
        api_main.members.upsert_member(
            connection, project_id=home, member_id=manager,
            display_name="Repo manager", member_type="human", role_keys=[],
            permissions=["providerops_manage"], actor="ceo",
        )
        for run_id, project_id, repo, branch in (
            (f"crun_home_{suffix}", home, home_repo, "branch-home"),
            (f"crun_other_{suffix}", home, other_repo, "branch-other"),
            (f"crun_foreign_{suffix}", foreign, foreign_repo, "branch-private"),
        ):
            connection.execute(
                text("INSERT INTO code_task_runs "
                     "(id, project_id, title, status, phase, spec, finalize_ctx) "
                     "VALUES (:id, :project_id, 'Repo run', 'completed', 'done', "
                     "CAST(:spec AS JSONB), CAST(:finalize_ctx AS JSONB))"),
                {"id": run_id, "project_id": project_id,
                 "spec": json.dumps({"owner": "owner", "repo": repo}),
                 "finalize_ctx": json.dumps({"work_branch": branch})},
            )

    host_calls = []

    def fake_github(path, token):
        host_calls.append(path)
        if path.endswith("/branches?per_page=100&page=1"):
            return []
        if path.endswith("/contents"):
            return []
        return {"default_branch": "main"}

    monkeypatch.setattr(delivery_sources, "_github", fake_github)
    catalog = client.get("/v1/delivery/repos", headers=headers)
    assert catalog.status_code == 200, catalog.text
    names = {item["repo"] for item in catalog.json()["repos"]}
    assert home_repo in names and other_repo in names and foreign_repo not in names
    assert client.get(
        "/v1/delivery/repos", params={"project_id": foreign}, headers=headers,
    ).status_code == 403
    assert client.get(
        f"/v1/delivery/repos/owner/{foreign_repo}/refs",
        params={"project_id": foreign}, headers=headers,
    ).status_code == 403
    assert client.get(
        f"/v1/delivery/repos/owner/{foreign_repo}/apps", headers=headers,
    ).status_code == 403
    assert client.get(
        f"/v1/delivery/repos/owner/{home_repo}/refs", headers=headers,
    ).status_code == 403
    assert client.get(
        f"/v1/delivery/repos/owner/{home_repo}/apps", headers=headers,
    ).status_code == 403
    assert client.get(
        f"/v1/delivery/repos/owner/{foreign_repo}/apps", headers=manager_headers,
    ).status_code == 404
    assert host_calls == []

    refs = client.get(
        f"/v1/delivery/repos/owner/{home_repo}/refs", headers=manager_headers,
    )
    assert refs.status_code == 200, refs.text
    names = {item["name"] for item in refs.json()["branches"]}
    assert "branch-home" in names
    assert "branch-other" not in names and "branch-private" not in names
    apps = client.get(
        f"/v1/delivery/repos/owner/{home_repo}/apps", headers=manager_headers,
    )
    assert apps.status_code == 200, apps.text
    assert host_calls
