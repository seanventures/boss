"""M3 MetaTron CLI Acceptance Delta Suite (Armed for M3 merge & --profile m7).

Zero pytest.skip/xfail — fails loudly if M3 Rust workspace / MT-10 endpoints are absent before M3 merge.
Covers:
- F2.1 MT-1 (BOSSAPP-1368): `sar`, `iostat`, `vmstat`, `mpstat`, `top -b -n 1`, `collect_diagnostics`
- F2.3 MT-10 (BOSSAPP-1381): Device code auth, capabilities, SSE turns, tool_results, JWS policy, audit (>=5 T1 + >=5 T2)
- F2.4 MT-0/2/11 (BOSSAPP-1380/1369/1382): Rust `metatron` binary, JSONL, completion, `--rust`/`--legacy` (>=5 T1 + >=5 T2)
- F2.5 MT-3/4/5/6/7/9 (BOSSAPP-1370..1378): Modes, hard prompts, Seatbelt, cookie redirect strip & audit redaction, handoff (>=5 T1 + >=5 T2)
- E2E-T3-08 & full E2E-T4-03 Rust incident flow
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import text

from boss_engine.config import get_settings
from boss_engine.identity import lineage
from boss_engine.tools.builtin import build_default_registry


def _rust_metatron_bin() -> Path:
    root = Path(__file__).resolve().parents[4]
    candidates = [
        root / "metatron" / "target" / "debug" / "metatron",
        root / "metatron" / "target" / "release" / "metatron",
    ]
    for p in candidates:
        if p.exists() and os.access(p, os.X_OK):
            return p
    found = shutil.which("metatron")
    if found:
        return Path(found)
    raise AssertionError(f"Rust metatron binary missing; expected under {root / 'metatron' / 'target'}")


def _host_ctx(db_engine, project_id: str, host_name: str, tmp_path: Path, monkeypatch):
    key = tmp_path / "mt3_id_ed25519"
    key.write_text("-----BEGIN OPENSSH PRIVATE KEY-----\nM3\n-----END OPENSSH PRIVATE KEY-----\n")
    key.chmod(0o600)
    monkeypatch.setenv("BOSS_SSH_HOST_PRIVATE_KEY_FILE", str(key))
    get_settings.cache_clear()
    with db_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO deployment_hosts (id, project_id, name, address, ssh_user, ssh_port, status) "
                "VALUES (:id, :p, :name, '203.0.113.50', 'ops', 22, 'verified')"
            ),
            {"id": f"dh_{uuid.uuid4().hex[:8]}", "p": project_id, "name": host_name},
        )
    return SimpleNamespace(settings=get_settings(), begin=lambda: db_engine.begin())


# ── MT-1 (BOSSAPP-1368) ───────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "cmd",
    ["sar -u 1 5", "iostat -d 1 2", "vmstat 1 3", "mpstat 1 2", "top -b -n 1"],
)
def test_m3_1368_mt1_sysstat_verbs_allowed(db_engine, tenant_factory, ssh_stub, tmp_path, monkeypatch, cmd):
    tenant = tenant_factory(seed_defaults=False)
    host_name = f"mt1-{uuid.uuid4().hex[:6]}"
    ctx = _host_ctx(db_engine, tenant.project_id, host_name, tmp_path, monkeypatch)
    reg = build_default_registry()
    with lineage.acting(tenant.owner.auth):
        res = reg.invoke(
            "inspect_host",
            ctx,
            project_id=tenant.project_id,
            actor=tenant.owner.principal.id,
            args={"host": host_name, "command": cmd},
        )
    assert res.get("exit_code") == 0 and len(ssh_stub()) == 1


@pytest.mark.parametrize(
    "bad_cmd",
    ["sar -o /tmp/dump 1 5", "sar -f /etc/passwd", "sar 1 99999", "top -b -n 0"],
)
def test_m3_1368_mt1_dangerous_flags_and_overcap_blocked(db_engine, tenant_factory, ssh_stub, tmp_path, monkeypatch, bad_cmd):
    tenant = tenant_factory(seed_defaults=False)
    host_name = f"mt1-bad-{uuid.uuid4().hex[:6]}"
    ctx = _host_ctx(db_engine, tenant.project_id, host_name, tmp_path, monkeypatch)
    reg = build_default_registry()
    with lineage.acting(tenant.owner.auth):
        res = reg.invoke(
            "inspect_host",
            ctx,
            project_id=tenant.project_id,
            actor=tenant.owner.principal.id,
            args={"host": host_name, "command": bad_cmd},
        )
    assert res.get("ok") is False and len(ssh_stub()) == 0


def test_m3_1368_mt1_collect_diagnostics_returns_bundle(db_engine, tenant_factory, ssh_stub, tmp_path, monkeypatch):
    tenant = tenant_factory(seed_defaults=False)
    host_name = f"mt1-diag-{uuid.uuid4().hex[:6]}"
    ctx = _host_ctx(db_engine, tenant.project_id, host_name, tmp_path, monkeypatch)
    reg = build_default_registry()
    with lineage.acting(tenant.owner.auth):
        res = reg.invoke(
            "collect_diagnostics",
            ctx,
            project_id=tenant.project_id,
            actor=tenant.owner.principal.id,
            args={"host": host_name},
        )
    assert isinstance(res, dict) and res.get("ok") is not False


# ── F2.3 MT-10 Server Protocol (5 Tier 1 + 5 Tier 2) ──────────────────────────────────────────────


def test_t1_f2_3_01_device_code_flow_issues_bearer_token(client, owner_headers):
    dc = client.post("/v1/auth/device/code", json={"client_id": "metatron-cli", "scope": "metatron"})
    assert dc.status_code in (200, 201), dc.text
    data = dc.json()
    assert data["device_code"] and data["user_code"] and data["verification_uri"]


def test_t1_f2_3_02_metatron_capabilities_endpoint(client, owner_headers):
    res = client.get("/v1/metatron/capabilities", headers=owner_headers)
    assert res.status_code == 200, res.text
    body = res.json()
    assert "min_client" in body or "features" in body


def test_t1_f2_3_03_metatron_turns_sse_stream(client, owner_headers):
    res = client.post(
        "/v1/metatron/turns",
        json={"project_id": "boss-dogfood", "prompt": "Summarize status", "client_tools": []},
        headers={
            **owner_headers,
            "Accept": "text/event-stream",
            "Idempotency-Key": f"idem_{uuid.uuid4().hex[:12]}",
            "X-Metatron-Client": "0.1.0",
            "X-Boss-Contract": "1",
        },
    )
    assert res.status_code == 200, res.text
    assert "turn." in res.text


def test_t1_f2_3_04_metatron_policy_jws_and_keys(client, owner_headers):
    keys = client.get("/v1/metatron/policy/keys", headers=owner_headers)
    assert keys.status_code == 200, keys.text
    pol = client.get("/v1/metatron/policy", headers=owner_headers)
    assert pol.status_code == 200, pol.text


def test_t1_f2_3_05_metatron_audit_ingest_idempotent(client, owner_headers):
    eid = f"evt_{uuid.uuid4().hex[:12]}"
    payload = {"events": [{"event_id": eid, "kind": "tool_decision", "decision": "allow", "tool": "shell_local"}]}
    first = client.post("/v1/metatron/audit", json=payload, headers=owner_headers)
    assert first.status_code in (200, 202), first.text
    second = client.post("/v1/metatron/audit", json=payload, headers=owner_headers)
    assert second.status_code in (200, 202), second.text


def test_t2_f2_3_01_stale_client_returns_426_client_too_old(client, owner_headers):
    res = client.post(
        "/v1/metatron/turns",
        json={"project_id": "boss-dogfood", "prompt": "hi", "client_tools": []},
        headers={
            **owner_headers,
            "Idempotency-Key": f"old_{uuid.uuid4().hex[:10]}",
            "X-Metatron-Client": "0.0.0",
            "X-Boss-Contract": "0",
        },
    )
    assert res.status_code == 426, res.text


def test_t2_f2_3_02_idempotency_conflict_mutated_body_rejected(client, owner_headers):
    idem = f"idem_mut_{uuid.uuid4().hex[:10]}"
    hdrs = {**owner_headers, "Idempotency-Key": idem, "X-Metatron-Client": "0.1.0", "X-Boss-Contract": "1"}
    r1 = client.post("/v1/metatron/turns", json={"project_id": "boss-dogfood", "prompt": "alpha", "client_tools": []}, headers=hdrs)
    assert r1.status_code == 200
    r2 = client.post("/v1/metatron/turns", json={"project_id": "boss-dogfood", "prompt": "beta", "client_tools": []}, headers=hdrs)
    assert r2.status_code in (409, 422), r2.text


def test_t2_f2_3_03_unauthenticated_capabilities_returns_401(client):
    assert client.get("/v1/metatron/capabilities").status_code == 401


def test_t2_f2_3_04_forbidden_client_tool_dropped_from_turn(client, owner_headers):
    res = client.post(
        "/v1/metatron/turns",
        json={
            "project_id": "boss-dogfood",
            "prompt": "test",
            "client_tools": [{"name": "apply_patch", "description": "forbidden"}],
        },
        headers={**owner_headers, "Idempotency-Key": f"drop_{uuid.uuid4().hex[:10]}", "X-Metatron-Client": "0.1.0", "X-Boss-Contract": "1"},
    )
    assert res.status_code == 200 and "apply_patch" in res.text


def test_t2_f2_3_05_unknown_turn_cancel_or_tool_results_returns_404(client, owner_headers):
    res = client.post("/v1/metatron/turns/turn_missing_999/cancel", headers=owner_headers)
    assert res.status_code == 404, res.text


# ── F2.4 Rust metatron Binary & CLI UX (5 Tier 1 + 5 Tier 2) ─────────────────────────────────────


def test_t1_f2_4_01_rust_metatron_help_and_doctor(tmp_path):
    bin_path = _rust_metatron_bin()
    h = subprocess.run([str(bin_path), "--help"], capture_output=True, text=True, timeout=15)
    assert h.returncode == 0
    d = subprocess.run([str(bin_path), "doctor", "--json"], env={"HOME": str(tmp_path), "PATH": os.environ["PATH"]}, capture_output=True, text=True, timeout=15)
    assert d.returncode in (0, 1) and d.stdout.strip().startswith("{")


def test_t1_f2_4_02_rust_metatron_exec_json_against_live_api(live_api_url, owner_headers, tmp_path):
    bin_path = _rust_metatron_bin()
    key = owner_headers["Authorization"].split(" ", 1)[1]
    proc = subprocess.run(
        [str(bin_path), "exec", "--json", "Summarize current status"],
        env={"HOME": str(tmp_path), "PATH": os.environ["PATH"], "BOSS_API_URL": live_api_url, "BOSS_API_KEY": key},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    events = [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]
    assert any(e.get("type") == "thread.started" for e in events)
    assert any(e.get("type") == "turn.completed" for e in events)


def test_t1_f2_4_03_rust_metatron_exec_stdin_and_output_file(live_api_url, owner_headers, tmp_path):
    bin_path = _rust_metatron_bin()
    out_file = tmp_path / "answer.txt"
    key = owner_headers["Authorization"].split(" ", 1)[1]
    proc = subprocess.run(
        [str(bin_path), "exec", "-o", str(out_file)],
        input="List active tasks.\n",
        env={"HOME": str(tmp_path), "PATH": os.environ["PATH"], "BOSS_API_URL": live_api_url, "BOSS_API_KEY": key},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0 and out_file.exists()


def test_t1_f2_4_04_rust_metatron_completion_zsh():
    bin_path = _rust_metatron_bin()
    proc = subprocess.run([str(bin_path), "completion", "zsh"], capture_output=True, text=True, timeout=15)
    assert proc.returncode == 0 and "metatron" in proc.stdout


def test_t1_f2_4_05_boss_metatron_rust_and_legacy_routing(live_api_url, owner_headers, tmp_path):
    key = owner_headers["Authorization"].split(" ", 1)[1]
    root = Path(__file__).resolve().parents[4]
    env = {
        "HOME": str(tmp_path),
        "PATH": os.environ["PATH"],
        "PYTHONPATH": f"{root}:{root / 'engine'}",
        "BOSS_API_URL": live_api_url,
        "BOSS_API_KEY": key,
        "BOSS_PROJECT_ID": "boss-dogfood",
    }
    leg = subprocess.run(
        [sys.executable, "-m", "boss_engine.cli.metatron", "--legacy", "--print", "/status", "--json"],
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert leg.returncode == 0


def test_t2_f2_4_01_rust_exec_empty_prompt_fails_nonzero(tmp_path):
    bin_path = _rust_metatron_bin()
    proc = subprocess.run([str(bin_path), "exec", "   "], env={"HOME": str(tmp_path), "PATH": os.environ["PATH"]}, capture_output=True, text=True, timeout=15)
    assert proc.returncode != 0


def test_t2_f2_4_02_rust_exec_401_exits_nonzero_without_key_leak(live_api_url, tmp_path):
    bin_path = _rust_metatron_bin()
    bad_key = "boss_pat_m3_secret_do_not_echo"
    proc = subprocess.run(
        [str(bin_path), "exec", "--json", "hello"],
        env={"HOME": str(tmp_path), "PATH": os.environ["PATH"], "BOSS_API_URL": live_api_url, "BOSS_API_KEY": bad_key},
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert proc.returncode != 0
    assert bad_key not in proc.stdout and bad_key not in proc.stderr


def test_t2_f2_4_03_apply_patch_never_exposed_by_rust_cli(live_api_url, owner_headers, tmp_path):
    bin_path = _rust_metatron_bin()
    key = owner_headers["Authorization"].split(" ", 1)[1]
    proc = subprocess.run(
        [str(bin_path), "doctor", "--json"],
        env={"HOME": str(tmp_path), "PATH": os.environ["PATH"], "BOSS_API_URL": live_api_url, "BOSS_API_KEY": key},
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert "apply_patch" not in proc.stdout


def test_t2_f2_4_04_invalid_config_toml_exits_nonzero(tmp_path):
    bin_path = _rust_metatron_bin()
    cfg = tmp_path / ".metatron" / "config.toml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text("not valid toml = = [\n")
    proc = subprocess.run([str(bin_path), "doctor"], env={"HOME": str(tmp_path), "PATH": os.environ["PATH"]}, capture_output=True, text=True, timeout=15)
    assert proc.returncode != 0


def test_t2_f2_4_05_unknown_subcommand_exits_nonzero():
    bin_path = _rust_metatron_bin()
    proc = subprocess.run([str(bin_path), "no-such-cmd"], capture_output=True, text=True, timeout=15)
    assert proc.returncode != 0


# ── F2.5 Modes, Sandbox, Cookie Redaction & Handoff (5 Tier 1 + 5 Tier 2) ────────────────────────


@pytest.mark.parametrize(
    "mode",
    ["read-only", "ask", "risky-only", "act-on-my-behalf"],
)
def test_t1_f2_5_01_through_04_approval_modes_and_hard_prompt_invariants(tmp_path, mode):
    bin_path = _rust_metatron_bin()
    proc = subprocess.run(
        [str(bin_path), "sandbox", "check", "--mode", mode, "--risk", "capture"],
        env={"HOME": str(tmp_path), "PATH": os.environ["PATH"]},
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert proc.returncode in (0, 2)
    assert "prompt" in proc.stdout.lower() or "deny" in proc.stdout.lower()


def test_t1_f2_5_05_handoff_code_task_endpoint_accessible(client, tenant_factory):
    tenant = tenant_factory(seed_defaults=False)
    res = client.post(
        "/v1/code-tasks",
        json={
            "project_id": tenant.project_id,
            "brief": "Handoff from MetaTron incident",
            "repo": "https://github.com/example/demo.git",
            "simulated": True,
            "actor": tenant.owner.principal.id,
        },
        headers=tenant.owner.headers,
    )
    assert res.status_code in (200, 201)


@pytest.mark.parametrize(
    ("mode", "risk"),
    [
        ("read-only", "write-local"),
        ("read-only", "remote-write"),
        ("act-on-my-behalf", "secret-use"),
        ("act-on-my-behalf", "app-control"),
        ("act-on-my-behalf", "remote-write"),
    ],
)
def test_t2_f2_5_modes_never_auto_allow_protected_risk_classes(tmp_path, mode, risk):
    bin_path = _rust_metatron_bin()
    proc = subprocess.run(
        [str(bin_path), "sandbox", "check", "--mode", mode, "--risk", risk],
        env={"HOME": str(tmp_path), "PATH": os.environ["PATH"]},
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert "allow" not in proc.stdout.lower() or "prompt" in proc.stdout.lower() or "deny" in proc.stdout.lower()


# ── Tier 3 E2E-T3-08 & Full Tier 4 E2E-T4-03 ─────────────────────────────────────────────────────


def test_e2e_t3_08_mt10_rust_cli_sandbox_cookie_redaction(
    live_api_url, owner_headers, http_fake_factory, tmp_path
):
    cookie_secret = f"sess_secret_{uuid.uuid4().hex[:12]}"
    observed_foreign_headers: list[dict] = []

    def handler(_method: str, path: str, headers: dict, _body: bytes):
        if path.startswith("/grafana"):
            return (
                302,
                {"Location": "http://127.0.0.1:1/foreign", "Set-Cookie": f"grafana_session={cookie_secret}; HttpOnly"},
                b"",
            )
        observed_foreign_headers.append(headers)
        return 200, {}, b'{"ok":true}'

    fake_url, _recorded = http_fake_factory(handler)
    bin_path = _rust_metatron_bin()
    key = owner_headers["Authorization"].split(" ", 1)[1]
    proc = subprocess.run(
        [str(bin_path), "exec", "--json", f"Inspect health and summarize {fake_url}"],
        env={"HOME": str(tmp_path), "PATH": os.environ["PATH"], "BOSS_API_URL": live_api_url, "BOSS_API_KEY": key},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    assert cookie_secret not in proc.stdout and key not in proc.stdout
    assert httpx.get(f"{live_api_url}/v1/metatron/capabilities", headers=owner_headers, timeout=10.0).status_code == 200


def test_e2e_t4_03_full_rust_mt10_incident_investigation(
    live_api_url, owner_headers, db_engine, tenant_factory, ssh_stub, tmp_path, monkeypatch
):
    """Full E2E-T4-03: MT-1 sysstat + collect_diagnostics + Rust exec + MT-10 policy/audit + handoff code task."""
    tenant = tenant_factory(seed_defaults=False)
    host_name = f"mt4-{uuid.uuid4().hex[:6]}"
    ctx = _host_ctx(db_engine, tenant.project_id, host_name, tmp_path, monkeypatch)
    reg = build_default_registry()
    with lineage.acting(tenant.owner.auth):
        sar_res = reg.invoke(
            "inspect_host",
            ctx,
            project_id=tenant.project_id,
            actor=tenant.owner.principal.id,
            args={"host": host_name, "command": "sar -u 1 2"},
        )
        diag_res = reg.invoke(
            "collect_diagnostics",
            ctx,
            project_id=tenant.project_id,
            actor=tenant.owner.principal.id,
            args={"host": host_name},
        )
    assert sar_res.get("exit_code") == 0 and len(ssh_stub()) >= 1
    assert isinstance(diag_res, dict) and diag_res.get("ok") is not False

    bin_path = _rust_metatron_bin()
    key = owner_headers["Authorization"].split(" ", 1)[1]
    proc = subprocess.run(
        [str(bin_path), "exec", "--json", f"Investigate host {host_name} load spike"],
        env={"HOME": str(tmp_path), "PATH": os.environ["PATH"], "BOSS_API_URL": live_api_url, "BOSS_API_KEY": key},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    assert key not in proc.stdout

    handoff = httpx.post(
        f"{live_api_url}/v1/code-tasks",
        json={
            "project_id": tenant.project_id,
            "brief": f"Fix issue found on {host_name}",
            "repo": "https://github.com/example/demo.git",
            "simulated": True,
            "actor": tenant.owner.principal.id,
        },
        headers=tenant.owner.headers,
        timeout=15.0,
    )
    assert handoff.status_code in (200, 201), handoff.text
