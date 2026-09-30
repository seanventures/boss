"""One-command install, the `boss` lifecycle command, and the remembered release.

Every test runs the real install.sh / upgrade.sh / boss.sh against local Git repositories
with inert Docker, curl, and provider CLIs on PATH. No daemon, network, or model call.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[2]
MARKER = re.compile(r"# >>> boss_pick_release\n(.*?)# <<< boss_pick_release", re.S)
PICKERS = (REPO / "install.sh", REPO / "scripts/install/upgrade.sh", REPO / "docs/landing/install.sh")
HELPERS = (
    "community-compose.yml", "restore_preserved_env.sh", "reconcile_api_credentials.py",
    "boss.sh", "upgrade.sh", "uninstall.sh", "discover_models.py", "coding_subscriptions.py",
    "console_password.py",
)
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
        "case \"$1:$2\" in compose:version) echo 'Docker Compose version v2' ;; esac\n"
        "exit 0\n",
    )
    _executable(
        bin_dir / "curl",
        "#!/bin/sh\nout=; code=\n"
        "while [ \"$#\" -gt 0 ]; do\n"
        "  if [ \"$1\" = -o ]; then out=\"$2\"; shift 2; continue; fi\n"
        "  if [ \"$1\" = -w ]; then code=200; shift 2; continue; fi\n"
        "  shift\ndone\n"
        "if [ -n \"$out\" ] && [ \"$out\" != /dev/null ]; then printf '{\"edition\":\"community\"}' > \"$out\"; fi\n"
        "if [ -n \"$code\" ]; then printf '%s' \"$code\"; fi\n",
    )
    # Provider CLIs: not signed in unless the test says so. Status output only.
    _executable(bin_dir / "codex",
                "#!/bin/sh\n[ \"$1 $2\" = 'login status' ] || exit 0\n"
                "if [ \"${BOSS_TEST_CODEX_LOGIN:-0}\" = 1 ]; then echo 'Logged in using ChatGPT'; exit 0; fi\n"
                "echo 'Not logged in'; exit 1\n")
    _executable(bin_dir / "claude", "#!/bin/sh\necho '{\"loggedIn\": false}'\n")
    _executable(bin_dir / "grok", "#!/bin/sh\nexit 0\n")
    home = tmp_path / "home"
    home.mkdir()
    env = {
        key: value for key, value in os.environ.items()
        if not key.startswith(("BOSS_", "OPENAI_", "ANTHROPIC_", "GOOGLE_", "XAI_", "OPENROUTER_",
                               "CODEX_", "CLAUDE_"))
    }
    env.update(
        HOME=str(home),
        PATH=f"{bin_dir}:{os.environ['PATH']}",
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
    assert "Codex login found, not connected: boss connect codex" in rendered
    assert "Connecting the Codex ChatGPT login" not in rendered
    assert "not found  Ollama" in rendered
    assert CODEX_SECRET not in rendered


def test_connect_found_uses_the_existing_import_path_and_reports_failure(world) -> None:
    _codex_login(world)
    database_only = _install(world, "--connect-found")
    assert database_only.returncode == 0, database_only.stdout + database_only.stderr
    assert "also needs --accept-database-fallback" in database_only.stdout
    assert "Connecting the Codex ChatGPT login" not in database_only.stdout

    # Consent given. Nothing listens on the API port, so the import fails visibly and BOSS
    # itself is still reported as running.
    result = _install(world, "--connect-found", "--accept-database-fallback")
    rendered = result.stdout + result.stderr
    assert result.returncode == 0, rendered
    assert "Connecting the Codex ChatGPT login" in rendered
    assert "✗ Codex login not connected: boss connect codex" in rendered
    assert CODEX_SECRET not in rendered


def test_boss_connect_without_a_terminal_copies_nothing(world) -> None:
    boss = _installed(world)
    _codex_login(world)
    result = _run(boss, "connect", env=world["env"])
    assert result.returncode == 0, result.stdout + result.stderr
    assert "found      Codex ChatGPT login" in result.stdout
    assert "skipped   Codex (nothing was copied)" in result.stdout
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
        assert "found      Ollama" in found.stdout and "(1 model)" in found.stdout
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
    result = _run("bash", "-s", "--", "--yes", stdin=(REPO / "docs/landing/install.sh").read_text(),
                  env=env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "raw.githubusercontent.com/seanventures/boss/v0.10.0/install.sh" in (tmp_path / "curl.log").read_text()
    assert (tmp_path / "ran").read_text().strip() == "ref=v0.10.0 args=--yes"


def test_bootstrap_honours_an_explicit_ref_and_stops_on_a_failed_download(tmp_path: Path) -> None:
    env = _bootstrap_world(tmp_path, "v1.0-rc1\n")
    pinned = _run("bash", REPO / "docs/landing/install.sh", "--ref", "v1.0-rc1", "--no-web", env=env)
    assert pinned.returncode == 0, pinned.stderr
    assert (tmp_path / "ran").read_text().strip() == "ref=v1.0-rc1 args=--no-web"

    only_pre = _run("bash", REPO / "docs/landing/install.sh", env=env)
    assert only_pre.returncode == 0, only_pre.stderr
    assert (tmp_path / "ran").read_text().startswith("ref=v1.0-rc1")

    bad = _run("bash", REPO / "docs/landing/install.sh", "--ref", "-x", env=env)
    assert bad.returncode != 0

    failing_dir = tmp_path / "failing"
    failing_dir.mkdir()
    failing = _bootstrap_world(failing_dir, "v1.0-rc1\n", download_fails=True)
    failed = _run("bash", REPO / "docs/landing/install.sh", env=failing)
    assert failed.returncode != 0
    assert "nothing was installed" in failed.stderr
    assert not (failing_dir / "ran").exists()
