"""Deterministic NL process compiler — no LLM, no scores, fail-closed."""

import pytest

from boss_engine.services.agent_process import PRD_TO_ACCEPTED_SPEC, validate_process_spec
from boss_engine.services.process_compiler import compile_preview, compile_process_from_text


DEFAULT_STORY = (
    "PRD delivered, architect, CTO, and VP product review, improve, "
    "captain builds, QA, UI/UX, captain verifies, panel certifies, captain accepts"
)


def _step_ids(spec: dict) -> list[str]:
    return [step["id"] for step in spec["steps"]]


def _actions(spec: dict) -> set[str]:
    return {step["action"] for step in spec["steps"]}


def _reachable(spec: dict) -> set[str]:
    by_id = {step["id"]: step for step in spec["steps"]}
    seen: set[str] = set()
    queue = [spec["entry"]]
    while queue:
        step_id = queue.pop(0)
        if step_id in seen or step_id not in by_id:
            continue
        seen.add(step_id)
        queue.extend(by_id[step_id].get("next") or [])
    return seen


def test_default_story_compiles_and_validates():
    spec = validate_process_spec(compile_process_from_text(DEFAULT_STORY))
    default = validate_process_spec(PRD_TO_ACCEPTED_SPEC)
    assert set(_step_ids(spec)) == set(_step_ids(default))
    assert _actions(spec) == _actions(default)
    assert spec["entry"] == default["entry"]
    assert compile_process_from_text(DEFAULT_STORY) == compile_process_from_text(DEFAULT_STORY)
    described = validate_process_spec(
        compile_process_from_text(PRD_TO_ACCEPTED_SPEC["description"])
    )
    assert set(_step_ids(described)) == set(_step_ids(default))


def test_parallel_reviews_get_wait_for():
    spec = validate_process_spec(
        compile_process_from_text(
            "PRD is delivered then checked by architect, cto, and vp "
            "then product manager improves"
        )
    )
    reviews = [step for step in spec["steps"] if step["action"] == "review_prd"]
    assert {step["role"] for step in reviews} == {"chief_architect", "cto", "vp_product"}
    join = next(step for step in spec["steps"] if step["action"] == "improve_prd")
    assert set(join["wait_for"]) == {step["id"] for step in reviews}
    intake = next(step for step in spec["steps"] if step["id"] == spec["entry"])
    assert set(intake["next"]) == {step["id"] for step in reviews}


def test_unknown_role_raises():
    with pytest.raises(ValueError, match="role"):
        compile_process_from_text("wizard reviews the prd")


def test_unknown_action_raises():
    with pytest.raises(ValueError, match="action"):
        compile_process_from_text("captain deploys the service")


def test_empty_text_raises():
    with pytest.raises(ValueError):
        compile_process_from_text("")
    with pytest.raises(ValueError):
        compile_process_from_text("   \n\t")


def test_compiled_default_can_complete_conceptually():
    spec = validate_process_spec(compile_process_from_text(DEFAULT_STORY))
    assert spec["entry"]
    last = spec["steps"][-1]
    assert last["action"] == "accept_work"
    assert last.get("next") in ([], None)
    reachable = _reachable(spec)
    assert spec["entry"] in reachable
    assert last["id"] in reachable


def test_compile_preview_returns_spec_and_warnings_without_scores():
    preview = compile_preview(DEFAULT_STORY)
    assert "spec" in preview
    assert "warnings" in preview
    validate_process_spec(preview["spec"])
    assert isinstance(preview["warnings"], list)
    dumped = repr(preview).lower()
    assert "score" not in dumped
    assert "confidence" not in dumped
    assert "probability" not in dumped
