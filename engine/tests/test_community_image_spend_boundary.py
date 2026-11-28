"""Image generation behaves identically across editions and still enforces provider/credential gates."""

from contextlib import nullcontext
from types import SimpleNamespace
import os
import urllib.request

import pytest

from boss_engine.services import model_routing
from boss_engine.tools.builtin import build_default_registry, build_discuss_read_registry


@pytest.mark.parametrize("edition", ["community", "oss", "local", "unrecognized", "enterprise_self_hosted"])
def test_image_generation_exposed_uniformly_and_checks_provider_before_network(monkeypatch, edition):
    def unexpected(*_args, **_kwargs):
        raise AssertionError("provider or file effect started")

    monkeypatch.setattr(urllib.request, "urlopen", unexpected)
    monkeypatch.setattr(os, "makedirs", unexpected)
    monkeypatch.setattr(
        model_routing, "provider_credential",
        lambda *_a, **_k: (_ for _ in ()).throw(
            model_routing.ProviderRequirementMissing(
                "generate_image",
                model_routing.requirement("openai_images"),
            ),
        ),
    )
    assert "generate_image" in build_discuss_read_registry(edition=edition).names()

    ctx = SimpleNamespace(
        settings=SimpleNamespace(
            boss_edition=edition, openai_base_url="https://example.invalid/v1",
            openai_api_key="test-only",
        ),
        begin=lambda: nullcontext(object()),
    )
    result = build_default_registry().invoke(
        "generate_image", ctx, project_id="own", actor="owner",
        args={"prompt": "a blue square", "path": "sample.png"},
    )
    assert result["ok"] is False
    assert result.get("needs") == "provider:openai_images"
