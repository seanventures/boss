"""Deployment failure states, and whether BOSS can hold a useful conversation about each.

Every deployment complaint so far has had the same shape: BOSS was RIGHT that it could not
deploy, and useless about why. "This app has never been deployed, so there is no build to
re-ship" was returned to an operator who already knew the app had a staging target and no
production one — a different problem, with different options, that BOSS had found a turn
earlier and dropped.

An agent's answer is only as good as the facts it is handed, so this drives the fact-builder
across the failure states an operator actually hits, and asserts each one produces:

  * WHAT EXISTS — the environments that are configured, by name;
  * WHAT IS MISSING — stated as the specific gap, not "not possible";
  * WHAT THEY CAN DO — at least one option that is real for that state.

No model needed: this is the input to the model, and it is where the last three bad answers
came from. Scenarios are added here as they are found in production, so a case that burned an
operator once cannot come back silently.
"""

import pytest

from boss_engine.services.shepherd_service import describe_undeployable

DONE = "completed"


# (id, status, targets, must_mention, must_offer)
SCENARIOS = [
    pytest.param(
        DONE,
        [{"environment": "staging", "status": "active"}],
        ["staging", "no production/live target"],
        ["deploy to an environment that DOES have a target", "create the missing production"],
        id="live-asked-for-but-only-staging-exists",   # Living Colors, 2026-08-24
    ),
    pytest.param(
        DONE,
        [],
        ["NO deployment targets configured at all"],
        ["create a target"],
        id="no-targets-at-all",
    ),
    pytest.param(
        DONE,
        [{"environment": "production", "status": "paused"}],
        ["not usable", "production=paused"],
        ["re-activate that target", "deploy to another"],
        id="live-target-exists-but-is-paused",
    ),
    pytest.param(
        "cancelled",
        [{"environment": "production", "status": "active"}],
        ["build run is 'cancelled'", "REBUILD, which costs money"],
        ["deployment run itself"],
        id="target-is-fine-but-the-build-was-cancelled",
    ),
    pytest.param(
        "failed",
        [{"environment": "staging", "status": "active"},
         {"environment": "production", "status": "active"}],
        ["staging", "production", "build run is 'failed'"],
        ["deployment run itself"],
        id="both-targets-exist-but-the-build-failed",
    ),
    pytest.param(
        DONE,
        [{"environment": "staging", "status": "active"},
         {"environment": "preview", "status": "active"}],
        ["staging", "preview", "no production/live target"],
        ["create the missing production"],
        id="several-non-live-targets",
    ),
]


@pytest.mark.parametrize("status,targets,must_mention,must_offer", SCENARIOS)
def test_the_operator_is_told_what_exists_and_what_they_can_do(
    status, targets, must_mention, must_offer
):
    out = describe_undeployable(status=status, repo="paramg/boss-sandbox", targets=targets)
    for phrase in must_mention:
        assert phrase in out, f"never mentions {phrase!r}:\n{out}"
    assert any(o in out for o in must_offer), f"offers no real next step:\n{out}"


@pytest.mark.parametrize("status,targets,_m,_o", SCENARIOS)
def test_no_failure_state_answers_with_a_bare_refusal(status, targets, _m, _o):
    """"Not possible" with no state reads as BOSS not knowing — which is what made the operator
    stop trusting it. Every state must name an environment or say plainly there are none."""
    out = describe_undeployable(status=status, repo="r/r", targets=targets)
    named = [str(t["environment"]) in out for t in targets]
    assert (targets and all(named)) or (not targets and "NO deployment targets" in out)


@pytest.mark.parametrize("status,targets,_m,_o", SCENARIOS)
def test_the_deploy_is_not_a_build_guardrail_survives_every_state(status, targets, _m, _o):
    """The bug that started this: asked to deploy, BOSS silently started a rebuild. That rule
    has to hold in every branch, not just the one it was written for."""
    out = describe_undeployable(status=status, repo="r/r", targets=targets)
    assert "DO NOT substitute a build for a deploy" in out
    assert "propose_action" in out, "options must be offerable as buttons, not prose"


def test_a_completed_build_is_not_blamed():
    """A successful build must not be described as a reason deployment cannot happen — that
    misdirection is what sent the operator to retry a run that was already fine."""
    out = describe_undeployable(status=DONE, repo="r/r",
                                targets=[{"environment": "production", "status": "active"}])
    assert "REBUILD" not in out
    assert "deployment run itself" in out


@pytest.mark.parametrize("env", ["production", "prod", "live", "PRODUCTION"])
def test_live_is_recognised_by_any_of_its_names(env):
    """"live", "prod" and "production" are the same request; treating them as different is how
    an app with a production target gets told it has none."""
    out = describe_undeployable(status=DONE, repo="r/r",
                                targets=[{"environment": env, "status": "active"}])
    assert "no production/live target" not in out, f"{env!r} not recognised as live"


# --------------------------------------------------------------------------- #
# The capability the guardrails were standing in for
# --------------------------------------------------------------------------- #
def test_boss_can_perform_a_first_deployment():
    """Every "why don't the buttons work" report traced to the same hole: BOSS could create
    hosts and targets and could REDEPLOY, and had nothing that performs a deployment which has
    never happened. So "deploy it" had no executable action, the agent improvised markup for a
    button that could not exist, and each parser I wrote was treating the symptom."""
    from boss_engine.tools.builtin import build_discuss_read_registry, _DISCUSS_OPERATIONS_TOOLS
    from boss_engine.agent.engine_agent import EngineAgent

    assert "deploy_app" in build_discuss_read_registry().names()
    assert "deploy_app" in _DISCUSS_OPERATIONS_TOOLS, "registered but unreachable"
    assert "deploy_app" not in EngineAgent.PROPOSE_ONLY, (
        "an operator who said 'deploy it' has already authorised it")


def test_deploying_is_never_a_way_to_finish_a_build():
    """The substitution this whole area kept attempting: asked to deploy, start a rebuild."""
    import inspect
    from boss_engine.services import delivery_pipeline
    src = inspect.getsource(delivery_pipeline.deploy_build)
    assert "not a successful build" in src
    assert "rebuild first" in src


def test_a_first_deployment_cannot_invent_infrastructure():
    """It must refuse a missing target rather than create one — and say what DOES exist, so the
    operator is offered a real choice instead of a wall."""
    import inspect
    from boss_engine.services import delivery_pipeline
    src = inspect.getsource(delivery_pipeline.deploy_build)
    assert "INSERT INTO deployment_targets" not in src
    assert "environments_available" in src
    assert "no deployment targets at all" in src
