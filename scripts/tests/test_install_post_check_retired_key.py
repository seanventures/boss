"""The installer's post-start check and the upgrade rollback survive the shared key's retirement.

Identity retires the shared BOSS_API_KEY (sys_legacy_console) 30 days after its first use. The
installer's own edition probe used that key, so on 2026-11-08 every upgrade of an older install
died with "Edition probe returned HTTP 401" (authn: legacy_credential_retired), and the rollback
"did not start either" because the previous installer probes the same way. These tests run the
real scripts against a fake API that refuses the retired key exactly like the guard does.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LEGACY = "legacy-console-key-retired"
ISSUED = "boss_sak_" + "f" * 40
STALE = "boss_sak_" + "0" * 40

FAKE_CURL = r'''#!/usr/bin/env python3
import os, sys
args = sys.argv[1:]
url = args[-1]
key = ""
if "@-" in args:
    for line in sys.stdin.read().splitlines():
        if line.lower().startswith("authorization: bearer "):
            key = line.split(" ", 2)[2]
valid = set(filter(None, os.environ.get("FAKE_VALID_KEYS", "").split(",")))
out = args[args.index("-o") + 1] if "-o" in args else os.devnull
def answer(code, body=""):
    with open(out, "w") as stream:
        stream.write(body)
    print(code, end="")
    sys.exit(0)
if "/health" in url:
    answer(200 if os.environ.get("FAKE_API_UP", "1") == "1" else 0, '{"status":"ok"}')
if os.environ.get("FAKE_API_UP", "1") != "1":
    answer("000")
if key == os.environ.get("FAKE_RETIRED_KEY"):
    answer(401, '{"error":"unauthenticated","authn":"legacy_credential_retired"}')
if key not in valid:
    answer(401, '{"error":"unauthenticated"}')
if "/v1/identity/me" in url:
    answer(200, '{"principal":"sva_host_tools"}')
if "/v1/edition" in url:
    answer(200, '{"edition":"community"}')
answer(200, "{}")
'''

# `docker compose version` works; `... exec ... identity.cli host-key --quiet` prints the issued
# key (FAKE_ISSUED_KEY) and makes it valid; every other call is recorded and succeeds.
FAKE_DOCKER = r'''#!/usr/bin/env python3
import os, sys
args = " ".join(sys.argv[1:])
with open(os.environ["FAKE_DOCKER_CALLS"], "a") as stream:
    stream.write(args + "\n")
if " version" in args:
    print("Docker Compose test")
    sys.exit(0)
if "host-key" in args:
    issued = os.environ.get("FAKE_ISSUED_KEY", "")
    if not issued:
        sys.exit(1)
    with open(os.environ["FAKE_VALID_FILE"], "a") as stream:
        stream.write(issued + "\n")
    print("noise from the container")
    print(issued)
sys.exit(0)
'''


def _fakes(tmp_path: Path, *, issued: str, valid: list[str], api_up: bool = True) -> dict[str, str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    valid_file = tmp_path / "valid-keys"
    valid_file.write_text("".join(k + "\n" for k in valid))
    # The fake curl reads the valid set from the file the fake docker appends to.
    curl = bin_dir / "curl"
    curl.write_text(FAKE_CURL.replace(
        'valid = set(filter(None, os.environ.get("FAKE_VALID_KEYS", "").split(",")))',
        'valid = set(open(os.environ["FAKE_VALID_FILE"]).read().split())'))
    curl.chmod(0o700)
    docker = bin_dir / "docker"
    docker.write_text(FAKE_DOCKER)
    docker.chmod(0o700)
    return {
        **os.environ,
        "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"],
        "FAKE_VALID_FILE": str(valid_file),
        "FAKE_RETIRED_KEY": LEGACY,
        "FAKE_ISSUED_KEY": issued,
        "FAKE_API_UP": "1" if api_up else "0",
        "FAKE_DOCKER_CALLS": str(tmp_path / "docker-calls"),
    }


def _install_dir(tmp_path: Path, extra_env: str = "") -> Path:
    install = tmp_path / "install"
    (install / "scripts/install").mkdir(parents=True)
    (install / "scripts/install/community-compose.yml").write_text("name: boss-community\n")
    env_file = install / ".env"
    env_file.write_text(f"BOSS_API_KEY={LEGACY}\nBOSS_API_PORT=49999\n{extra_env}")
    env_file.chmod(0o600)
    return install


def _env_values(install: Path) -> dict[str, str]:
    return dict(line.split("=", 1) for line in (install / ".env").read_text().splitlines()
                if "=" in line)


def _check(install: Path, env: dict[str, str], *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(ROOT / "scripts/install/post_check.sh"), "--dir", str(install),
         "--wait", "2", *extra, "--", "docker", "compose"],
        env=env, capture_output=True, text=True, timeout=60, check=False)


def _no_secret(result: subprocess.CompletedProcess) -> None:
    rendered = result.stdout + result.stderr
    for secret in (LEGACY, ISSUED, STALE):
        assert secret not in rendered


def test_retired_shared_key_and_no_host_key_issues_one_and_passes(tmp_path: Path) -> None:
    install = _install_dir(tmp_path)
    env = _fakes(tmp_path, issued=ISSUED, valid=[])
    result = _check(install, env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "GET /v1/edition → 200 (host tools key)" in result.stdout
    assert "Edition reports community" in result.stdout
    assert _env_values(install)["BOSS_HOST_API_KEY"] == ISSUED
    assert _env_values(install)["BOSS_API_KEY"] == LEGACY          # never un-retired, never moved
    assert oct((install / ".env").stat().st_mode & 0o777) == "0o600"
    calls = (tmp_path / "docker-calls").read_text()
    assert "exec -T -u boss api python -m boss_engine.identity.cli host-key --quiet" in calls
    _no_secret(result)


def test_working_host_key_is_kept_and_nothing_is_issued(tmp_path: Path) -> None:
    install = _install_dir(tmp_path, f"BOSS_HOST_API_KEY={ISSUED}\n")
    env = _fakes(tmp_path, issued="", valid=[ISSUED])
    result = _check(install, env)
    assert result.returncode == 0, result.stdout + result.stderr
    calls = tmp_path / "docker-calls"
    assert not calls.exists() or "host-key" not in calls.read_text()
    assert _env_values(install)["BOSS_HOST_API_KEY"] == ISSUED
    _no_secret(result)


def test_refused_host_key_is_issued_again(tmp_path: Path) -> None:
    install = _install_dir(tmp_path, f"BOSS_HOST_API_KEY={STALE}\n")
    env = _fakes(tmp_path, issued=ISSUED, valid=[])
    result = _check(install, env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert _env_values(install)["BOSS_HOST_API_KEY"] == ISSUED
    _no_secret(result)


def test_retired_key_without_any_host_key_fails_and_names_the_fix(tmp_path: Path) -> None:
    install = _install_dir(tmp_path)
    env = _fakes(tmp_path, issued="", valid=[])
    result = _check(install, env)
    assert result.returncode == 1
    assert "retired" in result.stderr and "boss identity host-key" in result.stderr
    _no_secret(result)


def test_api_down_fails_on_health(tmp_path: Path) -> None:
    install = _install_dir(tmp_path)
    env = _fakes(tmp_path, issued=ISSUED, valid=[], api_up=False)
    result = _check(install, env)
    assert result.returncode == 1
    assert "/health" in result.stderr


def test_install_sh_never_probes_with_the_shared_key() -> None:
    """Every authenticated call install.sh makes after start uses the checked key."""
    source = (ROOT / "install.sh").read_text()
    after_start = source.split('ui_ok "Containers started"', 1)[1]
    assert "scripts/install/post_check.sh" in after_start
    assert "Bearer ${BOSS_API_KEY}" not in after_start
    assert 'BOSS_API_KEY="$BOSS_API_KEY"' not in after_start
    assert "grep -m 1 '^BOSS_API_KEY='" not in after_start


# ---- the upgrade rollback ----------------------------------------------------------------------

def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=Upgrade Test",
         "-c", "user.email=upgrade@example.invalid", *args],
        check=True, capture_output=True, text=True).stdout.strip()


def _upgrade_fixture(tmp_path: Path, *, new_has_check: bool) -> tuple[Path, str]:
    """A git install whose old and new installers both die at their own final check."""
    install = tmp_path / "install"
    helpers = install / "scripts/install"
    helpers.mkdir(parents=True)
    (helpers / "community-compose.yml").write_text("name: boss-community\n")
    (helpers / "backup.sh").write_text("#!/bin/sh\nexit 0\n")
    failing = ("#!/usr/bin/env bash\n"
               "echo 'Edition probe returned HTTP 401. BOSS was started but is not ready' >&2\n"
               "exit 1\n")
    (install / "install.sh").write_text(failing)
    subprocess.run(["git", "init", "-q", "-b", "main", str(install)], check=True)
    _git(install, "add", ".")
    _git(install, "commit", "-qm", "old release")
    old = _git(install, "rev-parse", "HEAD")
    _git(install, "remote", "add", "origin", str(install))
    if new_has_check:
        shutil.copy2(ROOT / "scripts/install/post_check.sh", helpers / "post_check.sh")
    # The new installer moves the checkout forward (as the real one does), then fails.
    (install / "install.sh").write_text(failing.replace(
        "exit 1\n", 'git -C "$2" reset -q --hard "$4"\nexit 1\n').replace("401", "500"))
    _git(install, "add", ".")
    _git(install, "commit", "-qm", "new release")
    _git(install, "branch", "-q", "next")
    _git(install, "reset", "-q", "--hard", old)
    env_file = install / ".env"
    env_file.write_text(f"BOSS_API_KEY={LEGACY}\nBOSS_API_PORT=49999\n")
    env_file.chmod(0o600)
    return install, old


def _upgrade(install: Path, env: dict[str, str]) -> subprocess.CompletedProcess:
    env = {**env, "BOSS_ROLLBACK_CHECK_WAIT": "2"}
    return subprocess.run(
        ["bash", str(ROOT / "scripts/install/upgrade.sh"), "--dir", str(install), "--ref", "next"],
        env=env, capture_output=True, text=True, timeout=120, check=False)


def test_rollback_after_a_failed_post_check_is_verified_with_the_host_key(tmp_path: Path) -> None:
    install, old = _upgrade_fixture(tmp_path, new_has_check=True)
    env = _fakes(tmp_path, issued=ISSUED, valid=[])
    result = _upgrade(install, env)
    assert result.returncode == 1                                   # the upgrade itself failed
    assert _git(install, "rev-parse", "HEAD") == old                # the previous source is back
    assert "Rolled back:" in result.stderr, result.stderr
    assert "verified with next's check" in result.stderr
    assert "did not start either" not in result.stderr
    assert _env_values(install)["BOSS_HOST_API_KEY"] == ISSUED
    _no_secret(result)


def test_rollback_that_really_is_down_still_says_so(tmp_path: Path) -> None:
    install, old = _upgrade_fixture(tmp_path, new_has_check=True)
    env = _fakes(tmp_path, issued=ISSUED, valid=[], api_up=False)
    result = _upgrade(install, env)
    assert result.returncode == 1
    assert _git(install, "rev-parse", "HEAD") == old
    assert "did not start either" in result.stderr
    _no_secret(result)
