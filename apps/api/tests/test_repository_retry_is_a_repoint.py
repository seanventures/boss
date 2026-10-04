"""Fixing a token and pasting it again re-points the one connection; it never duplicates.

The retry fixes this preserves: "already has a GitHub connection with this label" and "already
has an active primary repository" were both dead ends on a second Connect.
"""
from sqlalchemy import text

from apps.api.tests.setup_helpers import AUTH, PROJECT, as_ceo, connect_repository, fakes


def test_replace_token_keeps_one_connection_and_one_binding(client, engine, monkeypatch):
    as_ceo(monkeypatch)
    fakes(monkeypatch)
    first = connect_repository(client, token="ghp_e2e_contents_read_only")
    [repo] = first["repository"]["repos"]
    assert repo["pill"]["label"] == "Can't write"
    assert repo["pill"]["tip"] == "Token needs Contents: Read and write."
    assert repo["pill"]["fix_url"].startswith("https://github.com/settings/personal-access-tokens")
    assert first["next"] == "repository"

    replaced = client.post("/v1/setup/repository/token", headers=AUTH, json={
        "project_id": PROJECT, "token": "ghp_e2e_full",
        "connection_id": first["repository"]["connection"]["id"]})
    assert replaced.status_code == 200, replaced.text
    assert replaced.json()["connection_id"] == first["repository"]["connection"]["id"]
    assert replaced.json()["rechecked"] == [repo["binding_id"]]
    again = client.get("/v1/setup", params={"project_id": PROJECT}, headers=AUTH).json()
    [repo2] = again["repository"]["repos"]
    assert repo2["binding_id"] == repo["binding_id"]
    assert repo2["pill"]["label"] == "Working"
    with engine.begin() as c:
        assert c.execute(text("SELECT count(*) FROM github_connections WHERE project_id=:p"),
                         {"p": PROJECT}).scalar() == 1
        assert c.execute(text("SELECT count(*) FROM github_repository_bindings "
                              "WHERE project_id=:p"), {"p": PROJECT}).scalar() == 1


def test_a_second_repo_is_added_and_can_become_the_default(client, monkeypatch):
    as_ceo(monkeypatch)
    fakes(monkeypatch)
    first = connect_repository(client)
    connection_id = first["repository"]["connection"]["id"]
    added = client.post("/v1/setup/repository/select", headers=AUTH, json={
        "project_id": PROJECT, "connection_id": connection_id, "full_name": "e2e/site",
        "make_default": False}).json()
    names = {r["full_name"]: r["default"] for r in added["repository"]["repos"]}
    assert names == {"e2e/app": True, "e2e/site": False}
    site = next(r for r in added["repository"]["repos"] if r["full_name"] == "e2e/site")
    swapped = client.post(f"/v1/setup/repository/{site['binding_id']}/default",
                          params={"project_id": PROJECT}, headers=AUTH).json()
    assert {r["full_name"]: r["default"] for r in swapped["repository"]["repos"]} == {
        "e2e/app": False, "e2e/site": True}


def test_a_token_github_refuses_is_never_stored(client, engine, monkeypatch):
    as_ceo(monkeypatch)
    fakes(monkeypatch)
    refused = client.post("/v1/setup/repository/token", headers=AUTH,
                          json={"project_id": PROJECT, "token": "ghp_e2e_expired"})
    assert refused.status_code == 422
    assert refused.json()["detail"]["headline"] == "Token rejected"
    with engine.begin() as c:
        assert c.execute(text("SELECT count(*) FROM github_connections WHERE project_id=:p"),
                         {"p": PROJECT}).scalar() == 0
