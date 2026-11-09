"""Every bundled template asks for the operator's own brief and says what it needs to run here.

The boss-1 map (2026-10-02) found the catalogue unstartable in practice:

  * no template declared an input_schema, so the console's Start sent `sample: true` and a run
    spent research, a panel and a build in the operator's repository on a random demo idea —
    a new-product brief even for Data Migration and Deprecation / Sunset;
  * nothing checked what a template needs before it started, so every template ending in
    `deploy` ran for hours on an install with no deployment host and failed at its last step,
    after a person had approved the release;
  * nine templates promised a person signs the PRD before anything is built, and the step was
    approved by `auto_approval` twenty seconds after the panel (wfr_9c78…);
  * gate approvers were agents no install has (product_owner, design_lead, tech_lead, ...).

These enumerate EVERY catalogue entry and EVERY start door, so a template or a door added later
is held to the same rules without anyone remembering to add it here.
"""

from __future__ import annotations

import ast
import pathlib
import re

import pytest
import yaml

from boss_engine.services import workflow_capabilities as caps
from boss_engine.services import workflow_catalogue as wc
from boss_engine.services import workflow_readiness as ready
from boss_engine.services import workflows

REPO = pathlib.Path(__file__).resolve().parents[2]
ENTRIES = {entry["key"]: entry for entry in wc.CATALOGUE}


@pytest.mark.parametrize("key", sorted(ENTRIES))
def test_every_template_asks_for_its_own_brief(key):
    """Start opens a form for the operator's work: every field a step reads is declared, with a
    title and a description, and required where a step cannot run without it."""
    entry = ENTRIES[key]
    assert key in wc.BRIEFS, f"{key} has no brief: Start would have nothing to ask"
    spec = wc.entry_spec(entry)
    schema = spec["input_schema"]
    # It is a schema the runtime accepts, and the one a published version carries.
    assert workflows.validate_workflow_spec(dict(spec))["input_schema"] == schema
    read, needed = wc._fields_read(entry["steps"])  # noqa: SLF001
    assert set(read) <= set(schema["properties"]), f"{key} reads undeclared inputs"
    assert needed <= set(schema["required"]), f"{key}: a step needs {needed} but Start does not"
    for name, prop in schema["properties"].items():
        assert str(prop.get("title") or "").strip(), f"{key}.{name} has no title"
        assert str(prop.get("description") or "").strip(), f"{key}.{name} has no description"
    directive = schema["properties"]["directive"]
    assert directive.get("format") == "textarea"
    assert directive.get("examples"), f"{key}: the brief has no example to show"
    # A start with only the required fields would get past the runtime's own check.
    filled = {name: "x" for name in schema["required"]}
    assert workflows.missing_required_inputs(spec, filled) == []
    assert workflows.missing_required_inputs(spec, {}) == sorted(schema["required"])


@pytest.mark.parametrize("key", sorted(ENTRIES))
def test_every_template_has_an_example_that_fits_it(key):
    """"Try an example" fills the form with the template's OWN example — not a product brief
    handed to a migration."""
    example = wc.example_for(key)
    assert example and example.get("title") and len(example.get("directive", "")) >= 40
    others = [wc.example_for(k)["directive"] for k in ENTRIES if k != key]
    assert example["directive"] not in others


def test_an_any_class_sample_is_the_templates_own_example():
    from boss_engine.services.workflow_samples import sample_for

    for key, entry in ENTRIES.items():
        if entry["work_class"] != "any":
            continue
        assert sample_for(None, "p", "any", workflow_key=key) == wc.example_for(key), key


#: Phrases that promised a PERSON approves the spec before anything is built. The step behind
#: them is answered by the panel's own decision, automatically by default.
_PROMISES = re.compile(
    r"(sign\w* off on it before anyone builds|reviewed by a (human|person)|"
    r"person reviews it before|human before any code)", re.I)
_SPEC = re.compile(r"\b(PRD|spec)\b", re.I)
_PERSON = re.compile(r"\b(person|human|someone)\b", re.I)


@pytest.mark.parametrize("key", sorted(ENTRIES))
def test_no_template_promises_a_person_signs_the_prd(key):
    """The PRD approval stays automatic (the owner's decision), so nothing a template says may
    promise a person signs it."""
    entry = ENTRIES[key]
    texts = [entry["name"], entry["description"], entry.get("objective") or "",
             *wc.BRIEFS[key]["directive"]]
    for text_ in texts:
        assert not _PROMISES.search(text_), (key, text_)
        for sentence in re.split(r"[.;—]", text_):
            assert not (_SPEC.search(sentence) and _PERSON.search(sentence)), (key, sentence)


def test_the_prd_approval_says_it_is_automatic_and_is_not_counted_as_yours():
    spec_review = caps.get("spec_review")
    # "(automatic)" where the install approves it itself; a person's sign-off under enforcement
    # is not called automatic (test_a_step_label_says_what_the_run_does).
    assert "automatic" in caps.label_for("spec_review", prd_auto_approve=True).lower()
    assert "automatic" not in caps.label_for("spec_review", prd_auto_approve=False).lower()
    assert "a person reads" not in spec_review.summary.lower()
    assert caps.stops_for_a_person("spec_review") is False
    for key in ("deploy", "ux_review", "security_review"):
        assert caps.stops_for_a_person(key) is True, key
    for summary in wc.catalogue_summary():
        assert "spec_review" not in summary["gates"], summary["key"]


def _default_roles() -> set[str]:
    pack = yaml.safe_load((REPO / "packages/specpack/packs/core_v1.yaml").read_text())
    return set((pack.get("installs") or {}).get("roles") or [])


@pytest.mark.parametrize("key", sorted(ENTRIES))
def test_every_agent_a_template_names_comes_with_a_default_install(key):
    """Approvers were product_owner, design_lead, security_lead, data_owner and tech_lead — no
    install has them, so the run's team named agents nobody could open (boss-1 project_agents:
    product_owner, release_manager)."""
    roles = {need.split(":", 1)[1] for need in ready.needs_of(wc.entry_spec(ENTRIES[key]))
             if need.startswith("role:")}
    assert roles <= _default_roles(), f"{key} names {sorted(roles - _default_roles())}"


class _NothingConnected:
    """A connection to a project that has nothing: every count is zero, every row absent."""

    class _Result:
        def scalar(self):
            return 0

        def first(self):
            return None

        def mappings(self):
            return self

        def all(self):
            return []

    def execute(self, *_a, **_k):
        return self._Result()

    def begin_nested(self):
        import contextlib

        return contextlib.nullcontext()


class _Settings:
    specpack_root = REPO / "packages" / "specpack"
    openai_api_key = ""
    target_github_repo = ""
    target_github_owner = ""
    github_token = ""
    email_smtp_host = None
    email_from_address = None
    email_real_send_allowed = False
    mattermost_real_send_allowed = False
    mattermost_base_url = ""
    mattermost_bot_token = ""


@pytest.mark.parametrize("key", sorted(ENTRIES))
def test_every_need_is_checked_and_comes_with_a_fix(key):
    """Each thing a template needs is checked, and when it is missing the operator is told what
    it is, which steps need it, and what to do — never a bare key."""
    spec = wc.entry_spec(ENTRIES[key])
    result = ready.preflight(_NothingConnected(), _Settings(), "p_empty", spec,
                             {"notify_channel": "email:team@example.com"})
    assert not result["ready"]
    assert {m["key"] for m in result["missing"]} >= {"model"}
    for need in result["missing"]:
        assert need["label"] and need["fix"] and need["detail"], need
        assert need["needed_by"], need
    assert "missing" in result["summary"]


def test_every_template_that_deploys_needs_somewhere_to_deploy():
    for key, entry in ENTRIES.items():
        steps = {(s.get("binding") or {}).get("capability") for s in entry["steps"]}
        needs = set(ready.needs_of(wc.entry_spec(entry)))
        if "deploy" in steps:
            assert "deployment_target" in needs, key
        if "build" in steps:
            assert "repository" in needs, key


def test_a_notify_channel_is_a_need_of_its_own_kind():
    assert caps.notify_channel_need("email:a@b.c") == "email"
    assert caps.notify_channel_need("mattermost:abc") == "mattermost"
    assert caps.notify_channel_need("slack:#x") == "channel:slack:#x"
    spec = wc.entry_spec(ENTRIES["standard_product_delivery"])
    with_email = {i["key"] for i in ready.requirements(spec, input_data={
        "notify_channel": "email:team@example.com"})}
    assert "email" in with_email
    assert "email" not in {i["key"] for i in ready.requirements(spec, input_data={})}


def test_skills_say_what_they_need_from_their_own_spec():
    """The example skills' needs come from their specs, not a list kept beside them."""
    specs = ready._skill_specs(str(REPO / "packages" / "specpack"))  # noqa: SLF001
    assert ready.skill_needs("publish_youtube", specs.get("publish_youtube")) >= {"google"}
    assert "work_tracker" in ready.skill_needs("tracker.issues.create",
                                               specs.get("tracker.issues.create"))
    assert "email" in ready.skill_needs("email.send_real_guarded",
                                        specs.get("email.send_real_guarded"))
    assert "model" in ready.skill_needs("model.generate_text", specs.get("model.generate_text"))
    assert "model" in ready.skill_needs("meeting.extract_actions",
                                        specs.get("meeting.extract_actions"))


def test_plrr_says_it_is_a_second_build():
    """PLRR extends the build into a second code task; the plan said 12 minutes and nothing
    about money."""
    plrr = caps.get("plrr")
    assert "second build" in plrr.cost_note
    assert plrr.typical_minutes >= 30
    standard = next(s for s in wc.catalogue_summary() if s["key"] == "standard_product_delivery")
    assert any(c["step"] == "plrr" for c in standard["cost_notes"])


# ── Every door that starts a run asks first ──────────────────────────────────────────────────

#: Doors that start a run without the preflight, and why that is right. Anything else that
#: calls `start_run` must call `refuse_unless_ready` (or the API's `_delivery_readiness_or_409`).
#: `start_project_run` is no longer here: it was exempt ("the build is already running"), yet the
#: run it starts is the whole catalogue pipeline, deploy included, so the MCP/chat path got no
#: warning while the console refused (review of the readiness change). It cannot refuse, so it
#: checks and NOTES (`note_unready_start`), which counts as asking.
EXEMPT = {
    ("engine/boss_engine/services/workflows.py", "_process_control_step"):
        "a subworkflow's child run; the parent's preflight included it "
        "(workflow_readiness.requirements recurses into subworkflow steps)",
    # identity/runs.py calls its OWN start_run: it mints a build's or tool call's run identity
    # (token, shim), not a workflow run. Same name, a different function.
    ("engine/boss_engine/identity/runs.py", "start_code_task_run"):
        "identity.runs.start_run issues the build's run identity, not a workflow run",
    ("engine/boss_engine/identity/runs.py", "repo_access"):
        "identity.runs.start_run issues a clone's run identity, not a workflow run",
    ("engine/boss_engine/identity/runs.py", "delegated"):
        "identity.runs.start_run issues a delegated act's run identity, not a workflow run",
    ("engine/boss_engine/services/workflow_test_runs.py", "start_test_run"):
        "a TEST-mode run: every spending step is pinned or refused, never deployed for real, "
        "so the readiness preflight (built for a real delivery) does not apply",
    ("engine/boss_engine/services/workflow_test_runs.py", "rerun_from_step"):
        "a real re-run's check is its caller's own `door` (the API's readiness/pre-check), "
        "called here before anything is written; a test re-run keeps the test path above",
}
_MODULE_ALIASES = {"wf", "workflows", "workflow_runtime"}
_CHECKS = ("refuse_unless_ready(", "_delivery_readiness_or_409(", "note_unready_start(")
#: A door that can start a run WITHOUT something must put that on the run's record.
_RECORDS = ("record_started_anyway(", "note_unready_start(")
#: Doors with nobody at the keyboard. A refusal there must be said where people look — the
#: event door used to refuse and only log.
_UNATTENDED = {
    ("engine/boss_engine/services/workflow_schedules.py", "run_due"),
    # dispatch_event itself no longer calls start_run: it delegates to
    # start_workflow_on_grant, which makes the same preflight/refusal under its own name.
}


def _start_run_callers() -> list[tuple[str, str, str]]:
    found = []
    roots = [REPO / "engine" / "boss_engine", REPO / "apps" / "api" / "app",
             REPO / "apps" / "worker"]
    for root in roots:
        for path in root.rglob("*.py"):
            if "tests" in path.parts:
                continue
            source = path.read_text(encoding="utf-8")
            if "start_run(" not in source:
                continue
            tree = ast.parse(source)
            for func in ast.walk(tree):
                if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                for node in ast.walk(func):
                    if not isinstance(node, ast.Call):
                        continue
                    target = node.func
                    is_runtime = (
                        (isinstance(target, ast.Name) and target.id == "start_run")
                        or (isinstance(target, ast.Attribute) and target.attr == "start_run"
                            and isinstance(target.value, ast.Name)
                            and target.value.id in _MODULE_ALIASES))
                    if is_runtime:
                        rel = str(path.relative_to(REPO))
                        found.append((rel, func.name, ast.get_source_segment(source, func)))
    # The innermost function only: a nested def is reported once, as itself.
    innermost: dict[tuple[str, str], str] = {}
    for rel, name, body in found:
        innermost[(rel, name)] = body
    return [(rel, name, body) for (rel, name), body in innermost.items()]


def test_every_door_that_starts_a_run_checks_readiness_first():
    callers = _start_run_callers()
    names = {(rel, name) for rel, name, _ in callers}
    # The enumeration found the doors it must: HTTP, the agent's tool, schedules, events.
    assert ("apps/api/app/main.py", "start_delivery_workflow") in names
    assert ("engine/boss_engine/tools/builtin.py", "start_workflow") in names
    assert ("engine/boss_engine/services/workflow_schedules.py", "run_due") in names
    unchecked = [
        f"{rel}:{name}" for rel, name, body in callers
        if (rel, name) not in EXEMPT and not any(check in body for check in _CHECKS)
    ]
    assert not unchecked, (
        "these start a workflow run without asking whether the project can run it: "
        f"{unchecked}. Call workflow_readiness.refuse_unless_ready first, or add the door to "
        "EXEMPT with the reason it cannot apply.")
    stale = [door for door in EXEMPT if door not in names]
    assert not stale, f"exempt doors that no longer exist: {stale}"
    # A start that went ahead without something says so on the run, at every door.
    unrecorded = [
        f"{rel}:{name}" for rel, name, body in callers
        if (rel, name) not in EXEMPT and not any(r in body for r in _RECORDS)
    ]
    assert not unrecorded, (
        f"these doors can start a run without something and do not record it: {unrecorded}. "
        "Call workflow_readiness.record_started_anyway with the preflight's result.")


def test_every_unattended_door_says_why_it_did_not_start():
    """Schedules and event subscriptions: a refusal is a `workflow.start_refused` event naming
    the door, and the operator's accepted exceptions are honoured."""
    sources = {(rel, name): body for rel, name, body in _start_run_callers()}
    for door in _UNATTENDED:
        assert door in sources, f"{door} no longer starts runs; update _UNATTENDED"
        body = sources[door]
        assert "record_refused(" in body or "_refused(" in body, door
        assert "start_anyway=" in body, f"{door} cannot honour an accepted exception"
    # The schedule helper is the one that records.
    schedules = (REPO / "engine/boss_engine/services/workflow_schedules.py").read_text()
    assert "record_refused(" in schedules
