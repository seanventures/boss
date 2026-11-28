"""Tests for BOSSAPP-1469 (local-first delete copy & rollout commit URLs) and
BOSSAPP-1470 (delete export link via portability.py, real SSH app removal, Watch this app
dynamic cost & code-discovered watches)."""
from __future__ import annotations

import os
import subprocess
import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from boss_engine.config import Settings
from boss_engine.services import portability, projects_delete, watch_app
from boss_engine.services.deployment_rollout_engine import DeploymentRolloutEngine

REPO_ROOT = Path(__file__).resolve().parents[2]


def _db_url() -> str:
    return os.environ.get("BOSS_TEST_DATABASE_URL") or os.environ.get("DATABASE_URL") or ""


@pytest.fixture()
def pg_engine():
    url = _db_url()
    if not url:
        pytest.skip("requires Postgres (DATABASE_URL / BOSS_TEST_DATABASE_URL)")
    eng = create_engine(url, future=True)
    yield eng
    eng.dispose()


def _seed_project(conn, *, suffix: str, local_only: bool = False) -> str:
    org_id = f"org_p_{suffix}"
    ws_id = f"ws_p_{suffix}"
    pid = f"p_pol_{suffix}"
    conn.execute(text("INSERT INTO orgs (id, name) VALUES (:o, :n) ON CONFLICT (id) DO NOTHING"),
                 {"o": org_id, "n": f"Org {suffix}"})
    conn.execute(text("INSERT INTO workspaces (id, org_id, name) VALUES (:w, :o, :n) ON CONFLICT (id) DO NOTHING"),
                 {"w": ws_id, "o": org_id, "n": f"WS {suffix}"})
    conn.execute(text("INSERT INTO projects (id, workspace_id, name, slug) VALUES (:p, :w, :n, :s)"),
                 {"p": pid, "w": ws_id, "n": f"Proj {suffix}", "s": pid})
    if local_only:
        conn.execute(
            text(
                "INSERT INTO local_change_sets (id, project_id, repository, number, branch, "
                "base_branch, title, state) VALUES (:i, :p, :r, 1, 'feat/x', 'main', 't', 'open')"
            ),
            {"i": f"lcs_{suffix}", "p": pid, "r": f"local/{pid}"},
        )
    return pid


# ── BOSSAPP-1469 ──────────────────────────────────────────────────────────────────────────────

def test_local_first_delete_ui_copy_has_no_hardcoded_github_claim():
    for rel in ("apps/web/app/project-delete.tsx", "apps/web/app/project-menu.tsx"):
        body = (REPO_ROOT / rel).read_text(encoding="utf-8")
        assert "The GitHub repository stays" not in body, f"hardcoded GitHub copy remained in {rel}"


def test_rollout_commit_url_uses_local_url_for_local_projects_and_github_for_github(pg_engine):
    suffix = uuid.uuid4().hex[:8]
    with pg_engine.begin() as c:
        local_pid = _seed_project(c, suffix=f"loc_{suffix}", local_only=True)
        gh_pid = _seed_project(c, suffix=f"gh_{suffix}", local_only=False)

        local_url = DeploymentRolloutEngine._rollout_commit_url(
            c, local_pid, f"local/{local_pid}", "deadbeef1234"
        )
        assert local_url == f"http://boss-local/local/{local_pid}/commit/deadbeef1234"
        assert "github.com" not in local_url

        bare_local_url = DeploymentRolloutEngine._rollout_commit_url(
            c, local_pid, "my-local-app", "deadbeef1234"
        )
        assert bare_local_url == "http://boss-local/my-local-app/commit/deadbeef1234"

        gh_url = DeploymentRolloutEngine._rollout_commit_url(
            c, gh_pid, "acme/payments", "abc123456789"
        )
        assert gh_url == "https://github.com/acme/payments/commit/abc123456789"


def test_projects_delete_preview_local_keeps_and_portability_export_link(pg_engine):
    suffix = uuid.uuid4().hex[:8]
    settings = Settings(_env_file=None)
    with pg_engine.begin() as c:
        local_pid = _seed_project(c, suffix=f"del_{suffix}", local_only=True)
        prev = projects_delete.preview(c, settings, local_pid)
        assert prev is not None
        assert prev["local_only"] is True
        assert not any("GitHub" in k for k in prev["keeps"])
        assert prev["export"]["available"] is True
        assert prev["export"]["href"] == f"/v1/projects/{local_pid}/export"
        assert prev["export"]["format"] == portability.FORMAT

        # The linked export actually produces a clean portable bundle with no secrets.
        bundle = portability.export_bundle(c, settings, scope="project", target_id=local_pid, actor="console")
        assert bundle["format"] == portability.FORMAT
        assert len(bundle["projects"]) == 1
        assert bundle["projects"][0]["ref"]["slug"] == local_pid


# ── BOSSAPP-1470: real SSH app removal path ───────────────────────────────────────────────────

def test_real_ssh_app_removal_materialises_key_runs_script_and_handles_failure(pg_engine, monkeypatch, tmp_path):
    from boss_engine.services import app_removal, deploy_service, secret_backends

    suffix = uuid.uuid4().hex[:8]
    settings = Settings(_env_file=None)
    target_id = f"dt_ssh_{suffix}"
    with pg_engine.begin() as c:
        pid = _seed_project(c, suffix=f"ssh_{suffix}")
        c.execute(
            text(
                "INSERT INTO deployment_targets (id, project_id, app_name, repo, environment, "
                "target_kind, target_ref, status, created_by) "
                "VALUES (:t, :p, 'billing', 'acme/billing', 'staging', 'ssh', 'ubuntu@10.20.30.40:2222', 'active', 'test')"
            ),
            {"t": target_id, "p": pid},
        )

    fake_key = tmp_path / "ephemeral_key"
    fake_key.write_text("PRIVATE KEY", encoding="utf-8")
    discarded: list[Path] = []
    ssh_calls: list[dict] = []

    monkeypatch.setattr(app_removal, "removal_scope", lambda _c, _row: {
        "compose_projects": ["boss-billing-staging"],
        "containers": ["billing-web"],
        "directories": ["/opt/boss-apps/billing-staging"],
    })
    monkeypatch.setattr(secret_backends, "resolve_target_secret", lambda _c, _row, _k, _s: "ssh-key-body")
    monkeypatch.setattr(deploy_service, "_materialise_key", lambda _mat: fake_key)
    monkeypatch.setattr(deploy_service, "_discard_key_file", lambda p: discarded.append(p))

    def _fake_ssh(key, host, user, port, script, timeout=300):
        ssh_calls.append({"key": key, "host": host, "user": user, "port": port, "script": script})
        return subprocess.CompletedProcess(["ssh"], 0, "removed", "")

    monkeypatch.setattr(deploy_service, "_ssh", _fake_ssh)

    res = projects_delete._remove_on_host(pg_engine, settings, {"target_id": target_id})
    assert res["status"] == "removed"
    assert len(ssh_calls) == 1
    assert ssh_calls[0]["host"] == "10.20.30.40"
    assert ssh_calls[0]["user"] == "ubuntu"
    assert ssh_calls[0]["port"] == 2222
    assert "billing-web" in ssh_calls[0]["script"]
    assert discarded == [fake_key]

    # Non-zero exit must refuse cleanly and still discard the ephemeral key file.
    discarded.clear()
    monkeypatch.setattr(
        deploy_service, "_ssh",
        lambda *a, **kw: subprocess.CompletedProcess(["ssh"], 255, "", "connection refused"),
    )
    fail = projects_delete._remove_on_host(pg_engine, settings, {"target_id": target_id})
    assert fail["status"] == "failed"
    assert "255" in fail["reason"]
    assert discarded == [fake_key]


# ── BOSSAPP-1470: Watch this app dynamic cost & code-discovered watches ───────────────────────

def test_watch_app_cost_is_computed_dynamically():
    w4 = [
        {"kind": "error_rate", "tone": "wake", "enabled": True},
        {"kind": "health_check", "tone": "wake", "enabled": True},
        {"kind": "latency_p95", "tone": "tell", "enabled": True},
        {"kind": "deploy_health", "tone": "tell", "enabled": True},
    ]
    w6 = w4 + [
        {"kind": "no_traffic", "tone": "wake", "enabled": True},
        {"kind": "custom", "tone": "tell", "enabled": True},
    ]
    prom_est = watch_app.estimate_watch_cost(w4, {"kind": "prometheus"}, {"metrics": True, "probes": 10})
    boss_est = watch_app.estimate_watch_cost(w6, {"kind": "boss"}, {"metrics": False, "probes": 0})
    assert prom_est["daily_usd"] < boss_est["daily_usd"]
    assert prom_est["first_data_seconds"] == 30 and "30 s" in prom_est["formatted"]
    assert boss_est["first_data_seconds"] == 60 and "1 min" in boss_est["formatted"]
    assert prom_est["watch_count"] == 4 and boss_est["watch_count"] == 6

    # Ensure watch-app.tsx no longer falls back to the hardcoded "$0.20 a day" literal.
    tsx = (REPO_ROOT / "apps/web/app/projects/watch-app.tsx").read_text(encoding="utf-8")
    assert "≈ $0.20 a day" not in tsx


def test_watch_app_discovers_watches_from_project_code(tmp_path: Path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "server.py").write_text(
        'from prometheus_client import Counter\n'
        '@app.get("/healthz")\ndef health(): return {"ok": True}\n',
        encoding="utf-8",
    )
    (tmp_path / "src" / "worker.py").write_text(
        'def run_webhook_worker():\n    # celery worker_loop consumes intake events\n    pass\n',
        encoding="utf-8",
    )
    discovered = watch_app.discover_code_watches(tmp_path)
    by_kind = {d["kind"]: d for d in discovered}
    assert set(by_kind) == {"health_check", "latency_p95", "no_traffic"}
    assert by_kind["health_check"]["endpoint"] == "/healthz"
    assert by_kind["health_check"]["discovered_in"].endswith("src/server.py")
    assert by_kind["no_traffic"]["discovered_in"].endswith("src/worker.py")
