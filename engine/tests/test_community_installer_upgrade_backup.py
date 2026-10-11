"""A pasted installer rerun must leave an older Community install recoverable."""

from __future__ import annotations

import os
import json
import re
import tarfile
import shlex
import subprocess
import threading
import time
import uuid
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[2]


@contextmanager
def _health_listener(replies):
    """Real TCP/HTTP fixture; None accepts the request but never sends a response."""
    requests = []
    finished = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append((self.path, time.monotonic()))
            reply = replies[min(len(requests) - 1, len(replies) - 1)]
            if reply is None:
                finished.wait(20)
                return
            status, delay = reply
            if finished.wait(delay):
                return
            body = b'{"status":"ok"}'
            self.send_response(status)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02},
                              daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", requests
    finally:
        finished.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _readiness(tmp_path, url, *, budget=None, clock=""):
    # Exercise the shipped readiness block, including its failure/log/exit path, without
    # installing anything. A shorter function argument keeps real timeout tests bounded.
    block = (REPO / "install.sh").read_text().split(
        "# ---- wait health", 1)[1].split("# ---- seed", 1)[0]
    block = block[block.index('ui_step "Waiting for API health"'):]
    if budget is not None:
        block = block.replace("if ! wait_for_api_health; then",
                              f"if ! wait_for_api_health {int(budget)}; then")
    script = tmp_path / "readiness.sh"
    script.write_text(
        "set -euo pipefail\n"
        "ui_step() { printf '%s\\n' \"$*\"; }; ui_info() { ui_step \"$@\"; }\n"
        "ui_err() { ui_step \"$@\"; }; ui_ok() { ui_step \"$@\"; }\n"
        "compose() { printf 'DIAGNOSTIC %s\\n' \"$*\"; }\n"
        "COMPOSE=(compose); COMPOSE_PROFILES=''; ENV_FILE=unused\n"
        f"API_URL={shlex.quote(url)}\n{clock}\n{block}"
    )
    started = time.monotonic()
    result = subprocess.run(["bash", str(script)], capture_output=True, text=True,
                            check=False, timeout=15)
    return result, time.monotonic() - started


def test_installer_health_silent_connection_obeys_the_remaining_phase_time(tmp_path):
    with _health_listener([None]) as (url, requests):
        result, elapsed = _readiness(tmp_path, url, budget=2)
    assert result.returncode == 1, result.stdout + result.stderr
    assert 1 <= elapsed < 4, elapsed
    assert [path for path, _ in requests] == ["/health"]
    assert "limit 2s" in result.stdout
    assert "requests time out after at most 5s" in result.stdout
    assert url + "/health" in result.stdout and "another service" in result.stdout
    assert "DIAGNOSTIC" in result.stdout and "logs --tail 40 api db" in result.stdout
    assert "API healthy at" not in result.stdout


def test_installer_health_request_timeout_allows_a_later_healthy_response(tmp_path):
    with _health_listener([None, (200, 0)]) as (url, requests):
        result, elapsed = _readiness(tmp_path, url)
    assert result.returncode == 0, result.stdout + result.stderr
    assert len(requests) == 2
    assert 7 <= requests[1][1] - requests[0][1] < 11  # max-time 5 + retry sleep 3
    assert elapsed < 12
    assert "API healthy at" in result.stdout and "DIAGNOSTIC" not in result.stdout


@pytest.mark.parametrize("replies", [[(200, 0.25)], [(503, 0), (200, 0.25)]])
def test_installer_health_accepts_slow_success_and_recovers_from_http_failure(tmp_path, replies):
    with _health_listener(replies) as (url, requests):
        result, elapsed = _readiness(tmp_path, url)
    assert result.returncode == 0, result.stdout + result.stderr
    assert len(requests) == len(replies)
    assert elapsed < 6
    assert "API healthy at" in result.stdout and "DIAGNOSTIC" not in result.stdout


@pytest.mark.parametrize("request_seconds", [0, 2])
def test_installer_health_default_phase_includes_requests_in_180_seconds(tmp_path, request_seconds):
    # Advance Bash's own elapsed-time clock instead of taking three real minutes. Curl
    # failures and retry sleeps both consume the same phase budget.
    trace = tmp_path / "attempts"
    clock = (
        "SECONDS=0\n"
        f"curl() {{ printf 'request\\n' >> {shlex.quote(str(trace))}; "
        f"SECONDS=$((SECONDS + {request_seconds})); return 22; }}\n"
        "sleep() { SECONDS=$((SECONDS + $1)); }"
    )
    result, _ = _readiness(tmp_path, "http://127.0.0.1:1", clock=clock)
    assert result.returncode == 1
    attempts = trace.read_text().splitlines()
    assert len(attempts) == (60 if request_seconds == 0 else 36)
    assert "180s; limit 180s" in result.stdout
    assert "DIAGNOSTIC" in result.stdout


def test_installer_health_never_exceeds_sixty_attempts_even_without_clock_progress(tmp_path):
    trace = tmp_path / "attempts"
    clock = (f"curl() {{ printf 'request\\n' >> {shlex.quote(str(trace))}; return 22; }}\n"
             "sleep() { :; }")
    result, _ = _readiness(tmp_path, "http://127.0.0.1:1", clock=clock)
    assert result.returncode == 1
    assert len(trace.read_text().splitlines()) == 60
    assert "after 60 attempts" in result.stdout and "limit 180s" in result.stdout


def _run(*args: str | Path, cwd: Path | None = None, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(arg) for arg in args], cwd=cwd, env=env, text=True,
        capture_output=True, check=False, timeout=60,
    )


def _git(*args: str | Path, cwd: Path) -> str:
    result = _run("git", *args, cwd=cwd)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


@pytest.fixture
def install_pair(tmp_path: Path) -> tuple[Path, str, str, dict[str, str], Path]:
    """Two local commits plus fake Docker; no daemon, network, or provider call."""
    origin = tmp_path / "origin.git"
    source = tmp_path / "source"
    installed = tmp_path / "installed"
    origin.mkdir()
    source.mkdir()
    _git("init", "--bare", cwd=origin)
    _git("init", "-b", "main", cwd=source)
    _git("config", "user.name", "BOSS installer test", cwd=source)
    _git("config", "user.email", "installer-test@example.invalid", cwd=source)
    _git("remote", "add", "origin", origin, cwd=source)
    (source / "scripts/install").mkdir(parents=True)
    (source / "scripts/install/community-compose.yml").write_text("services: {}\n")
    (source / "install.sh").write_bytes((REPO / "install.sh").read_bytes())
    (source / "install.sh").chmod(0o755)
    for helper in ("restore_preserved_env.sh", "reconcile_api_credentials.py", "backup_archive.py"):
        (source / "scripts/install" / helper).write_bytes(
            (REPO / "scripts/install" / helper).read_bytes()
        )
    (source / "version").write_text("old\n")
    backup = source / "scripts/install/backup.sh"
    backup.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "[[ $1 == --dir ]]\n"
        "dir=$2\n"
        "git -C \"$dir\" rev-parse HEAD >> \"$BOSS_TEST_BACKUP_TRACE\"\n"
        "[[ ${BOSS_TEST_BACKUP_FAIL:-0} != 1 ]] || exit 19\n"
        "mkdir -p \"$BOSS_TEST_BACKUP_DIR\"\n"
        "cp \"$dir/.env\" \"$BOSS_TEST_BACKUP_DIR/.env\"\n"
        "git -C \"$dir\" rev-parse HEAD > \"$BOSS_TEST_BACKUP_DIR/source-commit\"\n"
        "printf 'database fixture\\n' > \"$BOSS_TEST_BACKUP_DIR/boss.dump\"\n"
        "printf 'artifact fixture\\n' > \"$BOSS_TEST_BACKUP_DIR/artifacts.tar\"\n"
        "printf '%s\\n' \"$BOSS_TEST_BACKUP_DIR\"\n"
    )
    _git("add", ".", cwd=source)
    _git("commit", "-m", "old install", cwd=source)
    old = _git("rev-parse", "HEAD", cwd=source)
    _git("push", "origin", "main", cwd=source)
    clone = _run("git", "clone", "--branch", "main", origin, installed)
    assert clone.returncode == 0, clone.stderr
    private_env = b"BOSS_API_KEY=private-test-fixture\nBOSS_DB_PASSWORD=existing\n"
    (installed / ".env").write_bytes(private_env)
    (source / "version").write_text("new\n")
    _git("add", ".", cwd=source)
    _git("commit", "-m", "new install", cwd=source)
    new = _git("rev-parse", "HEAD", cwd=source)
    _git("push", "origin", "main", cwd=source)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text(
        "#!/usr/bin/env bash\n"
        "if [[ $1 == info || $1 == context ]]; then exit 0; fi\n"
        "if [[ $1 == image && $2 == inspect ]]; then exit 0; fi\n"
        "if [[ $1 == compose && $2 == version ]]; then echo 'Docker Compose test'; exit 0; fi\n"
        "if [[ $* == *'config --format json' ]]; then echo '{\"services\":{\"api\":{\"build\":{}}}}'; exit 0; fi\n"
        "printf '%s\\n' \"$*\" >> \"$BOSS_TEST_DOCKER_TRACE\"\n"
        "exit 86\n"
    )
    docker.chmod(0o755)
    home = tmp_path / "home"
    home.mkdir()
    env = {
        **os.environ,
        "HOME": str(home),
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "BOSS_TEST_BACKUP_TRACE": str(tmp_path / "backup-trace"),
        "BOSS_TEST_BACKUP_DIR": str(tmp_path / "recovery"),
        "BOSS_TEST_DOCKER_TRACE": str(tmp_path / "docker-trace"),
        "BOSS_REPO_REF": "main",
        "BOSS_REPO_URL": str(origin),
    }
    return installed, old, new, env, tmp_path


def _install(installed: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return _run(
        "bash", REPO / "install.sh", "--dir", installed, "--yes",
        "--skip-model-key", "--no-install-deps", cwd=REPO, env=env,
    )


def _stopped_database_backend(tmp: Path, env: dict[str, str]) -> None:
    """Simulate the old Compose DB being stopped, including a failing dump."""
    state = tmp / "db-state"
    state.write_text("stopped\n")
    env["BOSS_TEST_DB_STATE"] = str(state)
    env["BOSS_BACKUP_DIR"] = str(tmp / "backups")
    empty_tar = tmp / "empty.tar"
    with tarfile.open(empty_tar, "w"):
        pass
    env["BOSS_TEST_EMPTY_TAR"] = str(empty_tar)
    layout = {
        "services": {
            "api": {"build": {}, "image": "fixture-api:local", "volumes": [
                {"type": "volume", "target": "/var/lib/boss/identity", "source": "identity"},
                {"type": "volume", "target": "/var/lib/boss/artifacts", "source": "artifacts"}]},
            "db": {"volumes": [{"type": "volume", "target": "/var/lib/postgresql/data", "source": "pg"}]}},
        "volumes": {name: {"name": "fixture_" + name} for name in ("identity", "artifacts", "pg")},
    }
    env["BOSS_TEST_LAYOUT"] = json.dumps(layout)
    (tmp / "bin/docker").write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "if [[ $1 == info ]]; then exit 0; fi\n"
        "if [[ $1 == image && $2 == inspect ]]; then [[ ${BOSS_TEST_API_IMAGE_MISSING:-0} != 1 ]] || exit 1; echo sha256:" + "a" * 64 + "; exit 0; fi\n"
        "if [[ $1 == inspect ]]; then echo sha256:" + "b" * 64 + "; exit 0; fi\n"
        "if [[ $1 == volume && $2 == inspect ]]; then exit 0; fi\n"
        "if [[ $1 == compose && $2 == version ]]; then echo 'Docker Compose test'; exit 0; fi\n"
        "args=$*\n"
        "case \"$args\" in\n"
        "  *' config --format json') printf '%s\\n' \"$BOSS_TEST_LAYOUT\" ;;\n"
        "  *' ps --status running --services')\n"
        "    [[ $(cat \"$BOSS_TEST_DB_STATE\") != running ]] || echo db\n"
        "    [[ -z ${BOSS_TEST_WORKER_STATE:-} || $(cat \"$BOSS_TEST_WORKER_STATE\") != running ]] || echo worker ;;\n"
        "  *' ps --all -q api') exit 0 ;;\n"
        "  *' ps --all -q db') echo fixture-db ;;\n"
        "  *' ps --all --services')\n"
        "    [[ ${BOSS_TEST_CONTAINER_MISSING:-0} == 1 ]] || echo db ;;\n"
        "  *' start db')\n"
        "    echo running > \"$BOSS_TEST_DB_STATE\"; echo start >> \"$BOSS_TEST_DOCKER_TRACE\" ;;\n"
        "  *' exec -T db pg_isready '*)\n"
        "    [[ $(cat \"$BOSS_TEST_DB_STATE\") == running ]] ;;\n"
        "  *' exec -T db pg_dump '*)\n"
        "    [[ ${BOSS_TEST_DUMP_FAIL:-0} != 1 ]] || exit 19\n"
        "    printf 'database fixture\\n' ;;\n"
        "  *' exec -T db psql '*to_regclass*) echo f ;;\n"
        "  *' exec -T db psql '*) exit 0 ;;\n"
        "  *' exec -T db pg_restore '*) cat >/dev/null ;;\n"
        "  *' run --rm --no-deps --pull never -T --entrypoint tar api '*)\n"
        "    if [[ ${BOSS_TEST_INTERRUPT_ARCHIVE:-0} == 1 ]]; then kill -TERM \"$PPID\"; exit 143; fi\n"
        "    cat \"$BOSS_TEST_EMPTY_TAR\" ;;\n"
        "  *' stop db')\n"
        "    echo stopped > \"$BOSS_TEST_DB_STATE\"; echo stop >> \"$BOSS_TEST_DOCKER_TRACE\" ;;\n"
        "  *' stop') echo stopped > \"$BOSS_TEST_DB_STATE\"; echo stop-all >> \"$BOSS_TEST_DOCKER_TRACE\" ;;\n"
        "  *' stop worker')\n"
        "    echo stopped > \"$BOSS_TEST_WORKER_STATE\"; echo stop-worker >> \"$BOSS_TEST_DOCKER_TRACE\" ;;\n"
        "  *' start worker')\n"
        "    echo running > \"$BOSS_TEST_WORKER_STATE\"; echo start-worker >> \"$BOSS_TEST_DOCKER_TRACE\" ;;\n"
        "  *' build api') echo build >> \"$BOSS_TEST_DOCKER_TRACE\"; exit 86 ;;\n"
        "  *) echo \"unexpected Docker command: $args\" >&2; exit 90 ;;\n"
        "esac\n"
    )


def _publish_real_backup_helper(tmp: Path) -> str:
    source = tmp / "source"
    (source / "scripts/install/backup.sh").write_bytes(
        (REPO / "scripts/install/backup.sh").read_bytes()
    )
    (source / "scripts/install/backup_archive.py").write_bytes((REPO / "scripts/install/backup_archive.py").read_bytes())
    _git("add", "scripts/install/backup.sh", "scripts/install/backup_archive.py", cwd=source)
    _git("commit", "-m", "backup stopped database", cwd=source)
    new = _git("rev-parse", "HEAD", cwd=source)
    _git("push", "origin", "main", cwd=source)
    return new


def test_pasted_installer_backs_up_before_switching_source(install_pair):
    installed, old, new, env, tmp = install_pair
    prior_env = (installed / ".env").read_bytes()

    result = _install(installed, env)

    # Fake Docker deliberately stops the installer at its first image build.
    assert result.returncode == 86, result.stderr + result.stdout
    assert (tmp / "backup-trace").read_text().strip() == old
    assert (tmp / "recovery/source-commit").read_text().strip() == old
    assert _git("rev-parse", "HEAD", cwd=installed) == new
    assert b"BOSS_API_KEY=private-test-fixture\n" in (installed / ".env").read_bytes()
    assert b"BOSS_DB_PASSWORD=existing\n" in (installed / ".env").read_bytes()
    assert (tmp / "recovery/.env").read_bytes() == prior_env
    assert "private-test-fixture" not in result.stdout + result.stderr


def test_backup_failure_leaves_checkout_and_credentials_untouched(install_pair):
    installed, old, _, env, tmp = install_pair
    env["BOSS_TEST_BACKUP_FAIL"] = "1"
    prior_env = (installed / ".env").read_bytes()

    result = _install(installed, env)

    assert result.returncode != 0
    assert "Recovery backup failed" in result.stderr
    assert _git("rev-parse", "HEAD", cwd=installed) == old
    assert (installed / ".env").read_bytes() == prior_env
    assert not (tmp / "docker-trace").exists()


def test_upgrade_reuses_its_valid_backup_without_a_second_snapshot(install_pair):
    installed, old, new, env, tmp = install_pair
    prior_env = (installed / ".env").read_bytes()

    # --no-rollback: this test reads the state the failed installer left (the rollback that
    # otherwise puts the previous release back is test_community_boss_cli's)
    result = _run("bash", REPO / "scripts/install/upgrade.sh", "--dir", installed, "--no-rollback",
                  cwd=REPO, env=env)

    assert result.returncode != 0  # fake Docker stops the fetched installer at build
    assert (tmp / "backup-trace").read_text().splitlines() == [old]
    assert _git("rev-parse", "HEAD", cwd=installed) == new
    assert b"BOSS_API_KEY=private-test-fixture\n" in (installed / ".env").read_bytes()
    assert b"BOSS_DB_PASSWORD=existing\n" in (installed / ".env").read_bytes()
    assert (tmp / "recovery/.env").read_bytes() == prior_env


def test_inherited_matching_backup_cannot_skip_a_new_snapshot(install_pair):
    installed, old, new, env, tmp = install_pair
    backup = tmp / "claimed-backup"
    backup.mkdir()
    for filename in (".env", "boss.dump", "artifacts.tar"):
        (backup / filename).write_text("fixture\n")
    (backup / "source-commit").write_text(old + "\n")
    env["BOSS_PREUPGRADE_BACKUP_DIR"] = str(backup)

    result = _install(installed, env)

    assert result.returncode == 86
    assert "Ignoring a legacy pre-upgrade backup path" in result.stdout
    assert (tmp / "backup-trace").read_text().splitlines() == [old]
    assert _git("rev-parse", "HEAD", cwd=installed) == new
    assert (tmp / "docker-trace").exists()


def test_upgrade_forwards_backup_location_and_uses_one_fresh_snapshot(install_pair):
    installed, old, _, env, tmp = install_pair
    _publish_real_backup_helper(tmp)
    _stopped_database_backend(tmp, env)
    custom = tmp / "custom-backups"

    result = _run("bash", REPO / "scripts/install/upgrade.sh", "--dir", installed,
                  "--backup-dir", custom, cwd=REPO, env=env)

    assert result.returncode != 0  # fake Docker stops the installer at build
    backups = sorted(custom.glob("boss-*/source-commit"))
    assert len(backups) == 1
    assert backups[0].read_text().strip() == old
    assert not list((tmp / "backups").glob("boss-*/source-commit"))


def test_same_revision_rerun_does_not_require_a_running_database(install_pair):
    installed, _, new, env, tmp = install_pair
    _git("fetch", "origin", "main", cwd=installed)
    _git("merge", "--ff-only", "FETCH_HEAD", cwd=installed)

    result = _install(installed, env)

    assert result.returncode == 86, result.stderr + result.stdout
    assert _git("rev-parse", "HEAD", cwd=installed) == new
    assert not (tmp / "backup-trace").exists()


@pytest.mark.parametrize("dump_fails", [False, True])
def test_changed_source_reinstall_restores_stopped_database(install_pair, dump_fails):
    installed, old, _, env, tmp = install_pair
    target = _publish_real_backup_helper(tmp)
    _stopped_database_backend(tmp, env)
    if dump_fails:
        env["BOSS_TEST_DUMP_FAIL"] = "1"
    prior_env = (installed / ".env").read_bytes()

    result = _install(installed, env)

    assert (tmp / "db-state").read_text().strip() == "stopped"
    steps = (tmp / "docker-trace").read_text().splitlines()
    assert steps[:2] == ["start", "stop"]
    if dump_fails:
        assert result.returncode != 0
        assert "Recovery backup failed" in result.stderr
        assert steps == ["start", "stop"]
        assert _git("rev-parse", "HEAD", cwd=installed) == old
        assert (installed / ".env").read_bytes() == prior_env
    else:
        assert result.returncode == 86, result.stderr + result.stdout
        assert steps == ["start", "stop", "build"]
        assert _git("rev-parse", "HEAD", cwd=installed) == target
        backups = sorted((tmp / "backups").glob("boss-*/source-commit"))
        assert len(backups) == 1
        assert backups[0].read_text().strip() == old
        assert b"BOSS_API_KEY=private-test-fixture\n" in (installed / ".env").read_bytes()


def test_backup_preserves_running_database_and_worker_state(install_pair):
    installed, old, _, env, tmp = install_pair
    _stopped_database_backend(tmp, env)
    (tmp / "db-state").write_text("running\n")
    worker_state = tmp / "worker-state"
    worker_state.write_text("running\n")
    env["BOSS_TEST_WORKER_STATE"] = str(worker_state)

    result = _run("bash", REPO / "scripts/install/backup.sh", "--dir", installed,
                  "--backup-dir", tmp / "backups", cwd=REPO, env=env)

    assert result.returncode == 0, result.stderr
    assert (tmp / "docker-trace").read_text().splitlines() == ["stop-worker", "start-worker"]
    assert (tmp / "db-state").read_text().strip() == "running"
    assert worker_state.read_text().strip() == "running"
    assert (Path(result.stdout.strip()) / "source-commit").read_text().strip() == old


def test_missing_api_image_does_not_pause_running_writers(install_pair):
    installed, _, _, env, tmp = install_pair
    _stopped_database_backend(tmp, env)
    (tmp / "db-state").write_text("running\n")
    worker_state = tmp / "worker-state"
    worker_state.write_text("running\n")
    env["BOSS_TEST_WORKER_STATE"] = str(worker_state)
    env["BOSS_TEST_API_IMAGE_MISSING"] = "1"

    result = _run("bash", REPO / "scripts/install/backup.sh", "--dir", installed,
                  "--backup-dir", tmp / "backups", cwd=REPO, env=env)

    assert result.returncode != 0
    assert "backup will not build or pull it" in result.stderr
    assert (tmp / "db-state").read_text().strip() == "running"
    assert worker_state.read_text().strip() == "running"
    assert not (tmp / "docker-trace").exists()


def test_missing_database_container_fails_before_source_change(install_pair):
    installed, old, _, env, tmp = install_pair
    _publish_real_backup_helper(tmp)
    _stopped_database_backend(tmp, env)
    env["BOSS_TEST_CONTAINER_MISSING"] = "1"
    prior_env = (installed / ".env").read_bytes()

    result = _install(installed, env)

    assert result.returncode != 0
    assert "installed Postgres container is missing" in result.stderr
    assert _git("rev-parse", "HEAD", cwd=installed) == old
    assert (installed / ".env").read_bytes() == prior_env
    assert not (tmp / "docker-trace").exists()


def test_fresh_install_accepts_exact_commit_ref(install_pair):
    _, _, new, env, tmp = install_pair
    fresh = tmp / "fresh"
    env["BOSS_REPO_REF"] = new

    result = _install(fresh, env)

    assert result.returncode == 86, result.stderr + result.stdout
    assert _git("rev-parse", "HEAD", cwd=fresh) == new


def test_safe_uninstall_keeps_stopped_container_for_a_later_backup(install_pair):
    installed, _, _, env, tmp = install_pair
    _stopped_database_backend(tmp, env)

    result = _run("bash", REPO / "scripts/install/uninstall.sh", "--dir", installed,
                  "--yes", cwd=REPO, env=env)

    assert result.returncode == 0, result.stderr
    assert (tmp / "docker-trace").read_text().splitlines() == ["stop-all"]
    assert " down" not in (tmp / "docker-trace").read_text()


def _published_git_checkout_command():
    section = (REPO / "scripts/install/README.md").read_text().split(
        "### Straight from GitHub", 1)[1].split("### From a local checkout", 1)[0]
    return section.split("```bash\n", 1)[1].split("```", 1)[0]


@pytest.mark.parametrize("clone_fails", [False, True])
def test_published_git_checkout_pins_its_commit_and_stops_on_clone_failure(
    install_pair, clone_fails,
):
    _, _, new, env, tmp = install_pair
    source = tmp / "source"
    origin = tmp / "origin.git"
    _git("tag", "v1.0-rc8", new, cwd=source)
    _git("push", "origin", "refs/tags/v1.0-rc8", cwd=source)
    checkout = tmp / "boss-rc8"
    remote = tmp / "missing-origin.git" if clone_fails else origin
    command = _published_git_checkout_command().replace(
        "https://github.com/seanventures/boss.git", shlex.quote(str(remote)))
    # The private fixture remote contains its own two commits, not the public
    # release object named by the guide. Preserve the exact-SHA checkout contract.
    command = re.sub(r"BOSS_RELEASE_COMMIT=[0-9a-f]{40}", f"BOSS_RELEASE_COMMIT={new}", command)
    command = command.replace(
        './install.sh --dir "$PWD" --ref "$BOSS_RELEASE_COMMIT"',
        './install.sh --dir "$PWD" --ref "$BOSS_RELEASE_COMMIT" '
        '--yes --skip-model-key --no-install-deps')

    result = _run("bash", "-c", command, cwd=tmp, env=env)

    if clone_fails:
        assert result.returncode == 128, result.stderr
        assert not checkout.exists()
        assert not (tmp / "docker-trace").exists()
    else:
        assert result.returncode == 86, result.stderr + result.stdout
        assert _git("rev-parse", "HEAD", cwd=checkout) == new
        record = dict(line.split("=", 1) for line in
                      (checkout / ".boss-release").read_text().splitlines() if "=" in line)
        assert record["BOSS_RELEASE_CHANNEL"] == "pinned"
        assert record["BOSS_RELEASE_REF"] == new


def test_published_git_checkout_never_runs_an_existing_directory(install_pair):
    _, _, new, env, tmp = install_pair
    source = tmp / "source"
    _git("tag", "v1.0-rc8", new, cwd=source)
    _git("push", "origin", "refs/tags/v1.0-rc8", cwd=source)
    checkout = tmp / "boss-rc8"
    checkout.mkdir()
    marker = tmp / "unexpected-install"
    stale = checkout / "install.sh"
    stale.write_text("#!/usr/bin/env bash\ntouch " + shlex.quote(str(marker)) + "\n")
    stale.chmod(0o755)
    command = _published_git_checkout_command().replace(
        "https://github.com/seanventures/boss.git", shlex.quote(str(tmp / "origin.git")))

    result = _run("bash", "-c", command, cwd=tmp, env=env)

    assert result.returncode == 128, result.stderr
    assert not marker.exists()
    assert stale.exists()
    assert not (tmp / "docker-trace").exists()


@pytest.mark.skipif(os.environ.get("BOSS_RUN_DOCKER_LIFECYCLE_TEST") != "1",
                    reason="requires a local Docker daemon and cached postgres:16-alpine image")
def test_real_compose_backup_keeps_stopped_database_image_with_drifted_tag(tmp_path: Path):
    """Exercise Compose start/run against Docker, without pulling any image."""
    if _run("docker", "info").returncode or _run("docker", "image", "inspect", "postgres:16-alpine").returncode:
        pytest.skip("Docker or the cached Postgres image is unavailable")
    installed = tmp_path / "installed"
    compose_path = installed / "scripts/install/community-compose.yml"
    compose_path.parent.mkdir(parents=True)
    project = "bossbackup" + uuid.uuid4().hex[:12]
    api_image = f"{project}api:fixture"
    (installed / "Dockerfile.fixture-api").write_text(
        "FROM postgres:16-alpine\nRUN echo build-was-run >/build-was-run\n"
    )
    compose_path.write_text(
        f"name: {project}\n"
        "services:\n"
        "  db:\n"
        "    image: postgres:16-alpine\n"
        "    environment:\n"
        "      POSTGRES_USER: boss\n"
        "      POSTGRES_PASSWORD: fixture\n"
        "      POSTGRES_DB: boss\n"
        "    volumes:\n"
        "      - pg:/var/lib/postgresql/data\n"
        "  api:\n"
        "    build:\n"
        "      context: ../..\n"
        "      dockerfile: Dockerfile.fixture-api\n"
        f"    image: {api_image}\n"
        "    volumes:\n"
        "      - artifacts:/var/lib/boss/artifacts\n"
        "      - identity:/var/lib/boss/identity\n"
        "      - claude-home:/home/boss/.claude\n"
        "volumes:\n"
        "  pg: {}\n"
        "  artifacts: {}\n"
        "  identity: {}\n"
        "  claude-home: {}\n"
    )
    (installed / ".env").write_text("BOSS_DB_PASSWORD=fixture\n")
    _git("init", cwd=installed)
    _git("config", "user.name", "BOSS backup test", cwd=installed)
    _git("config", "user.email", "backup-test@example.invalid", cwd=installed)
    _git("add", ".", cwd=installed)
    _git("commit", "-m", "installed version", cwd=installed)
    compose = ("docker", "compose", "-f", compose_path, "--env-file", installed / ".env")
    try:
        assert _run("docker", "image", "tag", "postgres:16-alpine", api_image).returncode == 0
        marker = _run(*compose, "run", "--rm", "--no-deps", "--pull", "never", "-T",
                      "--entrypoint", "sh", "api", "-c",
                      "printf opaque-cli-test > /home/boss/.claude/session-marker")
        assert marker.returncode == 0, marker.stderr
        result = _run(*compose, "up", "-d", "--no-deps", "--pull", "never", "db")
        assert result.returncode == 0, result.stderr
        for _ in range(30):
            if _run(*compose, "exec", "-T", "db", "pg_isready", "-U", "boss", "-d", "boss").returncode == 0:
                break
            subprocess.run(["sleep", "1"], check=False)
        else:
            pytest.fail("fixture Postgres did not become ready")
        container_id = _run(*compose, "ps", "--all", "-q", "db").stdout.strip()
        image_id = _run("docker", "inspect", "--format", "{{.Image}}", container_id).stdout.strip()
        assert container_id and image_id
        assert _run(*compose, "stop", "db").returncode == 0
        compose_path.write_text(compose_path.read_text().replace(
            "    image: postgres:16-alpine", "    image: postgres:missing-backup-fixture", 1,
        ))

        backup = _run("bash", REPO / "scripts/install/backup.sh", "--dir", installed,
                      "--backup-dir", tmp_path / "backups", cwd=REPO)

        assert backup.returncode == 0, backup.stderr
        assert (Path(backup.stdout.strip()) / "boss.dump").stat().st_size > 0
        # BOSS does not copy the provider-owned CLI session into its backup.
        # The named volume itself survives a safe stop or source upgrade.
        assert not (Path(backup.stdout.strip()) / "claude-home.tar").exists()
        assert all(b"opaque-cli-test" not in path.read_bytes()
                   for path in Path(backup.stdout.strip()).iterdir() if path.is_file())
        assert _run(*compose, "ps", "--all", "-q", "db").stdout.strip() == container_id
        assert _run("docker", "inspect", "--format", "{{.Image}}", container_id).stdout.strip() == image_id
        assert _run("docker", "inspect", "--format", "{{.State.Status}}", container_id).stdout.strip() == "exited"

        # A real API service has both build and image. Removing the local image
        # must fail backup without silently building or pulling a replacement.
        assert _run("docker", "image", "rm", api_image).returncode == 0
        failed = _run("bash", REPO / "scripts/install/backup.sh", "--dir", installed,
                      "--backup-dir", tmp_path / "backups", cwd=REPO)
        assert failed.returncode != 0
        assert "backup will not build or pull it" in failed.stderr
        assert _run("docker", "inspect", "--format", "{{.State.Status}}", container_id).stdout.strip() == "exited"
        assert _run("docker", "inspect", "--format", "{{.Image}}", container_id).stdout.strip() == image_id
        assert _run("docker", "image", "inspect", api_image).returncode != 0

        # Legacy safe uninstall used `down`, removing the DB container while
        # leaving the volume. Preserve an offline copy, then recreate only DB
        # from an explicitly selected, already-cached immutable image ID.
        assert _run(*compose, "down").returncode == 0
        assert _run("docker", "volume", "inspect", f"{project}_claude-home").returncode == 0
        absent = _run("bash", REPO / "scripts/install/backup.sh", "--dir", installed,
                      "--backup-dir", tmp_path / "backups", cwd=REPO)
        assert absent.returncode != 0
        assert "installed Postgres container is missing" in absent.stderr
        volume = f"{project}_pg"
        assert _run("docker", "volume", "inspect", volume).returncode == 0
        guide = (REPO / "scripts/install/README.md").read_text()
        recovery = guide.split("### Recover an older safe uninstall\n", 1)[1].split(
            "```bash\n", 1,
        )[1].split("\n```", 1)[0].split("\n./scripts/install/backup.sh", 1)[0]
        recovery = recovery.replace("cd ~/boss", f"cd {shlex.quote(str(installed))}")
        recovery = recovery.replace(
            "DB_IMAGE_ID=sha256:REPLACE_WITH_REVIEWED_LOCAL_POSTGRES_16_IMAGE_ID",
            f"DB_IMAGE_ID={image_id}",
        )
        recovery_home = tmp_path / "recovery-home"
        recovery_home.mkdir()
        recovery_env = {
            **os.environ,
            "HOME": str(recovery_home),
            "DOCKER_CONFIG": os.environ.get("DOCKER_CONFIG", str(Path.home() / ".docker")),
        }

        # A customized Compose project without its expected volume must fail
        # before Docker run can silently create a new empty named volume.
        missing_project = f"{project}missing"
        missing = _run("bash", "-c", recovery, cwd=REPO,
                       env={**recovery_env, "COMPOSE_PROJECT_NAME": missing_project})
        assert missing.returncode != 0
        assert _run("docker", "volume", "inspect", f"{missing_project}_pg").returncode != 0
        assert not list((recovery_home / "boss-backups").glob("legacy-pg-offline.*/postgres-volume.tar"))
        assert _run(*compose, "ps", "--all", "-q", "db").stdout.strip() == ""

        # An existing but empty volume produces a nonempty tar header. Archive
        # validation must reject it before Compose creates a database.
        empty_project = f"{project}empty"
        empty_volume = f"{empty_project}_pg"
        assert _run("docker", "volume", "create", empty_volume).returncode == 0
        try:
            empty_env = {**recovery_env, "COMPOSE_PROJECT_NAME": empty_project}
            empty = _run("bash", "-c", recovery, cwd=REPO, env=empty_env)
            assert empty.returncode != 0
            assert _run(*compose, "ps", "--all", "-q", "db", env=empty_env).stdout.strip() == ""
            assert not list((recovery_home / "boss-backups").glob("legacy-pg-offline.*/postgres-volume.tar"))
        finally:
            _run("docker", "volume", "rm", empty_volume)

        recovered = _run("bash", "-c", recovery, cwd=REPO, env=recovery_env)
        assert recovered.returncode == 0, recovered.stderr
        archives = list((recovery_home / "boss-backups").glob("legacy-pg-offline.*/postgres-volume.tar"))
        assert len(archives) == 1
        assert _run("tar", "-xOf", archives[0], "./PG_VERSION").stdout.strip() == "16"
        assert _run("tar", "-tf", archives[0], "./global/pg_control").returncode == 0
        assert _run("docker", "image", "tag", "postgres:16-alpine", api_image).returncode == 0
        restored = _run("bash", REPO / "scripts/install/backup.sh", "--dir", installed,
                        "--backup-dir", tmp_path / "backups", cwd=REPO)
        assert restored.returncode == 0, restored.stderr
        recovered_id = _run(*compose, "ps", "--all", "-q", "db").stdout.strip()
        assert _run("docker", "inspect", "--format", "{{.Image}}", recovered_id).stdout.strip() == image_id
        assert _run("docker", "inspect", "--format", "{{.State.Status}}", recovered_id).stdout.strip() == "exited"
    finally:
        _run(*compose, "down", "--volumes", "--remove-orphans")
        _run("docker", "image", "rm", api_image)


def test_interrupted_snapshot_discards_completion_and_restores_prior_state(install_pair):
    installed, _, _, env, tmp = install_pair
    _stopped_database_backend(tmp, env)
    (tmp / "db-state").write_text("running\n")
    worker_state = tmp / "worker-state"
    worker_state.write_text("running\n")
    env["BOSS_TEST_WORKER_STATE"] = str(worker_state)
    env["BOSS_TEST_INTERRUPT_ARCHIVE"] = "1"
    result = _run("bash", REPO / "scripts/install/backup.sh", "--dir", installed,
                  "--backup-dir", tmp / "backups", cwd=REPO, env=env)
    assert result.returncode != 0
    assert worker_state.read_text().strip() == "running"
    assert (tmp / "db-state").read_text().strip() == "running"
    assert not list((tmp / "backups").glob("boss-*"))
    assert "down" not in (tmp / "docker-trace").read_text()
