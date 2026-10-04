"""Shared by the Repo and models API tests: auth, and the fake GitHub and model providers."""
from __future__ import annotations

import importlib.util
from pathlib import Path

from apps.api.app import main as api_main

ROOT = Path(__file__).resolve().parents[3]
PROJECT = "boss-dogfood"
AUTH = {"Authorization": "Bearer ceo-key"}


def stub(name: str):
    path = ROOT / "tests" / "e2e" / "stubs" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"_api_stub_{name}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def as_ceo(monkeypatch, *, edition: str = "community") -> None:
    monkeypatch.setattr(api_main.settings, "boss_edition", edition)
    monkeypatch.setattr(api_main.settings, "require_api_auth", True)
    monkeypatch.setattr(api_main.settings, "api_keys", "ceo-key")
    monkeypatch.setattr(api_main.settings, "api_key_principals", "ceo-key:ceo")
    for attr in ("github_token", "target_github_owner", "target_github_repo", "github_owner",
                 "github_repo", "openai_api_key",
                 "anthropic_api_key", "claude_code_oauth_token", "openrouter_api_key"):
        monkeypatch.setattr(api_main.settings, attr, "", raising=False)


def fakes(monkeypatch, *, github_calls=None, model_calls=None) -> None:
    """Route every probe to the fakes: GitHub by token profile, providers by key value. Also
    pins the secret store to the database (operator-chosen, so no consent is asked) and keeps
    CLI detection off this machine."""
    from boss_engine.services import secret_backends, setup_checks, setup_view

    github = stub("github_probes").http_for_tests(github_calls)
    model = stub("model_probes").http_for_tests(model_calls)

    def http(method, url, *, headers, json_body=None, timeout=15.0):
        if "api.github.com" in url:
            return github(method, url, headers=headers, json_body=json_body, timeout=timeout)
        return model(method, url, headers=headers, json_body=json_body, timeout=timeout)

    monkeypatch.setattr(setup_checks, "default_http", http)
    monkeypatch.setattr(secret_backends, "default_secret_backend", lambda _s: "database")
    monkeypatch.setenv("BOSS_COMMUNITY_SECRET_STORE", "database")
    setup_view.forget_cached()
    monkeypatch.setattr(setup_view, "_detect_clis", lambda _s: {})


def view(client) -> dict:
    response = client.get("/v1/setup", params={"project_id": PROJECT}, headers=AUTH)
    assert response.status_code == 200, response.text
    return response.json()


def connect_repository(client, token: str = "ghp_e2e_full", full_name: str = "e2e/app",
                       **extra) -> dict:
    found = client.post("/v1/setup/repository/token", headers=AUTH,
                        json={"project_id": PROJECT, "token": token, **extra})
    assert found.status_code == 200, found.text
    picked = client.post("/v1/setup/repository/select", headers=AUTH,
                         json={"project_id": PROJECT,
                               "connection_id": found.json()["connection_id"],
                               "full_name": full_name})
    assert picked.status_code == 200, picked.text
    return picked.json()


def connect_key(client, key: str = "sk-stub-ok", provider: str = "openrouter",
                model: str = "stub/small", **extra):
    return client.post("/v1/setup/models", headers=AUTH,
                       json={"project_id": PROJECT, "provider": provider, "kind": "api_key",
                             "api_key": key, "model": model, **extra})
