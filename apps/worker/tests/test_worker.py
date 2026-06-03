import subprocess
import sys


def test_worker_starts_and_exits_cleanly():
    result = subprocess.run(
        [sys.executable, "-m", "apps.worker.boss_worker.main", "--once"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert "worker_started" in result.stderr

