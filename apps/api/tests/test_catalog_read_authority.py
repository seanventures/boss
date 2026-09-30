"""Catalogs expose only projects and organizations the principal may read."""

from uuid import uuid4

from sqlalchemy import text

from apps.api.app import main as api_main


def test_project_and_org_catalogs_filter_by_active_membership(client, engine, monkeypatch):
    suffix = uuid4().hex[:10]
    home = "boss-dogfood"
    same_org_foreign = f"catalog_same_org_{suffix}"
    other_org = f"catalog_org_{suffix}"
    other_workspace = f"catalog_workspace_{suffix}"
    other_project = f"catalog_other_{suffix}"
    project_reader = f"catalog_project_reader_{suffix}"
    org_reader = f"catalog_org_reader_{suffix}"
    project_key = f"catalog-project-key-{suffix}"
    org_key = f"catalog-org-key-{suffix}"
    monkeypatch.setattr(api_main.settings, "require_api_auth", True)
    monkeypatch.setattr(api_main.settings, "api_keys", f"{project_key},{org_key}")
    monkeypatch.setattr(
        api_main.settings, "api_key_principals",
        f"{project_key}:{project_reader},{org_key}:{org_reader}",
    )

    with engine.begin() as connection:
        scope = connection.execute(
            text("""SELECT p.workspace_id, w.org_id FROM projects p
                    JOIN workspaces w ON w.id = p.workspace_id WHERE p.id = :id"""),
            {"id": home},
        ).mappings().one()
        connection.execute(
            text("""INSERT INTO projects (id, workspace_id, name, slug, created_at)
                    VALUES (:id, :workspace_id, 'Private sibling', :id,
                            now() - interval '10 years')"""),
            {"id": same_org_foreign, "workspace_id": scope["workspace_id"]},
        )
        connection.execute(
            text("INSERT INTO orgs (id, name) VALUES (:id, 'Foreign organization')"),
            {"id": other_org},
        )
        connection.execute(
            text("""INSERT INTO workspaces (id, org_id, name)
                    VALUES (:id, :org_id, 'Foreign workspace')"""),
            {"id": other_workspace, "org_id": other_org},
        )
        connection.execute(
            text("""INSERT INTO projects (id, workspace_id, name, slug)
                    VALUES (:id, :workspace_id, 'Foreign project', :id)"""),
            {"id": other_project, "workspace_id": other_workspace},
        )
        api_main.members.upsert_member(
            connection, project_id=home, member_id=project_reader,
            display_name="Project reader", member_type="human", role_keys=[],
            permissions=[], actor="ceo",
        )
        connection.execute(
            text("""INSERT INTO org_members
                    (id, org_id, member_id, display_name)
                    VALUES (:id, :org_id, :member_id, 'Org reader')"""),
            {"id": f"om_{suffix}", "org_id": scope["org_id"],
             "member_id": org_reader},
        )

    try:
        project_headers = {"Authorization": f"Bearer {project_key}"}
        project_list = client.get("/v1/projects", headers=project_headers)
        assert project_list.status_code == 200, project_list.text
        project_ids = {item["id"] for item in project_list.json()["items"]}
        assert home in project_ids
        assert same_org_foreign not in project_ids
        assert other_project not in project_ids

        org_list = client.get("/v1/orgs", headers=project_headers)
        assert org_list.status_code == 200, org_list.text
        visible_orgs = org_list.json()["items"]
        assert scope["org_id"] in {item["org_id"] for item in visible_orgs}
        assert other_org not in {item["org_id"] for item in visible_orgs}
        assert next(item for item in visible_orgs if item["org_id"] == scope["org_id"])[
            "project_id"
        ] == home

        org_headers = {"Authorization": f"Bearer {org_key}"}
        org_projects = client.get("/v1/projects", headers=org_headers)
        assert org_projects.status_code == 200, org_projects.text
        org_project_ids = {item["id"] for item in org_projects.json()["items"]}
        assert {home, same_org_foreign}.issubset(org_project_ids)
        assert other_project not in org_project_ids
    finally:
        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM project_members WHERE member_id = :member_id"),
                {"member_id": project_reader},
            )
            connection.execute(
                text("DELETE FROM org_members WHERE member_id = :member_id"),
                {"member_id": org_reader},
            )
            connection.execute(
                text("DELETE FROM projects WHERE id IN (:same_org, :other)"),
                {"same_org": same_org_foreign, "other": other_project},
            )
            connection.execute(
                text("DELETE FROM workspaces WHERE id = :id"),
                {"id": other_workspace},
            )
            connection.execute(
                text("DELETE FROM orgs WHERE id = :id"), {"id": other_org},
            )
