"""Ask BOSS authors workflows instead of refusing them (BOSSAPP-1351).

Asking in prose for an "API onboarding" workflow (validate a tenant config with gwlint, reject
with a report, fix PR, deploy to staging, contract tests, a person approves, promote, smoke
check) answered "unknown action 'deploy'" / "unknown role 'contract'": the ask went to the
role-handoff process compiler, whose closed vocabulary refused the words of a real delivery.

Driven through `route_turn` with the stub decision model: no real model, no network.
"""
import types

import pytest

from ._decision_stub import decided
from boss_engine.config import Settings
from boss_engine.services import process_compiler
from boss_engine.services.intent_router import (
    IntentRouter, RouterTurn, _DECIDED_MEANS, _decision_note)
from boss_engine.services.process_compiler import ProcessVocabularyError, compile_process
from boss_engine.services.turn_budget import turn_depth
from boss_engine.services.workflows import validate_workflow_spec
from boss_engine.tools.builtin import _spec_from_steps, build_default_registry

ASKS = [
    "create an API onboarding workflow: validate a tenant config with gwlint, reject with a "
    "report and suggested fix, open a fix PR, deploy to staging, run contract tests, a person "
    "approves, promote to production, then a smoke check",
    "design a workflow where we lint the tenant config, deploy it to staging, run the contract "
    "tests, get a release approval and then ship it to production",
    "Can you make me a workflow: validate config -> fix PR -> staging deploy -> contract tests "
    "-> human approval -> promote -> smoke test",
]


class _Cap:
    enabled = True
    reason = None


class _Manifest:
    def resolve(self, *_a, **_k):
        return None

    def enabled(self, *_a, **_k):
        return True

    def disabled(self, *_a, **_k):
        return []

    def get(self, *_a, **_k):
        return _Cap()


@pytest.fixture(autouse=True)
def _no_ambient_model(monkeypatch):
    for name in ("OPENAI_API_KEY", "AZURE_OPENAI_KEY", "AZURE_OPENAI_ENDPOINT",
                 "AZURE_OPENAI_DEPLOYMENT", "BOSS_AGENT_ROUTER_LLM_ENABLED", "BOSS_LLM_ENGINE"):
        monkeypatch.delenv(name, raising=False)


def _router():
    return IntentRouter(Settings(_env_file=None, DATABASE_URL="postgresql://x/y",
                                 OPENAI_API_KEY="sk-test"))


def _route(router, message):
    return router.route_turn(None, project_id="p", actor="ceo",
                             messages=[{"role": "user", "content": message}],
                             manifest=_Manifest())


@pytest.mark.parametrize("message", ASKS)
def test_a_workflow_authoring_ask_reaches_the_agent_with_the_authoring_brief(message):
    """Decided as authoring, the turn goes to the tool-holding agent with writes WITHHELD (so
    the draft is OFFERED as a button) and never through the work-starting process compiler."""
    router = _router()
    seen = {}
    router._answer = lambda *_a, **k: (seen.update(k, action=router._turn_action),
                                       RouterTurn(reply="offered"))[1]
    router._dispatch = lambda *_a, **_k: pytest.fail("authoring must not start work")
    model = decided(router, "author_workflow")
    turn = _route(router, message)
    assert seen["writes"] is False and seen["action"] == "author_workflow"
    assert turn.reply == "offered" and turn.decision["action"] == "author_workflow"
    # The decision model was offered authoring as a choice, in workflow terms.
    assert "author_workflow" in model.prompts[0] and "WORKFLOW" in model.prompts[0]


def test_the_authoring_brief_names_the_real_tools_and_the_button():
    note = _decision_note(types.SimpleNamespace(action="author_workflow", status="decided",
                                                reason="asked for a workflow"))
    for needle in ("list_capabilities", "draft_workflow", "propose_action", "draft_custom_step",
                   "config_deploy", "contract_test", "release_approval", "smoke_test",
                   "BOSS has no step that does"):
        assert needle in note, needle
    assert "author_workflow" in _DECIDED_MEANS
    assert turn_depth("author_workflow", None) == "deep"


def test_the_staged_proposal_maps_every_step_to_a_real_capability():
    """The offer the agent makes for the API-onboarding ask is a draft_workflow button whose
    steps are real capabilities (gwlint validation is an agent step), and the draft passes the
    same validation the builder applies."""
    steps = [
        {"id": "validate_config", "agent": True,
         "instructions": "run gwlint on the tenant config and write a report with a fix"},
        {"id": "fix_pr", "capability": "fix_pr", "title": "Fix tenant config"},
        {"id": "deploy_staging", "capability": "config_deploy"},
        {"id": "contract_tests", "capability": "contract_test", "cases": "contract/cases.yaml"},
        {"id": "approve_release", "capability": "release_approval"},
        {"id": "promote_production", "capability": "promote_config"},
        {"id": "smoke", "capability": "smoke_test", "url": "https://example.test/health"},
    ]
    from boss_engine.services import workflow_capabilities
    real = {c["key"] for c in workflow_capabilities.palette()}
    assert {s["capability"] for s in steps if "capability" in s} <= real
    offered = build_default_registry().get("propose_action").handler(
        types.SimpleNamespace(settings=_router().settings), project_id="p", actor="u",
        args={"tool": "draft_workflow", "label": "Draft API onboarding",
              "args": {"workflow_key": "api_onboarding", "name": "API onboarding",
                       "steps": [{k: v for k, v in s.items()
                                  if k in ("id", "capability", "agent", "instructions")}
                                 for s in steps]}})
    assert offered["ok"] and offered["proposed"] and offered["tool"] == "draft_workflow"
    assert "NOT executed" in offered["note"]
    assert offered["args"]["steps"][0]["agent"] is True
    spec = _spec_from_steps(steps, {"inputs": ["instructions", "title", "cases", "url"]})
    validate_workflow_spec(spec)


def test_a_start_work_ask_with_deploy_words_is_not_refused_by_the_compiler():
    """create_agent_process on delivery prose hands the ask to authoring, not to a refusal."""
    router = _router()
    seen = {}
    router._answer = lambda *_a, **k: (seen.update(k, action=router._turn_action),
                                       RouterTurn(reply="offered"))[1]
    router._dispatch = lambda *_a, **_k: (_ for _ in ()).throw(
        ProcessVocabularyError("BOSS's role-handoff builder has no step that does 'deploy'"))
    decided(router, "create_agent_process")
    turn = _route(router, ASKS[0])
    assert turn.reply == "offered"
    assert seen["action"] == "author_workflow" and seen["writes"] is False


@pytest.mark.parametrize("prose,word", [
    ("architect reviews the PRD, then deploy to staging, then captain builds", "deploy"),
    ("PM accepts the PRD, then contract tests run, then qa tests", "contract"),
    ("captain builds, then ship it", "ship"),
    ("architect reviews, then we migrate the database", "migrate"),
])
def test_the_compiler_refuses_helpfully_never_with_a_bare_unknown(prose, word):
    with pytest.raises(ProcessVocabularyError) as refused:
        compile_process(prose)
    message = str(refused.value)
    assert "unknown action" not in message and "unknown role" not in message
    assert f"has no step that does '{word}'" in message
    assert "workflow" in message and "custom step" in message


def test_the_refusal_names_the_closest_real_capabilities():
    with pytest.raises(ProcessVocabularyError) as refused:
        compile_process("architect reviews then deploy to staging then promote to production")
    assert "config_deploy" in str(refused.value) and "promote_config" in str(refused.value)


def test_a_genuinely_unsupported_verb_through_the_router_is_a_helpful_refusal():
    """With a real dispatch, a verb with no capability that survives to the compiler ends as
    the authoring turn (which says what BOSS can do), not as an `unknown action` reply."""
    router = _router()
    seen = {}
    router._answer = lambda *_a, **k: (seen.update(k), RouterTurn(reply="no step does X; closest …"))[1]
    router._dispatch = lambda _c, name, args, **_k: (compile_process(args["text"]), None)[1]
    decided(router, "create_agent_process")
    turn = _route(router, "architect reviews the PRD, then refund the customer")
    assert "unknown" not in turn.reply.lower() and "closest" in turn.reply


def test_a_known_role_handoff_still_compiles():
    spec = compile_process("PRD reviewed by architect, then captain builds")
    assert spec["steps"]
