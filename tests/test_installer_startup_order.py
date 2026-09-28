"""The one-line installer must not expose an unseeded project to its worker."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
INSTALLER = REPO / "install.sh"


def _harness(tmp_path: Path, *, secret_store: str = "database") -> tuple[Path, dict[str, str], Path, Path, Path]:
    upstream = tmp_path / "upstream"
    helpers = upstream / "scripts/install"
    helpers.mkdir(parents=True)
    for name in (
        "community-compose.yml", "restore_preserved_env.sh",
        "reconcile_api_credentials.py", "gen_infisical_keys.sh",
    ):
        shutil.copy2(REPO / "scripts/install" / name, helpers / name)
    (helpers / "bootstrap_infisical.sh").write_text("#!/bin/sh\nexit 0\n")
    subprocess.run(["git", "init", "-q", "-b", "main", str(upstream)], check=True)
    subprocess.run(["git", "-C", str(upstream), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(upstream), "-c", "user.name=Test",
         "-c", "user.email=test@example.invalid", "commit", "-qm", "fixture"],
        check=True,
    )

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "docker.log"
    seeded = tmp_path / "seeded"
    worker = tmp_path / "worker-running"
    docker = bin_dir / "docker"
    docker.write_text(
        "#!/bin/sh\n"
        'printf "%s\\n" "$*" >> "$BOSS_TEST_DOCKER_LOG"\n'
        'if [ "$1" = info ]; then exit 0; fi\n'
        'if [ "$1" = compose ] && [ "$2" = version ]; then echo "Docker Compose version v2"; exit 0; fi\n'
        'cmd=" $* "\n'
        'case "$cmd" in\n'
        '  *" ps --status running --services "*)\n'
        '    [ -e "$BOSS_TEST_WORKER" ] && echo worker\n'
        '    exit 0 ;;\n'
        '  *" stop worker "*) rm -f "$BOSS_TEST_WORKER"; exit 0 ;;\n'
        '  *" build "*) exit 0 ;;\n'
        '  *" exec -T api python -c "*|*" exec -T api python -m boss_engine.bootstrap "*)\n'
        '    [ ! -e "$BOSS_TEST_WORKER" ] || exit 81\n'
        '    [ "${BOSS_TEST_SEED_FAIL:-0}" != 1 ] || exit 9\n'
        '    touch "$BOSS_TEST_SEEDED"; exit 0 ;;\n'
        '  *" up -d "*)\n'
        '    case "$cmd" in\n'
        '      *" db api "*) [ ! -e "$BOSS_TEST_WORKER" ] || exit 82 ;;\n'
        '      *" infisical-redis infisical "*|*" --force-recreate api "*)\n'
        '        [ ! -e "$BOSS_TEST_WORKER" ] || exit 83 ;;\n'
        '      *) [ -e "$BOSS_TEST_SEEDED" ] || exit 84\n'
        '         touch "$BOSS_TEST_WORKER" ;;\n'
        '    esac\n'
        '    exit 0 ;;\n'
        'esac\n'
        'exit 0\n'
    )
    docker.chmod(0o755)
    curl = bin_dir / "curl"
    curl.write_text(
        "#!/bin/sh\n"
        "out=; code=\n"
        'while [ "$#" -gt 0 ]; do\n'
        '  if [ "$1" = -o ]; then out="$2"; shift 2; continue; fi\n'
        '  if [ "$1" = -w ]; then code=200; shift 2; continue; fi\n'
        '  shift\n'
        'done\n'
        'if [ -n "$out" ] && [ "$out" != /dev/null ]; then printf \'{"edition":"community"}\' > "$out"; fi\n'
        'if [ -n "$code" ]; then printf "%s" "$code"; fi\n'
    )
    curl.chmod(0o755)
    env = os.environ.copy()
    env.update(
        PATH=f"{bin_dir}:{env['PATH']}", BOSS_REPO_URL=str(upstream),
        BOSS_SECRET_STORE=secret_store, BOSS_TEST_DOCKER_LOG=str(log),
        BOSS_TEST_SEEDED=str(seeded), BOSS_TEST_WORKER=str(worker),
        OPENAI_API_KEY="", ANTHROPIC_API_KEY="", BOSS_GITHUB_TOKEN="",
    )
    return tmp_path / "boss", env, log, seeded, worker


def _install(install: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(INSTALLER), "--dir", str(install), "--yes",
         "--skip-model-key", "--no-install-deps"],
        env=env, text=True, capture_output=True, timeout=25, check=False,
    )


def test_fresh_install_and_rerun_seed_before_worker(tmp_path: Path) -> None:
    install, env, log, seeded, worker = _harness(tmp_path)
    first = _install(install, env)
    assert first.returncode == 0, first.stdout + first.stderr
    assert seeded.exists() and worker.exists()
    commands = log.read_text().splitlines()
    seed_index = next(i for i, command in enumerate(commands) if "exec -T api python -c" in command)
    worker_index = next(i for i, command in enumerate(commands) if " up -d --no-build" in command
                        and not command.endswith(" db api"))
    assert seed_index < worker_index
    assert "--profile web" in commands[worker_index]

    seeded.unlink()  # A rerun must pause the existing worker and seed again.
    before = len(commands)
    rerun = _install(install, env)
    assert rerun.returncode == 0, rerun.stdout + rerun.stderr
    assert seeded.exists() and worker.exists()
    rerun_commands = log.read_text().splitlines()[before:]
    stop_index = next(i for i, command in enumerate(rerun_commands) if " stop worker" in command)
    seed_index = next(i for i, command in enumerate(rerun_commands) if "exec -T api python -c" in command)
    worker_index = next(i for i, command in enumerate(rerun_commands) if " up -d --no-build" in command
                        and not command.endswith(" db api"))
    assert stop_index < seed_index < worker_index


def test_seed_failure_leaves_worker_stopped(tmp_path: Path) -> None:
    install, env, _, seeded, worker = _harness(tmp_path)
    env["BOSS_TEST_SEED_FAIL"] = "1"
    result = _install(install, env)
    assert result.returncode != 0
    assert "Seed failed" in result.stdout + result.stderr
    assert not seeded.exists() and not worker.exists()


def test_infisical_bootstrap_recreates_only_api_before_seed(tmp_path: Path) -> None:
    install, env, log, seeded, worker = _harness(tmp_path, secret_store="infisical")
    result = _install(install, env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert seeded.exists() and worker.exists()
    commands = log.read_text().splitlines()
    secret_index = next(i for i, command in enumerate(commands)
                        if command.endswith("up -d --no-build infisical-redis infisical"))
    recreate_index = next(i for i, command in enumerate(commands)
                          if "--force-recreate api" in command)
    seed_index = next(i for i, command in enumerate(commands) if "exec -T api python -c" in command)
    worker_index = next(i for i, command in enumerate(commands) if " up -d --no-build" in command
                        and not command.endswith(" db api")
                        and not command.endswith(" infisical-redis infisical"))
    assert secret_index < recreate_index < seed_index < worker_index
    assert all("--force-recreate api worker" not in command for command in commands)
