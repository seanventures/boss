from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest


def test_setup_all_passes_api_key_to_children_without_argv(monkeypatch: pytest.MonkeyPatch) -> None:
    path = Path(__file__).resolve().parents[1] / "examples" / "setup_all.py"
    spec = importlib.util.spec_from_file_location("boss_examples_setup_all", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    calls: list[tuple[list[str], dict[str, str]]] = []

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        env = kwargs["env"]
        assert isinstance(env, dict)
        calls.append((command, env))
        return subprocess.CompletedProcess(command, 0, stdout="would create workflow\n")

    monkeypatch.setenv("BOSS_API_KEY", "test-only-secret")
    monkeypatch.setattr(module.subprocess, "run", fake_run)
    monkeypatch.setattr(
        sys,
        "argv",
        ["setup_all.py", "--only", "video-workflow", "--dry-run"],
    )

    with pytest.raises(SystemExit) as exited:
        module.main()

    assert exited.value.code == 0
    assert len(calls) == 1
    command, env = calls[0]
    assert "--key" not in command
    assert "test-only-secret" not in command
    assert env["BOSS_API_KEY"] == "test-only-secret"
