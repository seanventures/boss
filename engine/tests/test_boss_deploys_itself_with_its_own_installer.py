"""BOSS can be deployed by BOSS: boss.deploy.yaml declares it, the repo's own installer does it.

A governed stack deployment extracts a commit to <dir>/releases/<commit>, points <dir>/current
at it and runs the manifest's steps there. For BOSS those steps must be the installer README.md
documents — not a second install path that drifts from it — and the one thing a release layout
adds must hold: the install's credentials survive moving to a new release directory. An
installer that finds no .env generates new credentials, and those do not open the Postgres
volume the previous release created.
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
MANIFEST = REPO / "boss.deploy.yaml"
STACK = REPO / "scripts" / "deploy" / "boss_stack.sh"
PLACEHOLDERS = {"dir", "commit", "ref", "target.host", "target.public_url", "target.user"}


def _manifest() -> dict:
    return yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))


def _commands(data: dict) -> list[str]:
    return [*data.get("install", []), *data.get("update", []), *data.get("teardown", [])]


def test_the_manifest_loads_with_the_engines_own_parser():
    """The deploy engine's parser is the authority. It is written alongside this file; until
    it exists on this branch the schema checks below stand in for it."""
    try:
        from boss_engine.services import app_deploy_manifest
    except ImportError:
        pytest.skip("app_deploy_manifest is not on this branch yet; schema checked below")
    manifest = app_deploy_manifest.load_manifest(MANIFEST.read_text(encoding="utf-8"))
    assert manifest.name == "boss"


def test_the_manifest_follows_the_stack_deploy_schema():
    data = _manifest()

    assert data["version"] == 1
    assert re.fullmatch(r"[a-z0-9-]+", data["name"])
    assert data["kind"] == "commands"
    assert set(data) <= {"version", "name", "kind", "compose", "env", "install", "update",
                         "health", "open", "teardown", "requires"}
    for key in ("install", "update", "teardown"):
        assert data[key] and all(isinstance(c, str) and c.strip() for c in data[key]), key
    for probe in data["health"]:
        assert probe["url"].startswith("http://127.0.0.1:")
        assert probe["expect"] == 200 and probe["timeout_seconds"] > 0
    assert data["open"] == {"url": "{target.public_url}"}
    assert data["requires"] == {"sudo": True, "docker": False}


def test_every_placeholder_is_one_the_deployer_fills():
    data = _manifest()
    text = " ".join([*_commands(data), data["open"]["url"]])

    used = set(re.findall(r"\{([^{}]*)\}", text))
    assert used <= PLACEHOLDERS, used - PLACEHOLDERS


def test_the_api_and_the_console_are_both_health_gated():
    urls = [probe["url"] for probe in _manifest()["health"]]

    assert "http://127.0.0.1:8000/health" in urls
    assert any(u.startswith("http://127.0.0.1:3000/") for u in urls)


def test_the_steps_call_the_documented_installer_with_flags_it_accepts():
    """Every flag the stack script passes must be one install.sh parses, or the install dies
    with "Unknown option" on the host — after the release was already extracted."""
    script = STACK.read_text(encoding="utf-8")
    installer = (REPO / "install.sh").read_text(encoding="utf-8")
    call = re.search(r'bash "\$DIR/install\.sh" (.*?) \|\|', script)
    assert call, "boss_stack.sh must run the repository's install.sh"

    flags = re.findall(r"(--[a-z-]+)", call.group(1))
    assert flags == ["--dir", "--yes", "--skip-model-key", "--console-origin"]
    for flag in flags:
        assert re.search(rf"^\s+{re.escape(flag)}(\|[^)]*)?\)", installer, re.M), flag
    for command in _manifest()["install"]:
        assert command.startswith("bash scripts/deploy/boss_stack.sh ")


# ------------------------------------------------------------------- the release layout
def _release(root: Path, name: str, *, installer_exit: int = 0) -> Path:
    """A stand-in source tree: an install.sh that generates .env only when there is none,
    exactly the behaviour that strands a database when .env goes missing."""
    rel = root / "releases" / name
    (rel / "scripts" / "install").mkdir(parents=True)
    (rel / "scripts" / "install" / "community-compose.yml").write_text("name: x\n")
    (rel / "scripts" / "install" / "backup.sh").write_text(
        '#!/usr/bin/env bash\necho backed-up > "$(dirname "$0")/../../backup.ran"\n')
    (rel / "install.sh").write_text(
        "#!/usr/bin/env bash\n"
        'cd "$(dirname "$0")"\n'
        '[ -f .env ] || echo "PG_PASSWORD=$RANDOM$RANDOM" > .env\n'
        '[ -f backup.ran ] && echo after-backup > installer.saw\n'
        'echo "$@" > installer.args\n'
        f"exit {installer_exit}\n")
    return rel


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", str(STACK), *args], capture_output=True, text=True,
                          env={**os.environ, "HOME": os.environ.get("HOME", "/tmp")})


def _deploy(action: str, rel: Path, state: Path) -> subprocess.CompletedProcess:
    return _run(action, "--dir", str(rel), "--commit", "abc1234", "--state", str(state),
                "--origin", "https://boss.example.com")


def test_the_install_credentials_follow_the_install_into_the_next_release(tmp_path):
    state = tmp_path / ".boss" / "boss"
    first = _release(tmp_path, "aaa")
    assert _deploy("install", first, state).returncode == 0
    credentials = (first / ".env").read_text()

    second = _release(tmp_path, "bbb")
    result = _deploy("update", second, state)

    assert result.returncode == 0, result.stderr
    assert (second / ".env").read_text() == credentials
    assert (state / ".env").stat().st_mode & 0o777 == 0o600
    assert (second / ".boss-shipped-commit").read_text().strip() == "abc1234"


def test_an_update_backs_up_before_the_installer_runs(tmp_path):
    state = tmp_path / "state"
    _deploy("install", _release(tmp_path, "aaa"), state)
    second = _release(tmp_path, "bbb")

    _deploy("update", second, state)

    assert (second / "installer.saw").read_text().strip() == "after-backup"


def test_credentials_a_failed_install_wrote_are_kept(tmp_path):
    """The installer may fail after starting Postgres with credentials it just generated; the
    next attempt must use those, not mint another set."""
    state = tmp_path / "state"
    failed = _release(tmp_path, "aaa", installer_exit=1)

    assert _deploy("install", failed, state).returncode == 1
    assert (state / ".env").read_text() == (failed / ".env").read_text()


def test_the_installer_gets_the_targets_console_origin(tmp_path):
    rel = _release(tmp_path, "aaa")
    _deploy("install", rel, tmp_path / "state")

    args = (rel / "installer.args").read_text().split()
    assert args[args.index("--console-origin") + 1] == "https://boss.example.com"
    assert "--yes" in args


def test_a_non_https_origin_configures_no_proxy(tmp_path):
    result = _run("https", "--origin", "http://boss.example.com")

    assert result.returncode == 0
    assert "no proxy configured" in result.stdout
