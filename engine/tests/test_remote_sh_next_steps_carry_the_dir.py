"""The next-step commands remote.sh prints must reach the install they describe.

After `remote.sh --dir '~/boss' install`, every printed follow-up (smoke, tunnel, subscription,
env) left out --dir, so pasting one went to the default /opt/boss and reported "no BOSS_API_KEY"
against a healthy install. Found installing on a tailnet VM (2026-09-30). This runs the `again`
helper exactly as remote.sh defines it.
"""
import re
import subprocess
from pathlib import Path

REMOTE = Path(__file__).resolve().parents[2] / "scripts" / "install" / "remote.sh"


def _again(**variables: str) -> str:
    match = re.search(r"^again\(\) \{\n.*?^\}$", REMOTE.read_text(), re.MULTILINE | re.DOTALL)
    assert match, "remote.sh no longer defines again()"
    assigns = "".join(f"{name}='{value}'\n" for name, value in variables.items())
    result = subprocess.run(["bash", "-c", f"set -euo pipefail\n{assigns}{match.group(0)}\nagain"],
                            capture_output=True, text=True, check=True)
    return result.stdout


def test_a_non_default_dir_is_carried():
    assert _again(HOST="vm", KEY="", DIR="/home/dev/boss") == "remote.sh --host vm --dir '/home/dev/boss'"


def test_the_default_dir_and_a_key():
    assert _again(HOST="vm", KEY="~/.ssh/k", DIR="/opt/boss") == "remote.sh --host vm --key ~/.ssh/k"


def test_every_printed_follow_up_uses_it():
    text = REMOTE.read_text()
    assert "remote.sh --host $HOST${KEY:+ --key $KEY} " not in text.replace(
        'local cmd="remote.sh --host $HOST${KEY:+ --key $KEY}"', "")
