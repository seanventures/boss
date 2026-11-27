"""BOSSAPP-557 (App Map 3) — Repo-shape corpus with golden App Maps in CI.

Runs deterministically in default CI:
  1. Verifies corpus manifest has >= 10 pinned repo shapes with license & reference metadata.
  2. Cheap scan regression tests across ALL 12 shapes asserting exact match against golden maps.
  3. Synthetic diff tests across ALL 12 shapes proving write-boundary enforcement (`files_outside_app`)
     and manifest-change drift detection (`detect_manifest_drift`).
  4. Real baseline execution on the small runnable subset (zero-network stdlib/node test apps).
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

from boss_engine.services import app_map, app_map_scanner, app_root

FIXTURE_ROOT = Path(__file__).resolve().parent / "fixtures" / "app_map"
CORPUS_FILE = FIXTURE_ROOT / "corpus.json"
CORPUS_DATA = json.loads(CORPUS_FILE.read_text(encoding="utf-8"))
CORPUS_REPOS: list[dict] = CORPUS_DATA["repos"]


def _snapshot(res: app_map_scanner.ScanResult) -> dict:
    return {
        "status": res.status,
        "source": res.source,
        "workspace_root": res.workspace_root,
        "selected_root_path": res.selected.root_path if res.selected else None,
        "shared_paths": list(res.shared_paths),
        "apps": [
            {
                "app_key": a.app_key,
                "root_path": a.root_path,
                "workspace_root": a.workspace_root,
                "stack": a.stack,
                "commands": a.commands,
                "run": a.run,
                "manifests": list(a.manifests),
                "shared_paths": list(a.shared_paths),
                "provenance_source": a.provenance.get("source"),
            }
            for a in res.apps
        ],
    }


def test_corpus_has_ten_plus_pinned_shapes_with_golden_maps():
    assert len(CORPUS_REPOS) >= 10, f"Expected >= 10 repo shapes, found {len(CORPUS_REPOS)}"
    shapes = {r["shape"] for r in CORPUS_REPOS}
    for required in (
        "root_app",
        "multi_app_stray_root_manifests",
        "workspaces_turbo",
        "nx_monorepo",
        "maven_multi_module",
        "gradle_multi_project",
        "python_plus_web",
        "polyglot_microservices",
        "go_cmd_multi_binary",
        "decoys_and_stray_manifests",
    ):
        assert required in shapes, f"Missing required repo shape: {required}"
    for item in CORPUS_REPOS:
        assert item.get("inspired_by") and item.get("pinned_ref") and item.get("license")
        assert (FIXTURE_ROOT / "repos" / item["id"]).is_dir()
        assert (FIXTURE_ROOT / "golden" / f"{item['id']}.json").is_file()


@pytest.mark.parametrize("item", CORPUS_REPOS, ids=[r["id"] for r in CORPUS_REPOS])
def test_corpus_scan_matches_golden_app_map(item):
    repo_dir = FIXTURE_ROOT / "repos" / item["id"]
    golden = json.loads((FIXTURE_ROOT / "golden" / f"{item['id']}.json").read_text(encoding="utf-8"))

    res1 = app_map_scanner.scan_repository(repo_dir)
    res2 = app_map_scanner.scan_repository(repo_dir)
    assert _snapshot(res1) == golden
    # Deterministic fingerprint across repeated scans
    assert [a.fingerprint for a in res1.apps] == [a.fingerprint for a in res2.apps]
    assert all(a.fingerprint and len(a.fingerprint) == 64 for a in res1.apps)


@pytest.mark.parametrize("item", CORPUS_REPOS, ids=[r["id"] for r in CORPUS_REPOS])
def test_corpus_synthetic_diffs_write_boundary_and_manifest_drift(tmp_path, item):
    src_dir = FIXTURE_ROOT / "repos" / item["id"]
    work = tmp_path / item["id"]
    shutil.copytree(src_dir, work)

    res = app_map_scanner.scan_repository(work)
    target_root = item["target_root_path"]
    target = next((a for a in res.apps if a.root_path == target_root), None)
    assert target is not None, f"Target {target_root!r} not in scanned apps {[a.root_path for a in res.apps]}"

    diffs = item["synthetic_diffs"]
    # 1. In-app + declared shared paths pass write boundary
    assert app_root.files_outside_app(diffs["in_app"], target.root_path, shared=target.shared_paths) == []

    # 2. Outside-app paths are flagged for subfolder apps
    if diffs["outside_app"]:
        flagged = app_root.files_outside_app(diffs["outside_app"], target.root_path, shared=target.shared_paths)
        assert flagged == sorted(diffs["outside_app"])

    # 3. Editing the declared manifest triggers deterministic fingerprint drift
    manifest_rel = diffs["manifest_change"]
    manifest_path = work / manifest_rel
    assert manifest_path.is_file(), f"Missing synthetic manifest {manifest_rel}"
    before = app_map.detect_manifest_drift(target, work, rescan=False)
    assert before["drifted"] is False
    manifest_path.write_text(manifest_path.read_text(encoding="utf-8") + "\n# synthetic change\n", encoding="utf-8")
    after = app_map.detect_manifest_drift(target, work, rescan=False)
    assert after["drifted"] is True
    assert after["new_fingerprint"] != target.fingerprint


@pytest.mark.parametrize(
    "item",
    [r for r in CORPUS_REPOS if r.get("runnable_baseline")],
    ids=[r["id"] for r in CORPUS_REPOS if r.get("runnable_baseline")],
)
def test_corpus_real_baseline_subset_runs_honestly(tmp_path, item):
    src_dir = FIXTURE_ROOT / "repos" / item["id"]
    work = tmp_path / item["id"]
    shutil.copytree(src_dir, work)

    res = app_map_scanner.scan_repository(work)
    assert res.selected is not None
    entry = res.selected
    # Keep zero-network execution: strip npm/pip install on offline stdlib/node-test fixtures and use current python
    cmds = dict(entry.commands)
    cmds.pop("install", None)
    if cmds.get("test", "").startswith("python3 "):
        cmds["test"] = cmds["test"].replace("python3 ", f"{sys.executable} ", 1)
    if cmds.get("test") == "npm test":
        cmds["test"] = "node --test"
    runnable = app_map.AppMapEntry(
        app_key=entry.app_key,
        app_name=entry.app_name,
        root_path=entry.root_path,
        workspace_root=entry.workspace_root,
        commands=cmds,
        status="confirmed",
    )
    baseline = app_map.verify_baseline(work, runnable, commit_sha="corpus-ci")
    assert baseline["status"] == "passed", baseline
    assert baseline["steps"] and baseline["steps"][0]["ok"] is True
