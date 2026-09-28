"""Community image generation must stop before provider spend or file output."""

from types import SimpleNamespace
import os
import urllib.request

import pytest

from boss_engine.tools.builtin import build_default_registry, build_discuss_read_registry


@pytest.mark.parametrize("edition", ["community", "oss", "local", "unrecognized"])
def test_image_generation_stops_before_network_or_file_effect(monkeypatch, edition):
    def unexpected(*_args, **_kwargs):
        raise AssertionError("provider or file effect started")

    monkeypatch.setattr(urllib.request, "urlopen", unexpected)
    monkeypatch.setattr(os, "makedirs", unexpected)
    assert "generate_image" not in build_discuss_read_registry(edition=edition).names()

    ctx = SimpleNamespace(settings=SimpleNamespace(
        boss_edition=edition, openai_base_url="https://example.invalid/v1",
        openai_api_key="test-only",
    ))
    result = build_default_registry().invoke(
        "generate_image", ctx, project_id="own", actor="owner",
        args={"prompt": "a blue square", "path": "sample.png"},
    )
    assert result["ok"] is False
    assert "budget reservation" in result["error"]
