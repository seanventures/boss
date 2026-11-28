"""BOSSAPP-556 (App Map 2) — Scanner precedence, ambiguity, drift detection, and baseline verify."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest
from sqlalchemy import text

from boss_engine.db import begin
from boss_engine.services import app_map, app_map_scanner


def test_scanner_precedence_boss_apps_yaml_always_wins(tmp_path):
    repo = tmp_path / "yaml_wins"
    repo.mkdir()
    (repo / "pom.xml").write_text("<project><modules><module>api</module></modules></project>\n")
    (repo / "api").mkdir()
    (repo / "api" / "pom.xml").write_text("<project/>\n")
    (repo / ".boss").mkdir()
    (repo / ".boss" / "apps.yaml").write_text(
        "version: 1\n"
        "apps:\n"
        "  - key: custom-svc\n"
        "    name: Custom Service\n"
        "    root_path: services/custom\n"
        "    commands:\n"
        "      test: python3 -m unittest\n"
        "    shared_paths:\n"
        "      - libs/common\n"
    )
    (repo / "services" / "custom").mkdir(parents=True)
    (repo / "services" / "custom" / "pyproject.toml").write_text('[project]\nname = "custom"\n')

    res = app_map_scanner.scan_repository(repo)
    assert res.status == "proposed"
    assert res.source == "boss_apps_yaml"
    assert res.selected is not None
    assert res.selected.app_key == "custom-svc"
    assert res.selected.root_path == "services/custom"
    assert res.selected.commands["test"] == "python3 -m unittest"
    assert res.selected.shared_paths == ("libs/common",)
    assert res.selected.status == "confirmed"


def test_scanner_precedence_workspaces_over_descriptors_and_markers(tmp_path):
    repo = tmp_path / "ws_prio"
    repo.mkdir()
    (repo / "pnpm-workspace.yaml").write_text("packages:\n  - 'apps/*'\n  - 'packages/*'\n")
    (repo / "turbo.json").write_text(json.dumps({"tasks": {"build": {}}}))
    (repo / "package.json").write_text(json.dumps({"name": "root", "private": True}))
    (repo / "apps" / "web").mkdir(parents=True)
    (repo / "apps" / "web" / "package.json").write_text(json.dumps({
        "name": "@org/web", "dependencies": {"next": "^15.0.0"}, "scripts": {"test": "vitest run"},
    }))
    (repo / "packages" / "ui").mkdir(parents=True)
    (repo / "packages" / "ui" / "package.json").write_text(json.dumps({"name": "@org/ui"}))
    # Descriptor in another folder should NOT override native workspace graph
    (repo / "legacy").mkdir()
    (repo / "legacy" / "Dockerfile").write_text("FROM alpine\n")
    (repo / "legacy" / "requirements.txt").write_text("flask\n")

    res = app_map_scanner.scan_repository(repo)
    assert res.status == "proposed"
    assert res.source == "workspace_graph:turborepo"
    assert res.selected is not None
    assert res.selected.root_path == "apps/web"
    assert res.selected.workspace_root == "."
    assert "packages/ui" in res.selected.shared_paths


def test_scanner_precedence_descriptors_over_stray_markers(tmp_path):
    repo = tmp_path / "desc_prio"
    (repo / ".github" / "workflows").mkdir(parents=True)
    (repo / ".github" / "workflows" / "ci.yml").write_text(
        "jobs:\n  test:\n    defaults:\n      run:\n        working-directory: services/portal\n"
    )
    (repo / "services" / "portal").mkdir(parents=True)
    (repo / "services" / "portal" / "package.json").write_text(json.dumps({
        "name": "portal", "dependencies": {"express": "^5.0.0"}, "scripts": {"test": "node --test"},
    }))
    # Stray unreferenced folder with a manifest
    (repo / "scratch").mkdir()
    (repo / "scratch" / "requirements.txt").write_text("requests\n")

    res = app_map_scanner.scan_repository(repo)
    assert res.status == "proposed"
    assert res.source == "descriptor"
    assert res.selected is not None
    assert res.selected.root_path == "services/portal"


def test_scanner_ambiguity_asks_with_list_and_model_hook_runs_last(tmp_path):
    repo = tmp_path / "ambig"
    repo.mkdir()
    for name in ("alpha", "beta", "gamma"):
        (repo / name).mkdir()
        (repo / name / "package.json").write_text(json.dumps({
            "name": name, "dependencies": {"express": "^5.0.0"}, "scripts": {"test": "node --test"},
        }))

    # Without model hook: returns ambiguous with full candidate list, selected=None
    res = app_map_scanner.scan_repository(repo)
    assert res.status == "ambiguous"
    assert res.selected is None
    assert [a.root_path for a in res.apps] == ["alpha", "beta", "gamma"]
    assert res.refusal and "alpha/" in res.refusal

    # Model hook is invoked LAST when ambiguous and can narrow down
    called: list[int] = []

    def fake_model(_base: Path, candidates: list[app_map.AppMapEntry]) -> list[app_map.AppMapEntry]:
        called.append(len(candidates))
        return [c for c in candidates if c.root_path == "beta"]

    res_model = app_map_scanner.scan_repository(repo, model_classifier=fake_model)
    assert called == [3]
    assert res_model.status == "proposed"
    assert res_model.source == "model"
    assert res_model.selected is not None and res_model.selected.root_path == "beta"


def test_manifest_fingerprint_drift_triggers_reproposal(tmp_path):
    repo = tmp_path / "drift_repo"
    app = repo / "app"
    app.mkdir(parents=True)
    pkg = app / "package.json"
    pkg.write_text(json.dumps({"name": "demo", "scripts": {"test": "node --test"}}))

    scan1 = app_map_scanner.scan_repository(repo)
    assert scan1.selected is not None
    entry = scan1.selected

    # Unchanged tree -> no drift
    d0 = app_map.detect_manifest_drift(entry, repo)
    assert d0["drifted"] is False
    assert d0["new_fingerprint"] == entry.fingerprint

    # Mutate package.json -> fingerprint changes and re-proposal is produced
    pkg.write_text(json.dumps({"name": "demo", "dependencies": {"fastify": "^5.0.0"}, "scripts": {"test": "vitest run"}}))
    d1 = app_map.detect_manifest_drift(entry, repo)
    assert d1["drifted"] is True
    assert d1["new_fingerprint"] != entry.fingerprint
    assert d1["proposal"] is not None
    assert d1["proposal"]["stack"]["framework"] == "fastify"


def test_baseline_verify_real_execution_pass_and_fail(tmp_path):
    repo = tmp_path / "baseline_py"
    svc = repo / "svc"
    svc.mkdir(parents=True)
    (svc / "pyproject.toml").write_text('[project]\nname = "svc"\n')
    (svc / "test_ok.py").write_text(
        "import unittest\nclass T(unittest.TestCase):\n    def test_one(self): self.assertEqual(2 + 2, 4)\n"
    )
    entry = app_map.AppMapEntry(
        app_key="svc",
        app_name="Svc",
        root_path="svc",
        workspace_root=".",
        commands={"test": f"{sys.executable} -m unittest discover -v"},
        status="confirmed",
    )

    passed = app_map.verify_baseline(repo, entry, commit_sha="abc1234")
    assert passed["status"] == "passed"
    assert passed["commit_sha"] == "abc1234"
    assert passed["root_path"] == "svc"
    assert len(passed["steps"]) == 1 and passed["steps"][0]["ok"] is True

    # Now break the test -> baseline records failure honestly
    (svc / "test_ok.py").write_text(
        "import unittest\nclass T(unittest.TestCase):\n    def test_one(self): self.assertEqual(2 + 2, 5)\n"
    )
    failed = app_map.verify_baseline(repo, entry, commit_sha="def5678")
    assert failed["status"] == "failed"
    assert failed["error"] and "test failed" in failed["error"]
    assert failed["steps"][0]["ok"] is False


@pytest.mark.skipif(not os.environ.get("BOSS_TEST_DATABASE_URL"), reason="requires test Postgres")
def test_baseline_verify_persists_on_app_components_and_emits_audit_event(tmp_path):
    repo = tmp_path / "baseline_db"
    svc = repo / "svc"
    svc.mkdir(parents=True)
    (svc / "pyproject.toml").write_text('[project]\nname = "svc"\n')
    (svc / "test_ok.py").write_text(
        "import unittest\nclass T(unittest.TestCase):\n    def test_one(self): self.assertTrue(True)\n"
    )
    pid = "proj_appmap_556"
    bid = "bind_appmap_556"
    with begin() as c:
        ws_id = c.execute(text("SELECT id FROM workspaces LIMIT 1")).scalar()
        if not ws_id:
            pytest.skip("test DB not seeded")
        c.execute(text("DELETE FROM events WHERE project_id = :p"), {"p": pid})
        c.execute(text("DELETE FROM projects WHERE id = :p"), {"p": pid})
        c.execute(
            text("INSERT INTO projects (id, workspace_id, name, slug) VALUES (:p, :w, 'AppMap 556', 'appmap-556')"),
            {"p": pid, "w": ws_id},
        )
        c.execute(
            text(
                "INSERT INTO github_connections (id, project_id, connection_label, auth_method, credential_ref, status, created_by) "
                "VALUES (:cid, :p, 'test', 'github_app', 'ref:test', 'active', 'test')"
            ),
            {"cid": f"conn_{pid}", "p": pid},
        )
        c.execute(
            text(
                "INSERT INTO github_repository_bindings (id, project_id, connection_id, owner, name, full_name, default_branch, role, status, created_by) "
                "VALUES (:b, :p, :cid, 'acme', 'svc', 'acme/svc', 'main', 'primary', 'active', 'test')"
            ),
            {"b": bid, "p": pid, "cid": f"conn_{pid}"},
        )
        saved = app_map.confirm_app_map(
            c, pid, "svc",
            {
                "root_path": "svc",
                "commands": {"test": f"{sys.executable} -m unittest discover -v"},
                "confirm": True,
            },
            repo_root=repo,
            actor="operator_test",
        )
        assert saved.status == "confirmed"
        baseline = app_map.verify_baseline(repo, saved, db=c, project_id=pid, commit_sha="111aaaa", actor="operator_test")
        assert baseline["status"] == "passed"

        reloaded = app_map.load_app_map(c, pid, app_key="svc")
        assert reloaded is not None
        assert reloaded.baseline.get("status") == "passed"
        assert reloaded.baseline.get("commit_sha") == "111aaaa"

        ev = c.execute(
            text("SELECT event_type FROM events WHERE project_id = :p AND event_type = 'app_map.baseline_verified'"),
            {"p": pid},
        ).scalar()
        assert ev == "app_map.baseline_verified"

        c.execute(text("DELETE FROM events WHERE project_id = :p"), {"p": pid})
        c.execute(text("DELETE FROM projects WHERE id = :p"), {"p": pid})
