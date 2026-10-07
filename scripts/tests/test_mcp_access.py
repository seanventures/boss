"""Community MCP first-run credential setup without exposing owner authority."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts/install"))
import mcp_access  # noqa: E402
from reconcile_api_credentials import _principal_map, _values  # noqa: E402


def _env(tmp_path: Path) -> Path:
    path = tmp_path / ".env"
    path.write_text(
        "BOSS_API_KEY=owner-key\n"
        "BOSS_WORKER_API_KEY=worker-key\n"
        "BOSS_API_KEYS=owner-key,worker-key,existing-key\n"
        "BOSS_API_KEY_PRINCIPALS=owner-key:ceo,worker-key:system,existing-key:reader\n"
        "BOSS_DEFAULT_PROJECT_ID=alpha\n"
    )
    return path


SAK = "boss_sak_" + "A" * 43


def _minter(calls):
    def mint(env_path, project, permissions):
        calls.append((project, permissions))
        return SAK + str(len(calls))
    return mint


def test_setup_issues_a_project_service_account_key_and_keeps_other_keys(tmp_path):
    """make mcp-setup asks the host shell for a service-account key pinned to the project; it no
    longer creates a member with the owner key (which loses that right at the claim, audit U4)."""
    path = _env(tmp_path)
    calls = []
    changed, restart = mcp_access.configure(path, "alpha", mint=_minter(calls))
    values = _values(path.read_text().splitlines())
    assert changed and not restart
    assert values["BOSS_MCP_API_KEY"] == SAK + "1"
    assert values["BOSS_MCP_PROJECT_ID"] == "alpha"
    assert values["BOSS_API_KEYS"] == "owner-key,worker-key,existing-key"     # untouched
    assert calls == [("alpha", None)]
    assert os.stat(path).st_mode & 0o777 == 0o600
    # a rerun keeps the key and issues nothing
    assert mcp_access.configure(path, "alpha", mint=_minter(calls)) == (False, False)
    assert len(calls) == 1


def test_setup_replaces_an_rc5_key_and_unbinds_it(tmp_path):
    path = _env(tmp_path)
    path.write_text(path.read_text().replace(
        "existing-key:reader\n", "existing-key:reader,old-mcp:mcp_operator\n",
    ).replace("existing-key\n", "existing-key,old-mcp\n")
        + "BOSS_MCP_API_KEY=old-mcp\nBOSS_MCP_PROJECT_ID=alpha\n")
    changed, restart = mcp_access.configure(path, "alpha", mint=_minter([]))
    values = _values(path.read_text().splitlines())
    assert changed and restart                       # the API reloads .env to end the old key
    assert values["BOSS_MCP_API_KEY"].startswith("boss_sak_")
    assert "old-mcp" not in values["BOSS_API_KEYS"].split(",")
    assert "old-mcp" not in _principal_map(values["BOSS_API_KEY_PRINCIPALS"])


def test_setup_never_rebinds_a_preexisting_unbound_api_key(tmp_path):
    path = _env(tmp_path)
    path.write_text(path.read_text() + "BOSS_MCP_API_KEY=legacy-mcp-key\n")
    before = path.read_bytes()
    with pytest.raises(mcp_access.MCPSetupError, match="not bound"):
        mcp_access.configure(path, "alpha", mint=lambda *a: pytest.fail("must not issue"))
    assert path.read_bytes() == before


def test_setup_rejects_a_second_project_before_issuing(tmp_path):
    path = _env(tmp_path)
    calls = []
    mcp_access.configure(path, "alpha", mint=_minter(calls))
    before = path.read_bytes()
    with pytest.raises(mcp_access.MCPSetupError, match="another project"):
        mcp_access.configure(path, "beta", mint=_minter(calls))
    assert path.read_bytes() == before and len(calls) == 1


def test_setup_refuses_what_boss_did_not_issue(tmp_path):
    path = _env(tmp_path)
    before = path.read_bytes()
    with pytest.raises(mcp_access.MCPSetupError):
        mcp_access.configure(path, "alpha", mint=lambda *a: "not-a-boss-key")
    assert path.read_bytes() == before


def test_make_setup_rerun_reuses_key_and_skips_api_restart(monkeypatch, tmp_path):
    path = _env(tmp_path)
    calls = []
    monkeypatch.setattr(mcp_access, "_mint", _minter(calls))
    restarts = []
    monkeypatch.setattr(mcp_access, "_restart_api", lambda env: restarts.append(env))
    monkeypatch.setattr(sys, "argv", ["mcp_access", "setup", "--env-file", str(path),
                                     "--restart-api"])
    assert mcp_access.main() == 0
    first = _values(path.read_text().splitlines())["BOSS_MCP_API_KEY"]
    assert mcp_access.main() == 0
    second = _values(path.read_text().splitlines())["BOSS_MCP_API_KEY"]
    assert first == second and restarts == [] and len(calls) == 1


def test_the_minted_key_is_asked_of_the_host_shell_as_the_boss_user(monkeypatch, tmp_path):
    path = _env(tmp_path)
    seen = {}
    monkeypatch.setattr(mcp_access, "_docker_prefix", lambda: ["docker"])

    def fake_run(args, **kwargs):
        seen["args"] = args
        return type("Result", (), {"returncode": 0, "stdout": SAK + "\n", "stderr": ""})()
    monkeypatch.setattr(mcp_access.subprocess, "run", fake_run)
    assert mcp_access._mint(path, "alpha", ["agent_chat"]) == SAK
    args = seen["args"]
    assert args[args.index("exec"):args.index("api")] == ["exec", "-T", "-u", "boss"]
    assert args[-6:] == ["mcp-key", "--project", "alpha", "--quiet", "--permission", "agent_chat"]


def test_mcp_stdio_command_forwards_only_separate_key_in_environment(monkeypatch, tmp_path):
    path = _env(tmp_path)
    with pytest.raises(mcp_access.MCPSetupError, match="not pinned"):
        mcp_access.launch(path, "alpha")
    path.write_text(path.read_text().replace(
        "existing-key:reader\n",
        "existing-key:reader,dedicated-secret:mcp_operator\n",
    ) + "BOSS_MCP_API_KEY=dedicated-secret\nBOSS_MCP_PROJECT_ID=alpha\n")
    monkeypatch.setattr(mcp_access.shutil, "which", lambda name: "/usr/bin/docker")
    monkeypatch.setattr(mcp_access.subprocess, "run", lambda *args, **kwargs: type(
        "Result", (), {"returncode": 0}
    )())
    captured = {}
    monkeypatch.setattr(mcp_access.os, "execvpe", lambda name, args, env: captured.update(
        name=name, args=args, env=env
    ))
    mcp_access.launch(path, "alpha")
    assert captured["name"] == "docker"
    assert "dedicated-secret" not in " ".join(captured["args"])
    assert captured["env"]["BOSS_MCP_API_KEY"] == "dedicated-secret"
    assert "-u" in captured["args"] and "boss" in captured["args"]
    assert captured["args"][-5:] == [
        "--project", "alpha", "serve-mcp", "--api-url", "http://127.0.0.1:8000",
    ]
    with pytest.raises(mcp_access.MCPSetupError, match="not pinned"):
        mcp_access.launch(path, "beta")


def test_mcp_stdio_uses_sudo_only_when_key_preservation_is_verified(monkeypatch, tmp_path):
    path = _env(tmp_path)
    path.write_text(path.read_text().replace(
        "existing-key:reader\n", "existing-key:reader,dedicated-secret:mcp_operator\n",
    ) + "BOSS_MCP_API_KEY=dedicated-secret\nBOSS_MCP_PROJECT_ID=alpha\n")
    monkeypatch.setattr(mcp_access.shutil, "which", lambda name: f"/usr/bin/{name}")
    def fake_run(args, **kwargs):
        if args == ["docker", "info"]:
            return type("Result", (), {"returncode": 1})()
        if args[-1] == "env":
            return type("Result", (), {"returncode": 0,
                                       "stdout": "BOSS_MCP_API_KEY=dedicated-secret\n"})()
        return type("Result", (), {"returncode": 0})()
    monkeypatch.setattr(mcp_access.subprocess, "run", fake_run)
    captured = {}
    monkeypatch.setattr(mcp_access.os, "execvpe", lambda name, args, env: captured.update(
        name=name, args=args, env=env,
    ))
    mcp_access.launch(path, "alpha")
    assert captured["name"] == "sudo"
    assert captured["args"][:4] == [
        "sudo", "-n", "--preserve-env=BOSS_MCP_API_KEY", "docker",
    ]
    assert "dedicated-secret" not in " ".join(captured["args"])


def test_installer_rerun_preserves_dedicated_mcp_key_and_bindings(tmp_path):
    old = _env(tmp_path)
    old.write_text(old.read_text().replace(
        "existing-key:reader\n", "existing-key:reader,mcp-key:mcp_operator\n",
    ).replace(
        "existing-key\n", "existing-key,mcp-key\n",
    ) + "BOSS_MCP_API_KEY=mcp-key\nBOSS_MCP_PROJECT_ID=alpha\n")
    new = tmp_path / "new.env"
    new.write_text(
        "BOSS_API_KEY=owner-key\nBOSS_WORKER_API_KEY=new-worker\n"
        "BOSS_API_KEYS=owner-key,new-worker\n"
        "BOSS_API_KEY_PRINCIPALS=owner-key:ceo,new-worker:system\n"
    )
    result = subprocess.run(
        ["sh", str(ROOT / "scripts/install/restore_preserved_env.sh"), str(old), str(new)],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    values = _values(new.read_text().splitlines())
    assert values["BOSS_MCP_API_KEY"] == "mcp-key"
    assert values["BOSS_MCP_PROJECT_ID"] == "alpha"
    assert "mcp-key" in values["BOSS_API_KEYS"].split(",")
    assert _principal_map(values["BOSS_API_KEY_PRINCIPALS"])["mcp-key"] == "mcp_operator"
