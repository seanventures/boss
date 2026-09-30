"""console_origin.py is the public check for --console-origin.

One https origin is printed on stdout. Anything else is one line on stderr
and exit 2. The web listener stays on loopback either way.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "install" / "console_origin.py"


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["python3", str(SCRIPT), *args],
        text=True,
        capture_output=True,
        check=False,
    )


def test_port_443_collapses_and_a_different_port_stays() -> None:
    collapsed = _run("https://console.example:443")
    assert collapsed.returncode == 0
    assert collapsed.stdout == "https://console.example\n"
    assert collapsed.stderr == ""

    kept = _run("  https://console.example:8443/  ")
    assert kept.returncode == 0
    assert kept.stdout == "https://console.example:8443\n"
    assert kept.stderr == ""


def test_http_a_path_and_loopback_are_rejected() -> None:
    http = _run("http://console.example")
    assert http.returncode == 2
    assert http.stdout == ""
    assert "https origin" in http.stderr

    path = _run("https://console.example/mission")
    assert path.returncode == 2
    assert path.stdout == ""
    assert "no path" in path.stderr

    loopback = _run("https://127.0.0.1")
    assert loopback.returncode == 2
    assert loopback.stdout == ""
    assert "loopback" in loopback.stderr

    missing = _run()
    assert missing.returncode == 2
    assert missing.stdout == ""
    assert "pass one https origin" in missing.stderr
