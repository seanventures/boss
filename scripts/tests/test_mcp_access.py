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


def test_setup_preserves_existing_keys_and_creates_dedicated_member(monkeypatch, tmp_path):
    path = _env(tmp_path)
    requests = []

    def fake_request(origin, key, method, route, body=None):
        requests.append((origin, key, method, route, body))
        if route == "/v1/projects":
            return 200, {"items": [{"id": "alpha"}, {"id": "beta"}]}
        if method == "GET":
            return 404, {}
        return 201, {"status": "active"}

    monkeypatch.setattr(mcp_access, "_request", fake_request)
    mcp_access.configure(path, "alpha")
    values = _values(path.read_text().splitlines())
    key = values["BOSS_MCP_API_KEY"]
    assert key not in {"owner-key", "worker-key", "existing-key"}
    assert set(values["BOSS_API_KEYS"].split(",")) == {
        "owner-key", "worker-key", "existing-key", key,
    }
    assert _principal_map(values["BOSS_API_KEY_PRINCIPALS"]) == {
        "owner-key": "ceo", "worker-key": "system", "existing-key": "reader",
        key: "mcp_operator",
    }
    assert os.stat(path).st_mode & 0o777 == 0o600
    assert requests[-1][-1]["permissions"] == list(mcp_access.DEFAULT_PERMISSIONS)
    assert all(entry[1] == "owner-key" for entry in requests)

    # Re-running setup reuses the same key and does not overwrite the member.
    requests.clear()
    monkeypatch.setattr(mcp_access, "_request", lambda origin, key, method, route, body=None: (
        (200, {"items": [{"id": "alpha"}]}) if route == "/v1/projects" else
        (200, {"status": "active", "member_type": "system", "role_keys": ["mcp_operator"]})
    ))
    mcp_access.configure(path, "alpha")
    assert _values(path.read_text().splitlines())["BOSS_MCP_API_KEY"] == key


def test_setup_fails_closed_when_project_is_not_visible(monkeypatch, tmp_path):
    path = _env(tmp_path)
    before = path.read_bytes()
    monkeypatch.setattr(mcp_access, "_request", lambda *args, **kwargs: (
        200, {"items": [{"id": "alpha"}]}
    ))
    with pytest.raises(mcp_access.MCPSetupError, match="Selected project"):
        mcp_access.configure(path, "other")
    assert path.read_bytes() == before


def test_setup_never_rebinds_a_preexisting_unbound_api_key(monkeypatch, tmp_path):
    path = _env(tmp_path)
    path.write_text(path.read_text().replace(
        "BOSS_API_KEYS=owner-key,worker-key,existing-key",
        "BOSS_API_KEYS=owner-key,worker-key,existing-key,legacy-mcp-key",
    ) + "BOSS_MCP_API_KEY=legacy-mcp-key\n")
    before = path.read_bytes()
    monkeypatch.setattr(mcp_access, "_request", lambda *args, **kwargs: pytest.fail(
        "Unsafe key should fail before changing the API member"
    ))
    with pytest.raises(mcp_access.MCPSetupError, match="not bound"):
        mcp_access.configure(path, "alpha")
    assert path.read_bytes() == before


def test_make_setup_rerun_reuses_key_and_skips_api_restart(monkeypatch, tmp_path):
    path = _env(tmp_path)
    present = False
    def fake_request(origin, key, method, route, body=None):
        nonlocal present
        if route == "/v1/projects":
            return 200, {"items": [{"id": "alpha"}]}
        if method == "GET":
            return ((200, {"status": "active", "member_type": "system",
                           "role_keys": ["mcp_operator"]}) if present else (404, {}))
        present = True
        return 201, {}
    monkeypatch.setattr(mcp_access, "_request", fake_request)
    restarts = []
    monkeypatch.setattr(mcp_access, "_restart_api", lambda env: restarts.append(env))
    monkeypatch.setattr(sys, "argv", ["mcp_access", "setup", "--env-file", str(path),
                                     "--restart-api"])
    assert mcp_access.main() == 0
    first = _values(path.read_text().splitlines())["BOSS_MCP_API_KEY"]
    assert mcp_access.main() == 0
    second = _values(path.read_text().splitlines())["BOSS_MCP_API_KEY"]
    assert first == second and restarts == [path]


def test_mcp_stdio_command_forwards_only_separate_key_in_environment(monkeypatch, tmp_path):
    path = _env(tmp_path)
    with pytest.raises(mcp_access.MCPSetupError, match="unbound"):
        mcp_access.launch(path, "alpha")
    path.write_text(path.read_text().replace(
        "existing-key:reader\n",
        "existing-key:reader,dedicated-secret:mcp_operator\n",
    ) + "BOSS_MCP_API_KEY=dedicated-secret\n")
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
    assert captured["args"][-5:] == [
        "--project", "alpha", "serve-mcp", "--api-url", "http://127.0.0.1:8000",
    ]


def test_mcp_stdio_uses_sudo_only_when_key_preservation_is_verified(monkeypatch, tmp_path):
    path = _env(tmp_path)
    path.write_text(path.read_text().replace(
        "existing-key:reader\n", "existing-key:reader,dedicated-secret:mcp_operator\n",
    ) + "BOSS_MCP_API_KEY=dedicated-secret\n")
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


def test_setup_http_client_refuses_redirects():
    assert mcp_access._NoRedirect().redirect_request(None, None, 302, "moved", {},
                                                     "https://example.invalid/") is None


def test_installer_rerun_preserves_dedicated_mcp_key_and_bindings(tmp_path):
    old = _env(tmp_path)
    old.write_text(old.read_text().replace(
        "existing-key:reader\n", "existing-key:reader,mcp-key:mcp_operator\n",
    ).replace(
        "existing-key\n", "existing-key,mcp-key\n",
    ) + "BOSS_MCP_API_KEY=mcp-key\n")
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
    assert "mcp-key" in values["BOSS_API_KEYS"].split(",")
    assert _principal_map(values["BOSS_API_KEY_PRINCIPALS"])["mcp-key"] == "mcp_operator"
