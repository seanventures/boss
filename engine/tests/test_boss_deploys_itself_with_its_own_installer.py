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
import tempfile
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
MANIFEST = REPO / "boss.deploy.yaml"
STACK = REPO / "scripts" / "deploy" / "boss_stack.sh"
PLACEHOLDERS = {"dir", "commit", "ref", "target.host", "target.public_url", "target.user"}


def _manifest() -> dict:
    return yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))


def _step_command(step) -> str:
    """A step is a command string or the engine's {run, name, timeout_seconds} form."""
    return step["run"] if isinstance(step, dict) else step


def _commands(data: dict) -> list[str]:
    return [_step_command(s) for key in ("install", "update", "teardown")
            for s in data.get(key, [])]


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
        assert data[key], key
        for step in data[key]:
            if isinstance(step, dict):
                assert set(step) <= {"run", "name", "timeout_seconds"}, step
                assert isinstance(step.get("timeout_seconds", 1), int)
            assert isinstance(_step_command(step), str) and _step_command(step).strip(), key
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
    for step in _manifest()["install"]:
        assert _step_command(step).startswith("bash scripts/deploy/boss_stack.sh ")


def test_the_install_and_update_steps_outlast_a_full_image_build():
    """The engine's default step limit is 30 minutes; the install builds every image plus
    Playwright and takes longer on a small VM. A step killed mid-build strands the host."""
    data = _manifest()
    for key in ("install", "update"):
        installer = [s for s in data[key]
                     if re.search(r"boss_stack\.sh (install|update) ", _step_command(s))]
        assert installer, key
        for step in installer:
            assert isinstance(step, dict) and step["timeout_seconds"] >= 3600, (key, step)


def test_the_step_limits_are_ones_the_engine_accepts():
    try:
        from boss_engine.services import app_deploy_manifest
    except ImportError:
        pytest.skip("app_deploy_manifest is not on this branch yet")
    manifest = app_deploy_manifest.load_manifest(MANIFEST.read_text(encoding="utf-8"))
    assert manifest.install[0].timeout_seconds >= 3600
    assert manifest.update[0].timeout_seconds >= 3600


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


def _stub(bin_dir: Path, name: str, body: str) -> None:
    bin_dir.mkdir(parents=True, exist_ok=True)
    (bin_dir / name).write_text("#!/usr/bin/env bash\n" + body)
    (bin_dir / name).chmod(0o755)


def _docker(bin_dir: Path, *, compose_ls: str = "", volumes: tuple[str, ...] = ()) -> None:
    """A docker that answers only what boss_stack.sh asks: info, compose ls, volume inspect.
    The host running these tests may run a real BOSS; the script must never see it."""
    listing = "NAME STATUS CONFIG FILES\n" + compose_ls
    _stub(bin_dir, "docker",
          'case "$1 $2" in\n'
          '  "info "*) exit 0 ;;\n'
          f'  "compose ls") printf %b {listing!r} ;;\n'
          f'  "volume inspect") for v in {" ".join(volumes) or "__none__"}; do '
          '[ "$v" = "$3" ] && exit 0; done; exit 1 ;;\n'
          '  *) exit 0 ;;\n'
          'esac\n')


def _run(*args: str, bin_dir: Path | None = None, home: Path | None = None
         ) -> subprocess.CompletedProcess:
    if bin_dir is None:
        bin_dir = Path(tempfile.mkdtemp())
        _docker(bin_dir)
    home = home or Path(tempfile.mkdtemp())
    return subprocess.run(["bash", str(STACK), *args], capture_output=True, text=True,
                          env={**os.environ, "HOME": str(home),
                               "PATH": f"{bin_dir}:{os.environ['PATH']}"})


def _deploy(action: str, rel: Path, state: Path, **kw) -> subprocess.CompletedProcess:
    return _run(action, "--dir", str(rel), "--commit", "abc1234", "--state", str(state),
                "--origin", "https://boss.example.com", **kw)


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


# ----------------------------------------------------------- an install it did not make
def test_a_host_running_another_boss_install_is_refused(tmp_path):
    """The README install in ~/boss and a stack deployment share one compose project and its
    ports: deploying would recreate the existing install's containers from this tree, with
    fresh credentials its database volume rejects."""
    bin_dir = tmp_path / "bin"
    _docker(bin_dir, compose_ls="boss-community running(6) "
                                "/home/ops/boss/scripts/install/community-compose.yml\n")
    rel = _release(tmp_path / "stack", "aaa")

    result = _deploy("install", rel, tmp_path / "stack" / ".boss" / "boss", bin_dir=bin_dir)

    assert result.returncode == 3
    assert "already runs a BOSS install" in result.stderr and "/home/ops/boss" in result.stderr
    assert not (rel / "installer.args").exists()


def test_the_stacks_own_running_install_is_updated(tmp_path):
    stack = tmp_path / "stack"
    state = stack / ".boss" / "boss"
    assert _deploy("install", _release(stack, "aaa"), state).returncode == 0
    bin_dir = tmp_path / "bin"
    _docker(bin_dir, compose_ls=f"boss-community running(6) "
                                f"{stack}/releases/aaa/scripts/install/community-compose.yml\n",
            volumes=("boss-community_boss_community_pg",))
    second = _release(stack, "bbb")

    result = _deploy("update", second, state, bin_dir=bin_dir)

    assert result.returncode == 0, result.stderr
    assert (second / "installer.args").exists()


def test_a_leftover_database_volume_without_its_credentials_is_refused(tmp_path):
    bin_dir = tmp_path / "bin"
    _docker(bin_dir, volumes=("boss-community_boss_community_pg",))
    rel = _release(tmp_path / "stack", "aaa")

    result = _deploy("install", rel, tmp_path / "stack" / ".boss" / "boss", bin_dir=bin_dir)

    assert result.returncode == 3
    assert "database volume" in result.stderr
    assert not (rel / "installer.args").exists()


# --------------------------------------------------------------- the boss command
LAUNCHER_MARKER = "# BOSS Community command (generated by install.sh)"


def test_the_boss_command_follows_the_current_release_not_the_one_it_was_installed_from(
        tmp_path):
    """install.sh writes the launcher with the resolved dir, <dir>/releases/<sha>, and the
    deployer prunes old releases: the command would point at a deleted folder."""
    stack = tmp_path / "stack"
    rel = _release(stack, "aaa")
    text = (rel / "install.sh").read_text().replace(
        "exit 0\n",
        'mkdir -p "$HOME/.local/bin"\n'
        '{ echo "#!/usr/bin/env bash"; echo "' + LAUNCHER_MARKER + '"; '
        'printf "BOSS_INSTALL_DIR_DEFAULT=%q\\n" "$(pwd -P)"; echo "exec true"; } '
        '> "$HOME/.local/bin/boss"\n'
        "exit 0\n")
    (rel / "install.sh").write_text(text)
    current = stack / "current"
    current.symlink_to(rel)
    home = tmp_path / "home"

    result = _run("install", "--dir", str(current), "--commit", "abc1234",
                  "--state", str(stack / ".boss" / "boss"),
                  "--origin", "https://boss.example.com", home=home)

    assert result.returncode == 0, result.stderr
    launcher = (home / ".local" / "bin" / "boss").read_text().splitlines()
    assert launcher[1] == LAUNCHER_MARKER
    assert f"BOSS_INSTALL_DIR_DEFAULT={current}" in launcher
    assert os.access(home / ".local" / "bin" / "boss", os.X_OK)


def test_another_installs_boss_command_is_left_alone(tmp_path):
    stack = tmp_path / "stack"
    rel = _release(stack, "aaa")
    current = stack / "current"
    current.symlink_to(rel)
    home = tmp_path / "home"
    (home / ".local" / "bin").mkdir(parents=True)
    other = f"#!/usr/bin/env bash\n{LAUNCHER_MARKER}\nBOSS_INSTALL_DIR_DEFAULT=/home/ops/boss\n"
    (home / ".local" / "bin" / "boss").write_text(other)

    _run("install", "--dir", str(current), "--commit", "abc1234",
         "--state", str(stack / ".boss" / "boss"), "--origin", "https://boss.example.com",
         home=home)

    assert (home / ".local" / "bin" / "boss").read_text() == other


# ------------------------------------------------------------------------ Caddy
def _caddy_host(tmp_path: Path, *, candidate: str) -> tuple[Path, Path]:
    """A host without Caddy whose privileged commands are recorded, not run."""
    bin_dir = tmp_path / "bin"
    log = tmp_path / "sudo.log"
    _docker(bin_dir)
    _stub(bin_dir, "sudo", f'while [[ "$1" == -* ]]; do shift; done; echo "$*" >> {log}\n')
    _stub(bin_dir, "apt-get", "exit 0\n")
    _stub(bin_dir, "apt-cache", f'echo "caddy:"; echo "  Candidate: {candidate}"\n')
    _stub(bin_dir, "curl", "exit 0\n")
    return bin_dir, log


def test_caddy_is_installed_after_refreshing_the_package_lists(tmp_path):
    bin_dir, log = _caddy_host(tmp_path, candidate="2.6.2-5")

    result = _run("https", "--origin", "https://boss.example.com", bin_dir=bin_dir)

    calls = log.read_text().splitlines()
    assert result.returncode == 0, result.stderr
    update = calls.index("env DEBIAN_FRONTEND=noninteractive apt-get -y -qq update")
    assert calls[update + 1] == "env DEBIAN_FRONTEND=noninteractive apt-get -y -qq install caddy"
    assert not any("cloudsmith" in c or "caddy-stable" in c for c in calls)


def test_a_release_without_a_caddy_package_gets_caddys_own_repository(tmp_path):
    """Ubuntu 22.04's archive has no caddy package at all."""
    bin_dir, log = _caddy_host(tmp_path, candidate="(none)")

    result = _run("https", "--origin", "https://boss.example.com", bin_dir=bin_dir)

    calls = log.read_text().splitlines()
    assert result.returncode == 0, result.stderr
    repo = next(i for i, c in enumerate(calls) if c.startswith("tee ") and "caddy-stable" in c)
    install = calls.index("env DEBIAN_FRONTEND=noninteractive apt-get -y -qq install caddy")
    assert repo < install
    assert calls[install - 1] == "env DEBIAN_FRONTEND=noninteractive apt-get -y -qq update"
