"""`remote.sh --dir '~/boss'` means the SSH user's home ON THE HOST.

Every remote command single-quotes the install dir, so a literal "~/boss" reached the host and
was never expanded: smoke reported "no BOSS_API_KEY in ~/boss/.env" against a healthy install
and skipped its authenticated check, and install or uninstall would have used the wrong path.
Found while documenting the laptop-to-server deploy in the README (2026-09-30).

A stand-in `ssh` runs each "remote" command locally, with HOME pointed at a temp directory
playing the host's home, so the real script is exercised without a server.
"""
from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REMOTE = ROOT / "scripts" / "install" / "remote.sh"

FAKE_SSH = """#!/usr/bin/env bash
# ssh [options] HOST COMMAND... -> run COMMAND locally as if on the host.
while [[ $# -gt 0 ]]; do
  case "$1" in
    -o|-i|-p|-l) shift 2 ;;
    -t|-T|-q|-N) shift ;;
    *) break ;;
  esac
done
shift  # the host
echo "$*" >> "$SSH_LOG"
HOME="$FAKE_HOST_HOME" exec bash -c "$*"
"""


def _run(tmp_path: Path, dir_arg: str, command: str = "env") -> tuple[subprocess.CompletedProcess, str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    host_home = tmp_path / "hosthome"
    (host_home / "boss").mkdir(parents=True)
    for name, body in (("ssh", FAKE_SSH),
                       ("docker", "#!/usr/bin/env bash\nexit 0\n"),
                       ("curl", "#!/usr/bin/env bash\nexit 7\n")):
        path = bin_dir / name
        path.write_text(body)
        path.chmod(path.stat().st_mode | stat.S_IEXEC)
    log = tmp_path / "ssh.log"
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}",
           "SSH_LOG": str(log), "FAKE_HOST_HOME": str(host_home)}
    result = subprocess.run(["bash", str(REMOTE), "--host", "fake-host", "--dir", dir_arg, command],
                            env=env, text=True, capture_output=True, timeout=60)
    return result, log.read_text() if log.exists() else ""


def test_a_tilde_dir_becomes_the_hosts_home(tmp_path: Path) -> None:
    result, log = _run(tmp_path, "~/boss")
    resolved = str(tmp_path / "hosthome" / "boss")
    assert resolved in log, log
    assert "'~/boss" not in log, "a literal ~/boss still reached the host"
    assert "~/boss" not in result.stdout + result.stderr


def test_an_absolute_dir_is_left_alone(tmp_path: Path) -> None:
    _, log = _run(tmp_path, "/srv/boss")
    assert "/srv/boss" in log
    assert 'printf %s "$HOME"' not in log, "no home lookup is needed for an absolute path"
