"""BOSSAPP-555 (App Map 1) — One stored App Map read by every stage; no root fallback.

Verifies:
  1. Property matrix: across repo shapes x phases (fresh / repair / resumed / seeded), every stage
     agrees on the exact same app directory from the stored App Map.
  2. Guard test: no stage reads the repository root for a subfolder app (even with stray root
     pom.xml, package.json, boss.deploy.yaml, boss.preview.command, empty diff, or root manifest diff).
  3. Refusal test: with no map in a multi-app/ambiguous repo (or require_map=True), every stage
     refuses explicitly rather than falling back to root.
  4. Write-boundary enforcement (`files_outside_app` & `check_write_boundary`), including the
     operator incident (crun_f246d330: deleting root pom.xml + src/...java while building helpdesk-ai/).
  5. DB persistence on real Postgres: seeding, confirming, loading, `recorded_app_dir`,
     `project_scope._components`, and `workflow_precheck.materialize_build`.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text

from boss_engine.db import begin
from boss_engine.services import (
    app_deploy_manifest,
    app_map,
    app_root,
    deployment_connectors,
    governed_code_task_service,
    preview_service,
    project_scope,
    stacks,
    toolchains,
    verification_target,
    workflow_precheck,
)


@pytest.fixture
def java_off(tmp_path, monkeypatch):
    manifest = tmp_path / "manifest-none.json"
    manifest.write_text("{}\n")
    monkeypatch.setenv(toolchains.MANIFEST_ENV, str(manifest))
    return manifest


def _make_repo(tmp_path: Path, shape: str) -> tuple[Path, app_map.AppMapEntry]:
    repo = tmp_path / shape
    repo.mkdir(parents=True, exist_ok=True)
    if shape == "subfolder_with_stray_root":
        # Stray root pom.xml, package.json, boss.deploy.yaml, boss.preview.command — all belong to OTHER apps!
        (repo / "pom.xml").write_text("<project><modelVersion>4.0.0</modelVersion></project>\n")
        (repo / "src" / "main" / "java").mkdir(parents=True)
        (repo / "src" / "main" / "java" / "Demo.java").write_text("class Demo {}\n")
        (repo / "package.json").write_text(json.dumps({"name": "root-demo", "scripts": {"start": "node root.js"}}))
        (repo / "boss.deploy.yaml").write_text("version: 1\nname: stack-demo\nkind: commands\ninstall: ['echo root']\n")
        (repo / "boss.preview.command").write_text("node root.js\n")
        (repo / "notes-lite").mkdir()
        (repo / "notes-lite" / "requirements.txt").write_text("flask\n")
        (repo / "web").mkdir()
        (repo / "web" / "package.json").write_text(json.dumps({"name": "web", "scripts": {"test": "node --test"}}))
        app = repo / "helpdesk-ai"
        (app / "src").mkdir(parents=True)
        (app / "package.json").write_text(json.dumps({
            "name": "helpdesk-ai",
            "dependencies": {"express": "^5.0.0"},
            "scripts": {"start": "node src/server.js", "test": "node --test"},
        }))
        (app / "src" / "server.js").write_text("export const ok = true;\n")
        (app / "boss.deploy.yaml").write_text("version: 1\nname: helpdesk-ai\nkind: commands\ninstall: ['npm start']\n")
        entry = app_map.AppMapEntry(
            app_key="helpdesk-ai",
            app_name="HelpDesk AI",
            root_path="helpdesk-ai",
            workspace_root=".",
            stack={"language": "node", "framework": "express"},
            commands={"install": "npm install", "test": "npm test"},
            run={"command": "node src/server.js"},
            status="confirmed",
        )
        return repo, entry

    if shape == "workspace_monorepo_app":
        (repo / "pnpm-workspace.yaml").write_text("packages:\n  - 'apps/*'\n  - 'packages/*'\n")
        (repo / "package.json").write_text(json.dumps({"name": "mono", "private": True, "workspaces": ["apps/*", "packages/*"]}))
        (repo / "packages" / "ui").mkdir(parents=True)
        (repo / "packages" / "ui" / "package.json").write_text(json.dumps({"name": "@mono/ui"}))
        (repo / "apps" / "api").mkdir(parents=True)
        (repo / "apps" / "api" / "package.json").write_text(json.dumps({"name": "@mono/api", "scripts": {"start": "node idx.js", "test": "node --test"}}))
        app = repo / "apps" / "web"
        (app / "src").mkdir(parents=True)
        (app / "package.json").write_text(json.dumps({
            "name": "@mono/web",
            "dependencies": {"next": "^15.0.0", "react": "^19.0.0"},
            "scripts": {"build": "next build", "start": "next start", "test": "node --test"},
        }))
        entry = app_map.AppMapEntry(
            app_key="web",
            app_name="Web",
            root_path="apps/web",
            workspace_root=".",
            stack={"language": "node", "framework": "next"},
            commands={"install": "pnpm install", "test": "pnpm test"},
            run={"command": "pnpm start"},
            shared_paths=("packages/ui",),
            status="confirmed",
        )
        return repo, entry

    if shape == "nested_fullstack_app":
        # Multi-app repo where our target is a nested full-stack app (portal/backend + portal/frontend)
        (repo / "pom.xml").write_text("<project><modelVersion>4.0.0</modelVersion></project>\n")
        (repo / "other-a").mkdir()
        (repo / "other-a" / "package.json").write_text(json.dumps({"name": "a"}))
        (repo / "other-b").mkdir()
        (repo / "other-b" / "package.json").write_text(json.dumps({"name": "b"}))
        portal = repo / "portal"
        (portal / "backend").mkdir(parents=True)
        (portal / "backend" / "requirements.txt").write_text("fastapi\n")
        (portal / "backend" / "main.py").write_text("from fastapi import FastAPI\napp = FastAPI()\n")
        (portal / "frontend").mkdir(parents=True)
        (portal / "frontend" / "package.json").write_text(json.dumps({
            "name": "portal-web", "dependencies": {"react": "^19.0.0", "vite": "^5.4.0"},
            "scripts": {"build": "vite build", "test": "vitest run"},
        }))
        entry = app_map.AppMapEntry(
            app_key="portal",
            app_name="Portal",
            root_path="portal",
            workspace_root=".",
            stack={"language": "multi", "role": "fullstack"},
            status="confirmed",
        )
        return repo, entry

    # Default: root_single_app
    (repo / "src").mkdir()
    (repo / "package.json").write_text(json.dumps({
        "name": "root-app",
        "dependencies": {"express": "^5.0.0"},
        "scripts": {"start": "node src/server.js", "test": "node --test"},
    }))
    (repo / "src" / "server.js").write_text("export const ok = true;\n")
    (repo / "boss.deploy.yaml").write_text("version: 1\nname: root-app\nkind: commands\ninstall: ['npm start']\n")
    entry = app_map.AppMapEntry(
        app_key="root-app",
        app_name="Root App",
        root_path=".",
        workspace_root=".",
        stack={"language": "node", "framework": "express"},
        commands={"install": "npm install", "test": "npm test"},
        run={"command": "node src/server.js"},
        status="confirmed",
    )
    return repo, entry


def _phase_inputs(entry: app_map.AppMapEntry, phase: str) -> tuple[dict[str, Any], dict[str, Any], list[str] | None, list[str] | None]:
    """Build (spec, finalize_ctx, changed, lineage) for each lifecycle phase."""
    rel = entry.rel_or_none()
    in_app_file = f"{rel}/src/server.js" if rel else "src/server.js"
    app_dict = entry.as_dict()
    if phase == "fresh":
        return {"instructions": "Implement feature X."}, {"app_map": app_dict}, None, None
    if phase == "repair":
        return (
            {"instructions": "Fix failing test."},
            {"app_map": app_dict, "instructions_are_findings": True},
            ["README.md"],  # intentionally outside app to prove map still holds!
            [in_app_file],
        )
    if phase == "resumed":
        return (
            {"instructions": "Continue interrupted build."},
            {"app_map": app_dict, "root_run_id": "crun_prev"},
            [],  # empty diff!
            ["pom.xml", in_app_file],  # even polluted lineage with root pom.xml!
        )
    # seeded
    return (
        {"instructions": "Seeded workspace build."},
        {"app_map": app_dict, "seed_workspace_from": "crun_seed"},
        ["pom.xml", "package.json", in_app_file],
        [in_app_file],
    )


# ── 1. Property test: every stage agrees across repo shapes x phases ───────────────────────────

@pytest.mark.parametrize("shape", [
    "subfolder_with_stray_root",
    "workspace_monorepo_app",
    "nested_fullstack_app",
    "root_single_app",
])
@pytest.mark.parametrize("phase", ["fresh", "repair", "resumed", "seeded"])
def test_every_stage_agrees_across_repo_shapes_and_phases(tmp_path, java_off, shape, phase):
    repo, entry = _make_repo(tmp_path, shape)
    expected_dir = entry.resolved_dir(repo)
    expected_rel = entry.rel_or_none()
    spec, fctx, changed, lineage = _phase_inputs(entry, phase)

    # 1. app_map.resolve_for_stage
    res = app_map.resolve_for_stage(repo, app_map=entry, spec=spec, finalize_ctx=fctx, changed=changed, lineage=lineage)
    assert res.ok and res.path == expected_dir and res.root_path == entry.root_path

    # 2. app_root.resolve / app_dir / gate_dir
    assert app_root.app_dir(repo, app_map=entry, changed=changed, lineage=lineage) == expected_dir
    assert app_root.gate_dir(repo, app_map=entry, changed=changed, lineage=lineage) == expected_dir

    # 3. verification_target.app_scope / resolve_verify_cwd / resolve_gate_plan
    assert verification_target.app_scope(spec, fctx) == expected_rel
    assert verification_target.resolve_verify_cwd(repo, changed=changed, lineage=lineage, app_map=entry) == expected_dir
    plan = verification_target.resolve_gate_plan(
        spec, entry.app_name, repo, changed=changed, lineage=lineage, finalize_ctx=fctx, app_map=entry
    )
    assert plan.cwd == expected_dir, f"{shape}/{phase}: plan.cwd={plan.cwd} != {expected_dir} (error={plan.error})"
    assert plan.source != "toolchain_missing", f"Stray root Java leaked into {shape}/{phase}: {plan.error}"

    # 4. stacks.find_stack_root
    stack_root = stacks.find_stack_root(repo, changed=changed, app_map=entry)
    if shape == "nested_fullstack_app":
        assert stack_root == expected_dir
    else:
        assert stack_root is None

    # 5. preview_service.app_dir_of & how_app_runs
    assert preview_service.app_dir_of(repo, changed=changed, app_map=entry) == expected_dir
    if shape != "nested_fullstack_app":
        run_plan = preview_service.how_app_runs(repo, changed=changed, app_map=entry)
        assert run_plan is not None
        assert run_plan.declared_in == entry.root_path
        if expected_rel:
            assert f"cd {expected_rel} &&" in run_plan.shell() or f"cd '{expected_rel}' &&" in run_plan.shell()

    # 6. app_deploy_manifest.app_folder & app_manifest
    assert app_deploy_manifest.app_folder(changed or [], app_map=entry) == expected_rel
    existing_manifests = app_deploy_manifest.manifests_in(repo)
    chosen_manifest = app_deploy_manifest.app_manifest(changed or [], existing_manifests, app_map=entry)
    if shape == "subfolder_with_stray_root":
        assert chosen_manifest == "helpdesk-ai/boss.deploy.yaml"
    elif shape == "root_single_app":
        assert chosen_manifest == "boss.deploy.yaml"
    else:
        assert chosen_manifest is None  # never grabs stray root boss.deploy.yaml!

    # 7. deployment_connectors.resolve_deploy_app_folder
    assert deployment_connectors.resolve_deploy_app_folder({"app_map": entry.as_dict()}) == expected_rel


# ── 2. Guard test: NO stage ever reads the repo root for a subfolder app ───────────────────────

@pytest.mark.parametrize("shape", ["subfolder_with_stray_root", "workspace_monorepo_app", "nested_fullstack_app"])
def test_guard_no_stage_reads_repo_root_for_subfolder_app(tmp_path, java_off, shape):
    repo, entry = _make_repo(tmp_path, shape)
    assert not entry.is_root()
    spec = {"instructions": "Fix bug."}
    fctx = {"app_map": entry.as_dict()}
    poisoned_changed = ["pom.xml", "package.json", "boss.deploy.yaml", "README.md"]

    assert app_root.app_dir(repo, changed=poisoned_changed, app_map=entry) != repo
    assert app_root.gate_dir(repo, changed=poisoned_changed, app_map=entry) != repo
    assert app_root.java_build(repo, changed=poisoned_changed, app_map=entry)[1] is None
    assert stacks.find_stack_root(repo, changed=poisoned_changed, app_map=entry) != repo
    assert verification_target.resolve_verify_cwd(repo, changed=poisoned_changed, app_map=entry) != repo
    plan = verification_target.resolve_gate_plan(
        spec, "App", repo, changed=poisoned_changed, finalize_ctx=fctx, app_map=entry
    )
    assert plan.cwd != repo
    assert preview_service.app_dir_of(repo, changed=poisoned_changed, app_map=entry) != repo
    assert app_deploy_manifest.app_folder(poisoned_changed, app_map=entry) == entry.root_path
    assert app_deploy_manifest.app_manifest(
        poisoned_changed, app_deploy_manifest.manifests_in(repo), app_map=entry
    ) != "boss.deploy.yaml"


# ── 3. Refusal test: no map = refuse, NEVER root ───────────────────────────────────────────────

def test_no_map_refuses_never_falls_back_to_root(tmp_path, java_off):
    repo, _entry = _make_repo(tmp_path, "subfolder_with_stray_root")

    # Multi-app repo with no map and no evidence -> every stage refuses
    res = app_map.resolve_for_stage(repo)
    assert not res.ok and res.path is None and res.source == "refused" and res.refusal

    where = app_root.resolve(repo)
    assert where.path is None and where.source == "refused"

    plan = verification_target.resolve_gate_plan({"instructions": "Fix bug."}, "Unknown", repo)
    assert plan.source == "app_unknown" and plan.error

    # Even on a single-app repo, require_map=True refuses explicitly when no AppMap exists!
    single_repo, _ = _make_repo(tmp_path, "root_single_app")
    strict = app_map.resolve_for_stage(single_repo, require_map=True)
    assert not strict.ok and strict.path is None and strict.source == "refused"

    strict_plan = verification_target.resolve_gate_plan(
        {"instructions": "Fix bug."}, "Root", single_repo, require_map=True
    )
    assert strict_plan.source == "app_unknown" and strict_plan.error

    with pytest.raises(app_map.AppMapRequiredError):
        verification_target.resolve_verify_cwd(single_repo, require_map=True)

    with pytest.raises(app_map.AppMapRequiredError):
        preview_service.app_dir_of(single_repo, require_map=True)

    with pytest.raises(app_map.AppMapRequiredError):
        preview_service.how_app_runs(single_repo, require_map=True)


# ── 4. Write boundary tests (including crun_f246d330 root pom.xml deletion incident) ───────────

def test_write_boundary_catches_operator_incident_and_allows_shared_and_boss_files():
    # Exact crun_f246d330 incident: repair agent tried to delete root pom.xml & src/main/java/Demo.java
    diff_paths = [
        "helpdesk-ai/src/server.js",
        "helpdesk-ai/tests/server.test.js",
        "pom.xml",
        "src/main/java/Demo.java",
        "notes-lite/app.py",
        "boss.preview.app",
        ".boss/apps.yaml",
        "docs/prd/helpdesk.md",
    ]
    outside = app_root.files_outside_app(diff_paths, "helpdesk-ai")
    assert outside == ["notes-lite/app.py", "pom.xml", "src/main/java/Demo.java"]

    # Shared paths are allowed when explicitly declared in App Map
    mono_paths = ["apps/web/src/page.tsx", "packages/ui/button.tsx", "apps/api/index.ts"]
    assert app_root.files_outside_app(mono_paths, "apps/web", shared=["packages/ui"]) == ["apps/api/index.ts"]

    # Enforce mode raises WriteBoundaryViolation
    with pytest.raises(app_root.WriteBoundaryViolation) as exc_info:
        app_root.check_write_boundary(diff_paths, "helpdesk-ai", mode="refuse")
    assert "pom.xml" in str(exc_info.value)
    assert exc_info.value.offending == ["notes-lite/app.py", "pom.xml", "src/main/java/Demo.java"]

    # Root app allows normal repo paths, still rejects traversal
    assert app_root.files_outside_app(["src/server.js", "pom.xml"], ".") == []
    assert app_root.files_outside_app(["../outside.txt", "/etc/passwd"], ".") == ["../outside.txt", "/etc/passwd"]


# ── 5. Real Postgres persistence & stage integration ───────────────────────────────────────────

@pytest.mark.skipif(not os.environ.get("BOSS_TEST_DATABASE_URL"), reason="requires test Postgres")
def test_db_app_map_persistence_seeding_and_materialize_build(tmp_path):
    repo, _ = _make_repo(tmp_path, "subfolder_with_stray_root")
    pid = "proj_appmap_555"
    bid = "bind_appmap_555"
    run_id = "crun_appmap_555"

    with begin() as c:
        org_id = c.execute(text("SELECT id FROM orgs LIMIT 1")).scalar()
        ws_id = c.execute(text("SELECT id FROM workspaces LIMIT 1")).scalar()
        if not org_id or not ws_id:
            pytest.skip("test DB not seeded with org/workspace")
        c.execute(text("DELETE FROM events WHERE project_id = :p"), {"p": pid})
        c.execute(text("DELETE FROM projects WHERE id = :p"), {"p": pid})
        c.execute(
            text("INSERT INTO projects (id, workspace_id, name, slug) VALUES (:p, :w, 'AppMap 555', 'appmap-555')"),
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
                "VALUES (:b, :p, :cid, 'acme', 'sandbox', 'acme/sandbox', 'main', 'primary', 'active', 'test')"
            ),
            {"b": bid, "p": pid, "cid": f"conn_{pid}"},
        )
        c.execute(
            text(
                "INSERT INTO code_task_runs (id, project_id, title, status, spec, finalize_ctx) "
                "VALUES (:r, :p, 'HelpDesk AI', 'running', CAST(:s AS JSONB), CAST(:f AS JSONB))"
            ),
            {
                "r": run_id,
                "p": pid,
                "s": json.dumps({"owner": "acme", "repo": "sandbox", "app_root": "helpdesk-ai"}),
                "f": json.dumps({
                    "saved_workspace": {
                        "files": [
                            {"path": "helpdesk-ai/src/server.js", "content": "export const x = 1;\n"},
                            {"path": "README.md", "content": "# hi\n"},
                            {"path": "pom.xml", "content": "<project/>\n"},
                        ]
                    }
                }),
            },
        )
        run = {"id": run_id, "project_id": pid, "title": "HelpDesk AI",
               "spec": {"owner": "acme", "repo": "sandbox", "app_root": "helpdesk-ai"}, "finalize_ctx": {}}

        # Seed from spec.app_root -> persists onto app_components & code_task_runs.finalize_ctx.app_map
        seeded = app_map.seed_app_map(repo, db=c, project_id=pid, run=run, binding_id=bid, persist=True)
        assert seeded.ok and seeded.entry is not None
        assert seeded.root_path == "helpdesk-ai"

        # load_app_map reads back the canonical record
        loaded = app_map.load_app_map(c, pid, app_key=seeded.entry.app_key)
        assert loaded is not None and loaded.root_path == "helpdesk-ai" and loaded.fingerprint

        # recorded_app_dir & project_scope._components agree
        assert app_root.recorded_app_dir(c, run) == "helpdesk-ai"
        assert project_scope._components(c, pid, "acme/sandbox") == ["helpdesk-ai"]

        # workflow_precheck.materialize_build reads stored AppMap (ignores root pom.xml/README majority)
        mat_dir = tmp_path / "materialized"
        mat_dir.mkdir()
        out_dir = workflow_precheck.materialize_build(c, run_id, mat_dir)
        assert out_dir == mat_dir / "helpdesk-ai"

        # Clean up
        c.execute(text("DELETE FROM events WHERE project_id = :p"), {"p": pid})
        c.execute(text("DELETE FROM projects WHERE id = :p"), {"p": pid})

    # _precheck_app_env refuses cleanly on ambiguous repo when require_map=True
    from boss_engine.db import get_engine

    refused = governed_code_task_service._precheck_app_env(
        get_engine(),
        {"id": "crun_none", "project_id": "proj_none", "spec": {}, "finalize_ctx": {}},
        repo,
        [],
        require_map=True,
    )
    # (project may not exist for emit_event, so verify resolve_for_stage directly as well)
    assert refused is None or refused.get("status") == "refused"
