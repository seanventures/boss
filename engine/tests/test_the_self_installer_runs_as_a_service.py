"""THE WATCHER AS A USER SERVICE (BOSSAPP-571 spec §6.5): `boss self-fix watch --install-service`
writes a launchd agent (macOS) or a systemd --user unit (Linux) that runs this install's watcher
at login and keeps it running, and starts it; `--remove-service` undoes it. Nothing secret is in
the unit (the key stays in its 0600 file), and a host that was never enrolled is refused before
anything is written."""
from __future__ import annotations

import os
import plistlib
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "install" / "self_fix_installer.py"


def _run(tmp_path: Path, platform: str, *args: str, key: bool = True):
    install = tmp_path / "install"
    (install / ".boss").mkdir(parents=True, exist_ok=True)
    if key:
        (install / ".boss" / "self-installer.key").write_text("boss_sak_test_key\n")
    ctl_log = tmp_path / "ctl.log"
    ctl = tmp_path / "ctl.sh"
    ctl.write_text(f'#!/bin/sh\necho "$@" >> {ctl_log}\nexit 0\n')
    ctl.chmod(0o755)
    env = {k: v for k, v in os.environ.items() if k != "BOSS_SELF_INSTALLER_KEY"}
    env.update({"BOSS_SELF_FIX_PLATFORM": platform,
                "BOSS_SELF_FIX_SERVICE_DIR": str(tmp_path / "units"),
                "BOSS_SELF_FIX_SERVICE_CTL": str(ctl)})
    done = subprocess.run([sys.executable, str(SCRIPT), "--dir", str(install), "--api",
                           "http://127.0.0.1:8000", "watch", *args], capture_output=True,
                          text=True, env=env, timeout=60)
    calls = ctl_log.read_text().splitlines() if ctl_log.exists() else []
    return done, calls, install


def test_macos_gets_a_launch_agent_that_runs_the_watcher(tmp_path):
    done, calls, install = _run(tmp_path, "darwin", "--install-service")
    assert done.returncode == 0, done.stdout + done.stderr
    unit = tmp_path / "units" / "org.boss.self-fix-watch.plist"
    plist = plistlib.loads(unit.read_bytes())
    assert plist["Label"] == "org.boss.self-fix-watch" and plist["KeepAlive"] is True
    assert plist["ProgramArguments"][-1] == "watch" and str(install) in plist["ProgramArguments"]
    assert "boss_sak" not in unit.read_text(), "the key is never in the unit"
    assert any(c.startswith("launchctl bootstrap gui/") for c in calls), calls
    done, calls, _ = _run(tmp_path, "darwin", "--remove-service")
    assert done.returncode == 0 and not unit.exists()
    assert any(c.startswith("launchctl bootout gui/") for c in calls)


def test_linux_gets_a_systemd_user_unit(tmp_path):
    done, calls, install = _run(tmp_path, "linux", "--install-service")
    assert done.returncode == 0, done.stdout + done.stderr
    unit = (tmp_path / "units" / "boss-self-fix-watch.service").read_text()
    assert "Restart=always" in unit and "WantedBy=default.target" in unit
    assert f"--dir {install}" in unit and unit.rstrip().endswith("WantedBy=default.target")
    assert "systemctl --user enable --now boss-self-fix-watch.service" in calls
    done, calls, _ = _run(tmp_path, "linux", "--service-status")
    assert done.returncode == 0 and "running" in done.stdout


@pytest.mark.parametrize("platform", ["darwin", "linux"])
def test_a_host_that_was_never_enrolled_is_refused_first(tmp_path, platform):
    done, calls, _ = _run(tmp_path, platform, "--install-service", key=False)
    assert done.returncode == 1 and "enroll" in done.stdout
    assert not (tmp_path / "units").exists() and calls == []
