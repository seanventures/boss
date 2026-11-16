"""An upgrade leaves the test Postgres running (BOSSAPP-1335).

Every laptop upgrade stopped and restarted `boss-community-db-test-1` (the test Postgres on
:55700), killing agents' bench-DB test runs mid-suite — five times on 2026-11-16. The
installer's fresh backup paused EVERY running service "for a consistent backup", db-test
included, and a rollback stopped and force-recreated it with the BOSS services.

db-test (compose profile `dev`, label kind=test) holds only test databases. Now the backup and
the rollback leave it — and any kind=test service — running and say so, and the upgrade applies
a change to db-test only when its own definition or image changed.
"""
from __future__ import annotations

from pathlib import Path

from .test_community_installer_upgrade_backup import (  # noqa: F401 - fixtures used by name
    REPO,
    _run,
    _stopped_database_backend,
    install_pair,
)
from .test_instant_rollback import FAKE_DOCKER, _log, _rollback, install  # noqa: F401

INSTALL = REPO / "scripts" / "install"


def _with_db_test(docker: Path, running_line: str) -> None:
    """The fake Docker reports db-test running beside the other services."""
    text = docker.read_text()
    assert running_line in text
    docker.write_text(text.replace(running_line, running_line + "\n    echo db-test", 1))


def test_the_backup_pauses_the_writers_and_leaves_the_test_postgres_running(install_pair):  # noqa: F811
    installed, _old, _, env, tmp = install_pair
    _stopped_database_backend(tmp, env)
    (tmp / "db-state").write_text("running\n")
    worker = tmp / "worker-state"
    worker.write_text("running\n")
    env["BOSS_TEST_WORKER_STATE"] = str(worker)
    _with_db_test(tmp / "bin/docker",
                  "    [[ -z ${BOSS_TEST_WORKER_STATE:-} || $(cat \"$BOSS_TEST_WORKER_STATE\") "
                  "!= running ]] || echo worker ;;")
    # `;;` ends the case arm: put db-test's echo inside it, before the `;;`.
    text = (tmp / "bin/docker").read_text().replace(
        "|| echo worker ;;\n    echo db-test", "|| echo worker\n    echo db-test ;;", 1)
    (tmp / "bin/docker").write_text(text)

    result = _run("bash", INSTALL / "backup.sh", "--dir", installed,
                  "--backup-dir", tmp / "backups", cwd=REPO, env=env)

    assert result.returncode == 0, result.stderr
    # The worker is paused and started again; db-test is never stopped (an unexpected
    # `stop worker db-test` would fail the fake Docker).
    assert (tmp / "docker-trace").read_text().splitlines() == ["stop-worker", "start-worker"]
    assert "Left the test Postgres (db-test) running" in result.stderr


def test_a_rollback_never_stops_or_recreates_the_test_postgres(install):  # noqa: F811
    install["commit_current"](False)
    fake = install["tmp"] / "bin" / "docker"
    fake.write_text(FAKE_DOCKER.replace(r"printf 'api\nworker\ndb\n'",
                                        r"printf 'api\nworker\ndb\ndb-test\n'"))
    dry = _rollback(install, "--dry-run")
    assert dry.returncode == 0, dry.stderr
    assert "would recreate without building: api worker\n" in dry.stdout
    assert "Left the test Postgres (db-test) running" in dry.stderr

    done = _rollback(install)
    assert done.returncode == 0, done.stderr
    calls = _log(install).splitlines()
    up = [c for c in calls if " up " in f" {c} "]
    assert len(up) == 1 and up[0].endswith("api worker"), up
    assert not any("db-test" in c for c in calls if " stop " in f" {c} " or " up " in f" {c} ")


def test_an_upgrade_dry_run_says_the_test_postgres_stays_up(install_pair):  # noqa: F811
    installed, _old, _new, env, tmp = install_pair
    docker = tmp / "bin" / "docker"
    original = docker.read_text()
    docker.write_text(original.replace(
        "#!/usr/bin/env bash\n",
        "#!/usr/bin/env bash\n"
        "if [[ \"$*\" == *'--profile dev ps -q --status running db-test'* ]]; then "
        "echo fixture-db-test; exit 0; fi\n", 1))

    result = _run("bash", INSTALL / "upgrade.sh", "--dir", installed, "--dry-run",
                  cwd=REPO, env=env)

    assert result.returncode == 0, result.stderr
    assert "Would leave the test Postgres (db-test) running" in result.stdout
    assert not (tmp / "docker-trace").exists() or "db-test" not in (
        tmp / "docker-trace").read_text(), "a dry run touches nothing"


def test_the_upgrade_applies_only_a_change_to_the_test_postgres():
    text = (INSTALL / "upgrade.sh").read_text()
    # After the installer (which never touches db-test): `up -d --no-deps` on db-test alone
    # recreates it only when its definition or image changed, and says which.
    assert text.index('bash "$FETCHED_INSTALLER"') < text.index('TEST_DB_BEFORE="$(test_db_running)"')
    assert 'up -d --no-build --no-deps db-test' in text
    assert text.index('test_db_after "$TEST_DB_BEFORE"') < text.index("Upgrade complete:")
    # The installer never names the `dev` profile, so its own `up` cannot touch db-test.
    assert "--profile dev" not in (REPO / "install.sh").read_text()
