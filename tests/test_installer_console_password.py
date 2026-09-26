"""Console password rotation leaves other credentials intact."""
from __future__ import annotations

import importlib.util
import stat
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/install/console_password.py"
SPEC = importlib.util.spec_from_file_location("console_password", SCRIPT)
assert SPEC and SPEC.loader
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


def test_reset_rotates_only_console_password_and_keeps_private_mode(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("BOSS_API_KEY=untouched\nBOSS_CONSOLE_PASSWORD=" + "a" * 48 + "\n"
                   "BOSS_CONSOLE_SESSION_SECRET=" + "b" * 64 + "\n")
    env.chmod(0o600)

    module.reset(env, module.private_env(env))

    lines = env.read_text().splitlines()
    assert module.password_from(lines) != "a" * 48
    assert len(module.password_from(lines)) == 48
    assert "BOSS_API_KEY=untouched" in lines
    assert "BOSS_CONSOLE_SESSION_SECRET=" + "b" * 64 in lines
    assert stat.S_IMODE(env.stat().st_mode) == 0o600


def test_password_helper_refuses_world_readable_env(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("BOSS_CONSOLE_PASSWORD=" + "a" * 48 + "\n")
    env.chmod(0o644)
    with pytest.raises(SystemExit, match="must be private"):
        module.private_env(env)
