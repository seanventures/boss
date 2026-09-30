"""Skill executor registry — the model.generate_code binding (Plane #298)."""
from __future__ import annotations

import pytest

from boss_engine.services.skill_executors import (
    SkillExecutorConfigurationError,
    _model_generate_code_dedupe_key,
    _split_patch_and_test_plan,
    registered_executor_ids,
    resolve_executor,
)


def test_model_generate_code_is_registered():
    assert "model.generate_code" in registered_executor_ids()


def test_resolve_executor_requires_context():
    execute = resolve_executor("model.generate_code")
    assert execute is not None
    with pytest.raises(SkillExecutorConfigurationError):
        execute({"task_spec": "x"})


def test_unbacked_skill_still_resolves_to_none():
    assert resolve_executor("model.summarize_no_such_skill") is None


def test_split_patch_and_test_plan():
    content = (
        "```diff\n--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-old\n+new\n```\n"
        "TEST PLAN:\nrun pytest tests/test_x.py\n"
    )
    patch, plan = _split_patch_and_test_plan(content)
    assert patch.startswith("--- a/x.py")
    assert patch.endswith("+new")
    assert plan == "run pytest tests/test_x.py"


def test_split_tolerates_missing_sections():
    patch, plan = _split_patch_and_test_plan("no fences here")
    assert patch == "" and plan == ""


def test_generate_code_dedupe_includes_constraints():
    """Same task/repo under different constraints must not collide."""
    base = dict(
        project_id="p1",
        task_spec="add login",
        repo_context="app/main.py",
    )
    ts = _model_generate_code_dedupe_key(**base, constraints=["TypeScript"])
    py = _model_generate_code_dedupe_key(**base, constraints=["Python"])
    same = _model_generate_code_dedupe_key(**base, constraints=["TypeScript"])
    assert ts != py
    assert ts == same
    assert ts.startswith("model.generate_code:")
