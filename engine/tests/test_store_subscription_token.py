"""store_subscription_token.py is retired (BOSSAPP-769): it reads nothing, sends nothing, and
says where a subscription is connected now — signed in inside BOSS through its own login.

It used to copy a Codex auth file or a Claude setup token to POST
/v1/setup/coding-agents/credential, which refused every import, so its users saw `None` and a
--accept-database-fallback hint that looped.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "store_subscription_token.py"


@pytest.mark.parametrize("argv, command", [
    (["codex"], "boss connect codex"),
    (["claude_code", "--stdin"], "boss connect claude"),
    (["grok"], "boss connect grok"),
    ([], "boss connect codex|claude|grok"),
])
def test_it_points_at_the_sign_in_and_touches_nothing(tmp_path, argv, command):
    if not SCRIPT.exists():  # pragma: no cover - engine installed standalone
        pytest.skip("scripts/ not present in this checkout")
    secret = "sk-ant-oat01-must-never-be-read-or-sent"
    result = subprocess.run([sys.executable, str(SCRIPT), *argv], input=secret, text=True,
                            capture_output=True, timeout=30, cwd=tmp_path,
                            env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)})
    assert result.returncode == 2
    assert command in result.stderr
    assert "Nothing was read or sent" in result.stderr
    assert secret not in result.stdout + result.stderr
    assert "None" not in result.stderr
    assert list(tmp_path.iterdir()) == []


def test_it_has_no_way_to_reach_boss():
    text = SCRIPT.read_text()
    assert "urllib" not in text and "coding-agents" not in text
