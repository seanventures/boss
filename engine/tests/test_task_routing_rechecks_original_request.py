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
    # The verdict grammar every plan review uses: last marker wins, markdown and notes allowed.
    ("The user asks for an app.\n**PLAN: OK**", "corrected", False, True),
    ("The user asks for an app.\nPLAN: OK\n\nNote: add an index later.", "corrected", False, True),
    ("PLAN: OK\nROUTING: UNRESOLVED — the request names no deliverable", "unresolved", True, False),
])
def test_recheck_is_read_only_bounded_and_requires_an_explicit_verdict(
        monkeypatch, tmp_path, review, outcome, blocked, plan_ok):
    monkeypatch.setattr(service, "set_llm_context", lambda **kwargs: None)
    calls = []

    def run_role(prompt, root, **kwargs):
        calls.append(prompt)
        assert "Build Delivery Stops" in prompt
        assert "- Reordering persists" in prompt and "['Reordering persists']" not in prompt
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
    assert cost == 23 and result["routing_recheck"]["cost_usd_micros"] == 23
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


def test_recheck_failure_keeps_the_block_and_never_crashes_the_build(monkeypatch):
    """The extra call raising is `unresolved` with the error recorded: the block stays (fail
    closed), no code is written, and the build ends with its own reason, not a traceback."""
    monkeypatch.setattr(service, "set_llm_context", lambda **kwargs: None)

    def unavailable(*args, **kwargs):
        assert kwargs["read_only"] is True
        raise RuntimeError("review engine unavailable")

    design = {"plan_review": "ROUTING: NOT_A_CODE_TASK — document", "plan_ok": True}
    result, cost = service._recheck_task_routing(
        design, title="App", instructions="Build app", criteria=[], root=None,
        route=lambda stage: (SimpleNamespace(run_role=unavailable), None))
    assert cost == 0
    assert result["routing_recheck"]["outcome"] == "unresolved"
    assert "review engine unavailable" in result["routing_recheck"]["error"]
    assert service._is_not_a_code_task(result["plan_review"]) and result["plan_ok"] is False
    assert "routing_recheck" not in design


def test_criteria_are_bullet_lines_in_both_fallback_prompts():
    for prompt in (service._plan_prompt("App", "spec", instructions="Build it",
                                        criteria=["Reordering persists", "Stops sync"]),
                   service._review_plan_prompt("App", "plan", instructions="Build it",
                                               criteria=["Reordering persists", "Stops sync"])):
        assert "- Reordering persists\n- Stops sync" in prompt
        assert "[" not in prompt.split("ACCEPTANCE CRITERIA:")[1].split("\n\n")[0]
    assert "- (none specified)" in service._plan_prompt("App", "spec")
    assert "(not given)" in service._review_plan_prompt("App", "plan")


def test_scenario_a_build_brief_falsely_routed_as_not_code_is_corrected(monkeypatch):
    """REPLAY (enterprise install, 2026-11-19): the person asked BOSS to build a web app; the
    fallback reviewer read BOSS's own planning rule ("Do NOT edit any files") as the person
    forbidding code and answered NOT_A_CODE_TASK, so the build was blocked and no code written.
    The recheck sees the person's words and BOSS's stage rule told apart; a code verdict, even
    in markdown with a note after it, carries the build on to coverage and approval."""
    monkeypatch.setattr(service, "set_llm_context", lambda **kwargs: None)
    request = ("Build a Delivery Stops web app: a driver sees today's stops on a map and can "
               "reorder them; the order persists across reloads.")
    seen = {}

    def run_role(prompt, root, **kwargs):
        seen["prompt"] = prompt
        return {"summary": "The person asks for a working web app; the no-edit rule is this "
                           "planning stage's, not theirs.\n**PLAN: OK**\n\nNote: add an index "
                           "on stop order.", "cost_usd_micros": 41}

    first = {"plan": "server.js, stops.html, a stops table with a position column",
             "plan_ok": False,
             "plan_review": "The task says Do NOT edit any files, so nothing is to be built.\n"
                            "ROUTING: NOT_A_CODE_TASK — the deliverable is the plan itself"}
    result, cost = service._recheck_task_routing(
        first, title="Delivery Stops", instructions=request,
        criteria=["Reordering persists across reloads"], root=None,
        route=lambda stage: (SimpleNamespace(run_role=run_role), None))
    assert request in seen["prompt"] and "- Reordering persists across reloads" in seen["prompt"]
    assert "stage-local" in seen["prompt"] and first["plan_review"] in seen["prompt"]
    assert not service._is_not_a_code_task(result["plan_review"]) and result["plan_ok"] is True
    assert result["routing_recheck"]["outcome"] == "corrected"
    assert result["routing_recheck"]["previous"] == first["plan_review"]
    assert cost == 41
