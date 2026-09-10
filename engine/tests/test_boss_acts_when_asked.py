"""Asked to fix it, BOSS fixes it.

An operator said "fix and retry the deployment" and received an analysis ending with "Let me
know if you'd like me to open a PR with the change and rerun the task."

Two reasons, and neither was a safety decision:
  1. There was NO deployment tool in the registry at all, so recommending was the only thing
     BOSS could do.
  2. The answer path passed `allow_writes=False`, so even the operational tools it IS trusted
     with were parked as "confirmation_required".

This is an autonomous system. Being asked to act and replying with advice is a failure of the
product, not a guardrail.

The boundary is the REGISTRY, not a flag: the discuss set carries every read tool plus five
operational writes, and `create_code_task` / `retry_code_task` — everything that starts work and
spends money — is deliberately absent.
"""

import inspect

from boss_engine.services.intent_router import IntentRouter

from ._source import prompt_text
from boss_engine.tools.builtin import (
    _DISCUSS_OPERATIONS_TOOLS,
    build_default_registry,
    build_discuss_read_registry,
)


def test_boss_has_a_way_to_retry_a_deployment():
    assert "redeploy" in build_default_registry().names()


def test_it_can_check_the_host_before_shipping_again():
    """Redeploying at a host that is down repeats the failure and learns nothing."""
    reg = build_default_registry()
    assert "preflight_deployment_target" in reg.names()
    assert reg.get("preflight_deployment_target").kind == "read"


def test_the_deployment_fix_is_reachable_from_a_question():
    assert "redeploy" in build_discuss_read_registry().names()
    assert "redeploy" in _DISCUSS_OPERATIONS_TOOLS


def test_the_answer_path_may_actually_act():
    """Told to fix something, BOSS fixes it. Still true — and now conditional on having been
    TOLD: asked "can you check why this is stuck?", it cancelled the build. See
    test_a_question_is_not_an_instruction. The flag is the same flag; what changed is that a
    question no longer sets it."""
    src = inspect.getsource(IntentRouter)
    # The PROPERTY — writes follow the question/instruction gate — not the step count that
    # happened to sit beside it. Making the budget configurable broke this test, which has
    # nothing to say about budgets.
    assert "allow_writes=not _looking_only" in src


def test_and_an_instruction_still_reaches_that_path():
    """The regression this pairs with. If the question gate ever widened to swallow orders,
    BOSS would be back to answering "let me know if you would like me to"."""
    from boss_engine.services.intent_router import asked_only_to_look

    for order in ("fix and retry the deployment", "redeploy it", "cancel that job", "go ahead"):
        assert asked_only_to_look(order) is False, order


def test_boss_can_recover_work_that_already_exists():
    """The requests an operator actually makes. Every one of these was previously a tool BOSS
    owned and was forbidden to call, which is why it could only ever recommend."""
    names = set(build_discuss_read_registry().names())
    for fix in ("retry_code_task", "extend_code_task", "steer_code_task", "cancel_code_task",
                "redeploy", "start_preview", "stop_preview", "decide", "waive_gates"):
        assert fix in names, f"asked to fix something, BOSS still cannot call {fix}"


def test_writing_the_fix_is_a_fix_tool():
    """A bug in a customer's app is repaired by WRITING CODE. Withholding create_code_task meant
    BOSS could diagnose an app bug and had no way to fix it — which was the whole complaint.

    Two boundaries were invented here and both were wrong: first "don't spend money", then
    "recover but don't create". Each left BOSS unable to do the thing it had just been asked to
    do.
    """
    names = set(build_discuss_read_registry().names())
    for fix in ("create_code_task", "start_workflow", "register_repository"):
        assert fix in names, f"BOSS cannot write the fix: {fix} is unreachable"
    # NOT create_discovery / request_meeting. Not a third invented boundary — the opposite: a
    # bare research run or panel produces an artefact that belongs to no run, and the console
    # deliberately has no page for one. `start_workflow` starts the whole process, so research
    # and the panel still happen; they happen as steps, and the run is what the operator opens.
    for step_only in ("create_discovery", "request_meeting"):
        assert step_only not in names


def test_every_write_the_registry_has_is_reachable():
    """If a tool should not be reachable, remove it from the REGISTRY. Silently disarming it in
    the allow-list is how BOSS ended up owning eighteen tools and being allowed five.

    The exception is written down and enumerated — `_WORKFLOW_ONLY_TOOLS` — so withholding one
    is a decision on the record rather than a quiet edit to an allow-list.
    """
    from boss_engine.tools.builtin import _WORKFLOW_ONLY_TOOLS

    reg_writes = {s.name for s in build_default_registry().specs(kind="write")}
    unreachable = reg_writes - set(_DISCUSS_OPERATIONS_TOOLS) - set(_WORKFLOW_ONLY_TOOLS)
    assert not unreachable, f"unreachable writes: {sorted(unreachable)}"


def test_boss_does_not_patch_its_own_foundation():
    """The ONE distinction the product owner asked for. Not a permission BOSS lacks — a
    different subject. It reports that the framework needs a fix, names the cause, and a human
    changes it through the governed pipeline."""
    src = prompt_text(IntentRouter)
    assert "BOSS'S OWN FOUNDATION" in src
    assert "You do NOT patch yourself" in src
    assert "this is a BOSS framework bug" in src


def test_the_customer_side_has_no_advice_escape():
    src = prompt_text(IntentRouter)
    assert "There is no request in this category you should answer with advice" in src


def test_it_is_told_how_to_tell_them_apart():
    """A boundary the agent cannot APPLY is not a boundary."""
    src = prompt_text(IntentRouter)
    assert "if the defect is in the repo the run" in src


def test_the_operational_writes_are_a_closed_named_set():
    """A write reachable from chat must be a deliberate entry on this list, not an accident of
    being registered."""
    reg = build_discuss_read_registry()
    assert {s.name for s in reg.specs(kind="write")} == set(_DISCUSS_OPERATIONS_TOOLS)


def test_redeploy_reships_rather_than_rebuilds():
    """It must not quietly become a spend path: re-shipping an existing build runs no model."""
    src = inspect.getsource(build_default_registry)
    assert "redeploy" in src or True  # registration is asserted above
    spec = build_default_registry().get("redeploy")
    assert "does NOT rebuild" in spec.description
    assert "spend" in spec.description


def test_the_prompt_tells_it_to_act_not_to_offer():
    src = prompt_text(IntentRouter)
    assert "WHEN ASKED TO FIX SOMETHING, FIX IT" in src
    assert "let me know if you would like me to" in src, (
        "the phrase it kept ending replies with must be named, or it keeps using it")


def test_the_prompt_forbids_inventing_evidence():
    """It asserted `app.listen(3000, "127.0.0.1")` in a file that does not exist, about an app
    whose server reads $PORT/$HOST correctly. Acting on that would have changed working code."""
    src = prompt_text(IntentRouter)
    assert "NEVER INVENT EVIDENCE" in src
    assert "If you have not READ the line, do not" in src
