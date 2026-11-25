"""MetaTron CLI M6 Baseline Opaque-Box E2E Suite (F2.1, F2.2 — Tier 1 & Tier 2).

Covers M1-present MetaTron capabilities with zero skips and zero LAST_MILE mocking:
- F2.1: `inspect_host` read-only allowlist & shell-metacharacter ban (via real tool registry +
  fake `ssh` binary on PATH + real Postgres `deployment_hosts`).
- F2.2: Python Metatron CLI (`boss_engine.cli.metatron`) & Captain HTTP endpoints
  (`/v1/metatron/session`, `/goals`, `/watch`, `/drive`, `/overwatch`, `/theater`).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from boss_engine.config import get_settings
from boss_engine.identity import lineage
from boss_engine.tools.builtin import build_default_registry


def _make_ssh_key(tmp_path: Path) -> Path:
    key = tmp_path / "m6_test_id_ed25519"
    key.write_text("-----BEGIN OPENSSH PRIVATE KEY-----\nM6_STUB_KEY\n-----END OPENSSH PRIVATE KEY-----\n")
    key.chmod(0o600)
    return key


def _host_ctx(db_engine, project_id: str, host_name: str, key_file: Path,
              monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("BOSS_SSH_HOST_PRIVATE_KEY_FILE", str(key_file))
    get_settings.cache_clear()
    settings = get_settings()
    with db_engine.begin() as conn:
        hid = f"dh_{uuid.uuid4().hex[:10]}"
        conn.execute(
            text(
                "INSERT INTO deployment_hosts (id, project_id, name, address, ssh_user, ssh_port, status) "
                "VALUES (:id, :p, :name, '203.0.113.20', 'ops', 22, 'verified')"
            ),
            {"id": hid, "p": project_id, "name": host_name},
        )
    return SimpleNamespace(
        settings=settings,
        begin=lambda: db_engine.begin(),
    )


# ═════════════════════════════════════════════════════════════════════════════════════════════════
# F2.1 — inspect_host Read-Only Allowlist (Tier 1 >= 5, Tier 2 >= 5)
# ═════════════════════════════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    ("command", "expected_token"),
    [
        ("docker ps --format '{{.Names}}'", "docker ps --format '{{.Names}}'"),
        ("cat /etc/os-release", "cat /etc/os-release"),
        ("journalctl -u docker -n 20 --no-pager", "journalctl -u docker -n 20 --no-pager"),
        ("ss -tulnp", "ss -tulnp"),
        ("ps aux", "ps aux"),
    ],
    ids=[
        "t1_f2_1_01_docker_ps",
        "t1_f2_1_02_cat_os_release",
        "t1_f2_1_03_journalctl",
        "t1_f2_1_04_ss_tulnp",
        "t1_f2_1_05_ps_aux",
    ],
)
def test_t1_f2_1_inspect_host_allowed_reads(
    db_engine, tenant_factory, ssh_stub, tmp_path, monkeypatch, command, expected_token
):
    tenant = tenant_factory(seed_defaults=False)
    host_name = f"host-{uuid.uuid4().hex[:8]}"
    key_file = _make_ssh_key(tmp_path)
    ctx = _host_ctx(db_engine, tenant.project_id, host_name, key_file, monkeypatch)
    reg = build_default_registry()
    with lineage.acting(tenant.session_auth):
        res = reg.invoke(
            "inspect_host",
            ctx,
            project_id=tenant.project_id,
            actor=tenant.owner.principal.id,
            args={"host": host_name, "command": command},
        )
    assert res.get("exit_code") == 0 and res.get("ok") is not False, res
    assert "STUB_SSH_OK:" in res.get("stdout", "")
    calls = ssh_stub()
    assert len(calls) == 1
    assert expected_token in calls[0]["argv"][-1]


@pytest.mark.parametrize(
    "bad_command",
    [
        "ls; rm -rf /",
        "ls | tee /tmp/pwn",
        "ls $(id)",
        "ls\ntouch /tmp/x",
        "ss -K dst 192.0.2.1",
        "journalctl --rotate",
        "ls 'unterminated",
        "rm -rf /var/log",
    ],
    ids=[
        "t2_f2_1_01_semicolon_metachar",
        "t2_f2_1_02_pipe_metachar",
        "t2_f2_1_03_subshell_metachar",
        "t2_f2_1_04_newline_control",
        "t2_f2_1_05_ss_kill_flag",
        "t2_f2_1_06_journalctl_rotate",
        "t2_f2_1_07_unbalanced_quote",
        "t2_f2_1_08_disallowed_verb_rm",
    ],
)
def test_t2_f2_1_inspect_host_rejects_unsafe_commands_before_ssh(
    db_engine, tenant_factory, ssh_stub, tmp_path, monkeypatch, bad_command
):
    tenant = tenant_factory(seed_defaults=False)
    host_name = f"host-{uuid.uuid4().hex[:8]}"
    key_file = _make_ssh_key(tmp_path)
    ctx = _host_ctx(db_engine, tenant.project_id, host_name, key_file, monkeypatch)
    reg = build_default_registry()
    res = reg.invoke(
        "inspect_host",
        ctx,
        project_id=tenant.project_id,
        actor=tenant.owner.principal.id,
        args={"host": host_name, "command": bad_command},
    )
    assert res["ok"] is False
    assert ssh_stub() == []


# ═════════════════════════════════════════════════════════════════════════════════════════════════
# F2.2 — Python Metatron CLI & Captain Endpoints (Tier 1 >= 5, Tier 2 >= 5)
# ═════════════════════════════════════════════════════════════════════════════════════════════════


def test_t1_f2_2_01_metatron_session_load(client, owner_headers):
    res = client.get(
        "/v1/metatron/session",
        params={"project_id": "boss-dogfood"},
        headers=owner_headers,
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body.get("thread_id")
    assert "goals" in body and "messages" in body


def test_t1_f2_2_02_metatron_watch_creates_tick_and_receipt(client, owner_headers):
    res = client.post(
        "/v1/metatron/watch",
        json={"project_id": "boss-dogfood", "actor": "ceo"},
        headers=owner_headers,
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert str(body.get("watch_id", "")).startswith("mwt_") or "watch" in body or "receipt_id" in body
    latest = client.get(
        "/v1/metatron/watch/latest",
        params={"project_id": "boss-dogfood"},
        headers=owner_headers,
    )
    assert latest.status_code == 200, latest.text
    assert latest.json().get("watch") is not None


def test_t1_f2_2_03_metatron_drive_proposes_moves(client, owner_headers):
    res = client.post(
        "/v1/metatron/drive",
        json={"project_id": "boss-dogfood", "actor": "ceo", "auto": False},
        headers=owner_headers,
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert isinstance(body, dict)


def test_t1_f2_2_04_metatron_overwatch_and_theater(client, owner_headers):
    ov = client.post(
        "/v1/metatron/overwatch",
        json={"project_id": "boss-dogfood", "actor": "ceo"},
        headers=owner_headers,
    )
    assert ov.status_code == 200, ov.text
    th = client.get(
        "/v1/metatron/theater",
        params={"project_id": "boss-dogfood"},
        headers=owner_headers,
    )
    assert th.status_code == 200, th.text
    assert isinstance(th.json(), dict)


def test_t1_f2_2_05_python_metatron_cli_subprocess_status_json(live_api_url, owner_headers, tmp_path):
    api_key = owner_headers["Authorization"].split(" ", 1)[1]
    root = Path(__file__).resolve().parents[3]
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "PYTHONPATH": f"{root}:{root / 'engine'}",
        "HOME": str(tmp_path),
        "BOSS_API_URL": live_api_url,
        "BOSS_API_KEY": api_key,
        "BOSS_PROJECT_ID": "boss-dogfood",
    }
    proc = subprocess.run(
        [sys.executable, str(root / "scripts" / "metatron_tui.py"), "--print", "/status", "--json"],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, f"stderr={proc.stderr} stdout={proc.stdout}"
    parsed = json.loads(proc.stdout)
    assert isinstance(parsed, dict)
    assert api_key not in proc.stdout and api_key not in proc.stderr


def test_t2_f2_2_01_metatron_session_missing_project_rejected(client, owner_headers):
    res = client.get(
        "/v1/metatron/session",
        params={"project_id": "   "},
        headers=owner_headers,
    )
    assert res.status_code in (403, 404, 422), res.text


def test_t2_f2_2_02_metatron_watch_missing_project_rejected(client, owner_headers):
    res = client.post(
        "/v1/metatron/watch",
        json={"project_id": "", "actor": "ceo"},
        headers=owner_headers,
    )
    assert res.status_code in (403, 404, 422), res.text


def test_t2_f2_2_03_metatron_theater_unauthenticated_refused(client):
    res = client.get("/v1/metatron/theater", params={"project_id": "boss-dogfood"})
    assert res.status_code == 401, res.text


def test_t2_f2_2_04_metatron_cli_invalid_key_exits_nonzero_without_leak(live_api_url, tmp_path):
    bad_key = "boss_pat_invalid_secret_never_echoed_999"
    root = Path(__file__).resolve().parents[3]
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "PYTHONPATH": f"{root}:{root / 'engine'}",
        "HOME": str(tmp_path),
        "BOSS_API_URL": live_api_url,
        "BOSS_API_KEY": bad_key,
        "BOSS_PROJECT_ID": "boss-dogfood",
    }
    proc = subprocess.run(
        [sys.executable, str(root / "scripts" / "metatron_tui.py"), "--print", "/tasks", "--json"],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode != 0
    assert bad_key not in proc.stdout and bad_key not in proc.stderr


def test_t2_f2_2_05_metatron_goal_empty_title_rejected(client, owner_headers):
    res = client.post(
        "/v1/metatron/goals",
        json={"project_id": "boss-dogfood", "actor": "ceo", "title": "   "},
        headers=owner_headers,
    )
    assert res.status_code == 422, res.text
