"""Static checks for the HOST/PORT deployability rule plus a fail-fast check.

These inspect app/main.py's source and, for the PORT-validation case, run it
as a subprocess so the fail-fast behavior is exercised without depending on
whether uvicorn actually manages to bind in this sandbox.
"""

from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
MAIN_PATH = APP_DIR / "app" / "main.py"


class MainModuleSourceTests(unittest.TestCase):
    def test_main_module_exists(self):
        self.assertTrue(MAIN_PATH.exists(), f"expected {MAIN_PATH} to exist")

    def test_host_is_read_from_environment_with_0_0_0_0_default(self):
        source = MAIN_PATH.read_text()
        self.assertRegex(
            source,
            r'os\.(environ\.get|getenv)\(\s*["\']HOST["\']\s*,\s*["\']0\.0\.0\.0["\']',
        )

    def test_port_is_read_from_environment_with_a_numeric_default(self):
        source = MAIN_PATH.read_text()
        self.assertRegex(
            source,
            r'os\.(environ\.get|getenv)\(\s*["\']PORT["\']\s*,\s*["\']?\d+',
        )

    def test_no_hardcoded_loopback_bind_in_server_startup(self):
        source = MAIN_PATH.read_text()
        self.assertNotIn("127.0.0.1", source)


class PortValidationTests(unittest.TestCase):
    def test_non_numeric_port_fails_fast_instead_of_serving(self):
        env = dict(os.environ)
        env["PORT"] = "not-a-number"
        env["HOST"] = "127.0.0.1"
        result = subprocess.run(
            [sys.executable, "-m", "app.main"],
            cwd=str(APP_DIR),
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertNotEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()
