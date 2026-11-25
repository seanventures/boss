"""BOSSAPP-1392 & BOSSAPP-1401: Command Center local-repo recognition, plain-http custom model
connect-vs-build parity, and stuck_work no doubled 'It failed: ' prefix.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

from sqlalchemy import text

from apps.api.tests.setup_helpers import PROJECT, as_ceo, connect_key, fakes
from boss_engine.services import (
    command_center,
    command_center_view as ccv,
    env_refs,
    local_repo,
    secret_backends,
    setup_view,
    stuck_work,
)


def test_command_center_recognizes_org_local_repo_like_setup(engine, tmp_path):
    owner = f"local_proj_{uuid.uuid4().hex[:8]}"
    repo_name = "hello-world"
    settings = SimpleNamespace(
        local_repo_root=str(tmp_path),
        default_project_id=None,
    )
    repo_dir = local_repo.bare_path(owner, repo_name, settings)
    repo_dir.mkdir(parents=True, exist_ok=True)
    (repo_dir / "HEAD").write_text("ref: refs/heads/main\n")

    with engine.begin() as c:
        wid = c.execute(text("SELECT workspace_id FROM projects WHERE id = :p"), {"p": PROJECT}).scalar()
        child_id = f"p_local_{uuid.uuid4().hex[:8]}"
        c.execute(text(
            "INSERT INTO projects (id, workspace_id, name, slug) VALUES (:i, :w, 'Hello World', :s)"),
            {"i": child_id, "w": wid, "s": child_id})
        c.execute(text(
            "INSERT INTO github_repository_bindings (id, project_id, full_name, owner, name, "
            "host, role, status, created_by) VALUES (:id, :p, :fn, :o, :n, 'local', 'primary', 'active', 'ceo')"),
            {"id": f"grb_{uuid.uuid4().hex[:8]}", "p": child_id,
             "fn": f"{owner}/{repo_name}", "o": owner, "n": repo_name})

        settings_home = SimpleNamespace(local_repo_root=str(tmp_path), default_project_id=PROJECT)
        basics_view = setup_view.build_basics(c, settings_home, PROJECT)
        assert basics_view["repository"]["local"] is not None
        assert setup_view.basics(basics_view)["github"] is True

        gaps = command_center.setup_gaps(c, settings_home, PROJECT)
        assert not any(g["id"] == "github" for g in gaps)

        dots = {d["key"]: d for d in ccv.systems(
            probe={"previews": [], "targets": 0, "deploy": None, "calls": [], "incidents": []},
            basics=basics_view, budget={"budget_state": "green", "cap_usd_micros": 1000},
            host={"show": False}, now=datetime.now(timezone.utc))}
        assert dots["repo"]["tone"] == "green"
        assert dots["repo"]["state"] == "OK"


def test_custom_private_http_parity_and_public_http_refusal(client, engine, monkeypatch):
    as_ceo(monkeypatch)
    calls: list = []
    fakes(monkeypatch, model_calls=calls)

    # Public plain-http 'Other' URL is refused at connect time (before any call).
    public_res = connect_key(client, provider="custom", model="m",
                             base_url="http://public.example.com/v1")
    assert public_res.status_code == 422
    assert public_res.json()["detail"]["headline"] == "Base URL"
    assert "https" in public_res.json()["detail"]["fix"].lower()
    assert calls == []

    # Metadata/link-local plain-http is also refused at connect time.
    meta_res = connect_key(client, provider="custom", model="m",
                           base_url="http://169.254.169.254/latest")
    assert meta_res.status_code == 422
    assert calls == []

    # Private RFC1918 / loopback / single-label plain-http 'Other' passes connect AND build guard.
    priv_res = connect_key(client, provider="custom", model="m",
                           base_url="http://10.0.0.7:8080/v1")
    assert priv_res.status_code == 200, priv_res.text

    binding = env_refs.provider_binding("custom", ["10.0.0.7", "vllm", "127.0.0.1"],
                                        allow_private_http=True)
    assert env_refs.destination_allowed(binding, "http://10.0.0.7:8080/v1/chat/completions") is True
    assert env_refs.destination_allowed(binding, "http://vllm:8000/v1/chat/completions") is True
    assert env_refs.destination_allowed(binding, "http://127.0.0.1:8000/v1/chat/completions") is True
    assert env_refs.destination_allowed(binding, "http://169.254.169.254/v1") is False

    # GitHub (and non-custom providers) still strictly refuse plain-http even on 127.0.0.1.
    gh = env_refs.provider_binding("github", ["127.0.0.1"])
    assert env_refs.destination_allowed(gh, "http://127.0.0.1:8080/") is False

    # secret_backends._hold_to_provider grants allow_private_http ONLY to provider='custom'.
    env_refs._registered.clear()
    secret_backends._hold_to_provider(
        "sref_custom",
        {"scope_type": "org", "provider": "custom", "metadata": {"allowed_hosts": ["10.0.0.7"]}},
        "sk-custom-canary-0123456789abcdef",
    )
    env_refs.check_outbound("http://10.0.0.7:8080/v1/chat/completions",
                            {"Authorization": "Bearer sk-custom-canary-0123456789abcdef"})
    env_refs._registered.clear()


def test_stuck_work_does_not_double_it_failed_prefix():
    got = stuck_work.triage({"run": {"id": "crun_1", "status": "failed",
                                     "error": "Failed: disk full"}})
    assert got["cause_code"] == "failed"
    assert got["cause"] == "disk full"
    assert "It failed:" not in got["cause"]
    got2 = stuck_work.triage({"run": {"id": "crun_2", "status": "failed",
                                      "error": "It failed: out of memory"}})
    assert got2["cause"] == "out of memory"
