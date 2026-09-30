"""A lane never inherits a model that belongs to a different coding harness."""

from boss_engine.config import Settings
from boss_engine.services import project_settings as ps


def _deployment(**changes):
    return Settings().model_copy(update={
        "code_agent_engine": "claude_code",
        "code_agent_model": "",
        "code_review_engine": "",
        "code_review_model": "",
        "code_agent_strong_engine": "",
        "code_agent_strong_model": "opus",
        "code_agent_low_cost_engine": "",
        "code_agent_low_cost_model": "haiku",
        **changes,
    })


def test_project_engine_switch_masks_inherited_claude_model(monkeypatch):
    monkeypatch.setattr(ps, "get_all", lambda *_: {"code_agent_strong_engine": "codex"})
    deployment = _deployment()

    effective = ps.applied(object(), "project", deployment)

    assert effective.code_agent_strong_engine == "codex"
    assert effective.code_agent_strong_model == ""
    assert deployment.code_agent_strong_model == "opus"
    assert effective.code_agent_low_cost_model == "haiku"


def test_implementer_switch_masks_inherited_models_in_other_lanes(monkeypatch):
    monkeypatch.setattr(ps, "get_all", lambda *_: {"code_agent_engine": "codex"})
    effective = ps.applied(object(), "project", _deployment(code_review_model="opus"))

    assert effective.code_agent_engine == "codex"
    assert effective.code_review_model == ""
    assert effective.code_agent_strong_model == ""
    assert effective.code_agent_low_cost_model == ""


def test_compatible_explicit_project_model_is_preserved(monkeypatch):
    monkeypatch.setattr(ps, "get_all", lambda *_: {
        "code_agent_strong_engine": "codex",
        "code_agent_strong_model": "gpt-6-codex-preview",
    })

    effective = ps.applied(object(), "project", _deployment())

    assert effective.code_agent_strong_model == "gpt-6-codex-preview"


def test_old_incompatible_project_model_is_masked(monkeypatch):
    monkeypatch.setattr(ps, "get_all", lambda *_: {
        "code_agent_strong_engine": "codex",
        "code_agent_strong_model": "opus",
    })
    assert ps.applied(object(), "project", _deployment()).code_agent_strong_model == ""


def test_deployment_pair_is_checked_without_project_overrides():
    bad = _deployment(code_agent_engine="codex", code_agent_model="opus")
    effective = ps.applied(None, None, bad)

    assert effective.code_agent_model == ""
    assert effective.code_agent_strong_model == ""
    assert effective.code_agent_low_cost_model == ""


def test_compatible_deployment_models_are_unchanged():
    deployment = _deployment()
    assert ps.applied(None, None, deployment) is deployment
    assert ps.model_is_compatible_with_harness("opus", "claude_code")
    assert not ps.model_is_compatible_with_harness("opus", "codex")
    assert ps.model_is_compatible_with_harness("gpt-6-codex-preview", "codex")
    assert ps.model_is_compatible_with_harness("gpt-6", "native")
