"""An upgrade must survive a .env that predates a key the installer now reads.

install.sh runs under `set -euo pipefail`. Its `_existing` helper greps .env for a key; the first
upgrade after BOSS_KMS_MASTER_KEY was added found no such line, the grep failed the pipeline, and
the installer stopped after "Reusing the existing API key" with no message. Found on a real
laptop re-install. This runs the helper exactly as install.sh defines it.
"""
import re
import subprocess
from pathlib import Path

INSTALL_SH = Path(__file__).resolve().parents[2] / "install.sh"


def _existing_definition() -> str:
    match = re.search(r"^\s*_existing\(\) \{.*\}$", INSTALL_SH.read_text(), re.MULTILINE)
    assert match, "install.sh no longer defines _existing"
    return match.group(0).strip()


def _run(env_text: str, key: str, tmp_path: Path) -> subprocess.CompletedProcess:
    env_file = tmp_path / ".env"
    env_file.write_text(env_text)
    script = (
        "set -euo pipefail\n"
        f'ENV_FILE="{env_file}"\n'
        f"{_existing_definition()}\n"
        f'value="$(_existing {key})"\n'
        'echo "reached:[$value]"\n'
    )
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True)


def test_a_missing_key_reads_as_empty_and_the_installer_carries_on(tmp_path):
    result = _run("BOSS_API_KEY=abc\n", "BOSS_KMS_MASTER_KEY", tmp_path)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "reached:[]"


def test_a_present_key_is_read_whole(tmp_path):
    result = _run("BOSS_KMS_MASTER_KEY=a=b-c_d\n", "BOSS_KMS_MASTER_KEY", tmp_path)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "reached:[a=b-c_d]"
