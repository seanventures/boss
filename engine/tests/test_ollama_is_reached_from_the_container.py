"""Local › Ollama reaches the HOST's Ollama from the Community containers (audit P1-5).

The drawer was pre-filled with http://127.0.0.1:11434/v1 and the backend fell back to the same
address: inside the api container that is the container itself, so the row was saved as "Can't
reach", detection never found the host's Ollama (which answered at host.docker.internal), and
`unreachable` still counted as usable, so Auto could pick it. Now one setting decides where BOSS
looks — OLLAMA_BASE_URL, which the Community compose sets for api and worker — and a local
model that does not answer is never usable.
"""
from __future__ import annotations

from pathlib import Path

import yaml

from boss_engine.config import Settings
from boss_engine.services import setup_actions, setup_checks, setup_service, setup_view

REPO = Path(__file__).resolve().parents[2]
HOST_OLLAMA = "http://host.docker.internal:11434/v1"


def _compose() -> dict:
    return yaml.safe_load((REPO / "scripts/install/community-compose.yml").read_text())


def test_the_community_compose_points_api_and_worker_at_the_hosts_ollama():
    services = _compose()["services"]
    for name in ("api", "worker"):
        service = services[name]
        assert service["environment"]["OLLAMA_BASE_URL"] == \
            "${OLLAMA_BASE_URL:-http://host.docker.internal:11434/v1}", name
        # ...and host.docker.internal resolves there on Linux too.
        assert "host.docker.internal:host-gateway" in service.get("extra_hosts", []), name


def test_the_setting_decides_and_a_dev_run_keeps_localhost(monkeypatch):
    monkeypatch.delenv("OLLAMA_BASE_URL", raising=False)
    assert setup_service.ollama_base_url(Settings()) == "http://localhost:11434/v1"
    monkeypatch.setenv("OLLAMA_BASE_URL", HOST_OLLAMA)
    assert setup_service.ollama_base_url(Settings()) == HOST_OLLAMA
    # No hardcoded loopback is left to fall back to.
    assert setup_service.PROVIDERS["ollama"]["base_url"] == ""


def _settings(monkeypatch) -> Settings:
    monkeypatch.setenv("OLLAMA_BASE_URL", HOST_OLLAMA)
    return Settings()


def test_detection_and_the_model_list_look_where_the_setting_says(monkeypatch):
    settings = _settings(monkeypatch)
    asked: list[str] = []

    def list_models(*, base_url, **_kw):
        asked.append(base_url)
        return ["qwen2.5-coder:7b"]

    remembered = {}
    monkeypatch.setattr(setup_checks, "list_models", list_models)
    monkeypatch.setattr(setup_view, "remember_local",
                        lambda provider, base, names: remembered.update({provider: base}))
    setup_actions.detect_local(settings)
    setup_actions._LISTS.clear()
    names = setup_actions.model_catalogue(None, settings, "p", "ollama")
    assert asked == [HOST_OLLAMA, HOST_OLLAMA]
    assert names == ["qwen2.5-coder:7b"]
    assert remembered == {"ollama": HOST_OLLAMA}


def test_the_drawer_placeholder_comes_from_the_setting(monkeypatch):
    settings = _settings(monkeypatch)
    entry = next(e for e in setup_view._catalogue(settings) if e["provider"] == "ollama")
    assert entry["example_base_url"] == HOST_OLLAMA
    # The module-level catalogue is not mutated per request.
    assert "example_base_url" not in setup_view.CATALOGUE_BY_PROVIDER["ollama"]


def _row(kind: str, state: str, base_url: str = "", provider: str = "ollama") -> dict:
    return {"provider": provider, "kind": kind, "base_url": base_url,
            "pill": {"state": state}}


def test_an_unreachable_local_model_is_not_usable_for_auto():
    assert not setup_view.model_working(_row("local", "unreachable", HOST_OLLAMA))
    # `boss connect ollama` saves an OpenAI-compatible row at host.docker.internal: same rule.
    assert not setup_view.model_working(_row("metered", "unreachable", HOST_OLLAMA, "custom"))
    # A hosted provider that is briefly unreachable recovers by itself and stays usable.
    assert setup_view.model_working(_row("metered", "unreachable",
                                         "https://api.openai.com/v1", "openai"))
    assert setup_view.model_working(_row("local", "working", HOST_OLLAMA))
    assert setup_view.model_working(_row("local", "not_checked", HOST_OLLAMA))
