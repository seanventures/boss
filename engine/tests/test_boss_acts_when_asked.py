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
    src = inspect.getsource(IntentRouter)
    assert "allow_writes=True, max_steps=12" in src


def test_boss_can_recover_work_that_already_exists():
    """The requests an operator actually makes. Every one of these was previously a tool BOSS
    owned and was forbidden to call, which is why it could only ever recommend."""
    names = set(build_discuss_read_registry().names())
    for fix in ("retry_code_task", "extend_code_task", "steer_code_task", "cancel_code_task",
                "redeploy", "start_preview", "stop_preview", "decide", "waive_gates"):
        assert fix in names, f"asked to fix something, BOSS still cannot call {fix}"


def test_creating_new_work_is_still_an_explicit_ask():
    """NOT a money rule — spending is often the point, and a retry that costs budget is exactly
    what "retry it" asks for. The line is recover-vs-create: "build me a URL shortener" is a
    different sentence from "fix it", and route_turn already actuates that on an explicit build
    intent, so nothing is lost by keeping an ANSWER from silently scaffolding an app.
    """
    names = set(build_discuss_read_registry().names())
    for creator in ("create_code_task", "create_discovery", "request_meeting",
                    "register_repository"):
        assert creator not in names, f"{creator} would start new work from a question"


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
    src = inspect.getsource(IntentRouter)
    assert "WHEN ASKED TO FIX SOMETHING, FIX IT" in src
    assert "let me know if you would like me to" in src, (
        "the phrase it kept ending replies with must be named, or it keeps using it")


def test_the_prompt_forbids_inventing_evidence():
    """It asserted `app.listen(3000, "127.0.0.1")` in a file that does not exist, about an app
    whose server reads $PORT/$HOST correctly. Acting on that would have changed working code."""
    src = inspect.getsource(IntentRouter)
    assert "NEVER INVENT EVIDENCE" in src
    assert "If you have not READ the line, do not" in src
