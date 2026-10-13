"""ONE connector policy on every path: a connector is pre-tested, so nothing a build reads asks
for its suite (templates.connector_policy).

Gate B (crun_2d097414, PR paramg/boss-sandbox#227) applied login-oauth to a new app. BOSS's own
directive said "do NOT copy its test files", but the template's brief said "add its two scripts
`pretest:login-oauth` and `test:login-oauth` ... unchanged" and "Copy tests/template_login_oauth/",
and the acceptance criteria said "the template's tests pass: npm run test:login-oauth". The
reviewer and the consistency seat both called the missing suite a BLOCKER. Now the brief, the
criteria, every seat's context, the gate and the plumbing gate say the same thing.
"""
from __future__ import annotations

import inspect
import re

import pytest

from boss_engine.services import governed_code_task_service as g
from boss_engine.templates import apply, connector_policy, service
from boss_engine.templates.validator import tree_from_dir, validate_tree

from ._fixtures import REPO_ROOT

CONNECTORS = ("login-oauth", "stripe-checkout", "paypal-checkout")

#: What the build that failed carried (crun_2d097414's stored acceptance_criteria).
OLD_CRITERIA = [
    "GET /auth/providers behaves as the template's tests specify",
    "GET /api/me behaves as the template's tests specify",
    "the template's tests pass: npm run test:login-oauth",
]

#: A brief that tells the agent to put the connector's suite into the app.
_ASKS_FOR_SUITE = re.compile(
    r"(?<!not )copy\s+`tests/|scripts `pretest:|two scripts `|test:login-oauth` to the app|"
    r"into the app's test sources|tests are the proof|Keep the template's tests", re.I)


def _connector(name):
    tree = tree_from_dir(REPO_ROOT / "templates" / "apps" / name)
    report = validate_tree(tree, source="builtin")
    return report.manifest, tree.files()


@pytest.mark.parametrize("name", CONNECTORS)
def test_the_builtin_connector_briefs_never_ask_for_the_suite(name):
    manifest, files = _connector(name)
    assert manifest.item_kind == "connector"
    brief = files["brief.md"].decode()
    assert not _ASKS_FOR_SUITE.search(brief), _ASKS_FOR_SUITE.search(brief).group(0)
    assert "pre-tested" in brief and "own test tier" in brief


@pytest.mark.parametrize("name", CONNECTORS)
def test_every_stacks_directive_carries_the_policy_and_no_suite(name):
    manifest, files = _connector(name)
    for stack in manifest.stacks:
        text = apply.build_directive(row={"source": "builtin"}, manifest=manifest, stack=stack,
                                     framework=None, files=files, note="", app_name="Shop")
        assert connector_policy.POLICY in text
        assert "`template_tests` gate" not in text
        assert not _ASKS_FOR_SUITE.search(text), (stack, _ASKS_FOR_SUITE.search(text).group(0))
        # The suite is neither inlined nor listed as reference material.
        assert "### tests/" not in text


def test_an_imported_connectors_own_words_are_overridden_outside_its_envelope():
    """An imported brief may still say "copy the tests": the policy is BOSS's, outside the
    untrusted envelope, and says it overrides the template's own material."""
    manifest, files = _connector("login-oauth")
    files = dict(files)
    files["brief.md"] = b"Copy tests/template_login_oauth/ into the app and add test:login-oauth."
    text = apply.build_directive(row={"source": "git"}, manifest=manifest, stack="node",
                                 framework=None, files=files, note="", app_name=None)
    assert text.index(connector_policy.POLICY) < text.index(apply.UNTRUSTED_BEGIN)
    assert "overrides anything the template's brief" in connector_policy.POLICY


def test_the_criteria_name_the_wiring_never_the_suite():
    manifest, _ = _connector("login-oauth")
    crit = connector_policy.acceptance_criteria(manifest.routes)
    assert not any(connector_policy.requires_suite(c) for c in crit)
    assert any("GET /auth/providers" in c and "answers" in c for c in crit)
    # apply.plan (every entry path's planner) takes them from here for a connector.
    src = inspect.getsource(apply.plan)
    assert "connector_policy.acceptance_criteria(manifest.routes)" in src


def test_every_entry_path_plans_through_apply_plan():
    """The composer's New app and Add a feature (the apply sheet), POST /v1/templates/{id}/apply
    and the apply_template tool all build their run input with apply.plan; a retry re-runs that
    spec and is normalised at run start (below)."""
    from boss_engine.tools import app_platform_tools

    routes = (REPO_ROOT / "apps" / "api" / "app" / "routes_templates.py").read_text()
    assert "apply_service.plan(" in routes
    assert "apply_service.plan(" in inspect.getsource(app_platform_tools)


def test_a_retry_of_an_old_build_runs_under_the_policy():
    manifest, _ = _connector("login-oauth")
    instructions, criteria = connector_policy.normalize(
        "Integrate the connector ...", OLD_CRITERIA + ["the buddy dashboard lists paths"],
        manifest.routes)
    assert instructions.startswith(connector_policy.POLICY)
    assert not any(connector_policy.requires_suite(c) for c in criteria)
    assert "the buddy dashboard lists paths" in criteria
    assert any("is mounted in the app's own router" in c for c in criteria)
    # Idempotent: a second pass changes nothing.
    assert connector_policy.normalize(instructions, criteria, manifest.routes) == (instructions,
                                                                                  criteria)


def test_the_pipeline_normalises_connector_builds_and_only_them(monkeypatch):
    manifest, _ = _connector("login-oauth")
    monkeypatch.setattr(g, "_applied_connector", lambda engine, run: manifest)
    ins, crit = g.connector_build_input(None, {}, "brief", OLD_CRITERIA)
    assert connector_policy.POLICY in ins and not any(connector_policy.requires_suite(c) for c in crit)
    monkeypatch.setattr(g, "_applied_connector", lambda engine, run: None)
    assert g.connector_build_input(None, {}, "brief", OLD_CRITERIA) == ("brief", OLD_CRITERIA)
    # It runs where the spec is read, before any stage builds a prompt.
    src = inspect.getsource(g._run_governed_code_task)
    read = src.index('criteria = spec.get("acceptance_criteria", [])')
    assert src.index("connector_build_input(engine, run, instructions, criteria)") - read < 200


def test_every_seat_reads_the_policy_as_its_checklist():
    """The reviewer, the security and consistency seats and QA all get _ctx_block: the policy
    (with its "never a finding" line) and the wiring criteria are in each one's context."""
    manifest, _ = _connector("login-oauth")
    instructions, criteria = connector_policy.normalize("brief", OLD_CRITERIA, manifest.routes)
    prompts = [
        g._review_prompt("t", criteria, instructions=instructions, change=""),
        g._security_prompt("t", instructions=instructions, criteria=criteria, change=""),
        g._consistency_prompt("t", criteria, {}, instructions=instructions, change=""),
    ]
    for prompt in prompts:
        assert "absence of the connector's test files or test scripts is correct" in prompt
        assert "template's tests pass" not in prompt


def test_the_bundled_connectors_still_pass_their_own_tier_shape():
    """The suite still exists, in the template, with its scripts: that is its own test tier."""
    for name in CONNECTORS:
        manifest, files = _connector(name)
        for stack in manifest.stacks:
            assert any(p.startswith(f"stacks/{stack}/tests/") for p in files), (name, stack)
    assert any(r.manifest and r.manifest.id == "login-oauth" for _n, r, _f in service.builtins())
