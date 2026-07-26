from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from apps.api.app.config import get_settings
from apps.api.app.services import preview_service
from apps.api.app.services.preview_service import (
    _detect_command,
    _inject_shim,
    reconcile_previews,
    start_rootfs_preview,
)
from apps.api.app.services.execution_substrate import (
    ExecutionSubstrate,
    normalize_build_substrate,
)


def _insert_run(engine, run_id: str = "crun_feedback", *, substrate: str = "local_subprocess"):
    settings = get_settings()
    spec = {
        "instructions": "Improve the checkout page.",
        "acceptance_criteria": ["checkout remains usable"],
        "test_commands": ["python3 -m pytest"],
        "owner": "acme", "repo": "widget", "base_branch": "main",
    }
    fctx = {"execution_substrate": substrate, "attempt": 1}
    if substrate == "microvm":
        fctx["sandbox_task_id"] = "sbx_retained"
        fctx["sandbox_task_ids"] = ["sbx_retained"]
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO code_task_runs "
            "(id,project_id,status,phase,title,spec,state,finalize_ctx) "
            "VALUES (:id,:p,'completed','done','Checkout task',CAST(:spec AS JSONB),"
            "'{}'::jsonb,CAST(:fctx AS JSONB))"
        ), {"id": run_id, "p": settings.default_project_id,
            "spec": json.dumps(spec), "fctx": json.dumps(fctx)})
    return settings


def test_detect_command_honors_repo_hint_and_common_app_conventions(tmp_path):
    (tmp_path / "boss.preview.command").write_text("uv run app.py --serve")
    assert _detect_command(tmp_path) == ["sh", "-lc", "uv run app.py --serve"]

    (tmp_path / "boss.preview.command").unlink()
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {"dev": "vite"}}))
    command = _detect_command(tmp_path)
    assert command and "npm run dev -- --host 0.0.0.0" in command[-1]

    (tmp_path / "package.json").unlink()
    (tmp_path / "Procfile").write_text("web: gunicorn app:app\n")
    assert _detect_command(tmp_path) == ["sh", "-lc", "gunicorn app:app"]

    (tmp_path / "Procfile").unlink()
    (tmp_path / "Makefile").write_text("dev:\n\tpython3 app.py\n")
    assert _detect_command(tmp_path) == ["make", "dev"]


def test_preview_proxy_shim_rewrites_static_dynamic_spa_and_live_assets():
    html = (
        b'<html><head><link href="/app.css"></head><body>'
        b'<a href="/account">Account</a><img src="/hero.png" '
        b'srcset="/hero-1.png 1x, /hero-2.png 2x" style="background:url(/bg.png)"></body></html>'
    )
    rendered = _inject_shim(html, "/v1/previews/prev_1/web").decode()
    assert '<base href="/v1/previews/prev_1/web/">' in rendered
    assert 'href="/v1/previews/prev_1/web/app.css"' in rendered
    assert 'src="/v1/previews/prev_1/web/hero.png"' in rendered
    assert "/v1/previews/prev_1/web/hero-2.png 2x" in rendered
    assert "history[k]" in rendered
    assert "window.WebSocket" in rendered
    assert "window.EventSource" in rendered
    assert "Send preview feedback" in rendered
    assert "of.call(window,F" in rendered


def test_reconcile_keeps_healthy_preview_and_stops_only_dead_one(engine, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(preview_service, "_respawn_process_preview", lambda *_args: None)
    expires = datetime.now(timezone.utc) + timedelta(hours=1)
    with engine.begin() as connection:
        for preview_id, pid, url in (
            ("prev_alive", 101, "http://127.0.0.1:4101"),
            ("prev_dead", 102, "http://127.0.0.1:4102"),
        ):
            connection.execute(text(
                "INSERT INTO app_previews "
                "(id,project_id,status,mode,pid,url,expires_at) "
                "VALUES (:id,:p,'running','process',:pid,:url,:exp)"
            ), {"id": preview_id, "p": settings.default_project_id, "pid": pid,
                "url": url, "exp": expires})
    monkeypatch.setattr(preview_service, "_process_alive", lambda pid: pid == 101)
    monkeypatch.setattr(preview_service, "_url_ok", lambda url, **_kwargs: url.endswith("4101"))

    first = reconcile_previews(engine, settings)
    second = reconcile_previews(engine, settings)
    assert first == {
        "checked": 2, "running": 1, "stopped": 1, "expired": 0, "respawning": 1
    }
    assert second == {
        "checked": 1, "running": 1, "stopped": 0, "expired": 0, "respawning": 0
    }
    with engine.begin() as connection:
        rows = dict(connection.execute(text(
            "SELECT id,status FROM app_previews WHERE id IN ('prev_alive','prev_dead')"
        )).all())
    assert rows == {"prev_alive": "running", "prev_dead": "stopped"}


def test_rootfs_preview_uses_retained_microvm_without_reclone(engine, monkeypatch):
    settings = _insert_run(engine, "crun_rootfs", substrate="microvm")
    calls: list[tuple] = []
    monkeypatch.setattr(
        preview_service.MicroVMProvider, "hold",
        lambda _self, sid, **kwargs: calls.append(("hold", sid, kwargs)) or True,
    )
    monkeypatch.setattr(
        preview_service.MicroVMProvider, "serve_preview",
        lambda _self, sid, **kwargs: calls.append(("serve", sid, kwargs)) or {
            "url": "http://sandbox.test/v1/sandbox/tasks/sbx_retained/preview/web/",
            "command": "npm run dev",
        },
    )
    result = start_rootfs_preview(engine, settings, "crun_rootfs", ttl_minutes=30)
    assert result["substrate"] == "preview_rootfs"
    assert result["sandbox_task_id"] == "sbx_retained"
    assert [call[0] for call in calls] == ["hold", "serve"]
    with engine.begin() as connection:
        row = connection.execute(text(
            "SELECT mode,substrate,build_substrate,sandbox_task_id FROM app_previews WHERE id=:id"
        ), {"id": result["id"]}).mappings().one()
    assert dict(row) == {
        "mode": "rootfs", "substrate": "preview_rootfs",
        "build_substrate": "microvm", "sandbox_task_id": "sbx_retained",
    }


def test_feedback_agent_clarifies_then_confirmation_dispatches_structured_retry(client, engine):
    _insert_run(engine)
    vague = client.post(
        "/v1/code-tasks/crun_feedback/feedback", json={"message": "the button doesn't work"}
    )
    assert vague.status_code == 200
    assert vague.json()["status"] == "awaiting_clarification"
    assert vague.json()["dispatched"] is False

    proposed = client.post(
        "/v1/code-tasks/crun_feedback/feedback",
        json={
            "message": "On checkout, clicking Pay leaves the form idle.",
            "expected": "Show progress and submit exactly once.",
            "anchor": "/checkout", "selected_element": "button#pay", "severity": "high",
        },
    )
    assert proposed.status_code == 200
    body = proposed.json()
    assert body["status"] == "awaiting_confirmation"
    assert body["proposal"]["classification"] == "scoped_tweak"
    assert "Observed behavior:" in body["proposal"]["fix_note"]
    assert "Anchor URL / route: /checkout" in body["proposal"]["fix_note"]

    confirmed = client.post(f"/v1/feedback-threads/{body['thread_id']}/confirm", json={"actor": "ceo"})
    assert confirmed.status_code == 200
    result = confirmed.json()
    assert result["dispatched"] is True
    with engine.begin() as connection:
        retry = connection.execute(text(
            "SELECT spec,finalize_ctx FROM code_task_runs WHERE id=:id"
        ), {"id": result["run_id"]}).mappings().one()
    assert retry["spec"]["profile"] == "balanced"
    assert retry["spec"]["repair_retries"] == 1
    assert "## Feedback to address" in retry["spec"]["instructions"]
    assert retry["finalize_ctx"]["root_run_id"] == "crun_feedback"
    duplicate = client.post(
        f"/v1/feedback-threads/{body['thread_id']}/confirm", json={"actor": "ceo"}
    )
    assert duplicate.status_code == 409
    with engine.begin() as connection:
        attempts = connection.execute(text(
            "SELECT count(*) FROM code_task_runs WHERE finalize_ctx->>'root_run_id'='crun_feedback'"
        )).scalar_one()
    assert attempts == 1


def test_preview_anchored_feedback_captures_route_without_dispatch(client, engine):
    settings = _insert_run(engine, "crun_anchor")
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO app_previews (id,project_id,run_id,status,expires_at) "
            "VALUES ('prev_anchor',:p,'crun_anchor','running',:exp)"
        ), {"p": settings.default_project_id,
            "exp": datetime.now(timezone.utc) + timedelta(hours=1)})
    response = client.post(
        "/v1/previews/prev_anchor/feedback",
        json={"message": "Change the pricing link label to View plans.",
              "anchor": "/pricing", "selected_element": "a#pricing"},
    )
    assert response.status_code == 200
    assert response.json()["dispatched"] is False
    assert "Anchor URL / route: /pricing" in response.json()["proposal"]["fix_note"]


def test_feedback_agent_routes_cross_app_architecture_change_to_strict_rerun(client, engine):
    _insert_run(engine, "crun_architecture")
    response = client.post(
        "/v1/code-tasks/crun_architecture/feedback",
        json={
            "message": "Refactor authentication architecture across the app to use one session boundary.",
            "expected": "Every page uses the new session boundary.",
        },
    )
    assert response.status_code == 200
    proposal = response.json()["proposal"]
    assert proposal["classification"] == "full_rerun"
    assert proposal["profile"] == "strict"
    assert proposal["repair_retries"] == 2


def test_execution_substrate_normalization_is_explicit_and_validated():
    assert normalize_build_substrate("local") is ExecutionSubstrate.LOCAL_SUBPROCESS
    assert normalize_build_substrate("firecracker") is ExecutionSubstrate.MICROVM
    try:
        normalize_build_substrate("mystery")
    except ValueError as exc:
        assert "Unsupported execution substrate" in str(exc)
    else:
        raise AssertionError("unknown substrate should fail closed")
