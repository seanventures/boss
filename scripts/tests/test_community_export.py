from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "community_export.py"
spec = importlib.util.spec_from_file_location("community_export", MODULE_PATH)
assert spec and spec.loader
export = importlib.util.module_from_spec(spec)
spec.loader.exec_module(export)


def test_manifest_rejects_unsafe_and_overlapping_rules() -> None:
    manifest = {
        "schema_version": 1,
        "include": ["apps/"],
        "exclude": ["apps/private.py"],
        "requires_split": ["apps/private.py"],
        "required_runtime": ["apps/main.py"],
        "required_release": ["LICENSE"],
    }
    try:
        export.validate_manifest(manifest)
    except ValueError:
        pass
    else:
        raise AssertionError("overlapping export rules were accepted")
    manifest["requires_split"] = ["../escape.py"]
    try:
        export.validate_manifest(manifest)
    except ValueError:
        pass
    else:
        raise AssertionError("unsafe export path was accepted")


def test_local_import_graph_finds_withheld_modules() -> None:
    source = {
        "apps/api/app/main.py",
        "apps/api/app/secret.py",
        "apps/web/app/page.tsx",
        "apps/web/app/paid.tsx",
    }
    selected = {"apps/api/app/main.py", "apps/web/app/page.tsx"}
    py = export.python_edges(
        "apps/api/app/main.py", b"from .secret import paid\n", source, selected
    )
    ts = export.typescript_edges(
        "apps/web/app/page.tsx", b"import Paid from './paid';\n", source, selected
    )
    assert [(edge["kind"], edge["to"]) for edge in py + ts] == [
        ("python-import", "apps/api/app/secret.py"),
        ("typescript-import", "apps/web/app/paid.tsx"),
    ]


def test_internal_ui_audit_media_is_excluded() -> None:
    assert (
        export.matching_rule("apps/web/audits/command-center.png", ["apps/web/audits/"])
        == "apps/web/audits/"
    )


def test_fixed_commit_candidate_withholds_private_and_reports_edges(tmp_path: Path) -> None:
    repo = tmp_path / "source"
    repo.mkdir()
    files = {
        "apps/api/app/main.py": b"from boss_engine.services.paid import Paid\n",
        "engine/boss_engine/services/paid.py": b"class Paid: pass\n",
        "install.sh": b"BOSS_REPO_URL=https://github.com/seanheiney/boss.git\n",
        "README.md": b"CloudProduct is available only in the hosted edition.\n",
        "docker-compose.yaml": b"services:\n  account-connect:\n    image: private\n",
        "scripts/install/community-compose.yml": b"services:\n  api:\n    build:\n      dockerfile: Dockerfile\n",
        "scripts/install/launch.sh": b"#!/bin/sh\nexit 0\n",
    }
    for name, data in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "update-index", "--chmod=+x", "scripts/install/launch.sh"],
        check=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "fixture",
        ],
        check=True,
    )
    (repo / "untracked-secret.txt").write_text("never export me")
    manifest = {
        "schema_version": 1,
        "include": [
            "apps/api/",
            "engine/boss_engine/",
            "install.sh",
            "docker-compose.yaml",
            "README.md",
            "scripts/install/",
        ],
        "exclude": ["engine/boss_engine/services/paid.py"],
        "requires_split": ["docker-compose.yaml"],
        "required_runtime": ["apps/api/app/main.py", "install.sh", "docker-compose.yaml"],
        "required_release": ["README.md"],
    }
    export.validate_manifest(manifest)
    sha = export.git(repo, "rev-parse", "HEAD").decode().strip()
    report, candidate = export.analyze(repo, sha, manifest)
    kinds = {item["kind"] for item in report["blockers"]}
    assert "python-import" in kinds
    assert "private-source-or-host-reference" in kinds
    assert "mixed-paid-source-needs-split" in kinds
    assert "compose-private-service" in kinds
    assert "compose-dockerfile-missing" in kinds
    assert "missing-runtime-entrypoint" in kinds
    assert set(candidate) == {
        "apps/api/app/main.py",
        "scripts/install/community-compose.yml",
        "scripts/install/launch.sh",
    }
    candidate_dir = tmp_path / "candidate"
    export.write_candidate(candidate_dir, candidate, report["executable_paths"])
    assert (candidate_dir / "scripts/install/launch.sh").stat().st_mode & 0o111
    assert report["ready_for_publication"] is False
