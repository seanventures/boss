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

import ast
import inspect

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


# --------------------------------------------------------------------------- #
# One repo, many apps — never guess which one
# --------------------------------------------------------------------------- #
def test_deploy_refuses_when_the_target_is_ambiguous():
    """Asked to deploy Living Colors, it shipped that build to Invoice-Chaser's port and
    reported "succeeded". Target resolution matched on repo + environment and took
    ORDER BY created_at DESC LIMIT 1 — and every app in this project shares one repo and one
    environment, so it resolved to whichever target was created most recently.

    A wrong deployment that reports success is the worst outcome available here, so ambiguity
    must be a refusal that lists the candidates."""
    from boss_engine.services import delivery_pipeline
    from engine.tests._source import code_only
    # code_only strips comments AND docstrings. Without it this matched the comment explaining
    # the bug, which is the sixth time a test in this suite has asserted on prose rather than
    # behaviour. The helper exists precisely for this; use it.
    code = code_only(delivery_pipeline.deploy_build)
    src = inspect.getsource(delivery_pipeline.deploy_build)
    assert "ORDER BY created_at DESC LIMIT 1" not in code, "still silently picking the newest"
    assert "candidates" in src, "an ambiguous target does not report the options"
    assert "candidates" in src, "an ambiguous target does not report the options"
    assert "app_name" in src, "the app is still not part of target resolution"
    # AMBIGUOUS AND MISSING ARE DIFFERENT ANSWERS, and conflating them is what turned a routine
    # first deployment into an incident. Every app in this project is built into ONE repo, so
    # "N targets share this repo" fired whenever an app simply had no target of its own — and
    # it named the other apps as CANDIDATES, so BOSS spent two turns working out whether to
    # deploy a change calendar onto Meeting cost meter's port.
    assert "has no" in src and "deployment target of its own" in src
    assert "other_apps_targets" in src, "other apps are still offered as candidates"
    assert "create_deployment_target" in src, (
        "the refusal does not name the tool that fixes it — 'pass target_id' is advice that "
        "cannot be followed when no target for this app exists")
    # Identity, not substring. `name in app_name.lower()` matched a target called "Change" to an
    # app called "Change Calendar", and matched nothing at all when app_name fell back to the
    # operator's whole directive.
    assert "identity_key" in code


def test_an_explicit_target_id_is_always_honoured():
    """The escape hatch the refusal points at has to work."""
    import inspect
    from boss_engine.services import delivery_pipeline
    src = inspect.getsource(delivery_pipeline.deploy_build)
    i = src.index("if target_id:")
    assert "WHERE id = :i AND project_id = :p" in src[i:i + 400], (
        "target_id is not resolved directly, or is not project-scoped"
    )


def test_deploy_build_reads_the_stored_app_name():
    """The 8af4142e fix matched on finalize_ctx.app_name, then never selected that column.

    Resolution fell through to the run title. A Living Colors build titled
    'Port Invoice-Chaser auth' uniquely matched the Invoice-Chaser target and
    shipped the wrong app — the exact failure the refusal was written to stop.
    """
    from boss_engine.services import delivery_pipeline
    src = inspect.getsource(delivery_pipeline.deploy_build)
    select = src[src.index("SELECT id, project_id"): src.index("if not run:")]
    assert "finalize_ctx" in select, "app_name resolution cannot see finalize_ctx"


def test_the_only_target_in_the_repo_is_not_assumed_to_be_this_app():
    """First-deploy of Living Colors, when Invoice-Chaser is the only staging target.

    8af4142e refused when several targets shared a repo. It still took
    `elif len(candidates) == 1: target = candidates[0]` when the name did not
    match. Every app in this project shares one repo, so a second app's first
    deploy is exactly one candidate — the first app — and that shipped Living
    Colors onto Invoice-Chaser's port and reported success.
    """
    from boss_engine.services.delivery_pipeline import pick_deploy_target

    only = [{"id": "deptgt_invoice", "app_name": "Invoice-Chaser"}]
    assert pick_deploy_target(only, "Living Colors") is None
    assert pick_deploy_target(only, "Port Invoice-Chaser auth") is None


def test_a_substring_is_not_a_unique_target_match():
    """`name in app_name` uniquely matched 'Colors' against 'Living Colors'."""
    from boss_engine.services.delivery_pipeline import pick_deploy_target

    assert pick_deploy_target(
        [{"id": "deptgt_colors", "app_name": "Colors"}],
        "Living Colors",
    ) is None


def test_an_exact_app_name_is_the_target():
    from boss_engine.services.delivery_pipeline import pick_deploy_target

    picked = pick_deploy_target(
        [
            {"id": "deptgt_invoice", "app_name": "Invoice-Chaser"},
            {"id": "deptgt_colors", "app_name": "Living Colors"},
        ],
        "Living Colors",
    )
    assert picked is not None
    assert picked["id"] == "deptgt_colors"


def test_hyphen_and_space_are_the_same_app_name():
    """Card display name 'Living Colors'; target stored as 'living-colors'."""
    from boss_engine.services.delivery_pipeline import pick_deploy_target

    picked = pick_deploy_target(
        [{"id": "deptgt_lc", "app_name": "living-colors"}],
        "Living Colors",
    )
    assert picked is not None
    assert picked["id"] == "deptgt_lc"


def test_deploy_build_selects_finalize_ctx_and_does_not_guess_the_only_target():
    """finalize_ctx.app_name is the stored identity; a lone candidate is not a match."""
    from boss_engine.services import delivery_pipeline
    from engine.tests._source import code_only

    select = inspect.getsource(delivery_pipeline.deploy_build)
    select = select[select.index("SELECT id, project_id"): select.index("if not run:")]
    assert "finalize_ctx" in select, "app_name resolution cannot see finalize_ctx"
    code = code_only(delivery_pipeline.deploy_build)
    assert "len(candidates) == 1" not in code, "still shipping to the only target in the repo"
    assert "pick_deploy_target" in code


# --------------------------------------------------------------------------- #
# Binding a target must be undoable
# --------------------------------------------------------------------------- #
def test_the_console_can_unbind_a_target():
    """"If I already bound the port there's no way to unbind." True: DELETE
    /v1/deployment-targets/{id} has existed all along and nothing in the console called it, so
    a wrong binding — this app sitting on another app's port — could only be undone in the
    database. Retry was the ONLY control offered, and a deployment that fails because the
    target is wrong fails identically on every retry."""
    import pathlib
    route = pathlib.Path("apps/web/app/api/deployment-targets/route.ts")
    assert route.exists(), "no console route for retiring a target"
    body = route.read_text(encoding="utf-8")
    assert "export async function DELETE" in body
    assert "/v1/deployment-targets/" in body

    ui = pathlib.Path("apps/web/app/code-tasks-ui.tsx").read_text(encoding="utf-8")
    assert "app-deployment-unbind" in ui, "no unbind control beside Retry"
    assert "app-target-retire" in ui, "no unbind control on an unshipped binding"


def test_the_unbind_control_has_the_id_it_needs():
    import pathlib
    """The first version of this shipped disabled forever: the deployments feed did not return
    deployment_target_id, so `disabled={!latest.deployment_target_id}` was always true. A
    control that cannot fire is worse than no control — it looks like the feature exists."""
    main = pathlib.Path("apps/api/app/main.py").read_text(encoding="utf-8")
    i = main.index('@app.get("/v1/code-tasks/{run_id}/deployments")')
    body = main[i:i + 2000]
    assert "r.deployment_target_id" in body, "the id the unbind button needs is not selected"


def test_the_card_follows_the_binding_not_the_deployment_history():
    """"Unbind target is not working." It WAS working — the target showed status=retired in the
    database and the button's own message said "target retired". The card was wrong: it decided
    whether an app was bound by asking whether any deployment RUN existed, so an app that had
    ever deployed kept showing "DEPLOY FAILED · hetzner-ci-01" and a Retry button aimed at a
    target that no longer existed.

    Deployment runs are permanent history; a retired target is current state."""
    import pathlib
    ui = pathlib.Path("apps/web/app/code-tasks-ui.tsx").read_text(encoding="utf-8")
    assert "if (!latest || (loaded && !targetId))" in ui, (
        "the card still decides boundness from deployment history"
    )
    assert "ct-deploy-history" in ui, "the old deployment is not shown as history"
    # and it must not flash "not bound" before the binding has been read
    assert "const [loaded, setLoaded]" in ui, "no guard against the pre-load render"


def test_the_deploy_row_cannot_paint_outside_its_card():
    """Adding the Unbind button widened the row; with a long failure reason beside it, the row
    overflowed and overlapped the neighbouring card."""
    import pathlib
    css = pathlib.Path("apps/web/app/globals.css").read_text(encoding="utf-8")
    i = css.rindex(".ct-deploy-reason {")
    block = css[i:css.index("}", i)]
    assert "flex: 1 1" in block and "min-width: 0" in block, (
        "the failure reason is still sized by its content"
    )
    assert ".ct-card, .ct-deploy-wrap { overflow: hidden; }" in css


def test_no_card_button_waits_on_a_route_re_render():
    """"Set target" showed "Saving…" for minutes. The write takes milliseconds — the wait was
    the REPLY: a Server Action re-renders the current route to respond, setupDeploymentAction
    also called revalidatePath("/"), and code_tasks has a measured TTFB of ~115s. Two full page
    renders for a one-row write."""
    import pathlib
    import re
    ui = pathlib.Path("apps/web/app/code-tasks-ui.tsx").read_text(encoding="utf-8")
    code = re.sub(r"/\*.*?\*/", "", ui, flags=re.S)
    code = re.sub(r"^\s*//.*$", "", code, flags=re.M)
    for gone in ("setupDeploymentAction", "deployRunNowAction", "retryDeploymentAction"):
        assert gone not in code, f"{gone} still re-renders the route to reply"
    assert "appDeployOp(" in code
    assert pathlib.Path("apps/web/app/api/app-deploy/route.ts").exists()


def test_no_route_handler_invents_an_actor():
    """"Authenticated principal 'ceo' is not allowed to act as 'operator'." My route handlers
    defaulted actor to "operator" when the caller sent none — and the card never sends one.
    Claiming a principal you are not is impersonation even by accident, and the API is right to
    refuse it. Omitting the field attributes the action to whoever is authenticated."""
    import pathlib
    import re
    for name in ("app-deploy", "builds", "discoveries"):
        f = pathlib.Path(f"apps/web/app/api/{name}/route.ts")
        if not f.exists():
            continue
        code = re.sub(r"/\*.*?\*/", "", f.read_text(encoding="utf-8"), flags=re.S)
        code = re.sub(r"^\s*//.*$", "", code, flags=re.M)
        assert '"operator"' not in code, f"{name} still invents an actor"


def test_the_deploy_controls_send_the_operator_s_own_actor():
    """Two failures in a row on the same field. Sending actor="operator" was rejected —
    "Authenticated principal 'ceo' is not allowed to act as 'operator'" — so I removed the
    default entirely and got "actor is required". The field is mandatory AND must name a real
    principal; the only honest value is the one the console is operating as.

    Verified against the live API: actor=operator is refused, actor=executive_console is
    accepted."""
    import pathlib
    ui = pathlib.Path("apps/web/app/code-tasks-ui.tsx").read_text(encoding="utf-8")
    for fn in ("function SetupDeployment(", "function DeploymentLine("):
        i = ui.index(fn)
        assert "actor" in ui[i:i + 220], f"{fn} does not receive the actor"
    # every op must carry it
    for op in ('op: "set-target"', 'op: "deploy"', 'op: "retry"'):
        i = ui.index(op)
        assert "actor" in ui[i - 120:i + 260], f"{op} is sent without an actor"
    assert "retireTarget(targetId, actor)" in ui
    assert "retireTarget(String(tid), actor)" in ui


def test_console_first_deploy_does_not_hit_the_target_registry():
    """The card's Deploy now button posted to POST /v1/deployments.

    That path is the target registry. After first-deploy moved off it, posting
    there either 400s (no app_name) or creates a destination instead of shipping.
    """
    import pathlib
    route = pathlib.Path("apps/web/app/api/app-deploy/route.ts").read_text(encoding="utf-8")
    assert '"/v1/delivery/first-deploy"' in route
    deploy = route[route.index("deploy:"): route.index("retry:")]
    assert "/v1/deployments" not in deploy


# --------------------------------------------------------------------------- #
# Health gate must actually probe — a skipped check is not a pass
# --------------------------------------------------------------------------- #
def _observe(target):
    from boss_engine.config import Settings
    from boss_engine.services.deployment_connectors import SSHContainerConnector
    return SSHContainerConnector().observe(target, Settings())


def test_observe_probes_the_health_check_set_target_writes():
    """Set target stored {scheme, path, expect_status} with no kind.

    observe() required kind=='http', skipped the only check, and returned healthy=True.
    Every first-deploy from the card then reported success without asking the public URL —
    the exact 'succeeded when it did not' outcome this pipeline exists to prevent.

    Trigger: bind a host on the app card, click Deploy now. apply() may pass a localhost
    curl; observe() never fired; the row said succeeded.
    """
    from unittest.mock import MagicMock, patch

    target = {
        "domains": ["203.0.113.10:3010"],
        "health_checks": [{"scheme": "http", "path": "/", "expect_status": 200}],
        "target_ref": "root@203.0.113.10",
        "app_name": "Living Colors",
        "repo": "boss-sandbox",
    }
    resp = MagicMock()
    resp.status = 200
    opener = MagicMock()
    opener.return_value.__enter__.return_value = resp
    with patch("urllib.request.urlopen", opener):
        result = _observe(target)
    assert opener.called, "observe skipped the Set-target check (missing kind=http)"
    assert result.get("healthy") is True
    assert not any(c.get("skipped") for c in result.get("checks") or [])


def test_observe_does_not_succeed_when_the_public_url_is_down():
    """The complement: a probe that fails must fail the deployment, not skip-and-pass."""
    from unittest.mock import patch

    target = {
        "domains": ["203.0.113.10:3010"],
        "health_checks": [{"scheme": "http", "path": "/", "expect_status": 200}],
        "target_ref": "root@203.0.113.10",
        "app_name": "Living Colors",
        "repo": "boss-sandbox",
    }
    with patch("urllib.request.urlopen", side_effect=TimeoutError("timed out")):
        result = _observe(target)
    assert result.get("healthy") is False, (
        f"a down public URL was treated as healthy: {result}"
    )


def test_observe_does_not_succeed_when_every_check_is_skipped():
    """An unsupported check must not default to a healthy deployment."""
    from unittest.mock import patch

    target = {
        "domains": ["example.test"],
        "health_checks": [{"kind": "unknown"}],
    }
    with patch("urllib.request.urlopen") as opener:
        result = _observe(target)
    assert not opener.called
    assert result.get("healthy") is not True, (
        f"skipped-only checks reported healthy: {result}"
    )


def test_set_target_writes_an_http_health_check():
    """The producer must write the shape observe() already understood, so a future
    reader does not have to infer kind from path/scheme again."""
    import pathlib
    from engine.tests._source import code_only

    main = pathlib.Path("apps/api/app/main.py").read_text(encoding="utf-8")
    endpoint = next(
        node for node in ast.parse(main).body
        if isinstance(node, ast.FunctionDef)
        and node.name == "setup_deployment_for_code_task_endpoint"
    )
    src = code_only(ast.get_source_segment(main, endpoint) or "")
    assert '"kind": "http"' in src or "'kind': 'http'" in src, (
        "Set target still writes health_checks without kind=http"
    )
