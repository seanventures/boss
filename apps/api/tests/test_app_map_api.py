"""API integration tests for BOSSAPP-555/556 App Map endpoints."""
from __future__ import annotations

import sys

from sqlalchemy import text

from boss_engine.db import begin


def _seed_project_with_binding(db, project_id: str, binding_id: str) -> None:
    ws_id = db.execute(text("SELECT id FROM workspaces LIMIT 1")).scalar()
    if not ws_id:
        org_id = "org_appmap_api"
        ws_id = "ws_appmap_api"
        db.execute(text("INSERT INTO orgs (id, name) VALUES (:o, 'Org') ON CONFLICT DO NOTHING"), {"o": org_id})
        db.execute(
            text("INSERT INTO workspaces (id, org_id, name) VALUES (:w, :o, 'WS') ON CONFLICT DO NOTHING"),
            {"w": ws_id, "o": org_id},
        )
    db.execute(text("DELETE FROM events WHERE project_id = :p"), {"p": project_id})
    db.execute(text("DELETE FROM projects WHERE id = :p"), {"p": project_id})
    db.execute(
        text("INSERT INTO projects (id, workspace_id, name, slug) VALUES (:p, :w, 'AppMap API', 'appmap-api')"),
        {"p": project_id, "w": ws_id},
    )
    db.execute(
        text(
            "INSERT INTO github_connections (id, project_id, connection_label, auth_method, credential_ref, status, created_by) "
            "VALUES (:cid, :p, 'test', 'github_app', 'ref:test', 'active', 'test')"
        ),
        {"cid": f"conn_{project_id}", "p": project_id},
    )
    db.execute(
        text(
            "INSERT INTO github_repository_bindings (id, project_id, connection_id, owner, name, full_name, default_branch, role, status, created_by) "
            "VALUES (:b, :p, :cid, 'acme', 'demo', 'acme/demo', 'main', 'primary', 'active', 'test')"
        ),
        {"b": binding_id, "p": project_id, "cid": f"conn_{project_id}"},
    )


def test_app_map_scan_confirm_drift_and_baseline_api(client, tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    svc = repo / "svc"
    svc.mkdir(parents=True)
    (svc / "pyproject.toml").write_text('[project]\nname = "svc"\n')
    (svc / "test_svc.py").write_text(
        "import unittest\nclass T(unittest.TestCase):\n    def test_ok(self): self.assertTrue(True)\n"
    )
    monkeypatch.setenv("BOSS_APP_MAP_SCAN_ROOTS", str(tmp_path))

    pid = "proj_am_api"
    bid = "bind_am_api"
    with begin() as db:
        _seed_project_with_binding(db, pid, bid)

    # 1. Initially unmapped
    r0 = client.get(f"/v1/apps/svc/app-map?project_id={pid}")
    assert r0.status_code == 200, r0.text
    assert r0.json()["status"] == "unmapped"
    assert r0.json()["app_map"] is None

    # 2. Disallowed arbitrary repo_path outside BOSS_APP_MAP_SCAN_ROOTS is refused (403)
    r_bad = client.post(f"/v1/projects/{pid}/app-maps/scan", json={"repo_path": "/etc"})
    assert r_bad.status_code == 403
    assert r_bad.json()["error"] == "repo_path_not_allowed"

    # 3. Scan allowlisted repo proposes `svc`
    r_scan = client.post(
        f"/v1/projects/{pid}/app-maps/scan",
        json={"repo_path": str(repo), "binding_id": bid, "persist": True},
    )
    assert r_scan.status_code == 200, r_scan.text
    body = r_scan.json()
    assert body["status"] == "proposed"
    assert body["selected"]["root_path"] == "svc"
    assert len(body["stored"]) == 1

    # 4. Confirm / edit via PUT /v1/apps/{app_key}/app-map
    r_put = client.put(
        "/v1/apps/svc/app-map",
        json={
            "project_id": pid,
            "root_path": "svc",
            "workspace_root": ".",
            "commands": {"test": f"{sys.executable} -m unittest discover -v"},
            "shared_paths": ["libs/shared"],
            "repo_path": str(repo),
            "confirm": True,
        },
    )
    assert r_put.status_code == 200, r_put.text
    confirmed = r_put.json()["app_map"]
    assert confirmed["status"] == "confirmed"
    assert confirmed["shared_paths"] == ["libs/shared"]
    assert confirmed["fingerprint"]

    # 5. List project app maps returns confirmed map
    r_list = client.get(f"/v1/projects/{pid}/app-maps")
    assert r_list.status_code == 200
    assert len(r_list.json()["items"]) == 1
    assert r_list.json()["items"][0]["root_path"] == "svc"

    # 6. Run baseline verify via POST /v1/apps/{app_key}/app-map/baseline
    r_base = client.post(
        "/v1/apps/svc/app-map/baseline",
        json={"project_id": pid, "repo_path": str(repo), "commit_sha": "deadbeef"},
    )
    assert r_base.status_code == 200, r_base.text
    assert r_base.json()["baseline"]["status"] == "passed"
    assert r_base.json()["app_map"]["baseline"]["commit_sha"] == "deadbeef"

    # 7. Mutate manifest -> rescan detects drift and marks map stale
    (svc / "pyproject.toml").write_text('[project]\nname = "svc"\nversion = "2.0.0"\n')
    r_drift = client.post(
        f"/v1/projects/{pid}/app-maps/scan",
        json={"repo_path": str(repo), "binding_id": bid, "persist": False},
    )
    assert r_drift.status_code == 200
    assert len(r_drift.json()["drifted"]) == 1
    assert r_drift.json()["drifted"][0]["drifted"] is True

    # 8. Invalid traversal root_path refused (422)
    r_trav = client.put(
        "/v1/apps/svc/app-map",
        json={"project_id": pid, "root_path": "../escape", "confirm": True},
    )
    assert r_trav.status_code == 422

    with begin() as db:
        db.execute(text("DELETE FROM events WHERE project_id = :p"), {"p": pid})
        db.execute(text("DELETE FROM projects WHERE id = :p"), {"p": pid})
