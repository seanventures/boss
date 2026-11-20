from types import SimpleNamespace

import pytest

from boss_engine.services import governed_code_task_service as service


@pytest.mark.parametrize("review,outcome,blocked,plan_ok", [
    ("The user asks for an app, not a document.\nPLAN: OK", "corrected", False, True),
    ("The app plan needs persistence tests.\nPLAN: REVISE", "corrected", False, False),
    ("ROUTING: NOT_A_CODE_TASK — the user requested only a PRD", "confirmed", True, False),
    ("Probably a coding task", "unresolved", True, False),
    ("", "unresolved", True, False),
    ("ROUTING: NOT_A_CODE_TASK — only a document\nPLAN: OK", "confirmed", True, False),
])
def test_recheck_is_read_only_bounded_and_requires_an_explicit_verdict(
        monkeypatch, tmp_path, review, outcome, blocked, plan_ok):
    monkeypatch.setattr(service, "set_llm_context", lambda **kwargs: None)
    calls = []

    def run_role(prompt, root, **kwargs):
        calls.append(prompt)
        assert "Build Delivery Stops" in prompt
        assert "Reordering persists" in prompt
        assert "stage-local" in prompt
        assert root == tmp_path
        assert kwargs == {"read_only": True, "model": "test-model"}
        return {"summary": review, "cost_usd_micros": 23}

    def route(stage):
        assert stage == "review_plan"
        return SimpleNamespace(run_role=run_role), "test-model"

    design = {"plan": "Add server.js and tests", "plan_ok": False,
              "plan_review": "ROUTING: NOT_A_CODE_TASK — planning prohibits editing"}
    args = dict(title="Delivery Stops", instructions="Build Delivery Stops",
                criteria=["Reordering persists"], root=tmp_path, route=route)
    result, cost = service._recheck_task_routing(design, **args)
    assert result["routing_recheck"]["outcome"] == outcome
    assert service._is_not_a_code_task(result["plan_review"]) == blocked
    assert result["plan_ok"] == plan_ok
    assert cost == 23
    assert service._recheck_task_routing(result, **args) == (result, 0)
    assert len(calls) == 1
    assert "routing_recheck" not in design


def test_normal_plan_needs_no_extra_model_call():
    design = {"plan_review": "PLAN: OK"}
    result = service._recheck_task_routing(
        design, title="App", instructions="Build app", criteria=[], root=None,
        route=lambda stage: pytest.fail("unnecessary routing call"))
    assert result == (design, 0)


def test_fallback_prompts_preserve_document_only_user_intent():
    request = "Write only a PRD. Do not implement the app."
    for prompt in (service._plan_prompt("App", "spec", instructions=request),
                   service._review_plan_prompt("App", "plan", instructions=request)):
        assert request in prompt
        assert "ORIGINAL REQUEST" in prompt
        assert "ROUTING: NOT_A_CODE_TASK" in prompt


def test_missing_original_request_cannot_be_approved(monkeypatch):
    monkeypatch.setattr(service, "set_llm_context", lambda **kwargs: None)
    agent = SimpleNamespace(run_role=lambda *args, **kwargs: {"summary": "PLAN: OK"})
    design = {"plan_review": "ROUTING: NOT_A_CODE_TASK — document", "plan_ok": True}
    result, _ = service._recheck_task_routing(
        design, title="App", instructions="", criteria=[], root=None,
        route=lambda stage: (agent, None))
    assert result["routing_recheck"]["outcome"] == "unresolved"
    assert service._is_not_a_code_task(result["plan_review"])
    assert result["plan_ok"] is False


def test_recheck_failure_does_not_approve_or_write(monkeypatch):
    monkeypatch.setattr(service, "set_llm_context", lambda **kwargs: None)

    def unavailable(*args, **kwargs):
        assert kwargs["read_only"] is True
        raise RuntimeError("review engine unavailable")

    design = {"plan_review": "ROUTING: NOT_A_CODE_TASK — document"}
    with pytest.raises(RuntimeError, match="review engine unavailable"):
        service._recheck_task_routing(
            design, title="App", instructions="Build app", criteria=[], root=None,
            route=lambda stage: (SimpleNamespace(run_role=unavailable), None))
    assert "routing_recheck" not in design
