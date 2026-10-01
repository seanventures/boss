"""The node agent installs on the default machine and on extra machines.

Tokens stay hashed. These tests do not open a socket.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shlex
import shutil
import socket
import subprocess
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy import text

from boss_engine.services.hardware_agent import (
    EnrollTokenError,
    accept_agent_call_home,
    enroll_token_state,
    ensure_local_place,
    get_place,
    install_local_agent,
    list_places,
    mint_agent_enroll_command,
)


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "install" / "node_agent.sh"
PROJECT = "project-hardware"
REPORT = {
    "packages": ["curl", "ca-certificates", "python3"],
    "missing": [],
    "installed_by_agent": False,
    "package_manager": "none",
}


@pytest.fixture()
def conn():
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(text(
            """
            CREATE TABLE hardware_places (
              id TEXT PRIMARY KEY,
              project_id TEXT NOT NULL,
              kind TEXT NOT NULL,
              name TEXT NOT NULL,
              provider_key TEXT,
              status TEXT NOT NULL,
              is_default INTEGER NOT NULL DEFAULT 0,
              agent_state TEXT NOT NULL DEFAULT 'absent',
              endpoint TEXT,
              hostname TEXT,
              arch TEXT,
              os_name TEXT,
              dependency_report TEXT NOT NULL DEFAULT '{}',
              labels TEXT NOT NULL DEFAULT '{}',
              secret_ref TEXT,
              created_by TEXT NOT NULL,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              CHECK (agent_state IN ('absent', 'installed')),
              CHECK ((kind = 'local') = is_default)
            )
            """
        ))
        connection.execute(text(
            """
            CREATE UNIQUE INDEX uq_hardware_places_one_local
            ON hardware_places (project_id) WHERE kind = 'local'
            """
        ))
        connection.execute(text(
            """
            CREATE TABLE hardware_agent_enroll_tokens (
              id TEXT PRIMARY KEY,
              project_id TEXT NOT NULL,
              place_id TEXT NOT NULL,
              token_hash TEXT NOT NULL UNIQUE,
              consumed_at TEXT,
              created_by TEXT NOT NULL,
              created_at TEXT NOT NULL
            )
            """
        ))
        yield connection


def _token(command: str) -> str:
    assignment = command.splitlines()[0]
    prefix, separator, _rest = assignment.partition(" bash -s ")
    assert separator
    for part in shlex.split(prefix):
        if part.startswith("BOSS_ENROLL_TOKEN="):
            return part.split("=", 1)[1]
    raise AssertionError("operator command did not carry an enroll token")


def _facts(place: dict) -> dict:
    return {
        "id": place["id"],
        "kind": place["kind"],
        "is_default": place["is_default"],
        "agent_state": place["agent_state"],
        "status": place["status"],
    }


def test_local_install_records_the_agent_on_the_default_machine(conn) -> None:
    before = ensure_local_place(
        conn, project_id=PROJECT, hostname="installer", arch="arm64",
        os_name="darwin", actor="installer",
    )
    assert before["agent_state"] == "absent"
    assert before["is_default"] is True
    assert before["kind"] == "local"

    installed = install_local_agent(
        conn, project_id=PROJECT, hostname="installer", arch="arm64",
        os_name="darwin", dependency_report=REPORT, actor="node-agent",
        endpoint="agent://installer",
    )
    assert installed["id"] == before["id"]
    assert installed["agent_state"] == "installed"
    assert installed["is_default"] is True
    assert installed["dependency_report"] == REPORT
    assert installed["endpoint"] == "agent://installer"
    again = get_place(conn, installed["id"])
    assert again["agent_state"] == "installed"
    assert list_places(conn, PROJECT)[0]["id"] == installed["id"]


def test_remote_token_is_single_use_and_stores_the_dependency_report(conn, caplog) -> None:
    local = ensure_local_place(
        conn, project_id=PROJECT, hostname="installer", arch="arm64",
        os_name="darwin", actor="installer",
    )
    caplog.set_level(logging.DEBUG)
    minted = mint_agent_enroll_command(
        conn, project_id=PROJECT, name="spare-vm", actor="operator",
        control_plane_url="http://127.0.0.1:8000",
    )
    token = _token(minted["command"])
    state = enroll_token_state(conn, minted["place"]["id"])
    assert state["token_hash"] == hashlib.sha256(token.encode("utf-8")).hexdigest()
    assert state["used"] is False
    assert token not in json.dumps(minted["place"])
    assert token not in json.dumps(state)
    assert token not in caplog.text
    assert minted["place"]["is_default"] is False
    assert minted["place"]["agent_state"] == "absent"

    ready = accept_agent_call_home(conn, {
        "mode": "remote",
        "token": token,
        "hostname": "spare",
        "arch": "x86_64",
        "os": "linux/debian",
        "endpoint": "agent://spare",
        "dependency_report": REPORT,
        "labels": {"source": "node-agent"},
    })
    assert ready["agent_state"] == "installed"
    assert ready["is_default"] is False
    assert ready["dependency_report"] == REPORT
    assert ready["endpoint"] == "agent://spare"
    assert enroll_token_state(conn, ready["id"])["used"] is True
    assert token not in json.dumps(get_place(conn, ready["id"]))
    assert token not in caplog.text

    with pytest.raises(EnrollTokenError, match="already used"):
        accept_agent_call_home(conn, {
            "mode": "remote",
            "token": token,
            "hostname": "spare",
            "arch": "x86_64",
            "os": "linux/debian",
            "dependency_report": REPORT,
        })
    assert _facts(get_place(conn, local["id"])) == _facts(local)
    assert get_place(conn, local["id"])["is_default"] is True


def test_a_bad_enroll_token_is_rejected(conn) -> None:
    local = ensure_local_place(
        conn, project_id=PROJECT, hostname="installer", arch="arm64",
        os_name="darwin", actor="installer",
    )
    minted = mint_agent_enroll_command(
        conn, project_id=PROJECT, name="spare-vm", actor="operator",
        control_plane_url="https://boss.example",
    )
    with pytest.raises(EnrollTokenError, match="enroll token rejected"):
        accept_agent_call_home(conn, {
            "mode": "remote",
            "token": "not-the-issued-token",
            "hostname": "spare",
            "arch": "x86_64",
            "os": "linux",
            "dependency_report": REPORT,
        })
    assert enroll_token_state(conn, minted["place"]["id"])["used"] is False
    assert get_place(conn, minted["place"]["id"])["agent_state"] == "absent"
    assert _facts(get_place(conn, local["id"])) == _facts(local)


def test_agent_paths_do_not_open_a_socket(conn, monkeypatch) -> None:
    def opened(*_args, **_kwargs):
        raise AssertionError("socket opened")

    monkeypatch.setattr(socket, "socket", opened)
    monkeypatch.setattr(socket, "create_connection", opened)
    place = install_local_agent(
        conn, project_id=PROJECT, hostname="installer", arch="arm64",
        os_name="darwin", dependency_report=REPORT, actor="node-agent",
    )
    minted = mint_agent_enroll_command(
        conn, project_id=PROJECT, name="spare-vm", actor="operator",
        control_plane_url="http://127.0.0.1:8000",
    )
    accept_agent_call_home(conn, {
        "mode": "remote",
        "token": _token(minted["command"]),
        "hostname": "spare",
        "arch": "amd64",
        "os": "linux",
        "dependency_report": REPORT,
    })
    assert place["agent_state"] == "installed"
    source = (ROOT / "engine" / "boss_engine" / "services" / "hardware_agent.py").read_text(
        encoding="utf-8"
    )
    assert "paramiko" not in source
    assert "subprocess" not in source


def test_node_agent_reports_without_opening_a_socket(conn, tmp_path: Path) -> None:
    guard = tmp_path / "guard"
    guard.mkdir()
    (guard / "sitecustomize.py").write_text(
        "import socket\n"
        "def _blocked(*_args, **_kwargs):\n"
        "    raise OSError('socket opened')\n"
        "socket.socket = _blocked\n"
        "socket.create_connection = _blocked\n"
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    real_python = shutil.which("python3")
    assert real_python
    python = bin_dir / "python3"
    python.write_text(
        "#!/bin/sh\n"
        "export PYTHONPATH=\"$BOSS_SOCKET_GUARD${PYTHONPATH:+:$PYTHONPATH}\"\n"
        f"exec {real_python} \"$@\"\n"
    )
    python.chmod(0o755)
    curl = bin_dir / "curl"
    curl.write_text("#!/bin/sh\necho curl-invoked >&2\nexit 86\n")
    curl.chmod(0o755)
    env = os.environ.copy()
    env.update(
        PATH=f"{bin_dir}:{env['PATH']}",
        BOSS_SOCKET_GUARD=str(guard),
        BOSS_PROJECT_ID=PROJECT,
        BOSS_AGENT_CONFIG=str(tmp_path / "agent.env"),
        BOSS_CALL_HOME_SINK=str(tmp_path / "local.json"),
        BOSS_AGENT_HOSTNAME="installer",
        BOSS_AGENT_ARCH="arm64",
        BOSS_AGENT_OS="darwin",
        HOME=str(tmp_path),
    )
    local = subprocess.run(
        ["bash", str(SCRIPT), "--local"],
        env=env, text=True, capture_output=True, check=False,
    )
    assert local.returncode == 0, local.stderr
    payload = json.loads((tmp_path / "local.json").read_text())
    assert "token" not in payload
    assert payload["dependency_report"]["packages"] == [
        "curl", "ca-certificates", "python3",
    ]
    config = (tmp_path / "agent.env").read_text()
    assert "TOKEN" not in config
    installed = accept_agent_call_home(conn, payload)
    assert installed["is_default"] is True
    assert installed["agent_state"] == "installed"
    assert installed["dependency_report"]["packages"] == payload["dependency_report"]["packages"]

    minted = mint_agent_enroll_command(
        conn, project_id=PROJECT, name="spare-vm", actor="operator",
        control_plane_url="http://127.0.0.1:8000",
    )
    remote_env = dict(env)
    remote_env.update(
        BOSS_CALL_HOME_SINK=str(tmp_path / "remote.json"),
        BOSS_ENROLL_TOKEN=_token(minted["command"]),
        BOSS_CONTROL_PLANE="http://127.0.0.1:9",
        BOSS_AGENT_HOSTNAME="spare",
        BOSS_AGENT_CONFIG=str(tmp_path / "remote.env"),
    )
    remote = subprocess.run(
        ["bash", str(SCRIPT)],
        env=remote_env, text=True, capture_output=True, check=False,
    )
    assert remote.returncode == 0, remote.stderr
    body = json.loads((tmp_path / "remote.json").read_text())
    assert body["token"] == remote_env["BOSS_ENROLL_TOKEN"]
    assert body["token"] not in (tmp_path / "remote.env").read_text()
    ready = accept_agent_call_home(conn, body)
    assert ready["agent_state"] == "installed"
    assert ready["is_default"] is False
    assert ready["dependency_report"]["packages"] == [
        "curl", "ca-certificates", "python3",
    ]
    assert get_place(conn, installed["id"])["is_default"] is True
    script = SCRIPT.read_text(encoding="utf-8")
    for line in script.splitlines():
        if line.strip().startswith("#"):
            continue
        assert "ssh" not in line.lower()
    assert "BEGIN PRIVATE KEY" not in script


def test_node_agent_refuses_package_install_when_not_root(tmp_path: Path) -> None:
    env = os.environ.copy()
    env.update(
        BOSS_NODE_AGENT_FORCE_MISSING="curl",
        BOSS_AGENT_CONFIG=str(tmp_path / "agent.env"),
        HOME=str(tmp_path),
    )
    result = subprocess.run(
        ["bash", str(SCRIPT), "--local"],
        env=env, text=True, capture_output=True, check=False,
    )
    assert result.returncode != 0
    assert "not root" in result.stderr
    assert "curl" in result.stderr
    assert "ca-certificates" in result.stderr
    assert "python3" in result.stderr
    assert not (tmp_path / "agent.env").exists()


def test_package_manager_commands_name_the_declared_packages() -> None:
    expected = {
        "apt": "apt-get install -y",
        "dnf": "dnf install -y",
        "yum": "yum install -y",
        "apk": "apk add --no-cache",
    }
    for manager, needle in expected.items():
        result = subprocess.run(
            ["bash", "-c", f'source "{SCRIPT}"; node_agent_install_command {manager}'],
            text=True, capture_output=True, check=False,
        )
        assert result.returncode == 0, result.stderr
        assert needle in result.stdout
        for package in ("curl", "ca-certificates", "python3"):
            assert package in result.stdout
