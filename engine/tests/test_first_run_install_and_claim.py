"""THE FIRST RUN A STRANGER SEES, from the install to the claim (stranger test, rc10.27).

  - The installer's final card leads with the claim (`boss identity setup-link`, open the link,
    create the Owner), then Setup. `boss open` and `boss doctor` print the same words, from one
    helper (scripts/install/next_step.sh).
  - A second install on the same machine records its Compose project name (and its boss command
    directory) in its own .env, and `boss` reads it from there, so it never acts on the first
    install. The first install's `boss` command is not taken over.
  - `boss doctor` says "BOSS is running, N warnings to look at" when there are warnings, explains
    the drift report it could not run instead of warning about it, and its SSH tunnel names the
    previews port.

The installer runs for real, against the fake Docker, curl and git origin of the Community CLI
tests (no daemon, no network).
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from .test_community_boss_cli import _docker_calls, _install, _run, world  # noqa: F401

REPO = Path(__file__).resolve().parents[2]
NEXT_STEP = REPO / "scripts/install/next_step.sh"
CHECKUP = REPO / "scripts/install/checkup.sh"


def _next(*args: str) -> str:
    out = subprocess.run(["bash", str(NEXT_STEP), *args], capture_output=True, text=True, check=True)
    return out.stdout


# ---- the next step, one set of words -------------------------------------------------------

def test_an_unclaimed_boss_leads_with_the_claim_then_setup() -> None:
    out = _next("--claim", "unclaimed", "--setup-url", "http://127.0.0.1:3000/?area=setup")
    assert "Next: claim this BOSS (nobody owns it yet)" in out
    order = ["boss identity setup-link", "Open the one-hour link", "Create the Owner account",
             "Then finish setup", "http://127.0.0.1:3000/?area=setup"]
    assert [out.index(w) for w in order] == sorted(out.index(w) for w in order)
    assert "password" not in out


def test_a_claimed_boss_goes_straight_to_setup() -> None:
    out = _next("--claim", "claimed 2026-11-01; shared credentials stop 2026-12-01",
                "--setup-url", "http://127.0.0.1:3000/?area=setup", "--boss", "/opt/bin/boss")
    assert "setup-link" not in out
    assert "Finish setup" in out and "/opt/bin/boss password" in out


def test_the_named_boss_command_is_the_one_printed() -> None:
    out = _next("--claim", "unclaimed", "--setup-url", "u", "--boss", "bash /srv/b2/scripts/install/boss.sh")
    assert "bash /srv/b2/scripts/install/boss.sh identity setup-link" in out


# ---- the installer's card -----------------------------------------------------------------

def test_the_install_card_leads_with_the_claim(world) -> None:  # noqa: F811
    env = {**world["env"], "BOSS_TEST_CLAIM": "unclaimed"}
    result = _run("bash", REPO / "install.sh", "--dir", world["install"], "--yes",
                  "--no-install-deps", env=env, timeout=300)
    out = result.stdout
    assert result.returncode == 0, out + result.stderr
    card = out[out.index("BOSS Community is running"):]
    assert "Next: claim this BOSS (nobody owns it yet)" in card
    assert card.index("identity setup-link") < card.index("Then finish setup") < card.index("?area=setup")
    assert "Password " not in card            # the shared password is not how a fresh BOSS is claimed
    nxt = card[card.index("Next\n"):] if "Next\n" in card else card
    assert nxt.index("identity setup-link") < nxt.index(" open ")


# ---- two installs on one machine ----------------------------------------------------------

def _env_lines(install: Path) -> dict[str, str]:
    return dict(line.split("=", 1) for line in (install / ".env").read_text().splitlines()
                if "=" in line and not line.startswith("#"))


def test_the_project_name_and_bin_dir_are_saved_and_boss_uses_them(world) -> None:  # noqa: F811
    bin_dir = world["tmp"] / "second-bin"
    env = {**world["env"], "COMPOSE_PROJECT_NAME": "boss-second", "BOSS_BIN_DIR": str(bin_dir)}
    result = _install(world, env=env)
    assert result.returncode == 0, result.stdout + result.stderr
    saved = _env_lines(world["install"])
    assert saved["COMPOSE_PROJECT_NAME"] == "boss-second"
    assert saved["BOSS_BIN_DIR"] == str(bin_dir)

    # Later: no name exported (or a different one in the shell). boss acts on THIS install's project.
    tags = Path(world["env"]["BOSS_TEST_DOCKER_LOG"] + ".tags")
    for shell in ({}, {"COMPOSE_PROJECT_NAME": "boss-community"}):
        tags.write_text("")
        plain = {k: v for k, v in world["env"].items() if k != "COMPOSE_PROJECT_NAME"}
        status = _run(bin_dir / "boss", "status", env={**plain, **shell})
        assert status.returncode == 0, status.stdout + status.stderr
        compose = [line for line in tags.read_text().splitlines() if line]
        assert compose and all(line.startswith("boss-second ") for line in compose), compose

    # A rerun without the variables keeps them.
    rerun = _install(world, env={k: v for k, v in world["env"].items() if k != "BOSS_BIN_DIR"})
    assert rerun.returncode == 0, rerun.stdout + rerun.stderr
    again = _env_lines(world["install"])
    assert again["COMPOSE_PROJECT_NAME"] == "boss-second" and again["BOSS_BIN_DIR"] == str(bin_dir)


def test_a_bad_project_name_is_refused(world) -> None:  # noqa: F811
    result = _install(world, env={**world["env"], "COMPOSE_PROJECT_NAME": "Bad Name;rm"})
    assert result.returncode != 0
    assert "COMPOSE_PROJECT_NAME must be lowercase" in result.stdout + result.stderr


def test_a_second_install_never_takes_over_the_first_ones_boss_command(world) -> None:  # noqa: F811
    home_bin = world["home"] / ".local/bin"
    base = {k: v for k, v in world["env"].items() if k != "BOSS_BIN_DIR"}
    base["PATH"] = f"{home_bin}{os.pathsep}{base['PATH']}"
    first = _install(world, env=base)
    assert first.returncode == 0, first.stdout + first.stderr
    launcher = home_bin / "boss"
    assert str(world["install"].resolve()) in launcher.read_text()

    second_dir = world["tmp"] / "boss-two"
    second = _run("bash", REPO / "install.sh", "--dir", second_dir, "--yes", "--no-install-deps",
                  "--no-web", env={**base, "COMPOSE_PROJECT_NAME": "boss-two"}, timeout=300)
    out = second.stdout + second.stderr
    assert second.returncode == 0, out
    assert str(world["install"].resolve()) in launcher.read_text()     # still the first install
    assert "left it alone" in out and f"bash {second_dir}/scripts/install/boss.sh" in out


# ---- boss doctor ----------------------------------------------------------------------------

FAKE_DOCKER = r"""#!/usr/bin/env bash
case "$1" in
  info) exit 0 ;;
  ps) name="$(printf '%s' "$*" | sed -n 's/.*name=\([^ ]*\).*/\1/p')"; echo "${name}1"; exit 0 ;;
  exec)
    case "$*" in
      *NEXT_PUBLIC_BOSS_API_URL*) echo "http://127.0.0.1:8000" ;;
      *claim-status*) echo "${FAKE_CLAIM:-}" ;;
      *) exit 1 ;;
    esac ;;
  *) exit 0 ;;
esac
"""
FAKE_CURL = r"""#!/usr/bin/env bash
for a in "$@"; do
  case "$a" in */health) exit 0 ;; */v1/settings*) echo '{"connected_engines":["codex"]}'; exit 0 ;; esac
done
exit 7
"""


@pytest.fixture()
def doctor(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (("docker", FAKE_DOCKER), ("curl", FAKE_CURL)):
        (bin_dir / name).write_text(body)
        (bin_dir / name).chmod(0o755)
    env_file = tmp_path / ".env"
    env_file.write_text("BOSS_COMMUNITY_WEB_ENABLED=1\nBOSS_PREVIEW_PORT=3101\n")
    base = {"PATH": f"{bin_dir}:/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
            "HOME": str(tmp_path), "BOSS_ENV_FILE": str(env_file), "TMPDIR": str(tmp_path)}

    def run(**extra: str) -> subprocess.CompletedProcess:
        return subprocess.run(["bash", str(CHECKUP)], capture_output=True, text=True,
                              env={**base, **extra}, timeout=120)
    return run


def test_doctor_counts_its_warnings_instead_of_saying_ready(doctor) -> None:
    out = doctor(FAKE_CLAIM="unclaimed").stdout
    assert "BOSS is ready" not in out
    warns = out.count("  \x1b[33mwarn\x1b[0m  ")
    assert warns >= 1
    word = "1 warning" if warns == 1 else f"{warns} warnings"
    assert f"BOSS is running\x1b[0m, \x1b[33m{word} to look at" in out


def test_doctor_names_the_next_step_the_card_names(doctor) -> None:
    out = doctor(FAKE_CLAIM="unclaimed").stdout
    tail = out[out.index("BOSS is running"):]
    assert "Next: claim this BOSS (nobody owns it yet)" in tail
    assert tail.index("boss identity setup-link") < tail.index("Then finish setup")
    claimed = doctor(FAKE_CLAIM="claimed 2026-11-01").stdout
    assert "Next: claim" not in claimed


def test_doctor_never_warns_about_a_drift_report_it_cannot_run(doctor) -> None:
    out = doctor().stdout
    assert "could not compare against a stock install" not in out
    # On a host whose python3 lacks BOSS's packages it says why, as a note, not a warning;
    # with them, it reports the drift.
    assert ("note  config: drift report skipped" in out) or ("config:" in out and "stock" in out)


def test_the_ssh_tunnel_hint_includes_the_previews_port(doctor) -> None:
    out = doctor(SSH_CONNECTION="10.0.0.2 5000 10.0.0.1 22").stdout
    assert "ssh -L 3000:127.0.0.1:3000 -L 3101:127.0.0.1:3101 -L 8000:127.0.0.1:8000" in out
