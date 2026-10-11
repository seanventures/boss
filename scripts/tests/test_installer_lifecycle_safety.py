"""Safe local checks for the Community install and remote model command."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import shlex

import pytest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _installer_block(name: str) -> str:
    source = (ROOT / "install.sh").read_text()
    return source.split(f"# ---- {name} ---", 1)[1].split(f"# ---- end {name} ---", 1)[0].split("\n", 1)[1]


def _serial_builds(tmp_path, profiles="", *, fail="", invalid=False, config_fail=False, bad_name=None):
    config = {"services": {
        "db": {"image": "postgres:16-alpine"},
        "api": {"build": {}, "environment": {"SECRET": "never-render-this-secret"}},
        "worker": {"build": {}},
        "mcp-tools": {"build": {}},
        "egress": {"build": {}},
        "web": {"build": {}, "profiles": ["web"]},
        "optional-builder": {"build": {}, "profiles": ["secrets"]},
        "redis": {"image": "redis", "profiles": ["secrets"]},
    }}
    if bad_name is not None:
        config["services"][bad_name] = {"build": {}}
    config_path = tmp_path / "config.json"
    config_path.write_text("never-render-this-secret" if invalid else json.dumps(config))
    fake = tmp_path / "compose"
    fake.write_text("""#!/usr/bin/env python3
import json, os, pathlib, sys, time
args = sys.argv[1:]
with open(os.environ['TRACE'], 'a') as log:
    log.write(json.dumps(args) + '\\n')
if args[-3:] == ['config', '--format', 'json']:
    if os.environ['CONFIG_FAIL'] == '1':
        print('never-render-this-secret', file=sys.stderr)
        sys.exit(27)
    print(pathlib.Path(os.environ['CONFIG']).read_text())
elif len(args) >= 2 and args[-2] == 'build':
    service = args[-1]
    active = pathlib.Path(os.environ['TRACE'] + '.active')
    active.mkdir()  # A second build cannot start until the first load finishes.
    with open(os.environ['TRACE'], 'a') as log:
        log.write(json.dumps(['start', service]) + '\\n')
    time.sleep(0.01)
    if service == os.environ['FAIL_SERVICE']:
        print('fixture build failed', file=sys.stderr)
        sys.exit(37)
    active.rmdir()
    with open(os.environ['TRACE'], 'a') as log:
        log.write(json.dumps(['loaded', service]) + '\\n')
else:
    sys.exit(91)
""")
    fake.chmod(0o700)
    script = tmp_path / "build.sh"
    script.write_text(
        "set -euo pipefail\nui_info() { printf '%s\\n' \"$*\"; }\n"
        "die() { printf '%s\\n' \"$*\" >&2; exit 1; }\n"
        f"COMPOSE=({shlex.quote(str(fake))}); COMPOSE_PROFILES={shlex.quote(profiles)}\n"
        "ENV_FILE='private path/.env'\n" + _installer_block("serial image builds")
        + "\nprintf 'reached start phase\\n'\n"
    )
    trace = tmp_path / "trace"
    result = subprocess.run(["bash", str(script)], text=True, capture_output=True, timeout=10,
                            env={**os.environ, "TRACE": str(trace), "CONFIG": str(config_path),
                                 "FAIL_SERVICE": fail, "CONFIG_FAIL": str(int(config_fail))})
    return result, [json.loads(line) for line in trace.read_text().splitlines()]


@pytest.mark.parametrize("profiles,optional", [
    ("", []), ("--profile web", ["web"]),
    ("--profile secrets", ["optional-builder"]),
    ("--profile web --profile secrets", ["web", "optional-builder"]),
])
def test_installer_finishes_each_selected_image_before_starting_another(tmp_path, profiles, optional):
    result, calls = _serial_builds(tmp_path, profiles)
    assert result.returncode == 0, result.stdout + result.stderr
    expected = ["api", "worker", "mcp-tools", "egress", *optional]
    events = [call for call in calls if call[0] in {"start", "loaded"}]
    assert events == [event for service in expected for event in (["start", service], ["loaded", service])]
    builds = [call for call in calls if len(call) > 2 and call[-2] == "build"]
    assert [call[-1] for call in builds] == expected
    prefix = ["-f", "scripts/install/community-compose.yml", "--env-file", "private path/.env", *profiles.split()]
    assert all(call[:-2] == prefix for call in builds)
    assert "never-render-this-secret" not in result.stdout + result.stderr
    assert "reached start phase" in result.stdout


def test_installer_failed_image_load_stops_later_builds_and_startup(tmp_path):
    result, calls = _serial_builds(tmp_path, "--profile web", fail="worker")
    assert result.returncode == 37
    assert [call for call in calls if call[0] == "start"] == [["start", "api"], ["start", "worker"]]
    assert "reached start phase" not in result.stdout
    assert "fixture build failed" in result.stderr


@pytest.mark.parametrize("invalid,config_fail", [(True, False), (False, True)])
def test_installer_config_failure_has_no_builds_or_expanded_secret_output(tmp_path, invalid, config_fail):
    result, calls = _serial_builds(tmp_path, invalid=invalid, config_fail=config_fail)
    assert result.returncode != 0
    assert len(calls) == 1 and calls[0][-3:] == ["config", "--format", "json"]
    assert "reached start phase" not in result.stdout
    assert "never-render-this-secret" not in result.stdout + result.stderr
    assert "Could not resolve" in result.stderr


@pytest.mark.parametrize("name", ["--push", "api;touch-unowned-file", "two services"])
def test_installer_rejects_unsafe_service_names_before_any_build(tmp_path, name):
    result, calls = _serial_builds(tmp_path, bad_name=name)
    assert result.returncode != 0
    assert len(calls) == 1 and calls[0][-3:] == ["config", "--format", "json"]
    assert "reached start phase" not in result.stdout
    assert name not in result.stdout + result.stderr


def _storage_advice(tmp_path, **overrides):
    store = tmp_path / "docker-store"
    store.mkdir()
    script = tmp_path / "storage.sh"
    script.write_text(
        "set -euo pipefail\n"
        "ui_info() { printf '%s\\n' \"$*\"; }; ui_warn() { ui_info \"$@\"; }\n"
        "need_cmd() { command -v \"$1\" >/dev/null; }\n"
        "docker() { case $1 in context) printf '%s\\n' \"$ENDPOINT\";; info) printf '%s|%s\\n' \"$STORE\" \"$DAEMON_OS\";; *) return 91;; esac; }\n"
        "df() { printf '%s\\n' \"$2\" >> \"$TRACE\"; local free=$INSTALL_FREE; [[ $2 != \"$STORE\" ]] || free=$STORE_FREE; printf 'Filesystem 1024-blocks Used Available Capacity Mounted\\nfixture 100000000 1000 %s 50%% /\\n' \"$free\"; }\n"
        "DOCKER=(docker)\n" + _installer_block("storage advice")
    )
    trace = tmp_path / "df-trace"
    env = {**os.environ, "OS": "Linux", "INSTALL_DIR": str(tmp_path / "missing/subdir"),
           "DOCKER_CONTEXT": "", "DOCKER_HOST": "", "ENDPOINT": "unix:///var/run/docker.sock",
           "STORE": str(store), "DAEMON_OS": "Ubuntu 24.04", "INSTALL_FREE": "100000000",
           "STORE_FREE": "14000000", "TRACE": str(trace), **overrides}
    result = subprocess.run(["bash", str(script)], text=True, capture_output=True, timeout=10, env=env)
    return result, trace.read_text().splitlines(), store


def test_installer_measures_separate_local_store_without_rejecting_small_capacity(tmp_path):
    result, measured, store = _storage_advice(tmp_path)
    assert result.returncode == 0
    assert measured == [str(tmp_path), str(store)]
    assert "Docker image-store filesystem" in result.stdout and "13.4 GiB free" in result.stdout
    assert "This is advisory" in result.stdout
    assert "Disk space looks OK" not in result.stdout


@pytest.mark.parametrize("overrides", [
    {"DOCKER_HOST": "ssh://remote.invalid"},
    {"ENDPOINT": "tcp://remote.invalid:2376"},
    {"OS": "Darwin"},
    {"DAEMON_OS": "Docker Desktop"},
    {"STORE": "/nonexistent-boss-test-store"},
    {"DOCKER_CONTEXT": "remote", "DOCKER_HOST": "unix:///var/run/docker.sock", "ENDPOINT": "ssh://remote.invalid"},
])
def test_installer_never_measures_remote_desktop_or_inaccessible_store_as_local(tmp_path, overrides):
    result, measured, _ = _storage_advice(tmp_path, **overrides)
    assert result.returncode == 0
    assert measured == [str(tmp_path)]
    assert "Docker image-store free space is unknown" in result.stdout
    assert "Docker image-store filesystem (" not in result.stdout


def test_installer_unknown_df_output_is_not_reported_as_sufficient(tmp_path):
    result, _, _ = _storage_advice(tmp_path, INSTALL_FREE="unknown", STORE_FREE="")
    assert result.returncode == 0
    assert "Install filesystem: free space could not be measured" in result.stdout
    assert "Docker image-store filesystem: free space could not be measured" in result.stdout
    assert "GiB free" not in result.stdout and "Disk space looks OK" not in result.stdout


def test_checkup_treats_private_env_as_data(tmp_path: Path) -> None:
    marker = tmp_path / "must-not-run"
    env_file = tmp_path / ".env"
    env_file.write_text(
        "BOSS_API_PORT=49999\n"
        "BOSS_WEB_PORT=49998\n"
        "BOSS_DEFAULT_PROJECT_ID=boss-dogfood\n"
        f"BOSS_API_KEY=$(touch {marker})\n"
    )
    result = subprocess.run(
        ["bash", str(ROOT / "scripts/install/checkup.sh")],
        env={**os.environ, "BOSS_ENV_FILE": str(env_file)},
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode in (0, 1)
    assert not marker.exists(), "a .env value must never be executed as shell code"
    assert "49999" in result.stdout


def _fake_ssh(tmp_path: Path, response: str) -> tuple[Path, Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    payload_path = tmp_path / "payload.json"
    fake = bin_dir / "ssh"
    fake.write_text(
        "#!/bin/sh\n"
        "case \"$*\" in\n"
        "  *'/v1/setup/model'*)\n"
        "    cat > \"$BOSS_TEST_PAYLOAD_PATH\"\n"
        f"    printf '%s\\n' '{response}'\n"
        "    ;;\n"
        "  *) exit 0 ;;\n"
        "esac\n"
    )
    fake.chmod(0o700)
    return bin_dir, payload_path


def test_remote_model_reads_key_from_stdin_and_reports_unverified(tmp_path: Path) -> None:
    bin_dir, payload_path = _fake_ssh(
        tmp_path, '{"ok": true, "provider": "custom", "verification_state": "unverified"}'
    )
    secret = "test-secret-should-not-appear-in-output"
    result = subprocess.run(
        [
            "bash", str(ROOT / "scripts/install/remote.sh"),
            "--host", "example.invalid", "model", "--provider", "custom",
            "--base-url", "http://model.internal/v1", "--model", "local-model",
        ],
        input=secret + "\n",
        env={
            **os.environ,
            "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"],
            "BOSS_TEST_PAYLOAD_PATH": str(payload_path),
        },
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(payload_path.read_text())
    assert payload["api_key"] == secret
    assert payload["base_url"] == "http://model.internal/v1"
    assert "not been verified" in result.stdout
    assert secret not in result.stdout + result.stderr


def test_remote_model_error_with_provider_field_is_not_success(tmp_path: Path) -> None:
    bin_dir, payload_path = _fake_ssh(
        tmp_path, '{"detail": {"provider": "openrouter", "error": "bad key"}}'
    )
    result = subprocess.run(
        [
            "bash", str(ROOT / "scripts/install/remote.sh"),
            "--host", "example.invalid", "model", "--provider", "openrouter",
        ],
        input="test-secret\n",
        env={
            **os.environ,
            "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"],
            "BOSS_TEST_PAYLOAD_PATH": str(payload_path),
        },
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 1
    assert payload_path.exists()
    assert "did not accept" in result.stdout
    assert "stored" not in result.stdout


def test_remote_claude_setup_token_missing_fails_before_contact(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    ssh_log = tmp_path / "ssh.log"
    fake = bin_dir / "ssh"
    fake.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' \"$*\" >> {ssh_log}\n"
        "echo STORE:should-not-run\n"
    )
    fake.chmod(0o700)
    result = subprocess.run(
        [
            "bash", str(ROOT / "scripts/install/remote.sh"),
            "--host", "example.invalid", "subscription", "--provider", "claude_code",
            "--accept-database-fallback",
        ],
        input="\n",
        env={**os.environ, "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"]},
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode != 0
    assert "empty" in result.stderr
    assert "test-claude-token" not in result.stdout + result.stderr
    assert not ssh_log.exists()


def test_installer_rerun_keeps_headless_choice_and_credentials(tmp_path: Path) -> None:
    """Exercise the real installer with local Git and inert Docker/API commands."""
    checkout = tmp_path / "checkout"
    helpers = checkout / "scripts/install"
    helpers.mkdir(parents=True)
    for name in ("restore_preserved_env.sh", "reconcile_api_credentials.py"):
        shutil.copy2(ROOT / "scripts/install" / name, helpers / name)
    (helpers / "community-compose.yml").write_text("name: boss-community\n")
    subprocess.run(["git", "init", "-q", str(checkout)], check=True)
    subprocess.run(["git", "-C", str(checkout), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(checkout), "-c", "user.name=Installer Test",
         "-c", "user.email=installer@example.invalid", "commit", "-qm", "fixture"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(checkout), "remote", "add", "origin", str(checkout)],
        check=True,
    )
    env_file = checkout / ".env"
    env_file.write_text(
        "BOSS_API_KEY=kept-owner\nBOSS_WORKER_API_KEY=kept-worker\n"
        "BOSS_API_KEYS=kept-owner,kept-worker\n"
        "BOSS_API_KEY_PRINCIPALS=kept-owner:ceo,kept-worker:system\n"
        "BOSS_DB_PASSWORD=kept-db\n"
        "BOSS_CONSOLE_PASSWORD=kept-console\n"
        "BOSS_CONSOLE_SESSION_SECRET=kept-session\n"
        "BOSS_API_PORT=49999\nBOSS_WEB_PORT=49998\nBOSS_POSTGRES_PORT=49997\n"
        "BOSS_COMMUNITY_WEB_ENABLED=0\n"
        "INFISICAL_ENCRYPTION_KEY=kept-encryption\n"
        "INFISICAL_AUTH_SECRET=kept-auth-secret\n"
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker_calls = tmp_path / "docker-calls"
    docker = bin_dir / "docker"
    docker.write_text(
        "#!/bin/sh\n"
        "case \" $* \" in *' version '*) echo 'Docker Compose test'; exit 0 ;; esac\n"
        "case \" $* \" in *' config --format json '*) echo '{\"services\":{\"api\":{\"build\":{}},\"web\":{\"build\":{},\"profiles\":[\"web\"]}}}'; exit 0 ;; esac\n"
        "printf '%s\\n' \"$*\" >> \"$BOSS_TEST_DOCKER_CALLS\"\n"
        "exit 0\n"
    )
    docker.chmod(0o700)
    curl = bin_dir / "curl"
    curl.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "args=sys.argv[1:]\n"
        "if '/v1/edition' in ' '.join(args):\n"
        "    out=args[args.index('-o')+1]\n"
        "    open(out,'w').write('{\"edition\":\"community\"}')\n"
        "    print('200',end='')\n"
        "elif '/v1/providers' in ' '.join(args):\n"
        "    print('200',end='')\n"
    )
    curl.chmod(0o700)
    env = {
        **os.environ,
        "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"],
        "BOSS_TEST_DOCKER_CALLS": str(docker_calls),
    }
    for extra, expected in (([], "0"), (["--web"], "1")):
        result = subprocess.run(
            ["bash", str(ROOT / "install.sh"), "--dir", str(checkout),
             "--yes", "--skip-model-key", "--ref", "HEAD", *extra],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        rendered = result.stdout + result.stderr
        assert "kept-owner" not in rendered
        assert "kept-db" not in rendered
        assert "kept-console" not in rendered
        assert "kept-session" not in rendered
        values = dict(
            line.split("=", 1) for line in env_file.read_text().splitlines()
            if "=" in line and not line.startswith("#")
        )
        assert values["BOSS_COMMUNITY_WEB_ENABLED"] == expected
        assert values["BOSS_API_KEY"] == "kept-owner"
        assert values["BOSS_DB_PASSWORD"] == "kept-db"
        assert values["BOSS_CONSOLE_PASSWORD"] == "kept-console"
        calls = docker_calls.read_text().splitlines()
        if expected == "0":
            assert any("--profile web stop web" in call for call in calls)
            assert not any("--profile web" in call and " build" in call for call in calls)
        else:
            assert any("--profile web" in call and " build" in call for call in calls)

    failed_store = subprocess.run(
        ["bash", str(ROOT / "install.sh"), "--dir", str(checkout),
         "--yes", "--skip-model-key", "--ref", "HEAD", "--secret-store", "infisical"],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert failed_store.returncode != 0
    assert "selected secret store cannot be provisioned" in failed_store.stderr
    assert "BOSS Community is running" not in failed_store.stdout
    assert "BOSS_COMMUNITY_SECRET_STORE=infisical" in env_file.read_text()

    rerun = subprocess.run(
        ["bash", str(ROOT / "install.sh"), "--dir", str(checkout),
         "--yes", "--skip-model-key", "--ref", "HEAD"],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert rerun.returncode != 0
    assert "selected secret store cannot be provisioned" in rerun.stderr


def _private_host_denylist() -> tuple[str, ...]:
    """Private CI loads the unexported denylist. A Community tree has no list and skips it."""
    path = ROOT / "scripts" / "private" / "public_export_host_denylist.txt"
    if not path.is_file():
        return ()
    hosts: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        host = line.split("#", 1)[0].strip()
        if host:
            hosts.append(host)
    return tuple(hosts)


def test_installer_sources_do_not_name_private_hosts_or_registries() -> None:
    """A stranger's install must not call a private network or registry."""
    forbidden = (*_private_host_denylist(), "OPS_TOKEN")
    private_registry = re.compile(r"[\w.-]+\.ts\.net(?::\d+)?", re.IGNORECASE)
    paths = [ROOT / "install.sh", *sorted((ROOT / "scripts/install").rglob("*"))]
    offenders: list[str] = []
    for path in paths:
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        # The public website is the one reviewed host under the private domain; the
        # exporter applies the same exact-origin allowance.
        text = re.sub(r"https://boss\.sean\.network(?=[/\"'\s)<>\]`]|$)", "", text)
        relative = path.relative_to(ROOT).as_posix()
        offenders.extend(f"{relative}: {needle}" for needle in forbidden if needle in text)
        if private_registry.search(text):
            offenders.append(f"{relative}: private registry host")
    assert offenders == []
    compose = (ROOT / "scripts/install/community-compose.yml").read_text(encoding="utf-8")
    published = [
        line.strip() for line in compose.splitlines()
        if re.search(r'- "[^"]+:\d+"\s*$', line)
    ]
    assert published == [
        '- "127.0.0.1:${BOSS_POSTGRES_PORT:-5432}:5432"',
        '- "127.0.0.1:${INFISICAL_PORT:-8080}:8080"',
        '- "${BOSS_API_BIND:-127.0.0.1}:${BOSS_API_PORT:-8000}:8000"',
        '- "127.0.0.1:${BOSS_WEB_PORT:-3000}:3000"',
    ]
    assert all("0.0.0.0" not in line for line in published)
    installer = (ROOT / "install.sh").read_text(encoding="utf-8")
    assert 'BIND_ADDRESS="127.0.0.1"' in installer
    # The GitHub-only command checks out and installs the same literal full commit.
    # Resolving an arbitrary tag's HEAD alone does not establish the reviewed source.
    readme = (ROOT / "scripts/install/README.md").read_text(encoding="utf-8")
    github_command = readme.split("### Straight from GitHub", 1)[1].split("```bash", 1)[1].split("```", 1)[0]
    assert re.search(r"^\s*BOSS_RELEASE_COMMIT=[0-9a-f]{40}\s*$", github_command, re.MULTILINE)
    assert 'git checkout --detach "$BOSS_RELEASE_COMMIT"' in github_command
    assert './install.sh --dir "$PWD" --ref "$BOSS_RELEASE_COMMIT"' in github_command
    assert ".boss-shipped-commit" in installer
    remote = (ROOT / "scripts/install/remote.sh").read_text(encoding="utf-8")
    assert "Bearer $key" not in remote
    assert "Bearer ${key}" not in remote


def test_secret_arguments_are_refused_and_not_echoed() -> None:
    secret = "history-secret-should-not-echo"
    install = subprocess.run(
        ["bash", str(ROOT / "install.sh"), "--openai-key", secret],
        capture_output=True, text=True, timeout=10, check=False,
    )
    assert install.returncode != 0
    rendered = install.stdout + install.stderr
    assert secret not in rendered
    assert "shell history" in rendered
    remote = subprocess.run(
        ["bash", str(ROOT / "scripts/install/remote.sh"),
         "--host", "example.invalid", "model", "--api-key", secret],
        capture_output=True, text=True, timeout=10, check=False,
    )
    assert remote.returncode != 0
    assert secret not in remote.stdout + remote.stderr
    assert "shell history" in remote.stdout + remote.stderr


def test_installer_makes_initdb_readable_before_the_database_starts() -> None:
    """umask 077 makes a cloned checkout owner-only; Postgres must still read initdb/."""
    text = (ROOT / "install.sh").read_text(encoding="utf-8")
    assert "umask 077" in text
    fix = text.index("chmod 755 scripts/install/initdb")
    first_up = text.index("up -d --no-build db api")
    assert fix < first_up
    assert "-name '*.sql' -exec chmod 644" in text
    compose = (ROOT / "scripts/install/community-compose.yml").read_text(encoding="utf-8")
    mounts = re.findall(r"^\s+- (\./[^:]+):", compose, re.MULTILINE)
    assert mounts == ["./initdb"], "a new bind mount needs the same readable-permissions step"
