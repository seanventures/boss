"""INSTANT ROLLBACK (BOSSAPP-839): `boss rollback` puts the previous release back from the images
upgrade.sh kept as prev-<tag>, without a build, and refuses a newer schema unless the
pre-upgrade backup is restored first. Driven against a throwaway git checkout and a fake
`docker` on PATH that logs every call, so nothing here touches a real install.
"""
from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
INSTALL = ROOT / "scripts" / "install"
ROLLBACK = INSTALL / "rollback.sh"
TAG = "boss-community"
REPOS = ["boss-community-api", "boss-community-worker", "boss-community-web"]

FAKE_DOCKER = r"""#!/usr/bin/env bash
printf '%s\n' "$*" >> "$FAKE_DOCKER_LOG"
case "$1" in
  info) exit 0 ;;
  image)
    [[ "$2" == inspect ]] || exit 0
    for ref in $FAKE_IMAGES; do [[ "$ref" == "${@: -1}" ]] && exit 0; done
    exit 1 ;;
  images)
    if [[ "$*" == *"--format {{.Repository}}"* && "$*" != *CreatedAt* ]]; then
      printf 'boss-community-api\n'
    elif [[ "$*" == *CreatedAt* ]]; then
      printf '2026-11-12 03|boss-community-api:boss-community\n'
      printf '2026-11-11 02|boss-community-api:prev-boss-community\n'
      printf '2026-11-10 01|boss-community-api:old-one\n'
      printf '2026-11-09 01|boss-community-api:older-one\n'
    fi
    exit 0 ;;
  ps) exit 0 ;;
  compose)
    if [[ "$*" == *" ps "* ]]; then printf 'api\nworker\ndb\n'; fi
    exit 0 ;;
  *) exit 0 ;;
esac
"""


def _sh(*args, cwd=None, env=None, check=True):
    return subprocess.run(list(args), cwd=cwd, env=env, capture_output=True, text=True,
                          check=check)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


@pytest.fixture()
def install(tmp_path):
    """A checkout with two commits: prev (schema v1) and current (schema v1 or v2)."""
    d = tmp_path / "boss"
    (d / "scripts" / "install").mkdir(parents=True)
    (d / "db").mkdir()
    (d / "scripts" / "install" / "community-compose.yml").write_text("services: {}\n")
    (d / "db" / "schema.sql").write_text("CREATE TABLE a (id int);\n")
    (d / ".gitignore").write_text(".env\n.boss-*\n")
    (d / ".env").write_text("BOSS_DB_PASSWORD=x\n")
    git = ["git", "-C", str(d), "-c", "user.email=t@example.invalid", "-c", "user.name=t"]
    _sh("git", "init", "-q", "-b", "main", str(d))
    _sh(*git, "add", "-A")
    _sh(*git, "commit", "-q", "-m", "prev")
    prev = _sh("git", "-C", str(d), "rev-parse", "HEAD").stdout.strip()
    (d / "release-notes").write_text("new\n")
    (d / ".boss-release").write_text("BOSS_RELEASE_REF=v2\nBOSS_INSTALLED_COMMIT=x\n")

    def commit_current(schema_changed: bool) -> str:
        if schema_changed:
            (d / "db" / "schema.sql").write_text("CREATE TABLE a (id int);\nCREATE TABLE b (id int);\n")
        _sh(*git, "add", "-A")
        _sh(*git, "commit", "-q", "-m", "current")
        (d / ".boss-rollback").write_text("\n".join([
            "# BOSS rollback point",
            f"BOSS_PREV_COMMIT={prev}", "BOSS_PREV_REF=v1", f"BOSS_IMAGE_TAG={TAG}",
            f"BOSS_PREV_IMAGE_TAG=prev-{TAG}", f"BOSS_PREV_IMAGES={' '.join(REPOS)}",
            f"BOSS_PREV_SCHEMA_SHA256={_sha('CREATE TABLE a (id int);' + chr(10))}",
        ]) + "\n")
        return _sh("git", "-C", str(d), "rev-parse", "HEAD").stdout.strip()

    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "docker").write_text(FAKE_DOCKER)
    (bindir / "docker").chmod(0o755)
    log = tmp_path / "docker.log"
    env = {**os.environ, "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
           "FAKE_DOCKER_LOG": str(log),
           "FAKE_IMAGES": " ".join(f"{r}:{t}" for r in REPOS for t in (TAG, f"prev-{TAG}")),
           "HOME": str(tmp_path)}
    return {"dir": d, "prev": prev, "commit_current": commit_current, "env": env, "log": log,
            "tmp": tmp_path}


def _rollback(inst, *args):
    return _sh("bash", str(ROLLBACK), "--dir", str(inst["dir"]), *args, env=inst["env"],
               check=False)


def _log(inst) -> str:
    return inst["log"].read_text() if inst["log"].exists() else ""


def _head(inst) -> str:
    return _sh("git", "-C", str(inst["dir"]), "rev-parse", "HEAD").stdout.strip()


def test_the_scripts_parse():
    for script in ("rollback.sh", "upgrade.sh", "prune.sh", "boss.sh"):
        assert _sh("bash", "-n", str(INSTALL / script), check=False).returncode == 0, script


def test_no_rollback_point_changes_nothing(install):
    install["commit_current"](False)
    (install["dir"] / ".boss-rollback").unlink()
    done = _rollback(install)
    assert done.returncode == 1 and "No rollback point" in done.stderr


def test_a_newer_schema_is_refused_without_a_backup(install):
    current = install["commit_current"](True)
    done = _rollback(install)
    assert done.returncode == 1
    assert "schema is newer" in done.stderr and "--restore-backup" in done.stderr
    assert _head(install) == current
    calls = _log(install).splitlines()
    assert not any(c.startswith("tag ") or " up " in c or "psql" in c for c in calls)


def test_dry_run_says_what_it_would_do_and_changes_nothing(install):
    current = install["commit_current"](False)
    done = _rollback(install, "--dry-run")
    assert done.returncode == 0, done.stderr
    assert f"would point boss-community-api:{TAG} at boss-community-api:prev-{TAG}" in done.stdout
    assert "would recreate without building: api worker" in done.stdout
    assert _head(install) == current
    assert not any(line.startswith("tag ") for line in _log(install).splitlines())
    assert (install["dir"] / ".boss-rollback").exists()


def test_rollback_repoints_the_images_and_restarts_without_a_build(install):
    install["commit_current"](False)
    done = _rollback(install)
    assert done.returncode == 0, done.stderr
    calls = _log(install).splitlines()
    for repo in REPOS:
        assert f"tag {repo}:{TAG} {repo}:rolled-back-{TAG}" in calls
        assert f"tag {repo}:prev-{TAG} {repo}:{TAG}" in calls
    up = [c for c in calls if " up " in f" {c} "]
    assert len(up) == 1 and "--no-build" in up[0] and "--pull never" in up[0]
    assert up[0].endswith("api worker")
    assert not any(" build" in f" {c}" for c in calls)
    assert _head(install) == install["prev"]
    assert not (install["dir"] / ".boss-rollback").exists()
    assert (install["dir"] / ".boss-rollback.used").exists()
    record = (install["dir"] / ".boss-release").read_text()
    assert "BOSS_RELEASE_REF=v1" in record and f"BOSS_INSTALLED_COMMIT={install['prev']}" in record


def test_a_missing_kept_image_refuses(install):
    current = install["commit_current"](False)
    install["env"]["FAKE_IMAGES"] = f"boss-community-api:prev-{TAG}"
    done = _rollback(install)
    assert done.returncode == 1 and "is gone" in done.stderr
    assert _head(install) == current


def _backup(inst, commit: str) -> Path:
    b = inst["tmp"] / "boss-20261112T000000Z-1"
    b.mkdir()
    (b / "boss.dump").write_bytes(b"PGDMP")
    (b / "source-commit").write_text(commit + "\n")
    return b


def test_a_backup_from_another_commit_is_refused(install):
    current = install["commit_current"](True)
    done = _rollback(install, "--restore-backup", str(_backup(install, current)))
    assert done.returncode == 1 and "not on the previous release" in done.stderr
    assert _head(install) == current


def test_a_newer_schema_rolls_back_after_restoring_into_a_new_database(install):
    install["commit_current"](True)
    backup = _backup(install, install["prev"])
    dry = _rollback(install, "--restore-backup", str(backup), "--dry-run")
    assert dry.returncode == 0 and "would restore" in dry.stdout
    done = _rollback(install, "--restore-backup", str(backup))
    assert done.returncode == 0, done.stderr
    calls = _log(install)
    restore = calls.index("pg_restore -U boss -d boss_rollback_restore")
    swap = calls.index("ALTER DATABASE boss_rollback_restore RENAME TO boss")
    retag = calls.index(f"tag boss-community-api:prev-{TAG}")
    assert restore < swap < retag
    assert "DROP DATABASE IF EXISTS boss WITH" not in calls     # the live DB is renamed, not dropped
    assert _head(install) == install["prev"]


def test_prune_never_removes_the_rollback_point(install):
    done = _sh("bash", str(INSTALL / "prune.sh"), "--dry-run", env=install["env"], check=False)
    assert done.returncode == 0, done.stderr
    assert "boss-community-api:older-one" in done.stdout
    assert "prev-boss-community" not in done.stdout


def test_upgrade_keeps_the_running_images_before_the_installer_runs():
    text = (INSTALL / "upgrade.sh").read_text()
    keep = text.index('keep_previous_images "$(image_tag)"\n')
    assert keep < text.index('bash "$FETCHED_INSTALLER"')
    assert text.index('if [[ "$DRY_RUN" -eq 1 ]]') < keep
    assert '"boss-community-$svc:prev-$tag"' in text and "BOSS_PREV_SCHEMA_SHA256" in text
