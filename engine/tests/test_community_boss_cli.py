"""One-command install, the `boss` lifecycle command, and the remembered release.

Every test runs the real install.sh / upgrade.sh / boss.sh against local Git repositories
with inert Docker, curl, and provider CLIs on PATH. No daemon, network, or model call.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[2]
MARKER = re.compile(r"# >>> boss_pick_release\n(.*?)# <<< boss_pick_release", re.S)
PICKERS = (REPO / "install.sh", REPO / "scripts/install/upgrade.sh", REPO / "scripts/install/bootstrap.sh")
HELPERS = (
    "community-compose.yml", "restore_preserved_env.sh", "reconcile_api_credentials.py",
    "boss.sh", "upgrade.sh", "uninstall.sh", "discover_models.py", "coding_subscriptions.py",
    "console_password.py", "connect_ollama.py", "model_status.py", "local_model.py",
    "boss_refusal.py",
)
# What `docker compose config --format json` would say about the build services (install.sh
# builds them one image at a time from this answer). Kept equal to the real compose file below.
COMPOSE_BUILDS = {"db": None, "build-services": None, "api": None, "worker": None, "mcp-tools": None,
                  "egress": None, "web": ["web"]}
COMPOSE_CONFIG_JSON = json.dumps({"services": {
    name: {"build": {"context": "."}, **({"profiles": profiles} if profiles else {})}
    for name, profiles in COMPOSE_BUILDS.items()}})
SECRET = "sk-test-value-that-must-never-print"
CODEX_SECRET = "codex-auth-bytes-that-must-never-print"


def _run(*args: str | Path, cwd: Path | None = None, env: dict[str, str] | None = None,
         timeout: int = 60, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(arg) for arg in args], cwd=cwd, env=env, text=True, input=stdin,
        capture_output=True, check=False, timeout=timeout,
    )


def _git(*args: str | Path, cwd: Path) -> str:
    result = _run("git", "-c", "user.name=BOSS test", "-c", "user.email=boss-test@example.invalid",
                  *args, cwd=cwd)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _executable(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(0o755)


def test_the_fake_compose_config_names_the_real_build_services() -> None:
    import yaml

    services = yaml.safe_load((REPO / "scripts/install/community-compose.yml").read_text())["services"]
    assert COMPOSE_BUILDS == {name: s.get("profiles") for name, s in services.items() if "build" in s}


# ---- the tag rule --------------------------------------------------------------------------

def test_every_copy_of_the_release_picker_is_identical() -> None:
    blocks = {path.name + str(i): MARKER.search(path.read_text()).group(1).strip()
              for i, path in enumerate(PICKERS)}
    assert len(set(blocks.values())) == 1, blocks.keys()


def _pick(tags: list[str], include_pre: int) -> str:
    program = MARKER.search((REPO / "scripts/install/upgrade.sh").read_text()).group(1)
    script = f"pick() {{\n{program}\n}}\npick {include_pre}\n"
    result = _run("bash", "-c", script, stdin="\n".join(tags) + "\n")
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def test_release_picker_orders_versions_not_text() -> None:
    tags = ["v0.9.1", "v0.10.0", "v1.0-rc1", "v1.0-rc2", "not-a-tag", "v1.0.0.0", "vX"]
    assert _pick(tags, 0) == "v0.10.0"
    assert _pick(tags, 1) == "v1.0-rc2"
    assert _pick(tags + ["v1.0"], 1) == "v1.0"  # a final release outranks its prereleases
    assert _pick(["main", "feature"], 1) == ""


def test_release_candidate_numbers_compare_as_numbers() -> None:
    """The label was compared as text, so rc9 outranked rc10.4: a fresh install asking for the
    newest candidate got rc9 for as long as rc10.x was published."""
    published = ["v1.0-rc8", "v1.0-rc9", "v1.0-rc9-reconciled", "v1.0-rc10", "v1.0-rc10.1",
                 "v1.0-rc10.4"]
    assert _pick(published, 1) == "v1.0-rc10.4"
    assert _pick(["v1.0-rc10.9", "v1.0-rc10.10"], 1) == "v1.0-rc10.10"
    assert _pick(["v1.0-rc2", "v1.0-rc10"], 1) == "v1.0-rc10"
    assert _pick(["v1.0-rc9-reconciled"], 1) == ""   # a second hyphen is not a release ref
    assert _pick(published + ["v1.0"], 1) == "v1.0"
    assert _pick(published, 0) == ""          # stable only: no candidate is offered


# ---- fixture: a published repository with release tags ------------------------------------

def _publish(upstream: Path) -> None:
    helpers = upstream / "scripts/install"
    helpers.mkdir(parents=True)
    for name in HELPERS:
        shutil.copy2(REPO / "scripts/install" / name, helpers / name)
    shutil.copy2(REPO / "scripts/store_subscription_token.py", upstream / "scripts")
    shutil.copy2(REPO / "install.sh", upstream / "install.sh")
    _executable(
        helpers / "backup.sh",
        "#!/usr/bin/env bash\nset -euo pipefail\n"
        "dir=$2\n"
        "git -C \"$dir\" rev-parse HEAD >> \"$BOSS_TEST_BACKUP_TRACE\"\n"
        "out=\"$BOSS_TEST_BACKUP_ROOT/$(git -C \"$dir\" rev-parse --short HEAD)\"\n"
        "mkdir -p \"$out\"\n"
        "cp \"$dir/.env\" \"$out/.env\"\n"
        "git -C \"$dir\" rev-parse HEAD > \"$out/source-commit\"\n"
        "printf 'db\\n' > \"$out/boss.dump\"\nprintf 'art\\n' > \"$out/artifacts.tar\"\n"
        "printf '%s\\n' \"$out\"\n",
    )
    # The archive check, inert like backup.sh above (the real format has its own contract test,
    # test_recovery_archive_contract): it records what it validated and accepts the fake backup.
    _executable(
        helpers / "backup_archive.py",
        "#!/usr/bin/env python3\nimport os, pathlib, sys\n"
        "with open(os.environ['BOSS_TEST_BACKUP_TRACE'] + '.validated', 'a') as f:\n"
        "    f.write(' '.join(sys.argv[1:]) + '\\n')\n"
        "sys.exit(0 if sys.argv[1] == 'validate' and not os.environ.get('BOSS_TEST_ARCHIVE_REFUSE') and "
        "(pathlib.Path(sys.argv[2]) / 'source-commit').is_file() else 1)\n",
    )
    (upstream / "version").write_text("1.0.0\n")
    _git("init", "-q", "-b", "main", cwd=upstream)
    _git("add", ".", cwd=upstream)
    _git("commit", "-qm", "v1.0.0", cwd=upstream)
    _git("tag", "-a", "v1.0.0", "-m", "release 1.0.0", cwd=upstream)


def _release(upstream: Path, version: str, *, tag: bool = True) -> str:
    (upstream / "version").write_text(version + "\n")
    _git("commit", "-qam", version, cwd=upstream)
    if tag:
        _git("tag", "-a", f"v{version}", "-m", f"release {version}", cwd=upstream)
    return _git("rev-parse", "HEAD", cwd=upstream)


@pytest.fixture
def world(tmp_path: Path) -> dict:
    upstream = tmp_path / "upstream"
    _publish(upstream)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _executable(
        bin_dir / "docker",
        "#!/bin/sh\n"
        "printf '%s\\n' \"$*\" >> \"$BOSS_TEST_DOCKER_LOG\"\n"
        # The image tag Compose would resolve, beside the call that would resolve it.
        "[ \"$1\" = compose ] && printf '%s %s\\n' \"${BOSS_COMMUNITY_IMAGE_TAG:-local}\" \"$*\" "
        ">> \"$BOSS_TEST_DOCKER_LOG.tags\"\n"
        "case \"$1:$2\" in compose:version) echo 'Docker Compose version v2' ;; esac\n"
        f"case \"$*\" in compose*'config --format json'*) echo '{COMPOSE_CONFIG_JSON}' ;; esac\n"
        # The container-side checks and logins the connect paths run.
        "case \"$*\" in\n"
        "  *'network inspect bridge'*) echo 127.0.0.1 ;;\n"
        "  *host.docker.internal*)\n"
        "    [ \"${BOSS_TEST_OLLAMA_UNREACHABLE:-0}\" = 1 ] && exit 1\n"
        "    [ -n \"${BOSS_TEST_STATE:-}\" ] && [ -e \"$BOSS_TEST_STATE/ollama-loopback-only\" ] && exit 1 ;;\n"
        "  *'identity.cli claim-status'*) [ -n \"${BOSS_TEST_CLAIM:-}\" ] && echo \"$BOSS_TEST_CLAIM\" ;;\n"
        "  *'claude auth login'*) [ -n \"${BOSS_TEST_STATE:-}\" ] && touch \"$BOSS_TEST_STATE/claude\" ;;\n"
        "  *'grok login --device-auth'*) [ -n \"${BOSS_TEST_STATE:-}\" ] && touch \"$BOSS_TEST_STATE/grok\" ;;\n"
        "esac\n"
        "exit 0\n",
    )
    _executable(
        bin_dir / "curl",
        "#!/bin/sh\nout=; code=; url=\n"
        "while [ \"$#\" -gt 0 ]; do\n"
        "  if [ \"$1\" = -o ]; then out=\"$2\"; shift 2; continue; fi\n"
        "  if [ \"$1\" = -w ]; then code=200; shift 2; continue; fi\n"
        "  case \"$1\" in http://*|https://*) url=\"$1\" ;; esac\n"
        "  shift\ndone\n"
        "printf '%s\\n' \"$url\" >> \"$BOSS_TEST_CURL_LOG\"\n"
        # The official Ollama installer is never downloaded: a fixture stands in for it.
        "case \"$url\" in *ollama.com*)\n"
        "  [ -n \"${BOSS_TEST_OLLAMA_INSTALLER:-}\" ] || exit 22\n"
        "  cp \"$BOSS_TEST_OLLAMA_INSTALLER\" \"$out\"; exit 0 ;;\nesac\n"
        "if [ -n \"$out\" ] && [ \"$out\" != /dev/null ]; then printf '{\"edition\":\"community\"}' > \"$out\"; fi\n"
        "if [ -n \"$code\" ]; then printf '%s' \"$code\"; fi\n",
    )
    # Free disk space, as df -Pk prints it.
    _executable(bin_dir / "df",
                "#!/bin/sh\necho 'Filesystem 1024-blocks Used Available Capacity Mounted on'\n"
                "echo \"/dev/test 999999999 1 ${BOSS_TEST_FREE_KB:-900000000} 1% /\"\n")
    # uname -s answers BOSS_TEST_UNAME when a test sets it, so both host paths run anywhere.
    _executable(bin_dir / "uname",
                "#!/bin/sh\nif [ -n \"${BOSS_TEST_UNAME:-}\" ] && [ \"$1\" = -s ]; then\n"
                "  echo \"$BOSS_TEST_UNAME\"; exit 0\nfi\nexec /usr/bin/uname \"$@\"\n")
    # Provider CLIs: not signed in unless the test says so. Status output only.
    _executable(bin_dir / "codex",
                "#!/bin/sh\n[ \"$1 $2\" = 'login status' ] || exit 0\n"
                "if [ \"${BOSS_TEST_CODEX_LOGIN:-0}\" = 1 ]; then echo 'Logged in using ChatGPT'; exit 0; fi\n"
                "echo 'Not logged in'; exit 1\n")
    _executable(bin_dir / "claude",
                "#!/bin/sh\n"
                "if [ \"${BOSS_TEST_CLAUDE_LOGIN:-0}\" = 1 ]; then\n"
                "  echo '{\"loggedIn\": true, \"authMethod\": \"claude_ai\"}'; exit 0\nfi\n"
                "echo '{\"loggedIn\": false}'\n")
    _executable(bin_dir / "grok", "#!/bin/sh\nexit 0\n")
    home = tmp_path / "home"
    home.mkdir()
    env = {
        key: value for key, value in os.environ.items()
        if not key.startswith(("BOSS_", "OPENAI_", "ANTHROPIC_", "GOOGLE_", "XAI_", "OPENROUTER_",
                               "CODEX_", "CLAUDE_"))
    }
    # Never reach a real Ollama or Homebrew on the machine running the tests. A directory that
    # holds either is dropped from PATH; the tools the scripts need from it are linked back.
    tools = tmp_path / "tools"
    tools.mkdir()
    for tool in ("python3", "git", "bash"):
        found = shutil.which(tool)
        if found:
            (tools / tool).symlink_to(Path(found).resolve())
    path = [part for part in os.environ["PATH"].split(os.pathsep)
            if part and not any((Path(part) / tool).exists() for tool in ("ollama", "brew"))]
    env.update(
        HOME=str(home),
        PATH=os.pathsep.join([str(bin_dir), str(tools), *path]),
        BOSS_TEST_BIN=str(bin_dir),
        BOSS_TEST_CURL_LOG=str(tmp_path / "curl-urls.log"),
        BOSS_OLLAMA_REGISTRY="http://127.0.0.1:9",
        BOSS_OLLAMA_DROPIN_DIR=str(tmp_path / "ollama.service.d"),
        BOSS_REPO_URL=str(upstream),
        BOSS_BIN_DIR=str(tmp_path / "userbin"),
        BOSS_OLLAMA_URL="http://127.0.0.1:9",
        BOSS_API_PORT="59731",
        BOSS_TEST_DOCKER_LOG=str(tmp_path / "docker.log"),
        BOSS_TEST_BACKUP_TRACE=str(tmp_path / "backup-trace"),
        BOSS_TEST_BACKUP_ROOT=str(tmp_path / "backups"),
    )
    return {"tmp": tmp_path, "upstream": upstream, "install": tmp_path / "boss", "env": env,
            "home": home, "boss": tmp_path / "userbin/boss"}


def _install(world: dict, *extra: str, env: dict | None = None) -> subprocess.CompletedProcess[str]:
    return _run("bash", REPO / "install.sh", "--dir", world["install"], "--yes",
                "--no-install-deps", "--no-web", *extra, env=env or world["env"])


def _state(install: Path) -> dict[str, str]:
    return dict(line.split("=", 1) for line in (install / ".boss-release").read_text().splitlines()
                if "=" in line and not line.startswith("#"))


# ---- one command, newest release, remembered -----------------------------------------------

def test_fresh_install_needs_no_answers_and_picks_the_newest_stable_release(world) -> None:
    upstream = world["upstream"]
    stable = _release(upstream, "1.1.0")
    _release(upstream, "1.2.0-rc1")
    _release(upstream, "1.2.1-dev", tag=False)  # main moves past every release

    result = _install(world)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "Release: v1.1.0 (newest published)" in result.stdout
    assert _git("rev-parse", "HEAD", cwd=world["install"]) == stable
    state = _state(world["install"])
    assert state["BOSS_RELEASE_REF"] == "v1.1.0"
    assert state["BOSS_RELEASE_CHANNEL"] == "release"
    assert state["BOSS_INSTALLED_COMMIT"] == stable
    assert "BOSS Community is running" in result.stdout
    assert "v1.1.0 (release; reruns and upgrades stay on it)" in result.stdout


def test_rerun_without_ref_stays_on_the_recorded_release_not_main(world) -> None:
    upstream = world["upstream"]
    first = _install(world)
    assert first.returncode == 0, first.stdout + first.stderr
    v100 = _git("rev-parse", "HEAD", cwd=world["install"])
    _release(upstream, "1.0.1-dev", tag=False)

    rerun = _install(world)

    assert rerun.returncode == 0, rerun.stdout + rerun.stderr
    assert "kept from the last install" in rerun.stdout
    assert _git("rev-parse", "HEAD", cwd=world["install"]) == v100
    assert _state(world["install"])["BOSS_RELEASE_REF"] == "v1.0.0"
    assert not Path(world["env"]["BOSS_TEST_BACKUP_TRACE"]).exists()


def test_an_older_release_install_without_a_record_stays_on_its_tag(world) -> None:
    upstream = world["upstream"]
    v100 = _git("rev-parse", "HEAD", cwd=upstream)
    _release(upstream, "1.0.1-dev", tag=False)
    install = world["install"]
    _git("clone", "-q", "--no-checkout", upstream, install, cwd=world["tmp"])
    _git("checkout", "-q", "--detach", "v1.0.0", cwd=install)
    (install / ".env").write_text("BOSS_API_KEY=legacy-owner\nBOSS_DB_PASSWORD=legacy-db\n")

    result = _install(world)

    assert result.returncode == 0, result.stdout + result.stderr
    assert _git("rev-parse", "HEAD", cwd=install) == v100
    assert _state(install)["BOSS_RELEASE_REF"] == "v1.0.0"
    assert "legacy-owner" not in result.stdout + result.stderr


def test_explicit_ref_is_recorded_and_bad_refs_are_refused_before_any_action(world) -> None:
    result = _install(world, "--ref", "main")
    assert result.returncode == 0, result.stdout + result.stderr
    assert _state(world["install"])["BOSS_RELEASE_REF"] == "main"
    assert _state(world["install"])["BOSS_RELEASE_CHANNEL"] == "branch"

    refused = _run("bash", REPO / "install.sh", "--ref", "--upload-pack=touch", env=world["env"])
    assert refused.returncode != 0
    assert "--ref must be" in refused.stderr
    assert "Checking your machine" not in refused.stdout


# ---- the boss command ---------------------------------------------------------------------

def test_install_puts_boss_on_path_pointing_at_this_install(world) -> None:
    result = _install(world)
    assert result.returncode == 0, result.stdout + result.stderr
    boss = world["boss"]
    assert boss.exists() and os.access(boss, os.X_OK)
    assert "BOSS Community command (generated by install.sh)" in boss.read_text()
    assert "open" in result.stdout and "doctor" in result.stdout

    # From any directory.
    version = _run(boss, "version", cwd=world["tmp"], env=world["env"])
    assert version.returncode == 0, version.stderr
    assert str(world["install"].resolve()) in version.stdout
    assert "v1.0.0 (release channel" in version.stdout


def test_another_programs_boss_is_never_replaced(world) -> None:
    world["boss"].parent.mkdir(parents=True)
    _executable(world["boss"], "#!/bin/sh\necho someone-elses-boss\n")
    result = _install(world)
    assert result.returncode == 0, result.stdout + result.stderr
    assert world["boss"].read_text() == "#!/bin/sh\necho someone-elses-boss\n"
    assert "belongs to another program" in result.stdout


def _installed(world) -> Path:
    result = _install(world)
    assert result.returncode == 0, result.stdout + result.stderr
    return world["boss"]


def test_boss_open_status_and_logs(world) -> None:
    boss = _installed(world)
    env = world["env"]
    (world["install"] / ".env").write_text(
        (world["install"] / ".env").read_text().replace(
            "BOSS_COMMUNITY_WEB_ENABLED=0", "BOSS_COMMUNITY_WEB_ENABLED=1"))
    opened = _run(boss, "open", env=env)
    assert opened.returncode == 0, opened.stderr
    assert "http://127.0.0.1:3000" in opened.stdout
    assert "boss password" in opened.stdout

    # NOT CLAIMED: `boss open` says how, with the command — nobody has to find the link in a log.
    unclaimed = _run(boss, "open", env={**env, "BOSS_TEST_CLAIM": "unclaimed"})
    assert unclaimed.returncode == 0, unclaimed.stderr
    assert "Not claimed yet" in unclaimed.stdout and "  boss identity setup-link" in unclaimed.stdout

    # BOSSAPP-813: with an https console origin, that address IS the claim from the server's own
    # network; the setup link is for everywhere else.
    dotenv = world["install"] / ".env"
    before = dotenv.read_text()
    dotenv.write_text("".join(line for line in before.splitlines(keepends=True)
                              if not line.startswith("BOSS_CONSOLE_PUBLIC_ORIGIN="))
                      + "\nBOSS_CONSOLE_PUBLIC_ORIGIN=https://boss-1.example.ts.net\n")
    onsite = _run(boss, "open", env={**env, "BOSS_TEST_CLAIM": "unclaimed"})
    dotenv.write_text(before)
    assert onsite.returncode == 0, onsite.stderr
    assert "Open https://boss-1.example.ts.net to claim this BOSS (from your own network)" in onsite.stdout
    assert "from elsewhere run, on this server: boss identity setup-link" in onsite.stdout

    status = _run(boss, "status", env=env)
    assert status.returncode == 0, status.stdout + status.stderr
    assert "healthy at http://127.0.0.1:59731" in status.stdout

    Path(env["BOSS_TEST_DOCKER_LOG"]).write_text("")
    logs = _run(boss, "logs", "api", env=env)
    assert logs.returncode == 0, logs.stderr
    calls = Path(env["BOSS_TEST_DOCKER_LOG"]).read_text().splitlines()
    assert any(call.endswith("logs --tail 200 api") for call in calls)
    refused = _run(boss, "logs", "api;rm", env=env)
    assert refused.returncode != 0

    owner = next(line.split("=", 1)[1] for line in
                 (world["install"] / ".env").read_text().splitlines() if line.startswith("BOSS_API_KEY="))
    for result in (opened, status, logs):
        assert owner not in result.stdout + result.stderr


def test_boss_password_refuses_to_print_outside_a_terminal(world) -> None:
    boss = _installed(world)
    result = _run(boss, "password", env=world["env"])
    assert result.returncode != 0
    assert "outside a terminal" in result.stdout + result.stderr
    console = next(line.split("=", 1)[1] for line in
                   (world["install"] / ".env").read_text().splitlines()
                   if line.startswith("BOSS_CONSOLE_PASSWORD="))
    assert console not in result.stdout + result.stderr


def test_boss_uninstall_keeps_data_and_purge_needs_typed_confirmation(world) -> None:
    boss = _installed(world)
    env = world["env"]
    log = Path(env["BOSS_TEST_DOCKER_LOG"])
    log.write_text("")

    kept = _run(boss, "uninstall", "--yes", env=env)
    assert kept.returncode == 0, kept.stderr
    calls = log.read_text()
    assert " stop" in calls and " down" not in calls
    assert (world["install"] / ".env").exists()

    purge = _run(boss, "uninstall", "--purge", "--yes", env=env)
    assert purge.returncode != 0
    assert "nothing was removed" in purge.stderr
    assert (world["install"] / ".env").exists()
    assert "--volumes" not in log.read_text()


def test_boss_update_reapplies_config_on_the_same_release(world) -> None:
    boss = _installed(world)
    env = dict(world["env"], XAI_API_KEY=SECRET)
    head = _git("rev-parse", "HEAD", cwd=world["install"])
    _release(world["upstream"], "1.0.1-dev", tag=False)

    result = _run(boss, "update", env=env, timeout=90)

    assert result.returncode == 0, result.stdout + result.stderr
    assert _git("rev-parse", "HEAD", cwd=world["install"]) == head
    assert f"XAI_API_KEY={SECRET}" in (world["install"] / ".env").read_text()
    assert SECRET not in result.stdout + result.stderr
    assert "XAI_API_KEY" in result.stdout

    moved = _run(boss, "update", "--ref", "main", env=env)
    assert moved.returncode != 0
    assert "boss upgrade --to" in moved.stderr


def test_boss_upgrade_moves_to_the_newest_release_and_records_it(world) -> None:
    boss = _installed(world)
    env = world["env"]
    old = _git("rev-parse", "HEAD", cwd=world["install"])
    _release(world["upstream"], "1.1.0-rc1")      # a stable install skips prereleases
    target = _release(world["upstream"], "1.1.0")
    _release(world["upstream"], "1.2.0-dev", tag=False)

    dry = _run(boss, "upgrade", "--dry-run", env=env)
    assert dry.returncode == 0, dry.stdout + dry.stderr
    assert "Newest release: v1.1.0" in dry.stdout
    assert _git("rev-parse", "HEAD", cwd=world["install"]) == old

    result = _run(boss, "upgrade", env=env, timeout=120)

    assert result.returncode == 0, result.stdout + result.stderr
    assert _git("rev-parse", "HEAD", cwd=world["install"]) == target
    assert Path(env["BOSS_TEST_BACKUP_TRACE"]).read_text().split() == [old]
    state = _state(world["install"])
    assert state["BOSS_RELEASE_REF"] == "v1.1.0"
    assert state["BOSS_RELEASE_CHANNEL"] == "release"

    again = _run(boss, "upgrade", "--dry-run", env=env)
    assert again.returncode == 0, again.stderr
    assert "Already on the newest release, v1.1.0" in again.stdout


def test_boss_upgrade_of_a_pinned_install_needs_an_explicit_target(world) -> None:
    upstream = world["upstream"]
    pinned = _release(upstream, "1.0.1-dev", tag=False)
    result = _install(world, "--ref", pinned)
    assert result.returncode == 0, result.stdout + result.stderr
    assert _state(world["install"])["BOSS_RELEASE_CHANNEL"] == "pinned"
    _release(upstream, "1.1.0")

    refused = _run(world["boss"], "upgrade", env=world["env"])
    assert refused.returncode != 0
    assert "pinned to commit" in refused.stderr
    assert _git("rev-parse", "HEAD", cwd=world["install"]) == pinned

    moved = _run(world["boss"], "upgrade", "--to", "v1.1.0", "--dry-run", env=world["env"])
    assert moved.returncode == 0, moved.stderr
    assert "from v1.1.0" in moved.stdout


def test_a_failed_upgrade_puts_the_previous_release_back(world) -> None:
    """A release that cannot start on this host (an API that refuses to boot) must never leave
    BOSS down (audit U6): upgrade.sh puts the previous source back and starts it again."""
    boss = _installed(world)
    env = world["env"]
    old = _git("rev-parse", "HEAD", cwd=world["install"])
    upstream = world["upstream"]
    installer = upstream / "install.sh"
    installer.write_text(installer.read_text().replace(
        "set -euo pipefail",
        # moves the source (as the real installer does), then fails to start
        "set -euo pipefail\ngit -C \"$2\" merge -q --ff-only \"$4\"\n"
        "echo 'this release cannot start here' >&2; exit 1", 1))
    _git("commit", "-qam", "broken", cwd=upstream)
    _release(upstream, "1.1.0")

    result = _run(boss, "upgrade", env=env, timeout=180)

    assert result.returncode != 0
    assert "Putting back the previous release" in result.stderr
    assert "Rolled back" in result.stderr, result.stdout + result.stderr
    assert _git("rev-parse", "HEAD", cwd=world["install"]) == old
    assert _state(world["install"])["BOSS_RELEASE_REF"] == "v1.0.0"


# ---- named accounts from the host shell ------------------------------------------------------

def test_boss_identity_runs_the_identity_cli_as_the_boss_user(world) -> None:
    boss = _installed(world)
    Path(world["env"]["BOSS_TEST_DOCKER_LOG"]).write_text("")
    result = _run(boss, "identity", "setup-link", env=world["env"])
    assert result.returncode == 0, result.stdout + result.stderr
    assert "exec -T -u boss api python -m boss_engine.identity.cli setup-link" in _docker_calls(world)
    # the commands that print a credential are never run from here
    for refused in ("mcp-key", "egress-key"):
        blocked = _run(boss, "identity", refused, env=world["env"])
        assert blocked.returncode != 0 and "prints a credential" in blocked.stderr


def test_boss_identity_host_key_keeps_the_key_in_env_and_never_prints_it(world) -> None:
    boss = _installed(world)
    key = "boss_sak_" + "Q" * 43
    real = Path(world["env"]["BOSS_TEST_BIN"]) / "docker"
    wrapper = world["tmp"] / "docker-with-key"
    _executable(wrapper, "#!/bin/sh\ncase \"$*\" in *identity.cli*host-key*) echo '%s' ;; esac\n"
                         "exec '%s.real' \"$@\"\n" % (key, real))
    real.rename(str(real) + ".real")
    wrapper.rename(real)
    result = _run(boss, "identity", "host-key", env=world["env"])
    assert result.returncode == 0, result.stdout + result.stderr
    assert key not in result.stdout + result.stderr
    env_lines = (world["install"] / ".env").read_text().splitlines()
    assert f"BOSS_HOST_API_KEY={key}" in env_lines
    assert oct((world["install"] / ".env").stat().st_mode & 0o777) == "0o600"


def test_boss_metatron_runs_natively_with_the_host_key_never_printed(world) -> None:
    """`boss metatron` runs the terminal on the host (not through docker compose exec), against
    the local API, with the host tools' key passed through the environment."""
    import sys

    sys.path.insert(0, str(REPO / "scripts" / "tests"))
    from metatron_fake_api import FakeBoss

    boss = _installed(world)
    shutil.copy2(REPO / "scripts/metatron_tui.py", world["install"] / "scripts")
    key = "boss_sak_" + "M" * 43
    usage = _run(boss, "help", env=world["env"])
    assert "metatron" in usage.stdout
    with FakeBoss() as api:
        _set_env_line(world["install"], "BOSS_API_PORT", api.url.rsplit(":", 1)[1])
        _set_env_line(world["install"], "BOSS_HOST_API_KEY", key)
        Path(world["env"]["BOSS_TEST_DOCKER_LOG"]).write_text("")
        env = dict(world["env"], BOSS_METATRON_NO_INSTALL="1")
        result = _run(boss, "metatron", "--print", "/tasks", "--json", env=env)
        keys = set(api.keys)
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["items"][0]["id"] == "task_1"
    assert keys == {key}
    assert key not in result.stdout + result.stderr
    assert "metatron" not in _docker_calls(world)


def test_host_tools_never_put_a_key_on_a_command_line(world) -> None:
    """The host tools send their key through stdin or the environment: argv is visible to every
    user of the host (ps)."""
    text = (REPO / "scripts/install/boss.sh").read_text()
    assert '-H "Authorization: Bearer $key"' not in text
    assert "-H @-" in text


# ---- models and subscriptions already on this machine --------------------------------------

def _codex_login(world) -> None:
    world["env"]["BOSS_TEST_CODEX_LOGIN"] = "1"
    codex_home = world["home"] / ".codex"
    codex_home.mkdir()
    auth = codex_home / "auth.json"
    auth.write_text('{"token": "%s"}' % CODEX_SECRET)
    auth.chmod(0o600)


def test_exported_keys_are_saved_by_name_and_never_printed(world) -> None:
    env = dict(world["env"], GOOGLE_API_KEY=SECRET + "-g", OPENROUTER_API_KEY=SECRET + "-o")
    result = _install(world, env=env)
    assert result.returncode == 0, result.stdout + result.stderr
    text = (world["install"] / ".env").read_text()
    assert f"GOOGLE_API_KEY={SECRET}-g" in text
    assert f"OPENROUTER_API_KEY={SECRET}-o" in text
    rendered = result.stdout + result.stderr
    assert SECRET not in rendered
    assert "GOOGLE_API_KEY" in rendered and "OPENROUTER_API_KEY" in rendered

    # A rerun without the export keeps them.
    rerun = _install(world)
    assert rerun.returncode == 0, rerun.stdout + rerun.stderr
    assert f"GOOGLE_API_KEY={SECRET}-g" in (world["install"] / ".env").read_text()


def test_yes_reports_a_found_login_without_copying_it(world) -> None:
    _codex_login(world)
    result = _install(world)
    assert result.returncode == 0, result.stdout + result.stderr
    rendered = result.stdout + result.stderr
    assert "found      Codex ChatGPT login on this machine" in rendered
    assert "found, not connected: boss connect codex" in rendered
    assert "Connecting the Codex ChatGPT login" not in rendered
    assert "not found  Ollama" in rendered
    assert CODEX_SECRET not in rendered


def test_connect_found_never_copies_a_host_codex_login(world) -> None:
    """A Codex login on this machine only says which subscription you use. Nothing is copied
    from it (BOSSAPP-769): Codex signs in inside BOSS, which needs a person at a terminal."""
    _codex_login(world)
    result = _install(world, "--connect-found")
    rendered = result.stdout + result.stderr
    assert result.returncode == 0, rendered
    assert "Codex: signing in needs a person at a terminal. Run: boss connect codex" in rendered
    assert "accept-database-fallback" not in rendered
    assert "Connecting the Codex ChatGPT login" not in rendered
    assert CODEX_SECRET not in rendered


def test_an_old_accept_database_fallback_flag_is_ignored_not_fatal(world) -> None:
    result = _install(world, "--accept-database-fallback")
    rendered = result.stdout + result.stderr
    assert result.returncode == 0, rendered
    assert "Ignoring --accept-database-fallback" in rendered


def test_boss_connect_without_a_terminal_copies_nothing(world) -> None:
    boss = _installed(world)
    _codex_login(world)
    result = _run(boss, "connect", env=world["env"])
    assert result.returncode == 0, result.stdout + result.stderr
    assert "found      Codex ChatGPT login" in result.stdout
    assert "Codex: signing in needs a person at a terminal. Run: boss connect codex" in result.stdout
    assert CODEX_SECRET not in result.stdout + result.stderr


def test_ollama_is_found_on_loopback_only(world, tmp_path: Path) -> None:
    import http.server
    import threading

    class Tags(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            body = b'{"models": [{"name": "llama3"}]}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Tags)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        env = dict(world["env"], BOSS_OLLAMA_URL=f"http://127.0.0.1:{server.server_port}")
        summary = tmp_path / "summary"
        found = _run("python3", REPO / "scripts/install/discover_models.py", "--summary", summary, env=env)
        assert found.returncode == 0, found.stderr
        assert "found      Ollama" in found.stdout and "(1 model pulled: llama3)" in found.stdout
        assert "ollama=found" in summary.read_text()
        assert oct(summary.stat().st_mode & 0o777) == "0o600"

        remote = dict(env, BOSS_OLLAMA_URL=f"http://example.invalid:{server.server_port}")
        skipped = _run("python3", REPO / "scripts/install/discover_models.py", "--summary", summary,
                       env=remote)
        assert "ollama=missing" in summary.read_text(), skipped.stdout
    finally:
        server.shutdown()


# ---- the website bootstrap -----------------------------------------------------------------

def _bootstrap_world(tmp_path: Path, tags: str, *, download_fails: bool = False) -> dict[str, str]:
    bin_dir = tmp_path / "bootbin"
    bin_dir.mkdir()
    _executable(bin_dir / "git",
                "#!/bin/sh\n[ \"$1\" = ls-remote ] || exit 2\n"
                f"printf '%s' '{tags}' | while read -r t; do [ -n \"$t\" ] && printf '0000\\trefs/tags/%s\\n' \"$t\"; done\n")
    fake_installer = tmp_path / "fake-install.sh"
    fake_installer.write_text(
        "#!/usr/bin/env bash\n# BOSS Community installer (fixture)\n"
        "printf 'ref=%s args=%s\\n' \"$BOSS_REPO_REF\" \"$*\" > \"$BOSS_TEST_RAN\"\n")
    _executable(bin_dir / "curl",
                "#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$BOSS_TEST_CURL\"\n"
                + ("exit 22\n" if download_fails else "")
                + "while [ \"$#\" -gt 0 ]; do\n"
                "  if [ \"$1\" = -o ]; then cp \"$BOSS_TEST_INSTALLER\" \"$2\"; exit 0; fi\n"
                "  shift\ndone\nexit 2\n")
    return {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "BOSS_TEST_INSTALLER": str(fake_installer),
        "BOSS_TEST_RAN": str(tmp_path / "ran"),
        "BOSS_TEST_CURL": str(tmp_path / "curl.log"),
    }


def test_bootstrap_runs_the_newest_releases_installer_with_that_release(tmp_path: Path) -> None:
    env = _bootstrap_world(tmp_path, "v0.9.1\nv1.0-rc1\nv0.10.0\n")
    env.pop("BOSS_REF", None)
    result = _run("bash", "-s", "--", "--yes", stdin=(REPO / "scripts/install/bootstrap.sh").read_text(),
                  env=env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "raw.githubusercontent.com/seanventures/boss/v0.10.0/install.sh" in (tmp_path / "curl.log").read_text()
    assert (tmp_path / "ran").read_text().strip() == "ref=v0.10.0 args=--yes"


def test_bootstrap_honours_an_explicit_ref_and_stops_on_a_failed_download(tmp_path: Path) -> None:
    env = _bootstrap_world(tmp_path, "v1.0-rc1\n")
    pinned = _run("bash", REPO / "scripts/install/bootstrap.sh", "--ref", "v1.0-rc1", "--no-web", env=env)
    assert pinned.returncode == 0, pinned.stderr
    assert (tmp_path / "ran").read_text().strip() == "ref=v1.0-rc1 args=--no-web"

    only_pre = _run("bash", REPO / "scripts/install/bootstrap.sh", env=env)
    assert only_pre.returncode == 0, only_pre.stderr
    assert (tmp_path / "ran").read_text().startswith("ref=v1.0-rc1")

    bad = _run("bash", REPO / "scripts/install/bootstrap.sh", "--ref", "-x", env=env)
    assert bad.returncode != 0

    failing_dir = tmp_path / "failing"
    failing_dir.mkdir()
    failing = _bootstrap_world(failing_dir, "v1.0-rc1\n", download_fails=True)
    failed = _run("bash", REPO / "scripts/install/bootstrap.sh", env=failing)
    assert failed.returncode != 0
    assert "nothing was installed" in failed.stderr
    assert not (failing_dir / "ran").exists()


# ---- phase 2: in-container sign-in, Ollama, keys from `boss connect`, purge -----------------

class _FakeBoss:
    """BOSS API, Ollama, and the Ollama registry, stand-ins on one loopback port.

    Records every request. Ollama answers with `models` unless `local` is set; then it answers
    only while the state directory says it is running, with the models pulled there.
    """

    ROLE_KEYS = ("code_agent_engine", "code_agent_model", "code_review_engine",
                 "code_review_model", "code_agent_strong_engine", "code_agent_strong_model",
                 "code_agent_low_cost_engine", "code_agent_low_cost_model")
    SUBSCRIPTION_SOURCES = {"claude_code": "anthropic_claude_code", "grok": "xai_grok_build",
                            "codex": "openai_codex_chatgpt"}
    #: The container-side login each sign-in leaves behind (read by GET coding-agents).
    STATE_FILE = {"claude_code": "claude", "grok": "grok", "codex": "codex"}

    def __init__(self, state: Path, models: tuple[str, ...] = ("llama3.2:latest",)) -> None:
        import http.server
        import json
        import threading

        self.state = state
        self.models = list(models)
        self.local = False
        self.model_bytes = 4_683_000_000
        self.sources: list[dict] = []
        self.settings: dict[str, dict] = {
            key: {"key": key, "value": "claude_code" if key.endswith("_engine") else "",
                  "source": "deployment default"} for key in self.ROLE_KEYS}
        self.requests: list[tuple[str, str, dict | None]] = []
        self.sign_ins: dict[str, dict] = {}
        self.sign_in_fails = False
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def _reply(self, code: int, body: dict) -> None:
                data = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _body(self) -> dict:
                return json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")

            def do_GET(self):  # noqa: N802
                outer.requests.append(("GET", self.path, None))
                if self.path.startswith("/api/tags"):
                    names = outer.ollama_names()
                    if names is None:
                        return self._reply(503, {})
                    return self._reply(200, {"models": [{"name": n} for n in names]})
                if self.path.startswith("/v2/") and "/manifests/" in self.path:
                    return self._reply(200, {"config": {"size": 1000},
                                             "layers": [{"size": outer.model_bytes}]})
                if self.path.startswith("/health"):
                    return self._reply(200, {"status": "ok"})
                if self.path.startswith("/v1/setup/coding-agents"):
                    claude = (outer.state / "claude").exists()
                    return self._reply(200, {"agents": [
                        {"name": "claude_code", "installed": True, "token_present": False,
                         "authenticated": claude, "auth_method": "claude_ai" if claude else None},
                        {"name": "grok", "installed": True,
                         "authenticated": (outer.state / "grok").exists()},
                        {"name": "codex", "installed": True,
                         "authenticated": (outer.state / "codex").exists()},
                    ]})
                if self.path.startswith("/v1/setup/sign-in/"):
                    # The person finished the provider's own login in their browser.
                    session = outer.sign_ins[self.path.split("/")[4].split("?")[0]]
                    if outer.sign_in_fails:
                        return self._reply(200, {**session, "state": "failed",
                                                 "headline": "Sign-in failed",
                                                 "fix": "The login stopped. Start it again."})
                    (outer.state / outer.STATE_FILE[session["provider"]]).touch()
                    return self._reply(200, {**session, "state": "signed_in"})
                if self.path.startswith("/v1/entitlements/sources"):
                    return self._reply(200, {"project_id": "boss-dogfood", "items": outer.sources})
                if self.path.startswith("/v1/settings"):
                    return self._reply(200, {"knobs": list(outer.settings.values())})
                return self._reply(404, {})

            def do_PUT(self):  # noqa: N802
                body = self._body()
                outer.requests.append(("PUT", self.path, body))
                key = self.path.rsplit("/", 1)[-1]
                if key not in outer.settings:
                    return self._reply(404, {})
                outer.settings[key] = {"key": key, "value": body["value"], "source": "project"}
                return self._reply(200, {"key": key, "value": body["value"], "source": "project"})

            def do_DELETE(self):  # noqa: N802
                outer.requests.append(("DELETE", self.path, None))
                return self._reply(200, {"state": "cancelled"})

            def do_POST(self):  # noqa: N802
                body = self._body()
                outer.requests.append(("POST", self.path, body))
                if self.path == "/v1/setup/sign-in":
                    provider = body["provider"]
                    signed_in = (outer.state / outer.STATE_FILE[provider]).exists()
                    session = {"sign_in_id": f"si_{len(outer.sign_ins)}", "provider": provider,
                               "state": "signed_in" if signed_in else "waiting",
                               "verification_url": None if signed_in else
                               "https://auth.openai.com/codex/device",
                               "user_code": None if signed_in else "ABCD-EFGH1",
                               "needs_code_input": False, "expires_at": None,
                               "headline": "h", "fix": "f"}
                    outer.sign_ins[session["sign_in_id"]] = session
                    return self._reply(200, session)
                if self.path == "/v1/setup/models" and body.get("kind") == "subscription":
                    outer.sources.append({"source_id": outer.SUBSCRIPTION_SOURCES[body["provider"]],
                                          "connection_status": "connected"})
                    # Repo and models' connect proves it with one governed test turn, then
                    # answers with the whole view.
                    return self._reply(200, {"models": [
                        {"provider": body["provider"], "pill": {"state": "working"}}]})
                if self.path == "/v1/setup/model":
                    outer.sources = [item for item in outer.sources
                                     if item.get("source_id") != "setup-custom"]
                    outer.sources.append({"source_id": "setup-custom",
                                          "credential_secret_ref_id": "ref-1",
                                          "connection_metadata": {"base_url": body["base_url"]},
                                          "supported_models": [body["model"]]})
                    return self._reply(201, {"ok": True, "source_id": "setup-custom",
                                             "model": body["model"]})
                return self._reply(404, {})

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_port
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def ollama_names(self) -> list[str] | None:
        if not self.local:
            return self.models
        if not (self.state / "ollama-running").exists():
            return None
        pulled = self.state / "ollama-models"
        return pulled.read_text().split() if pulled.exists() else []

    def posts(self, path: str) -> list[dict]:
        return [body for method, where, body in self.requests if method == "POST" and where == path]

    def puts(self) -> dict[str, str]:
        return {where.rsplit("/", 1)[-1]: body["value"]
                for method, where, body in self.requests if method == "PUT"}

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def boss_api(world):
    state = world["tmp"] / "container-state"
    state.mkdir()
    fake = _FakeBoss(state)
    world["env"].update(BOSS_API_PORT=str(fake.port),
                        BOSS_OLLAMA_URL=f"http://127.0.0.1:{fake.port}",
                        BOSS_OLLAMA_REGISTRY=f"http://127.0.0.1:{fake.port}",
                        BOSS_TEST_STATE=str(state))
    yield fake
    fake.close()


def _run_tty(*argv: str | Path, env: dict[str, str], answers: str = "",
             timeout: int = 120) -> tuple[int, str]:
    """Run with a controlling terminal, as a person at a shell would, typing `answers`."""
    import fcntl
    import select
    import termios
    import time

    master, slave = os.openpty()
    proc = subprocess.Popen(
        [str(arg) for arg in argv], env=env, stdin=slave, stdout=slave, stderr=slave,
        start_new_session=True, preexec_fn=lambda: fcntl.ioctl(0, termios.TIOCSCTTY, 0),
    )
    os.close(slave)
    if answers:
        os.write(master, answers.encode())
    chunks: list[bytes] = []
    deadline = time.monotonic() + timeout
    while True:
        ready, _, _ = select.select([master], [], [], 0.2)
        if ready:
            try:
                data = os.read(master, 65536)
            except OSError:
                data = b""
            if not data:
                break
            chunks.append(data)
            continue
        if proc.poll() is not None:
            break
        if time.monotonic() > deadline:
            proc.kill()
            break
    proc.wait(timeout=10)
    os.close(master)
    return proc.returncode, b"".join(chunks).decode(errors="replace").replace("\r\n", "\n")


def _docker_calls(world) -> str:
    log = Path(world["env"]["BOSS_TEST_DOCKER_LOG"])
    return log.read_text() if log.exists() else ""


def _set_env_line(install: Path, name: str, value: str) -> None:
    env_file = install / ".env"
    lines = [line for line in env_file.read_text().splitlines() if not line.startswith(name + "=")]
    env_file.write_text("\n".join(lines + [f"{name}={value}"]) + "\n")


def _found_on_host(world) -> None:
    """A Claude Code and a Grok Build login on this machine (host side only)."""
    world["env"]["BOSS_TEST_CLAUDE_LOGIN"] = "1"
    grok = world["home"] / ".grok"
    grok.mkdir()
    (grok / "auth.json").write_text('{"token": "%s"}' % CODEX_SECRET)


def test_compose_lets_containers_reach_the_host_without_publishing_anything() -> None:
    compose = (REPO / "scripts/install/community-compose.yml").read_text()
    blocks = {
        "api": compose[compose.index("\n  api:\n"):compose.index("\n  worker:\n")],
        "worker": compose[compose.index("\n  worker:\n"):compose.index("\n  web:\n")],
    }
    for service, block in blocks.items():
        assert '- "host.docker.internal:host-gateway"' in block, service
        assert "BOSS_MODEL_PRICES_JSON: ${BOSS_MODEL_PRICES_JSON:-}" in block, service
    assert compose.count("host-gateway") == 2


def test_yes_alone_signs_nothing_in_and_connects_no_ollama(world, boss_api) -> None:
    _found_on_host(world)
    result = _install(world)
    rendered = result.stdout + result.stderr
    assert result.returncode == 0, rendered
    assert "Claude Code  found, not connected: boss connect claude" in rendered
    assert "Grok Build   found, not connected: boss connect grok" in rendered
    assert "Ollama       found, not connected: boss connect ollama" in rendered
    calls = _docker_calls(world)
    assert "auth login" not in calls and "login --device-auth" not in calls
    assert "host.docker.internal" not in calls
    assert boss_api.posts("/v1/setup/model") == []
    assert CODEX_SECRET not in rendered


def test_connect_found_without_a_terminal_skips_sign_in_and_connects_ollama(world, boss_api) -> None:
    _found_on_host(world)
    result = _install(world, "--connect-found")
    rendered = result.stdout + result.stderr
    assert result.returncode == 0, rendered
    assert "Claude Code: signing in needs a person at a terminal. Run: boss connect claude" in rendered
    assert "Grok Build: signing in needs a person at a terminal. Run: boss connect grok" in rendered
    assert "Claude Code  found, not connected: boss connect claude" in rendered
    calls = _docker_calls(world)
    assert "auth login" not in calls and "login --device-auth" not in calls

    # Ollama needs no person: reachability is checked from the API container, then it is
    # registered through the setup route with a placeholder key.
    assert "exec -T api python -c" in calls
    assert f"http://host.docker.internal:{boss_api.port} llama3.2:latest" in calls
    assert boss_api.posts("/v1/setup/model") == [{
        "project_id": "boss-dogfood", "actor": "ceo", "provider": "custom", "api_key": "ollama",
        "base_url": f"http://host.docker.internal:{boss_api.port}/v1", "model": "llama3.2:latest",
    }]
    assert "Ollama       connected: llama3.2:latest" in rendered
    # Nothing hosted is connected, so the found Ollama became every role's model.
    assert "Default      Ollama llama3.2:latest for every role" in rendered
    assert CODEX_SECRET not in rendered


def test_connect_found_at_a_terminal_signs_in_inside_the_container(world, boss_api) -> None:
    _found_on_host(world)
    code, output = _run_tty("bash", REPO / "install.sh", "--dir", world["install"], "--yes",
                            "--no-install-deps", "--no-web", "--connect-found", env=world["env"])
    assert code == 0, output
    assert "login" not in _docker_calls(world)
    started = [body["provider"] for body in boss_api.posts("/v1/setup/sign-in")]
    assert started == ["claude_code", "grok"]
    recorded = [body["provider"] for body in boss_api.posts("/v1/setup/models")]
    assert recorded == ["claude_code", "grok"]
    assert "Claude Code  connected" in output
    assert "Grok Build   connected" in output
    # Nothing from the host login reaches BOSS.
    assert CODEX_SECRET not in output
    assert CODEX_SECRET not in str(boss_api.requests)


def test_boss_connect_asks_once_per_found_login_and_no_changes_nothing(world, boss_api) -> None:
    boss = _installed(world)
    _found_on_host(world)
    Path(world["env"]["BOSS_TEST_DOCKER_LOG"]).write_text("")
    # Claude: yes. Grok: no. Ollama: no.
    code, output = _run_tty(boss, "connect", env=world["env"], answers="y\nn\nn\n")
    assert code == 0, output
    assert output.count("[y/N]") == 3
    calls = _docker_calls(world)
    assert [b["provider"] for b in boss_api.posts("/v1/setup/sign-in")] == ["claude_code"]
    assert "login" not in calls
    assert "host.docker.internal" not in calls
    assert "skipped   Grok Build. Later: boss connect grok" in output
    assert "skipped   Ollama (nothing was changed)" in output
    assert [b["provider"] for b in boss_api.posts("/v1/setup/models")] == ["claude_code"]
    assert boss_api.posts("/v1/setup/model") == []

    # Signed in already: nothing to ask, nothing to sign in again.
    again_code, again = _run_tty(boss, "connect", env=world["env"], answers="n\nn\n")
    assert again_code == 0, again
    assert "Claude Code is already signed in inside BOSS" in again
    assert len(boss_api.posts("/v1/setup/sign-in")) == 1


def test_boss_connect_claude_and_grok_at_a_terminal(world, boss_api) -> None:
    boss = _installed(world)
    code, output = _run_tty(boss, "connect", "grok", env=world["env"])
    assert code == 0, output
    assert "1. Open  https://auth.openai.com/codex/device" in output
    assert "2. Enter the code  ABCD-EFGH1" in output
    assert "✓ Grok Build is signed in inside BOSS and connected: it answered one test turn" in output

    code, output = _run_tty(boss, "connect", "claude", env=world["env"])
    assert code == 0, output
    assert "✓ Claude Code is signed in inside BOSS and connected" in output

    # A second explicit run: BOSS answers "signed in" at once and the proof runs again.
    code, output = _run_tty(boss, "connect", "claude", env=world["env"])
    assert code == 0, output
    assert "Claude Code is already signed in inside BOSS." in output
    assert "login" not in _docker_calls(world)
    assert [b["provider"] for b in boss_api.posts("/v1/setup/sign-in")] == [
        "grok", "claude_code", "claude_code"]
    assert len(boss_api.posts("/v1/setup/models")) == 3


def test_boss_connect_codex_signed_in_inside_boss_connects_and_proves_once(world, boss_api) -> None:
    """P0-1 (c): Codex already signed in inside the container: exactly one POST
    /v1/setup/models, exit 0 on a working pill; no import, no credential route."""
    boss = _installed(world)
    (boss_api.state / "codex").touch()
    result = _run(boss, "connect", "codex", env=world["env"])
    rendered = result.stdout + result.stderr
    assert result.returncode == 0, rendered
    assert boss_api.posts("/v1/setup/models") == [{
        "project_id": "boss-dogfood", "actor": "ceo", "provider": "codex",
        "kind": "subscription"}]
    assert not any("credential" in where for _m, where, _b in boss_api.requests)
    assert "✓ Codex is signed in inside BOSS and connected" in rendered
    assert "None" not in rendered


def test_boss_connect_codex_at_a_terminal_signs_in_through_its_own_login(world, boss_api) -> None:
    boss = _installed(world)
    code, output = _run_tty(boss, "connect", "codex", env=world["env"])
    assert code == 0, output
    assert "Signing Codex in inside BOSS" in output
    assert "ABCD-EFGH1" in output
    assert [b["provider"] for b in boss_api.posts("/v1/setup/sign-in")] == ["codex"]
    assert len(boss_api.posts("/v1/setup/models")) == 1
    assert "login" not in _docker_calls(world)


def test_a_failed_codex_sign_in_says_why_and_records_nothing(world, boss_api) -> None:
    boss = _installed(world)
    boss_api.sign_in_fails = True
    code, output = _run_tty(boss, "connect", "codex", env=world["env"])
    assert code == 1, output
    assert "Sign-in failed — The login stopped. Start it again." in output
    assert "✗ Codex was not connected; the reason is above. Retry: boss connect codex" in output
    assert boss_api.posts("/v1/setup/models") == []


def test_install_connect_codex_never_dies_after_boss_is_up(world, boss_api) -> None:
    """P0-1 (d): --connect-codex unattended names the next step; at a terminal a failed
    sign-in is reported. Either way the install finishes."""
    unattended = _install(world, "--connect-codex")
    rendered = unattended.stdout + unattended.stderr
    assert unattended.returncode == 0, rendered
    assert "Next: boss connect codex" in rendered
    assert boss_api.posts("/v1/setup/sign-in") == []

    boss_api.sign_in_fails = True
    code, output = _run_tty("bash", REPO / "install.sh", "--dir", world["install"], "--yes",
                            "--no-install-deps", "--no-web", "--connect-codex", env=world["env"])
    assert code == 0, output
    assert "Codex was not connected; the reason is above. BOSS is running." in output
    assert boss_api.posts("/v1/setup/models") == []


def test_boss_connect_claude_without_a_terminal_or_with_an_api_key_signs_nothing_in(
        world, boss_api) -> None:
    boss = _installed(world)
    no_tty = _run(boss, "connect", "claude", env=world["env"])
    assert no_tty.returncode == 2, no_tty.stdout + no_tty.stderr
    assert "needs a person at a terminal. Run: boss connect claude" in no_tty.stdout

    _set_env_line(world["install"], "ANTHROPIC_API_KEY", SECRET)
    blocked_code, blocked = _run_tty(boss, "connect", "claude", env=world["env"])
    assert blocked_code == 1, blocked
    assert "holds ANTHROPIC_API_KEY" in blocked
    assert SECRET not in blocked
    assert "auth login" not in _docker_calls(world)
    assert boss_api.posts("/v1/setup/models") == []


def test_boss_connect_ollama_registers_once_and_reports_an_unreachable_host(world, boss_api) -> None:
    boss = _installed(world)
    world["env"]["BOSS_TEST_OLLAMA_UNREACHABLE"] = "1"
    unreachable = _run(boss, "connect", "ollama", env=world["env"])
    assert unreachable.returncode == 1
    assert "cannot reach Ollama at http://host.docker.internal:" in unreachable.stdout
    assert boss_api.posts("/v1/setup/model") == []

    world["env"]["BOSS_TEST_OLLAMA_UNREACHABLE"] = "0"
    first = _run(boss, "connect", "ollama", env=world["env"])
    assert first.returncode == 0, first.stdout + first.stderr
    assert "Ollama connected: llama3.2:latest" in first.stdout
    again = _run(boss, "connect", "ollama", env=world["env"])
    assert again.returncode == 0, again.stdout + again.stderr
    assert "already connected" in again.stdout
    assert len(boss_api.posts("/v1/setup/model")) == 1

    # Another OpenAI-compatible connection is never replaced from here.
    boss_api.sources[0]["connection_metadata"]["base_url"] = "https://llm.example.invalid/v1"
    other = _run(boss, "connect", "ollama", env=world["env"])
    assert other.returncode == 1
    assert "Nothing was changed" in other.stderr
    assert len(boss_api.posts("/v1/setup/model")) == 1

    # A model the user names must exist in Ollama.
    boss_api.sources = []
    missing = _run(boss, "connect", "ollama", env=dict(world["env"], BOSS_OLLAMA_MODEL="nope:1b"))
    assert missing.returncode == 1
    assert "no model named 'nope:1b'" in missing.stderr


def test_boss_connect_saves_exported_keys_and_restarts_only_api_and_worker(world, boss_api) -> None:
    boss = _installed(world)
    log = Path(world["env"]["BOSS_TEST_DOCKER_LOG"])
    log.write_text("")
    env = dict(world["env"])
    for provider, value in (("OPENAI", SECRET), ("XAI", SECRET + "-x")):
        env[f"{provider}_API_KEY"] = value

    result = _run(boss, "connect", "keys", env=env)

    rendered = result.stdout + result.stderr
    assert result.returncode == 0, rendered
    env_file = world["install"] / ".env"
    text = env_file.read_text()
    assert f"OPENAI_API_KEY={SECRET}\n" in text and f"XAI_API_KEY={SECRET}-x\n" in text
    assert text.count("OPENAI_API_KEY=") == 1
    assert oct(env_file.stat().st_mode & 0o777) == "0o600"
    assert SECRET not in rendered
    assert "OPENAI_API_KEY" in rendered and "XAI_API_KEY" in rendered
    calls = log.read_text()
    assert "up -d --no-deps --no-build --force-recreate api worker" in calls
    assert " down" not in calls and "--volumes" not in calls
    assert "Keys applied" in rendered
    assert SECRET not in calls  # never on a command line

    # Nothing new: no restart.
    log.write_text("")
    again = _run(boss, "connect", "keys", env=env)
    assert again.returncode == 0, again.stderr
    assert "already holds these keys" in again.stdout
    assert "force-recreate" not in log.read_text()

    # Plain `boss connect` picks up a newly exported key too.
    plain = _run(boss, "connect", env=dict(world["env"], GOOGLE_API_KEY=SECRET + "-g"))
    assert plain.returncode == 0, plain.stdout + plain.stderr
    assert f"GOOGLE_API_KEY={SECRET}-g" in env_file.read_text()
    assert SECRET not in plain.stdout + plain.stderr

    # A key typed as an argument is refused and not echoed.
    argv = _run(boss, "connect", SECRET, env=world["env"])
    assert argv.returncode != 0
    assert SECRET not in argv.stdout + argv.stderr


def test_boss_restarts_the_images_the_installer_built(world, boss_api) -> None:
    # install.sh tags api, worker and web with the compose project name. `boss` must ask
    # Compose for the same tag, or the restart looks for boss-community-api:local and fails.
    boss = _installed(world)
    tags = Path(world["env"]["BOSS_TEST_DOCKER_LOG"] + ".tags")
    tags.write_text("")

    result = _run(boss, "connect", "keys", env=dict(world["env"], OPENAI_API_KEY=SECRET))

    assert result.returncode == 0, result.stdout + result.stderr
    restarts = [line for line in tags.read_text().splitlines() if "force-recreate" in line]
    assert restarts, tags.read_text()
    assert all(line.startswith("boss-community ") for line in restarts), restarts


def test_purge_removes_this_installs_boss_command_and_nothing_else(world) -> None:
    boss = _installed(world)
    env = world["env"]

    kept = _run(boss, "uninstall", "--yes", env=env)
    assert kept.returncode == 0, kept.stderr
    assert boss.exists()

    refused = _run(boss, "uninstall", "--purge", "--yes", env=env)
    assert refused.returncode != 0
    assert boss.exists()

    dry = _run(boss, "uninstall", "--purge", "--dry-run", env=env)
    assert dry.returncode == 0, dry.stderr
    assert f"Would also remove the boss command: {boss}" in dry.stdout
    assert boss.exists()

    # A launcher that manages another install is left alone.
    other = world["tmp"] / "other-bin" / "boss"
    other.parent.mkdir()
    default_line = next(line for line in boss.read_text().splitlines()
                        if line.startswith("BOSS_INSTALL_DIR_DEFAULT="))
    other.write_text(boss.read_text().replace(default_line, "BOSS_INSTALL_DIR_DEFAULT=/elsewhere"))
    other.chmod(0o755)

    purged = _run(boss, "uninstall", "--purge", "--yes", "--confirm-purge", "DELETE-BOSS-DATA",
                  env=env)

    assert purged.returncode == 0, purged.stdout + purged.stderr
    validated = Path(env["BOSS_TEST_BACKUP_TRACE"] + ".validated").read_text().split()
    assert validated[0] == "validate" and Path(validated[1]).parent == Path(env["BOSS_TEST_BACKUP_ROOT"])
    assert "--volumes" in _docker_calls(world)
    assert not (world["install"] / ".env").exists()
    assert not boss.exists()
    assert f"Removed the boss command: {boss}" in purged.stdout
    assert other.exists()


def test_purge_deletes_nothing_when_the_backup_does_not_validate(world) -> None:
    boss = _installed(world)
    env = dict(world["env"], BOSS_TEST_ARCHIVE_REFUSE="1")
    purged = _run(boss, "uninstall", "--purge", "--yes", "--confirm-purge", "DELETE-BOSS-DATA",
                  env=env)
    assert purged.returncode != 0
    assert "data purge was cancelled" in purged.stderr
    assert "--volumes" not in _docker_calls(world)
    assert (world["install"] / ".env").exists() and boss.exists()


def test_purge_never_removes_another_programs_boss(world) -> None:
    _installed(world)
    foreign = world["tmp"] / "foreign" / "boss"
    foreign.parent.mkdir()
    _executable(foreign, "#!/bin/sh\necho someone-elses-boss\n")
    env = dict(world["env"], BOSS_LAUNCHER=str(foreign),
               PATH=f"{foreign.parent}:{world['env']['PATH']}")
    purged = _run("bash", world["install"] / "scripts/install/boss.sh", "--dir", world["install"],
                  "uninstall", "--purge", "--yes", "--confirm-purge", "DELETE-BOSS-DATA", env=env)
    assert purged.returncode == 0, purged.stdout + purged.stderr
    assert foreign.read_text() == "#!/bin/sh\necho someone-elses-boss\n"
    assert world["boss"].exists()  # not on PATH and not named by BOSS_LAUNCHER: left alone
    assert "Removed the boss command" not in purged.stdout


def test_release_record_is_not_a_local_change_in_the_install(world) -> None:
    _installed(world)
    assert (world["install"] / ".boss-release").exists()
    assert ".boss-release" not in _git("status", "--porcelain", cwd=world["install"])
    assert ".boss-release" in (REPO / ".gitignore").read_text().splitlines()
    rerun = _install(world)
    assert rerun.returncode == 0, rerun.stdout + rerun.stderr
    exclude = Path(_git("rev-parse", "--git-path", "info/exclude", cwd=world["install"]))
    exclude = exclude if exclude.is_absolute() else world["install"] / exclude
    assert exclude.read_text().splitlines().count("/.boss-release") == 1


# ---- phase 2b: the Models step and the local model --------------------------------------------

DEFAULT_LOCAL = "qwen2.5-coder:7b"


def _local_ollama(world, boss_api, *, installed: bool = False, running: bool = False,
                  pulled: tuple[str, ...] = (), unit: bool = False, brew: bool = False,
                  loopback_only: bool = False) -> Path:
    """Ollama as BOSS would set it up: its installer, binary, service, and Homebrew, all fakes.

    Nothing is downloaded and nothing runs a model. Returns the call log.
    """
    boss_api.local = True
    state = boss_api.state
    bin_dir = Path(world["env"]["BOSS_TEST_BIN"])
    fake = world["tmp"] / "fake-ollama"
    _executable(fake,
                "#!/bin/sh\n"
                "printf 'ollama %s OLLAMA_HOST=%s\\n' \"$*\" \"${OLLAMA_HOST:-}\" >> \"$BOSS_TEST_STATE/calls\"\n"
                "case \"$1\" in\n"
                "  serve) touch \"$BOSS_TEST_STATE/ollama-running\" ;;\n"
                "  pull) printf '%s\\n' \"$2\" >> \"$BOSS_TEST_STATE/ollama-models\" ;;\n"
                "esac\n")
    installer = world["tmp"] / "ollama-install.sh"
    installer.write_text("#!/bin/sh\n# stands in for the official Ollama installer\n"
                         f"cp '{fake}' \"$BOSS_TEST_BIN/ollama\"\n"
                         "touch \"$BOSS_TEST_STATE/ollama-unit\" \"$BOSS_TEST_STATE/ollama-running\"\n")
    _executable(bin_dir / "systemctl",
                "#!/bin/sh\nprintf 'systemctl %s\\n' \"$*\" >> \"$BOSS_TEST_STATE/calls\"\n"
                "case \"$1\" in\n"
                "  cat) [ -e \"$BOSS_TEST_STATE/ollama-unit\" ] ;;\n"
                "  enable|start) touch \"$BOSS_TEST_STATE/ollama-running\" ;;\n"
                "  restart) touch \"$BOSS_TEST_STATE/ollama-running\"\n"
                "           rm -f \"$BOSS_TEST_STATE/ollama-loopback-only\" ;;\n"
                "esac\n")
    _executable(bin_dir / "sudo",
                "#!/bin/sh\nprintf 'sudo %s\\n' \"$*\" >> \"$BOSS_TEST_STATE/calls\"\n"
                "[ \"$1\" = -n ] && shift\nexec \"$@\"\n")
    _executable(bin_dir / "brew",
                "#!/bin/sh\nprintf 'brew %s\\n' \"$*\" >> \"$BOSS_TEST_STATE/calls\"\n"
                "case \"$1 $2\" in\n"
                f"  'install ollama') cp '{fake}' \"$BOSS_TEST_BIN/ollama\"\n"
                "                    touch \"$BOSS_TEST_STATE/brew-ollama\" ;;\n"
                "  'list --formula') [ -e \"$BOSS_TEST_STATE/brew-ollama\" ] ;;\n"
                "  'services start') touch \"$BOSS_TEST_STATE/ollama-running\" ;;\n"
                "esac\n")
    world["env"]["BOSS_TEST_OLLAMA_INSTALLER"] = str(installer)
    if installed:
        shutil.copy2(fake, bin_dir / "ollama")
    for flag, name in ((brew, "brew-ollama"), (unit, "ollama-unit"), (running, "ollama-running"),
                       (loopback_only, "ollama-loopback-only")):
        if flag:
            (state / name).touch()
    if pulled:
        (state / "ollama-models").write_text("\n".join(pulled) + "\n")
    calls = state / "calls"
    calls.touch()
    return calls


def _curl_urls(world) -> str:
    log = Path(world["env"]["BOSS_TEST_CURL_LOG"])
    return log.read_text() if log.exists() else ""


ROLE_DEFAULT = {key: ("native" if key.endswith("_engine") else DEFAULT_LOCAL)
                for key in _FakeBoss.ROLE_KEYS}


def test_install_ollama_from_scratch_on_linux_sets_up_everything_and_becomes_the_default(
        world, boss_api) -> None:
    boss = _installed(world)
    calls = _local_ollama(world, boss_api, loopback_only=True)
    Path(world["env"]["BOSS_TEST_DOCKER_LOG"]).write_text("")
    env = dict(world["env"], BOSS_TEST_UNAME="Linux")

    result = _run(boss, "connect", "--yes", "--install-ollama", env=env, timeout=120)

    out = result.stdout + result.stderr
    assert result.returncode == 0, out
    # Installed with the official installer (a fixture here), then started by it.
    assert "https://ollama.com/install.sh" in _curl_urls(world)
    assert "✓ Ollama installed" in out
    # Linux: the containers could not reach a loopback-only Ollama, so it was bound to the
    # Docker bridge address through a systemd drop-in, never to 0.0.0.0.
    dropin = (world["tmp"] / "ollama.service.d" / "boss-docker-bridge.conf").read_text()
    assert f'Environment="OLLAMA_HOST=127.0.0.1:{boss_api.port}"' in dropin
    assert "After=docker.service" in dropin and "0.0.0.0" not in dropin
    assert "systemctl restart ollama.service" in calls.read_text()
    # Size and disk shown before the pull.
    assert out.index("about 4.4 GB to download") < out.index("Pulling qwen2.5-coder:7b")
    assert "Disk: " in out and "GB free" in out
    assert f"ollama pull {DEFAULT_LOCAL} OLLAMA_HOST=127.0.0.1:{boss_api.port}" in calls.read_text()
    # Registered through the setup route, then made every role's model: nothing hosted.
    assert boss_api.posts("/v1/setup/model")[-1]["model"] == DEFAULT_LOCAL
    assert boss_api.puts() == ROLE_DEFAULT
    assert "is now the default for every role" in out
    env_text = (world["install"] / ".env").read_text()
    local = f"http://host.docker.internal:{boss_api.port}/v1"
    # Zero price for this endpoint only: a hosted model with the same name never matches it.
    assert f"BOSS_MODEL_PRICES_JSON='{{\"{local}#{DEFAULT_LOCAL}\":[0,0]}}'" in env_text
    assert f"BOSS_OLLAMA_URL=http://127.0.0.1:{boss_api.port}" in env_text
    # Nothing hosted: chat and the other OPENAI_* surfaces use the local model too.
    assert "OPENAI_API_KEY=ollama\n" in env_text
    assert f"OPENAI_BASE_URL={local}\n" in env_text
    assert f"OPENAI_MODEL={DEFAULT_LOCAL}\n" in env_text
    assert "Chat         the local Ollama model" in out
    assert "API keys     not found" in out  # the placeholder is not a hosted key
    assert "up -d --no-deps --no-build --force-recreate api worker" in _docker_calls(world)
    assert "No completion was run" in out
    assert f"Default      Ollama {DEFAULT_LOCAL} for every role" in out
    # The local model counts as usable: no "nothing connected" line after connecting it.
    assert "No model is connected yet" not in out
    # Not a terminal: the pull's progress spinner never reaches the output.
    assert "\x1b[" not in out
    # Discovery afterwards looks where the install recorded Ollama, not only at 127.0.0.1:11434.
    plain = {k: v for k, v in env.items() if k != "BOSS_OLLAMA_URL"}  # only .env knows it now
    listed = _run(boss, "connect", "--list", env=plain, timeout=60)
    assert "stopped" not in listed.stdout, listed.stdout
    assert "not found  Ollama" not in listed.stdout, listed.stdout
    # A reinstall after a purge has a new .env without BOSS_OLLAMA_URL; the Ollama an earlier
    # install bound with BOSS's own drop-in must still be found there, not reported as stopped.
    env_file = world["install"] / ".env"
    env_file.write_text("".join(line for line in env_file.read_text().splitlines(keepends=True)
                                if not line.startswith("BOSS_OLLAMA_URL=")))
    relisted = _run(boss, "connect", "--list", env=plain, timeout=60)
    assert "stopped" not in relisted.stdout, relisted.stdout
    assert "not found  Ollama" not in relisted.stdout, relisted.stdout


def test_install_ollama_starts_a_stopped_ollama_and_keeps_a_hosted_default(world, boss_api) -> None:
    boss = _installed(world)
    calls = _local_ollama(world, boss_api, installed=True, brew=True, pulled=(DEFAULT_LOCAL,))
    boss_api.sources.append({"source_id": "anthropic_claude_code", "connection_status": "connected"})
    env = dict(world["env"], BOSS_TEST_UNAME="Darwin")

    result = _run(boss, "connect", "--yes", "--install-ollama", env=env, timeout=120)

    out = result.stdout + result.stderr
    assert result.returncode == 0, out
    log = calls.read_text()
    assert "brew services start ollama" in log
    assert "brew install" not in log and "ollama pull" not in log
    assert "ollama.com" not in _curl_urls(world)
    assert f"{DEFAULT_LOCAL} is already pulled" in out
    assert boss_api.posts("/v1/setup/model")[-1]["model"] == DEFAULT_LOCAL
    # A hosted model is connected: Ollama is added, the roles are not touched.
    assert boss_api.puts() == {}
    assert "Ollama is added but not made the default" in out
    assert "Default      hosted:" in out and "Ollama is added but not the default" in out
    # Chat is untouched when a hosted model is connected.
    env_text = (world["install"] / ".env").read_text()
    assert "OPENAI_API_KEY=\n" in env_text and "OPENAI_BASE_URL=\n" in env_text
    assert "Chat         the local Ollama model" not in out


def test_install_ollama_checks_disk_space_before_pulling(world, boss_api) -> None:
    boss = _installed(world)
    calls = _local_ollama(world, boss_api, installed=True, unit=True, running=True)
    env = dict(world["env"], BOSS_TEST_UNAME="Linux", BOSS_TEST_FREE_KB="1000000")

    result = _run(boss, "connect", "--install-ollama", "--local-model", "qwen2.5-coder:14b", env=env)

    assert result.returncode == 1, result.stdout + result.stderr
    assert "Not enough disk space; nothing was downloaded" in result.stdout
    assert "boss connect --install-ollama --local-model NAME" in result.stdout
    assert "ollama pull" not in calls.read_text()
    assert boss_api.posts("/v1/setup/model") == []
    assert boss_api.puts() == {}


def test_install_ollama_fails_visibly_when_the_containers_cannot_reach_it(world, boss_api) -> None:
    boss = _installed(world)
    _local_ollama(world, boss_api, installed=True, brew=True, running=True, pulled=(DEFAULT_LOCAL,),
                  loopback_only=True)
    env = dict(world["env"], BOSS_TEST_UNAME="Darwin")

    result = _run(boss, "connect", "--install-ollama", env=env)

    assert result.returncode == 1, result.stdout + result.stderr
    assert "The BOSS containers cannot reach Ollama at http://host.docker.internal:" in result.stdout
    assert "boss connect --install-ollama" in result.stdout
    assert boss_api.posts("/v1/setup/model") == []
    assert not (world["tmp"] / "ollama.service.d").exists()


def test_a_role_the_operator_chose_is_kept_when_ollama_becomes_the_default(world, boss_api) -> None:
    boss = _installed(world)
    boss_api.settings["code_review_engine"] = {"key": "code_review_engine", "value": "codex",
                                               "source": "project"}
    result = _run(boss, "connect", "ollama", env=world["env"])
    assert result.returncode == 0, result.stdout + result.stderr
    puts = boss_api.puts()
    assert "code_review_engine" not in puts and "code_review_model" not in puts
    assert puts["code_agent_engine"] == "native" and puts["code_agent_model"] == "llama3.2:latest"


def _role_models(boss_api) -> dict[str, tuple[str, str]]:
    return {engine: (boss_api.settings[engine]["value"],
                     boss_api.settings[engine.replace("_engine", "_model")]["value"])
            for engine in ("code_agent_engine", "code_review_engine",
                           "code_agent_strong_engine", "code_agent_low_cost_engine")}


def test_switching_the_local_model_moves_the_roles_boss_set_and_keeps_a_hand_set_one(
        world, boss_api) -> None:
    """`boss connect ollama --local-model NAME` used to say "every role already has a model you
    chose" and move only chat: the roles BOSS itself had given the old local model stayed on
    it. BOSS records the roles it set, so those follow the switch; one the operator set by hand
    afterwards is still never overwritten."""
    boss_api.models = ["llama3.2:latest", "qwen2.5-coder:1.5b"]
    boss = _installed(world)
    first = _run(boss, "connect", "ollama", env=world["env"])
    assert first.returncode == 0, first.stdout + first.stderr
    assert all(value == ("native", "llama3.2:latest") for value in _role_models(boss_api).values())
    record = world["install"] / ".env.boss-local-roles"
    assert record.exists() and oct(record.stat().st_mode & 0o777) == "0o600"
    import json
    assert json.loads(record.read_text()) == {  # role names and the model, never a credential
        "model": "llama3.2:latest",
        "lanes": ["code_agent_engine", "code_agent_low_cost_engine",
                  "code_agent_strong_engine", "code_review_engine"]}

    # The operator picks the reviewer by hand: same engine, a model of their own.
    boss_api.settings["code_review_model"] = {"key": "code_review_model",
                                              "value": "my-reviewer:latest", "source": "project"}
    switched = _run(boss, "connect", "ollama", "--local-model", "qwen2.5-coder:1.5b",
                    env=world["env"])
    out = switched.stdout + switched.stderr
    assert switched.returncode == 0, out
    roles = _role_models(boss_api)
    assert roles["code_agent_engine"] == ("native", "qwen2.5-coder:1.5b")
    assert roles["code_agent_strong_engine"] == ("native", "qwen2.5-coder:1.5b")
    assert roles["code_agent_low_cost_engine"] == ("native", "qwen2.5-coder:1.5b")
    assert roles["code_review_engine"] == ("native", "my-reviewer:latest")
    assert "every role already has a model you chose" not in out
    assert "The 3 role(s) BOSS had given the previous local model now use Ollama " \
           "qwen2.5-coder:1.5b; 1 role(s) you chose were left alone" in out


def test_a_role_set_by_hand_to_the_old_local_model_stays_when_boss_recorded_its_roles(
        world, boss_api) -> None:
    boss_api.models = ["llama3.2:latest", "qwen2.5-coder:1.5b"]
    boss = _installed(world)
    assert _run(boss, "connect", "ollama", env=world["env"]).returncode == 0
    # BOSS's record says the reviewer is not one of its roles: the operator owns it.
    record = world["install"] / ".env.boss-local-roles"
    record.write_text('{"model": "llama3.2:latest", "lanes": ["code_agent_engine"]}\n')
    switched = _run(boss, "connect", "ollama", "--local-model", "qwen2.5-coder:1.5b",
                    env=world["env"])
    assert switched.returncode == 0, switched.stdout + switched.stderr
    roles = _role_models(boss_api)
    assert roles["code_agent_engine"] == ("native", "qwen2.5-coder:1.5b")
    assert roles["code_review_engine"] == ("native", "llama3.2:latest")


def test_a_role_set_by_hand_to_the_new_model_stays_the_operators_on_the_way_back(
        world, boss_api) -> None:
    """The reviewer is put on the small model by hand. Switching BOSS to that model must not
    adopt it, so switching back moves only BOSS's roles and the reviewer stays where the
    operator put it."""
    boss_api.models = ["llama3.2:latest", "qwen2.5-coder:1.5b"]
    boss = _installed(world)
    assert _run(boss, "connect", "ollama", env=world["env"]).returncode == 0
    (world["install"] / ".env.boss-local-roles").unlink()  # an install from before the record
    boss_api.settings["code_review_model"] = {"key": "code_review_model",
                                              "value": "qwen2.5-coder:1.5b", "source": "project"}
    there = _run(boss, "connect", "ollama", "--local-model", "qwen2.5-coder:1.5b",
                 env=world["env"])
    assert there.returncode == 0, there.stdout + there.stderr
    assert "1 role(s) you chose were left alone" in there.stdout
    back = _run(boss, "connect", "ollama", "--local-model", "llama3.2:latest", env=world["env"])
    assert back.returncode == 0, back.stdout + back.stderr
    roles = _role_models(boss_api)
    assert roles["code_agent_engine"] == ("native", "llama3.2:latest")
    assert roles["code_agent_low_cost_engine"] == ("native", "llama3.2:latest")
    assert roles["code_review_engine"] == ("native", "qwen2.5-coder:1.5b")


def test_an_install_from_before_the_record_moves_roles_on_the_old_local_model(
        world, boss_api) -> None:
    """Installs that predate the record (v1.0-rc2 and earlier) are read by comparison: a role
    still native on exactly the previously connected Ollama model was set by BOSS."""
    boss_api.models = ["llama3.2:latest", "qwen2.5-coder:1.5b"]
    boss = _installed(world)
    assert _run(boss, "connect", "ollama", env=world["env"]).returncode == 0
    (world["install"] / ".env.boss-local-roles").unlink()
    boss_api.settings["code_agent_low_cost_engine"] = {
        "key": "code_agent_low_cost_engine", "value": "codex", "source": "project"}
    switched = _run(boss, "connect", "ollama", "--local-model", "qwen2.5-coder:1.5b",
                    env=world["env"])
    assert switched.returncode == 0, switched.stdout + switched.stderr
    roles = _role_models(boss_api)
    assert roles["code_agent_engine"] == ("native", "qwen2.5-coder:1.5b")
    assert roles["code_review_engine"] == ("native", "qwen2.5-coder:1.5b")
    assert roles["code_agent_low_cost_engine"][0] == "codex"


def test_boss_connect_offers_a_local_model_with_yes_as_the_default(world, boss_api) -> None:
    boss = _installed(world)
    _local_ollama(world, boss_api)
    env = dict(world["env"], BOSS_TEST_UNAME="Linux")

    suppressed_code, suppressed = _run_tty(boss, "connect", "--no-local-model", env=env,
                                           answers="n\nn\nn\n")
    assert suppressed_code == 0, suppressed
    assert "[Y/n]" not in suppressed
    assert "ollama.com" not in _curl_urls(world)

    # Codex: no. Claude: no. Grok: no. Local model: Enter, which is yes.
    code, output = _run_tty(boss, "connect", env=env, answers="n\nn\nn\n\n", timeout=180)
    assert code == 0, output
    assert output.count("[y/N]") == 3 and output.count("[Y/n]") == 1
    assert f"downloads {DEFAULT_LOCAL} (about 4.4 GB)" in output
    assert "https://ollama.com/install.sh" in _curl_urls(world)
    assert f"Ollama       connected: {DEFAULT_LOCAL}" in output


def test_yes_alone_never_installs_a_local_model(world, boss_api) -> None:
    _local_ollama(world, boss_api)
    env = dict(world["env"], BOSS_TEST_UNAME="Linux")
    result = _install(world, env=env)
    out = result.stdout + result.stderr
    assert result.returncode == 0, out
    assert "ollama.com" not in _curl_urls(world)
    assert not (Path(env["BOSS_TEST_BIN"]) / "ollama").exists()
    assert "A free local model: boss connect --install-ollama" in out
    # The card: one row per source, each with its fix.
    assert "API keys     not found. Export a key, then: boss connect keys" in out
    assert "Codex        not found. Sign in inside BOSS: boss connect codex" in out
    assert "Claude Code  not found. Sign in inside BOSS: boss connect claude" in out
    assert "Ollama       not found. Set it up: boss connect --install-ollama" in out
    assert "Default      none. Model calls fail closed until one is connected: boss connect" in out

    again = _run(world["boss"], "connect", "--yes", env=env)
    assert again.returncode == 0, again.stdout + again.stderr
    assert "ollama.com" not in _curl_urls(world)


def test_installer_install_ollama_sets_up_the_local_model_unattended(world, boss_api) -> None:
    calls = _local_ollama(world, boss_api, loopback_only=True)
    env = dict(world["env"], BOSS_TEST_UNAME="Linux")
    result = _install(world, "--install-ollama", "--local-model", "qwen2.5-coder:3b", env=env)
    out = result.stdout + result.stderr
    assert result.returncode == 0, out
    assert "ollama pull qwen2.5-coder:3b" in calls.read_text()
    assert "Ollama       connected: qwen2.5-coder:3b" in out
    assert "Default      Ollama qwen2.5-coder:3b for every role" in out

    refused = _install(world, "--local-model", "bad name;rm", env=env)
    assert refused.returncode != 0
    assert "--local-model takes an Ollama model name" in refused.stdout + refused.stderr


def test_boss_connect_list_only_reports(world, boss_api) -> None:
    boss = _installed(world)
    Path(world["env"]["BOSS_TEST_DOCKER_LOG"]).write_text("")
    before = len(boss_api.requests)
    result = _run(boss, "connect", "--list", env=world["env"])
    assert result.returncode == 0, result.stderr
    assert "Coding CLIs on PATH: Claude Code, Codex CLI, Grok Build" in result.stdout
    assert f"found      Ollama at http://127.0.0.1:{boss_api.port} (1 model pulled: llama3.2:latest)" in result.stdout
    assert _docker_calls(world) == ""
    assert [r for r in boss_api.requests[before:] if r[0] != "GET"] == []


# ---- phase 2b decisions: rebind consent, scoped price, hosted first, chat ---------------------

def test_an_existing_ollama_is_moved_only_with_consent(world, boss_api) -> None:
    boss = _installed(world)
    calls = _local_ollama(world, boss_api, installed=True, unit=True, running=True,
                          pulled=(DEFAULT_LOCAL,), loopback_only=True)
    env = dict(world["env"], BOSS_TEST_UNAME="Linux")
    dropin = world["tmp"] / "ollama.service.d" / "boss-docker-bridge.conf"

    # Unattended: never moved without --rebind-ollama; the fix is printed.
    refused = _run(boss, "connect", "--yes", "--install-ollama", env=env)
    assert refused.returncode == 1, refused.stdout + refused.stderr
    assert "Your existing Ollama was left on 127.0.0.1" in refused.stdout
    assert "boss connect --install-ollama --rebind-ollama" in refused.stdout
    assert not dropin.exists() and "restart" not in calls.read_text()
    assert boss_api.posts("/v1/setup/model") == []

    # At a terminal the question defaults to no.
    # Rebind: Enter (no). Then the Codex, Claude Code and Grok sign-in offers: no.
    code, output = _run_tty(boss, "connect", "--install-ollama", env=env, answers="\nn\nn\nn\n")
    assert code == 1, output
    assert "Move it to the Docker bridge address" in output and "[y/N]" in output
    assert not dropin.exists()

    # The explicit flag allows it.
    moved = _run(boss, "connect", "--yes", "--install-ollama", "--rebind-ollama", env=env)
    assert moved.returncode == 0, moved.stdout + moved.stderr
    assert f"OLLAMA_HOST=127.0.0.1:{boss_api.port}" in dropin.read_text()


def test_a_hosted_openai_key_takes_chat_and_the_fallback_roles_back(world, boss_api) -> None:
    boss = _installed(world)
    _local_ollama(world, boss_api, installed=True, unit=True, running=True, pulled=(DEFAULT_LOCAL,))
    env = dict(world["env"], BOSS_TEST_UNAME="Linux")
    setup = _run(boss, "connect", "--yes", "--install-ollama", env=env)
    assert setup.returncode == 0, setup.stdout + setup.stderr
    assert boss_api.puts() == ROLE_DEFAULT
    assert "OPENAI_API_KEY=ollama" in (world["install"] / ".env").read_text()

    keyed = dict(env)
    keyed["OPENAI_API_KEY"] = SECRET
    result = _run(boss, "connect", "keys", env=keyed)
    out = result.stdout + result.stderr
    assert result.returncode == 0, out
    assert SECRET not in out
    env_text = (world["install"] / ".env").read_text()
    assert f"OPENAI_API_KEY={SECRET}\n" in env_text
    assert "OPENAI_BASE_URL=\n" in env_text and "OPENAI_MODEL=gpt-4o-mini\n" in env_text
    assert "move from the local Ollama to the OpenAI key" in out
    # The four lanes the fallback set are handed back; Ollama stays connected.
    released = [body for method, where, body in boss_api.requests
                if method == "PUT" and body["value"] == ""]
    assert len(released) == 4
    assert "went back to their defaults" in out


def test_a_rerun_with_a_hosted_openai_key_does_not_keep_the_local_chat_address(world) -> None:
    first = _install(world)
    assert first.returncode == 0, first.stdout + first.stderr
    local = "http://host.docker.internal:11434/v1"
    for name, value in (("OPENAI_API_KEY", "ollama"), ("OPENAI_BASE_URL", local),
                        ("OPENAI_MODEL", DEFAULT_LOCAL)):
        _set_env_line(world["install"], name, value)

    kept = _install(world)
    assert kept.returncode == 0, kept.stdout + kept.stderr
    assert f"OPENAI_BASE_URL={local}" in (world["install"] / ".env").read_text()

    env = dict(world["env"])
    env["OPENAI_API_KEY"] = SECRET
    hosted = _install(world, env=env)
    assert hosted.returncode == 0, hosted.stdout + hosted.stderr
    text = (world["install"] / ".env").read_text()
    assert f"OPENAI_API_KEY={SECRET}" in text
    assert local not in text and "OPENAI_MODEL=gpt-4o-mini" in text
